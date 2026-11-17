"""Preview and queue explicit English-first metadata-only updates.

Indexing the MusicBrainz catalogue never mutates local audio. This separate
operation must be confirmed and excludes user-edited or busy tracks.
"""
from __future__ import annotations

from pathlib import Path

import common
from localized_names import is_localized

MAX_TRACKS = 5000


def candidates(store, user_id, *, cap=MAX_TRACKS):
    store.require_user(user_id)
    root = (common.LIBRARY / user_id).resolve()
    rows = store.rows(
        """SELECT t.id,t.mbid,t.matched_title,t.file_path,t.metadata_edited,
                  t.health,t.operation_state,t.current_asset_id,a.state AS asset_state,
                  a.path AS asset_path,a.sha256
           FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
           WHERE t.user_id=? AND t.mbid IS NOT NULL
           ORDER BY t.id LIMIT ?""", (user_id, min(MAX_TRACKS, int(cap))),
    )
    ready = []
    summary = {"already_localized": 0, "manual_or_busy": 0,
               "invalid_or_missing": 0}
    for row in rows:
        if (row["metadata_edited"] or row["operation_state"] != "IDLE"
                or row["health"] != "AVAILABLE"):
            summary["manual_or_busy"] += 1
            continue
        path = Path(row["file_path"]) if row["file_path"] else None
        if (not path or path.is_symlink()
                or path != root / "audio" / (row["id"] + ".opus")
                or row["asset_path"] != str(path)
                or row["asset_state"] != "CURRENT" or not row["sha256"]
                or not path.is_file()):
            summary["invalid_or_missing"] += 1
            continue
        if is_localized(path):
            summary["already_localized"] += 1
            continue
        ready.append(row)
    return ready, summary


def preview(store, user_id):
    ready, skipped = candidates(store, user_id)
    return {"eligible": len(ready), "skipped": skipped,
            "sample": [{"id": row["id"], "current_name": row["matched_title"] or row["id"]}
                       for row in ready[:8]],
            "note": "MusicBrainz catalogue indexing is read-only. Apply English tags in a separate confirmed pass; start with three tracks and verify Navidrome."}


def queue(store, user_id, limit):
    if not 1 <= limit <= MAX_TRACKS:
        raise ValueError("Choose between 1 and 5000 tracks")
    ready, skipped = candidates(store, user_id)
    queued = 0
    for row in ready[:limit]:
        try:
            store.queue_track(row["id"], user_id, {"mode": "language"})
            queued += 1
        except (ValueError, LookupError):
            skipped["manual_or_busy"] += 1
    return {"queued": queued, "eligible_before": len(ready),
            "skipped": skipped, "message": "English-first metadata jobs queued; no downloads or album guessing."}
