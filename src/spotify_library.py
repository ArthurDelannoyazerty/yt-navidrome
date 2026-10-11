"""Explicit Spotify-to-library links, serialized by the existing durable worker.

Spotify metadata never establishes MusicBrainz identity or a downloadable origin.
Name matches are suggestions which require the user's confirmation.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import unicodedata
import uuid
from pathlib import Path

from common import utcnow
from runtime import DeferredOperation

MODES = {"spotify_link", "spotify_audio"}


def normalized(title):
    # Do not discard edition/version markers, artist names, or punctuation.
    return " ".join(unicodedata.normalize("NFKC", title or "").casefold().split())


def metadata_only(con, track_id, user):
    track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)).fetchone()
    if not track:
        raise LookupError("Spotify entry not found for this library")
    origins = con.execute("SELECT * FROM track_origins WHERE track_id=?", (track_id,)).fetchall()
    if (track["file_path"] or track["current_asset_id"] or len(origins) != 1
            or origins[0]["provider"] != "spotify" or origins[0]["downloadable"]
            or con.execute("SELECT 1 FROM assets WHERE track_id=?", (track_id,)).fetchone()):
        raise ValueError("Only an unlinked, metadata-only Spotify entry can be linked")
    return dict(track), dict(origins[0])


def preview(store, user, page=1, limit=50):
    store.require_user(user)
    pending = store.rows(
        """SELECT t.id,o.title,t.updated_at FROM tracks t JOIN track_origins o ON o.track_id=t.id
           WHERE t.user_id=? AND o.provider='spotify' AND o.downloadable=0
             AND t.current_asset_id IS NULL AND t.file_path IS NULL
             AND t.operation_state IN ('IDLE','FAILED')
             AND (SELECT COUNT(*) FROM track_origins WHERE track_id=t.id)=1
             AND NOT EXISTS(SELECT 1 FROM assets WHERE track_id=t.id) ORDER BY o.title,t.id""", (user,),
    )
    targets = store.rows(
        """SELECT t.id,t.matched_title,t.mbid,t.current_asset_id FROM tracks t
           JOIN assets a ON a.id=t.current_asset_id AND a.state='CURRENT'
           WHERE t.user_id=? AND t.health='AVAILABLE' AND t.operation_state='IDLE'
             AND t.mbid IS NOT NULL AND t.mbid<>'' ORDER BY t.id""", (user,),
    )
    by_title = {}
    for target in targets:
        by_title.setdefault(normalized(target["matched_title"]), []).append(target)
    items = [{**track, "candidates": by_title.get(normalized(track["title"]), [])} for track in pending]
    revision = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
    return {"items": items[(page - 1) * limit:page * limit], "total": len(items),
            "page": page, "limit": limit, "revision": revision,
            "unique": sum(len(item["candidates"]) == 1 for item in items),
            "ambiguous": sum(len(item["candidates"]) > 1 for item in items),
            "unmatched": sum(not item["candidates"] for item in items)}


def queue_link(store, track_id, user, *, target_id=None, audio_url=None, expected=None):
    from providers import parse_source
    if bool(target_id) == bool(audio_url):
        raise ValueError("Choose one library track or one YouTube video")
    ref = parse_source(audio_url) if audio_url else None
    if ref and (ref.provider != "youtube" or ref.is_playlist):
        raise ValueError("An audio origin must be a single YouTube video, not a playlist or Spotify URL")
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        source, origin = metadata_only(con, track_id, user)
        if expected and expected.get("title", origin["title"]) != origin["title"]:
            raise ValueError("Spotify metadata changed; review the link again")
        if con.execute("SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track_id,)).fetchone():
            raise ValueError("This Spotify entry already has an active operation")
        payload = {"mode": "spotify_link", "origin_id": origin["id"], "source_title": origin["title"]}
        if target_id:
            target = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (target_id, user)).fetchone()
            if not target or not target["mbid"] or not target["current_asset_id"] or target["health"] != "AVAILABLE":
                raise ValueError("Choose an available, MusicBrainz-confirmed track from this library")
            if target["operation_state"] != "IDLE":
                raise ValueError("The chosen library track is busy; retry when its operation finishes")
            if expected and (expected.get("mbid", target["mbid"]) != target["mbid"] or expected.get("asset_id", target["current_asset_id"]) != target["current_asset_id"]):
                raise ValueError("The library candidate changed; review the link again")
            payload.update(target_id=target_id, target_mbid=target["mbid"], asset_id=target["current_asset_id"])
        else:
            existing = con.execute("SELECT id FROM track_origins WHERE user_id=? AND provider='youtube' AND media_key=?", (user, ref.key.split(":", 1)[1])).fetchone()
            if not existing:
                con.execute(
                    """INSERT OR IGNORE INTO sources(id,user_id,provider,source_key,url,title,is_playlist)
                       VALUES (?,?,'youtube',?,?,?,0)""",
                    (str(uuid.uuid4()), user, ref.key, ref.url, ref.key),
                )
                source_id = con.execute("SELECT id FROM sources WHERE user_id=? AND provider='youtube' AND source_key=?", (user, ref.key)).fetchone()[0]
                store.enqueue("sync", source_id, user, con=con)
            payload.update(mode="spotify_audio", youtube_key=ref.key.split(":", 1)[1])
            # This is metadata work, not a YouTube download; the circuit must not
            # postpone it based on the Spotify origin field.
        store.enqueue("track", track_id, user, payload, con=con)
        con.execute("UPDATE tracks SET operation_state='QUEUED',operation_kind=?,operation_error=NULL,updated_at=? WHERE id=?",
                    (payload["mode"].upper(), utcnow(), track_id))
    return {"message": "Spotify link queued; audio and identity remain separate"}


def queue_exact(store, user, revision):
    # Read all candidates once so queueing one item cannot invalidate later rows.
    matches = preview(store, user, limit=1000000)
    if matches["revision"] != revision:
        raise ValueError("Library matches changed; review the preview again before linking")
    queued = skipped = 0
    for item in matches["items"]:
        if len(item["candidates"]) != 1:
            continue
        try:
            candidate = item["candidates"][0]
            queue_link(store, item["id"], user, target_id=candidate["id"], expected={
                "title": item["title"], "mbid": candidate["mbid"], "asset_id": candidate["current_asset_id"],
            })
            queued += 1
        except (LookupError, ValueError):
            skipped += 1
    return {"message": f"Queued {queued} reviewed Spotify links; skipped {skipped}", "queued": queued, "skipped": skipped}


def cancel_waiting(store, track_id, user):
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        metadata_only(con, track_id, user)
        job = con.execute("SELECT * FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (track_id,)).fetchone()
        if not job or job["state"] != "PENDING" or json.loads(job["payload"]).get("mode") not in MODES:
            raise ValueError("Only a waiting Spotify link can be cancelled")
        con.execute("UPDATE jobs SET state='CANCELLED',finished_at=? WHERE id=?", (utcnow(), job["id"]))
        con.execute("UPDATE tracks SET operation_state='IDLE',operation_kind=NULL,operation_error=NULL,updated_at=? WHERE id=?", (utcnow(), track_id))
    return {"message": "Waiting link cancelled; independently queued YouTube imports were not cancelled"}


def audio_target(store, user, payload):
    target = store.one(
        """SELECT t.* FROM tracks t JOIN track_origins o ON o.track_id=t.id
           WHERE o.user_id=? AND o.provider='youtube' AND o.media_key=?""", (user, payload["youtube_key"]),
    )
    if not target:
        source = store.one("SELECT status FROM sources WHERE user_id=? AND provider='youtube' AND source_key=?", (user, "video:" + payload["youtube_key"]))
        if not source or source["status"] == "FAILED":
            raise ValueError("The selected YouTube source failed or was removed. Fix its source error, then retry the Spotify link")
        if source["status"] == "OK":
            raise ValueError("The YouTube source completed without an audio track; it may be ignored or deleted")
        raise DeferredOperation("Waiting for the selected YouTube source to finish syncing")
    if target["operation_state"] == "FAILED":
        raise ValueError("The selected YouTube import failed. Retry that track, then retry this Spotify link")
    if target["operation_state"] != "IDLE":
        raise DeferredOperation("Waiting for YouTube audio processing/metadata approval; approve the YouTube track when needed")
    if not target["mbid"] or not target["current_asset_id"]:
        raise ValueError("The YouTube import has no confirmed recording/audio; confirm its identity before linking")
    if normalized(payload["source_title"]) != normalized(target["matched_title"]):
        raise ValueError("The YouTube recording's artist/title differs from Spotify. Review it and use Link library track to confirm explicitly")
    return target


async def finish_link(pipeline, job, receipt, directory):
    store = pipeline.store
    if receipt["finalized_at"]:
        shutil.rmtree(directory, ignore_errors=True)
        return
    result = receipt["result"]
    target = store.owned("tracks", result["target_id"], job["user_id"])
    if target["mbid"] != result["mbid"] or target["current_asset_id"] != result["asset_id"]:
        raise ValueError("Linked target changed before finalization; manual review is required")
    directory.mkdir(parents=True, exist_ok=True)
    await pipeline.bridge(job, target, directory, "comment", path=target["file_path"])
    await pipeline.refresh_asset_fingerprint(target["id"])
    await pipeline.rewrite_track_playlists(target["id"])
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM tracks WHERE id=? AND user_id=? AND current_asset_id IS NULL AND NOT EXISTS(SELECT 1 FROM track_origins WHERE track_id=tracks.id)", (result["source_id"], job["user_id"]))
        con.execute("UPDATE tracks SET operation_state='IDLE',operation_kind=NULL,operation_error=NULL,updated_at=? WHERE id=? AND operation_kind='SPOTIFY_LINK'", (utcnow(), target["id"]))
        con.execute("UPDATE operation_receipts SET finalized_at=? WHERE operation_id=?", (utcnow(), receipt["operation_id"]))
    shutil.rmtree(directory, ignore_errors=True)
    pipeline.report(job)("INFO", "Linked Spotify discovery and playlists to existing confirmed audio")


async def process_link(pipeline, job, directory):
    store = pipeline.store
    payload = json.loads(job["payload"])
    operation = payload["operation"]
    receipt = store.operation_receipt(operation)
    if not receipt:
        with store.db() as con:
            source, origin = metadata_only(con, job["target"], job["user_id"])
        if origin["id"] != payload["origin_id"] or origin["title"] != payload["source_title"]:
            raise ValueError("Spotify entry changed; review the link again")
        target = (audio_target(store, job["user_id"], payload) if payload["mode"] == "spotify_audio"
                  else store.owned("tracks", payload["target_id"], job["user_id"]))
        if payload["mode"] == "spotify_link" and (target["mbid"] != payload["target_mbid"] or target["current_asset_id"] != payload["asset_id"]):
            raise ValueError("The selected library identity/audio changed; review the link again")
        if target["operation_state"] != "IDLE":
            raise DeferredOperation("Waiting for the chosen library track's current operation")
        if not await pipeline.usable_asset(target, store.current_asset(target["id"])):
            raise ValueError("The chosen audio is missing or changed; repair/redownload it before linking")
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            metadata_only(con, source["id"], job["user_id"])
            latest = con.execute("SELECT * FROM tracks WHERE id=?", (target["id"],)).fetchone()
            if (not latest or latest["operation_state"] != "IDLE" or latest["mbid"] != target["mbid"]
                    or latest["current_asset_id"] != target["current_asset_id"]
                    or con.execute("SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (target["id"],)).fetchone()):
                raise DeferredOperation("Library target changed or became busy; retrying the link later")
            con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (target["id"], origin["id"]))
            store._recompute_discovery_locked(con, target["id"])
            # A target lease prevents simultaneous API writes while tags/playlists
            # are finalized. Store.queue_track also honors this persisted marker.
            con.execute("UPDATE tracks SET operation_state='RUNNING',operation_kind='SPOTIFY_LINK',updated_at=? WHERE id=?", (utcnow(), target["id"]))
            store._record_receipt_locked(con, operation, job["user_id"], source["id"], target["id"], "spotify_link", {
                "kind": "SPOTIFY_LINKED", "source_id": source["id"], "target_id": target["id"],
                "asset_id": target["current_asset_id"], "mbid": target["mbid"],
            })
        receipt = store.operation_receipt(operation)
    try:
        await finish_link(pipeline, job, receipt, directory)
    except Exception as exc:
        # Completion stays retryable even after deleting the metadata placeholder.
        raise DeferredOperation(f"Spotify link committed; finalization will retry: {exc}") from exc
