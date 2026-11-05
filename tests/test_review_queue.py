"""Regression tests for explicit review scope, stale choices and audio ownership."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from main import create_app
from providers import Entry, Snapshot, parse_source
from test_reliability import active_track


def waiting(db, track, environment, title="Artist - Song"):
    origin = db.origins_for_track(track["id"])[0]
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / "candidate.opus"
    candidate.write_bytes(b"pending-audio")
    choices = [
        {"kind": "candidate", "mbid": "11111111-1111-4111-8111-111111111111",
         "artist": "Artist", "title": title, "tags": {}},
        {"kind": "asis", "mbid": None, "title": "Source title", "tags": {}},
    ]
    db.pause_for_approval(
        track["id"], choices,
        {"action": "ingest", "origin_id": origin["id"], "operation": "review"},
        str(candidate),
    )
    return db.owned("tracks", track["id"], track["user_id"])


def second_track(db, user="admin", title="Other - Song"):
    db.add_user(user)
    source = db.add_source(
        parse_source("https://youtube.com/playlist?list=PLsecond" + user), user
    )
    db.apply_snapshot(source, Snapshot("Second", [
        Entry("lmnopqrstuv", "https://www.youtube.com/watch?v=lmnopqrstuv",
              title, "2022-01-01T00:00:00Z", "second-entry", 0),
    ]))
    db.execute("UPDATE jobs SET state='DONE'")
    return db.one(
        "SELECT t.* FROM tracks t JOIN track_origins o ON o.track_id=t.id "
        "WHERE t.user_id=? AND o.media_key='lmnopqrstuv'", (user,),
    )


def client_for(db):
    return TestClient(create_app(db, start_workers=False))


def item(db, track, index=0):
    current = db.owned("tracks", track["id"], track["user_id"])
    return {"track_id": track["id"], "index": index,
            "approval_token": db.approval_token(current)}


def test_queue_respects_user_search_and_status(db, track, environment):
    waiting(db, track, environment)
    guest = second_track(db, "guest")
    waiting(db, guest, environment)
    with client_for(db) as client:
        result = client.get("/api/review/queue", params={
            "user_id": "admin", "q": "Song", "status": "NEEDS_APPROVAL",
        })
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"
        assert result.json()["ids"] == [track["id"]]
        assert result.json()["total_scope"] == "filtered_reviewable"
        assert not result.json()["truncated"]
        assert client.get("/api/review/queue", params={
            "user_id": "admin", "status": "not-a-filter",
        }).status_code == 400


def test_queue_excludes_busy_and_includes_playable_tracks(db, track, environment):
    current, _, _ = active_track(db, track, environment)
    other = second_track(db)
    waiting(db, other, environment)
    db.queue_track(other["id"], "admin", {"mode": "approve", "index": 0})
    with client_for(db) as client:
        data = client.get("/api/review/queue", params={
            "user_id": "admin", "status": "ALL",
        }).json()
        assert data["ids"] == [current["id"]]
        assert data["matching_total"] == 2
        assert data["excluded"] == 1


def test_review_search_is_literal_and_includes_native_channel(db, track, environment):
    waiting(db, track, environment)
    origin = db.origins_for_track(track["id"])[0]
    db.update("track_origins", origin["id"], channel="末廣健一郎 - Topic")
    with client_for(db) as client:
        assert client.get("/api/review/queue", params={
            "user_id": "admin", "q": "末廣健一郎",
        }).json()["ids"] == [track["id"]]
        for literal in ("%", "_", "\\"):
            assert client.get("/api/review/queue", params={
                "user_id": "admin", "q": literal,
            }).json()["total"] == 0


def test_selected_approval_queues_only_explicit_ids_and_choice(db, track, environment):
    waiting(db, track, environment)
    other = second_track(db)
    waiting(db, other, environment)
    with client_for(db) as client:
        response = client.post("/api/review/approve", json={
            "user_id": "admin", "items": [item(db, track, 1)],
        })
    assert response.status_code == 200
    assert response.json()["scope"] == "selected_tracks"
    assert response.json()["queued"] == 1
    after = db.owned("tracks", track["id"], "admin")
    assert json.loads(after["selected"])["kind"] == "asis"
    assert db.owned("tracks", other["id"], "admin")["operation_state"] == "NEEDS_APPROVAL"


def test_stale_selection_is_reported_without_substituting_choice(db, track, environment):
    waiting(db, track, environment)
    selection = item(db, track)
    db.update("tracks", track["id"], choices=json.dumps([
        {"kind": "asis", "mbid": None, "title": "Changed", "tags": {}},
    ]))
    with client_for(db) as client:
        response = client.post("/api/review/approve", json={
            "user_id": "admin", "items": [selection],
        })
    assert response.status_code == 200
    assert response.json()["queued"] == 0
    assert response.json()["skipped"] == 1
    assert "changed" in response.json()["results"][0]["message"].lower()
    assert db.owned("tracks", track["id"], "admin")["selected"] is None


def test_cross_user_batch_rejected_before_any_track_is_queued(db, track, environment):
    waiting(db, track, environment)
    guest = second_track(db, "guest")
    waiting(db, guest, environment)
    with client_for(db) as client:
        response = client.post("/api/review/approve", json={
            "user_id": "admin", "items": [item(db, track), item(db, guest)],
        })
    assert response.status_code == 404
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "NEEDS_APPROVAL"


def test_selected_approval_requires_unique_ids_and_review_tokens(db, track, environment):
    waiting(db, track, environment)
    selected = item(db, track)
    invalid = [
        [{key: value for key, value in selected.items() if key != "approval_token"}],
        [selected, selected],
        [{**selected, "index": True}],
    ]
    with client_for(db) as client:
        for items in invalid:
            assert client.post("/api/review/approve", json={
                "user_id": "admin", "items": items,
            }).status_code == 422
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "NEEDS_APPROVAL"


def test_local_audio_supports_seeking_and_ownership(db, track, environment):
    current, _, path = active_track(db, track, environment)
    with client_for(db) as client:
        response = client.get(
            f"/api/tracks/{current['id']}/audio",
            params={"user_id": "admin"}, headers={"Range": "bytes=0-7"},
        )
        assert response.status_code == 206
        assert response.content == path.read_bytes()[:8]
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get(f"/api/tracks/{current['id']}/audio",
                          params={"user_id": "guest"}).status_code == 404


def test_audio_rejects_paths_outside_user_library_and_missing_files(db, track, environment):
    current, _, path = active_track(db, track, environment)
    outside = environment[1] / "guest" / "other.opus"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"private-other-user-audio")
    asset = db.current_asset(current["id"])
    with client_for(db) as client:
        db.update("assets", asset["id"], path=str(outside))
        assert client.get(f"/api/tracks/{current['id']}/audio",
                          params={"user_id": "admin"}).status_code == 404
        db.update("assets", asset["id"], path=str(path))
        path.unlink()
        assert client.get(f"/api/tracks/{current['id']}/audio",
                          params={"user_id": "admin"}).status_code == 404
