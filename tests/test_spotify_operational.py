"""Operational Spotify regressions; credentials and responses are synthetic."""
import logging
import time

import pytest
import requests

from common import atomic_json
from http_policy import ApiDeferred, HttpPolicy
from main import create_app
import spotify_auth
from spotify_auth import SCOPES, SpotifyAccessLogFilter, SpotifyAuth, SpotifyHTTPError


@pytest.fixture
def auth(db, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "fixtureclient")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8008/api/spotify/callback")
    auth = SpotifyAuth(db)
    auth._directory()
    for user in ("admin", "guest"):
        atomic_json(auth._path(user), {
            "client_id": "fixtureclient", "access_token": "synthetic-access",
            "refresh_token": "synthetic-refresh", "scope": " ".join(sorted(SCOPES)),
            "expires_at": time.time() - 1,
        })
    return auth


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_refresh_grant_disconnects_only_affected_library(auth, db, monkeypatch, status):
    def rejected(*args, **kwargs):
        raise SpotifyHTTPError(status, "Spotify authorization rejected; reconnect")
    monkeypatch.setattr(spotify_auth, "request_json", rejected)
    with pytest.raises(SpotifyHTTPError):
        auth.access_token("admin", HttpPolicy(db))
    assert not auth.status("admin")["connected"]
    assert not auth._path("admin").exists()
    assert auth.status("guest")["connected"]


def test_transient_refresh_error_retains_credentials(auth, db, monkeypatch):
    before = auth._path("admin").read_bytes()
    def unavailable(*args, **kwargs):
        raise ApiDeferred("Spotify temporarily unavailable")
    monkeypatch.setattr(spotify_auth, "request_json", unavailable)
    with pytest.raises(ApiDeferred):
        auth.access_token("admin", HttpPolicy(db))
    assert auth._path("admin").read_bytes() == before
    assert auth.status("admin")["connected"]


def test_callback_access_log_redacts_code_and_state_not_other_paths():
    args = ("127.0.0.1", "GET", "/api/spotify/callback?code=private-code&state=private-state", "1.1", 303)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                               '%s - "%s %s HTTP/%s" %d', args, None)
    assert SpotifyAccessLogFilter().filter(record)
    assert "private-code" not in record.getMessage()
    assert "private-state" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
    record.args = ("127.0.0.1", "GET", "/api/tracks?page=2", "1.1", 200)
    assert SpotifyAccessLogFilter().filter(record)
    assert "/api/tracks?page=2" in record.getMessage()


def test_access_log_filter_is_installed_once_across_app_factories(db):
    create_app(db, start_workers=False)
    create_app(db, start_workers=False)
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(value, SpotifyAccessLogFilter) for value in filters) == 1


@pytest.mark.parametrize("body", [
    b'{"error":{"status":429,"reason":"QUOTA_EXCEEDED"}}',
    b'{"status":429,"reason":"QUOTA_EXCEEDED"}',
])
def test_developer_quota_uses_shared_long_cooldown_not_immediate_retries(db, body):
    response = requests.Response()
    response.status_code = 429
    response._content = body
    response._content_consumed = True
    response.headers["Retry-After"] = "7200"
    calls = []
    def send(*args, **kwargs):
        calls.append(1)
        return response
    prepared = requests.Request("GET", "https://api.spotify.com/v1/me/playlists").prepare()
    policy = HttpPolicy(db)
    before = time.time()
    with requests.Session() as session:
        with pytest.raises(ApiDeferred, match="developer-account quota") as exc:
            policy.send(send, session, prepared)
        assert exc.value.retry_at >= before + 7200
        with pytest.raises(ApiDeferred, match="cooldown"):
            policy.send(send, session, prepared)
    assert calls == [1]
