from urllib.parse import urlsplit

import pytest

import spotify_provider as sp
from common import timestamp
from http_policy import ApiDeferred
from providers import Entry, Snapshot, parse_source
from spotify_auth import SpotifyHTTPError

PID, TID, AID = "P" * 22, "T" * 22, "A" * 22


def item(identity=TID, name="Song", **kwargs):
    return {"id": identity, "name": name, "type": "track", "artists": [{"name": "Artist"}], **kwargs}


def page(items, offset=0, total=None, next=None):
    return {"items": items, "offset": offset, "total": len(items) if total is None else total, "next": next}


class Client:
    def __init__(self, responses):
        self.responses = iter(responses); self.calls = []
    def get(self, path, **params):
        self.calls.append((path, params))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


class HTTP:
    def __init__(self):
        self.messages = []
    def report(self, *message):
        self.messages.append(message)


def resolve(monkeypatch, responses, kind="playlist"):
    client = Client(responses); http = HTTP()
    monkeypatch.setattr(sp, "SpotifyClient", lambda *args: client)
    source = {"user_id": "admin", "source_key": f"{kind}:{PID}", "provider": "spotify"}
    result = sp.Spotify().resolve(source, http)
    return result, client, http


@pytest.mark.parametrize("url,kind,identity", [
    (f"https://open.spotify.com/track/{TID}?si=secret", "track", TID),
    (f"https://open.spotify.com/intl-fr/playlist/{PID}", "playlist", PID),
    (f"https://open.spotify.com/album/{AID}/", "album", AID),
    (f"spotify:playlist:{PID}", "playlist", PID),
    (f"spotify:track:{TID}", "track", TID),
    (f"spotify:album:{AID}", "album", AID),
])
def test_spotify_canonical_urls(url, kind, identity):
    ref = parse_source(url)
    assert ref.provider == "spotify" and ref.key == kind + ":" + identity
    assert ref.is_playlist == (kind != "track")
    assert ref.url == f"https://open.spotify.com/{kind}/{identity}"


@pytest.mark.parametrize("url", [f"https://open.spotify.com.evil.test/track/{TID}",
    f"https://user:pass@open.spotify.com/track/{TID}", f"http://open.spotify.com/track/{TID}",
    f"https://open.spotify.com:8000/track/{TID}", f"https://open.spotify.com/artist/{TID}",
    "https://spotify.link/example", "spotify:local:artist:track", "spotify:track:bad", "https://open.spotify.com/playlist/%2e%2e",
])
def test_unsupported_or_unsafe_urls(url):
    with pytest.raises(ValueError):
        parse_source(url)


def test_new_playlist_shape_duplicate_occurrences_and_exact_times(monkeypatch):
    added = "2021-04-17T23:36:42+02:00"
    result, client, http = resolve(monkeypatch, [
        {"name": "Playlist", "snapshot_id": "v1"},
        page([{"item": item(), "added_at": added}, {"item": item(), "added_at": None}], total=3,
             next=f"https://api.spotify.com/v1/playlists/{PID}/items?offset=2&limit=50"),
        page([{"item": item("U" * 22), "added_at": "2020-01-01T00:00:00Z"}], offset=2, total=3),
        {"snapshot_id": "v1"},
    ])
    assert len(result.entries) == 3
    assert result.entries[0].added_at == "2021-04-17T21:36:42Z"
    assert result.entries[1].added_at is None
    assert len({e.entry_id for e in result.entries}) == 3
    assert [e.position for e in result.entries] == [0, 1, 2]
    assert not any(e.downloadable for e in result.entries)
    assert client.calls[2][1]["offset"] == 2
    assert client.calls[1][0].endswith("/items")


def test_skips_local_podcasts_and_null_preserves_positions(monkeypatch):
    result, _, http = resolve(monkeypatch, [
        {"name": "Mixed", "snapshot_id": "v1"},
        page([{"item": None}, {"item": item(), "is_local": True},
              {"item": {"type": "episode", "id": TID}}, {"track": item(), "added_at": None}]),
        {"snapshot_id": "v1"},
    ])
    assert len(result.entries) == 1 and result.entries[0].position == 3
    assert "Skipped 3" in http.messages[0][1]


def test_empty_playlist_is_valid_but_hidden_contents_are_not(monkeypatch):
    result, _, _ = resolve(monkeypatch, [{"name": "Empty", "snapshot_id": "v1"}, page([]), {"snapshot_id": "v1"}])
    assert not result.entries
    with pytest.raises(SpotifyHTTPError):
        resolve(monkeypatch, [{"name": "Hidden", "snapshot_id": "v1"}, SpotifyHTTPError(403, "not owned")])
    with pytest.raises(ApiDeferred):
        resolve(monkeypatch, [{"name": "Hidden", "snapshot_id": "v1"}, {}])


@pytest.mark.parametrize("bad_page", [
    {"items": [], "offset": 0, "total": 1, "next": None},
    page([], total=1, next=f"https://api.spotify.com/v1/playlists/{PID}/items?offset=0"),
    page([{"item": item()}], total=2, next="https://evil.test/v1/playlists/x/items?offset=1"),
    page([{"item": item()}], total=2, next=f"https://api.spotify.com/v1/playlists/{PID}/items?offset=0"),
    page([{"item": item()}], total=2, next=f"https://api.spotify.com/v1/playlists/{PID}/items?offset=1&offset=2"),
    page([], total=10001), {"items": [], "total": 0},
])
def test_incomplete_repeated_and_untrusted_pages_rejected(monkeypatch, bad_page):
    with pytest.raises(ApiDeferred):
        resolve(monkeypatch, [{"name": "Playlist", "snapshot_id": "v1"}, bad_page])


def test_snapshot_change_rejects_entire_sync(monkeypatch):
    with pytest.raises(ApiDeferred, match="changed"):
        resolve(monkeypatch, [{"name": "Playlist", "snapshot_id": "v1"}, page([{"item": item()}]), {"snapshot_id": "v2"}])


def test_album_does_not_turn_release_date_into_discovery(monkeypatch):
    result, _, _ = resolve(monkeypatch, [{"name": "Album", "release_date": "1999-01-01"}, page([item()])], kind="album")
    assert result.entries[0].added_at is None
    assert not result.entries[0].downloadable


def test_direct_track_uses_observation_and_original_requested_id(monkeypatch):
    result, _, _ = resolve(monkeypatch, [item(identity="R" * 22, linked_from={"id": "S" * 22})], kind="track")
    assert result.entries[0].key == PID
    assert result.entries[0].basis == "first_seen"
    assert timestamp(result.entries[0].added_at)


def test_metadata_only_resync_preserves_duplicates_and_user_scope(db, monkeypatch):
    for user in ["admin", "guest"]:
        source = db.add_source(parse_source(f"spotify:playlist:{PID}"), user)
        result, _, _ = resolve(monkeypatch, [{"name": "Mix", "snapshot_id": "v1"}, page([
            {"item": item(), "added_at": None}, {"item": item(), "added_at": "2019-01-01T01:02:03Z"}]), {"snapshot_id": "v1"}])
        db.apply_snapshot(source, result); db.apply_snapshot(source, result)
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM memberships")["n"] == 4
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='track'")["n"] == 0
    assert all(t["discovered_at"] == "2019-01-01T01:02:03Z" for t in db.rows("SELECT * FROM tracks"))
    assert not db.dashboard_tracks("admin", 1, 50, "NEEDS_AUDIO")["tracks"][0]["origins"][0]["downloadable"]
