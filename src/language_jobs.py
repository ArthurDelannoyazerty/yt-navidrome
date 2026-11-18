"""Review and queue opt-in English-first metadata changes for approved audio.

Catalogue indexing never modifies local files. The user reviews a bounded local
scan, tests three files in Navidrome, acknowledges the check, then enables bulk.
"""
from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

from mutagen.oggopus import OggOpus

from pathlib import Path

import common
from localized_names import (LANGUAGE_POLICY, POLICY_TAG, artist_credit,
                             is_localized, preferred_english_name)

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
               stop_after=None, inspect=None):
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
            else:
                try:
                    tags = OggOpus(str(path))
                    already_tagged = LANGUAGE_POLICY in (tags.get(POLICY_TAG) or [])
                except Exception:
                    summary["invalid_or_missing"] += 1
                    continue
                if already_tagged and not include_localized:
                    summary["already_localized"] += 1
                else:
                    ready.append(row)
                    if inspect:
                        inspect(row, tags)
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


def _review_key(user_id):
    return "english-language-review:" + user_id


def _tag(tags, name):
    return str((tags.get(name) or [""])[0]).strip()


def _classify(con, track, tags):
    """Cache-based estimate from the current file tags, not an online claim."""
    current_title = _tag(tags, "TITLE")
    current_artist = _tag(tags, "ARTIST")
    current_album = _tag(tags, "ALBUM")
    current = " - ".join(x for x in (current_artist, current_title) if x)
    base = {"id": track["id"], "current_name": current or track["matched_title"] or track["id"],
            "proposed_name": None, "canonical_name": None, "album": None,
            "prediction": "MusicBrainz cache estimate", "changes": []}
    try:
        row = con.execute("SELECT data FROM entities WHERE kind='recording' AND id=?",
                          (track["mbid"],)).fetchone()
        if not row:
            return {**base, "kind": "unknown", "prediction": "Recording is not cached; index it in Catalogue first"}
        recording = json.loads(row["data"])
        if not recording.get("title") or not recording.get("artist-credit"):
            return {**base, "kind": "unknown", "prediction": "Recording credits are not cached yet"}
        native_title = str(recording["title"]).strip()
        native_artist = artist_credit(recording)
        preferred_title = preferred_english_name(recording)
        preferred_artist = _credited_english_name(con, recording["artist-credit"]) or native_artist
        proposed_album, native_album = None, None
        if track["release_id"]:
            row = con.execute("SELECT data FROM entities WHERE kind='release' AND id=?",
                              (track["release_id"],)).fetchone()
            if not row:
                return {**base, "kind": "unknown", "prediction": "Confirmed album edition is not cached yet"}
            release = json.loads(row["data"])
            proposed_album = preferred_english_name(release)
            native_album = str(release.get("title") or "").strip()
        expected_display = {"TITLE": preferred_title, "ARTIST": preferred_artist}
        if proposed_album:
            expected_display["ALBUM"] = proposed_album
        display_changes = [key.title() for key, expected in expected_display.items()
                           if expected and _tag(tags, key) != expected]
        expected_native = {"ORIGINALTITLE": native_title, "ORIGINALARTIST": native_artist}
        if native_album:
            expected_native["ORIGINALALBUM"] = native_album
        original_changes = [key.title() for key, expected in expected_native.items()
                            if expected and _tag(tags, key) != expected]
        kind = ("display_changes" if display_changes else
                "original_updates" if original_changes else "unchanged")
        return {**base, "kind": kind,
                "proposed_name": " - ".join(x for x in (preferred_artist, preferred_title) if x),
                "canonical_name": " - ".join(x for x in (native_artist, native_title) if x),
                "album": proposed_album,
                "changes": display_changes + original_changes,
                "prediction": "Cache estimate; exact MusicBrainz data checked when applying"}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {**base, "kind": "unknown", "prediction": "Incomplete cached metadata; index in Catalogue"}


def preview(store, user_id, include_localized=False):
    from catalogue import Catalogue

    store.require_user(user_id)
    preview_progress.update(user_id, state="running", processed=0, total=0,
                            eligible=0, skipped={}, error=None)
    started = time.monotonic()
    store.event("INFO", "English-name review started (read-only; no audio or tags will change).",
                user_id, "language-review")
    counts = {"display_changes": 0, "original_updates": 0, "unchanged": 0, "unknown": 0}
    ids = {key: [] for key in counts}
    evidence = {}
    examples = []
    def on_progress(done, total, eligible, skipped):
        preview_progress.update(user_id, state="running", processed=done, total=total,
                                eligible=eligible, skipped=skipped, changes=dict(counts))
        if done and (done % 250 == 0 or done == total):
            store.event("INFO", f"English-name review: examined {done}/{total}; "
                        f"{eligible} safe candidates, {sum(skipped.values())} excluded.",
                        user_id, "language-review")

    try:
        with Catalogue(store).db() as con:
            def on_candidate(track, tags):
                item = _classify(con, track, tags)
                kind = item["kind"]
                counts[kind] += 1
                ids[kind].append(track["id"])
                evidence[track["id"]] = track["sha256"]
                # Show substantive examples first, not eight unchanged titles.
                if kind != "unchanged" and len(examples) < 24:
                    examples.append(item)
                elif len(examples) < 8:
                    examples.append(item)
            ready, skipped = candidates(store, user_id,
                                        include_localized=include_localized,
                                        progress=on_progress, inspect=on_candidate)
        # Reorder examples by usefulness and keep the UI bounded.
        priority = {"display_changes": 0, "original_updates": 1, "unknown": 2, "unchanged": 3}
        examples.sort(key=lambda item: priority[item["kind"]])
        result = {"eligible": len(ready), "skipped": skipped, "sample": examples[:8],
                  "changes": counts, "include_localized": bool(include_localized),
                  "scanned": len(ready) + sum(skipped.values()),
                  "note": "Safe candidates are not necessarily changes. Predictions use cached metadata; the worker checks MusicBrainz again before writing."}
        token = uuid4().hex
        store.set_setting(_review_key(user_id), {
            "token": token, "created": time.time(),
            "include_localized": bool(include_localized),
            "by_kind": ids, "asset_hashes": evidence,
        })
        result["review_token"] = token
    except Exception as exc:
        preview_progress.update(user_id, state="error", error=str(exc))
        store.event("ERROR", f"English-name review failed without changing audio: {exc}",
                    user_id, "language-review")
        raise
    elapsed = round(time.monotonic() - started, 1)
    preview_progress.update(user_id, state="complete", elapsed_seconds=elapsed,
                            changes=dict(counts), **{
        k: result[k] for k in ("eligible", "skipped", "scanned")})
    store.event("INFO", f"English-name review finished in {elapsed}s: "
                f"{counts['display_changes']} possible display-name changes, "
                f"{counts['original_updates']} original-tag repairs, "
                f"{counts['unchanged']} no detectable changes, "
                f"{counts['unknown']} require catalogue metadata. "
                "No audio or tags changed.",
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
