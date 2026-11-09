"""Durable per-track image changes without audio downloads or re-encoding."""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from pathlib import Path

from artwork import Artwork, FRAMINGS
from common import atomic_copy, atomic_json, sha256_file, utcnow
from source_metadata import correction_revision


def revision(con, track):
    pref = con.execute("SELECT * FROM artwork_preferences WHERE track_id=? AND user_id=?", (track["id"], track["user_id"])).fetchone()
    return hashlib.sha256(json.dumps([correction_revision(track), dict(pref) if pref else None], sort_keys=True).encode()).hexdigest()


def queue(store, track_id, user, reviewed_revision, *, selection=None, framing="original", refresh=False):
    if framing not in FRAMINGS:
        raise ValueError("Unknown image framing")
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)).fetchone()
        if not track:
            raise LookupError("Track not found for this library")
        if reviewed_revision != revision(con, track):
            raise ValueError("This track changed; reopen its artwork and review again")
        if track["health"] != "AVAILABLE" or not track["current_asset_id"]:
            raise ValueError("Download audio before changing artwork")
        if track["operation_state"] not in {"IDLE", "NEEDS_APPROVAL", "STOPPED"} or con.execute(
            "SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track_id,)
        ).fetchone():
            raise ValueError("Finish or retry the existing operation before changing artwork")
        if (store.path.parent / "staging" / track_id / "apply-request.json").exists():
            raise ValueError("Retry the interrupted metadata application before changing artwork")
        if selection and selection != "auto" and not con.execute(
            "SELECT 1 FROM artwork_candidates WHERE id=? AND track_id=? AND user_id=?", (selection, track_id, user)
        ).fetchone():
            raise LookupError("Artwork is not available on this track")
        payload = {"mode": "artwork", "operation": uuid.uuid4().hex, "selection": selection,
                   "framing": framing, "refresh": bool(refresh), "asset_id": track["current_asset_id"],
                   "previous_state": track["operation_state"], "previous_kind": track["operation_kind"],
                   "previous_error": track["operation_error"]}
        if not store.enqueue("track", track_id, user, payload, con=con):
            raise ValueError("This track already has an active job")
        # Leave candidate bytes, choices and approval token intact.
        con.execute("UPDATE tracks SET operation_state='QUEUED',operation_kind='ARTWORK',operation_error=NULL,updated_at=? WHERE id=?", (utcnow(), track_id))
    return {"message": "Artwork update queued. Audio and music metadata are retained."}


