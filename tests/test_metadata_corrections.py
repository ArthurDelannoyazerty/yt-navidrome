"""Source recovery, manual identity selection and diagnostic regressions."""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.oggopus import OggOpus

from common import atomic_json, sha256_file
from main import create_app
from metadata_routes import metadata_context
from pipeline import Pipeline
from source_metadata import capture_source, clear_descriptive_tags, correction_revision, snapshot, source_choice
from test_reliability import active_track, MBID

OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def opus(tmp_path):
    path = tmp_path / "source.opus"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", str(path)], check=True)
    return path


def correction(track, mode="restore_source", **extra):
    return {"mode": mode, "revision": correction_revision(track), **extra}


def test_capture_is_pristine_immutable_and_deleted_with_origin(db, track, opus):
    origin = db.origins_for_track(track["id"])[0]
    raw = OggOpus(opus); raw["title"] = ["Actual song"]; raw["artist"] = ["Actual artist"]
    raw["album"] = ["Original album"]; raw.save()
    saved = capture_source(db, origin, opus)
    assert saved["basis"] == "original_download_tags"
    raw = OggOpus(opus); raw["title"] = ["Wrong song"]; raw["musicbrainz_trackid"] = [MBID]; raw.save()
    assert capture_source(db, origin, opus) == saved
    assert snapshot(db, origin)["tags"]["title"] == ["Actual song"]
    db.delete_track(track["id"], "admin")
    assert not db.rows("SELECT * FROM origin_metadata")


def test_processed_library_tags_are_never_archived_as_source(db, track, opus):
    origin = db.origins_for_track(track["id"])[0]
    raw = OggOpus(opus); raw["title"] = ["Wrong song"]; raw["musicbrainz_trackid"] = [MBID]; raw.save()
    saved = capture_source(db, origin, opus)
    assert saved["basis"] == "provider_title_only"
    assert saved["tags"] == {"artist": ["Artist"], "title": ["Song"]}


def test_legacy_source_preview_uses_origin_not_bad_current_tags(db, track, environment):
    track, origin, _ = active_track(db, track, environment)
    db.update("tracks", track["id"], matched_title="Wrong artist - Wrong song")
    data = metadata_context(db, track["id"], "admin")
    assert data["current_title"] == "Wrong artist - Wrong song"
    assert data["source"]["tags"] == {"artist": ["Artist"], "title": ["Song"]}
    assert data["source"]["basis"] == "provider_title_only"
    assert not data["source"]["captured_at"]
    assert data["latest_identification"] is None


def test_source_reset_clears_identity_art_and_lyrics_without_touching_audio(opus):
    def packet_hash():
        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(opus), "-map", "0:a:0",
                                        "-c", "copy", "-f", "hash", "-"])
    before = packet_hash()
    raw = OggOpus(opus)
    for key in ("musicbrainz_trackid", "musicbrainz_albumid", "musicbrainz_artistid", "lyrics", "genre", "date", "metadata_block_picture"):
        raw[key] = ["bad"]
    raw["r128_track_gain"] = ["100"]; raw["replaygain_track_peak"] = ["0.7"]; raw.save()
    clear_descriptive_tags(opus, {"tags": {"title": ["Right title"], "artist": ["Right artist"]}})
    result = OggOpus(opus)
    assert result["title"] == ["Right title"]
    assert result["artist"] == ["Right artist"]
    assert result["r128_track_gain"] == ["100"]
    assert not any(k.startswith("musicbrainz") for k in result)
    assert "lyrics" not in result and "metadata_block_picture" not in result and "date" not in result
    assert packet_hash() == before


def test_manual_selection_queues_retag_without_download(db, track, environment):
    track, _, _ = active_track(db, track, environment)
    db.queue_track(track["id"], "admin", correction(track, "select_recording", mbid=OTHER))
    payload = json.loads(db.claim()["payload"])
    assert payload["mode"] == "retag"
    assert payload["overrides"] == {"mbid": OTHER}
    assert payload["reviewed_correction"]


@pytest.mark.parametrize("mode", ["restore_source", "select_recording"])
def test_manual_correction_rejects_stale_context(db, track, environment, mode):
    track, _, _ = active_track(db, track, environment)
    payload = correction(track, mode, mbid=OTHER)
    db.update("tracks", track["id"], mbid=OTHER)
    with pytest.raises(ValueError, match="changed"):
        db.queue_track(track["id"], "admin", payload)


