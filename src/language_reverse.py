"""Verified one-level undo for completed English-name retags.

Restore original Opus bytes from the snapshot written before a Language job.
Never guess a canonical spelling or overwrite a file edited after acceptance.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import common
from common import sha256_file


_SHA = re.compile(r"^[0-9a-f]{64}$")


def key(user_id, track_id):
    return f"language-undo-last:{user_id}:{track_id}"


def _latest(store, user_id, track_id):
    return store.one(
        """SELECT id,state,payload FROM jobs WHERE user_id=? AND kind='track'
           AND target=? AND json_extract(payload,'$.mode') IN ('language','language_reverse')
           ORDER BY id DESC LIMIT 1""", (user_id, track_id),
    )


def _record(store, user_id, track_id, job, track, asset):
    saved = store.setting(key(user_id, track_id), {}) or {}
    if (saved.get("state") == "applied" and saved.get("after") == asset["sha256"]
            and saved.get("asset_id") == asset["id"]
            and saved.get("mbid") == track["mbid"]
            and saved.get("release_id") == track["release_id"]
            and saved.get("path") == track["file_path"]):
        return saved
    # Backward compatibility: earlier versions wrote persistent undo files but
    # didn't store a manifest. Only accept an unambiguous single snapshot.
    directory = common.STATE / "language-undo" / user_id
    candidates = list(directory.glob(track_id + "-*.opus"))
    if len(candidates) != 1:
        return None
    backup = candidates[0]
    digest = backup.name[len(track_id) + 1:-5]
    if not _SHA.fullmatch(digest) or backup.is_symlink() or not backup.is_file():
        return None
    return {"state": "applied", "before": digest, "after": asset["sha256"],
            "backup_path": str(backup), "asset_id": asset["id"],
            "path": track["file_path"], "mbid": track["mbid"],
            "release_id": track["release_id"], "previous_matched_title": None,
            "legacy": True}


def undo_info(store, user_id, track_id, review_token=None, verify_bytes=False):
    """Return an authorized, hash-bound snapshot for the *latest* Language job."""
    store.require_user(user_id)
    latest = _latest(store, user_id, track_id)
    if not latest:
        return None
    # If any subsequent track job ran (album, manual correction, repair, etc.),
    # do not restore an older snapshot over those newer changes.
    latest_track_job = store.one(
        "SELECT id FROM jobs WHERE user_id=? AND kind='track' AND target=? ORDER BY id DESC LIMIT 1",
        (user_id, track_id),
    )
    if not latest_track_job or latest_track_job["id"] != latest["id"]:
        return None
    payload = json.loads(latest["payload"])
    if review_token and payload.get("review_token") != review_token:
        return None
    if payload.get("mode") != "language" or latest["state"] != "DONE":
        return None
    row = store.one(
        """SELECT t.*,a.id AS asset_uid,a.sha256 AS current_sha,
                  a.path AS asset_path,a.state AS asset_state
           FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
           WHERE t.id=? AND t.user_id=?""", (track_id, user_id))
    if not row:
        return None
    root = (common.LIBRARY / user_id).resolve()
    path = Path(row["file_path"]) if row["file_path"] else None
    if not path or path.is_symlink() or path != root / "audio" / (track_id + ".opus"):
        return None
    if (row["health"] != "AVAILABLE" or row["operation_state"] != "IDLE"
            or row["metadata_edited"] or not row["mbid"]
            or row["asset_state"] != "CURRENT"
            or row["asset_path"] != str(path) or not row["current_sha"]
            or not path.is_file()):
        return None
    asset = {"id": row["asset_uid"], "sha256": row["current_sha"]}
    record = _record(store, user_id, track_id, latest, row, asset)
    if not record or not _SHA.fullmatch(record.get("before") or ""):
        return None
    backup_dir = common.STATE / "language-undo" / user_id
    backup = backup_dir / f"{track_id}-{record['before']}.opus"
    if (record.get("backup_path") != str(backup) or backup.is_symlink()
            or not backup.is_file() or backup.stat().st_size == 0):
        return None
    if verify_bytes:
        if sha256_file(path) != record["after"] or sha256_file(backup) != record["before"]:
            return None
    return record


def queue_reverse(store, user_id, track_id, review_token):
    from language_jobs import _checked_name_review

    review = _checked_name_review(store, user_id, review_token)
    if track_id not in review["by_kind"].get("display_changes", []):
        raise ValueError("This track was not part of the reviewed name-change list")
    record = undo_info(store, user_id, track_id, review_token, verify_bytes=True)
    if record is None:
        raise ValueError("Original tags are unavailable or audio has changed; reverse is unsafe")
    store.queue_track(track_id, user_id, {
        "mode": "language_reverse", "review_token": review_token,
        "expected_after": record["after"], "restore_sha": record["before"],
        "asset_id": record["asset_id"], "backup_path": record["backup_path"],
        "mbid": record["mbid"], "release_id": record["release_id"],
        "previous_matched_title": record.get("previous_matched_title"),
    })
    store.event("INFO", f"Reversing the English-name update for {track_id}. "
                "Restoring the exact original tags; recording identity and path stay unchanged.",
                user_id, "language-review")
    return {"queued": 1, "track_id": track_id, "message": "Reverse queued; progress appears in Activity."}


def display_job_state(store, user_id, track_id, job):
    """A completed update is reversible only when its original bytes are present."""
    payload = json.loads(job["payload"])
    mode, state = payload.get("mode"), job["state"]
    if mode == "language_reverse":
        return {"PENDING": "REVERSE_PENDING", "RUNNING": "REVERSING",
                "DONE": "REVERSED", "FAILED": "REVERSE_FAILED",
                "CANCELLED": "REVERSE_FAILED"}.get(state, state)
    if mode == "language" and state == "DONE":
        return "REVERSIBLE" if undo_info(
            store, user_id, track_id, payload.get("review_token")) else "DONE"
    return state
