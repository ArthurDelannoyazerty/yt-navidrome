"""Admin-facing Spotify connection and explicit library-link endpoints."""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urlencode, urlsplit

from fastapi import Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from http_policy import ApiDeferred, HttpPolicy
from spotify_auth import CALLBACK_PATH, SpotifyAuth, SpotifyClient, configuration, install_access_log_redaction
from spotify_library import cancel_waiting, preview, queue_exact, queue_link

COOKIE = "spotify_oauth"


class SpotifyInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    target_id: str | None = Field(default=None, max_length=64)
    audio_url: str | None = Field(default=None, max_length=2048)
    expected_mbid: str | None = Field(default=None, max_length=64)
    revision: str | None = Field(default=None, max_length=64)


def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def install_spotify_routes(app, store):
    install_access_log_redaction()
    auth = SpotifyAuth(store)

    @app.get("/api/spotify/status")
    def status(user_id: str):
        return private_response(JSONResponse(auth.status(user_id)))

    @app.post("/api/spotify/authorize")
    def authorize(data: SpotifyInput, request: Request):
        _, redirect = configuration()
        if request.headers.get("host") != urlsplit(redirect).netloc:
            raise ValueError("Open Music Ingestor at the host configured in SPOTIFY_REDIRECT_URI before connecting")
        url, cookie = auth.begin(data.user_id)
        response = private_response(JSONResponse({"url": url}))
        response.set_cookie(COOKIE, cookie, max_age=600, httponly=True, samesite="lax",
                            secure=redirect.startswith("https:"), path="/api/spotify")
        return response

    @app.get(CALLBACK_PATH)
    def callback(request: Request, state: str = Query("", max_length=256),
                 code: str = Query("", max_length=4096), error: str = Query("", max_length=128)):
        try:
            user = auth.complete(state, request.cookies.get(COOKIE), code, bool(error))
            response = RedirectResponse("/?" + urlencode({"spotify": "connected", "library": user}), status_code=303)
        except (ValueError, ApiDeferred) as exc:
            response = HTMLResponse(
                "<!doctype html><html lang='en'><meta charset='utf-8'><title>Spotify connection</title>"
                "<h1>Spotify connection was not completed</h1><p>" + html.escape(str(exc)) +
                "</p><p><a href='/'>Return to Music Ingestor</a> and connect again.</p></html>", status_code=400,
            )
        response.delete_cookie(COOKIE, path="/api/spotify")
        return private_response(response)

    @app.delete("/api/spotify/connection")
    def disconnect(user_id: str):
        auth.disconnect(user_id)
        return {"message": "Spotify credentials removed for this library. Imported music and links are retained. Remove app access in Spotify to revoke the grant."}

    @app.get("/api/spotify/playlists")
    def playlists(user_id: str, page: int = Query(1, ge=1, le=200)):
        store.require_user(user_id)
        data = SpotifyClient(user_id, HttpPolicy(store)).get("me/playlists", limit=50, offset=(page - 1) * 50)
        if not isinstance(data.get("items"), list) or type(data.get("total")) is not int:
            raise ValueError("Spotify did not return a playlist list")
        entries = []
        account = auth.status(user_id)["spotify_user_id"]
        for item in data["items"]:
            if not item or not re.fullmatch(r"[A-Za-z0-9]{22}", item.get("id", "")):
                continue
            entries.append({"id": item["id"], "title": item.get("name") or "Spotify playlist",
                            "url": "https://open.spotify.com/playlist/" + item["id"],
                            "owned": (item.get("owner") or {}).get("id") == account})
        return private_response(JSONResponse({"items": entries, "page": page, "total": data["total"], "limit": 50}))

    @app.get("/api/spotify/matches")
    def matches(user_id: str, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100)):
        return preview(store, user_id, page, limit)

    @app.post("/api/spotify/link-exact")
    def link_exact(data: SpotifyInput):
        if not data.revision:
            raise ValueError("Review the match preview first")
        return queue_exact(store, data.user_id, data.revision)

    @app.get("/api/spotify/candidates")
    def candidates(user_id: str, q: str = Query("", max_length=200), page: int = Query(1, ge=1)):
        data = store.dashboard_tracks(user_id, page, 50, "AVAILABLE", q)
        return {"items": [{"id": t["id"], "title": t["matched_title"], "mbid": t["mbid"]}
                          for t in data["tracks"] if t["mbid"] and t["operation_state"] == "IDLE"],
                "page": page, "total": data["total"], "limit": 50}

    @app.post("/api/spotify/entries/{track_id}/link")
    def link(track_id: str, data: SpotifyInput):
        if data.audio_url and not os.getenv("YT_API_KEY", "").strip():
            raise ValueError("Configure YT_API_KEY before requesting a YouTube audio origin")
        return queue_link(store, track_id, data.user_id, target_id=data.target_id, audio_url=data.audio_url,
                          expected={"mbid": data.expected_mbid} if data.expected_mbid else None)

    @app.post("/api/spotify/entries/{track_id}/cancel")
    def cancel(track_id: str, data: SpotifyInput):
        return cancel_waiting(store, track_id, data.user_id)
