"""Persistent catalogue, graph, album preview/commit/undo and user isolation."""
from __future__ import annotations

import asyncio
import json
import time
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from album_management import AlbumManager
from catalogue import Catalogue
from common import sha256_file, utcnow
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


def test_local_only_catalogue_search_uses_per_user_musicbrainz_identity(db, environment):
    """Exact local IDs, not display names or shared public cache entries."""
    cat, _ = setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_2)
    add_audio_track(db, environment, RECORDING_4, user="guest")
    other_artist = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    work = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with cat.db() as con:
        # Same artist label, unrelated MusicBrainz identity.
        cat.ingest(con, "artist", {"id": other_artist, "name": "Composer"})
        cat.ingest(con, "recording", {
            "id": RECORDING_2, "title": "My recording",
            "artist-credit": [{"artist": {"id": ARTIST, "name": "Composer"}}],
            "relations": [{"target-type": "work", "type": "performance",
                           "work": {"id": work, "title": "My composition"}}],
        })
        cat.ingest(con, "recording", {
            "id": RECORDING_4, "title": "Other user's recording",
            "artist-credit": [{"artist": {"id": other_artist, "name": "Composer"}}],
        })

    def ids(kind, name, user="admin", **kwargs):
        return {item["id"] for item in cat.search(
            kind, name, user_id=user, local_only=True, **kwargs)["items"]}

    assert ids("artist", "Composer") == {ARTIST}
    assert ids("recording", "recording") == {RECORDING_2}
    assert ids("release", "Preview") == {RELEASE_A}
    assert ids("release-group", "Soundtrack") == {GROUP}
    assert ids("work", "composition") == {work}
    assert ids("recording", "recording", user="guest") == {RECORDING_4}
    assert ids("artist", "Composer", user="guest") == {ARTIST, other_artist}
    assert {item["id"] for item in cat.search("artist", "Composer")["items"]} == {ARTIST, other_artist}

    # Remote searches still cache all public hits, but only return locally
    # linked artists when explicitly filtered.
    cat.request = lambda path, params: {"artists": [
        {"id": other_artist, "name": "Composer"},
        {"id": ARTIST, "name": "Composer"},
    ], "count": 2}
    assert ids("artist", "Composer", remote=True) == {ARTIST}
    # The cache query must filter *before* limiting results.
    assert ids("artist", "Composer", limit=1) == {ARTIST}
    with pytest.raises(ValueError, match="Unknown user"):
        cat.search("artist", "Composer", user_id="not-a-user", local_only=True)


def test_local_catalogue_filter_waits_for_relationship_index(db, environment):
    cat = Catalogue(db)
    cat.init()
    add_audio_track(db, environment, RECORDING_1)
    with cat.db() as con:
        cat.ingest(con, "artist", {"id": ARTIST, "name": "Creo"})
        cat.ingest(con, "recording", {"id": RECORDING_1, "title": "Song"})
    assert cat.search("artist", "Creo", user_id="admin", local_only=True)["items"] == []
    assert len(cat.search("artist", "Creo")["items"]) == 1
    with cat.db() as con:
        cat.ingest(con, "recording", {
            "id": RECORDING_1, "title": "Song",
            "artist-credit": [{"artist": {"id": ARTIST, "name": "Creo"}}],
        })
    assert {x["id"] for x in cat.search("artist", "Creo", user_id="admin",
                                        local_only=True)["items"]} == {ARTIST}


def test_catalogue_prefers_english_alias_but_finds_native_script(db):
    cat = Catalogue(db)
    cat.init()
    with cat.db() as con:
        cat.ingest(con, "artist", {
            "id": ARTIST, "name": "西木康智",
            "aliases": [{"name": "Yasunori Nishiki", "locale": "en", "primary": True}],
        })
    assert cat.get("artist", ARTIST)["entity"]["name"] == "Yasunori Nishiki"
    assert [r["id"] for r in cat.search("artist", "Yasunori")["items"]] == [ARTIST]
    assert [r["id"] for r in cat.search("artist", "西木")["items"]] == [ARTIST]
    assert cat.get("artist", ARTIST)["entity"]["data"]["name"] == "西木康智"


def test_existing_library_localization_is_previewed_and_confirmed(db, environment):
    """Indexing stays read-only; explicit language jobs do not queue downloads."""
    import json
    from mutagen.oggopus import OggOpus
    from language_jobs import preview as english_preview
    cat = Catalogue(db)
    cat.init()
    first, path, _ = add_audio_track(db, environment, RECORDING_1)
    edited, _, _ = add_audio_track(db, environment, RECORDING_2)
    tagged, tagged_path, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET metadata_edited=1 WHERE id=?", (edited,))
    already = OggOpus(tagged_path)
    already["INGESTOR_LANGUAGE_POLICY"] = ["english-first-v1"]
    already.save()

    result = english_preview(db, "admin")
    assert result["eligible"] == 1
    assert result["skipped"]["already_localized"] == 1
    assert result["skipped"]["manual_or_busy"] == 1
    assert result["sample"][0]["id"] == first
    with TestClient(create_app(db, start_workers=False)) as client:
        guest_preview = client.get("/api/catalogue/language-preview",
                                   params={"user_id": "guest"})
        assert guest_preview.status_code == 200
        assert guest_preview.json()["eligible"] == 0, "No other library's tracks should leak"
        assert client.get("/api/catalogue/language-preview",
                          params={"user_id": "missing-library"}).status_code == 400
        assert client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 3,
        }).status_code == 422
        queued = client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 3, "confirmed": True,
        })
        assert queued.status_code == 200
        assert queued.json()["queued"] == 1
        job = db.one("SELECT kind,payload FROM jobs WHERE target=? ORDER BY id DESC", (first,))
        assert job["kind"] == "track" and json.loads(job["payload"])["mode"] == "language"
        assert db.one("SELECT operation_state FROM tracks WHERE id=?", (first,))["operation_state"] == "QUEUED"
        assert OggOpus(path)["title"] == ["Track"], "Queueing is read-only until the worker runs"
        assert client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 3, "confirmed": True,
        }).json()["queued"] == 0


def test_language_job_preserves_stable_path_and_creates_undo_copy(db, environment):
    """A confirmed language job swaps only tags and refreshes the current asset."""
    import asyncio
    from mutagen.oggopus import OggOpus
    from localized_names import apply_to_opus, LANGUAGE_POLICY
    from pipeline import Pipeline
    tid, path, original_sha = add_audio_track(db, environment, RECORDING_1)
    assert path.parent.name == "audio"
    db.queue_track(tid, "admin", {"mode": "language"})
    job = db.claim()
    assert job and job["target"] == tid

    worker = Pipeline(db)

    async def fake_beets_bridge(job, track, directory, mode, **kwargs):
        if mode == "language":
            assert kwargs["path"] != str(path)
            return apply_to_opus(kwargs["path"],
                {"title": "Primrose, the Dancer", "artist": "Yasunori Nishiki",
                 "album": "Octopath Traveler"},
                {"title": "踊子プリムロゼのテーマ", "artist": "西木康智",
                 "album": "オクトパストラベラー"}, include_album=True)
        assert mode == "refresh"
        return {"beets_id": 17, "matched_title": "Yasunori Nishiki - Primrose, the Dancer"}

    worker.bridge = fake_beets_bridge
    asyncio.run(worker.process_track(job))

    new_row = db.one("SELECT file_path,mbid,release_id,matched_title FROM tracks WHERE id=?", (tid,))
    assert new_row["file_path"] == str(path)
    assert new_row["mbid"] == RECORDING_1
    assert new_row["release_id"] == RELEASE_A
    assert new_row["matched_title"] == "Yasunori Nishiki - Primrose, the Dancer"
    assert db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "RUNNING"
    raw = OggOpus(path)
    assert raw["title"] == ["Primrose, the Dancer"]
    assert raw["originaltitle"] == ["踊子プリムロゼのテーマ"]
    assert raw["ingestor_language_policy"] == [LANGUAGE_POLICY]
    assert db.one("SELECT sha256 FROM assets WHERE track_id=? AND state='CURRENT'", (tid,))["sha256"] == sha256_file(path)
    snapshots = list((environment[0] / "language-undo" / "admin").glob(tid + "-*.opus"))
    assert len(snapshots) == 1
    assert sha256_file(snapshots[0]) == original_sha
    assert OggOpus(snapshots[0])["title"] == ["Track"]
    assert not list((environment[1] / "admin" / "audio").glob(tid + ".1.opus"))
    events = db.rows("SELECT message FROM events WHERE user_id=? ORDER BY id", ("admin",))
    assert any("Visible metadata changed" in event["message"] and
               "Primrose, the Dancer" in event["message"] for event in events)


def test_confirmed_release_alias_is_english_and_native_album_is_preserved(db, environment):
    """Album-only edits never change the confirmed MusicBrainz recording."""
    from mutagen.oggopus import OggOpus
    cat, albums = setup_catalogue(db)
    tid, path, _ = add_audio_track(db, environment, RECORDING_2)
    with cat.db() as con:
        cat.upsert_entity(con, "release", RELEASE_B, {
            "id": RELEASE_B, "title": "オリジナル・サウンドトラック",
            "aliases": [{"name": "Original Soundtrack",
                         "locale": "en", "primary": True}],
        })
    tags = cat.release_assignment(RELEASE_B, RECORDING_2)
    assert tags["album"] == "Original Soundtrack"
    assert tags["native_album"] == "オリジナル・サウンドトラック"
    previous_title = OggOpus(path)["title"]
    albums._retag(path, tags)
    raw = OggOpus(path)
    assert raw["title"] == previous_title, "Album edition selection never changes the music title"
    assert raw["album"] == ["Original Soundtrack"]
    assert raw["originalalbum"] == ["オリジナル・サウンドトラック"]
    assert raw["musicbrainz_trackid"] == [RECORDING_2]


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



