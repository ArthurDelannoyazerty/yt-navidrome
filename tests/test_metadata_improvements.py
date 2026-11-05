"""Metadata review evidence, conservative search hints and explicit correction memory."""
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient
from mutagen.oggopus import OggOpus

import metadata_routes
from common import sha256_file
from http_policy import ApiDeferred, HttpPolicy
from main import create_app
from metadata_hints import clean_search_text, search_hints, version_clues
from metadata_routes import metadata_context
from musicbrainz_search import MusicBrainzSearch, normalize
from source_metadata import correction_revision, identification_hints, snapshot
from test_reliability import MBID, active_track

ARTIST = "33333333-3333-4333-8333-333333333333"
OTHER = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize("raw,expected", [
    ("誓いの旋律 【Official Music Video】", "誓いの旋律"),
    ("Ｓｏｎｇ　[HD]", "Song"),
    ("Song (Live) [Official Video]", "Song (Live)"),
    ("Song [TV size] (Instrumental)", "Song [TV size] (Instrumental)"),
    ("Song (Remix) - Official Audio", "Song (Remix)"),
    ("The Official - Song", "The Official - Song"),
    ("Song (an unrelated description)", "Song (an unrelated description)"),
])
def test_query_cleanup_is_conservative_and_preserves_versions(raw, expected):
    assert clean_search_text(raw) == expected


def test_topic_channel_is_a_hint_but_arbitrary_uploader_is_not():
    source = {"source_title": "誓いの旋律", "tags": {"title": ["誓いの旋律"]}, "basis": "provider_title_only"}
    topic = search_hints(source, {"channel": "Kenichiro Suehiro - Topic"})
    assert topic["artist"] == "Kenichiro Suehiro" and topic["artist_basis"] == "topic_channel"
    assert topic["artist_id"] is None
    assert search_hints(source, {"channel": "Anime music uploads"})["artist"] == ""
    assert source["tags"] == {"title": ["誓いの旋律"]}
    assert version_clues("曲 (TV size) [Live] インスト") == ["live", "instrumental", "TV size"]


def test_archive_is_unchanged_by_normalization_or_topic_artist_hint(db, track):
    origin = db.origins_for_track(track["id"])[0]
    db.update("track_origins", origin["id"], title="曲 【Official Video】", channel="Artist - Topic")
    origin = db.origins_for_track(track["id"])[0]
    before = snapshot(db, origin)
    hint = identification_hints(db, origin)
    assert hint["title"] == "曲" and hint["artist"] == "Artist"
    assert snapshot(db, origin) == before
    assert before["tags"] == {"title": ["曲 【Official Video】"]}


