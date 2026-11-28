"""Manual recovery of an unavailable video using verified existing music."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from runtime import DeferredOperation
from test_reliability import active_track


@pytest.fixture
def pair(db, track, environment):
    target, target_origin, audio = active_track(db, track, environment)
    db.update("tracks", target["id"], operation_state="IDLE", operation_kind=None)
    playlist = db.add_source(parse_source("https://youtube.com/playlist?list=PLolder"), "admin")
    db.apply_snapshot(playlist, Snapshot("Older playlist", [
        Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Artist - Original video",
              "2017-01-01T00:00:00Z", "old-entry", 0),
    ]))
    source_origin = db.one("SELECT * FROM track_origins WHERE media_key=?", ("lmnopqrstuv",))
    source = db.owned("tracks", source_origin["track_id"], "admin")
    reason = "Video unavailable, private, or removed. Choose another video."
    db.execute("UPDATE jobs SET state='DONE' WHERE target IN (?,?)", (source["id"], playlist["id"]))
    db.update("tracks", source["id"], operation_state="STOPPED", operation_error=reason)
    db.update("track_origins", source_origin["id"], availability="UNAVAILABLE", download_enabled=0,
              disabled_reason=reason, last_error=reason)
    return source, target, source_origin, target_origin, playlist, audio


def test_manual_merge_reuses_audio_and_preserves_discovery(db, pair, monkeypatch):
    source, target, old_origin, good_origin, playlist, audio = pair
    pipeline = Pipeline(db)
    comments = []

    async def bridge(job, track, directory, mode, **kwargs):
        assert mode == "comment"
        assert track["id"] == target["id"]
        comments.append(track["discovered_at"])
        assert kwargs["path"] == str(audio)
        return {}

    async def never(*args, **kwargs):
        raise AssertionError("Local merge must never download")

    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", never)
    with TestClient(create_app(db, start_workers=False)) as client:
        listing = client.get("/api/tracks", params={"user_id": "admin", "status": "ATTENTION"}).json()
        assert next(t for t in listing["tracks"] if t["id"] == source["id"])["can_merge_local"]
        url = f"/api/tracks/{source['id']}/merge-local"
        found = client.get(url + "/candidates", params={"user_id": "admin", "q": "Artist - Song"})
        assert found.status_code == 200
        assert [row["id"] for row in found.json()["tracks"]] == [target["id"]]
        response = client.post(url, json={"user_id": "admin", "target_id": target["id"]})
        assert response.status_code == 200, response.text
        with pytest.raises(ValueError):
            db.queue_track(target["id"], "admin", {"mode": "comment"})
    job = db.claim()
    assert json.loads(job["payload"])["mode"] == "merge_local"
    asyncio.run(pipeline.process_track(job))
    db.finish_job(job["id"])
    assert not db.one("SELECT id FROM tracks WHERE id=?", (source["id"],))
    current = db.owned("tracks", target["id"], "admin")
    assert current["mbid"] == target["mbid"]
    assert current["current_asset_id"] == target["current_asset_id"]
    assert current["discovered_at"] == "2017-01-01T00:00:00Z"
    assert comments == ["2017-01-01T00:00:00Z"]
    origins = {o["id"]: o for o in db.origins_for_track(target["id"])}
    assert set(origins) == {old_origin["id"], good_origin["id"]}
    assert origins[old_origin["id"]]["download_enabled"] == 0
    assert audio.read_bytes() == b"original-audio"
    exported = db.owned("sources", playlist["id"], "admin")["playlist_path"]
    assert "#EXTINF" in Path(exported).read_text()
    # Receipt replay after a restart must be harmless.
    asyncio.run(pipeline.finish_operation_receipt(job, db.operation_receipt(json.loads(job["payload"])["operation"]),
                                                 db.path.parent / "staging" / source["id"]))


def test_manual_merge_refuses_invalid_or_busy_destination(db, pair):
    source, target, *_ = pair
    with pytest.raises(ValueError):
        db.queue_local_merge(source["id"], source["id"], "admin")
    with pytest.raises(LookupError):
        db.queue_local_merge(source["id"], "not-a-track", "admin")
    db.queue_track(target["id"], "admin", {"mode": "comment"})
    with pytest.raises(ValueError):
        db.queue_local_merge(source["id"], target["id"], "admin")


def test_manual_merge_rechecks_destination_bytes(db, pair):
    source, target, *_, audio = pair
    db.queue_local_merge(source["id"], target["id"], "admin")
    audio.write_bytes(b"externally changed")
    with pytest.raises(ValueError, match="missing or changed"):
        asyncio.run(Pipeline(db).process_track(db.claim()))
    assert db.owned("tracks", source["id"], "admin")
    assert len(db.origins_for_track(target["id"])) == 1


def test_manual_merge_recovers_interrupted_cleanup(db, pair, monkeypatch):
    source, target, *_ = pair
    pipeline = Pipeline(db)
    attempts = []

    async def bridge(job, track, directory, mode, **kwargs):
        attempts.append(mode)
        if len(attempts) == 1:
            raise RuntimeError("injected cleanup interruption")
        return {}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_local_merge(source["id"], target["id"], "admin")
    job = db.claim()
    with pytest.raises(DeferredOperation, match="cleanup will retry"):
        asyncio.run(pipeline.process_track(job))
    receipt = db.operation_receipt(json.loads(job["payload"])["operation"])
    assert receipt["finalized_at"] is None
    assert len(db.origins_for_track(target["id"])) == 2
    with pytest.raises(ValueError):
        db.queue_track(target["id"], "admin", {"mode": "comment"})
    asyncio.run(pipeline.process_track(job))
    assert not db.one("SELECT id FROM tracks WHERE id=?", (source["id"],))
    assert db.operation_receipt(receipt["operation_id"])["finalized_at"]