def test_correction_context_and_action_are_user_scoped(db, track, environment):
    track, _, _ = active_track(db, track, environment)
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get(f"/api/tracks/{track['id']}/metadata-context", params={"user_id": "guest"}).status_code == 404
        response = client.post(f"/api/tracks/{track['id']}/metadata-correction", json={"user_id": "guest", **correction(track)})
        assert response.status_code == 404
        response = client.post(f"/api/tracks/{track['id']}/metadata-correction", json={"user_id": "admin", **correction(track, "select_recording", mbid="not-an-id")})
        assert response.status_code == 400
    assert not db.rows("SELECT * FROM jobs WHERE state='PENDING'")


def test_correction_during_approval_preserves_candidate_binding(db, track, environment):
    origin = db.origins_for_track(track["id"])[0]
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "op", "candidate_sha256": "known"}
    db.pause_for_approval(track["id"], [{"title": "Bad choice"}], pending, "/tmp/a.opus")
    track = db.owned("tracks", track["id"], "admin")
    db.queue_track(track["id"], "admin", correction(track, "select_recording", mbid=OTHER))
    payload = json.loads(db.claim()["payload"])
    assert payload["mode"] == "resume"
    assert payload["candidate_sha256"] == "known"
    assert payload["operation"] == "op"
    assert payload["selected"] == {"mbid": OTHER, "manual": True}


def test_restore_source_single_origin_clears_identity_and_archives_history(db, track, environment, monkeypatch):
    track, origin, old = active_track(db, track, environment)
    pipeline = Pipeline(db)
    new = old.with_name("restored.opus")

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "apply"
        assert kwargs["selected"]["kind"] == "source"
        assert kwargs["selected"]["source"]["tags"]["title"] == ["Song"]
        new.write_bytes(Path(kwargs["path"]).read_bytes())
        return {"file_path": str(new), "mbid": None, "release_id": None, "beets_id": 1, "matched_title": "Artist - Song"}

    async def never(*args, **kwargs):
        raise AssertionError("Restoration must not download or query MusicBrainz")

    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", never)
    monkeypatch.setattr(pipeline, "metadata_for_recording", never)
    db.queue_track(track["id"], "admin", correction(track))
    job = db.claim(); asyncio.run(pipeline.process_track(job))
    current = db.owned("tracks", track["id"], "admin")
    assert current["mbid"] is None and current["release_id"] is None
    assert current["health"] == "AVAILABLE"
    assert new.read_bytes() == b"original-audio"
    assert db.current_asset(track["id"])["sha256"] == sha256_file(new)
    history = db.one("SELECT * FROM identity_history")
    assert history["previous_mbid"] == MBID and history["new_mbid"] is None
    assert history["reason"] == "restore_source"
    asyncio.run(pipeline.process_track(job))
    assert len(db.rows("SELECT * FROM identity_history")) == 1


def test_source_restore_splits_shared_identity_instead_of_clearing_other_origins(db, track, environment):
    track, origin, _ = active_track(db, track, environment)
    with db.db() as con:
        con.execute("INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), track["id"], "admin", "youtube", "other", "other-url", "Other origin", "2020", "2020"))
    plan = db.identity_plan(track["id"], origin["id"], source_choice(snapshot(db, origin)), "restore_source")
    assert plan["route"] == "split"
    assert plan["working_track"]["id"] != track["id"]
    assert db.owned("tracks", track["id"], "admin")["mbid"] == MBID


def test_manual_approval_resolves_selected_mbid_server_side(db, track, environment, monkeypatch):
    origin = db.origins_for_track(track["id"])[0]
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"; candidate.write_bytes(b"reviewed")
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "op", "candidate_sha256": sha256_file(candidate)}
    atomic_json(directory / "operation.json", pending)
    atomic_json(directory / "download-complete.json", {"source_url": origin["url"]})
    atomic_json(directory / "identity-plan.json", {"operation": "op", "plan": {"stale": True}})
    db.pause_for_approval(track["id"], [{"title": "Bad choice"}], pending, str(candidate))
    db.queue_track(track["id"], "admin", correction(db.owned("tracks", track["id"], "admin"), "select_recording", mbid=OTHER))
    pipeline = Pipeline(db); resolved = []

    async def metadata(job, track, directory, mbid):
        resolved.append(mbid); return {"mbid": mbid, "tags": {"title": "Verified"}}

    async def apply(job, track, origin, selected, *args):
        assert selected["tags"]["title"] == "Verified"
        assert not (directory / "identity-plan.json").exists()

    monkeypatch.setattr(pipeline, "metadata_for_recording", metadata)
    monkeypatch.setattr(pipeline, "_apply_candidate", apply)
    asyncio.run(pipeline.process_track(db.claim()))
    assert resolved == [OTHER]