async def process(pipeline, job):
    store = pipeline.store
    payload = json.loads(job["payload"])
    operation = payload["operation"]
    track = store.owned("tracks", job["target"], job["user_id"])
    directory = store.path.parent / "staging" / ("artwork-" + track["id"] + "-" + operation)
    completed = store.operation_receipt(operation)
    if completed and completed["finalized_at"]:
        # A crash after the receipt but before finish_job can leave QUEUED/RUNNING.
        previous = completed["result"]
        store.update("tracks", track["id"], operation_state=previous["previous_state"],
                     operation_kind=previous.get("previous_kind"), operation_error=previous.get("previous_error"))
        shutil.rmtree(directory, ignore_errors=True)
        return
    asset = store.current_asset(track["id"])
    if not asset or asset["id"] != payload["asset_id"]:
        raise ValueError("Current audio changed; reopen its artwork")
    from common import LIBRARY
    path = Path(asset["path"]).resolve()
    if not path.is_relative_to((LIBRARY / track["user_id"]).resolve()) or not path.is_file():
        raise ValueError("Audio is missing or outside this library")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "image-plan.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["asset_id"] != asset["id"] or manifest["path"] != str(path):
            raise ValueError("Current audio changed while artwork was pending")
    else:
        before = await asyncio.to_thread(sha256_file, path)
        if not asset.get("sha256") or before != asset["sha256"]:
            raise ValueError("Audio changed externally; verify it before changing artwork")
        staged = directory / "image.opus"
        await asyncio.to_thread(atomic_copy, path, staged)
        result = await pipeline.bridge(job, track, directory, "artwork", path=str(staged),
            selection=payload.get("selection"), framing=payload["framing"], refresh=payload["refresh"])
        manifest = {"asset_id": asset["id"], "path": str(path), "before": before,
                    "after": await asyncio.to_thread(sha256_file, staged), "artwork": result}
        atomic_json(manifest_path, manifest)
    pipeline.checkpoint(job, "applying")
    current = await asyncio.to_thread(sha256_file, path)
    if current == manifest["before"] and current != manifest["after"]:
        staged = directory / "image.opus"
        if not staged.is_file() or await asyncio.to_thread(sha256_file, staged) != manifest["after"]:
            raise ValueError("Staged artwork changed; refusing replacement")
        await asyncio.to_thread(atomic_copy, staged, path)
    elif current not in {manifest["before"], manifest["after"]}:
        raise ValueError("Audio changed during artwork review; it was not overwritten")
    refreshed = await pipeline.bridge(job, track, directory, "refresh", path=str(path))
    if await asyncio.to_thread(sha256_file, path) != manifest["after"]:
        raise ValueError("Audio changed during synchronization; verify it before retrying")
    stat = path.stat()
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        latest = con.execute("SELECT current_asset_id FROM tracks WHERE id=? AND user_id=?", (track["id"], track["user_id"])).fetchone()
        if not latest or latest["current_asset_id"] != asset["id"]:
            raise ValueError("Audio changed before committing artwork")
        con.execute("UPDATE assets SET sha256=?,size_bytes=?,mtime_ns=? WHERE id=? AND state='CURRENT'",
                    (manifest["after"], stat.st_size, stat.st_mtime_ns, asset["id"]))
        Artwork.record(con, track, manifest["artwork"])
        con.execute("UPDATE tracks SET beets_id=COALESCE(?,beets_id),operation_state=?,operation_kind=?,operation_error=?,updated_at=? WHERE id=?",
                    (refreshed.get("beets_id"), payload["previous_state"], payload["previous_kind"], payload["previous_error"], utcnow(), track["id"]))
        store._record_receipt_locked(con, operation, track["user_id"], track["id"], track["id"], "artwork",
            {"kind": "ARTWORK", "target_id": track["id"], "previous_state": payload["previous_state"],
             "previous_kind": payload["previous_kind"], "previous_error": payload["previous_error"]}, finalized=True)
    for warning in manifest["artwork"].get("warnings", []):
        pipeline.report(job)("WARNING", warning)
    shutil.rmtree(directory, ignore_errors=True)
    pipeline.report(job)("INFO", "Per-track artwork updated; encoded audio and pending review retained")


def queue_merge_followup(store, receipt):
    """Reconsider a new merged video's artwork once, after the merge commits."""
    if not receipt or not receipt.get("finalized_at"):
        return False
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        saved = con.execute("SELECT result FROM operation_receipts WHERE operation_id=?", (receipt["operation_id"],)).fetchone()
        result = json.loads(saved["result"]) if saved else {}
        if not result.get("artwork_pending"):
            return False
        track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (result.get("target_id"), receipt["user_id"])).fetchone()
        if track and (track["operation_state"] != "IDLE" or con.execute("SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track["id"],)).fetchone()):
            return False
        queued = False
        if track and track["health"] == "AVAILABLE" and track["current_asset_id"]:
            payload = {"mode": "artwork", "operation": uuid.uuid4().hex, "selection": None, "framing": "original",
                       "refresh": False, "asset_id": track["current_asset_id"], "previous_state": "IDLE",
                       "previous_kind": None, "previous_error": None}
            queued = store.enqueue("track", track["id"], track["user_id"], payload, con=con)
            if queued:
                con.execute("UPDATE tracks SET operation_state='QUEUED',operation_kind='ARTWORK',updated_at=? WHERE id=?", (utcnow(), track["id"]))
        result.pop("artwork_pending", None)
        con.execute("UPDATE operation_receipts SET result=? WHERE operation_id=?", (json.dumps(result), receipt["operation_id"]))
        return queued