def test_graph_node_totals_do_not_depend_on_requested_display_limit(db, environment):
    cat, _ = setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_1)
    tiny = cat.graph("admin", view="downloaded", kinds=["recording", "release", "artist"], limit=1)
    full = cat.graph("admin", view="downloaded", kinds=["recording", "release", "artist"], limit=10000)
    assert tiny["total_nodes"] == full["total_nodes"] == len(full["nodes"])
    assert tiny["total_nodes"] > 1
    assert tiny["limit"] == len(tiny["nodes"]) == 1
    assert tiny["truncated"] is True
    assert full["max_selectable_nodes"] == full["total_nodes"]
    assert full["limit"] == full["total_nodes"]
    assert not full["truncated"]
    artists = cat.graph("admin", view="downloaded", kinds=["artist"], limit=1)
    assert artists["total_nodes"] >= len(artists["nodes"])
    assert all(node["kind"] == "artist" for node in artists["nodes"])
    empty = cat.graph("admin", view="downloaded", kinds=[])
    assert empty["total_nodes"] == empty["max_selectable_nodes"] == 0


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


def test_language_review_reports_activity_and_cached_english_preview(db, environment):
    from language_jobs import preview, preview_progress
    cat, _ = setup_catalogue(db)
    track, path, original_hash = add_audio_track(db, environment, RECORDING_1)
    with cat.db() as con:
        cat.ingest(con, "artist", {"id": ARTIST, "name": "西木康智",
            "aliases": [{"name": "Yasunori Nishiki", "locale": "en", "primary": True}]})
        cat.ingest(con, "recording", {"id": RECORDING_1, "title": "原題",
            "aliases": [{"name": "English Song", "locale": "en", "primary": True}],
            "artist-credit": [{"artist": {"id": ARTIST, "name": "西木康智"}}]})
    result = preview(db, "admin")
    assert result["eligible"] == 1
    assert result["sample"][0]["id"] == track
    assert result["sample"][0]["proposed_name"] == "Yasunori Nishiki - English Song"
    assert result["sample"][0]["canonical_name"] == "西木康智 - 原題"
    assert sha256_file(path) == original_hash, "Read-only review must not write Opus tags"
    assert preview_progress.get("admin")["state"] == "complete"
    events = db.rows("SELECT message FROM events WHERE user_id=? ORDER BY id", ("admin",))
    assert any("English-name review started" in event["message"] for event in events)
    assert any("No audio or tags were changed" in event["message"] for event in events)


def test_language_review_start_uses_run_id_and_pollable_completed_summary(db, environment):
    from language_jobs import PreviewProgress, preview_progress
    setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_1)
    with TestClient(create_app(db, start_workers=False)) as client:
        started = client.post("/api/catalogue/language-review-start", json={
            "user_id": "admin", "include_localized": False,
        })
        assert started.status_code == 200
        assert started.json()["started"] is True
        assert len(started.json()["run_id"]) == 32
        status = client.get("/api/catalogue/language-progress",
                            params={"user_id": "admin"}).json()
        assert status["run_id"] == started.json()["run_id"]
        assert status["state"] == "complete"
        saved = client.get("/api/catalogue/language-review-state",
                           params={"user_id": "admin"}).json()
        assert saved["state"] == "ready"
        assert saved["result"]["review_token"]

    progress = PreviewProgress()
    first = progress.start("admin", (False,))
    assert first["started"]
    assert progress.start("admin", (False,)) == {
        "state": "running", "run_id": first["run_id"], "started": False,
    }
    with pytest.raises(ValueError, match="already running"):
        progress.start("admin", (True,))
    progress.update("admin", state="complete")
    assert progress.start("admin", (True,))["run_id"] != first["run_id"]


def test_language_canary_background_queue_reports_completion(db, environment):
    setup_catalogue(db)
    add_audio_track(db, environment, RECORDING_1)
    with TestClient(create_app(db, start_workers=False)) as client:
        started = client.post("/api/catalogue/language-apply-start", json={
            "user_id": "admin", "limit": 3, "confirmed": True,
        })
        assert started.status_code == 200
        progress = client.get("/api/catalogue/language-apply-progress",
                              params={"user_id": "admin"}).json()
        assert progress["state"] == "complete"
        assert progress["run_id"] == started.json()["run_id"]
        assert progress["queued"] == 1


def test_bulk_english_retag_requires_completed_verified_canary(db, environment):
    from language_jobs import preview, canary_status
    from mutagen.oggopus import OggOpus
    from localized_names import LANGUAGE_POLICY
    tid, path, _ = add_audio_track(db, environment, RECORDING_1)
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 5000, "confirmed": True,
        }).status_code == 400
        response = client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 3, "confirmed": True,
        })
        assert response.status_code == 200 and response.json()["queued"] == 1
        assert canary_status(db, "admin")["state"] == "running"
        assert client.post("/api/catalogue/language-canary-verify", json={
            "user_id": "admin", "confirmed": True,
        }).status_code == 400
        audio = OggOpus(path)
        audio["INGESTOR_LANGUAGE_POLICY"] = [LANGUAGE_POLICY]
        audio.save()
        db.execute("UPDATE tracks SET operation_state='IDLE' WHERE id=?", (tid,))
        assert canary_status(db, "admin")["state"] == "ready"
        verified = client.post("/api/catalogue/language-canary-verify", json={
            "user_id": "admin", "confirmed": True,
        })
        assert verified.status_code == 200
        assert verified.json()["verified"] is True
        assert client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 5000, "confirmed": True,
            "include_localized": True,
        }).status_code == 400, "Changing recheck mode requires a new canary"
        bulk = client.post("/api/catalogue/language-apply", json={
            "user_id": "admin", "limit": 5000, "confirmed": True,
        })
        assert bulk.status_code == 200
        assert bulk.json()["queued"] == 0, "Canary IDs must not be queued twice"
        plain = client.get("/api/catalogue/language-preview", params={"user_id": "admin"})
        assert plain.status_code == 200
        assert plain.json()["skipped"]["already_localized"] == 1
        refreshed = client.get("/api/catalogue/language-preview", params={
            "user_id": "admin", "include_localized": "true",
        })
        assert refreshed.status_code == 200
        assert refreshed.json()["eligible"] == 1
        progress = client.get("/api/catalogue/language-progress", params={"user_id": "admin"})
        assert progress.json()["state"] == "complete"
        assert client.get("/api/catalogue/language-progress", params={"user_id": "missing"}).status_code == 400



def test_indexed_recording_queues_exact_artist_alias_lookup(db):
    cat = Catalogue(db)
    cat.init()
    cat.index_recordings([RECORDING_1])
    cat.request = lambda endpoint, params: {
        "id": RECORDING_1, "title": "Original recording",
        "artist-credit": [{"artist": {"id": ARTIST, "name": "原作者"}}],
        "releases": [],
    }
    first = cat.claim()
    assert first["kind"] == "recording" and first["section"] == "lookup"
    cat.process_page(first)
    with cat.db() as con:
        scope = con.execute(
            "SELECT state FROM scopes WHERE kind='artist' AND entity_id=? AND section='lookup'",
            (ARTIST,),
        ).fetchone()
        assert scope is not None and scope["state"] == "PENDING"
        row = con.execute(
            "SELECT data FROM entities WHERE kind='artist' AND id=?", (ARTIST,)
        ).fetchone()
        assert json.loads(row["data"])["name"] == "原作者"




def test_language_diff_review_separates_name_tags_unknown_and_noops(db, environment):
    """A safe file must not automatically count as a necessary name change."""
    from mutagen.oggopus import OggOpus
    from language_jobs import preview, review_state
    cat, _ = setup_catalogue(db)
    changed_id, changed_path, original_hash = add_audio_track(db, environment, RECORDING_1)
    original_id, original_path, _ = add_audio_track(db, environment, RECORDING_2)
    unknown_id, _, _ = add_audio_track(db, environment, RECORDING_4)
    with cat.db() as con:
        cat.ingest(con, "recording", {
            "id": RECORDING_1, "title": "原題",
            "aliases": [{"name": "English title", "locale": "en", "primary": True}],
            "artist-credit": [{"artist": {"id": ARTIST, "name": "作曲家"}}],
        })
        cat.ingest(con, "recording", {
            "id": RECORDING_2, "title": "Track",
            "artist-credit": [{"artist": {"id": "99999999-9999-4999-8999-999999999999",
                                          "name": "Artist"}}],
        })
    # The second track already has its desired visible fields, but lacks two
    # canonical-name tags. The confirmed album's original tag is already right.
    tags = OggOpus(original_path)
    tags["album"] = ["Preview"]
    tags["albumartist"] = ["Composer"]
    tags["originalalbum"] = ["Preview"]
    tags["originalalbumartist"] = ["Composer"]
    tags.save()
    db.execute("UPDATE assets SET sha256=? WHERE track_id=?",
               (sha256_file(original_path), original_id))
    result = preview(db, "admin")
    assert result["eligible"] == 3
    assert result["changes"] == {
        "display_changes": 1, "original_updates": 1, "unchanged": 0, "unknown": 1,
    }
    assert [item["kind"] for item in result["sample"]][:3] == [
        "display_changes", "original_updates", "unknown",
    ]
    assert result["review_token"] and len(result["review_token"]) == 32
    assert review_state(db, "admin")["result"]["review_token"] == result["review_token"]
    assert sha256_file(changed_path) == original_hash, "Review must not write any tags"


