"""Cache-first Approval metadata and durable MusicBrainz prefetch regressions."""
import asyncio
import json
import time
import uuid

from fastapi.testclient import TestClient

from approval_prefetch import NAME_TTL, SEARCH_TTL, search_key
from catalogue import Catalogue
from main import create_app
from musicbrainz_search import PAGE_SIZE, normalize

RECORDING = "11111111-1111-4111-8111-111111111111"
ARTIST = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def test_approval_reuses_catalogue_and_persists_verified_names_across_restarts(db):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        catalogue = app.state.catalogue
        with catalogue.db() as con:
            catalogue.ingest(con, "recording", {
                "id": RECORDING, "title": "踊子プリムロゼのテーマ",
                "aliases": [{"name": "Primrose, the Dancer", "locale": "en"}],
                "artist-credit": [{"name": "西木康智",
                                   "artist": {"id": ARTIST, "name": "西木康智"}}],
            })
            catalogue.ingest(con, "artist", {
                "id": ARTIST, "name": "西木康智",
                "aliases": [{"name": "Yasunori Nishiki", "locale": "en"}],
            })
        params = {"user_id": "admin", "recording_ids": RECORDING, "artist_ids": ARTIST}
        first = client.get("/api/musicbrainz/approval-names", params=params)
        assert first.status_code == 200
        data = first.json()
        assert data["recordings"][RECORDING]["title"] == "踊子プリムロゼのテーマ"
        assert data["recordings"][RECORDING]["preferred_title"] == "Primrose, the Dancer"
        assert data["recordings"][RECORDING]["preferred_artist"] == "Yasunori Nishiki"
        assert data["pending_recordings"] == [], "Full Catalogue aliases need no new provider fetch"
        assert data["pending_artists"] == []
        prefetch = app.state.approval_prefetch
        prefetch.workers_enabled = True
        assert client.get("/api/musicbrainz/approval-names", params=params).json()["remaining"] == 0
        with catalogue.db() as con:
            assert not list(con.execute(
                "SELECT * FROM approval_prefetch_jobs WHERE kind IN ('recording','artist')")), (
                "Complete Catalogue names should not be refetched")
            assert con.execute(
                "SELECT COUNT(*) FROM approval_prefetch_jobs WHERE kind='releases'"
            ).fetchone()[0] == 1, "Album editions are independently prefetched"
        prefetch.workers_enabled = False

        prefetch._store("recording", RECORDING, {
            "id": RECORDING, "title": "踊子プリムロゼのテーマ",
            "preferred_title": "Primrose, the Dancer",
            "artist": "西木康智",
            "artist_credits": [{"id": ARTIST, "name": "西木康智", "joinphrase": ""}],
        }, NAME_TTL)
        prefetch._store("artist", ARTIST, {
            "id": ARTIST, "name": "西木康智",
            "aliases": [{"name": "Yasunori Nishiki", "locale": "en"}],
        }, NAME_TTL)
        resolved = client.get("/api/musicbrainz/approval-names", params=params).json()
        assert resolved["remaining"] == 0
        assert resolved["recordings"][RECORDING]["preferred_artist"] == "Yasunori Nishiki"
        assert resolved["artists"][ARTIST]["preferred_name"] == "Yasunori Nishiki"
        assert client.get("/api/musicbrainz/approval-names",
                          params={**params, "user_id": "unknown"}).status_code == 400

    # A different app instance using the same persistent Catalogue database
    # must load names instantly without contacting MusicBrainz again.
    second = create_app(db, start_workers=False)
    with TestClient(second) as client:
        cached = client.get("/api/musicbrainz/approval-names", params=params).json()
        assert cached["remaining"] == 0
        assert cached["recordings"][RECORDING]["title"] == "踊子プリムロゼのテーマ"


def test_library_seed_queues_every_beets_candidate_and_one_exact_search(db, track):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        origin = db.one("SELECT * FROM track_origins WHERE track_id=?", (track["id"],))
        db.execute("""UPDATE tracks SET operation_state='NEEDS_APPROVAL',
                   current_origin_id=?,pending_operation='{}',choices=?
                   WHERE id=?""", (origin["id"], json.dumps([
            {"kind": "candidate", "mbid": RECORDING},
            {"kind": "candidate", "mbid": OTHER},
            {"kind": "candidate", "mbid": RECORDING},
            {"kind": "source", "mbid": None},
        ]), track["id"]))
        prefetch = app.state.approval_prefetch
        result = prefetch.seed_pending_approvals()
        assert result["tracks"] == 1
        assert result["new_jobs"] == 5
        jobs = app.state.catalogue
        with jobs.db() as con:
            pending = [dict(row) for row in con.execute(
                "SELECT kind,cache_key,state FROM approval_prefetch_jobs ORDER BY requested_at"
            )]
        assert [job["kind"] for job in pending] == ["recording", "releases", "recording", "releases", "search"]
        assert {item["cache_key"] for item in pending if item["kind"] == "recording"} == {RECORDING, OTHER}
        assert {item["cache_key"] for item in pending if item["kind"] == "releases"} == {RECORDING + ":1", OTHER + ":1"}
        assert prefetch.seed_pending_approvals()["new_jobs"] == 0
        claimed = prefetch.claim()
        assert claimed["state"] == "PENDING"
        prefetch.init()
        assert prefetch.claim()["cache_key"] == claimed["cache_key"], (
            "Interrupted prefetch work must resume after restart")


