import json
import uuid

from fastapi.testclient import TestClient

from main import create_app


def test_search_pagination_and_user_scope(db, track):
    db.update("tracks", track["id"], matched_title="Geoxor - Patient Lips")
    now = "2020-01-01T00:00:00Z"
    with db.db() as con:
        for index in range(55):
            con.execute(
                """INSERT INTO tracks(id,user_id,title,first_seen_at,discovered_at,
                   discovery_basis,created_at,updated_at) VALUES (?,?,?,?,?,'first_seen',?,?)""",
                (str(uuid.uuid4()), "admin", f"Extra Artist {index} - Song", now, now, now, now),
            )
        con.execute(
            """INSERT INTO tracks(id,user_id,title,first_seen_at,discovered_at,
               discovery_basis,matched_title,created_at,updated_at)
               VALUES (?,?,?,?,?,'first_seen',?,?,?)""",
            (str(uuid.uuid4()), "guest", "Other", now, now, "Geoxor - Patient Lips", now, now),
        )
    with TestClient(create_app(db, start_workers=False)) as client:
        found = client.get("/api/tracks", params={"user_id": "admin", "q": "patient"}).json()
        assert found["total"] == 1
        page = client.get("/api/tracks", params={"user_id": "admin", "page": 2, "limit": 50}).json()
        assert page["total"] == 56
        assert len(page["tracks"]) == 6


def test_wrong_user_cannot_queue_operation(db, track):
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(
            f"/api/tracks/{track['id']}/action",
            json={"user_id": "guest", "mode": "delete"},
        )
        assert response.status_code == 404


def test_integrity_and_ignored_endpoints(db, track):
    ignored_id = db.create_tombstone(track["id"], "admin", "test")
    with TestClient(create_app(db, start_workers=False)) as client:
        ignored = client.get("/api/ignored", params={"user_id": "admin"}).json()
        assert ignored[0]["id"] == ignored_id
        restored = client.delete(f"/api/ignored/{ignored_id}", params={"user_id": "admin"})
        assert restored.status_code == 200
        assert client.get("/api/integrity", params={"user_id": "admin"}).status_code == 200
