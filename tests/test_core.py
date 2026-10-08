import json
from pathlib import Path

import pytest

from providers import Entry, Snapshot, parse_source
from store import SCHEMA_VERSION


def test_schema_is_versioned(db):
    assert db.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION


def test_same_origin_in_multiple_playlists_is_one_track_and_one_origin(db):
    first = db.add_source(parse_source("https://youtube.com/playlist?list=PLa"), "admin")
    second = db.add_source(parse_source("https://youtube.com/playlist?list=PLb"), "admin")
    entry_a = Entry("abcdefghijk", "https://youtu.be/abcdefghijk", "Artist - Song",
                    "2020-01-01T00:00:00Z", "entry-a", 0)
    entry_b = Entry("abcdefghijk", "https://youtu.be/abcdefghijk", "Artist - Song",
                    "2021-01-01T00:00:00Z", "entry-b", 0)
    db.apply_snapshot(first, Snapshot("A", [entry_a]))
    db.apply_snapshot(second, Snapshot("B", [entry_b]))
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM track_origins")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM memberships")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='track'")["n"] == 1


def test_playlist_discovery_beats_first_seen_and_never_moves_later(db):
    direct = db.add_source(parse_source("https://youtube.com/watch?v=abcdefghijk"), "admin")
    db.apply_snapshot(direct, Snapshot("Direct", [
        Entry("abcdefghijk", "u", "Song", "2020-01-01T00:00:00Z", "video", 0,
              "first_seen")
    ]))
    assert db.one("SELECT discovered_at,discovery_basis FROM tracks") == {
        "discovered_at": "2020-01-01T00:00:00Z",
        "discovery_basis": "first_seen",
    }

    later_playlist = db.add_source(parse_source("https://youtube.com/playlist?list=PLlater"), "admin")
    db.apply_snapshot(later_playlist, Snapshot("Later", [
        Entry("abcdefghijk", "u", "Song", "2021-01-01T00:00:00Z", "later", 0)
    ]))
    assert db.one("SELECT discovered_at,discovery_basis FROM tracks") == {
        "discovered_at": "2021-01-01T00:00:00Z",
        "discovery_basis": "playlist_added",
    }

    earliest = db.add_source(parse_source("https://youtube.com/playlist?list=PLearly"), "admin")
    db.apply_snapshot(earliest, Snapshot("Early", [
        Entry("abcdefghijk", "u", "Song", "2019-01-01T00:00:00Z", "early", 0)
    ]))
    newest = db.add_source(parse_source("https://youtube.com/playlist?list=PLnew"), "admin")
    db.apply_snapshot(newest, Snapshot("New", [
        Entry("abcdefghijk", "u", "Song", "2022-01-01T00:00:00Z", "new", 0)
    ]))
    assert db.one("SELECT discovered_at FROM tracks")["discovered_at"] == "2019-01-01T00:00:00Z"


def test_different_users_get_separate_logical_tracks(db):
    entry = Entry("abcdefghijk", "u", "Song", "2020-01-01T00:00:00Z", "e", 0)
    for user in ("admin", "guest"):
        source = db.add_source(parse_source("https://youtube.com/playlist?list=PLsame"), user)
        db.apply_snapshot(source, Snapshot("Same", [entry]))
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM track_origins")["n"] == 2


