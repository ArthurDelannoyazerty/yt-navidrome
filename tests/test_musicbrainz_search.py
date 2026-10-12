"""Literal search, bounded caching and a shared per-host MusicBrainz request gate."""
import asyncio
import json
import time
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from http_policy import ApiDeferred, HttpPolicy
from main import create_app
from musicbrainz_search import MusicBrainzSearch, normalize, query_for, PAGE_SIZE
from store import Store

ID = "11111111-1111-4111-8111-111111111111"
RID = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("title,artist,artist_id,expected", [
    ("Song", "", None, 'recording:("Song")'),
    ("", "Ganja White Night", None, 'artist:("Ganja" AND "White" AND "Night")'),
    ("Some Song", "Artist", None, 'recording:("Some" AND "Song") AND artist:("Artist")'),
    ("", "ignored", ID, "arid:" + ID),
])
def test_recording_search_supports_title_artist_and_artist_id(title, artist, artist_id, expected):
    assert query_for("recording", title, artist, artist_id) == expected


def test_user_text_cannot_inject_lucene_operators():
    query = query_for("recording", 'Song OR artist:*', 'AC/DC "Live"', None)
    assert '"OR"' in query
    assert '"artist\\:\\*"' in query
    assert '"AC\\/DC"' in query
    assert '\\"Live\\"' in query


@pytest.mark.parametrize("entity,title,artist,artist_id", [
    ("recording", "", "", None), ("artist", "ignored", "", None),
    ("recording", "Song", "", "not-a-uuid"), ("work", "Song", "Artist", None),
    ("recording", "x" * 201, "", None),
])
def test_invalid_search_is_rejected_without_network(entity, title, artist, artist_id):
    with pytest.raises(ValueError): query_for(entity, title, artist, artist_id)


def test_artist_search_returns_separate_entity_and_recording_versions():
    artists = normalize({"count": 1, "artists": [{"id": ID, "name": "Artist", "country": "BE", "type": "Group"}]}, "artist", 1)
    assert artists["items"][0]["name"] == "Artist"
    raw = {"count": 24, "recordings": [{"id": ID, "title": "Song", "length": 200000,
           "score": 100, "disambiguation": "live", "artist-credit": [{"name": "A", "joinphrase": " feat. "}, {"artist": {"name": "B"}}],
           "releases": [{"id": RID, "title": "Album", "date": "2020-01-01"}]}]}
    values = normalize(raw, "recording", 2)
    item = values["items"][0]
    assert item["artist"] == "A feat. B" and item["length_ms"] == 200000
    assert item["releases"][0]["id"] == RID and item["disambiguation"] == "live"
    assert values["limit"] == PAGE_SIZE and values["page"] == 2 and values["total"] == 24
    assert "not audio" in values["score_note"]


def test_identical_concurrent_queries_coalesce_and_cache(db, monkeypatch):
    searcher = MusicBrainzSearch(db); calls = []
    def request(*args):
        calls.append(args); return {"items": [{"id": ID}], "total": 1}
    monkeypatch.setattr(searcher, "request", request)
    async def run():
        first, second = await asyncio.gather(searcher.search(title="Song"), searcher.search(title="Song"))
        first["items"].clear()
        assert second["items"]
        assert (await searcher.search(title="Song"))["items"]
    asyncio.run(run())
    assert len(calls) == 1


def test_cache_is_bounded_and_expiry_triggers_refresh(db, monkeypatch):
    searcher = MusicBrainzSearch(db); calls = []
    monkeypatch.setattr(searcher, "request", lambda *a: calls.append(a) or {"items": []})
    async def run():
        for i in range(130): await searcher.search(title=f"Song {i}")
        assert len(searcher.cache) == 128
        key = next(reversed(searcher.cache)); searcher.cache[key] = (0, {"items": []})
        await searcher.search(title="Song 129")
    asyncio.run(run())
    assert len(calls) == 131


