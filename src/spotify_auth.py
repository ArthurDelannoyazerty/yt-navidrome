"""Read-only Spotify OAuth/HTTP boundary. Tokens never leave the server.

PKCE needs a client ID, not a client secret. Per-library credentials are stored
outside SQLite in owner-only files; protect the persistent state and its backups.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from common import atomic_json, user_name
from http_policy import ApiDeferred, HttpPolicy

SCOPES = frozenset({"playlist-read-private", "playlist-read-collaborative"})
CALLBACK_PATH = "/api/spotify/callback"
_LOCK = threading.RLock()


class SpotifyAccessLogFilter(logging.Filter):
    """Keep one-use OAuth codes and state out of Uvicorn access logs."""

    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split("?", 1)[0] == CALLBACK_PATH:
                args[2] = CALLBACK_PATH + "?[REDACTED]"
                record.args = tuple(args)
        return True


def install_access_log_redaction():
    # The filter has no app/account state and can safely serve multiple app
    # factories. Install once, without replacing existing logging configuration.
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(value, SpotifyAccessLogFilter) for value in logger.filters):
        logger.addFilter(SpotifyAccessLogFilter())


def configuration():
    client = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    redirect = os.getenv("SPOTIFY_REDIRECT_URI", "").strip()
    if not client or not redirect:
        raise ValueError("Set SPOTIFY_CLIENT_ID and SPOTIFY_REDIRECT_URI, then restart to connect Spotify")
    if not re.fullmatch(r"[A-Za-z0-9]{1,128}", client):
        raise ValueError("Invalid SPOTIFY_CLIENT_ID")
    parts = urlsplit(redirect)
    try:
        loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (not parts.hostname or parts.hostname == "localhost" or parts.username or parts.password
            or parts.fragment or parts.query or parts.path != CALLBACK_PATH
            or parts.scheme not in {"http", "https"}
            or (parts.scheme != "https" and not loopback)):
        raise ValueError("SPOTIFY_REDIRECT_URI must use HTTPS (or a loopback IP) and end with /api/spotify/callback")
    _ = parts.port  # Reject malformed/out-of-range ports before issuing a grant.
    return client, redirect


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class SpotifyAuth:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / "spotify"

    def _directory(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        return self.root

    def _path(self, user):
        return self.root / (user_name(user) + ".json")

    def _read(self, user):
        try:
            value = json.loads(self._path(user).read_text())
            if not isinstance(value, dict):
                raise ValueError("Invalid credential data")
            return value
        except FileNotFoundError:
            return None
        except (ValueError, OSError):
            raise ValueError("Spotify credentials could not be read; disconnect and reconnect this library") from None

    def status(self, user):
        self.store.require_user(user)
        try:
            client, redirect = configuration()
            error = None
        except ValueError as exc:
            client, redirect, error = None, None, str(exc)
        with _LOCK:
            try:
                token = self._read(user) or {}
            except ValueError as exc:
                token, error = {}, str(exc)
        connected = bool(client and token.get("client_id") == client and token.get("refresh_token"))
        return {"configured": bool(client), "connected": connected,
                "account_id": token.get("account_id") if connected else None,
                "spotify_user_id": token.get("spotify_user_id") if connected else None,
                "display_name": token.get("display_name") if connected else None,
                "redirect_uri": redirect, "configuration_error": error}

    def begin(self, user):
        self.store.require_user(user)
        client, redirect = configuration()
        state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with _LOCK:
            pending = self._directory() / "pending"
            pending.mkdir(mode=0o700, exist_ok=True)
            pending.chmod(0o700)
            # Expired attempts cannot accumulate indefinitely on an admin server.
            for path in pending.glob("*.json"):
                if path.stat().st_mtime < time.time() - 600:
                    path.unlink(missing_ok=True)
            atomic_json(pending / (digest(state) + ".json"), {
                "user": user, "cookie_hash": digest(cookie), "verifier": verifier,
                "expires_at": time.time() + 600, "client_id": client, "redirect_uri": redirect,
            })
        url = "https://accounts.spotify.com/authorize?" + urlencode({
            "client_id": client, "response_type": "code", "redirect_uri": redirect,
            "state": state, "scope": " ".join(sorted(SCOPES)),
            "code_challenge_method": "S256", "code_challenge": challenge,
            "show_dialog": "true",
        })
        return url, cookie

    def complete(self, state, cookie, code, denied=False):
        if not state or not cookie or len(state) > 256 or len(cookie) > 256:
            raise ValueError("Spotify authorization expired or did not start in this browser")
        path = self.root / "pending" / (digest(state) + ".json")
        with _LOCK:
            try:
                pending = json.loads(path.read_text())
            except (OSError, ValueError):
                raise ValueError("Spotify authorization expired or has already been used") from None
            if not secrets.compare_digest(pending["cookie_hash"], digest(cookie)):
                raise ValueError("Spotify authorization belongs to a different browser")
            path.unlink(missing_ok=True)  # One-use, including denial and failed exchange.
            client, redirect = configuration()
            if (pending["expires_at"] <= time.time() or pending["client_id"] != client
                    or pending["redirect_uri"] != redirect):
                raise ValueError("Spotify authorization expired; connect again")
            user = self.store.require_user(pending["user"])
            if denied or not code:
                raise ValueError("Spotify access was not granted; the existing connection was not changed")
            http = HttpPolicy(self.store)
            raw = request_json(http, "POST", "https://accounts.spotify.com/api/token", data={
                "grant_type": "authorization_code", "client_id": client, "code": code,
                "redirect_uri": redirect, "code_verifier": pending["verifier"],
            })
            token = self._validated(raw, client)
            profile = request_json(http, "GET", "https://api.spotify.com/v1/me",
                                   headers={"Authorization": "Bearer " + token["access_token"]})
            if not isinstance(profile.get("id"), str) or not profile["id"]:
                raise ValueError("Spotify did not return an account identity")
            old = self._read(user)
            account_id = profile.get("account_id") or profile["id"]
            if old and old.get("account_id") != account_id and old.get("spotify_user_id") != profile["id"]:
                raise ValueError("This library is connected to another Spotify account; disconnect it explicitly first")
            token.update(account_id=account_id, spotify_user_id=profile["id"],
                         display_name=profile.get("display_name") or profile["id"])
            self._directory()
            atomic_json(self._path(user), token)
        return user

    @staticmethod
    def _validated(raw, client, previous=None):
        previous = previous or {}
        access = raw.get("access_token")
        refresh = raw.get("refresh_token") or previous.get("refresh_token")
        scope = raw.get("scope") if "scope" in raw else previous.get("scope", "")
        scopes = set(scope.split()) if isinstance(scope, str) else set()
        try:
            expires = float(raw.get("expires_in", 0))
        except (TypeError, ValueError):
            expires = 0
        if (not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh
                or not math.isfinite(expires) or expires <= 0
                or str(raw.get("token_type", "")).lower() != "bearer" or not SCOPES <= scopes):
            raise ValueError("Spotify did not grant the required read-only access; reconnect this library")
        return {**previous, "access_token": access, "refresh_token": refresh,
                "expires_at": time.time() + expires, "scope": " ".join(sorted(scopes)), "client_id": client}

    def access_token(self, user, http, rejected=None):
        self.store.require_user(user)
        client, _ = configuration()
        with _LOCK:
            token = self._read(user)
            if not token or token.get("client_id") != client:
                raise ValueError("Connect Spotify for this library in Sources before syncing")
            if token.get("expires_at", 0) > time.time() + 60 and token.get("access_token") != rejected:
                return token["access_token"]
            try:
                raw = request_json(http, "POST", "https://accounts.spotify.com/api/token", data={
                    "grant_type": "refresh_token", "client_id": client, "refresh_token": token["refresh_token"],
                })
            except SpotifyHTTPError as exc:
                if exc.status in {400, 401}:
                    # A revoked/expired refresh grant cannot recover by retrying.
                    # Leave other users and all imported sources/music untouched.
                    self._path(user).unlink(missing_ok=True)
                raise
            token = self._validated(raw, client, token)
            atomic_json(self._path(user), token)
            return token["access_token"]

    def disconnect(self, user):
        self.store.require_user(user)
        with _LOCK:
            self._path(user).unlink(missing_ok=True)
            pending = self.root / "pending"
            for path in pending.glob("*.json"):
                try:
                    if json.loads(path.read_text()).get("user") == user:
                        path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    continue


class SpotifyHTTPError(ValueError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def request_json(http, method, url, *, headers=None, params=None, data=None):
    """Known endpoints only, shared cooldowns, no redirects or response-body errors."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.username or parts.password or parts.port is not None
            or parts.query or parts.fragment
            or not ((parts.netloc == "api.spotify.com" and parts.path.startswith("/v1/"))
                    or (parts.netloc == "accounts.spotify.com" and parts.path == "/api/token"))):
        raise ValueError("Refusing an untrusted Spotify API endpoint")
    with requests.Session() as session:
        prepared = session.prepare_request(requests.Request(method, url, headers=headers, params=params, data=data))
        response = http.send(requests.sessions.Session.send, session, prepared,
                             timeout=(10, 30), allow_redirects=False)
        try:
            status = response.status_code
            if status == 401:
                raise SpotifyHTTPError(status, "Spotify authorization expired or was revoked; reconnect this library")
            if status == 403:
                raise SpotifyHTTPError(status, "Spotify denied access. Use a playlist you own or collaborate on; check app allowlist, read scopes and Premium eligibility")
            if status == 404:
                raise SpotifyHTTPError(status, "Spotify source is unavailable or not accessible to the connected account")
            if not 200 <= status < 300:
                raise SpotifyHTTPError(status, f"Spotify request rejected (HTTP {status}); check the app configuration and reconnect")
            try:
                value = response.json()
            except ValueError:
                raise ApiDeferred("Spotify returned invalid JSON; no partial snapshot was applied") from None
            if not isinstance(value, dict):
                raise ApiDeferred("Spotify returned an unexpected response; no partial snapshot was applied")
            return value
        finally:
            response.close()


class SpotifyClient:
    def __init__(self, user, http):
        self.user, self.http = user, http
        self.auth = SpotifyAuth(http.store)

    def get(self, path, **params):
        if not re.fullmatch(r"[A-Za-z0-9/_-]+", path) or path.startswith("/"):
            raise ValueError("Invalid Spotify resource path")
        token = self.auth.access_token(self.user, self.http)
        for attempt in range(2):
            try:
                return request_json(self.http, "GET", "https://api.spotify.com/v1/" + path,
                                    params=params, headers={"Authorization": "Bearer " + token})
            except SpotifyHTTPError as exc:
                if exc.status != 401 or attempt:
                    raise
                token = self.auth.access_token(self.user, self.http, rejected=token)
        raise AssertionError("Unreachable")
