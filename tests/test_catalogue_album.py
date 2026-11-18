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
    albums._retag(path, tags)
    raw = OggOpus(path)
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
