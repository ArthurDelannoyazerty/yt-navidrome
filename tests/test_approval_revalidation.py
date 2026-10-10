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
