import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient

import spotify_auth as sa
from common import atomic_json
from main import create_app
from spotify_auth import SpotifyAuth, SpotifyClient, SpotifyHTTPError


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "a" * 32)
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8008/api/spotify/callback")


def token(**kwargs):
    return {"access_token": "ACCESS_SECRET", "refresh_token": "REFRESH_SECRET", "expires_in": 3600,
            "token_type": "Bearer", "scope": " ".join(sorted(sa.SCOPES)), **kwargs}


def begin(auth, user="admin"):
    url, cookie = auth.begin(user)
    return parse_qs(urlsplit(url).query), cookie


def authorize(auth, monkeypatch, user="admin", account="account-one"):
    query, cookie = begin(auth, user)
    monkeypatch.setattr(sa, "request_json", lambda *args, **kwargs: token() if args[1] == "POST" else {"id": account, "display_name": "Test account"})
    assert auth.complete(query["state"][0], cookie, "CODE_SECRET") == user


@pytest.mark.parametrize("uri", ["http://example.com/api/spotify/callback", "http://localhost:8008/api/spotify/callback",
    "https://user:pw@example.com/api/spotify/callback", "https://example.com/api/spotify/callback?to=other",
    "https://example.com/wrong", "ftp://example.com/api/spotify/callback", "https://example.com:bad/api/spotify/callback"])
def test_redirect_validation(monkeypatch, configured, uri):
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", uri)
    with pytest.raises(ValueError):
        sa.configuration()


