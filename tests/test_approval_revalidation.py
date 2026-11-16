"""Changing reviewed audio or candidates cannot reuse an older approval route."""
import asyncio
import json

from fastapi.testclient import TestClient

from common import atomic_json
from main import create_app
from pipeline import Pipeline


def test_changed_audio_discards_obsolete_identity_plan(db, track, environment, monkeypatch):
    origin = db.origins_for_track(track["id"])[0]
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    candidate.write_bytes(b"changed-audio")
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "review",
               "candidate_sha256": "old-reviewed-hash"}
    choice = {"title": "New candidate", "mbid": None, "asis": True}
    atomic_json(directory / "operation.json", pending)
    atomic_json(directory / "download-complete.json", {"source_url": origin["url"]})
    atomic_json(directory / "identity-plan.json", {"operation": "review", "plan": {"obsolete": True}})
    db.pause_for_approval(track["id"], [choice], pending, str(candidate))
    db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
    pipeline = Pipeline(db)

    async def already_published(job, current, *args):
        return current

    monkeypatch.setattr(pipeline, "ensure_temporary_audio", already_published)


    async def identify(*args):
        return {"automatic": True, "recommendation": "strong"}, [choice]

    monkeypatch.setattr(pipeline, "_identification_choices", identify)
    asyncio.run(pipeline.process_track(db.claim()))
    assert not (directory / "identity-plan.json").exists()
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "NEEDS_APPROVAL"


def test_stale_browser_candidate_token_is_rejected(db, track):
    origin = db.origins_for_track(track["id"])[0]
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "review"}
    db.pause_for_approval(track["id"], [{"title": "First"}], pending, "/tmp/candidate.opus")
    with TestClient(create_app(db, start_workers=False)) as client:
        old = client.get("/api/tracks", params={"user_id": "admin"}).json()["tracks"][0]
        db.pause_for_approval(track["id"], [{"title": "Changed"}], pending, "/tmp/candidate.opus")
        response = client.post(f'/api/tracks/{track["id"]}/action', json={
            "user_id": "admin", "mode": "approve", "index": 0,
            "approval_token": old["approval_token"],
        })
    assert response.status_code == 400
    current = db.owned("tracks", track["id"], "admin")
    assert current["operation_state"] == "NEEDS_APPROVAL"
    assert json.loads(current["choices"])[0]["title"] == "Changed"
    assert not db.one("SELECT id FROM jobs WHERE state='PENDING'")


def test_provider_deferral_after_tag_write_preserves_approved_candidate(
        db, track, environment, monkeypatch):
    """A deferred Beets operation must never rewrite reviewed bytes."""
    import hashlib
    import pytest
    from http_policy import ApiDeferred

    origin = db.origins_for_track(track["id"])[0]
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    candidate.write_bytes(b"pristine-approved-ogg-opus")
    before = hashlib.sha256(candidate.read_bytes()).hexdigest()
    selected = {"kind": "candidate", "mbid": "11111111-1111-4111-8111-111111111111",
                "title": "Approved song", "tags": {"title": "Approved song"}}
    plan = {"route": "in_place", "working_track": track, "origin": origin,
            "selected_mbid": selected["mbid"]}
    pipeline = Pipeline(db)
    monkeypatch.setattr(db, "identity_plan", lambda *args, **kwargs: dict(plan))
    monkeypatch.setattr(pipeline, "activate_result", async_return_result)
    paths, selected_releases = [], []

    async def deferred_bridge(job, target, directory, mode, **kwargs):
        assert mode == "apply"
        working = __import__("pathlib").Path(kwargs["path"])
        paths.append(working)
        assert working != candidate
        assert working.read_bytes() == candidate.read_bytes()
        selected_releases.append(kwargs["overrides"]["release_id"])
        if len(paths) == 1:
            working.write_bytes(b"partially-written-tags")
            raise ApiDeferred("MusicBrainz HTTP 503")
        return {"file_path": str(working), "matched_title": "Approved song"}

    monkeypatch.setattr(pipeline, "bridge", deferred_bridge)
    job = {"id": None, "user_id": "admin", "target": track["id"]}
    receipt = {"source_url": origin["url"]}
    overrides = {"release_id": "22222222-2222-4222-8222-222222222222"}

    async def run():
        return await pipeline._apply_candidate(
            job, track, origin, selected, candidate, receipt, "ingest",
            "approved-operation", directory, overrides)

    with pytest.raises(ApiDeferred, match="503"):
        asyncio.run(run())
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == before
    assert paths[0].read_bytes() == b"partially-written-tags"
    asyncio.run(run())
    assert paths[1] == paths[0]
    assert paths[1].read_bytes() == candidate.read_bytes()
    assert selected_releases == [overrides["release_id"]] * 2
    assert hashlib.sha256(candidate.read_bytes()).hexdigest() == before


async def async_return_result(job, plan, result, receipt, operation, directory):
    return result


def test_approve_release_kept_in_durable_job(db, track):
    origin = db.origins_for_track(track["id"])[0]
    selected = {"kind": "candidate", "title": "Song",
                "mbid": "11111111-1111-4111-8111-111111111111",
                "tags": {"title": "Song"}}
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "review",
               "candidate_sha256": "approved-digest"}
    db.pause_for_approval(track["id"], [selected], pending, "/tmp/approved.opus")
    token = db.approval_token(db.owned("tracks", track["id"], "admin"))
    release_id = "22222222-2222-4222-8222-222222222222"
    db.queue_track(track["id"], "admin", {
        "mode": "approve", "index": 0, "approval_token": token, "release_id": release_id,
    })
    job = db.one("SELECT payload FROM jobs WHERE target=? AND state='PENDING'", (track["id"],))
    payload = json.loads(job["payload"])
    assert payload["selected"]["mbid"] == selected["mbid"]
    assert payload["overrides"]["release_id"] == release_id
    assert payload["candidate_sha256"] == "approved-digest"


def test_source_approval_cannot_set_musicbrainz_release(db, track):
    import pytest

    origin = db.origins_for_track(track["id"])[0]
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "review"}
    db.pause_for_approval(track["id"], [{"kind": "source", "title": "Original"}], pending, "/tmp/approved.opus")
    with pytest.raises(ValueError, match="only be chosen for a MusicBrainz recording"):
        db.queue_track(track["id"], "admin", {
            "mode": "approve", "index": 0, "release_id": "22222222-2222-4222-8222-222222222222",
        })
    assert not db.one("SELECT id FROM jobs WHERE target=? AND state='PENDING'", (track["id"],))
