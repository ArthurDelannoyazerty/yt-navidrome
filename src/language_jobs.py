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
        proposed_albumartist, native_albumartist = None, None
        if track["release_id"]:
            row = con.execute("SELECT data FROM entities WHERE kind='release' AND id=?",
                              (track["release_id"],)).fetchone()
            if not row:
                return {**base, "kind": "unknown", "prediction": "Confirmed album edition is not cached yet"}
            release = json.loads(row["data"])
            proposed_album = preferred_english_name(release)
            native_album = str(release.get("title") or "").strip()
            if release.get("artist-credit"):
                native_albumartist = artist_credit(release)
                proposed_albumartist = _credited_english_name(con, release["artist-credit"]) or native_albumartist
        expected_display = {"TITLE": preferred_title, "ARTIST": preferred_artist}
        if proposed_album:
            expected_display["ALBUM"] = proposed_album
        if proposed_albumartist:
            expected_display["ALBUMARTIST"] = proposed_albumartist
        display_diffs = [
            {"field": key, "before": _tag(tags, key), "after": expected}
            for key, expected in expected_display.items()
            if expected and _tag(tags, key) != expected
        ]
        display_changes = [change["field"].title() for change in display_diffs]
        expected_native = {"ORIGINALTITLE": native_title, "ORIGINALARTIST": native_artist}
        if native_album:
            expected_native["ORIGINALALBUM"] = native_album
        if native_albumartist:
            expected_native["ORIGINALALBUMARTIST"] = native_albumartist
        original_changes = [key.title() for key, expected in expected_native.items()
                            if expected and _tag(tags, key) != expected]
        kind = ("display_changes" if display_changes else
                "original_updates" if original_changes else "unchanged")
        proposed_name = " - ".join(x for x in (preferred_artist, preferred_title) if x)
        if any(diff["field"] in {"TITLE", "ARTIST"} for diff in display_diffs):
            before_label = base["current_name"]
            after_label = proposed_name
            changed_field = "Title / artist"
        elif display_diffs:
            # Album-only changes must show the field that actually changes,
            # not an identical artist/title on both sides of the arrow.
            diff = display_diffs[0]
            before_label, after_label = diff["before"], diff["after"]
            changed_field = "Album artist" if diff["field"] == "ALBUMARTIST" else "Album"
        else:
            before_label = after_label = None
            changed_field = None
        return {**base, "kind": kind,
                "proposed_name": proposed_name,
                "canonical_name": " - ".join(x for x in (native_artist, native_title) if x),
                "album": proposed_album,
                "before_label": before_label, "after_label": after_label,
                "changed_field": changed_field, "display_diffs": display_diffs,
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
    display_rows = []
    examples = []
    examples_by_kind = {key: 0 for key in counts}
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
                if kind == "display_changes":
                    display_rows.append({
                        key: item.get(key) for key in
                        ("id", "current_name", "proposed_name",
                         "before_label", "after_label", "changed_field", "changes")
                    })
                # Show substantive examples first, not eight unchanged titles.
                if examples_by_kind[kind] < (3 if kind != "unchanged" else 2):
                    examples.append(item)
                    examples_by_kind[kind] += 1
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
        result["review_token"] = token
        store.set_setting(_review_key(user_id), {
            "token": token, "created": time.time(),
            "include_localized": bool(include_localized),
            "by_kind": ids, "asset_hashes": evidence,
            "display_rows": display_rows,
            "summary": result,
        })
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
                "No audio or tags were changed.",
                user_id, "language-review")
    return result



def review_state(store, user_id):
    """Return last review summary without rescanning thousands of audio files."""
    store.require_user(user_id)
    saved = store.setting(_review_key(user_id), {}) or {}
    if not saved:
        return {"state": "idle"}
    if time.time() - saved.get("created", 0) > 4 * 3600:
        return {"state": "expired"}
    summary = saved.get("summary")
    return {"state": "ready", "result": summary} if summary else {"state": "idle"}



def _checked_name_review(store, user_id, review_token):
    """Authoritative recent review; track identity still rechecked at queue time."""
    store.require_user(user_id)
    review = store.setting(_review_key(user_id), {}) or {}
    if (review.get("token") != review_token
            or time.time() - review.get("created", 0) > 4 * 3600):
        raise ValueError("Review expired or changed. Run the read-only review again.")
    if "display_rows" not in review:
        raise ValueError("Run the review again to generate the full name-change list.")
    return review