def test_reprocess_seeds_selected_origin_and_records_diagnostics(db, track, environment, monkeypatch):
    track, origin, _ = active_track(db, track, environment)
    db.update("tracks", track["id"], title="Different canonical source", matched_title="Wrong confirmed title")
    pipeline = Pipeline(db)

    async def download(url, track_id, directory, report):
        candidate = directory / f"temp_{track_id}.opus"; candidate.write_bytes(b"new audio")
        return candidate, {"source_url": url}

    async def bridge(job, seed, directory, mode, **kwargs):
        assert mode == "identify" and seed["title"] == origin["title"]
        return {"choices": [{"mbid": MBID, "title": "First"}], "automatic": False, "recommendation": "weak",
                "diagnostics": {"search_seed": {"title": "Song", "artist": "Artist"}}}

    monkeypatch.setattr(pipeline.downloader, "download", download)
    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_track(track["id"], "admin", {"mode": "reprocess"})
    asyncio.run(pipeline.process_track(db.claim()))
    context = metadata_context(db, track["id"], "admin")
    assert context["latest_identification"]["source_title"] == origin["title"]
    choices = json.loads(db.owned("tracks", track["id"], "admin")["choices"])
    assert any(c["kind"] == "source" and c["mbid"] is None for c in choices if "kind" in c)
    assert any(c.get("kind") == "asis" and c["mbid"] == MBID for c in choices)


def test_real_beets_source_restore_does_not_reapply_bad_identifiers(db, track, opus, environment):
    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1": pytest.fail("Pinned beets integration is required")
        pytest.skip("Pinned beets unavailable locally")
    script = r'''
import os, sys, shutil, json
from pathlib import Path
from unittest.mock import patch
from mutagen.oggopus import OggOpus
from beets.library import Item
from beets_bridge import Bridge
from source_metadata import clear_descriptive_tags
p = Path(sys.argv[1]); b = Bridge("admin")
a = OggOpus(p)
a["artist"] = ["Wrong artist"]; a["title"] = ["Wrong song"]
a["album"] = ["Wrong album"]; a["date"] = ["1999"]
a["musicbrainz_trackid"] = ["11111111-1111-4111-8111-111111111111"]
a["musicbrainz_artistid"] = ["33333333-3333-4333-8333-333333333333"]
a["lyrics"] = ["Wrong lyrics"]; a.save()
item = Item.from_path(str(p)); item.pipeline_id = "fixture"; item.add(b.lib)
track = {"id": "fixture", "user_id": "admin", "title": "Wrong artist - Wrong song", "beets_id": item.id,
         "file_path": str(p), "discovered_at": "2020-01-02T03:04:05Z"}
candidate = p.with_name("candidate.opus"); shutil.copy2(p, candidate)
def forbid(*a, **k): raise AssertionError("Source restore must not fetch external metadata")
with patch("requests.sessions.Session.send", forbid):
    selected = {"kind": "source", "mbid": None, "source": {"source_title": "Right artist - Right song",
      "tags": {"artist": ["Right artist"], "title": ["Right song"]}}}
    result = b.finalize(track, candidate, selected, operation="restore")
raw = OggOpus(result["file_path"])
assert raw["artist"] == ["Right artist"] and raw["title"] == ["Right song"]
assert not any(k.startswith("musicbrainz") for k in raw)
assert not raw.get("album") and not raw.get("date") and not raw.get("lyrics")
assert result["mbid"] is None and result["release_id"] is None
assert raw["comment"] == raw["description"]
assert raw.get("r128_track_gain") and raw.get("replaygain_track_peak")
print("Source restore integration passed")
'''
    result = subprocess.run([sys.executable, "-c", script, str(opus)], env=os.environ | {"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