def test_language_ui_scoped_apply_uses_recent_review_and_requires_matching_canary(db, environment):
    from mutagen.oggopus import OggOpus
    from language_jobs import preview, canary_status, batch_status
    cat, _ = setup_catalogue(db)
    name_id, name_path, _ = add_audio_track(db, environment, RECORDING_1)
    native_id, native_path, _ = add_audio_track(db, environment, RECORDING_2)
    unknown_id, _, _ = add_audio_track(db, environment, RECORDING_4)
    with cat.db() as con:
        cat.ingest(con, "recording", {
            "id": RECORDING_1, "title": "原題",
            "aliases": [{"name": "English title", "locale": "en"}],
            "artist-credit": [{"artist": {"id": ARTIST, "name": "Composer"}}],
        })
        cat.ingest(con, "recording", {
            "id": RECORDING_2, "title": "Track",
            "artist-credit": [{"artist": {"id": ARTIST, "name": "Artist"}}],
        })
    tags = OggOpus(native_path)
    tags["album"] = ["Preview"]
    tags["albumartist"] = ["Composer"]
    tags["originalalbum"] = ["Preview"]
    tags["originalalbumartist"] = ["Composer"]
    tags.save()
    db.execute("UPDATE assets SET sha256=? WHERE track_id=?",
               (sha256_file(native_path), native_id))
    review = preview(db, "admin")
    token = review["review_token"]
    assert review["changes"]["display_changes"] == 1
    assert review["changes"]["original_updates"] == 1
    assert review["changes"]["unknown"] == 1
    with TestClient(create_app(db, start_workers=False)) as client:
        data = {"user_id": "admin", "limit": 3, "confirmed": True,
                "scope": "names", "review_token": token}
        assert client.post("/api/catalogue/language-apply", json={
            **data, "review_token": "0" * 32,
        }).status_code == 400, "Forged review tokens must be rejected"
        sent = client.post("/api/catalogue/language-apply", json=data)
        assert sent.status_code == 200, sent.text
        assert sent.json()["queued"] == 1, "Original-tag-only and unknown records must not be included"
        assert canary_status(db, "admin")["review_tested"] == 1
        targets = {row["target"] for row in db.rows(
            "SELECT target FROM jobs WHERE kind='track' AND payload LIKE '%language%'")}
        assert targets == {name_id}
        batch = client.get("/api/catalogue/language-batch-status",
                           params={"user_id": "admin"}).json()
        assert batch["submitted"] == 1 and batch["pending"] == 1
        assert batch["review_token"] == token, "Batch must refer to this exact review"
        assert client.post("/api/catalogue/language-apply", json={
            **data, "limit": 5000,
        }).status_code == 400, "Bulk cannot bypass the canary"
        assert client.get("/api/catalogue/language-review-state",
                          params={"user_id": "guest"}).json()["state"] == "idle"
        assert client.get("/api/catalogue/language-batch-status",
                          params={"user_id": "not-a-user"}).status_code == 400

        # Simulate completed language job and Navidrome scan.
        tags = OggOpus(name_path)
        tags["INGESTOR_LANGUAGE_POLICY"] = ["english-first-v1"]
        tags.save()
        db.execute("UPDATE tracks SET operation_state='IDLE' WHERE id=?", (name_id,))
        assert canary_status(db, "admin")["state"] == "ready"
        verified = client.post("/api/catalogue/language-canary-verify", json={
            "user_id": "admin", "confirmed": True,
        })
        assert verified.status_code == 200
        assert verified.json()["scope"] == "names"
        # Retrying the review after the test omits now-tagged tracks. The
        # remaining counter must subtract only test IDs still in the review.
        latest_review = preview(db, "admin")
        data["review_token"] = latest_review["review_token"]
        assert canary_status(db, "admin")["review_tested"] == 0
        assert client.post("/api/catalogue/language-apply", json={
            **data, "limit": 5000, "scope": "names_and_originals",
        }).status_code == 400, "A broader scope needs a new canary"
        remainder = client.post("/api/catalogue/language-apply", json={
            **data, "limit": 5000,
        })
        assert remainder.status_code == 200
        assert remainder.json()["queued"] == 0, "Already-tested track excluded from remainder"
        assert db.one("SELECT operation_state FROM tracks WHERE id=?", (native_id,))["operation_state"] == "IDLE"
        assert db.one("SELECT operation_state FROM tracks WHERE id=?", (unknown_id,))["operation_state"] == "IDLE"


def test_language_name_changes_list_only_visible_diffs_and_paginates(db, environment):
    from language_jobs import preview, name_changes
    from mutagen.oggopus import OggOpus
    cat, _ = setup_catalogue(db)
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    second, _, _ = add_audio_track(db, environment, RECORDING_2)
    original_only, path, _ = add_audio_track(db, environment, RECORDING_4)
    with cat.db() as con:
        for mbid, title in ((RECORDING_1, "First Song"),
                             (RECORDING_2, "Second Song"),
                             (RECORDING_4, "Track")):
            cat.ingest(con, "recording", {
                "id": mbid, "title": title,
                "artist-credit": [{"name": "Artist",
                                   "artist": {"id": ARTIST, "name": "Artist"}}],
            })
    # Make the third track visibly correct but without ORIGINAL* tags.
    audio = OggOpus(path)
    audio["album"] = ["Preview"]
    audio["albumartist"] = ["Composer"]
    audio.save()
    db.execute("UPDATE assets SET sha256=? WHERE track_id=?",
               (sha256_file(path), original_only))
    review = preview(db, "admin")
    assert review["changes"]["display_changes"] == 2
    assert review["changes"]["original_updates"] == 1
    token = review["review_token"]
    first_page = name_changes(db, "admin", token, offset=0, limit=1)
    second_page = name_changes(db, "admin", token, offset=1, limit=1)
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["next_offset"] == 1 and second_page["next_offset"] is None
    assert {first_page["items"][0]["id"], second_page["items"][0]["id"]} == {first, second}
    for item in first_page["items"] + second_page["items"]:
        assert item["before_label"] == "Artist - Track"
        assert item["after_label"].startswith("Artist - ")
        assert item["changed_field"] == "Title / artist"
        assert item["job_state"] is None
    assert original_only not in {x["id"] for x in first_page["items"] + second_page["items"]}
    with pytest.raises(ValueError, match="expired or changed"):
        name_changes(db, "admin", "0" * 32)
    with pytest.raises(ValueError, match="Invalid"):
        name_changes(db, "admin", token, limit=101)