def name_changes(store, user_id, review_token, offset=0, limit=100):
    """Return only visible-name changes, with server-side pagination and job state."""
    if not 0 <= offset <= MAX_TRACKS or not 1 <= limit <= 100:
        raise ValueError("Invalid name-change list page")
    review = _checked_name_review(store, user_id, review_token)
    all_rows = review["display_rows"]
    page = [dict(row) for row in all_rows[offset:offset + limit]]
    if page:
        ids = [row["id"] for row in page]
        marks = ",".join("?" for _ in ids)
        jobs = store.rows(
            f"""SELECT target,state FROM jobs WHERE user_id=? AND kind='track'
                AND json_extract(payload,'$.mode')='language'
                AND json_extract(payload,'$.review_token')=?
                AND target IN ({marks}) ORDER BY id DESC""",
            (user_id, review_token, *ids),
        )
        latest = {}
        for job in jobs:
            latest.setdefault(job["target"], job["state"])
        for row in page:
            row["job_state"] = latest.get(row["id"])
    return {"total": len(all_rows), "offset": offset, "items": page,
            "next_offset": offset + len(page) if offset + len(page) < len(all_rows) else None}


def queue_one_name_change(store, user_id, track_id, review_token):
    """Explicitly queue one selected visible-name change; never modify the canary."""
    review = _checked_name_review(store, user_id, review_token)
    if track_id not in review["by_kind"].get("display_changes", []):
        raise ValueError("Only an explicitly reviewed visible-name change can be selected")
    asset_sha = review.get("asset_hashes", {}).get(track_id)
    track = store.one(
        """SELECT t.id,t.file_path,t.health,t.operation_state,t.metadata_edited,
                  t.mbid,t.current_asset_id,a.path AS asset_path,a.state AS asset_state,
                  a.sha256 AS asset_sha
           FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
           WHERE t.id=? AND t.user_id=?""", (track_id, user_id),
    )
    if not track or not asset_sha or track["asset_sha"] != asset_sha:
        raise ValueError("Music or its audio asset changed since review; review again")
    path = Path(track["file_path"]) if track["file_path"] else None
    root = (common.LIBRARY / user_id).resolve()
    if (track["health"] != "AVAILABLE" or track["operation_state"] != "IDLE"
            or track["metadata_edited"] or not track["mbid"]
            or track["asset_state"] != "CURRENT" or not track["current_asset_id"]
            or not path or path.is_symlink()
            or path != root / "audio" / (track_id + ".opus")
            or track["asset_path"] != str(path) or not path.is_file()):
        raise ValueError("Music is no longer safe for this update; review again")
    store.queue_track(track_id, user_id, {"mode": "language", "review_token": review_token})
    store.event("INFO", f"Individual English-name update queued for {track_id}; "
                "worker details will appear in Activity. Recording ID and audio path stay fixed.",
                user_id, "language-review")
    return {"queued": 1, "track_id": track_id, "message": "Name update queued; follow Activity for its result."}


