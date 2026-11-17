"""Persistent catalogue, graph, album preview/commit/undo and user isolation."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from album_management import AlbumManager
from catalogue import Catalogue
from common import sha256_file
from main import create_app

ARTIST = "11111111-1111-4111-8111-111111111111"
RECORDING_1 = "22222222-2222-4222-8222-222222222222"
RECORDING_2 = "33333333-3333-4333-8333-333333333333"
RECORDING_4 = "44444444-4444-4444-8444-444444444444"
RELEASE_A = "55555555-5555-4555-8555-555555555555"
RELEASE_B = "66666666-6666-4666-8666-666666666666"
GROUP = "77777777-7777-4777-8777-777777777777"
SOURCE_ID = "88888888-8888-4888-8888-888888888888"


def setup_catalogue(db, *releases):
    catalogue = Catalogue(db)
    catalogue.init()
    albums = AlbumManager(db, catalogue)
    albums.init()
    if not releases:
        releases = [(RELEASE_A, [RECORDING_1, RECORDING_2]), (RELEASE_B, [RECORDING_2, RECORDING_4])]
    with catalogue.db() as con:
        catalogue.ingest(con, "artist", {"id": ARTIST, "name": "Composer"})
        for rid, recording_ids in releases:
            catalogue.ingest(con, "release", {
                "id": rid, "title": "Original Soundtrack" if rid == RELEASE_B else "Preview",
                "date": "2018-07-13", "country": "JP", "status": "Official",
                "release-group": {"id": GROUP, "title": "Soundtrack"},
                "artist-credit": [{"artist": {"id": ARTIST, "name": "Composer"}}],
            })
            catalogue._store_release_tracks(con, {"id": rid, "media": [
                {"position": 1, "tracks": [{"position": pos, "id": str(uuid.uuid4()),
                    "title": f"Recording {pos}",
                    "recording": {"id": rec, "title": f"Recording {pos}"}}
                 for pos, rec in enumerate(recording_ids, 1)]},
            ]})
    return catalogue, albums


def add_audio_track(db, environment, recording=RECORDING_2, *, user="admin", legacy=False):
    state, library = environment
    from mutagen.oggopus import OggOpus
    if user != "admin":
        db.add_user(user)
    tid = str(uuid.uuid4())
    directory = library / user / ("Old Artist" if legacy else "audio")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (("old-" + tid) if legacy else tid)
    path = path.with_suffix(".opus")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=450:sample_rate=48000", "-t", "0.3", "-c:a", "libopus",
                    str(path)], check=True)
    audio = OggOpus(path)
    audio["title"] = ["Track"]
    audio["artist"] = ["Artist"]
    audio["musicbrainz_trackid"] = [recording]
    audio["album"] = ["Old album"]
    audio["musicbrainz_albumid"] = [RELEASE_A]
    audio.save()
    before = sha256_file(path)
    now = "2026-01-01T00:00:00Z"
    with db.db() as con:
        con.execute("""INSERT INTO tracks(id,user_id,title,discovery_basis,health,operation_state,
            file_path,mbid,release_id,created_at,updated_at)
            VALUES (?,?,'Track','first_seen','AVAILABLE','IDLE',?,?,?,?,?)""",
            (tid, user, str(path), recording, RELEASE_A, now, now))
        asset = con.execute("""INSERT INTO assets(track_id,path,state,created_at,sha256,size_bytes,mtime_ns)
            VALUES (?,?,'CURRENT',?,?,?,?)""",
            (tid, str(path), now, before, path.stat().st_size, path.stat().st_mtime_ns)).lastrowid
        con.execute("UPDATE tracks SET current_asset_id=? WHERE id=?", (asset, tid))
    return tid, path, before


def test_catalogue_entity_link_timestamps_and_discography_refresh(db):
    catalogue = Catalogue(db)
    catalogue.init()
    with catalogue.db() as con:
        catalogue.ingest(con, "artist", {"id": ARTIST, "name": "Composer"})
        before = con.execute("SELECT changed_at FROM entities WHERE kind='artist' AND id=?", (ARTIST,)).fetchone()[0]
        catalogue.ingest(con, "artist", {"id": ARTIST, "name": "Composer"})
        assert con.execute("SELECT changed_at FROM entities WHERE kind='artist' AND id=?", (ARTIST,)).fetchone()[0] == before
    catalogue.enqueue("artist", ARTIST, sections=["release-group"])
    assert catalogue.claim()["cursor"] == 0
    # Simulate one page of provider results; complete snapshots survive future partial refreshes.
    catalogue.request = lambda path, params: {"release-groups": [
        {"id": GROUP, "title": "Original Soundtrack"}], "release-group-count": 1}
    scope = {"kind": "artist", "entity_id": ARTIST, "section": "release-group",
             "scope_key": f"artist:{ARTIST}:release-group", "cursor": 0}
    catalogue.process_page(scope)
    status = catalogue.get("artist", ARTIST)
    assert status["scopes"][0]["state"] == "COMPLETE"
    assert any(row["id"] == GROUP for row in status["relations"])
    catalogue.enqueue("artist", ARTIST, sections=["release-group"], force=True)
    with catalogue.db() as con:
        assert con.execute("SELECT COUNT(*) FROM scope_members WHERE scope_key=?", (scope["scope_key"],)).fetchone()[0] == 1
        assert con.execute("SELECT last_complete_at FROM scopes WHERE scope_key=?", (scope["scope_key"],)).fetchone()[0]
    # Unfinished refresh leaves previous scope members and completeness date intact.


def test_recommendations_change_incrementally_without_guessing(db, environment):
    cat, albums = setup_catalogue(db)
    tid2, _, _ = add_audio_track(db, environment, RECORDING_2)
    recommendations = cat.album_recommendations("admin", track_ids=[tid2])
    assert {c["release_id"] for c in recommendations["candidates"]} == {RELEASE_A, RELEASE_B}
    assert {c["matched"] for c in recommendations["candidates"]} == {1}
    tid4, _, _ = add_audio_track(db, environment, RECORDING_4)
    recommendations = cat.album_recommendations("admin", track_ids=[tid2, tid4])
    assert recommendations["candidates"][0]["release_id"] == RELEASE_B
    assert recommendations["candidates"][0]["matched"] == 2
    assert recommendations["candidates"][1]["matched"] == 1


def test_album_preview_stale_gate_apply_and_byte_exact_undo(db, environment, monkeypatch):
    from mutagen.oggopus import OggOpus
    import album_management
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    cat, albums = setup_catalogue(db)
    tid, path, original_hash = add_audio_track(db, environment)
    preview = albums.preview("admin", RELEASE_B, track_ids=[tid])
    assert preview["can_apply"] == 1
    assert preview["items"][0]["status"] == "COMPATIBLE"
    # A stale approval is rejected before creating any background operations.
    db.execute("UPDATE tracks SET updated_at='2027-01-01T00:00:00Z' WHERE id=?", (tid,))
    with pytest.raises(ValueError, match="changed since preview"):
        albums.apply("admin", preview["preview_token"])
    preview = albums.preview("admin", RELEASE_B, track_ids=[tid])
    applied = albums.apply("admin", preview["preview_token"])
    assert applied["queued"] == 1
    change = db.one("SELECT * FROM album_changes WHERE group_id=?", (applied["group_id"],))
    albums.prepare(change["id"], "admin", "apply")
    # Prepared state can resume after a worker/process crash.
    albums.prepare(change["id"], "admin", "apply")
    changed_file = OggOpus(path)
    assert changed_file["musicbrainz_albumid"] == [RELEASE_B]
    assert changed_file["musicbrainz_trackid"] == [RECORDING_2]
    assert changed_file["title"] == ["Track"]
    assert str(path) == db.one("SELECT file_path FROM tracks WHERE id=?", (tid,))["file_path"]
    albums.finalize(change["id"], "admin", "apply", beets_id=None)
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (tid,))["release_id"] == RELEASE_B
    # The durable worker marks the finished apply job DONE before accepting undo.
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='album' AND target=?", (change["id"],))
    assert albums.undo("admin", change["id"])["queued"]
    albums.prepare(change["id"], "admin", "undo")
    albums.finalize(change["id"], "admin", "undo", beets_id=None)
    assert sha256_file(path) == original_hash
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (tid,))["release_id"] == RELEASE_A
    assert db.one("SELECT * FROM album_changes WHERE id=?", (change["id"],))["status"] == "UNDONE"


def test_approval_preview_rejects_nonmember_and_preference_requires_consent(db, environment, monkeypatch):
    import album_management
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    cat, albums = setup_catalogue(db)
    tid, path, _ = add_audio_track(db, environment, RECORDING_4)
    preview = albums.preview("admin", RELEASE_A, track_ids=[tid])
    assert preview["can_apply"] == 0
    assert preview["items"][0]["status"] == "INCOMPATIBLE"
    with pytest.raises(ValueError, match="No compatible"):
        albums.apply("admin", preview["preview_token"])


def test_graph_user_isolation_and_independent_node_filters(db, environment):
    cat, _ = setup_catalogue(db)
    admin_tid, _, _ = add_audio_track(db, environment, RECORDING_1)
    guest_tid, _, _ = add_audio_track(db, environment, RECORDING_4, user="guest")
    result = cat.graph("admin", view="downloaded", kinds=["recording"])
    ids = {n["id"] for n in result["nodes"]}
    assert f"recording:{RECORDING_1}" in ids
    assert f"recording:{RECORDING_4}" not in ids
    result = cat.graph("admin", view="downloaded", kinds=["recording", "release", "artist"])
    assert any(edge["relation"] == "appears-on" for edge in result["edges"])
    assert not cat.graph("admin", view="downloaded", kinds=[])["nodes"]
    with pytest.raises(ValueError, match="Unknown user"):
        cat.graph("missing", view="downloaded")


def test_catalogue_api_keeps_local_users_separate_and_tabs_available(db):
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get("/").status_code == 200
        html = client.get("/").text
        assert 'data-tab="catalogue"' in html and 'data-tab="graph"' in html
        for kind in ["recording", "artist", "release", "release-group", "work"]:
            assert f'value="{kind}"' in html
        assert client.get("/api/catalogue/progress", params={"user_id": "admin"}).status_code == 200
        assert client.get("/api/catalogue/graph", params={"user_id": "unknown"}).status_code == 400
        assert client.post("/api/catalogue/sync", json={"user_id": "unknown", "kind": "artist", "id": ARTIST}).status_code == 400
        result = client.post("/api/catalogue/sync", json={"user_id": "admin", "kind": "artist", "id": ARTIST})
        assert result.status_code == 200
        progress = client.get("/api/catalogue/progress", params={"user_id": "admin"}).json()
        assert progress["counts"].get("PENDING", 0) >= 1


def test_graph_projects_album_and_artist_nodes_when_recordings_hidden(db, environment):
    cat, _ = setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_2)
    artist = cat.graph("admin", view="downloaded", kinds=["artist"])
    assert any(n["kind"] == "artist" and n["id"].endswith(ARTIST) for n in artist["nodes"])
    assert all(n["kind"] == "artist" for n in artist["nodes"])
    album = cat.graph("admin", view="downloaded", kinds=["release"])
    assert {n["id"] for n in album["nodes"]} == {f"release:{RELEASE_A}", f"release:{RELEASE_B}"}
    families = cat.graph("admin", view="downloaded", kinds=["release-group"])
    assert any(n["id"] == f"release-group:{GROUP}" for n in families["nodes"])


def test_catalogue_partial_browse_never_exposes_unfinished_relationships(db):
    cat = Catalogue(db)
    cat.init()
    cat.enqueue("artist", ARTIST, sections=["release-group"])
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    first_page = [first] + [str(uuid.uuid4()) for _ in range(99)]
    cat.request = lambda path, params: {
        "release-groups": [{"id": identity, "title": "Release group"}
                           for identity in (first_page if params["offset"] == 0 else [second])],
        "release-group-count": 101,
    }
    # An exact 100-item page leaves the completed snapshot untouched.
    scope = cat.claim()
    cat.process_page(scope)
    assert cat.get("artist", ARTIST)["relations"] == []
    scope = cat.claim()
    cat.process_page(scope)
    related = cat.get("artist", ARTIST)["relations"]
    assert {r["id"] for r in related} == set(first_page + [second])


def test_discography_browse_uses_actual_offsets_and_fans_out_to_release_tracklists(db):
    cat = Catalogue(db)
    cat.init()
    groups = [str(uuid.uuid4()) for _ in range(4)]
    release = str(uuid.uuid4())
    recording = str(uuid.uuid4())
    received = []

    def provider(path, params):
        received.append((path, dict(params)))
        if path == "/release-group":
            # Simulate a provider page that is shorter than the requested 100.
            if params["offset"] == 0:
                return {"release-groups": [{"id": x, "title": f"Album {i}"}
                                            for i, x in enumerate(groups[:3])],
                        "release-group-count": 4}
            assert params["offset"] == 3
            return {"release-groups": [{"id": groups[3], "title": "Album 4"}],
                    "release-group-count": 4}
        if path == "/release":
            return {"releases": [{"id": release, "title": "Album", "release-group": {"id": groups[0]}}],
                    "release-count": 1}
        if path == f"/release/{release}":
            return {"id": release, "title": "Album", "release-group": {"id": groups[0]},
                    "media": [{"position": 1, "tracks": [{"position": 1,
                               "recording": {"id": recording, "title": "Previously unknown song"}}]}]}
        raise AssertionError(f"Unexpected request {path}")

    cat.request = provider
    cat.enqueue("artist", ARTIST, sections=["release-group"])
    scope = cat.claim()
    cat.process_page(scope)
    # An incomplete browse must not partially publish the new catalogue.
    with cat.db() as con:
        assert con.execute("SELECT COUNT(*) FROM scope_members").fetchone()[0] == 0
    scope = cat.claim()
    assert scope["cursor"] == 3
    cat.process_page(scope)
    with cat.db() as con:
        assert con.execute("SELECT COUNT(*) FROM scope_members").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM scopes WHERE kind='release-group' AND section='release'").fetchone()[0] == 4
        # Force this release-group to run before the other groups.
        con.execute("UPDATE scopes SET requested_at='2000-01-01T00:00:00Z' WHERE entity_id=?", (groups[0],))
    group_scope = cat.claim()
    assert group_scope["kind"] == "release-group" and group_scope["entity_id"] == groups[0]
    cat.process_page(group_scope)
    with cat.db() as con:
        con.execute("UPDATE scopes SET requested_at='1999-01-01T00:00:00Z' WHERE entity_id=?", (release,))
    release_scope = cat.claim()
    assert release_scope["kind"] == "release" and release_scope["entity_id"] == release
    cat.process_page(release_scope)
    assignment = cat.release_assignment(release, recording)
    assert assignment and assignment["album"] == "Album"
    assert ("/release-group", {"limit": 100, "offset": 3, "artist": ARTIST}) in received


def test_graph_node_type_filters_traverse_hidden_recordings(db, environment):
    cat, _ = setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_2)
    albums = cat.graph("admin", view="downloaded", kinds=["release"])
    assert {n["kind"] for n in albums["nodes"]} == {"release"}
    assert f"release:{RELEASE_A}" in {n["id"] for n in albums["nodes"]}
    artists = cat.graph("admin", view="downloaded", kinds=["artist"])
    assert f"artist:{ARTIST}" in {n["id"] for n in artists["nodes"]}
    assert not any(n["kind"] == "recording" for n in artists["nodes"])


def test_batch_indexing_does_not_duplicate_work(db):
    cat = Catalogue(db)
    cat.init()
    first = cat.index_recordings([RECORDING_1, RECORDING_2, RECORDING_1])
    assert first == {"recordings": 2, "new_requests": 4,
                     "note": "One request at a time, cached and resumable; no library-wide recomputation."}
    second = cat.index_recordings([RECORDING_1, RECORDING_2])
    assert second["new_requests"] == 0


def test_album_updates_block_until_navidrome_move_checkpoint(db, environment, monkeypatch):
    import album_management
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    cat, manager = setup_catalogue(db)
    tid, _, _ = add_audio_track(db, environment)
    journal = db.path.parent / "stable-path-migration.json"
    journal.write_text(json.dumps({"state": "AWAITING_NAVIDROME_SCAN"}))
    with pytest.raises(ValueError, match="not acknowledged"):
        manager.preview("admin", RELEASE_B, track_ids=[tid])
    journal.write_text(json.dumps({"state": "COMPLETE"}))
    assert manager.preview("admin", RELEASE_B, track_ids=[tid])["can_apply"] == 1


def test_app_refuses_startup_before_navidrome_scan_ack(db):
    from fastapi.testclient import TestClient
    from main import create_app
    journal = db.path.parent / "stable-path-migration.json"
    journal.write_text(json.dumps({"state": "AWAITING_NAVIDROME_SCAN"}))
    with pytest.raises(RuntimeError, match="Stable-path migration is AWAITING_NAVIDROME_SCAN"):
        with TestClient(create_app(db, start_workers=False)):
            pass