def test_language_name_change_queues_exact_reviewed_track_without_resetting_canary(db, environment):
    from language_jobs import preview, canary_status, name_change_status
    cat, _ = setup_catalogue(db)
    target, _, _ = add_audio_track(db, environment, RECORDING_1)
    other, _, _ = add_audio_track(db, environment, RECORDING_2)
    with cat.db() as con:
        cat.ingest(con, "recording", {"id": RECORDING_1, "title": "English Song",
            "artist-credit": [{"name": "Artist", "artist": {"id": ARTIST, "name": "Artist"}}]})
    review = preview(db, "admin")
    token = review["review_token"]
    with TestClient(create_app(db, start_workers=False)) as client:
        request = {"user_id": "admin", "track_id": target,
                   "review_token": token, "confirmed": True}
        assert client.post("/api/catalogue/language-name-change", json={
            **request, "review_token": "0" * 32,
        }).status_code == 400
        assert client.post("/api/catalogue/language-name-change", json={
            **request, "track_id": other,
        }).status_code == 400, "Track must be among reviewed visible-name changes"
        response = client.post("/api/catalogue/language-name-change", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["track_id"] == target
        assert response.json()["queued"] == 1
        jobs = db.rows("SELECT target,payload,state FROM jobs WHERE kind='track'")
        assert len(jobs) == 1 and jobs[0]["target"] == target
        assert json.loads(jobs[0]["payload"])["review_token"] == token
        assert jobs[0]["state"] == "PENDING"
        assert canary_status(db, "admin")["state"] == "not_started"
        status = client.get("/api/catalogue/language-name-change-status", params={
            "user_id": "admin", "review_token": token,
        })
        assert status.status_code == 200
        assert status.json()["states"] == {target: "PENDING"}
        page = client.get("/api/catalogue/language-name-changes", params={
            "user_id": "admin", "review_token": token,
        })
        assert page.status_code == 200
        assert page.json()["items"][0]["job_state"] == "PENDING"
        assert client.post("/api/catalogue/language-name-change", json=request).status_code == 400
        assert client.get("/api/catalogue/language-name-changes", params={
            "user_id": "guest", "review_token": token,
        }).status_code == 400, "Reviews cannot cross library users"


def test_language_single_change_rejects_changed_assets_and_manual_tags(db, environment):
    from language_jobs import preview, queue_one_name_change
    cat, _ = setup_catalogue(db)
    track, _, _ = add_audio_track(db, environment, RECORDING_1)
    with cat.db() as con:
        cat.ingest(con, "recording", {"id": RECORDING_1, "title": "English Song",
            "artist-credit": [{"name": "Artist", "artist": {"id": ARTIST, "name": "Artist"}}]})
    token = preview(db, "admin")["review_token"]
    db.execute("UPDATE tracks SET metadata_edited=1 WHERE id=?", (track,))
    with pytest.raises(ValueError, match="no longer safe"):
        queue_one_name_change(db, "admin", track, token)
    db.execute("UPDATE tracks SET metadata_edited=0 WHERE id=?", (track,))
    db.execute("UPDATE assets SET sha256='external-change' WHERE track_id=?", (track,))
    with pytest.raises(ValueError, match="asset changed"):
        queue_one_name_change(db, "admin", track, token)
    assert db.rows("SELECT id FROM jobs WHERE kind='track'") == []



def test_smart_filter_matches_list_canary_and_bulk_without_hidden_updates(db, environment):
    """A checked filter must constrain actual writes, not merely hide DOM rows."""
    import time
    from mutagen.oggopus import OggOpus
    from language_jobs import canary_status, batch_status

    setup_catalogue(db)
    samples = [
        ("The Toxic Avenger - It Doesn't Matter",
         "The Toxic Avenger - It Doesn’t Matter", False),
        ("LORN - Drawn Out Like an Ache",
         "Lorn - Drawn Out Like an Ache", False),
        ("Gareth Coker - Seperated by the Storm",
         "Gareth Coker - Separated by the Storm", True),
        ("Toby Fox - サーカス", "Toby Fox - The Circus", True),
        ("KEYGEN CHURCH - Duhicebdo ed varicecag parvabiguf",
         "KEYGEN CHURCH - Dubitando ad veritatem pervenimus", True),
        ("Daft Punk - Within", "Daft Punk feat. Chilly Gonzales - Within", True),
    ]
    track_by_id = {}
    display_rows = []
    assets = {}
    meaningful_ids = set()
    for before, after, meaningful in samples:
        # One confirmed recording identity per track: the production schema
        # correctly forbids two rows sharing a user's MusicBrainz MBID.
        track_id, path, sha = add_audio_track(db, environment, str(uuid.uuid4()))
        track_by_id[track_id] = path
        assets[track_id] = sha
        display_rows.append({"id": track_id, "before_label": before,
                             "after_label": after, "changed_field": "Title / artist"})
        if meaningful:
            meaningful_ids.add(track_id)
    token = "f" * 32
    db.set_setting("english-language-review:admin", {
        "token": token, "created": time.time(), "include_localized": False,
        "by_kind": {"display_changes": list(track_by_id), "original_updates": []},
        "asset_hashes": assets, "display_rows": display_rows,
        "summary": {"review_token": token, "changes": {"display_changes": 6}},
    })
    with TestClient(create_app(db, start_workers=False)) as client:
        args = {"user_id": "admin", "review_token": token}
        smart = client.get("/api/catalogue/language-name-changes",
                           params={**args, "smart_only": "true"}).json()
        all_changes = client.get("/api/catalogue/language-name-changes",
                                 params={**args, "smart_only": "false"}).json()
        assert smart["total"] == 4
        assert smart["all_total"] == 6 and smart["filtered_out"] == 2
        assert all_changes["total"] == 6 and all_changes["filtered_out"] == 0
        assert {row["id"] for row in smart["items"]} == meaningful_ids

        request = {**args, "scope": "names", "confirmed": True,
                   "smart_only": True, "include_localized": False}
        canary = client.post("/api/catalogue/language-apply", json={
            **request, "limit": 3,
        })
        assert canary.status_code == 200, canary.text
        assert canary.json()["queued"] == 3
        status = canary_status(db, "admin")
        assert status["smart_only"] is True
        assert set(status["track_ids"]) <= meaningful_ids

        for track_id in status["track_ids"]:
            tags = OggOpus(track_by_id[track_id])
            tags["INGESTOR_LANGUAGE_POLICY"] = ["english-first-v1"]
            tags.save()
            db.execute("UPDATE tracks SET operation_state='IDLE' WHERE id=?",
                       (track_id,))
        assert client.post("/api/catalogue/language-canary-verify", json={
            "user_id": "admin", "confirmed": True,
        }).status_code == 200

        unsafe = client.post("/api/catalogue/language-apply", json={
            **request, "limit": 5000, "smart_only": False,
        })
        assert unsafe.status_code == 400
        assert "canary" in unsafe.text.lower() or "verify" in unsafe.text.lower()

        bulk = client.post("/api/catalogue/language-apply", json={
            **request, "limit": 5000,
        })
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["queued"] == 1
        assert batch_status(db, "admin")["smart_only"] is True
        actual = {job["target"] for job in db.rows(
            "SELECT target FROM jobs WHERE kind='track' AND payload LIKE '%language%'")}
        assert actual == meaningful_ids, "The two hidden suggestions must never be queued"
        assert client.post("/api/catalogue/language-apply", json={
            **request, "limit": 5000,
        }).json()["queued"] == 0, "Do not queue successful jobs twice"


def test_reverse_accepted_language_update_restores_exact_opus_and_same_musicbrainz_identity(db, environment):
    import asyncio
    import time
    from mutagen.oggopus import OggOpus
    from localized_names import apply_to_opus
    from language_jobs import name_changes
    from language_reverse import undo_info
    from pipeline import Pipeline

    track_id, path, original_sha = add_audio_track(db, environment, RECORDING_1)
    original_tags = dict(OggOpus(path))
    original_row = db.one("SELECT mbid,release_id,matched_title,file_path FROM tracks WHERE id=?", (track_id,))
    token = "f" * 32
    db.set_setting("english-language-review:admin", {
        "token": token, "created": time.time(), "include_localized": False,
        "by_kind": {"display_changes": [track_id], "original_updates": []},
        "asset_hashes": {track_id: original_sha},
        "display_rows": [{"id": track_id, "before_label": "Artist - Track",
                          "after_label": "Yasunori Nishiki - Primrose, the Dancer",
                          "changed_field": "Title / artist"}],
        "summary": {"review_token": token},
    })
    db.queue_track(track_id, "admin", {"mode": "language", "review_token": token})
    job = db.claim()
    worker = Pipeline(db)

    async def fake_bridge(job, track, directory, mode, **kwargs):
        if mode == "language":
            return apply_to_opus(
                kwargs["path"],
                {"title": "Primrose, the Dancer", "artist": "Yasunori Nishiki",
                 "album": "Octopath Traveler"},
                {"title": "踊子プリムロゼのテーマ", "artist": "西木康智",
                 "album": "オクトパストラベラー"},
                include_album=True,
            )
        assert mode == "refresh"
        file_tags = OggOpus(kwargs["path"])
        return {"beets_id": 17, "matched_title": file_tags["artist"][0] + " - " + file_tags["title"][0]}

    worker.bridge = fake_bridge
    asyncio.run(worker.process_track(job))
    db.finish_job(job["id"])
    updated_sha = sha256_file(path)
    assert updated_sha != original_sha
    assert undo_info(db, "admin", track_id, token, verify_bytes=True) is not None
    assert name_changes(db, "admin", token)["items"][0]["job_state"] == "REVERSIBLE"

    with TestClient(create_app(db, start_workers=False)) as client:
        payload = {"user_id": "admin", "track_id": track_id, "review_token": token, "confirmed": True}
        assert client.post("/api/catalogue/language-reverse", json={
            **payload, "review_token": "0" * 32,
        }).status_code == 400
        queued = client.post("/api/catalogue/language-reverse", json=payload)
        assert queued.status_code == 200, queued.text
        assert queued.json()["queued"] == 1
        assert name_changes(db, "admin", token)["items"][0]["job_state"] == "REVERSE_PENDING"
        assert client.post("/api/catalogue/language-reverse", json=payload).status_code == 400
    reverse_job = db.claim()
    assert json.loads(reverse_job["payload"])["mode"] == "language_reverse"
    asyncio.run(worker.process_track(reverse_job))
    db.finish_job(reverse_job["id"])
    assert sha256_file(path) == original_sha
    assert dict(OggOpus(path)) == original_tags, "Reverse must restore original Opus tags byte for byte"
    after = db.one("SELECT mbid,release_id,file_path FROM tracks WHERE id=?", (track_id,))
    for key in ("mbid", "release_id", "file_path"):
        assert after[key] == original_row[key]
    assert name_changes(db, "admin", token)["items"][0]["job_state"] == "REVERSED"
    assert undo_info(db, "admin", track_id, token) is None
    assert not list(path.parent.glob(track_id + ".1.opus"))
    events = db.rows("SELECT message FROM events WHERE user_id=? ORDER BY id", ("admin",))
    assert any("Reversed English-name update" in row["message"] for row in events)


def test_reverse_rejects_external_audio_modifications_and_missing_undo(db, environment):
    import asyncio
    import time
    from mutagen.oggopus import OggOpus
    from localized_names import apply_to_opus
    from language_reverse import undo_info
    from pipeline import Pipeline

    tid, path, original_sha = add_audio_track(db, environment, RECORDING_1)
    token = "e" * 32
    db.set_setting("english-language-review:admin", {
        "token": token, "created": time.time(),
        "by_kind": {"display_changes": [tid]},
        "asset_hashes": {tid: original_sha}, "display_rows": [{"id": tid}],
        "summary": {"review_token": token},
    })
    db.queue_track(tid, "admin", {"mode": "language", "review_token": token})
    job = db.claim()
    worker = Pipeline(db)

    async def bridge(job, track, directory, mode, **kwargs):
        if mode == "language":
            return apply_to_opus(kwargs["path"], {"title": "New", "artist": "Artist"},
                                 {"title": "Original", "artist": "Artist"})
        return {"beets_id": 10, "matched_title": "Artist - New"}

    worker.bridge = bridge
    asyncio.run(worker.process_track(job))
    db.finish_job(job["id"])
    assert undo_info(db, "admin", tid, token, verify_bytes=True)
    tags = OggOpus(path)
    tags["title"] = ["Someone changed this later"]
    tags.save()
    with TestClient(create_app(db, start_workers=False)) as client:
        payload = {"user_id": "admin", "track_id": tid, "review_token": token, "confirmed": True}
        assert client.post("/api/catalogue/language-reverse", json=payload).status_code == 400
    assert sha256_file(path) != original_sha
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='track' AND json_extract(payload,'$.mode')='language_reverse'")["n"] == 0



def test_album_workspace_scopes_user_and_finds_cross_source_assigned_matches(db, environment):
    from album_workspace import AlbumWorkspace

    catalogue, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    moving, _, _ = add_audio_track(db, environment, RECORDING_2)
    new, _, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (new,))
    guest, _, _ = add_audio_track(db, environment, RECORDING_1, user="guest")
    base = workspace.tracks("admin")
    assert [row["id"] for row in base["items"]] == [new]
    assert base["total"] == 1
    entire = workspace.tracks("admin", include_assigned=True)
    assert {row["id"] for row in entire["items"]} == {first, moving, new}
    assert guest not in {row["id"] for row in entire["items"]}
    assert entire["total"] == 3
    subset = workspace.tracks("admin", include_assigned=True, limit=1)
    assert subset["total"] == 3 and subset["has_more"]
    searched = workspace.tracks("admin", include_assigned=True, query="Track")
    assert searched["total"] == 3
    assert workspace.tracks("admin", include_assigned=True, query="no-such-title")["total"] == 0
    next_page = workspace.tracks("admin", include_assigned=True, offset=1)
    assert not {row["id"] for row in subset["items"]} & {row["id"] for row in next_page["items"]}
    compared = workspace.compare("admin", [RELEASE_B], selected_track_ids=[new], include_assigned=True)
    release = compared["releases"][0]
    assert release["complete"] is True
    assert release["track_count"] == 2
    assert RECORDING_4 in release["matched_recording_ids"]
    assert {item["id"] for item in compared["additional_assigned_matches"]} == {moving}
    assert first not in {item["id"] for item in compared["additional_assigned_matches"]}
    assert guest not in {item["id"] for item in compared["additional_assigned_matches"]}
    with pytest.raises(ValueError, match="Unknown user"):
        workspace.tracks("missing")
    with pytest.raises(LookupError):
        workspace.compare("admin", [RELEASE_B], selected_track_ids=[guest])