def batch_status(store, user_id):
    """Durable, bounded summary of the last language-tag submission."""
    store.require_user(user_id)
    saved = store.setting("english-language-batch:" + user_id, {}) or {}
    ids = saved.get("ids") or []
    if not ids:
        return {"state": "idle", "submitted": 0, "done": 0, "running": 0,
                "pending": 0, "failed": 0}
    states = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        rows = store.rows(
            f"""SELECT target,state,id FROM jobs
                WHERE user_id=? AND kind='track'
                  AND json_extract(payload,'$.mode')='language'
                  AND target IN ({marks})
                ORDER BY id DESC""", (user_id, *chunk))
        for row in rows:
            if row["target"] not in states:
                states[row["target"]] = row["state"]
    done = sum(value == "DONE" for value in states.values())
    failed = sum(value in {"FAILED", "CANCELLED"} for value in states.values())
    running = sum(value == "RUNNING" for value in states.values())
    pending = len(ids) - done - failed - running
    return {"state": "failed" if failed else "running" if pending or running else "done",
            "mode": saved.get("mode"), "scope": saved.get("scope"),
            "review_token": saved.get("review_token"),
            "submitted": len(ids), "done": done, "running": running,
            "pending": pending, "failed": failed}

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
    review = store.setting(_review_key(user_id), {}) or {}
    scope = saved.get("scope", "names_and_originals")
    reviewed = review.get("by_kind", {})
    eligible_types = ("display_changes",) if scope == "names" else ("display_changes", "original_updates")
    in_current_review = set(track_id for kind in eligible_types for track_id in reviewed.get(kind, []))
    already_in_review = sum(track_id in in_current_review for track_id in ids)
    return {"state": "failed" if failed else "running" if pending else "ready",
            "verified": ready and bool(saved.get("verified")),
            "track_count": len(ids), "review_tested": already_in_review,
            "pending": pending, "failed": failed,
            "include_localized": bool(saved.get("include_localized")),
            "scope": saved.get("scope", "names_and_originals"),
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


def queue(store, user_id, limit, include_localized=False, review_token=None,
          scope="names_and_originals"):
    if not 1 <= limit <= MAX_TRACKS:
        raise ValueError("Choose between 1 and 5000 tracks")
    store.require_user(user_id)
    if scope not in {"names", "names_and_originals"}:
        raise ValueError("Choose a supported type of language update")
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
                             old_canary.get("include_localized") != bool(include_localized)
                             or (review_token and old_canary.get("scope") != scope)):
            raise ValueError("Verify the three-track canary in Navidrome for this update scope before bulk application.")
        skipped = {"already_localized": 0, "manual_or_busy": 0, "invalid_or_missing": 0}
        if review_token:
            review = store.setting(_review_key(user_id), {}) or {}
            if (review.get("token") != review_token or
                    time.time() - review.get("created", 0) > 4 * 3600 or
                    bool(review.get("include_localized")) != bool(include_localized)):
                raise ValueError("Language review expired or changed. Run the read-only review again.")
            kinds = ("display_changes",) if scope == "names" else ("display_changes", "original_updates")
            selected_ids = [id for kind in kinds for id in review["by_kind"].get(kind, [])]
            if not is_canary:
                tested = set(old_canary.get("track_ids") or [])
                selected_ids = [id for id in selected_ids if id not in tested]
            # Recheck asset identity and eligibility even though the review is
            # recent. It is never sufficient authority to enqueue changed files.
            hashes = review.get("asset_hashes") or {}
            ready = []
            for track_id in selected_ids[:limit]:
                row = store.one(
                    """SELECT t.id,t.current_asset_id,t.operation_state,t.health,
                              t.metadata_edited,t.file_path,t.mbid,a.sha256 AS current_sha
                       FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
                       WHERE t.id=? AND t.user_id=?""", (track_id, user_id))
                if not row or not hashes.get(track_id) or row["current_sha"] != hashes[track_id]:
                    skipped["invalid_or_missing"] += 1
                elif row["metadata_edited"] or row["operation_state"] != "IDLE":
                    skipped["manual_or_busy"] += 1
                else:
                    ready.append(row)
            scanned = len(selected_ids)
            queue_progress.update(user_id, state="queuing", processed=scanned,
                                  total=scanned, eligible=len(ready))
        else:
            # Compatibility for existing API clients; the new Language UI always
            # passes an exact, time-limited review token and scope.
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
                store.queue_track(row["id"], user_id, {"mode": "language",
                                                        "review_token": review_token})
                queued_ids.append(row["id"])
                queue_progress.update(user_id, queued=len(queued_ids))
            except (ValueError, LookupError) as exc:
                skipped["manual_or_busy"] += 1
                store.event("WARNING", f"English-name {mode}: skipped track "
                            f"{row['id']} after an eligibility change: {exc}",
                            user_id, "language-review")
        if queued_ids:
            store.set_setting("english-language-batch:" + user_id,
                              {"ids": queued_ids, "mode": mode,
                               "scope": scope, "review_token": review_token,
                               "created": time.time()})
        if is_canary and queued_ids:
            store.set_setting(_canary_key(user_id),
                              {"policy": LANGUAGE_POLICY, "track_ids": queued_ids,
                               "include_localized": bool(include_localized),
                               "scope": scope if review_token else "names_and_originals",
                               "verified": False})
        store.event("INFO", f"English-name {mode}: "
                    f"{len(queued_ids)} track jobs queued; {sum(skipped.values())} excluded. "
                    "No downloads or identity changes; worker results will appear in Activity.",
                    user_id, "language-review")
        queue_progress.update(user_id, state="complete", queued=len(queued_ids),
                              skipped=skipped, error=None)
        return {"queued": len(queued_ids), "eligible_before": len(ready),
                "scanned": scanned, "skipped": skipped, "mode": mode, "scope": scope,
                "message": "English-first metadata jobs queued; no downloads or album guessing."}
    except Exception as exc:
        queue_progress.update(user_id, state="error", error=str(exc))
        store.event("ERROR", f"English-name {mode} request failed: {exc}",
                    user_id, "language-review")
        raise
