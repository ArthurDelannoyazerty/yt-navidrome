"""Reviewed metadata correction endpoints; never trust browser-supplied tags."""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common import utcnow
from metadata_hints import alias_key, search_hints
from http_policy import ApiDeferred
from musicbrainz_search import MusicBrainzSearch
from source_metadata import artist_alias, correction_revision, identification_hints, snapshot


class CorrectionInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    mode: Literal["restore_source", "select_recording"]
    revision: str = Field(min_length=64, max_length=64)
    mbid: str | None = Field(default=None, max_length=36)
    release_id: str | None = Field(default=None, max_length=36)


class ArtistAliasInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    revision: str = Field(min_length=64, max_length=64)
    artist_id: str | None = Field(default=None, max_length=36)
    forget: bool = False


class CandidateRejectionInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    revision: str = Field(min_length=64, max_length=64)
    mbid: str = Field(min_length=36, max_length=36)
    reject: bool = True


def review_origin(track, origins):
    pending = json.loads(track["pending_operation"] or "{}")
    origin_id = pending.get("origin_id") if track["operation_state"] == "NEEDS_APPROVAL" else track["current_origin_id"]
    return next((o for o in origins if o["id"] == origin_id), None)


def metadata_context(store, track_id, user):
    track = store.owned("tracks", track_id, user)
    origins = store.origins_for_track(track_id, user)
    origin = review_origin(track, origins)
    report_row = store.one("SELECT report FROM identification_reports WHERE origin_id=?", (origin["id"],)) if origin else None
    report = json.loads(report_row["report"]) if report_row else None
    mutable = track["operation_state"] in {"IDLE", "FAILED", "NEEDS_APPROVAL"}
    pending = json.loads(track["pending_operation"] or "{}")
    source = snapshot(store, origin) if origin else None
    return {"track_id": track_id, "user_id": user, "revision": correction_revision(track),
            "current_title": track["matched_title"] or track["title"], "current_mbid": track["mbid"],
            "state": track["operation_state"], "health": track["health"],
            "can_correct": bool(mutable and origin and (track["file_path"] or pending.get("origin_id"))),
            "source": source, "source_url": origin["url"] if origin else None,
            "origin_id": origin["id"] if origin else None, "origin_count": len(origins),
            "search_hints": identification_hints(store, origin) if origin else None,
            "approved_artist_alias": artist_alias(store, origin) if origin else None,
            "candidates": json.loads(track["choices"] or "[]"),
            "rejected_candidates": store.rows("SELECT mbid,created_at FROM origin_identity_rejections WHERE origin_id=? ORDER BY created_at DESC", (origin["id"],)) if origin else [],
            "current_duration_seconds": (report or {}).get("diagnostics", {}).get("search_seed", {}).get("length_seconds"),
            "latest_identification": report}


def checked_review(store, con, track_id, data):
    track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, data.user_id)).fetchone()
    if not track:
        raise LookupError("Track not found for this user")
    if data.revision != correction_revision(track):
        raise ValueError("This track changed; reopen the correction dialog and review it again")
    if track["operation_state"] not in {"IDLE", "FAILED", "NEEDS_APPROVAL"} or con.execute(
        "SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track_id,)
    ).fetchone():
        raise ValueError("Wait for the active operation before changing review decisions")
    origins = [dict(x) for x in con.execute("SELECT * FROM track_origins WHERE track_id=? AND user_id=?", (track_id, data.user_id))]
    origin = review_origin(track, origins)
    if not origin:
        raise ValueError("This track has no audio origin to review")
    return track, origin


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

    @app.post("/api/tracks/{track_id}/artist-alias")
    async def remember_artist(track_id: str, data: ArtistAliasInput):
        with store.db() as con:
            checked_review(store, con, track_id, data)
        try:
            artist = None if data.forget else await searcher.artist(data.artist_id)
        except ApiDeferred as exc:
            delay = max(1, math.ceil((exc.retry_at or time.time() + 5) - time.time()))
            return JSONResponse({"error": str(exc), "retry_after": delay}, status_code=429,
                                headers={"Retry-After": str(delay), "Cache-Control": "no-store"})
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track, origin = checked_review(store, con, track_id, data)
            from source_metadata import source_snapshot
            hint = search_hints(source_snapshot(con, origin), origin)
            alias = hint["artist"]
            if not alias:
                raise ValueError("No source artist hint is available to remember")
            if data.forget:
                con.execute("DELETE FROM metadata_artist_aliases WHERE user_id=? AND alias_key=?", (data.user_id, alias_key(alias)))
            else:
                con.execute("INSERT INTO metadata_artist_aliases VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(user_id,alias_key) DO UPDATE SET alias=excluded.alias,artist_id=excluded.artist_id,artist_name=excluded.artist_name,updated_at=excluded.updated_at",
                            (data.user_id, alias_key(alias), alias, artist["id"], artist["name"], utcnow()))
            con.execute("UPDATE tracks SET updated_at=? WHERE id=?", (utcnow(), track_id))
        result = metadata_context(store, track_id, data.user_id)
        result["message"] = "Artist alias forgotten." if data.forget else "Artist alias confirmed for this library. Existing tags and approvals are unchanged."
        return result

    @app.post("/api/tracks/{track_id}/reject-candidate")
    def reject_candidate(track_id: str, data: CandidateRejectionInput):
        identity = str(uuid.UUID(data.mbid))
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track, origin = checked_review(store, con, track_id, data)
            choices = json.loads(track["choices"] or "[]")
            report = con.execute("SELECT report FROM identification_reports WHERE origin_id=?", (origin["id"],)).fetchone()
            known = {c.get("mbid") for c in choices if not c.get("asis")}
            if report:
                known.update(c.get("mbid") for c in json.loads(report["report"]).get("candidates", []))
            known.add(track["mbid"])
            if data.reject:
                if identity not in known:
                    raise ValueError("Only a displayed suggestion or current identity can be rejected")
                con.execute("INSERT OR IGNORE INTO origin_identity_rejections VALUES (?,?,?)", (origin["id"], identity, utcnow()))
                choices = [c for c in choices if c.get("mbid") != identity or c.get("asis")]
                con.execute("UPDATE tracks SET choices=?,selected=NULL,updated_at=? WHERE id=?",
                            (json.dumps(choices), utcnow(), track_id))
            else:
                con.execute("DELETE FROM origin_identity_rejections WHERE origin_id=? AND mbid=?", (origin["id"], identity))
                con.execute("UPDATE tracks SET updated_at=? WHERE id=?", (utcnow(), track_id))
        result = metadata_context(store, track_id, data.user_id)
        result["message"] = ("Suggestion rejected for this source. Current audio and identity are unchanged." if data.reject else
                             "Suggestion allowed again. Refresh suggestions to include it.")
        return result
