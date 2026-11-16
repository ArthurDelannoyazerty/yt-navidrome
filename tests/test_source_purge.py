"""Regression coverage for scoped, durable source cleanup."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from source_purge import commit, preview, process, queue


def source(db, user, name):
    return db.add_source(parse_source(
        "https://www.youtube.com/playlist?list=PL" + name
    ), user, monitored=True)


def populate(db, src, media="abcdefghijk"):
    db.apply_snapshot(src, Snapshot("Playlist", [
        Entry(media, "https://www.youtube.com/watch?v=" + media,
              "Artist - Song", "2024-01-01T00:00:00Z", "entry-" + media, 0)
    ]))


def test_explicit_source_url_and_user_are_required(db):
    a = source(db, "admin", "one")
    populate(db, a)
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get(
            f"/api/sources/{a['id']}/purge-preview",
            params={"user_id": "guest"},
        ).status_code == 404
        assert client.post(
            f"/api/sources/{a['id']}/purge",
            json={"user_id": "guest", "source_url": a["url"]},
        ).status_code == 404
        assert client.post(
            f"/api/sources/{a['id']}/purge",
            json={"user_id": "admin", "source_url": "https://wrong.invalid"},
        ).status_code == 404
        result = client.get(
            f"/api/sources/{a['id']}/purge-preview",
            params={"user_id": "admin"},
        ).json()
        assert result["exclusive_tracks"] == 1
        assert result["shared_tracks"] == 0
    assert db.owned("sources", a["id"], "admin")
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 1


def test_purge_exclusive_files_without_touching_another_user(db, environment, monkeypatch):
    admin = source(db, "admin", "one")
    guest = source(db, "guest", "two")
    populate(db, admin)
    populate(db, guest)
    row = db.one("SELECT * FROM tracks WHERE user_id='admin'")
    other = db.one("SELECT * FROM tracks WHERE user_id='guest'")
    library = environment[1]
    filename = library / "admin" / "Waiting for approval" / (row["id"] + ".opus")
    filename.parent.mkdir(parents=True)
    filename.write_bytes(b"audio")
    db.update("tracks", row["id"], file_path=str(filename))
    with db.db() as con:
        con.execute(
            """INSERT INTO assets(track_id,path,state,created_at)
               VALUES (?,?,?,?)""",
            (row["id"], str(filename), "CURRENT", "2024-01-01T00:00:00Z"),
        )
    received = []
    async def fake_bridge(job, track, directory, mode, **kwargs):
        received.append((track["id"], mode))
    pipeline = Pipeline(db)
    monkeypatch.setattr(pipeline, "bridge", fake_bridge)
    result = queue(db, admin["id"], "admin", admin["url"])
    assert result["job_id"]
    assert db.one("SELECT monitored,status FROM sources WHERE id=?", (admin["id"],)) == {
        "monitored": 0, "status": "PURGING"
    }
    job = db.claim()
    assert job["kind"] == "purge_source"
    asyncio.run(process(pipeline, job))
    assert not filename.exists()
    assert received == [(row["id"], "forget")]
    assert not db.one("SELECT * FROM sources WHERE id=?", (admin["id"],))
    assert not db.one("SELECT * FROM tracks WHERE id=?", (row["id"],))
    assert db.one("SELECT id FROM tracks WHERE id=?", (other["id"],))
    assert db.owned("sources", guest["id"], "guest")
    # Processing an already recorded receipt does not delete files twice.
    asyncio.run(process(pipeline, job))
    assert len(received) == 1


def test_shared_video_keeps_music_and_its_other_playlist(db):
    first = source(db, "admin", "one")
    second = source(db, "admin", "two")
    populate(db, first)
    populate(db, second)
    track = db.one("SELECT * FROM tracks")
    assert preview(db, first["id"], "admin")["shared_tracks"] == 1
    result = commit(db, first["id"], "admin", first["url"], "purge-source:" + first["id"])
    assert result["exclusive_tracks"] == 0
    assert result["shared_tracks"] == 1
    assert db.one("SELECT id FROM tracks WHERE id=?", (track["id"],))
    assert db.one(
        "SELECT COUNT(*) AS n FROM memberships WHERE source_id=?",
        (second["id"],),
    )["n"] == 1
    assert db.one(
        "SELECT COUNT(*) AS n FROM source_origins WHERE source_id=?",
        (second["id"],),
    )["n"] == 1
    assert not db.one("SELECT id FROM sources WHERE id=?", (first["id"],))


def test_resync_removed_entry_is_still_attributed_to_original_source(db):
    first = source(db, "admin", "one")
    populate(db, first)
    db.apply_snapshot(first, Snapshot("Now empty", []))
    assert not db.rows("SELECT * FROM memberships")
    assert preview(db, first["id"], "admin")["exclusive_tracks"] == 1
    result = commit(db, first["id"], "admin", first["url"], "purge-source:" + first["id"])
    assert result["exclusive_tracks"] == 1
    assert not db.rows("SELECT * FROM tracks")


def test_purge_does_not_delete_independently_sourced_track(db):
    first = source(db, "admin", "one")
    populate(db, first)
    original = db.one("SELECT * FROM tracks")
    with db.db() as con:
        con.execute(
            """INSERT INTO track_origins(
                id,track_id,user_id,provider,media_key,url,title,
                first_seen_at,last_seen_at,is_discovery)
                VALUES (?,?,?,?,?,?,?,?,?,1)""",
            ("independent", original["id"], "admin", "youtube", "independent",
             "https://www.youtube.com/watch?v=independent", "Other origin",
             "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z"),
        )
    assert preview(db, first["id"], "admin")["shared_tracks"] == 1
    commit(db, first["id"], "admin", first["url"], "purge-source:" + first["id"])
    assert db.one("SELECT id FROM tracks WHERE id=?", (original["id"],))
    assert db.one("SELECT id FROM track_origins WHERE id='independent'")


def test_purge_rejects_running_sync_before_committing(db):
    first = source(db, "admin", "one")
    populate(db, first)
    with db.db() as con:
        con.execute(
            "UPDATE jobs SET state='RUNNING' WHERE kind='sync' AND target=?",
            (first["id"],),
        )
    with pytest.raises(ValueError, match="running source sync"):
        commit(db, first["id"], "admin", first["url"], "purge-source:" + first["id"])
    assert db.owned("sources", first["id"], "admin")
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 1


def test_schema_9_backfills_existing_source_memberships(db, environment):
    first = source(db, "admin", "one")
    populate(db, first)
    db.execute("DELETE FROM source_origins")
    db.execute("PRAGMA user_version=8")
    db.init()
    assert db.one("PRAGMA user_version")["user_version"] == 9
    assert db.one("SELECT COUNT(*) AS n FROM source_origins")["n"] == 1
    assert preview(db, first["id"], "admin")["exclusive_tracks"] == 1

def test_archived_source_remains_visible_and_can_be_purged_later(db):
    first = source(db, "admin", "archive")
    populate(db, first)
    track = db.one("SELECT * FROM tracks WHERE user_id='admin'")
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.delete(
            f"/api/sources/{first['id']}", params={"user_id": "admin"},
        )
        assert response.status_code == 200
        listed = client.get("/api/sources", params={"user_id": "admin"}).json()
        assert listed[0]["status"] == "REMOVED"
        assert listed[0]["id"] == first["id"]
        assert db.one("SELECT id FROM tracks WHERE id=?", (track["id"],))
        assert db.one(
            "SELECT COUNT(*) AS n FROM source_origins WHERE source_id=?",
            (first["id"],),
        )["n"] == 1
        assert db.one("SELECT COUNT(*) AS n FROM memberships")["n"] == 0
        assert client.post(
            "/api/sources",
            json={"user_id": "admin", "urls": [first["url"]]},
        ).status_code == 400
        preview_response = client.get(
            f"/api/sources/{first['id']}/purge-preview",
            params={"user_id": "admin"},
        )
        assert preview_response.json()["exclusive_tracks"] == 1
        purge = client.post(
            f"/api/sources/{first['id']}/purge",
            json={"user_id": "admin", "source_url": first["url"]},
        )
        assert purge.status_code == 200
        assert purge.json()["job_id"]


def test_archived_source_can_be_restored_but_not_by_another_user(db):
    first = source(db, "admin", "restore")
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.delete(
            f"/api/sources/{first['id']}", params={"user_id": "admin"},
        ).status_code == 200
        url = f"/api/sources/{first['id']}/restore"
        assert client.post(
            url, json={"user_id": "guest", "source_url": first["url"]}
        ).status_code == 404
        assert client.post(
            url, json={"user_id": "admin", "source_url": "wrong"}
        ).status_code == 400
        result = client.post(
            url, json={"user_id": "admin", "source_url": first["url"]}
        )
        assert result.status_code == 200
        restored = db.owned("sources", first["id"], "admin")
        assert restored["status"] == "PENDING"
        assert restored["monitored"] == 0
        assert db.one(
            "SELECT 1 FROM jobs WHERE kind='sync' AND target=? AND state='PENDING'",
            (first["id"],),
        )


def test_two_archived_sources_preserve_shared_track_until_final_purge(db):
    first = source(db, "admin", "one")
    second = source(db, "admin", "two")
    populate(db, first)
    populate(db, second)
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    with TestClient(create_app(db, start_workers=False)) as client:
        for src in (first, second):
            assert client.delete(
                f"/api/sources/{src['id']}", params={"user_id": "admin"},
            ).status_code == 200
    music = db.one("SELECT id FROM tracks")
    first_result = commit(db, first["id"], "admin", first["url"],
                          "purge-source:" + first["id"])
    assert first_result["shared_tracks"] == 1
    assert db.one("SELECT id FROM tracks WHERE id=?", (music["id"],))
    second_result = commit(db, second["id"], "admin", second["url"],
                           "purge-source:" + second["id"])
    assert second_result["exclusive_tracks"] == 1
    assert not db.one("SELECT id FROM tracks WHERE id=?", (music["id"],))
