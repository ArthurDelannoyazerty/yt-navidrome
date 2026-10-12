"""Reviewed metadata correction endpoints; never trust browser-supplied tags."""
from __future__ import annotations

import json
import math
import time
from typing import Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from http_policy import ApiDeferred
from musicbrainz_search import MusicBrainzSearch
from source_metadata import correction_revision, snapshot


class CorrectionInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    mode: Literal["restore_source", "select_recording"]
    revision: str = Field(min_length=64, max_length=64)
    mbid: str | None = Field(default=None, max_length=36)
    release_id: str | None = Field(default=None, max_length=36)


def metadata_context(store, track_id, user):
    track = store.owned("tracks", track_id, user)
    origins = store.origins_for_track(track_id, user)
    pending = json.loads(track["pending_operation"] or "{}")
    origin_id = pending.get("origin_id") if track["operation_state"] == "NEEDS_APPROVAL" else track["current_origin_id"]
    origin = next((o for o in origins if o["id"] == origin_id), None)
    reports = []
    for entry in origins:
        report = store.one("SELECT report FROM identification_reports WHERE origin_id=?", (entry["id"],))
        if report:
            reports.append(json.loads(report["report"]))
    reports.sort(key=lambda r: r.get("at", ""), reverse=True)
    mutable = track["operation_state"] in {"IDLE", "FAILED", "NEEDS_APPROVAL"}
    return {"track_id": track_id, "user_id": user, "revision": correction_revision(track),
            "current_title": track["matched_title"] or track["title"], "current_mbid": track["mbid"],
            "state": track["operation_state"], "health": track["health"],
            "can_correct": bool(mutable and origin and (track["file_path"] or pending.get("origin_id"))),
            "source": snapshot(store, origin) if origin else None,
            "source_url": origin["url"] if origin else None, "origin_count": len(origins),
            "latest_identification": reports[0] if reports else None}


def install_metadata_routes(app, store):
    searcher = MusicBrainzSearch(store)
    app.state.musicbrainz_search = searcher

    @app.get("/api/musicbrainz/search")
    async def search(user_id: str, entity: Literal["recording", "artist"] = "recording",
                     title: str = Query("", max_length=200), artist: str = Query("", max_length=200),
                     artist_id: str | None = Query(None, max_length=36), page: int = Query(1, ge=1, le=100)):
        store.require_user(user_id)
        try:
            result = await searcher.search(entity, title, artist, artist_id, page)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except ApiDeferred as exc:
            delay = max(1, math.ceil((exc.retry_at or time.time() + 5) - time.time()))
            return JSONResponse({"error": str(exc), "retry_after": delay}, status_code=429,
                                headers={"Retry-After": str(delay), "Cache-Control": "no-store"})

    @app.get("/api/tracks/{track_id}/metadata-context")
    def context(track_id: str, user_id: str):
        return JSONResponse(metadata_context(store, track_id, user_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/tracks/{track_id}/metadata-correction")
    def correct(track_id: str, data: CorrectionInput):
        store.queue_track(track_id, data.user_id, data.model_dump(exclude={"user_id"}, exclude_none=True))
        return {"message": "Metadata correction queued. Existing audio remains active until success."}
