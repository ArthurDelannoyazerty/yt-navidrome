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
        assert data["pending_recordings"] == [RECORDING], "Catalogue snippets can be incomplete"
        assert data["pending_artists"] == [ARTIST]

        prefetch = app.state.approval_prefetch
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