def test_album_workspace_multi_album_plan_moves_only_explicit_tracks_and_can_undo(db, environment, monkeypatch):
    import album_management
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])

    catalogue, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    stays, _, _ = add_audio_track(db, environment, RECORDING_1)
    moves, moved_path, moved_hash = add_audio_track(db, environment, RECORDING_2)
    new, _, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (new,))
    proposal = workspace.preview("admin", [
        {"track_id": moves, "action": "release", "release_id": RELEASE_B},
        {"track_id": new, "action": "release", "release_id": RELEASE_B},
    ])
    assert proposal["can_apply"] == 2 and proposal["can_mark"] == 0
    assert {row["target_release_id"] for row in proposal["items"]} == {RELEASE_B}
    assert all(row["status"] == "COMPATIBLE" for row in proposal["items"])
    # No selected destination changes any audio until the explicit apply call.
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (moves,))["release_id"] == RELEASE_A
    assert moved_hash == sha256_file(moved_path)
    applied = workspace.apply("admin", proposal["preview_token"])
    assert applied["queued"] == 2
    changes = db.rows("SELECT * FROM album_changes WHERE group_id=?", (applied["group_id"],))
    assert len(changes) == 2
    assert {row["track_id"] for row in changes} == {moves, new}
    for change in changes:
        albums.prepare(change["id"], "admin", "apply")
        albums.finalize(change["id"], "admin", "apply", beets_id=None)
        db.execute("UPDATE jobs SET state='DONE' WHERE kind='album' AND target=?",
                   (change["id"],))
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (stays,))["release_id"] == RELEASE_A
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (moves,))["release_id"] == RELEASE_B
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (new,))["release_id"] == RELEASE_B
    with pytest.raises(ValueError, match="expired or was already applied"):
        workspace.apply("admin", proposal["preview_token"])
    undo = next(row for row in changes if row["track_id"] == moves)
    assert albums.undo("admin", undo["id"])["queued"]
    albums.prepare(undo["id"], "admin", "undo")
    albums.finalize(undo["id"], "admin", "undo", beets_id=None)
    assert sha256_file(moved_path) == moved_hash
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (moves,))["release_id"] == RELEASE_A


def test_album_workspace_supports_different_releases_in_one_plan(db, environment, monkeypatch):
    import album_management
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    _, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    second, _, _ = add_audio_track(db, environment, RECORDING_4)
    preview = workspace.preview("admin", [
        {"track_id": first, "action": "release", "release_id": RELEASE_A},
        {"track_id": second, "action": "release", "release_id": RELEASE_B},
    ])
    assert preview["can_apply"] == 1
    assert {r["status"] for r in preview["items"]} == {"UNCHANGED", "COMPATIBLE"}
    assert {r["target_release_id"] for r in preview["items"]} == {RELEASE_A, RELEASE_B}
    assert db.one("SELECT COUNT(*) AS n FROM album_previews")["n"] == 1, (
        "Multi-release preview must not leave orphaned single-release preview tokens")
    result = workspace.apply("admin", preview["preview_token"])
    assert result["queued"] == 1
    assert db.one("SELECT release_id FROM album_changes WHERE group_id=?",
                  (result["group_id"],))["release_id"] == RELEASE_B
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (first,))["release_id"] == RELEASE_A


def test_album_workspace_no_album_found_is_persistent_and_not_an_audio_tag(db, environment):
    from album_workspace import AlbumWorkspace
    catalogue, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, path, digest = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (tid,))
    proposal = workspace.preview("admin", [{"track_id": tid, "action": "not_found"}])
    assert proposal["can_mark"] == 1 and proposal["can_apply"] == 0
    assert proposal["items"][0]["status"] == "MARK_NOT_FOUND"
    result = workspace.apply("admin", proposal["preview_token"])
    assert result["queued"] == 0 and result["marked_not_found"] == 1
    assert sha256_file(path) == digest
    assert not db.rows("SELECT * FROM album_changes WHERE track_id=?", (tid,))
    assert workspace.tracks("admin")["items"][0]["not_found"] is True
    again = workspace.preview("admin", [{"track_id": tid, "action": "not_found"}])
    assert again["items"][0]["status"] == "UNCHANGED"
    assert workspace.clear_not_found("admin", tid)["cleared"] is True
    assert workspace.tracks("admin")["items"][0]["not_found"] is False
    assert workspace.clear_not_found("admin", tid)["cleared"] is False
    # The marker never writes a fake album value into the audio itself.
    assert sha256_file(path) == digest


def test_album_workspace_rejects_unverified_moves_and_stale_previews(db, environment, monkeypatch):
    import album_management
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    catalogue, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, _, _ = add_audio_track(db, environment, RECORDING_1)
    forbidden = workspace.preview("admin", [
        {"track_id": tid, "action": "release", "release_id": RELEASE_B},
    ])
    assert forbidden["can_apply"] == 0
    assert forbidden["items"][0]["status"] == "INCOMPATIBLE"
    with pytest.raises(ValueError, match="Nothing eligible"):
        workspace.apply("admin", forbidden["preview_token"])
    no_album = workspace.preview("admin", [{"track_id": tid, "action": "not_found"}])
    assert no_album["items"][0]["status"] == "INCOMPATIBLE"
    with pytest.raises(ValueError, match="Nothing eligible"):
        workspace.apply("admin", no_album["preview_token"])
    with pytest.raises(ValueError, match="one destination"):
        workspace.preview("admin", [
            {"track_id": tid, "action": "release", "release_id": RELEASE_A},
            {"track_id": tid, "action": "release", "release_id": RELEASE_B},
        ])
    with pytest.raises(ValueError, match="Invalid destination"):
        workspace.preview("admin", [{"track_id": tid, "action": "release", "release_id": "bad"}])
    valid, _, _ = add_audio_track(db, environment, RECORDING_2)
    preview = workspace.preview("admin", [
        {"track_id": valid, "action": "release", "release_id": RELEASE_B},
    ])
    db.execute("UPDATE tracks SET updated_at='2030-01-01T00:00:00Z' WHERE id=?", (valid,))
    with pytest.raises(ValueError, match="changed since preview"):
        workspace.apply("admin", preview["preview_token"])
    assert not db.rows("SELECT * FROM album_changes WHERE track_id=?", (valid,))


def test_album_workspace_http_apis_remain_user_scoped(db, environment):
    from album_workspace import AlbumWorkspace
    cat, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    local, _, _ = add_audio_track(db, environment, RECORDING_2)
    guest, _, _ = add_audio_track(db, environment, RECORDING_4, user="guest")
    with TestClient(create_app(db, start_workers=False)) as client:
        assert 'data-tab="album"' in client.get("/").text
        response = client.get("/api/albums/tracks", params={"user_id": "admin"})
        assert response.status_code == 200
        assert response.json()["total"] == 0  # currently assigned songs excluded by default
        include = client.get("/api/albums/tracks",
                             params={"user_id": "admin", "include_assigned": "true"})
        assert {r["id"] for r in include.json()["items"]} == {local}
        assert guest not in {r["id"] for r in include.json()["items"]}
        assert client.get("/api/albums/tracks",
                          params={"user_id": "unknown"}).status_code == 400
        assert client.post("/api/albums/compare", json={
            "user_id": "admin", "release_ids": [RELEASE_B],
            "selected_track_ids": [guest]}).status_code == 404
        assert client.post("/api/albums/plan-preview", json={
            "user_id": "admin", "decisions": [{"track_id": guest, "action": "not_found"}]
        }).status_code == 404
        assert client.post("/api/albums/not-found/clear", json={
            "user_id": "admin", "track_id": guest}).status_code == 404
        result=client.post("/api/albums/compare", json={
            "user_id": "admin", "release_ids": [RELEASE_B],
            "selected_track_ids": [local]})
        assert result.status_code == 200
        assert result.json()["releases"][0]["complete"] is True



