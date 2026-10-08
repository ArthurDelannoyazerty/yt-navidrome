import json
from fastapi.testclient import TestClient
from main import create_app


def test_ui_and_input_errors_are_visible(db):
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.get("/")
        assert response.status_code == 200 and "Music Ingestor" in response.text
        assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        assert client.post("/api/users", json={"name": "../escape"}).status_code == 400
        events = client.get("/api/events", params={"user_id": "admin"}).json()
        assert any(e["level"] == "ERROR" and "/api/users" in e["message"] for e in events)
        assert client.get("/api/tracks", params={"user_id": "admin", "page": 0}).status_code == 422
        assert client.post("/api/users", json={"name": "safe"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_full_input_validated_before_enqueue(db):
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post("/api/sources", json={"user_id": "admin", "urls": ["https://youtube.com/watch?v=abcdefghijk", "https://invalid.example"]})
        assert response.status_code == 400 and not db.rows("SELECT * FROM sources")


def test_wrong_user_cannot_mutate_track_and_approval_tags_not_exposed(db, track):
    db.update("tracks", track["id"], status="NEEDS_APPROVAL", choices=json.dumps([{"tags": {"title": "server-only"}, "similarity": 90}]))
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(f"/api/tracks/{track['id']}/action", json={"user_id": "guest", "mode": "approve", "index": 0})
        assert response.status_code == 404
        records = client.get("/api/tracks", params={"user_id": "admin"}).json()
        assert "tags" not in records["tracks"][0]["choices"][0]
        assert records["stats"]["NEEDS_APPROVAL"] == 1


def test_source_delete_is_rejected_while_running(db, track):
    source = db.one("SELECT * FROM sources")
    db.enqueue("sync", source["id"], "admin"); db.claim()
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.delete(f"/api/sources/{source['id']}", params={"user_id": "admin"}).status_code == 400
    assert db.one("SELECT * FROM sources")


def test_track_search_is_user_scoped_and_pagination_applies_after_search(db, track):
    import uuid

    db.update("tracks", track["id"], matched_title="Geoxor - Patient Lips")
    with db.db() as con:
        for i in range(55):
            con.execute("""INSERT INTO tracks(id,user_id,provider,media_key,url,title,discovery_basis)
                VALUES (?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), "admin", "youtube", f"extra-{i}",
                 f"https://example.test/{i}", f"Extra Artist {i} - Song", "first_seen"))
        con.execute("""INSERT INTO tracks(id,user_id,provider,media_key,url,title,discovery_basis,matched_title)
            VALUES (?,?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), "guest", "youtube", "guest-match",
             "https://example.test/guest", "Different Source Title", "first_seen",
             "Geoxor - Patient Lips"))

    with TestClient(create_app(db, start_workers=False)) as client:
        result = client.get("/api/tracks", params={"user_id": "admin", "q": "patient"}).json()
        assert result["total"] == 1
        assert result["tracks"][0]["id"] == track["id"]
        assert client.get("/api/tracks", params={"user_id": "admin", "q": "geoxor"}).json()["total"] == 1
        page_two = client.get("/api/tracks", params={"user_id": "admin", "page": 2, "limit": 50}).json()
        assert page_two["total"] == 56
        assert len(page_two["tracks"]) == 6


def test_delete_action_is_scoped_to_current_user_and_queued(db, track):
    with TestClient(create_app(db, start_workers=False)) as client:
        wrong = client.post(f"/api/tracks/{track['id']}/action",
                            json={"user_id": "guest", "mode": "delete"})
        assert wrong.status_code == 404
        ok = client.post(f"/api/tracks/{track['id']}/action",
                         json={"user_id": "admin", "mode": "delete"})
        assert ok.status_code == 200

    updated = db.owned("tracks", track["id"], "admin")
    assert updated["status"] == "DELETING"
    job = db.one("SELECT * FROM jobs WHERE kind='track' AND target=? AND state='PENDING'", (track["id"],))
    assert json.loads(job["payload"])["mode"] == "delete"