def test_pkce_cookie_binding_one_use_and_private_files(db, configured, monkeypatch):
    auth = SpotifyAuth(db)
    query, cookie = begin(auth)
    assert query["code_challenge_method"] == ["S256"]
    assert set(query["scope"][0].split()) == sa.SCOPES
    assert "client_secret" not in query and "verifier" not in query
    with pytest.raises(ValueError, match="different browser"):
        auth.complete(query["state"][0], "wrong", "code")
    calls = []
    def exchange(http, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return token() if method == "POST" else {"id": "test-account", "display_name": "Test"}
    monkeypatch.setattr(sa, "request_json", exchange)
    assert auth.complete(query["state"][0], cookie, "code") == "admin"
    assert calls[0][2]["data"]["code_verifier"]
    assert auth._path("admin").stat().st_mode & 0o777 == 0o600
    assert auth.root.stat().st_mode & 0o777 == 0o700
    assert auth.status("admin")["connected"]
    assert not auth.status("guest")["connected"]
    assert "SECRET" not in json.dumps(auth.status("admin"))
    with pytest.raises(ValueError, match="already been used"):
        auth.complete(query["state"][0], cookie, "code")


def test_expired_and_denied_grants_do_not_replace_connection(db, configured, monkeypatch):
    auth = SpotifyAuth(db)
    authorize(auth, monkeypatch)
    before = auth._path("admin").read_bytes()
    query, cookie = begin(auth)
    with pytest.raises(ValueError, match="not granted"):
        auth.complete(query["state"][0], cookie, "", denied=True)
    assert auth._path("admin").read_bytes() == before
    query, cookie = begin(auth)
    pending = auth.root / "pending" / (sa.digest(query["state"][0]) + ".json")
    data = json.loads(pending.read_text()); data["expires_at"] = 0; atomic_json(pending, data)
    with pytest.raises(ValueError, match="expired"):
        auth.complete(query["state"][0], cookie, "code")


def test_refresh_preserves_unrotated_refresh_token(db, configured, monkeypatch):
    auth = SpotifyAuth(db)
    authorize(auth, monkeypatch)
    current = auth._read("admin"); current["expires_at"] = 0; atomic_json(auth._path("admin"), current)
    replies = token(access_token="NEW_ACCESS"); replies.pop("refresh_token"); replies.pop("scope")
    calls = []
    def exchange(*args, **kwargs):
        calls.append(kwargs["data"])
        return replies
    monkeypatch.setattr(sa, "request_json", exchange)
    assert auth.access_token("admin", sa.HttpPolicy(db)) == "NEW_ACCESS"
    assert auth._read("admin")["refresh_token"] == "REFRESH_SECRET"
    assert auth.access_token("admin", sa.HttpPolicy(db)) == "NEW_ACCESS"
    assert len(calls) == 1


def test_account_switch_requires_explicit_disconnect(db, configured, monkeypatch):
    auth = SpotifyAuth(db); authorize(auth, monkeypatch)
    with pytest.raises(ValueError, match="another Spotify account"):
        authorize(auth, monkeypatch, account="another")
    auth.disconnect("admin")
    authorize(auth, monkeypatch, account="another")
    assert auth.status("admin")["account_id"] == "another"


def test_disconnect_cancels_pending_callback(db, configured, monkeypatch):
    auth = SpotifyAuth(db); query, cookie = begin(auth)
    auth.disconnect("admin")
    with pytest.raises(ValueError, match="expired"):
        auth.complete(query["state"][0], cookie, "code")


@pytest.mark.parametrize("bad", [{"expires_in": "nan"}, {"expires_in": -1}, {"access_token": ""},
    {"scope": None}, {"scope": "user-read-email"}, {"token_type": "Basic"}])
def test_rejects_malformed_or_inadequate_grants(bad):
    with pytest.raises(ValueError):
        SpotifyAuth._validated(token(**bad), "client")


def test_transport_never_follows_redirects_or_leaks_error_body(db):
    class Policy:
        def send(self, original, session, prepared, **kwargs):
            assert kwargs["allow_redirects"] is False
            assert kwargs["timeout"] == (10, 30)
            response = requests.Response(); response.status_code = 403
            response._content = b'{"error": "ACCESS_SECRET REFRESH_SECRET"}'; response._content_consumed = True
            return response
    with pytest.raises(SpotifyHTTPError) as caught:
        sa.request_json(Policy(), "GET", "https://api.spotify.com/v1/me", headers={"Authorization": "Bearer ACCESS_SECRET"})
    assert caught.value.status == 403 and "SECRET" not in str(caught.value)
    for endpoint in ["https://evil.test/v1/me", "https://api.spotify.com.evil.test/v1/me", "http://api.spotify.com/v1/me", "https://api.spotify.com/v1/me?token=secret"]:
        with pytest.raises(ValueError, match="untrusted"):
            sa.request_json(Policy(), "GET", endpoint)


def test_client_retries_401_once_with_refreshed_access(db, configured, monkeypatch):
    auth = SpotifyAuth(db); authorize(auth, monkeypatch)
    calls = []
    def exchange(http, method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            return token(access_token="ROTATED")
        if kwargs["headers"]["Authorization"] != "Bearer ROTATED":
            raise SpotifyHTTPError(401, "expired")
        return {"ok": True}
    monkeypatch.setattr(sa, "request_json", exchange)
    assert SpotifyClient("admin", sa.HttpPolicy(db)).get("me")["ok"]
    assert calls == ["GET", "POST", "GET"]


def test_oauth_routes_secure_cookie_and_no_token_exposure(db, configured, monkeypatch):
    with TestClient(create_app(db, start_workers=False), base_url="http://127.0.0.1:8008") as client:
        response = client.post("/api/spotify/authorize", json={"user_id": "admin"})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=lax" in response.headers["set-cookie"]
        state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
        monkeypatch.setattr(sa, "request_json", lambda *args, **kwargs: token() if args[1] == "POST" else {"id": "account"})
        response = client.get("/api/spotify/callback", params={"state": state, "code": "code"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/?spotify=connected&library=admin"
        assert response.headers["cache-control"] == "no-store"
        status = client.get("/api/spotify/status?user_id=admin")
        assert status.json()["connected"] and "SECRET" not in status.text
        assert not client.get("/api/spotify/status?user_id=guest").json()["connected"]
        assert client.delete("/api/spotify/connection?user_id=admin").status_code == 200
        assert not client.get("/api/spotify/status?user_id=admin").json()["connected"]


def test_configuration_missing_does_not_break_youtube_or_status(db, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    with TestClient(create_app(db, start_workers=False)) as client:
        result = client.get("/api/spotify/status?user_id=admin").json()
        assert result["configured"] is False and result["configuration_error"]
        assert client.get("/healthz").status_code == 200


def test_modern_stable_account_id_survives_spotify_user_id_change(db, configured, monkeypatch):
    auth = SpotifyAuth(db)
    def connect(raw_id):
        query, cookie = begin(auth)
        monkeypatch.setattr(sa, "request_json", lambda *args, **kwargs: token() if args[1] == "POST" else {"id": raw_id, "account_id": "immutable-account"})
        auth.complete(query["state"][0], cookie, "code")
    connect("old-id"); connect("new-id")
    assert auth.status("admin")["account_id"] == "immutable-account"
    assert auth.status("admin")["spotify_user_id"] == "new-id"