def test_full_musicbrainz_search_page_is_cached_and_every_candidate_enqueued(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch = app.state.approval_prefetch
        searcher = app.state.musicbrainz_search
        identities = [str(uuid.UUID(int=i + 100)) for i in range(PAGE_SIZE)]
        requested = []

        def fake_search(entity, query, page):
            requested.append((entity, query, page))
            return normalize({
                "count": PAGE_SIZE, "recordings": [
                    {"id": identity, "title": f"Japanese Track {i}",
                     "artist-credit": [{"name": "日本の作曲家",
                                        "artist": {"id": ARTIST, "name": "日本の作曲家"}}]}
                    for i, identity in enumerate(identities)
                ],
            }, entity, page)

        monkeypatch.setattr(searcher, "request", fake_search)
        prefetch.enqueue_search("recording", "Japanese Track", "", priority=0)
        job = prefetch.claim()
        assert job["kind"] == "search"
        asyncio.run(prefetch.process(job))
        with app.state.catalogue.db() as con:
            rows = list(con.execute(
                "SELECT cache_key FROM approval_prefetch_jobs WHERE kind='recording'"))
        assert {row["cache_key"] for row in rows} == set(identities), (
            "Prefetch all twenty results, not just the first three/twelve")
        assert len(requested) == 1
        prefetch.workers_enabled = True
        params = {"user_id": "admin", "entity": "recording",
                  "title": "Japanese Track", "page": 1}
        assert client.get("/api/musicbrainz/search", params=params).json()["total"] == PAGE_SIZE
        assert len(client.get("/api/musicbrainz/search", params=params).json()["items"]) == PAGE_SIZE
        assert len(requested) == 1, "Repeated Approval searches must reuse persistent cache"


def test_waiting_searches_are_queued_not_sent_from_browser(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch = app.state.approval_prefetch
        prefetch.workers_enabled = True
        searcher = app.state.musicbrainz_search
        monkeypatch.setattr(searcher, "request",
                            lambda *args: (_ for _ in ()).throw(AssertionError("Foreground provider call")))
        params = {"user_id": "admin", "title": "Queued Search", "page": 1}
        first = client.get("/api/musicbrainz/search", params=params)
        assert first.status_code == 202 and first.json()["pending"]
        second = client.get("/api/musicbrainz/search", params=params)
        assert second.status_code == 202
        with app.state.catalogue.db() as con:
            jobs = list(con.execute("SELECT kind,state FROM approval_prefetch_jobs"))
        assert jobs == [jobs[0]] and jobs[0]["kind"] == "search", "Do not duplicate queued searches"
        key = search_key("recording", "Queued Search", "", None, 1)
        prefetch._store("search", key, {
            "entity": "recording", "items": [], "total": 0,
            "page": 1, "limit": PAGE_SIZE, "query_used": {"title": "Queued Search"},
        }, SEARCH_TTL)
        cached = client.get("/api/musicbrainz/search", params=params)
        assert cached.status_code == 200 and cached.json()["total"] == 0
        assert cached.json()["cache_status"] == "fresh"
        with app.state.catalogue.db() as con:
            con.execute("UPDATE approval_prefetch_cache SET expires_at=0 "
                        "WHERE kind='search' AND cache_key=?", (key,))
        stale = client.get("/api/musicbrainz/search", params=params)
        assert stale.status_code == 200 and stale.json()["cache_status"] == "stale"
        with app.state.catalogue.db() as con:
            assert con.execute("SELECT 1 FROM approval_prefetch_jobs WHERE kind='search' "
                               "AND cache_key=?", (key,)).fetchone(), "Stale data must refresh in background"


def test_background_artist_alias_lookup_deduplicates_shared_credits(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        searcher = app.state.musicbrainz_search
        rid2 = OTHER
        for identity in (RECORDING, rid2):
            prefetch.enqueue_names([identity], priority=0)
        calls = []

        def fake_recording(identity):
            calls.append(("recording", identity))
            return {"id": identity, "title": "運命に囚われし者たち",
                    "preferred_title": None, "artist": "光田康典",
                    "artist_credits": [{"id": ARTIST, "name": "光田康典",
                                        "joinphrase": ""}]}

        def fake_artist(identity):
            calls.append(("artist", identity))
            return {"id": identity, "name": "光田康典",
                    "aliases": [{"name": "Yasunori Mitsuda", "locale": "en"}]}

        monkeypatch.setattr(searcher, "request_recording_names", fake_recording)
        monkeypatch.setattr(searcher, "request_artist", fake_artist)
        asyncio.run(prefetch.process(prefetch.claim()))
        asyncio.run(prefetch.process(prefetch.claim()))
        with app.state.catalogue.db() as con:
            artist_jobs = list(con.execute(
                "SELECT * FROM approval_prefetch_jobs WHERE kind='artist'"))
        assert len(artist_jobs) == 1
        asyncio.run(prefetch.process(prefetch.claim()))
        labels = prefetch.names_batch([RECORDING, rid2])
        assert labels["remaining"] == 0
        for identity in (RECORDING, rid2):
            assert labels["recordings"][identity]["artist"] == "光田康典"
            assert labels["recordings"][identity]["preferred_artist"] == "Yasunori Mitsuda"
        assert calls.count(("artist", ARTIST)) == 1


def test_cache_lookups_never_enqueue_when_background_workers_are_off(db):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        response = client.get("/api/musicbrainz/approval-names",
                              params={"user_id": "admin", "recording_ids": RECORDING})
        assert response.status_code == 200
        assert response.json()["pending_recordings"] == [RECORDING]
        with app.state.catalogue.db() as con:
            assert not con.execute("SELECT 1 FROM approval_prefetch_jobs").fetchone()
        assert not client.get("/api/musicbrainz/approval-prefetch-status",
                              params={"user_id": "unknown"}).is_success


def _create_waiting_review(db, *, title="Japanese Pending Track", artist="Composer"):
    origin_id = str(uuid.uuid4())
    track_id = str(uuid.uuid4())
    now = "2026-11-18T20:00:00Z"
    db.execute(
        """INSERT INTO tracks
           (id,user_id,title,discovery_basis,health,operation_state,choices,
            current_origin_id,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (track_id, "admin", title, "playlist", "UNAVAILABLE", "NEEDS_APPROVAL",
         json.dumps([{"kind": "candidate", "mbid": OTHER}]),
         origin_id, now, now),
    )
    db.execute(
        """INSERT INTO track_origins
           (id,track_id,user_id,provider,media_key,url,title,channel,first_seen_at,last_seen_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (origin_id, track_id, "admin", "youtube", "wait-review", "https://youtu.be/wait-review",
         title, artist + " - Topic", now, now),
    )
    return track_id


def test_prefetch_status_reports_queue_over_discovered_total_and_cache_storage(db):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch = app.state.approval_prefetch
        prefetch._store("artist", ARTIST, {
            "id": ARTIST, "name": "光田康典",
            "aliases": [{"name": "Yasunori Mitsuda", "locale": "en"}],
        }, NAME_TTL)
        assert prefetch.enqueue_names([RECORDING]) == 1
        value = client.get("/api/musicbrainz/approval-prefetch-status",
                           params={"user_id": "admin"}).json()
        assert value["queued"] == 1
        assert value["completed"] == 1
        assert value["total"] == 2
        assert value["cached"] == 1
        assert value["cache_entries"] == 1
        assert value["cache_bytes"] > 50
        assert value["catalogue_db_bytes"] >= value["cache_bytes"]
        assert value["deferred"] == 0 and value["cooldown_seconds"] == 0
        assert not client.get("/api/musicbrainz/approval-prefetch-status",
                              params={"user_id": "nonexistent"}).is_success


def test_cleanup_keeps_local_approved_and_pending_names_searches_and_artist_links(db, track):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        pending_id = _create_waiting_review(db)
        db.execute(
            "UPDATE tracks SET mbid=?,health='MISSING',operation_state='IDLE' WHERE id=?",
            (RECORDING, track["id"]),
        )
        # A missing local file must not invalidate its saved MusicBrainz identity.
        with catalogue.db() as con:
            catalogue.ingest(con, "recording", {
                "id": RECORDING, "title": "運命に囚われし者たち",
                "artist-credit": [{"name": "光田康典",
                    "artist": {"id": ARTIST, "name": "光田康典"}}],
            })
            catalogue.ingest(con, "recording", {
                "id": OTHER, "title": "Pending suggestion",
                "artist-credit": [{"name": "日本の作曲家",
                    "artist": {"id": ARTIST, "name": "日本の作曲家"}}],
            })

        unknown = str(uuid.uuid4())
        manually_found = str(uuid.uuid4())
        stale_search = search_key("recording", "An old approved search", "", None, 1)
        manual_search = search_key("recording", "Manually chosen title", "", None, 1)
        default_search = search_key("recording", "Japanese Pending Track", "Composer", None, 1)
        for rid in (RECORDING, OTHER, manually_found, unknown):
            prefetch._store("recording", rid, {
                "id": rid, "title": "歌", "artist": "光田康典",
                "artist_credits": [{"id": ARTIST, "name": "光田康典", "joinphrase": ""}],
            }, NAME_TTL)
        prefetch._store("artist", ARTIST, {"id": ARTIST, "name": "光田康典"}, NAME_TTL)
        prefetch._store("artist", unknown, {"id": unknown, "name": "Obsolete"}, NAME_TTL)
        prefetch._store("search", manually_found, {"items": []}, SEARCH_TTL)  # Unrelated cache
        prefetch._store("search", manual_search, {
            "entity": "recording", "items": [{"id": manually_found,
                                              "artists": [{"id": ARTIST}]}],
        }, SEARCH_TTL)
        prefetch._store("search", stale_search, {
            "entity": "recording", "items": [{"id": unknown}],
        }, SEARCH_TTL)
        prefetch._store("search", default_search, {
            "entity": "recording", "items": [{"id": OTHER}],
        }, SEARCH_TTL)
        prefetch.remember_search_owner(pending_id, "recording", "Manually chosen title", "")
        prefetch.remember_search_owner(track["id"], "recording", "An old approved search", "")
        prefetch.enqueue_names([str(uuid.uuid4())], priority=20)
        prefetch.enqueue_search("recording", "Obsolete queued query", "", priority=20)
        with catalogue.db() as con:
            con.execute("UPDATE approval_prefetch_cache SET expires_at=0 "
                        "WHERE kind='search' AND cache_key=?", (default_search,))
        prefetch.enqueue_search("recording", "Japanese Pending Track", "Composer", priority=20)

        preview = client.get("/api/musicbrainz/approval-prefetch-clean-preview",
                             params={"user_id": "admin"})
        assert preview.status_code == 200
        plan = preview.json()
        assert plan["protected_recordings"] >= 3
        assert plan["protected_artists"] >= 1
        assert plan["protected_searches"] >= 2
        assert plan["remove_entries"] >= 3
        assert plan["remove_queued"] >= 2
        assert plan["reclaim_bytes"] > 0
        assert client.post("/api/musicbrainz/approval-prefetch-clean",
                           json={"user_id": "admin", "confirmed": False}).status_code == 422
        assert client.post("/api/musicbrainz/approval-prefetch-clean",
                           json={"user_id": "unknown", "confirmed": True}).status_code == 400
        result = client.post("/api/musicbrainz/approval-prefetch-clean",
                             json={"user_id": "admin", "confirmed": True})
        assert result.status_code == 200
        assert result.json()["remove_entries"] == plan["remove_entries"]
        assert result.json()["remove_queued"] == plan["remove_queued"]
        with catalogue.db() as con:
            kept = {(r["kind"], r["cache_key"]) for r in con.execute(
                "SELECT kind,cache_key FROM approval_prefetch_cache")}
            assert ("recording", RECORDING) in kept
            assert ("recording", OTHER) in kept
            assert ("recording", manually_found) in kept
            assert ("artist", ARTIST) in kept
            assert ("search", manual_search) in kept
            assert ("search", default_search) in kept
            assert ("recording", unknown) not in kept
            assert ("artist", unknown) not in kept
            assert ("search", stale_search) not in kept
            refs = [dict(x) for x in con.execute("SELECT * FROM approval_prefetch_search_refs")]
            assert refs == [{"cache_key": manual_search, "track_id": pending_id}]
            assert con.execute("SELECT 1 FROM entities WHERE kind='recording' AND id=?",
                               (RECORDING,)).fetchone(), "Shared Catalogue is not cleared"
            assert con.execute("SELECT 1 FROM approval_prefetch_jobs WHERE kind='search' AND cache_key=?",
                               (default_search,)).fetchone()
        assert db.one("SELECT mbid FROM tracks WHERE id=?", (track["id"],))["mbid"] == RECORDING
        assert db.one("SELECT operation_state FROM tracks WHERE id=?",
                      (pending_id,))["operation_state"] == "NEEDS_APPROVAL"
        again = prefetch.cleanup_unused()
        assert again["remove_entries"] == 0 and again["remove_queued"] == 0


def test_clearing_inflight_prefetch_prevents_reintroducing_obsolete_names(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        assert prefetch.enqueue_names(recordings=[OTHER]) == 1
        job = prefetch.claim()
        assert job["kind"] == "recording" and job["state"] == "PENDING"
        prefetch.cleanup_unused()  # In-flight request was not for local/pending music.
        monkeypatch.setattr(app.state.musicbrainz_search, "request_recording_names",
                            lambda identity: {"id": identity, "title": "Obsolete",
                                              "artist_credits": [{"id": ARTIST, "name": "X"}]})
        asyncio.run(prefetch.process(job))
        with app.state.catalogue.db() as con:
            assert not con.execute("SELECT 1 FROM approval_prefetch_cache WHERE kind='recording'").fetchone()
            assert not con.execute("SELECT 1 FROM approval_prefetch_jobs WHERE kind='artist'").fetchone()


def test_provider_cooldown_pauses_the_entire_prefetch_worker(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        prefetch.enqueue_names(recordings=[RECORDING, OTHER])
        first = prefetch.claim()
        retry = time.time() + 30
        from http_policy import ApiDeferred
        prefetch.defer(first, ApiDeferred("musicbrainz.org: HTTP 503", retry_at=retry))
        assert prefetch.pause_until >= retry
        assert prefetch.status()["deferred"] == 1
        assert prefetch.status()["queued"] == 2
        assert prefetch.status()["cooldown_seconds"] >= 29
        seen = []
        original_claim = prefetch.claim
        def trace_claim():
            seen.append("claimed")
            return original_claim()
        prefetch.claim = trace_claim
        prefetch.last_seed_at = time.time()
        async def run():
            stopping = asyncio.Event()
            task = asyncio.create_task(prefetch.work(stopping))
            await asyncio.sleep(0.05)
            stopping.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run(run())
        assert seen == [], "No second provider request must start during a cooldown"
        assert prefetch.status()["queued"] == 2


JAPANESE_RECORDING = "b6173a0d-5ec7-44aa-bfe6-475d5fd5d9c2"
JAPANESE_ALIAS_RECORDING = "cc9a68ea-f7ba-41c6-9162-ad4aba7ea270"
PSEUDO_RELEASE = "bedbc15a-1e5c-4aa6-ae90-24c0b78c1f3a"
OFFICIAL_RELEASE = "99999999-9999-4999-8999-999999999999"
JAPANESE_TITLE = "運命に囚われし者たち"


def test_exact_pseudo_release_name_is_persisted_below_native_recording(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch = app.state.approval_prefetch
        prefetch.workers_enabled = True
        prefetch._store("recording", JAPANESE_RECORDING, {
            "id": JAPANESE_RECORDING, "title": JAPANESE_TITLE,
            "preferred_title": None, "artist": "Yasunori Mitsuda & Millennial Fair",
            "artist_credits": [],
        }, NAME_TTL)
        first = prefetch.names_batch([JAPANESE_RECORDING])
        assert first["recordings"][JAPANESE_RECORDING]["preferred_title"] is None
        assert first["pending_recordings"] == [JAPANESE_RECORDING]
        seen = []
        def fake_releases(rid, page):
            seen.append(("release-list", rid, page))
            return {"recording_id": rid, "page": page, "total": 2,
                    "releases": [
                        {"id": OFFICIAL_RELEASE, "title": "Official CD", "status": "Official"},
                        {"id": PSEUDO_RELEASE, "title": "Live English tracklist",
                         "status": "Pseudo-Release", "date": "2020-06-24"},
                    ]}
        def fake_pseudo(rid):
            seen.append(("pseudo-release", rid))
            return {"id": rid, "status": "pseudo-release",
                    "titles": [{"recording_id": JAPANESE_RECORDING,
                                "title": "Bound by Fate", "language": "eng"}]}
        monkeypatch.setattr(app.state.musicbrainz_search, "request_releases", fake_releases)
        monkeypatch.setattr(app.state.musicbrainz_search, "request_pseudo_release_titles", fake_pseudo)
        job = prefetch.claim()
        assert job["kind"] == "releases"
        asyncio.run(prefetch.process(job))
        waiting = prefetch.names_batch([JAPANESE_RECORDING])
        assert waiting["pending_recordings"] == [JAPANESE_RECORDING], (
            "The UI should keep checking until pseudo-release titles arrive")
        pseudo_job = prefetch.claim()
        assert pseudo_job["kind"] == "pseudo_release" and pseudo_job["cache_key"] == PSEUDO_RELEASE
        asyncio.run(prefetch.process(pseudo_job))
        data = client.get("/api/musicbrainz/approval-names",
                          params={"user_id": "admin", "recording_ids": JAPANESE_RECORDING}).json()
        resolved = data["recordings"][JAPANESE_RECORDING]
        assert resolved["title"] == JAPANESE_TITLE
        assert resolved["preferred_title"] == "Bound by Fate"
        assert resolved["preferred_title_source"] == "pseudo_release"
        assert resolved["preferred_title_release_id"] == PSEUDO_RELEASE
        assert resolved["preferred_title_language"] == "eng"
        assert data["pending_recordings"] == []
        assert seen == [("release-list", JAPANESE_RECORDING, 1),
                        ("pseudo-release", PSEUDO_RELEASE)]
        assert client.get("/api/musicbrainz/releases", params={
            "user_id": "admin", "recording_id": JAPANESE_RECORDING}).json()["releases"][1]["id"] == PSEUDO_RELEASE

    # Neither the English title nor the album lookup should need new network
    # requests after restart.
    app2 = create_app(db, start_workers=False)
    with TestClient(app2) as client:
        data = client.get("/api/musicbrainz/approval-names", params={
            "user_id": "admin", "recording_ids": JAPANESE_RECORDING}).json()
        assert data["recordings"][JAPANESE_RECORDING]["preferred_title"] == "Bound by Fate"
        assert client.get("/api/musicbrainz/releases", params={
            "user_id": "admin", "recording_id": JAPANESE_RECORDING}).json()["total"] == 2


def test_recording_level_alias_has_priority_over_other_pseudo_release_titles(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        prefetch._store("recording", JAPANESE_ALIAS_RECORDING, {
            "id": JAPANESE_ALIAS_RECORDING, "title": JAPANESE_TITLE,
            "preferred_title": "Bound by Fate", "artist": "光田康典",
            "artist_credits": [],
        }, NAME_TTL)
        prefetch._store("pseudo_title", JAPANESE_ALIAS_RECORDING, {
            "title": "Different Pseudo-release Edition",
            "release_id": PSEUDO_RELEASE, "language": "eng",
            "source": "pseudo_release",
        }, NAME_TTL)
        label = prefetch.names_batch([JAPANESE_ALIAS_RECORDING])["recordings"][JAPANESE_ALIAS_RECORDING]
        assert label["title"] == JAPANESE_TITLE
        assert label["preferred_title"] == "Bound by Fate"
        assert label["preferred_title_source"] == "recording_alias"


def test_album_edition_list_is_queued_cached_and_stale_revalidated(db, monkeypatch):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch = app.state.approval_prefetch
        prefetch.workers_enabled = True
        args = {"user_id": "admin", "recording_id": JAPANESE_RECORDING}
        first = client.get("/api/musicbrainz/releases", params=args)
        assert first.status_code == 202 and first.json()["pending"]
        with app.state.catalogue.db() as con:
            assert con.execute("SELECT 1 FROM approval_prefetch_jobs WHERE kind='releases'").fetchone()
        seen = []
        def fake_releases(recording, page):
            seen.append((recording, page))
            return {"recording_id": recording, "page": page, "total": 1,
                    "releases": [{"id": OFFICIAL_RELEASE, "title": "Chrono Cross Orchestral Arrangement",
                                  "status": "Official"}]}
        monkeypatch.setattr(app.state.musicbrainz_search, "request_releases", fake_releases)
        asyncio.run(prefetch.process(prefetch.claim()))
        done = client.get("/api/musicbrainz/releases", params=args)
        assert done.status_code == 200
        assert done.json()["releases"][0]["id"] == OFFICIAL_RELEASE
        assert done.json()["cache_status"] == "fresh"
        assert len(seen) == 1
        assert client.get("/api/musicbrainz/releases", params=args).status_code == 200
        assert len(seen) == 1
        with app.state.catalogue.db() as con:
            con.execute("UPDATE approval_prefetch_cache SET expires_at=0 WHERE kind='releases'")
        stale = client.get("/api/musicbrainz/releases", params=args)
        assert stale.status_code == 200 and stale.json()["cache_status"] == "stale"
        with app.state.catalogue.db() as con:
            assert con.execute("SELECT 1 FROM approval_prefetch_jobs WHERE kind='releases'").fetchone()


def test_pseudo_title_can_be_reused_from_existing_catalogue_release_tracks(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        with catalogue.db() as con:
            catalogue.ingest(con, "release", {
                "id": PSEUDO_RELEASE, "title": "CHRONO CROSS live",
                "status": "Pseudo-Release", "text-representation": {"language": "eng"},
            })
            catalogue._store_release_tracks(con, {"id": PSEUDO_RELEASE, "media": [{
                "position": 1,
                "tracks": [{"position": 18, "id": str(uuid.uuid4()),
                            "title": "Bound by Fate",
                            "recording": {"id": JAPANESE_RECORDING,
                                          "title": JAPANESE_TITLE}}],
            }]})
            catalogue.ingest(con, "recording", {
                "id": JAPANESE_RECORDING, "title": JAPANESE_TITLE,
                "artist-credit": [], "aliases": [],
            })
        label = prefetch.names_batch([JAPANESE_RECORDING])["recordings"][JAPANESE_RECORDING]
        assert label["preferred_title"] == "Bound by Fate"
        assert label["preferred_title_source"] == "pseudo_release"
        assert label["preferred_title_release_id"] == PSEUDO_RELEASE


def test_selective_cleanup_retains_new_album_and_pseudo_title_cache_for_local_music(db, track):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        db.execute("UPDATE tracks SET mbid=? WHERE id=?", (JAPANESE_RECORDING, track["id"]))
        prefetch._store("recording", JAPANESE_RECORDING, {
            "id": JAPANESE_RECORDING, "title": JAPANESE_TITLE, "artist_credits": [],
        }, NAME_TTL)
        prefetch._store("releases", JAPANESE_RECORDING + ":1", {
            "recording_id": JAPANESE_RECORDING, "page": 1, "total": 1,
            "releases": [{"id": PSEUDO_RELEASE, "status": "Pseudo-Release"}],
        }, NAME_TTL)
        prefetch._store("pseudo_release", PSEUDO_RELEASE, {
            "id": PSEUDO_RELEASE, "titles": [
                {"recording_id": JAPANESE_RECORDING, "title": "Bound by Fate"}],
        }, NAME_TTL)
        prefetch._store("pseudo_title", JAPANESE_RECORDING, {
            "title": "Bound by Fate", "release_id": PSEUDO_RELEASE, "language": "eng",
        }, NAME_TTL)
        orphan = str(uuid.uuid4())
        prefetch._store("releases", orphan + ":1", {
            "recording_id": orphan, "page": 1, "releases": [],
        }, NAME_TTL)
        prefetch.cleanup_unused()
        with app.state.catalogue.db() as con:
            kinds = {(x["kind"], x["cache_key"]) for x in con.execute(
                "SELECT kind,cache_key FROM approval_prefetch_cache")}
        assert ("releases", JAPANESE_RECORDING + ":1") in kinds
        assert ("pseudo_release", PSEUDO_RELEASE) in kinds
        assert ("pseudo_title", JAPANESE_RECORDING) in kinds
        assert ("releases", orphan + ":1") not in kinds


def test_existing_cached_searches_backfill_all_release_metadata_jobs(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        _create_waiting_review(db, title="Japanese Pending Track", artist="Composer")
        ids = [str(uuid.uuid4()) for _ in range(20)]
        key = search_key("recording", "Japanese Pending Track", "Composer", None, 1)
        prefetch._store("search", key, {
            "entity": "recording", "page": 1, "total": 20,
            "items": [{"id": rid, "title": JAPANESE_TITLE} for rid in ids],
        }, SEARCH_TTL)
        prefetch.seed_pending_approvals()
        with app.state.catalogue.db() as con:
            release_keys = {row["cache_key"] for row in con.execute(
                "SELECT cache_key FROM approval_prefetch_jobs WHERE kind='releases'")}
        assert {rid + ":1" for rid in ids}.issubset(release_keys)


def test_failed_prefetch_moves_to_queue_tail_after_three_errors_then_disables(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch = app.state.approval_prefetch
        catalogue = app.state.catalogue
        assert prefetch.enqueue_names(recordings=[RECORDING]) == 1
        for attempt in range(1, 4):
            job = prefetch.claim()
            assert job["cache_key"] == RECORDING
            prefetch.defer(job, ValueError("Repeated invalid response"))
            with catalogue.db() as con:
                row = con.execute(
                    "SELECT state,priority,attempts FROM approval_prefetch_jobs WHERE cache_key=?",
                    (RECORDING,),
                ).fetchone()
                assert row["attempts"] == attempt
                assert row["state"] == "PENDING"
                if attempt == 3:
                    assert row["priority"] == 1000
                con.execute("UPDATE approval_prefetch_jobs SET retry_at=0 WHERE cache_key=?",
                            (RECORDING,))
        # Scheduled discovery is not allowed to promote this failing request.
        assert prefetch.enqueue_names(recordings=[RECORDING], priority=0) == 0
        with catalogue.db() as con:
            row = con.execute(
                "SELECT priority FROM approval_prefetch_jobs WHERE cache_key=?",
                (RECORDING,),
            ).fetchone()
            assert row["priority"] == 1000

        assert prefetch.enqueue_names(recordings=[OTHER], priority=20) == 1
        other = prefetch.claim()
        assert other["cache_key"] == OTHER, "The normal queue must outrank a failed item"
        assert prefetch._store("recording", OTHER, {"id": OTHER, "title": "Other"},
                               NAME_TTL, expected_job=True)
        for attempt in range(4, 7):
            job = prefetch.claim()
            assert job["cache_key"] == RECORDING
            prefetch.defer(job, ValueError("Repeated invalid response"))
            with catalogue.db() as con:
                row = con.execute(
                    "SELECT state,priority,attempts FROM approval_prefetch_jobs WHERE cache_key=?",
                    (RECORDING,),
                ).fetchone()
                assert row["attempts"] == attempt
                assert row["priority"] == 1000
                assert row["state"] == ("FAILED" if attempt == 6 else "PENDING")
                if attempt < 6:
                    con.execute(
                        "UPDATE approval_prefetch_jobs SET retry_at=0 WHERE cache_key=?",
                        (RECORDING,))
        assert prefetch.claim() is None
        assert prefetch.enqueue_names(recordings=[RECORDING]) == 0, (
            "Periodic reseeding must not restart a disabled request")
        status = prefetch.status()
        assert status["failed"] == 1
        assert status["queued"] == 0
        assert status["total"] == status["completed"] + status["failed"]


def test_manual_failed_prefetch_retry_allows_exactly_three_tail_attempts(db):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        prefetch.enqueue_releases([RECORDING])
        with catalogue.db() as con:
            con.execute(
                """UPDATE approval_prefetch_jobs SET state='FAILED',attempts=6,
                   priority=1000,last_error='MusicBrainz releases returned HTTP 404'
                   WHERE kind='releases'""",
            )
        failures = client.get("/api/musicbrainz/approval-prefetch-failures",
                              params={"user_id": "admin"}).json()
        assert failures["total"] == 1
        item = failures["items"][0]
        assert item["kind"] == "releases"
        assert RECORDING in item["label"]
        assert item["attempts"] == 6
        assert "404" in item["last_error"]
        assert len(item["job_id"]) == 32
        assert "cache_key" not in item
        assert client.get("/api/musicbrainz/approval-prefetch-failures",
                          params={"user_id": "invalid"}).status_code == 400
        assert client.post("/api/musicbrainz/approval-prefetch-retry",
                           json={"user_id": "invalid", "job_id": item["job_id"]}).status_code == 400
        assert client.post("/api/musicbrainz/approval-prefetch-retry",
                           json={"user_id": "admin", "job_id": "not-a-job"}).status_code == 422
        bad = client.post("/api/musicbrainz/approval-prefetch-retry",
                          json={"user_id": "admin", "job_id": "0" * 32})
        assert bad.status_code == 404
        response = client.post("/api/musicbrainz/approval-prefetch-retry",
                               json={"user_id": "admin", "job_id": item["job_id"]})
        assert response.status_code == 200
        assert response.json()["attempts_remaining"] == 3
        assert client.get("/api/musicbrainz/approval-prefetch-failures",
                          params={"user_id": "admin"}).json()["total"] == 0
        assert client.post("/api/musicbrainz/approval-prefetch-retry",
                           json={"user_id": "admin", "job_id": item["job_id"]}).status_code == 404
        with catalogue.db() as con:
            row = con.execute(
                "SELECT state,attempts,priority,retry_at,last_error FROM approval_prefetch_jobs"
            ).fetchone()
            assert dict(row) == {
                "state": "PENDING", "attempts": 3, "priority": 1000,
                "retry_at": 0, "last_error": None,
            }
        for attempt in (4, 5, 6):
            job = prefetch.claim()
            assert job["kind"] == "releases"
            prefetch.defer(job, ValueError("Still unavailable"))
            if attempt < 6:
                with catalogue.db() as con:
                    con.execute("UPDATE approval_prefetch_jobs SET retry_at=0")
        assert prefetch.status()["failed"] == 1


def test_single_bad_lookup_does_not_pause_other_jobs_or_exhaust_shared_slot_budget(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        from http_policy import ApiDeferred
        prefetch.enqueue_releases([RECORDING, OTHER])
        first = prefetch.claim()
        prefetch.defer(first, ApiDeferred("MusicBrainz releases returned HTTP 404",
                                         retry_at=time.time() + 60))
        assert prefetch.pause_until == 0, "An individual 404 must not pause the worker"
        assert prefetch.claim()["cache_key"].startswith(OTHER)
        with catalogue.db() as con:
            con.execute("UPDATE approval_prefetch_jobs SET state='PENDING' WHERE state='RUNNING'")
        after = prefetch.claim()
        # The second job is claimed again; it hits only the shared provider clock.
        prefetch.defer(after, ApiDeferred(
            "musicbrainz.org: waiting for shared provider cooldown",
            retry_at=time.time() + 15))
        with catalogue.db() as con:
            row = con.execute(
                "SELECT attempts FROM approval_prefetch_jobs WHERE cache_key=?",
                (after["cache_key"],),
            ).fetchone()
        assert row["attempts"] == 0, "A shared clock wait is not a failed execution"
        assert prefetch.pause_until > time.time()


def test_prefetch_upgrade_disables_legacy_endlessly_retried_jobs(db):
    app = create_app(db, start_workers=False)
    with TestClient(app):
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        prefetch.enqueue_releases([RECORDING])
        prefetch.enqueue_releases([OTHER])
        with catalogue.db() as con:
            con.execute(
                """UPDATE approval_prefetch_jobs SET attempts=72,
                   last_error='MusicBrainz releases returned HTTP 404',
                   state='PENDING' WHERE cache_key=?""", (RECORDING + ":1",))
            con.execute(
                """UPDATE approval_prefetch_jobs SET attempts=72,
                   last_error='musicbrainz.org: waiting for shared provider cooldown',
                   state='PENDING' WHERE cache_key=?""", (OTHER + ":1",))
        prefetch.init()
        with catalogue.db() as con:
            rows = {r["cache_key"]: dict(r) for r in con.execute(
                "SELECT cache_key,state,attempts,priority FROM approval_prefetch_jobs")}
        assert rows[RECORDING + ":1"]["state"] == "FAILED"
        assert rows[RECORDING + ":1"]["attempts"] == 72
        assert rows[OTHER + ":1"]["state"] == "PENDING"
        assert rows[OTHER + ":1"]["attempts"] == 0


def test_failed_search_labels_do_not_expose_unowned_library_queries(db):
    app = create_app(db, start_workers=False)
    with TestClient(app) as client:
        prefetch, catalogue = app.state.approval_prefetch, app.state.catalogue
        assert prefetch.enqueue_search("recording", "Private video title", "Artist Name") == 1
        with catalogue.db() as con:
            con.execute(
                "UPDATE approval_prefetch_jobs SET state='FAILED',attempts=6,"
                "last_error='MusicBrainz search returned HTTP 404: Private video title' WHERE kind='search'")
        admin = client.get("/api/musicbrainz/approval-prefetch-failures",
                           params={"user_id": "admin"}).json()
        guest = client.get("/api/musicbrainz/approval-prefetch-failures",
                           params={"user_id": "guest"}).json()
        assert admin["total"] == guest["total"] == 1
        assert admin["items"][0]["job_id"] == guest["items"][0]["job_id"]
        assert "Private video title" not in str(admin)
        assert "Private video title" not in str(guest)
        assert admin["items"][0]["label"].startswith("Search request #")
        assert guest["items"][0]["label"].startswith("Search request #")
