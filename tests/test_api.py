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


def test_frontend_assets_are_content_versioned_and_cache_safe(db):
    import re

    with TestClient(create_app(db, start_workers=False)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "__STATIC_VERSION__" not in page.text
        versions = re.findall(r'/static/(?:style\.css|app\.js)\?v=([0-9a-f]{12})', page.text)
        assert len(versions) == 2 and versions[0] == versions[1]
        assert 'Loading…' in page.text
        assert page.headers["Cache-Control"] == "no-store, max-age=0"
        assert page.headers["Pragma"] == "no-cache"

        for path in (
            f"/static/style.css?v={versions[0]}",
            f"/static/app.js?v={versions[0]}",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"



def test_system_exposes_youtube_circuit_state(db):
    with TestClient(create_app(db, start_workers=False)) as client:
        data = client.get("/api/system").json()
        assert data["schema"] == 2
        assert data["youtube_circuit"]["open"] is False
        assert data["youtube_circuit"]["failures"] == 0



def test_bulk_approve_is_scoped_to_current_user(db, track):
    from providers import Entry, Snapshot, parse_source

    db.add_user("guest")
    guest_source = db.add_source(
        parse_source("https://youtube.com/playlist?list=PLguest"), "guest"
    )
    db.apply_snapshot(
        guest_source,
        Snapshot("Guest playlist", [
            Entry(
                "lmnopqrstuv",
                "https://www.youtube.com/watch?v=lmnopqrstuv",
                "Guest Artist - Guest Song",
                "2022-01-01T00:00:00Z",
                "guest-entry",
                0,
            )
        ]),
    )
    db.execute("UPDATE jobs SET state='DONE'")

    admin_origin = db.one(
        "SELECT * FROM track_origins WHERE track_id=?",
        (track["id"],),
    )
    guest_track = db.one("SELECT * FROM tracks WHERE user_id='guest'")
    guest_origin = db.one(
        "SELECT * FROM track_origins WHERE track_id=?",
        (guest_track["id"],),
    )
    choices = [
        {
            "mbid": "11111111-1111-4111-8111-111111111111",
            "title": "Best match",
            "artist": "Artist",
            "album": "Album",
            "similarity": 95.0,
            "description": "",
            "kind": "candidate",
            "tags": {},
        },
        {
            "mbid": None,
            "title": "Original",
            "artist": "",
            "album": "",
            "similarity": 0,
            "description": "source metadata",
            "kind": "asis",
            "tags": {},
        },
    ]
    db.pause_for_approval(
        track["id"],
        choices,
        {
            "action": "ingest",
            "origin_id": admin_origin["id"],
            "operation": "admin-approval",
        },
        "/tmp/admin.opus",
    )
    db.pause_for_approval(
        guest_track["id"],
        choices,
        {
            "action": "ingest",
            "origin_id": guest_origin["id"],
            "operation": "guest-approval",
        },
        "/tmp/guest.opus",
    )

    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(
            "/api/batch",
            json={"user_id": "admin", "mode": "best"},
        )

    assert response.status_code == 200
    assert response.json()["message"] == "Queued 1 tracks"

    admin_after = db.one("SELECT * FROM tracks WHERE id=?", (track["id"],))
    guest_after = db.one("SELECT * FROM tracks WHERE id=?", (guest_track["id"],))
    assert admin_after["operation_state"] == "QUEUED"
    assert json.loads(admin_after["selected"])["title"] == "Best match"
    assert guest_after["operation_state"] == "NEEDS_APPROVAL"
    assert guest_after["selected"] is None
    assert db.one(
        "SELECT COUNT(*) AS n FROM jobs WHERE user_id='guest' AND state='PENDING'"
    )["n"] == 0