def test_api_rate_error_has_retry_after_and_unknown_user_cannot_search(db, monkeypatch):
    app = create_app(db, start_workers=False)
    async def denied(*a, **kw):
        raise ApiDeferred("cooldown", retry_at=time.time() + 30)
    monkeypatch.setattr(app.state.musicbrainz_search, "search", denied)
    with TestClient(app) as client:
        response = client.get("/api/musicbrainz/search", params={"user_id": "admin", "title": "Song"})
        assert response.status_code == 429
        assert 29 <= int(response.headers["Retry-After"]) <= 31
        assert response.json()["retry_after"] >= 29
        assert client.get("/api/musicbrainz/search", params={"user_id": "nobody", "title": "Song"}).status_code == 400


def test_search_request_is_fixed_host_with_identifying_user_agent(db, monkeypatch):
    calls = []
    def send(self, original, session, prepared, **kwargs):
        calls.append((prepared, kwargs)); r = requests.Response(); r.status_code = 200
        r._content = json.dumps({"count": 0, "recordings": []}).encode(); return r
    monkeypatch.setattr(HttpPolicy, "send", send)
    MusicBrainzSearch(db).request("recording", 'recording:("Song")', 3)
    prepared, kwargs = calls[0]
    assert prepared.url.startswith("https://musicbrainz.org/ws/2/recording?")
    assert "offset=40" in prepared.url
    assert "Music-Ingestor/" in prepared.headers["User-Agent"]
    assert kwargs["allow_redirects"] is False


def test_beets_and_ui_share_non_accumulating_clock(db, monkeypatch):
    import http_policy
    clock = [1000.0]; calls = []
    monkeypatch.setattr(http_policy.time, "time", lambda: clock[0])
    monkeypatch.setattr(http_policy.time, "sleep", lambda wait: clock.__setitem__(0, clock[0] + wait))
    def send(*a, **kw):
        calls.append(clock[0]); r = requests.Response(); r.status_code = 200; r._content = b'{}'; return r
    request = requests.Request("GET", "https://musicbrainz.org/ws/2/recording").prepare()
    # Separate Store and policy instances model the UI and isolated beets process.
    first = HttpPolicy(db); second = HttpPolicy(Store(db.path))
    for policy in (first, second, first, second):
        policy.send(send, requests.Session(), request)
    assert all(b - a >= 1.099 for a, b in zip(calls, calls[1:]))
    db.defer_api("musicbrainz.org", clock[0] + 3600)
    original = db.one("SELECT next_at FROM api_clock WHERE host='musicbrainz.org'")["next_at"]
    for policy in (first, second):
        with pytest.raises(ApiDeferred): policy.send(send, requests.Session(), request)
    assert db.one("SELECT next_at FROM api_clock WHERE host='musicbrainz.org'")["next_at"] == original
    assert len(calls) == 4


def test_cooldown_set_during_wait_is_rechecked(db, monkeypatch):
    import http_policy
    clock = [1000.0]; sleeps = []
    monkeypatch.setattr(http_policy.time, "time", lambda: clock[0])
    db.defer_api("musicbrainz.org", 1001.1)
    def sleep(wait):
        sleeps.append(wait); clock[0] += wait
        if len(sleeps) == 1: db.defer_api("musicbrainz.org", clock[0] + 10)
    monkeypatch.setattr(http_policy.time, "sleep", sleep)
    with pytest.raises(ApiDeferred): HttpPolicy(db)._musicbrainz_slot()
    assert len(sleeps) == 1
    assert db.one("SELECT next_at FROM api_clock")["next_at"] == clock[0] + 10


def test_frontend_new_elements_and_scripts_are_present_and_cache_versioned(db):
    import re
    root = Path(__file__).parents[1]
    html = (root / "src/static/index.html").read_text()
    js = (root / "src/static/metadata-ui.js").read_text()
    assert set(re.findall(r'\$\("([^"\n]+)"\)', js)) <= set(re.findall(r'id="([^"\n]+)"', html))
    with TestClient(create_app(db, start_workers=False)) as client:
        page = client.get("/").text
        assert re.search(r'/static/metadata-ui\.js\?v=[a-f0-9]{12}', page)
    assert '"source"' in js and "500" in js
