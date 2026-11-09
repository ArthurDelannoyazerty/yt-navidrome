"""Reviewed metadata correction endpoints; never trust browser-supplied tags."""
from __future__ import annotations

import json
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import common
from common import utcnow
from mutagen.oggopus import OggOpus
from metadata_hints import alias_key, finite_number, search_hints
from http_policy import ApiDeferred
from musicbrainz_search import MusicBrainzSearch
from source_metadata import SOURCE_TAGS, artist_alias, correction_revision, identification_hints, snapshot


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


def review_updated_at(previous: str | None) -> str:
    """Every review decision advances the revision, even in the same clock tick."""
    now = datetime.now(UTC)
    try:
        old = datetime.fromisoformat(previous.replace("Z", "+00:00")) if previous else None
        if old and old.tzinfo is not None and now <= old:
            now = old + timedelta(microseconds=1)
    except (ValueError, AttributeError):
        pass
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def public_choices(track) -> list[dict]:
    """Indexes refer to durable choices; internal tags/fingerprints stay server-side."""
    fields = ("kind", "mbid", "title", "artist", "album", "similarity", "match_score",
              "description", "evidence", "asis", "manual")
    return [{"index": index, **{key: choice[key] for key in fields if key in choice}}
            for index, choice in enumerate(json.loads(track["choices"] or "[]"))]


def current_audio_metadata(track) -> dict:
    result = {"current_tags": {}, "current_duration_seconds": None, "current_tags_error": None}
    if track["health"] != "AVAILABLE" or not track["file_path"]:
        return result
    try:
        path = Path(track["file_path"]).resolve()
        library = (common.LIBRARY / track["user_id"]).resolve()
        if not path.is_relative_to(library) or not path.is_file():
            return {**result, "current_tags_error": "Current audio is unavailable in this library."}
        audio = OggOpus(path)
        result["current_tags"] = {
            key: [str(v)[:1024] for v in audio.get(key, [])[:16]]
            for key in SOURCE_TAGS if audio.get(key)
        }
        result["current_duration_seconds"] = finite_number(audio.info.length)
    except Exception:
        # Existing integrity checks diagnose unreadable files. A review dialog
        # still needs retained source information when current tags are unreadable.
        result["current_tags_error"] = "Current audio metadata could not be read."
    return result


def metadata_context(store, track_id, user):
    track = store.owned("tracks", track_id, user)
    origins = store.origins_for_track(track_id, user)
    origin = review_origin(track, origins)
    report_row = store.one("SELECT report FROM identification_reports WHERE origin_id=?", (origin["id"],)) if origin else None
    report = json.loads(report_row["report"]) if report_row else None
    mutable = track["operation_state"] in {"IDLE", "FAILED", "NEEDS_APPROVAL", "STOPPED"}
    pending = json.loads(track["pending_operation"] or "{}")
    source = snapshot(store, origin) if origin else None
    choices = public_choices(track)
    return {"track_id": track_id, "user_id": user, "revision": correction_revision(track),
            "current_title": track["matched_title"] or track["title"], "current_mbid": track["mbid"],
            "current_release_id": track["release_id"],
            "state": track["operation_state"], "health": track["health"],
            "can_correct": bool(mutable and origin and ((track["health"] == "AVAILABLE" and track["file_path"]) or (track["operation_state"] == "NEEDS_APPROVAL" and pending.get("origin_id")))),
            "source": source, "source_url": origin["url"] if origin else None,
            "source_title": origin["title"] if origin else track["title"],
            "source_channel": origin.get("channel") if origin else None,
            "origin_id": origin["id"] if origin else None, "origin_count": len(origins),
            "search_hints": identification_hints(store, origin) if origin else None,
            "approved_artist_alias": artist_alias(store, origin) if origin else None,
            "choices": choices, "candidates": choices,
            "approval_token": store.approval_token(track),
            "rejected_candidates": store.rows("SELECT mbid,created_at FROM origin_identity_rejections WHERE origin_id=? ORDER BY created_at DESC", (origin["id"],)) if origin else [],
            "candidate_duration_seconds": (report or {}).get("diagnostics", {}).get("search_seed", {}).get("length_seconds"),
            **current_audio_metadata(track), "latest_identification": report}


def checked_review(store, con, track_id, data):
    track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, data.user_id)).fetchone()
    if not track:
        raise LookupError("Track not found for this user")
    if data.revision != correction_revision(track):
        raise ValueError("This track changed; reopen the correction dialog and review it again")
    if track["operation_state"] not in {"IDLE", "FAILED", "NEEDS_APPROVAL", "STOPPED"} or con.execute(
        "SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track_id,)
    ).fetchone():
        raise ValueError("Wait for the active operation before changing review decisions")
    if track["operation_state"] == "STOPPED" and (track["health"] != "AVAILABLE" or not track["file_path"]):
        raise ValueError("Download audio before reviewing a stopped track")
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
            con.execute("UPDATE tracks SET updated_at=? WHERE id=?", (review_updated_at(track["updated_at"]), track_id))
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
            known = {c.get("mbid") for c in choices if not (c.get("asis") or c.get("kind") == "asis")}
            if report:
                known.update(c.get("mbid") for c in json.loads(report["report"]).get("candidates", []))
            known.add(track["mbid"])
            if data.reject:
                if identity not in known:
                    raise ValueError("Only a displayed suggestion or current identity can be rejected")
                con.execute("INSERT OR IGNORE INTO origin_identity_rejections VALUES (?,?,?)", (origin["id"], identity, utcnow()))
                choices = [c for c in choices if c.get("mbid") != identity or c.get("asis") or c.get("kind") == "asis"]
                con.execute("UPDATE tracks SET choices=?,selected=NULL,updated_at=? WHERE id=?",
                            (json.dumps(choices), review_updated_at(track["updated_at"]), track_id))
            else:
                con.execute("DELETE FROM origin_identity_rejections WHERE origin_id=? AND mbid=?", (origin["id"], identity))
                con.execute("UPDATE tracks SET updated_at=? WHERE id=?", (review_updated_at(track["updated_at"]), track_id))
        result = metadata_context(store, track_id, data.user_id)
        result["message"] = ("Suggestion rejected for this source. Current audio and identity are unchanged." if data.reject else
                             "Suggestion allowed again. Refresh suggestions to include it.")
        return result
