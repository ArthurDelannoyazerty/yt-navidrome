"""Review and queue opt-in English-first metadata changes for approved audio.

Catalogue indexing never modifies local files. The user reviews a bounded local
scan, tests three files in Navidrome, acknowledges the check, then enables bulk.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import common
from localized_names import LANGUAGE_POLICY, artist_credit, is_localized, preferred_english_name

MAX_TRACKS = 5000
PROGRESS_STEP = 25


class PreviewProgress:
    """Process-local progress for an in-flight HTTP preview (not a write job)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._users = {}

    def update(self, user, **values):
        with self._lock:
            self._users[user] = {**self._users.get(user, {}), **values}

    def get(self, user):
        with self._lock:
            return dict(self._users.get(user, {"state": "idle", "processed": 0, "total": 0}))


preview_progress = PreviewProgress()
queue_progress = PreviewProgress()


def candidates(store, user_id, *, cap=MAX_TRACKS, include_localized=False, progress=None,
               stop_after=None):
    store.require_user(user_id)
    root = (common.LIBRARY / user_id).resolve()
    rows = store.rows(
        """SELECT t.id,t.mbid,t.release_id,t.matched_title,t.file_path,t.metadata_edited,
                  t.health,t.operation_state,t.current_asset_id,a.state AS asset_state,
                  a.path AS asset_path,a.sha256
           FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
           WHERE t.user_id=? AND t.mbid IS NOT NULL
           ORDER BY t.id LIMIT ?""", (user_id, min(MAX_TRACKS, int(cap))),
    )
    ready = []
    summary = {"already_localized": 0, "manual_or_busy": 0,
               "invalid_or_missing": 0}
    for index, row in enumerate(rows, start=1):
        if (row["metadata_edited"] or row["operation_state"] != "IDLE"
                or row["health"] != "AVAILABLE"):
            summary["manual_or_busy"] += 1
        else:
            path = Path(row["file_path"]) if row["file_path"] else None
            if (not path or path.is_symlink()
                    or path != root / "audio" / (row["id"] + ".opus")
                    or row["asset_path"] != str(path)
                    or row["asset_state"] != "CURRENT" or not row["sha256"]
                    or not path.is_file()):
                summary["invalid_or_missing"] += 1
            elif is_localized(path) and not include_localized:
                summary["already_localized"] += 1
            else:
                ready.append(row)
        if progress and (index == 1 or index % PROGRESS_STEP == 0 or index == len(rows)
                         or (stop_after and len(ready) >= stop_after)):
            progress(index, len(rows), len(ready), dict(summary))
        if stop_after and len(ready) >= stop_after:
            break
    if progress and not rows:
        progress(0, 0, 0, dict(summary))
    return ready, summary


def _credited_english_name(con, credits):
    parts = []
    for credit in credits or []:
        if isinstance(credit, str):
            parts.append(credit)
        elif isinstance(credit, dict):
            artist = credit.get("artist") or {}
            name = str(credit.get("name") or artist.get("name") or "")
            if artist.get("id"):
                row = con.execute(
                    "SELECT data FROM entities WHERE kind='artist' AND id=?",
                    (artist["id"],),
                ).fetchone()
                if row:
                    cached = json.loads(row["data"])
                    english = preferred_english_name(cached, field="name")
                    if english and english != cached.get("name"):
                        name = english
            parts.extend((name, str(credit.get("joinphrase") or "")))
    return "".join(parts).strip()