def test_cluster_groups_connected_release_families_and_filters_whole_group(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    second, _, _ = add_audio_track(db, environment, RECORDING_2)
    third, _, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id IN (?,?)", (second,third))
    guest, _, _ = add_audio_track(db, environment, RECORDING_1, user="guest")
    unassigned = clusters.list("admin")
    assert unassigned["total_tracks"] == 3
    assert len(unassigned["clusters"]) == 1
    assert {x["id"] for x in unassigned["detail"]["tracks"]} == {first,second,third}
    assert {x["id"] for x in unassigned["detail"]["tracks"] if x["in_scope"]} == {second,third}
    entire = clusters.list("admin", include_assigned=True)
    assert entire["total_tracks"] == 3
    assert len(entire["clusters"]) == 1
    assert {x["id"] for x in entire["detail"]["tracks"]} == {first,second,third}
    assert guest not in {x["id"] for x in entire["detail"]["tracks"]}
    editions={x["id"]:x for x in entire["detail"]["releases"]}
    assert set(editions) == {RELEASE_A, RELEASE_B}
    assert editions[RELEASE_A]["matched_count"] == 2
    assert editions[RELEASE_B]["matched_count"] == 2
    assert editions[RELEASE_B]["track_count"] == 2
    assert editions[RELEASE_B]["complete"] is True
    assert {second,third} == set(editions[RELEASE_B]["matched_track_ids"])
    # A query matches a recording but keeps the *whole* cluster for review.
    filtered=clusters.list("admin", include_assigned=True, query="Recording 1")
    assert filtered["total_clusters"]==1
    assert len(filtered["detail"]["tracks"])==3
    assert clusters.list("admin", include_assigned=True, query="nowhere")["detail"] is None
    assert not clusters.list("guest", include_assigned=True)["detail"]["tracks"][0]["id"] == first
    with pytest.raises(ValueError, match="Unknown user"):
        clusters.list("missing")
    with pytest.raises(LookupError, match="selected library scope"):
        clusters.edition("admin", guest, RELEASE_A, include_assigned=True)


def test_album_snapshot_reuses_catalogue_reads_and_compact_response_preserves_family_detail(db, environment):
    from album_clusters import AlbumClusters

    catalogue, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    tid, _, _ = add_audio_track(db, environment, RECORDING_2)
    queue = []

    class DeferredTasks:
        def add_task(self, callback, *args, **kwargs):
            queue.append((callback, args, kwargs))

    orig = clusters._catalogue_data
    scans = []

    def counted(tracks):
        scans.append(len(tracks))
        return orig(tracks)

    clusters._catalogue_data = counted
    timing = {}
    result = clusters.list("admin", include_assigned=True,
                           use_cache=True, background_tasks=DeferredTasks(), timing=timing)
    assert result["clusters"] and result["detail"]
    assert len(scans) == 1
    assert queue, "Whole-library queueing belongs to response background tasks"
    assert timing["snapshot_cache_hit"] is False
    compact_timing = {}
    focused = clusters.list("admin", include_assigned=True,
                            focus_id=result["detail"]["id"], detail_only=True,
                            use_cache=True, background_tasks=DeferredTasks(), timing=compact_timing)
    assert focused["clusters"] == []
    assert focused["total_clusters"] == result["total_clusters"]
    assert {t["id"] for t in focused["detail"]["tracks"]} == {
        t["id"] for t in result["detail"]["tracks"]}
    assert compact_timing["snapshot_cache_hit"] is True
    assert len(scans) == 1, "Switching families should reuse cached SQLite MusicBrainz data"
    # An explicit album lock immediately invalidates the per-user snapshot.
    clusters.toggle_lock("admin", tid, True)
    after = clusters.list("admin", include_assigned=True,
                          use_cache=True, background_tasks=DeferredTasks())
    assert len(scans) == 2
    assert next(t for t in after["detail"]["tracks"] if t["id"] == tid)["locked"] is True


def test_album_clusters_api_reports_server_timing_and_focused_only_payload(db, environment):
    from main import create_app

    catalogue, albums = setup_catalogue(db)
    tid, _, _ = add_audio_track(db, environment, RECORDING_2)
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        first = client.get("/api/albums/clusters",
                           params={"user_id": "admin", "include_assigned": "true"})
        assert first.status_code == 200
        family = first.json()["detail"]["id"]
        assert family and tid in {row["id"] for row in first.json()["detail"]["tracks"]}
        assert "album-catalogue-db;dur=" in first.headers["server-timing"]
        lean = client.get("/api/albums/clusters", params={
            "user_id": "admin", "include_assigned": "true", "lean": "true",
        })
        assert lean.status_code == 200
        assert set(lean.json()["clusters"][0]) == {
            "id", "title", "track_count", "candidate_count", "locked"}
        assert len(lean.content) < len(first.content), "Lean list saves JSON transfer"
        detail = client.get("/api/albums/clusters", params={
            "user_id": "admin", "include_assigned": "true",
            "focus_id": family, "detail_only": "true",
        })
        assert detail.status_code == 200
        assert detail.json()["clusters"] == []
        assert detail.json()["detail"]["id"] == family
        assert 'album-snapshot;desc="hit"' in detail.headers["server-timing"]


def test_cluster_keeps_disjoint_music_separate_and_recognizes_incomplete_release(db, environment):
    from album_clusters import AlbumClusters
    other_rec = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    other_release = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    outside, _, _ = add_audio_track(db, environment, other_rec)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id IN (?,?)",(first,outside))
    # An explicit recording link without a complete release lookup must remain
    # an *unknown* match, never green just because an incomplete link exists.
    with cat.db() as con:
        cat.ingest(con, "release", {
            "id":other_release,"title":"Separate original","status":"Official",
            "release-group":{"id":"cccccccc-cccc-4ccc-8ccc-cccccccccccc","title":"Separate group"},
        })
        cat.upsert_link(con,"recording",other_rec,"release",other_release,"appears-on")
    result=clusters.list("admin")
    assert result["total_clusters"] == 2
    assert {c["track_count"] for c in result["clusters"]} == {1}
    detail=clusters.list("admin",focus_id=outside)["detail"]
    assert {t["id"] for t in detail["tracks"]} == {outside}
    candidate=detail["releases"][0]
    assert candidate["id"] == other_release
    assert candidate["complete"] is False
    assert candidate["matched_count"] == 0
    assert candidate["linked_track_count"] == 1
    assert candidate["matched_track_ids"] == []
    # Adding a release outside the cluster cannot make foreign recordings
    # appear in the local membership list.
    compared=clusters.edition("admin",outside,RELEASE_A)
    assert compared["matched_track_ids"] == []


def test_album_lock_prevents_bulk_manual_and_queued_edits(db, environment, monkeypatch):
    import album_management
    from album_clusters import AlbumClusters
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management,"LIBRARY",environment[1])
    cat,albums=setup_catalogue(db)
    cluster=AlbumClusters(albums);cluster.init()
    workspace=AlbumWorkspace(albums);workspace.init()
    tid, path, before=add_audio_track(db,environment,RECORDING_2)
    proposal=workspace.preview("admin",[{"track_id":tid,"action":"release","release_id":RELEASE_B}])
    assert proposal["can_apply"]==1
    assert cluster.toggle_lock("admin",tid,True)["locked"]
    assert cluster.locked("admin",tid) is True
    assert cluster.list("admin",include_assigned=True)["detail"]["tracks"][0]["locked"] is True
    new_preview=albums.preview("admin",RELEASE_B,track_ids=[tid])
    assert new_preview["items"][0]["status"] == "LOCKED"
    assert workspace.preview("admin",[{
        "track_id":tid,"action":"release","release_id":RELEASE_B
    }])["items"][0]["status"] == "LOCKED"
    with pytest.raises(ValueError,match="Album locked"):
        workspace.apply("admin",proposal["preview_token"])
    with pytest.raises(ValueError,match="Album locked"):
        db.queue_track(tid,"admin",{"mode":"retag"})
    assert before==sha256_file(path)
    assert db.one("SELECT release_id FROM tracks WHERE id=?", (tid,))["release_id"]==RELEASE_A
    with pytest.raises(LookupError):
        cluster.toggle_lock("guest",tid,False)
    assert cluster.toggle_lock("admin",tid,False)["locked"] is False
    assert workspace.apply("admin",proposal["preview_token"])["queued"]==1
    with pytest.raises(ValueError,match="active"):
        cluster.toggle_lock("admin",tid,True)


def test_cluster_api_is_user_scoped_and_exposes_explicit_lock(db, environment):
    cat, albums=setup_catalogue(db)
    tid, _, _=add_audio_track(db,environment,RECORDING_2)
    guest,_,_=add_audio_track(db,environment,RECORDING_4,user="guest")
    with TestClient(create_app(db,start_workers=False)) as client:
        response=client.get("/api/albums/clusters",
            params={"user_id":"admin","include_assigned":"true"})
        assert response.status_code==200
        result=response.json()
        assert result["total_clusters"]==1
        assert {x["id"] for x in result["detail"]["tracks"]}=={tid}
        assert guest not in str(result)
        rid=client.get("/api/albums/cluster-edition",params={
            "user_id":"admin","anchor_id":tid,"release_id":RELEASE_B,"include_assigned":"true"})
        assert rid.status_code==200
        assert rid.json()["matched_track_ids"]==[tid]
        assert client.get("/api/albums/cluster-edition",params={
            "user_id":"admin","anchor_id":guest,"release_id":RELEASE_B,
            "include_assigned":"true"}).status_code==404
        assert client.post("/api/albums/lock",json={
            "user_id":"admin","track_id":guest,"locked":True}).status_code==404
        locked=client.post("/api/albums/lock",json={
            "user_id":"admin","track_id":tid,"locked":True})
        assert locked.status_code==200 and locked.json()["locked"] is True
        assert client.get("/api/albums/lock",params={
            "user_id":"admin","track_id":tid}).json()["locked"] is True
        assert client.post("/api/albums/lock",json={
            "user_id":"admin","track_id":tid,"locked":False}).json()["locked"] is False


