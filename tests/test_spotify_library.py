import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common import sha256_file
from main import create_app
from pipeline import Pipeline, write_playlist
from providers import Entry, Snapshot, parse_source
from runtime import DeferredOperation
from spotify_library import cancel_waiting, preview, queue_exact, queue_link
from test_reliability import active_track

PID, SID = "P" * 22, "S" * 22


def spotify_track(db, user="admin", title="Artist - Song", key=SID):
    source = db.add_source(parse_source("spotify:playlist:" + PID), user)
    entry = Entry(key, "https://open.spotify.com/track/" + key, title,
                  "2019-01-01T01:02:03Z", key, 0, "playlist_added", False)
    db.apply_snapshot(source, Snapshot("Spotify mix", [entry]))
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    return db.one("SELECT t.* FROM tracks t JOIN track_origins o ON o.track_id=t.id WHERE o.provider='spotify' AND o.user_id=? AND o.media_key=?", (user, key)), source, entry


def fake_comment(monkeypatch, pipeline, fail_first=False):
    calls = []
    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "comment"
        calls.append(current["id"])
        if fail_first and len(calls) == 1:
            raise OSError("injected metadata write failure")
        Path(current["file_path"]).write_bytes(b"audio-with-discovery-update")
        return {}
    monkeypatch.setattr(pipeline, "bridge", bridge)
    return calls


def test_explicit_link_reuses_audio_moves_memberships_and_keeps_dates(db, track, environment, monkeypatch):
    target, _, audio = active_track(db, track, environment)
    source_track, source, entry = spotify_track(db)
    pipeline = Pipeline(db); fake_comment(monkeypatch, pipeline)
    asset_id = target["current_asset_id"]
    queue_link(db, source_track["id"], "admin", target_id=target["id"])
    asyncio.run(pipeline.process_track(db.claim()))
    assert not db.one("SELECT id FROM tracks WHERE id=?", (source_track["id"],))
    final = db.owned("tracks", target["id"], "admin")
    assert final["mbid"] == target["mbid"] and final["current_asset_id"] == asset_id
    assert final["discovered_at"] == "2019-01-01T01:02:03Z"
    assert final["operation_state"] == "IDLE"
    assert db.current_asset(target["id"])["sha256"] == sha256_file(audio)
    exported = Path(db.owned("sources", source["id"], "admin")["playlist_path"])
    assert "song.opus" in exported.read_text()
    db.apply_snapshot(source, Snapshot("Spotify mix", [entry]))
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 1
    assert len(db.origins_for_track(target["id"])) == 2


def test_committed_link_retries_after_restart_and_locks_target(db, track, environment, monkeypatch):
    target, _, audio = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    pipeline = Pipeline(db); calls = fake_comment(monkeypatch, pipeline, True)
    queue_link(db, source["id"], "admin", target_id=target["id"])
    job = db.claim()
    with pytest.raises(DeferredOperation):
        asyncio.run(pipeline.process_track(job))
    with pytest.raises(ValueError, match="Spotify link"):
        db.queue_track(target["id"], "admin", {"mode": "delete"})
    db.recover()
    with pytest.raises(ValueError, match="Spotify link"):
        db.queue_track(target["id"], "admin", {"mode": "retag"})
    asyncio.run(pipeline.process_track(db.claim()))
    asyncio.run(pipeline.process_track(job))
    assert len(calls) == 2
    assert db.owned("tracks", target["id"], "admin")["operation_state"] == "IDLE"
    assert audio.exists()


def test_link_rejects_missing_or_changed_target_audio(db, track, environment):
    target, _, audio = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    queue_link(db, source["id"], "admin", target_id=target["id"])
    audio.unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        asyncio.run(Pipeline(db).process_track(db.claim()))
    assert db.origins_for_track(source["id"])[0]["provider"] == "spotify"


def test_link_rejects_stale_identity_and_cross_user(db, track, environment):
    target, _, _ = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    guest, _, _ = spotify_track(db, "guest")
    with pytest.raises(ValueError):
        queue_link(db, guest["id"], "guest", target_id=target["id"])
    with pytest.raises(ValueError, match="changed"):
        queue_link(db, source["id"], "admin", target_id=target["id"], expected={"mbid": "different"})
    queue_link(db, source["id"], "admin", target_id=target["id"])
    db.update("tracks", target["id"], mbid="different")
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(Pipeline(db).process_track(db.claim()))


def test_reviewed_bulk_match_is_scoped_and_stale_preview_rejected(db, track, environment):
    target, _, _ = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    spotify_track(db, "guest")
    matched = preview(db, "admin")
    assert matched["unique"] == 1 and matched["total"] == 1
    with pytest.raises(ValueError, match="changed"):
        queue_exact(db, "admin", "stale-revision")
    result = queue_exact(db, "admin", matched["revision"])
    assert result["queued"] == 1
    assert not db.one("SELECT 1 FROM jobs WHERE kind='track' AND user_id='guest' AND state='PENDING'")