def _samples(store, ready):
    """Best-effort cached display predictions; never guess a missing alias."""
    from catalogue import Catalogue

    result = []
    try:
        with Catalogue(store).db() as con:
            for track in ready[:8]:
                current = track["matched_title"] or track["id"]
                recording = con.execute(
                    "SELECT data FROM entities WHERE kind='recording' AND id=?",
                    (track["mbid"],),
                ).fetchone()
                if not recording:
                    result.append({"id": track["id"], "current_name": current,
                                   "proposed_name": None, "canonical_name": None,
                                   "album": None, "prediction": "Needs MusicBrainz lookup on apply"})
                    continue
                raw = json.loads(recording["data"])
                title = preferred_english_name(raw)
                artist = _credited_english_name(con, raw.get("artist-credit"))
                proposed = f"{artist} - {title}" if artist else title
                native_artist = artist_credit(raw)
                native = f"{native_artist} - {raw.get('title', '')}" if native_artist else raw.get("title", "")
                album = None
                if track["release_id"]:
                    cached_album = con.execute(
                        "SELECT data FROM entities WHERE kind='release' AND id=?",
                        (track["release_id"],),
                    ).fetchone()
                    if cached_album:
                        album = preferred_english_name(json.loads(cached_album["data"]))
                result.append({"id": track["id"], "current_name": current,
                               "proposed_name": proposed or None, "canonical_name": native,
                               "album": album, "prediction": "Cached estimate; verified on apply"})
    except Exception:
        # No cached catalogue is not an error: the apply step uses beets'
        # authoritative MusicBrainz lookup before touching a staged copy.
        result = [{"id": track["id"], "current_name": track["matched_title"] or track["id"],
                   "proposed_name": None, "canonical_name": None, "album": None,
                   "prediction": "Needs MusicBrainz lookup on apply"} for track in ready[:8]]
    return result


def preview(store, user_id, include_localized=False):
    store.require_user(user_id)
    preview_progress.update(user_id, state="running", processed=0, total=0,
                            eligible=0, skipped={}, error=None)
    started = time.monotonic()
    store.event("INFO", "English-name review started (read-only; no audio or tags will change).",
                user_id, "language-review")

    def on_progress(done, total, eligible, skipped):
        preview_progress.update(user_id, state="running", processed=done, total=total,
                                eligible=eligible, skipped=skipped)
        if done and (done % 250 == 0 or done == total):
            store.event("INFO", f"English-name review: examined {done}/{total}; "
                        f"{eligible} eligible, {sum(skipped.values())} excluded.",
                        user_id, "language-review")

    try:
        ready, skipped = candidates(store, user_id,
                                    include_localized=include_localized, progress=on_progress)
        result = {"eligible": len(ready), "skipped": skipped, "sample": _samples(store, ready),
                  "include_localized": bool(include_localized), "scanned": len(ready) + sum(skipped.values()),
                  "note": "Read-only review; no tags or audio changed. Cached name predictions may change when the MusicBrainz lookup runs during apply."}
    except Exception as exc:
        preview_progress.update(user_id, state="error", error=str(exc))
        store.event("ERROR", f"English-name review failed without changing audio: {exc}",
                    user_id, "language-review")
        raise
    elapsed = round(time.monotonic() - started, 1)
    preview_progress.update(user_id, state="complete", elapsed_seconds=elapsed, **{
        k: result[k] for k in ("eligible", "skipped", "scanned")})
    store.event("INFO", f"English-name review finished in {elapsed}s: "
                f"{result['scanned']} checked, {result['eligible']} eligible, "
                f"{sum(skipped.values())} excluded. No audio or tags were changed.",
                user_id, "language-review")
    return result


def _canary_key(user_id):
    return "english-language-canary:" + user_id


def canary_status(store, user_id):
    store.require_user(user_id)
    saved = store.setting(_canary_key(user_id), {}) or {}
    ids = saved.get("track_ids") or []
    if not ids:
        return {"state": "not_started", "verified": False, "track_count": 0}
    pending = failed = 0
    for track_id in ids:
        track = store.one(
            "SELECT operation_state,file_path,health FROM tracks WHERE id=? AND user_id=?",
            (track_id, user_id),
        )
        if not track or track["operation_state"] in {"FAILED", "STOPPED"}:
            failed += 1
        elif track["operation_state"] != "IDLE":
            pending += 1
        elif (track["health"] != "AVAILABLE" or not track["file_path"]
              or not is_localized(Path(track["file_path"]))):
            failed += 1
    ready = failed == 0 and pending == 0
    return {"state": "failed" if failed else "running" if pending else "ready",
            "verified": ready and bool(saved.get("verified")),
            "track_count": len(ids), "pending": pending, "failed": failed,
            "include_localized": bool(saved.get("include_localized")),
            "track_ids": ids}


