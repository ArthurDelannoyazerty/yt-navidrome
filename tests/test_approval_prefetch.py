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
            assert not list(con.execute("SELECT * FROM approval_prefetch_jobs"))
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
        assert result["new_jobs"] == 3
        jobs = app.state.catalogue
        with jobs.db() as con:
            pending = [dict(row) for row in con.execute(
                "SELECT kind,cache_key,state FROM approval_prefetch_jobs ORDER BY requested_at"
            )]
        assert [job["kind"] for job in pending] == ["recording", "recording", "search"]
        assert {item["cache_key"] for item in pending[:2]} == {RECORDING, OTHER}
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