def test_album_edition_returns_full_official_tracklist_and_cluster_only_music(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    second, _, _ = add_audio_track(db, environment, RECORDING_2)
    third, _, _ = add_audio_track(db, environment, RECORDING_4)
    guest, _, _ = add_audio_track(db, environment, RECORDING_4, user="guest")
    result = clusters.list("admin", include_assigned=True)
    detail = result["detail"]
    assert {t["id"] for t in detail["tracks"]} == {first, second, third}
    edition = clusters.edition("admin", detail["id"], RELEASE_A, include_assigned=True)
    assert edition["complete"]
    assert edition["track_count"] == 2
    assert len(edition["tracks"]) == 2
    assert {t["recording_id"] for t in edition["tracks"]} == {RECORDING_1, RECORDING_2}
    assert sorted([tid for item in edition["tracks"] for tid in item["local_track_ids"]]) == sorted([first,second])
    assert edition["cluster_only"] == [third]
    assert guest not in str(edition)
    assert all("original_title" in item and "english_title" in item for item in edition["tracks"])


def test_album_cluster_prefetches_missing_tracklists_without_duplicate_queue_entries(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    with cat.db() as con:
        con.execute("DELETE FROM fetched_releases WHERE release_id=?", (RELEASE_A,))
        con.execute("DELETE FROM scopes WHERE kind='release' AND entity_id=?", (RELEASE_A,))
    initial = clusters.list("admin", include_assigned=True, focus_id=first)
    assert {t["id"] for t in initial["detail"]["tracks"]} == {first}
    assert initial["detail"]["cache_progress"]["complete"] < initial["detail"]["cache_progress"]["total"]
    with cat.db() as con:
        request = con.execute(
            "SELECT state,priority FROM scopes WHERE kind='release' AND entity_id=? AND section='lookup'",
            (RELEASE_A,),
        ).fetchone()
        assert request["state"] == "PENDING"
        assert request["priority"] == 10
        before = con.execute("SELECT COUNT(*) FROM scopes").fetchone()[0]
    clusters.list("admin", include_assigned=True, focus_id=first)
    with cat.db() as con:
        assert con.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == before
    assert not clusters.edition("admin",first,RELEASE_A,include_assigned=True)["complete"]


def test_catalogue_contradictions_are_verified_limited_and_per_user(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    right, _, _ = add_audio_track(db, environment, RECORDING_1)
    wrong, _, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=? WHERE id=?", (RELEASE_A, wrong))
    guest, _, _ = add_audio_track(db, environment, RECORDING_4, user="guest")
    db.execute("UPDATE tracks SET release_id=? WHERE id=?", (RELEASE_A, guest))
    result = clusters.contradictions("admin")
    assert result["total"] == 1
    assert result["items"][0]["track_id"] == wrong
    assert result["items"][0]["release_id"] == RELEASE_A
    assert right not in str(result)
    assert guest not in str(result)
    assert clusters.contradictions("admin", offset=1, limit=1)["items"] == []
    assert clusters.contradictions("guest")["total"] == 1
    with cat.db() as con:
        con.execute("DELETE FROM fetched_releases WHERE release_id=?", (RELEASE_A,))
    assert clusters.contradictions("admin")["total"] == 0, (
        "An incomplete MusicBrainz release must never yield a contradiction")
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get("/api/albums/contradictions",
                          params={"user_id": "nonexistent"}).status_code == 400
        response = client.get("/api/albums/contradictions",
                              params={"user_id": "admin"})
        assert response.status_code == 200
        assert response.json()["total"] == 0


def test_album_prefetch_repairs_old_completed_scope_without_tracklist_marker(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters=AlbumClusters(albums)
    clusters.init()
    tid,_,_=add_audio_track(db,environment,RECORDING_1)
    with cat.db() as con:
        con.execute("DELETE FROM fetched_releases WHERE release_id=?", (RELEASE_A,))
        con.execute("""INSERT INTO scopes(scope_key,kind,entity_id,section,state,cursor,
                     total,requested_at,last_complete_at,retry_after,priority)
                     VALUES (?,?,?,'lookup','COMPLETE',1,1,?,?,0,100)
                     ON CONFLICT(scope_key) DO UPDATE SET state='COMPLETE',
                        last_complete_at=excluded.last_complete_at""",
                    ("release:"+RELEASE_A+":lookup","release",RELEASE_A,
                     utcnow(),utcnow()))
    clusters.list("admin",include_assigned=True,focus_id=tid)
    with cat.db() as con:
        scope=con.execute(
            "SELECT state,priority FROM scopes WHERE scope_key=?",
            ("release:"+RELEASE_A+":lookup",),
        ).fetchone()
    assert scope["state"]=="PENDING"
    assert scope["priority"]==10


def test_album_edition_knows_downloaded_songs_excluded_by_unassigned_only_scope(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    clusters = AlbumClusters(albums)
    clusters.init()
    assigned, _, _ = add_audio_track(db, environment, RECORDING_2)
    unassigned, _, _ = add_audio_track(db, environment, RECORDING_4)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (unassigned,))
    cluster = clusters.list("admin")
    assert cluster["total_tracks"] == 2
    assert {t["id"] for t in cluster["detail"]["tracks"] if t["in_scope"]} == {unassigned}
    edition = clusters.edition("admin", unassigned, RELEASE_B)
    assert edition["complete"]
    by_recording = {r["recording_id"]: r for r in edition["tracks"]}
    # Assigned recordings are now visible in context but remain read-only.
    assert assigned in by_recording[RECORDING_2]["local_track_ids"]
    assert not next(t for t in cluster["detail"]["tracks"] if t["id"] == assigned)["in_scope"]
    assert by_recording[RECORDING_2]["local_copies"] == [
        {"id": assigned, "current_release_id": RELEASE_A,
         "locked": False, "display_title": "Track"}
    ]
    assert unassigned in by_recording[RECORDING_4]["local_track_ids"]


def test_release_group_workspaces_do_not_merge_compilations_or_featured_artists(db, environment):
    """A shared recording/artist can belong to multiple family views without union."""
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    bridge_release = "aaaa1111-1111-4111-8111-111111111111"
    bridge_group = "bbbb2222-2222-4222-8222-222222222222"
    independent_recording = "cccc3333-3333-4333-8333-333333333333"
    # A compilation includes a song from the first family and a new song.
    with cat.db() as con:
        cat.ingest(con, "release", {
            "id": bridge_release, "title": "Compilation",
            "release-group": {"id": bridge_group, "title": "Compilation"},
            "artist-credit": [{"artist": {"id": ARTIST, "name": "Composer"}}],
        })
        cat._store_release_tracks(con, {"id": bridge_release, "media": [{
            "position": 1, "tracks": [
                {"position": 1, "title": "Shared", "recording": {"id": RECORDING_2, "title": "Shared"}},
                {"position": 2, "title": "Other", "recording": {"id": independent_recording, "title": "Other"}},
            ]
        }]})
    shared, _, _ = add_audio_track(db, environment, RECORDING_2)
    other, _, _ = add_audio_track(db, environment, independent_recording)
    # This song is on the compilation but is not assigned to the other album.
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (other,))
    clusters = AlbumClusters(albums)
    clusters.init()
    result = clusters.list("admin", include_assigned=True)
    assert result["total_clusters"] == 2
    family = clusters.list("admin", include_assigned=True, focus_id=GROUP)["detail"]
    compilation = clusters.list("admin", include_assigned=True, focus_id=bridge_group)["detail"]
    assert {t["id"] for t in family["tracks"]} == {shared}
    assert {t["id"] for t in compilation["tracks"]} == {shared, other}


def test_im25_sibling_editions_offer_read_only_different_mbid_matches(db, environment):
    """Ten songs across two releases stay visible, without unsafe automatic moves."""
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db, (RELEASE_A, [RECORDING_1]),
                                  (RELEASE_B, [RECORDING_4, RECORDING_2]))
    # The B release has a Business as Usual identity not owned locally, plus
    # an unrelated local member anchoring the sibling family.
    with cat.db() as con:
        cat._store_release_tracks(con, {
            "id": RELEASE_A, "media": [{"position": 1, "tracks": [{
                "position": 3, "title": "Business as Usual", "length": 175000,
                "recording": {"id": RECORDING_1, "title": "Business As Usual", "length": 175000},
            }]}],
        })
        cat._store_release_tracks(con, {
            "id": RELEASE_B, "media": [{"position": 1, "tracks": [{
                "position": 3, "title": "Business as Usual", "length": 175000,
                "recording": {"id": RECORDING_4, "title": "Business As Usual", "length": 175000},
            }, {
                "position": 4, "title": "Boss La Rosh", "length": 195000,
                "recording": {"id": RECORDING_2, "title": "Boss La Rosh", "length": 195000},
            }]}],
        })
    first, _, _ = add_audio_track(db, environment, RECORDING_1)
    second, _, _ = add_audio_track(db, environment, RECORDING_2)
    db.execute("UPDATE tracks SET release_id=NULL WHERE id=?", (first,))
    db.execute("UPDATE tracks SET release_id=? WHERE id=?", (RELEASE_B, second))
    clusters = AlbumClusters(albums)
    clusters.init()
    original = clusters.list("admin")
    assert original["total_tracks"] == 2
    assert {t["id"] for t in original["detail"]["tracks"]} == {first, second}
    assert {t["id"] for t in original["detail"]["tracks"] if t["in_scope"]} == {first}
    album = clusters.edition("admin", GROUP, RELEASE_A)
    assert album["matched_track_ids"] == [first]
    # The copy with a different MBID must NEVER become an exact selection.
    assert album["tracks"][0]["local_track_ids"] == [first]
    other = clusters.edition("admin", GROUP, RELEASE_B)
    assert other["matched_track_ids"] == [second]
    assert not other["tracks"][0]["local_track_ids"]
    assert any(m["id"] == first for m in other["tracks"][0]["possible_local_matches"])
    assert first not in other["matched_track_ids"]
    assert other["possible_match_count"] == 1


def test_album_cache_progress_includes_release_group_discovery_and_tracklists(db, environment):
    from album_clusters import AlbumClusters
    cat, albums = setup_catalogue(db)
    tid, _, _ = add_audio_track(db, environment, RECORDING_2)
    clusters = AlbumClusters(albums)
    clusters.init()
    clusters.list("admin", include_assigned=True)
    with cat.db() as con:
        pending = con.execute(
            """SELECT state FROM scopes WHERE kind='release-group'
               AND entity_id=? AND section='release'""", (GROUP,)
        ).fetchone()
        assert pending and pending["state"] == "PENDING"
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.get("/api/catalogue/progress", params={"user_id":"admin"})
        assert response.status_code == 200
        status = response.json()["album_cache"]
        assert status["total"] >= 1
        assert status["pending"] >= 1
        assert status["cached_tracklists"] >= 2



def test_album_position_override_updates_recording_tags_and_undo_restores_identity(
        db, environment, monkeypatch):
    """An explicit different-MBID track slot updates tags, DB, and history reversibly."""
    import album_management
    from album_workspace import AlbumWorkspace
    from mutagen.oggopus import OggOpus

    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    cat, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, path, original_sha = add_audio_track(db, environment, RECORDING_1)
    proposal = workspace.preview("admin", [{
        "track_id": tid, "action": "release", "release_id": RELEASE_B,
        "target_recording_id": RECORDING_4,
    }])
    assert proposal["can_apply"] == 1
    item = proposal["items"][0]
    assert item["recording_id"] == RECORDING_1
    assert item["target_recording_id"] == RECORDING_4
    assert item["release_tags"]["replace_recording_id"] == RECORDING_4
    assert item["status"] == "COMPATIBLE"
    assert sha256_file(path) == original_sha
    assert db.one("SELECT mbid,release_id FROM tracks WHERE id=?", (tid,))["mbid"] == RECORDING_1

    applied = workspace.apply("admin", proposal["preview_token"])
    assert applied["queued"] == 1
    change = db.one("SELECT * FROM album_changes WHERE group_id=?", (applied["group_id"],))
    assert change["old_recording_id"] == RECORDING_1
    assert change["new_recording_id"] == RECORDING_4
    albums.prepare(change["id"], "admin", "apply")
    audio = OggOpus(path)
    assert audio["title"] == ["Track"]
    assert audio["artist"] == ["Artist"]
    assert audio["musicbrainz_trackid"] == [RECORDING_4]
    assert audio["musicbrainz_recordingid"] == [RECORDING_4]
    assert audio["musicbrainz_albumid"] == [RELEASE_B]
    # Only the durable finalization step updates the database.
    assert db.one("SELECT mbid FROM tracks WHERE id=?", (tid,))["mbid"] == RECORDING_1
    albums.finalize(change["id"], "admin", "apply", beets_id=None)
    updated = db.one("SELECT mbid,release_id,file_path FROM tracks WHERE id=?", (tid,))
    assert (updated["mbid"], updated["release_id"], updated["file_path"]) == (
        RECORDING_4, RELEASE_B, str(path))
    assert db.one("SELECT reason FROM identity_history WHERE track_id=?", (tid,))["reason"] == "album_edition_apply"

    db.execute("UPDATE jobs SET state='DONE' WHERE kind='album' AND target=?", (change["id"],))
    assert albums.undo("admin", change["id"])["queued"]
    albums.prepare(change["id"], "admin", "undo")
    albums.finalize(change["id"], "admin", "undo", beets_id=None)
    assert sha256_file(path) == original_sha
    old = db.one("SELECT mbid,release_id FROM tracks WHERE id=?", (tid,))
    assert (old["mbid"], old["release_id"]) == (RECORDING_1, RELEASE_A)
    assert len(db.rows("SELECT * FROM identity_history WHERE track_id=?", (tid,))) == 2


def test_album_override_blocks_duplicate_or_unlisted_recording_ids(db, environment, monkeypatch):
    from album_workspace import AlbumWorkspace
    import album_management
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    cat, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, _, _ = add_audio_track(db, environment, RECORDING_1)
    duplicate, _, _ = add_audio_track(db, environment, RECORDING_4)
    duplicate_preview = workspace.preview("admin", [{
        "track_id": tid, "action": "release", "release_id": RELEASE_B,
        "target_recording_id": RECORDING_4,
    }])
    assert duplicate_preview["can_apply"] == 0
    assert duplicate_preview["items"][0]["status"] == "DUPLICATE_RECORDING"
    with pytest.raises(ValueError, match="Nothing eligible"):
        workspace.apply("admin", duplicate_preview["preview_token"])
    invalid = workspace.preview("admin", [{
        "track_id": tid, "action": "release", "release_id": RELEASE_B,
        "target_recording_id": RECORDING_1,
    }])
    assert invalid["items"][0]["status"] == "INCOMPATIBLE"
    with pytest.raises(ValueError, match="only receive one local track"):
        workspace.preview("admin", [
            {"track_id": tid, "action": "release", "release_id": RELEASE_B,
             "target_recording_id": RECORDING_2},
            {"track_id": duplicate, "action": "release", "release_id": RELEASE_B,
             "target_recording_id": RECORDING_2},
        ])
    with pytest.raises(ValueError, match="Invalid destination MusicBrainz recording"):
        workspace.preview("admin", [{
            "track_id": tid, "action": "release", "release_id": RELEASE_B,
            "target_recording_id": "not-a-uuid",
        }])


def test_album_override_stale_identity_is_rejected_before_queue(db, environment, monkeypatch):
    import album_management
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    _, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, _, _ = add_audio_track(db, environment, RECORDING_1)
    proposal = workspace.preview("admin", [{
        "track_id": tid, "action": "release", "release_id": RELEASE_B,
        "target_recording_id": RECORDING_4,
    }])
    db.execute("UPDATE tracks SET mbid=? WHERE id=?", (RECORDING_2, tid))
    with pytest.raises(ValueError, match="changed since preview"):
        workspace.apply("admin", proposal["preview_token"])
    assert not db.rows("SELECT * FROM album_changes WHERE track_id=?", (tid,))


def test_album_override_with_same_release_still_corrects_recording_id(db, environment, monkeypatch):
    import album_management
    from album_workspace import AlbumWorkspace
    monkeypatch.setattr(album_management, "LIBRARY", environment[1])
    _, albums = setup_catalogue(db)
    workspace = AlbumWorkspace(albums)
    workspace.init()
    tid, _, _ = add_audio_track(db, environment, RECORDING_1)
    db.execute("UPDATE tracks SET release_id=? WHERE id=?", (RELEASE_B, tid))
    proposal = workspace.preview("admin", [{
        "track_id": tid, "action": "release", "release_id": RELEASE_B,
        "target_recording_id": RECORDING_4,
    }])
    assert proposal["can_apply"] == 1
    assert proposal["items"][0]["status"] == "COMPATIBLE"


def test_catalogue_worker_pauses_entire_backlog_during_provider_cooldown(db, caplog):
    from catalogue_routes import cache_worker
    from http_policy import ApiDeferred

    catalogue = Catalogue(db)
    catalogue.init()
    catalogue.enqueue("recording", RECORDING_1, sections=["lookup"])
    catalogue.enqueue("recording", RECORDING_2, sections=["lookup"])
    calls = []

    def blocked(*args):
        calls.append(1)
        raise ApiDeferred(
            "musicbrainz.org: waiting for shared provider cooldown",
            retry_at=time.time() + 30,
        )

    catalogue.request = blocked

    async def run():
        stopping = asyncio.Event()
        worker = asyncio.create_task(cache_worker(catalogue, stopping))
        try:
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert calls
            await asyncio.sleep(0.15)
        finally:
            stopping.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())
    assert calls == [1], "Must not try every pending Catalogue scope during one cooldown"
    assert caplog.text.count("MusicBrainz catalogue paused") == 1
    with catalogue.db() as con:
        remaining = con.execute("SELECT COUNT(*) FROM scopes WHERE state='PENDING'").fetchone()[0]
    assert remaining == 2


def test_language_review_and_beets_apply_use_same_official_english_title(db, environment):
    """Genshin-style cross-release title is reviewed, then actually written."""
    from types import SimpleNamespace
    from mutagen.oggopus import OggOpus
    from beets_bridge import Bridge
    from language_jobs import preview, name_changes
    from localized_names import POLICY_TAG

    catalogue, _ = setup_catalogue(db)
    track_id, path, _ = add_audio_track(db, environment, RECORDING_1)
    with catalogue.db() as con:
        catalogue.ingest(con, "recording", {
            "id": RECORDING_1, "title": "未始的供献颂", "aliases": [],
            "artist-credit": [{"artist": {"id": ARTIST, "name": "HOYO‐MiX"},
                               "name": "HOYO‐MiX"}],
        })
        catalogue.ingest(con, "release", {
            "id": RELEASE_A, "title": "Genshin Impact - The Stellar Moments",
            "status": "Official", "text-representation": {"language": "eng"},
            "artist-credit": [{"artist": {"id": ARTIST, "name": "HOYO‐MiX"}}],
        })
        catalogue._store_release_tracks(con, {
            "id": RELEASE_A, "media": [{"position": 1, "tracks": [{
                "id": str(uuid.uuid4()), "position": 11,
                "title": "Offertorium of Fortuitum",
                "recording": {"id": RECORDING_1, "title": "未始的供献颂"},
            }]}],
        })
    before = sha256_file(path)
    report = preview(db, "admin")
    assert report["eligible"] == 1
    row = report["sample"][0]
    assert row["title_source"] == "official_release"
    assert row["title_release_id"] == RELEASE_A
    assert "Offertorium of Fortuitum" in row["proposed_name"]
    assert sha256_file(path) == before, "Language review must never edit files"
    page = name_changes(db, "admin", report["review_token"])
    assert page["items"][0]["title_release_id"] == RELEASE_A

    # Exercise the actual Beets-side language operation, not just its preview.
    bridge = object.__new__(Bridge)
    bridge.store = db
    bridge.plugins = {"musicbrainz": SimpleNamespace(track_for_id=lambda identity:
        SimpleNamespace(track_id=identity, title="未始的供献颂", artist="HOYO‐MiX"))}
    bridge.raise_if_deferred = lambda: None
    bridge.release_metadata = lambda recording, release: (
        SimpleNamespace(album="Genshin Impact - The Stellar Moments",
                        albumartist="HOYO‐MiX"), {},
    )
    bridge.canonical_names = lambda recording, release: {
        "title": "未始的供献颂", "artist": "HOYO‐MiX",
        "album": "Genshin Impact - The Stellar Moments", "albumartist": "HOYO‐MiX",
    }
    result = bridge.localize_existing(
        {"id": track_id, "mbid": RECORDING_1, "release_id": RELEASE_A,
         "metadata_edited": False},
        str(path),
    )
    tags = OggOpus(path)
    assert tags["title"] == ["Offertorium of Fortuitum"]
    assert tags["originaltitle"] == ["未始的供献颂"]
    assert tags["musicbrainz_trackid"] == [RECORDING_1]
    assert tags["musicbrainz_albumid"] == [RELEASE_A]
    assert result["title"] == "Offertorium of Fortuitum"
    assert tags[POLICY_TAG] and path.is_file(), "Identity and audio path stay unchanged"