def verify_canary(store, user_id):
    status = canary_status(store, user_id)
    if status["state"] != "ready":
        raise ValueError("Finish a successful three-track canary before acknowledging the Navidrome check")
    saved = store.setting(_canary_key(user_id), {})
    saved["verified"] = True
    store.set_setting(_canary_key(user_id), saved)
    store.event("INFO", f"Navidrome canary check acknowledged for {status['track_count']} "
                "English-tagged tracks; bulk tagging may now be queued.",
                user_id, "language-review")
    return canary_status(store, user_id)


def queue(store, user_id, limit, include_localized=False):
    if not 1 <= limit <= MAX_TRACKS:
        raise ValueError("Choose between 1 and 5000 tracks")
    store.require_user(user_id)
    is_canary = limit <= 3
    mode = "canary" if is_canary else "bulk"
    queue_progress.update(user_id, state="scanning", mode=mode, processed=0,
                          total=0, eligible=0, queued=0, error=None)
    store.event("INFO", f"English-name {mode} requested: selecting up to {limit} "
                "eligible tracks. Audio has not changed; queue progress will follow.",
                user_id, "language-review")

    def on_scan(done, total, eligible, skipped):
        queue_progress.update(user_id, state="scanning", mode=mode, processed=done,
                              total=total, eligible=eligible, skipped=skipped)
        if done == 1 or done % 500 == 0 or done == total:
            store.event("INFO", f"English-name {mode} selection: checked "
                        f"{done}/{total}, {eligible} eligible, "
                        f"{sum(skipped.values())} excluded.",
                        user_id, "language-review")

    try:
        old_canary = canary_status(store, user_id)
        if not is_canary and (not old_canary.get("verified") or
                             old_canary.get("include_localized") != bool(include_localized)):
            raise ValueError("First apply three tracks, verify their Navidrome IDs/ratings/playlists, and acknowledge the canary check. Use the same recheck setting.")
        # A canary only needs three safe candidates, not thousands of Opus reads.
        ready, skipped = candidates(
            store, user_id, include_localized=include_localized, progress=on_scan,
            stop_after=3 if is_canary else None,
        )
        scanned = len(ready) + sum(skipped.values())
        if not is_canary:
            tested = set(old_canary.get("track_ids") or [])
            ready = [row for row in ready if row["id"] not in tested]
        queue_progress.update(user_id, state="queuing", processed=scanned,
                              eligible=len(ready))
        queued_ids = []
        for row in ready[:limit]:
            try:
                store.queue_track(row["id"], user_id, {"mode": "language"})
                queued_ids.append(row["id"])
                queue_progress.update(user_id, queued=len(queued_ids))
            except (ValueError, LookupError) as exc:
                skipped["manual_or_busy"] += 1
                store.event("WARNING", f"English-name {mode}: skipped track "
                            f"{row['id']} after an eligibility change: {exc}",
                            user_id, "language-review")
        if is_canary and queued_ids:
            store.set_setting(_canary_key(user_id),
                              {"policy": LANGUAGE_POLICY, "track_ids": queued_ids,
                               "include_localized": bool(include_localized), "verified": False})
        store.event("INFO", f"English-name {mode}: "
                    f"{len(queued_ids)} track jobs queued; {sum(skipped.values())} excluded. "
                    "No downloads or identity changes; worker results will appear in Activity.",
                    user_id, "language-review")
        queue_progress.update(user_id, state="complete", queued=len(queued_ids),
                              skipped=skipped, error=None)
        return {"queued": len(queued_ids), "eligible_before": len(ready),
                "scanned": scanned, "skipped": skipped, "mode": mode,
                "message": "English-first metadata jobs queued; no downloads or album guessing."}
    except Exception as exc:
        queue_progress.update(user_id, state="error", error=str(exc))
        store.event("ERROR", f"English-name {mode} request failed: {exc}",
                    user_id, "language-review")
        raise