def test_review_shows_only_selected_origins_evidence_and_public_choices(db, track, environment):
    track, origin, _ = active_track(db, track, environment)
    other_origin = str(uuid.uuid4())
    with db.db() as con:
        con.execute("INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (other_origin, track["id"], "admin", "youtube", "other", "other-url", "Other origin", "2020", "2020"))
        con.execute("INSERT INTO identification_reports VALUES (?,?,?)",
                    (origin["id"], json.dumps({"origin_id": origin["id"], "at": "2020"}), "2020"))
        con.execute("INSERT INTO identification_reports VALUES (?,?,?)",
                    (other_origin, json.dumps({"origin_id": other_origin, "at": "2040"}), "2040"))
    choices = [
        {"kind": "candidate", "mbid": OTHER, "title": "Candidate", "tags": {"acoustid_fingerprint": "private-large-data"}},
        {"kind": "source", "mbid": None, "title": "Source", "source": {"tags": {"title": ["raw"]}}},
    ]
    db.update("tracks", track["id"], choices=json.dumps(choices))
    data = metadata_context(db, track["id"], "admin")
    assert data["latest_identification"]["origin_id"] == origin["id"]
    assert [c["index"] for c in data["choices"]] == [0, 1]
    assert all("tags" not in c and "source" not in c for c in data["choices"])
    assert data["approval_token"] == db.approval_token(db.owned("tracks", track["id"], "admin"))
    db.pause_for_approval(track["id"], choices, {"origin_id": other_origin, "operation": "next", "action": "reprocess"}, "/candidate.opus")
    assert metadata_context(db, track["id"], "admin")["latest_identification"]["origin_id"] == other_origin


def test_current_tags_are_read_from_own_audio_not_source_fallback(db, track, environment):
    track, _, path = active_track(db, track, environment)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", str(path)], check=True)
    audio = OggOpus(path)
    audio["title"] = ["Current title"]; audio["artist"] = ["Current artist"]
    audio["album"] = ["Current album"]; audio["comment"] = ["Not exposed as an editable music field"]
    audio.save()
    data = metadata_context(db, track["id"], "admin")
    assert data["current_tags"] == {"title": ["Current title"], "artist": ["Current artist"], "album": ["Current album"]}
    assert 0.8 < data["current_duration_seconds"] < 1.2
    assert data["current_tags_error"] is None
    assert data["source"]["tags"]["title"] == ["Song"]
    outside = environment[1].parent / "outside.opus"
    outside.write_bytes(path.read_bytes())
    db.update("tracks", track["id"], file_path=str(outside))
    assert metadata_context(db, track["id"], "admin")["current_tags"] == {}


def test_artist_alias_is_explicit_server_resolved_user_scoped_and_reversible(db, track, environment, monkeypatch):
    track, origin, _ = active_track(db, track, environment)
    db.update("track_origins", origin["id"], title="曲", channel="Kenichiro Suehiro - Topic")
    db.update("tracks", track["id"], updated_at="2040-01-01T00:00:00.000001Z")
    app = create_app(db, start_workers=False)
    resolved = []
    async def artist(identity):
        resolved.append(identity)
        return {"id": ARTIST, "name": "末廣健一郎", "aliases": []}
    monkeypatch.setattr(app.state.musicbrainz_search, "artist", artist)
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(metadata_routes, "datetime", FrozenDateTime)
    before = metadata_context(db, track["id"], "admin")
    with TestClient(app) as client:
        body = {"user_id": "admin", "revision": before["revision"], "artist_id": ARTIST}
        assert client.post(f"/api/tracks/{track['id']}/artist-alias", json={**body, "user_id": "guest"}).status_code == 404
        response = client.post(f"/api/tracks/{track['id']}/artist-alias", json=body)
        assert response.status_code == 200, response.text
        learned = response.json()
        assert learned["search_hints"]["artist_id"] == ARTIST
        assert learned["search_hints"]["artist"] == "末廣健一郎"
        assert learned["approved_artist_alias"]["alias"] == "Kenichiro Suehiro"
        assert learned["revision"] != before["revision"]
        assert learned["source"] == before["source"]
        assert client.post(f"/api/tracks/{track['id']}/artist-alias", json=body).status_code == 400
        second = client.post(f"/api/tracks/{track['id']}/artist-alias", json={**body, "revision": learned["revision"]}).json()
        assert second["revision"] != learned["revision"]
        response = client.post(f"/api/tracks/{track['id']}/artist-alias",
                               json={"user_id": "admin", "revision": second["revision"], "forget": True})
        assert response.status_code == 200
        assert response.json()["approved_artist_alias"] is None
    assert resolved == [ARTIST, ARTIST]
    assert not db.rows("SELECT * FROM metadata_artist_aliases")
    assert db.owned("tracks", track["id"], "admin")["mbid"] == MBID


def test_alias_is_not_reused_by_another_library_user(db, track, environment):
    _, origin, _ = active_track(db, track, environment)
    db.execute("INSERT INTO metadata_artist_aliases VALUES (?,?,?,?,?,?)",
               ("admin", "artist", "Artist", ARTIST, "Resolved artist", "2026"))
    assert identification_hints(db, origin)["artist_id"] == ARTIST
    guest = {**origin, "user_id": "guest"}
    assert identification_hints(db, guest)["artist_id"] is None


@pytest.mark.parametrize("state,health,allowed", [
    ("STOPPED", "AVAILABLE", True), ("STOPPED", "MISSING", False),
    ("RUNNING", "AVAILABLE", False), ("DEFERRED", "AVAILABLE", False),
])
def test_review_memory_honors_operation_state_and_playable_stopped_tracks(db, track, environment, monkeypatch, state, health, allowed):
    track, _, _ = active_track(db, track, environment)
    db.update("tracks", track["id"], operation_state=state, health=health)
    app = create_app(db, start_workers=False)
    async def artist(identity):
        return {"id": ARTIST, "name": "Artist", "aliases": []}
    monkeypatch.setattr(app.state.musicbrainz_search, "artist", artist)
    context = metadata_context(db, track["id"], "admin")
    with TestClient(app) as client:
        response = client.post(f"/api/tracks/{track['id']}/artist-alias",
                               json={"user_id": "admin", "revision": context["revision"], "artist_id": ARTIST})
    assert (response.status_code == 200) is allowed, response.text
    assert context["can_correct"] is allowed


def test_rejecting_candidate_preserves_audio_source_fallback_and_approval_binding(db, track, environment):
    track, origin, path = active_track(db, track, environment)
    before_hash = sha256_file(path)
    choices = [
        {"kind": "candidate", "mbid": OTHER, "title": "Wrong"},
        {"kind": "source", "mbid": None, "title": "Original"},
        {"kind": "asis", "mbid": OTHER, "title": "Keep current"},
        {"kind": "candidate", "mbid": ARTIST, "title": "Other suggestion"},
    ]
    pending = {"origin_id": origin["id"], "action": "reprocess", "operation": "fixture", "candidate_sha256": "candidate-digest"}
    db.pause_for_approval(track["id"], choices, pending, "/candidate.opus")
    before = metadata_context(db, track["id"], "admin")
    with TestClient(create_app(db, start_workers=False)) as client:
        body = {"user_id": "admin", "revision": before["revision"], "mbid": OTHER}
        response = client.post(f"/api/tracks/{track['id']}/reject-candidate", json=body)
        assert response.status_code == 200, response.text
        after = response.json()
        assert after["revision"] != before["revision"]
        assert after["approval_token"] != before["approval_token"]
        assert [c["kind"] for c in after["choices"]] == ["source", "asis", "candidate"]
        assert [c["index"] for c in after["choices"]] == [0, 1, 2]
        assert after["rejected_candidates"][0]["mbid"] == OTHER
        assert client.post(f"/api/tracks/{track['id']}/reject-candidate", json=body).status_code == 400
        restored = client.post(f"/api/tracks/{track['id']}/reject-candidate",
                               json={**body, "revision": after["revision"], "reject": False})
        assert restored.status_code == 200
        assert restored.json()["rejected_candidates"] == []
    assert sha256_file(path) == before_hash
    current = db.owned("tracks", track["id"], "admin")
    assert current["mbid"] == MBID
    assert json.loads(current["pending_operation"]) == pending
    assert not db.rows("SELECT * FROM jobs WHERE state IN ('PENDING','RUNNING')")


def test_unknown_candidate_and_active_jobs_cannot_change_review_memory(db, track, environment):
    track, _, _ = active_track(db, track, environment)
    context = metadata_context(db, track["id"], "admin")
    with TestClient(create_app(db, start_workers=False)) as client:
        url = f"/api/tracks/{track['id']}/reject-candidate"
        body = {"user_id": "admin", "revision": context["revision"], "mbid": OTHER}
        assert client.post(url, json=body).status_code == 400
        assert client.post(url, json={**body, "user_id": "guest"}).status_code == 404
        db.enqueue("track", track["id"], "admin", {"mode": "comment"})
        assert client.post(url, json={**body, "mbid": MBID}).status_code == 400
    assert not db.rows("SELECT * FROM origin_identity_rejections")


def test_search_fallbacks_are_bounded_disclosed_and_page_stable(db, monkeypatch):
    searcher = MusicBrainzSearch(db)
    calls = []
    def request(entity, query, page):
        calls.append((query, page))
        found = "artist:" not in query and "Official" not in query
        return {"items": [{"id": MBID}] if found and page == 1 else [], "total": 40 if found else 0, "page": page}
    monkeypatch.setattr(searcher, "request", request)
    async def run():
        first = await searcher.search(title="Song [Official Video]", artist="Unknown")
        assert first["fallbacks_tried"] == ["exact", "cleaned", "title_only"]
        assert first["query_strategy"] == "title_only"
        assert first["query_used"] == {"title": "Song", "artist": "", "artist_id": None}
        assert "Verify the artist" in first["query_note"]
        second = await searcher.search(title="Song [Official Video]", artist="Unknown", page=2)
        assert second["query_strategy"] == "title_only" and second["page"] == 2
        assert second["query_used"] == first["query_used"]
    asyncio.run(run())
    assert len(calls) == 4  # three first-page requests, then only the effective second page
    assert calls[-1] == ('recording:("Song")', 2)


def test_artist_id_is_never_dropped_by_fallback_and_errors_do_not_broaden_search(db, monkeypatch):
    searcher = MusicBrainzSearch(db)
    calls = []
    def empty(entity, query, page):
        calls.append(query)
        return {"items": [], "total": 0}
    monkeypatch.setattr(searcher, "request", empty)
    result = asyncio.run(searcher.search(title="Song [Official Video]", artist="Artist", artist_id=ARTIST))
    assert len(calls) == 2 and all("arid:" + ARTIST in q for q in calls)
    assert result["query_strategy"] == "cleaned"
    def unavailable(*args):
        calls.append("unavailable")
        raise ApiDeferred("wait")
    monkeypatch.setattr(searcher, "request", unavailable)
    with pytest.raises(ApiDeferred):
        asyncio.run(searcher.search(title="Different Song", artist="Artist"))
    assert calls.count("unavailable") == 1


def test_search_results_expose_artist_identity_and_aliases():
    alias = {"name": "Kenichiro Suehiro", "sort-name": "Suehiro, Kenichiro", "locale": "en", "primary": True}
    artist = {"id": ARTIST, "name": "末廣健一郎", "sort-name": "末廣健一郎", "aliases": [alias]}
    result = normalize({"count": 1, "recordings": [{"id": MBID, "title": "曲", "artist-credit": [{"artist": artist, "name": "Credited name"}]}]}, "recording", 1)
    detail = result["items"][0]["artists"][0]
    assert detail["id"] == ARTIST and detail["name"] == "末廣健一郎"
    assert detail["credited_name"] == "Credited name"
    assert detail["aliases"][0]["name"] == "Kenichiro Suehiro"
    result = normalize({"count": 1, "artists": [artist]}, "artist", 1)
    assert result["items"][0]["aliases"][0]["locale"] == "en"


def test_alias_lookup_uses_shared_fixed_host_policy_and_validates_identity(db, monkeypatch):
    calls = []
    def send(self, original, session, prepared, **kwargs):
        calls.append((prepared, kwargs))
        response = requests.Response(); response.status_code = 200
        response._content = json.dumps({"id": ARTIST, "name": "Artist", "aliases": []}).encode()
        return response
    monkeypatch.setattr(HttpPolicy, "send", send)
    searcher = MusicBrainzSearch(db)
    async def run():
        first, second = await asyncio.gather(searcher.artist(ARTIST), searcher.artist(ARTIST))
        assert first == second and first["id"] == ARTIST
        with pytest.raises(ValueError):
            await searcher.artist("../../outside")
    asyncio.run(run())
    assert len(calls) == 1
    prepared, kwargs = calls[0]
    assert prepared.url.startswith("https://musicbrainz.org/ws/2/artist/" + ARTIST + "?")
    assert kwargs["allow_redirects"] is False


def test_bridge_uses_retained_hints_for_existing_audio_and_reports_real_beets_evidence(db, track, environment):
    """Exercise the pinned adapter, real Opus tags and actual Distance/TrackInfo APIs offline."""
    track, origin, path = active_track(db, track, environment)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:a", "libopus", str(path)], check=True)
    audio = OggOpus(path)
    audio["title"] = ["Wrong applied title"]; audio["artist"] = ["Wrong applied artist"]
    audio["musicbrainz_trackid"] = [OTHER]; audio.save()
    before = sha256_file(path)
    script = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from beets.autotag import Distance, TrackInfo, TrackMatch, Proposal
from beets.autotag.match import _recommendation
from beets.library import Item
from beetsplug import chroma
from beets_bridge import Bridge

path = Path(sys.argv[1]); bridge = Bridge("admin")
recording = "11111111-1111-4111-8111-111111111111"
artist = "33333333-3333-4333-8333-333333333333"
item = Item.from_path(str(path))
info = TrackInfo(title="曲", artists=["Known artist"], artists_ids=[artist],
                 track_id=recording, length=4.0, data_source="MusicBrainz")
distance = Distance(); distance.add("track_title", .02)
match = TrackMatch(distance, info, item)
proposal = Proposal([match], _recommendation([match]))

def lookup(source):
    tested = source.items[0]
    assert tested.title == "曲"
    assert tested.artist == "" and tested.artists == []
    assert not tested.mb_trackid and not tested.mb_artistid
    return proposal

def forbidden(*args, **kwargs):
    raise AssertionError("Offline evidence test attempted provider traffic")

hints = {"title": "曲", "artist": "", "artist_basis": "unknown", "version_clues": []}
with patch("requests.sessions.Session.send", forbidden), patch("beets.plugins.send"), \
     patch("beets.autotag.tag_item", side_effect=lookup), \
     patch.dict(chroma._matches, {item.path: ([recording], [])}, clear=True):
    result = bridge.identify(path, "曲", hints)
choice = result["choices"][0]
assert choice["match_score"] == choice["similarity"] == 98.0
assert choice["evidence"]["fingerprint_support"] is True
assert choice["evidence"]["artist_ids"] == [artist]
assert choice["evidence"]["duration_delta_seconds"] < .1
assert choice["evidence"]["penalties"][0]["field"] == "track_title"
assert result["automatic"] == (proposal.recommendation.name == "strong")
assert result["diagnostics"]["fingerprint_status"] == "matched_recordings"
assert result["diagnostics"]["search_seed"]["artist"] == ""
'''
    result = subprocess.run([sys.executable, "-c", script, str(path)],
                            env=os.environ | {"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                            text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sha256_file(path) == before


def test_bridge_entry_accepts_source_control_contract_and_confirmed_alias(db, track, environment, monkeypatch):
    import beets_bridge
    track, origin, _ = active_track(db, track, environment)
    db.execute("INSERT INTO metadata_artist_aliases VALUES (?,?,?,?,?,?)",
               ("admin", "artist", "Artist", ARTIST, "Resolved artist", "2026"))
    captured = []
    class FakeBridge:
        def __init__(self, user):
            self.store = db
        def identify(self, path, title, hints):
            captured.append((path, title, hints))
            return {"ok": True}
    monkeypatch.setattr(beets_bridge, "Bridge", FakeBridge)
    seed = {**track, "origin_id": origin["id"], "source_title": origin["title"],
            "source_tags": snapshot(db, origin)["tags"], "source_channel": None, "source_basis": "provider_title_only"}
    assert beets_bridge.main({"mode": "identify", "track": seed, "path": "candidate.opus"}) == {"ok": True}
    assert captured[-1][2]["artist_id"] == ARTIST
    assert captured[-1][2]["artist"] == "Resolved artist"
    seed.pop("origin_id")
    assert beets_bridge.main({"mode": "identify", "track": seed, "path": "candidate.opus"}) == {"ok": True}
    assert captured[-1][2]["artist"] == "Artist"
