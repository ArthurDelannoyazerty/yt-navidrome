"""Owned artwork gallery; GETs read local images only and never contact providers."""
from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from mutagen import MutagenError
from pydantic import BaseModel, Field

from artwork import Artwork, FRAMINGS, MAX_BYTES, decode, embedded
from artwork_jobs import queue, revision


class ArtworkInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: str | None = Field(default=None, max_length=64)
    framing: str = "original"
    refresh: bool = False


class LibraryInput(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)


def context(store, track_id, user):
    store.require_user(user)
    with store.db() as con:
        row = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)).fetchone()
        if not row:
            raise LookupError("Track no longer exists in this library")
        token, track = revision(con, row), dict(row)
    service = Artwork(store)
    pref = service.preference(track)
    origins = {o["id"]: o["url"] for o in store.origins_for_track(track_id, user)}
    items, seen = [], set()
    for candidate in service.candidates(track):
        selected = bool(pref and pref["candidate_id"] == candidate["id"])
        if candidate["digest"] in seen and not selected:
            continue
        seen.add(candidate["digest"])
        items.append({key: candidate[key] for key in ("id", "label", "source_type", "width", "height")} | {
            "selected": selected, "recommended_framing": "trim" if candidate["crop"] else "original",
            "source_url": origins.get(candidate["origin_id"]),
            "explanation": "Uniform border trimmed; original retained" if candidate["crop"] else "Original proportions; ranked by shape, resolution and sharpness",
            "image_url": f"/api/tracks/{track_id}/artwork/{candidate['id']}/image?" + urlencode({"user_id": user}),
        })
    return {"track_id": track_id, "title": track["matched_title"] or track["title"], "revision": token,
            "items": items, "preference": {k: pref[k] for k in ("mode", "candidate_id", "framing")} if pref else None,
            "can_edit": track["health"] == "AVAILABLE" and track["operation_state"] in {"IDLE", "NEEDS_APPROVAL", "STOPPED"},
            "working": track["operation_kind"] == "ARTWORK" and track["operation_state"] in {"QUEUED", "RUNNING", "DEFERRED"},
            "error": track["operation_error"] if track["operation_state"] == "FAILED" else None,
            "current_image_url": f"/api/tracks/{track_id}/artwork/current/image?" + urlencode({"user_id": user, "v": token})}


def install_artwork_routes(app, store):
    @app.get("/api/tracks/{track_id}/artwork")
    def get_artwork(track_id: str, user_id: str):
        return JSONResponse(context(store, track_id, user_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/tracks/{track_id}/artwork")
    def set_artwork(track_id: str, data: ArtworkInput):
        if not data.selection and not data.refresh:
            raise ValueError("Select an image or find covers first")
        return queue(store, track_id, data.user_id, data.revision, selection=data.selection,
                     framing=data.framing, refresh=data.refresh)

    @app.get("/api/tracks/{track_id}/artwork/{candidate_id}/image")
    def image(track_id: str, candidate_id: str, user_id: str, framing: str = "original"):
        if framing not in FRAMINGS:
            raise ValueError("Unknown image framing")
        track = store.owned("tracks", track_id, user_id)
        service = Artwork(store)
        if candidate_id == "current":
            from common import LIBRARY
            asset = store.current_asset(track_id)
            path = Path(asset["path"]).resolve() if asset else None
            if not path or not path.is_relative_to((LIBRARY / user_id).resolve()) or not path.is_file():
                raise LookupError("No local audio available")
            try:
                content = embedded(path)
                if not content:
                    return Response(status_code=204, headers={"Cache-Control": "no-store"})
                output = io.BytesIO(); decode(content).save(output, "JPEG", quality=92)
                content = output.getvalue()
            except (MutagenError, ValueError, OSError):
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
        else:
            content = service.render(service.owned(candidate_id, track), framing)
        return Response(content, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.post("/api/tracks/{track_id}/artwork/upload")
    async def upload(track_id: str, request: Request, user_id: str,
                     revision_token: str = Query(..., min_length=64, max_length=64),
                     filename: str = Query("Uploaded cover", max_length=200)):
        track = store.owned("tracks", track_id, user_id)
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "Use an image of at most 8 MiB")
            content.extend(chunk)
        with store.db() as con:
            current = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user_id)).fetchone()
            if not current or revision_token != revision(con, current):
                raise ValueError("Track changed; reopen artwork before uploading")
        candidate = Artwork(store).cache(track, bytes(content), "upload", filename)
        return {"candidate_id": candidate["id"], "message": "Image uploaded for preview. Choose Use this image to apply it."}

    @app.post("/api/artwork/fill-missing")
    def fill_missing(data: LibraryInput):
        from common import LIBRARY
        store.require_user(data.user_id)
        queued = skipped = 0
        for track in store.rows("SELECT * FROM tracks WHERE user_id=? AND health='AVAILABLE' AND operation_state='IDLE'", (data.user_id,)):
            try:
                asset = store.current_asset(track["id"])
                path = Path(asset["path"]).resolve() if asset else None
                if not path or not path.is_relative_to((LIBRARY / data.user_id).resolve()) or not path.is_file():
                    skipped += 1; continue
                if embedded(path):
                    continue
                with store.db() as con:
                    current = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track["id"], data.user_id)).fetchone()
                    if not current:
                        skipped += 1; continue
                    token = revision(con, current)
                queue(store, track["id"], data.user_id, token, refresh=True)
                queued += 1
            except (LookupError, ValueError, OSError, MutagenError):
                skipped += 1
        return {"queued": queued, "skipped": skipped, "message": f"Queued {queued} missing covers; {skipped} skipped. Existing images are retained."}