def test_confirmed_mbid_routes_new_origin_to_existing_track(db, tmp_path):
    import uuid
    now = "2020-01-01T00:00:00Z"
    mbid = "11111111-1111-4111-8111-111111111111"
    with db.db() as con:
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        first_origin, second_origin = str(uuid.uuid4()), str(uuid.uuid4())
        for track_id, title in ((first, "First"), (second, "Second")):
            con.execute(
                """INSERT INTO tracks(id,user_id,title,first_seen_at,discovered_at,
                   discovery_basis,created_at,updated_at) VALUES (?,?,?,?,?,'first_seen',?,?)""",
                (track_id, "admin", title, now, now, now, now),
            )
        con.execute("UPDATE tracks SET mbid=?,matched_title='Artist - Song' WHERE id=?", (mbid, first))
        con.execute(
            """INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,
               first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (first_origin, first, "admin", "youtube", "one", "u1", "Song", now, now),
        )
        con.execute(
            """INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,
               first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (second_origin, second, "admin", "future", "two", "u2", "Song", now, now),
        )
    plan = db.identity_plan(second, second_origin, {"mbid": mbid, "tags": {}}, "ingest")
    assert plan["route"] == "existing"
    committed = db.commit_existing_identity(plan, "ingest", "dedup-operation")
    assert committed["target_id"] == first
    assert db.one("SELECT track_id FROM track_origins WHERE id=?", (second_origin,))["track_id"] == first
    assert committed["source_orphan"] is True


def test_identity_change_is_archived_when_new_asset_commits(db, track, environment):
    origin = db.one("SELECT * FROM track_origins")
    db.update(
        "tracks", track["id"], mbid="11111111-1111-4111-8111-111111111111",
        release_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        matched_title="Artist - Old",
    )
    current = db.one("SELECT * FROM tracks")
    selected = {"mbid": "22222222-2222-4222-8222-222222222222", "tags": {}}
    plan = db.identity_plan(current["id"], origin["id"], selected, "reprocess")
    path = environment[1] / "admin" / "new.opus"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"new")
    result = {
        "file_path": str(path), "beets_id": 2, "matched_title": "Artist - New",
        "mbid": selected["mbid"], "release_id": None,
    }
    asset = {
        "path": str(path), "sha256": "abc", "size_bytes": 3, "mtime_ns": 1,
        "downloader_name": "yt-dlp", "downloader_version": "test", "source_url": origin["url"],
    }
    db.commit_processed_identity(plan, result, asset, "reprocess", "identity-operation")
    history = db.one("SELECT * FROM identity_history")
    assert history["previous_mbid"] == "11111111-1111-4111-8111-111111111111"
    assert history["new_mbid"] == selected["mbid"]


def test_delete_and_ignore_blocks_known_origin(db, track):
    ignored_id = db.create_tombstone(track["id"], "admin", "test")
    assert db.one("SELECT id FROM ignored_tracks")["id"] == ignored_id
    origin = db.one("SELECT * FROM track_origins")
    assert db.one(
        "SELECT 1 AS ok FROM ignored_origins WHERE user_id=? AND provider=? AND media_key=?",
        ("admin", origin["provider"], origin["media_key"]),
    )
    db.delete_track(track["id"], "admin")
    source = db.one("SELECT * FROM sources")
    db.apply_snapshot(source, Snapshot("A playlist", [
        Entry(origin["media_key"], origin["url"], origin["title"],
              "2021-04-17T21:36:42Z", "again", 0)
    ]))
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 0


