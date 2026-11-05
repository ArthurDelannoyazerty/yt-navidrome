"""Explicitly scoped review queues, selected approvals, and local audio previews."""
from pathlib import Path

from fastapi import Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from library_query import track_where

MAX_REVIEW_TRACKS = 10_000


class ReviewedChoice(BaseModel):
    track_id: str = Field(min_length=1, max_length=64)
    index: int = Field(strict=True, ge=0)
    approval_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelectedApproval(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    items: list[ReviewedChoice] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_tracks(self):
        ids = [item.track_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Select each track only once")
        return self


def review_queue(store, user, status="NEEDS_APPROVAL", query="", sort="updated"):
    store.require_user(user)
    where, params = track_where(user, status, query)
    if sort not in {"updated", "artist"}:
        raise ValueError("Unknown review order")
    eligible = (
        "t.operation_state IN ('IDLE','FAILED','STOPPED','NEEDS_APPROVAL') "
        "AND (t.health='AVAILABLE' OR "
        "(t.operation_state='NEEDS_APPROVAL' AND t.temp_path IS NOT NULL)) "
        "AND EXISTS(SELECT 1 FROM track_origins o WHERE o.track_id=t.id) "
        "AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.kind='track' AND j.target=t.id "
        "AND j.state IN ('PENDING','RUNNING'))"
    )
    order = (
        "COALESCE(NULLIF(t.matched_title,''),t.title) COLLATE NOCASE,t.id"
        if sort == "artist" else "t.updated_at DESC,t.id"
    )
    with store.db() as con:
        # Counts and IDs describe one snapshot even while the worker progresses.
        con.execute("BEGIN")
        matching = con.execute(
            f"SELECT COUNT(*) FROM tracks t WHERE {where}", params
        ).fetchone()[0]
        total = con.execute(
            f"SELECT COUNT(*) FROM tracks t WHERE {where} AND {eligible}", params
        ).fetchone()[0]
        tracks = [
            dict(row) for row in con.execute(
                f"""SELECT t.id,COALESCE(NULLIF(t.matched_title,''),t.title) AS title,
                    t.operation_state,t.health FROM tracks t
                    WHERE {where} AND {eligible} ORDER BY {order} LIMIT ?""",
                (*params, MAX_REVIEW_TRACKS),
            )
        ]
    return {
        "ids": [track["id"] for track in tracks], "tracks": tracks,
        "total": total, "matching_total": matching, "excluded": matching - total,
        "truncated": total > MAX_REVIEW_TRACKS, "limit": MAX_REVIEW_TRACKS,
        "total_scope": "filtered_reviewable", "sort": sort,
    }


def approve_selected(store, data):
    store.require_user(data.user_id)
    ids = [item.track_id for item in data.items]
    marks = ",".join("?" for _ in ids)
    owned = store.rows(
        f"SELECT id FROM tracks WHERE user_id=? AND id IN ({marks})",
        (data.user_id, *ids),
    )
    if len(owned) != len(ids):
        # Validate all ownership before any partial batch can be queued.
        raise LookupError("One or more selected tracks are no longer available")
    results = []
    for item in data.items:
        try:
            store.queue_track(
                item.track_id, data.user_id,
                {"mode": "approve", "index": item.index,
                 "approval_token": item.approval_token},
            )
            results.append({"track_id": item.track_id, "status": "queued",
                            "message": "Selected metadata queued"})
        except (ValueError, LookupError) as exc:
            results.append({"track_id": item.track_id, "status": "skipped",
                            "message": str(exc)})
    queued = sum(item["status"] == "queued" for item in results)
    skipped = len(results) - queued
    return {
        "queued": queued, "skipped": skipped, "results": results,
        "scope": "selected_tracks",
        "message": f"Queued {queued} selected track(s); {skipped} need another review",
    }


def install_review_routes(app, store):
    @app.get("/api/review/queue")
    def get_review_queue(
        response: Response, user_id: str, status: str = "NEEDS_APPROVAL",
        q: str = Query("", max_length=200), sort: str = "updated",
    ):
        response.headers["Cache-Control"] = "no-store"
        return review_queue(store, user_id, status, q, sort)

    @app.post("/api/review/approve")
    def post_selected_approval(data: SelectedApproval):
        return approve_selected(store, data)

    @app.get("/api/tracks/{track_id}/audio")
    def preview_audio(track_id: str, user_id: str):
        from common import LIBRARY

        track = store.owned("tracks", track_id, user_id)
        asset = store.current_asset(track_id)
        if (
            not asset or track["health"] != "AVAILABLE"
            or asset["id"] != track["current_asset_id"]
        ):
            raise LookupError("No playable local audio is available")
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to((LIBRARY / user_id).resolve()) or not path.is_file():
            raise LookupError("No playable local audio is available")
        return FileResponse(
            path, media_type="audio/ogg", headers={"Cache-Control": "no-store"}
        )
