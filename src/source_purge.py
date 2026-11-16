"""User-scoped, retryable source purge with shared-track preservation."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import common
from runtime import DeferredOperation


def _source(con, source_id: str, user: str, url: str | None = None) -> dict:
    row = con.execute(
        "SELECT * FROM sources WHERE id=? AND user_id=?", (source_id, user)
    ).fetchone()
    if not row or (url is not None and row["url"] != url):
        raise LookupError("Source URL not found for this library user")
    return dict(row)


def _plan(con, source_id: str, user: str) -> tuple[list[str], list[str]]:
    """Attribute tracks by durable source/origin history, not just current playlists.

    If another source has ever claimed any origin on the same logical track, or
    an independent discovery origin exists, retain the entire logical track.
    """
    tracks = con.execute(
        """SELECT DISTINCT t.id FROM source_origins so
           JOIN track_origins o ON o.id=so.origin_id
           JOIN tracks t ON t.id=o.track_id
           WHERE so.source_id=? AND o.user_id=? AND t.user_id=?
           ORDER BY t.id""",
        (source_id, user, user),
    ).fetchall()
    exclusive, shared = [], []
    for (track_id,) in tracks:
        another_source = con.execute(
            """SELECT 1 FROM source_origins so
               JOIN sources s ON s.id=so.source_id
               JOIN track_origins o ON o.id=so.origin_id
               WHERE o.track_id=? AND o.user_id=? AND s.user_id=?
                 AND s.id<>? LIMIT 1""",
            (track_id, user, user, source_id),
        ).fetchone()
        independent = con.execute(
            """SELECT 1 FROM track_origins o
               WHERE o.track_id=? AND o.user_id=? AND o.is_discovery=1
                 AND NOT EXISTS (
                   SELECT 1 FROM source_origins so WHERE so.origin_id=o.id
                 ) LIMIT 1""",
            (track_id, user),
        ).fetchone()
        (shared if another_source or independent else exclusive).append(track_id)
    return exclusive, shared


def preview(store, source_id: str, user: str) -> dict:
    store.require_user(user)
    with store.db() as con:
        source = _source(con, source_id, user)
        exclusive, shared = _plan(con, source_id, user)
    return {
        "source_id": source_id, "user_id": user, "source_url": source["url"],
        "exclusive_tracks": len(exclusive), "shared_tracks": len(shared),
    }


def queue(store, source_id: str, user: str, url: str) -> dict:
    """Disable monitoring immediately; place purge ahead of ordinary queued jobs."""
    store.require_user(user)
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        source = _source(con, source_id, user, url)
        if source["status"] == "PURGING":
            raise ValueError("This source is already being purged")
        con.execute(
            "UPDATE sources SET monitored=0,status='PURGING' WHERE id=? AND user_id=?",
            (source_id, user),
        )
        # A pending resync must not repopulate the source before its purge runs.
        con.execute(
            """UPDATE jobs SET state='CANCELLED',cancel_requested=1,
               error='Source purge requested',finished_at=?
               WHERE kind='sync' AND user_id=? AND target=? AND state='PENDING'""",
            (common.utcnow(), user, source_id),
        )
        if not store.enqueue("purge_source", source_id, user, {"url": url}, con=con):
            raise ValueError("This source already has an active purge")
        job = con.execute(
            """SELECT id FROM jobs WHERE kind='purge_source' AND user_id=?
               AND target=? AND state='PENDING'""", (user, source_id)
        ).fetchone()
        return {"message": "Source purge queued; shared music will be retained",
                "job_id": job["id"]}


def _inside_user(path: str, user: str) -> Path:
    resolved = Path(path).resolve()
    if not resolved.is_relative_to((common.LIBRARY / user).resolve()):
        raise ValueError("Refusing to remove a file outside the selected user's library")
    return resolved


def commit(store, source_id: str, user: str, url: str, operation_id: str) -> dict:
    """Delete source-owned database rows transactionally, retaining cleanup paths.

    The durable receipt lets the worker retry beets/file cleanup after a crash.
    """
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT user_id,result FROM operation_receipts WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing:
            if existing["user_id"] != user:
                raise LookupError("Purge receipt belongs to another user")
            return json.loads(existing["result"])
        source = _source(con, source_id, user, url)
        if con.execute(
            """SELECT 1 FROM jobs WHERE kind='sync' AND target=? AND user_id=?
               AND state='RUNNING'""", (source_id, user)
        ).fetchone():
            raise ValueError("Wait for the running source sync before purging")
        exclusive, shared = _plan(con, source_id, user)
        deleted = []
        for track_id in exclusive:
            if con.execute(
                """SELECT 1 FROM jobs WHERE kind='track' AND target=?
                   AND user_id=? AND state='RUNNING'""", (track_id, user)
            ).fetchone():
                raise ValueError("Wait for a running track operation before purging")
            track = con.execute(
                "SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            ).fetchone()
            paths = {row["path"] for row in con.execute(
                "SELECT path FROM assets WHERE track_id=?", (track_id,)
            )}
            if track["file_path"]:
                paths.add(track["file_path"])
            # Validate every path before committing *any* destructive DB work.
            for path in paths:
                _inside_user(path, user)
            deleted.append({"track": dict(track), "paths": sorted(paths)})

        playlist = source["playlist_path"]
        if playlist:
            folder = (common.LIBRARY / user / "000000-playlists").resolve()
            if Path(playlist).resolve().parent != folder:
                raise ValueError("Refusing to unlink a playlist outside this user's playlist directory")

        # This cascades only the chosen source's memberships and attribution.
        con.execute(
            "DELETE FROM sources WHERE id=? AND user_id=?", (source_id, user)
        )
        removed_origins = 0
        for track_id in shared:
            # Preserve shared tracks and their current audio's provenance.
            track = con.execute(
                "SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            ).fetchone()
            if not track or track["operation_state"] != "IDLE":
                continue
            origins = con.execute(
                """SELECT o.id FROM track_origins o
                   WHERE o.track_id=? AND o.user_id=?
                     AND NOT EXISTS (SELECT 1 FROM source_origins so WHERE so.origin_id=o.id)
                     AND NOT EXISTS (SELECT 1 FROM memberships m WHERE m.origin_id=o.id)
                     AND NOT EXISTS (SELECT 1 FROM assets a WHERE a.origin_id=o.id)
                     AND NOT EXISTS (SELECT 1 FROM spotify_audio_links l
                                     WHERE l.spotify_origin_id=o.id OR l.audio_origin_id=o.id)""",
                (track_id, user),
            ).fetchall()
            for origin in origins:
                if origin["id"] == track["current_origin_id"]:
                    continue
                con.execute(
                    "DELETE FROM track_origins WHERE id=? AND track_id=? AND user_id=?",
                    (origin["id"], track_id, user),
                )
                removed_origins += 1
            store._recompute_discovery_locked(con, track_id)

        for item in deleted:
            track_id = item["track"]["id"]
            con.execute(
                "DELETE FROM jobs WHERE kind='track' AND target=? AND user_id=?",
                (track_id, user),
            )
            con.execute(
                "DELETE FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            )

        result = {
            "kind": "PURGED_SOURCE", "source_id": source_id,
            "source_url": url, "playlist_path": playlist,
            "deleted": deleted, "exclusive_tracks": len(deleted),
            "shared_tracks": len(shared), "removed_origins": removed_origins,
        }
        store._record_receipt_locked(
            con, operation_id, user, None, None, "purge_source", result
        )
        return result


async def _finish(pipeline, job: dict, receipt: dict) -> None:
    """Retryable, idempotent cleanup of exclusively owned files and beets rows."""
    if receipt["finalized_at"]:
        return
    store = pipeline.store
    user = receipt["user_id"]
    result = receipt["result"]
    stage_root = (common.STATE / "staging").resolve()
    try:
        for item in result["deleted"]:
            track = item["track"]
            track_id = track["id"]
            # Only UUID-named staging directories under application state.
            track_stage = (stage_root / track_id).resolve()
            cleanup_stage = (stage_root / ("purge-" + track_id)).resolve()
            if not track_stage.is_relative_to(stage_root) or not cleanup_stage.is_relative_to(stage_root):
                raise ValueError("Invalid staging directory for source purge")
            if track.get("beets_id") is not None or track.get("file_path"):
                cleanup_stage.mkdir(parents=True, exist_ok=True)
                await pipeline.bridge(job, track, cleanup_stage, "forget")
            for recorded in item["paths"]:
                path = _inside_user(recorded, user)
                # Never unlink an audio file now referenced by another track.
                if not store.one("SELECT 1 FROM assets WHERE path=? LIMIT 1", (str(path),)):
                    path.unlink(missing_ok=True)
            shutil.rmtree(track_stage, ignore_errors=True)
            shutil.rmtree(cleanup_stage, ignore_errors=True)
        playlist = result.get("playlist_path")
        if playlist:
            folder = (common.LIBRARY / user / "000000-playlists").resolve()
            path = Path(playlist).resolve()
            if path.parent != folder:
                raise ValueError("Refusing to unlink another user's playlist")
            path.unlink(missing_ok=True)
        store.finalize_receipt(receipt["operation_id"])
    except Exception as exc:
        raise DeferredOperation(f"Source purge committed; file cleanup will retry: {exc}") from exc


async def process(pipeline, job: dict) -> None:
    user, source_id = job["user_id"], job["target"]
    operation_id = "purge-source:" + source_id
    receipt = pipeline.store.operation_receipt(operation_id)
    if receipt is None:
        url = json.loads(job["payload"])["url"]
        commit(pipeline.store, source_id, user, url, operation_id)
        receipt = pipeline.store.operation_receipt(operation_id)
    if receipt["user_id"] != user:
        raise LookupError("Source purge does not belong to this user")
    await _finish(pipeline, job, receipt)
    result = receipt["result"]
    pipeline.report(job)(
        "INFO",
        f"Source purged: {result['exclusive_tracks']} exclusive tracks deleted, "
        f"{result['shared_tracks']} shared tracks kept",
    )