def test_identity_split_preserves_asset_history_and_requests_remaining_origin(db, track, environment):
    import uuid

    original = db.one("SELECT * FROM track_origins")
    old_mbid = "11111111-1111-4111-8111-111111111111"
    old_path = environment[1] / "admin" / "old.opus"
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"old")
    first_plan = db.identity_plan(track["id"], original["id"], {"mbid": old_mbid, "tags": {}}, "ingest")
    stat = old_path.stat()
    db.commit_processed_identity(
        first_plan,
        {"file_path": str(old_path), "beets_id": 1, "matched_title": "Artist - Old",
         "mbid": old_mbid, "release_id": None},
        {"path": str(old_path), "sha256": "old", "size_bytes": stat.st_size,
         "mtime_ns": stat.st_mtime_ns, "downloader_name": "yt-dlp",
         "downloader_version": "old", "source_url": original["url"]},
        "ingest", "initial-operation",
    )
    second_origin = str(uuid.uuid4())
    now = "2022-01-01T00:00:00Z"
    with db.db() as con:
        con.execute(
            """INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,
               first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)""",
            (second_origin, track["id"], "admin", "youtube", "secondvideo", "u2",
             "Artist - Old", now, now),
        )

    current = db.one("SELECT * FROM tracks WHERE id=?", (track["id"],))
    new_mbid = "22222222-2222-4222-8222-222222222222"
    plan = db.identity_plan(current["id"], original["id"], {"mbid": new_mbid, "tags": {}}, "reprocess")
    assert plan["route"] == "split"
    new_path = environment[1] / "admin" / "new.opus"
    new_path.write_bytes(b"new")
    new_stat = new_path.stat()
    receipt = db.commit_processed_identity(
        plan,
        {"file_path": str(new_path), "beets_id": 2, "matched_title": "Artist - New",
         "mbid": new_mbid, "release_id": None},
        {"path": str(new_path), "sha256": "new", "size_bytes": new_stat.st_size,
         "mtime_ns": new_stat.st_mtime_ns, "downloader_name": "yt-dlp",
         "downloader_version": "new", "source_url": original["url"]},
        "reprocess", "split-operation",
    )
    target_id = receipt["target_id"]
    assert target_id != track["id"]
    assert db.one("SELECT track_id FROM track_origins WHERE id=?", (original["id"],))["track_id"] == target_id
    assert db.one("SELECT track_id FROM track_origins WHERE id=?", (second_origin,))["track_id"] == track["id"]
    source = db.one("SELECT * FROM tracks WHERE id=?", (track["id"],))
    assert source["health"] == "UNAVAILABLE"
    assert source["current_asset_id"] is None
    assert receipt["reingest_origin_id"] == second_origin
    archived = db.one("SELECT * FROM assets WHERE path=?", (str(old_path),))
    assert archived["track_id"] == target_id
    assert archived["state"] == "REPLACED"


def test_ignored_recording_tombstone_catches_a_new_origin_after_identification(db, track):
    from providers import Entry, Snapshot, parse_source

    mbid = "11111111-1111-4111-8111-111111111111"
    db.update("tracks", track["id"], mbid=mbid, matched_title="Artist - Song")
    ignored_id = db.create_tombstone(track["id"], "admin", "test")
    db.delete_track(track["id"], "admin")

    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(source, Snapshot("Other", [
        Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Another upload",
              "2022-01-01T00:00:00Z", "entry", 0)
    ]))
    new_track = db.one("SELECT * FROM tracks")
    new_origin = db.one("SELECT * FROM track_origins")
    plan = db.identity_plan(new_track["id"], new_origin["id"], {"mbid": mbid, "tags": {}}, "ingest")
    assert plan["ignored"]["id"] == ignored_id
    db.ignore_identified_origin(new_origin["id"], mbid, "Artist - Song", "ignored-operation")
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 0
    assert db.one(
        "SELECT ignored_track_id FROM ignored_origins WHERE provider='youtube' AND media_key='lmnopqrstuv'"
    )["ignored_track_id"] == ignored_id
    assert db.operation_receipt("ignored-operation")["finalized_at"]


def test_metadata_only_origin_is_recorded_without_download_job(db):
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLmetadata"), "admin")
    # A future provider can set downloadable=False while retaining source membership
    # and an authoritative playlist-added timestamp.
    entry = Entry(
        "catalog-id", "https://catalog.example/track/catalog-id", "Artist - Song",
        "2018-02-03T04:05:06Z", "catalog-entry", 0,
        "playlist_added", False,
    )
    db.apply_snapshot(source, Snapshot("Catalog playlist", [entry]))
    track = db.one("SELECT * FROM tracks")
    origin = db.one("SELECT * FROM track_origins")
    assert track["health"] == "UNAVAILABLE"
    assert track["operation_state"] == "IDLE"
    assert origin["downloadable"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='track'")["n"] == 0
    with pytest.raises(ValueError, match="no downloadable audio"):
        db.queue_track(track["id"], "admin", {
            "mode": "ingest", "origin_id": origin["id"],
        })