def test_version_markers_are_not_stripped_from_name_matching(db, track, environment):
    active_track(db, track, environment)
    spotify_track(db, title="Artist - Song (Live)")
    assert preview(db, "admin")["unique"] == 0


def test_unlinked_spotify_is_not_a_missing_file_integrity_error(db):
    source, _, _ = spotify_track(db)
    pipeline = Pipeline(db)
    async def audit(*args, **kwargs):
        return {source["id"]: {"found": False}}
    pipeline.bridge_request = audit
    asyncio.run(pipeline.process_integrity({"id": 0, "target": "admin", "user_id": "admin"}))
    assert not db.integrity_report("admin")["issues"]
    assert db.owned("tracks", source["id"], "admin")["health"] == "UNAVAILABLE"


def test_youtube_origin_has_independent_jobs_then_links(db, track, environment, monkeypatch):
    target, origin, _ = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    pipeline = Pipeline(db); fake_comment(monkeypatch, pipeline)
    queue_link(db, source["id"], "admin", audio_url=origin["url"])
    job = db.claim()
    assert json.loads(job["payload"])["mode"] == "spotify_audio"
    asyncio.run(pipeline.process_track(job))
    assert len(db.origins_for_track(target["id"])) == 2


def test_new_audio_import_waits_without_downloading_spotify(db):
    source, _, _ = spotify_track(db)
    queue_link(db, source["id"], "admin", audio_url="https://youtu.be/abcdefghijk")
    jobs = db.rows("SELECT * FROM jobs WHERE state='PENDING' ORDER BY id")
    assert [job["kind"] for job in jobs] == ["sync", "track"]
    link = jobs[-1]
    with pytest.raises(DeferredOperation, match="syncing"):
        asyncio.run(Pipeline(db).process_track(link))
    assert db.owned("tracks", source["id"], "admin")["current_asset_id"] is None
    cancel_waiting(db, source["id"], "admin")
    assert db.one("SELECT state FROM jobs WHERE id=?", (link["id"],))["state"] == "CANCELLED"
    assert db.one("SELECT state FROM jobs WHERE id=?", (jobs[0]["id"],))["state"] == "PENDING"


def test_different_youtube_identity_requires_explicit_review(db, track, environment):
    target, origin, _ = active_track(db, track, environment)
    source, _, _ = spotify_track(db, title="Artist - Another Song")
    queue_link(db, source["id"], "admin", audio_url=origin["url"])
    with pytest.raises(ValueError, match="differs"):
        asyncio.run(Pipeline(db).process_track(db.claim()))
    assert db.origins_for_track(source["id"])[0]["provider"] == "spotify"


@pytest.mark.parametrize("url", ["spotify:track:" + SID, "https://youtube.com/playlist?list=PLtest", "file:///tmp/music.opus"])
def test_audio_link_rejects_unsupported_urls(db, url):
    source, _, _ = spotify_track(db)
    with pytest.raises(ValueError):
        queue_link(db, source["id"], "admin", audio_url=url)


def test_api_links_and_preview_do_not_bypass_ownership(db, track, environment):
    target, _, _ = active_track(db, track, environment)
    source, _, _ = spotify_track(db)
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get("/api/spotify/matches?user_id=admin").json()["unique"] == 1
        wrong = client.post(f"/api/spotify/entries/{source['id']}/link", json={"user_id": "guest", "target_id": target["id"]})
        assert wrong.status_code == 404
        good = client.post(f"/api/spotify/entries/{source['id']}/link", json={"user_id": "admin", "target_id": target["id"]})
        assert good.status_code == 200


def test_bulk_links_all_pages_without_downloading(db, track, environment):
    active_track(db, track, environment)
    source = db.add_source(parse_source("spotify:playlist:" + PID), "admin")
    entries = [Entry(f"{n:022d}", "https://open.spotify.com/track/" + f"{n:022d}",
                     "Artist - Song", None, str(n), n, "playlist_added", False) for n in range(120)]
    db.apply_snapshot(source, Snapshot("Many", entries))
    result = preview(db, "admin", page=2, limit=50)
    assert result["total"] == result["unique"] == 120
    assert len(result["items"]) == 50
    queued = queue_exact(db, "admin", result["revision"])
    assert queued["queued"] == 120
    assert all(json.loads(j["payload"])["mode"] == "spotify_link" for j in db.rows("SELECT payload FROM jobs WHERE kind='track' AND state='PENDING'"))
