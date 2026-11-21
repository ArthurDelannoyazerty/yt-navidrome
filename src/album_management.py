"""Explicit and reversible album-only tag updates, preserving file locations.

No recording identity, source origin, audio codec, or playlist membership is
changed. Copy-on-write metadata edits are backed by persistent undo files.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from common import LIBRARY, atomic_copy, sha256_file, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS album_previews (
 token TEXT PRIMARY KEY, user_id TEXT NOT NULL, release_id TEXT NOT NULL,
 items TEXT NOT NULL, created_at TEXT NOT NULL, expires_at REAL NOT NULL,
 consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS album_preview_expiry ON album_previews(expires_at);
CREATE TABLE IF NOT EXISTS album_changes (
 id TEXT PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE RESTRICT,
 release_id TEXT NOT NULL, old_release_id TEXT, release_tags TEXT NOT NULL,
 asset_id INTEGER NOT NULL, path TEXT NOT NULL, expected_sha TEXT NOT NULL,
 backup_path TEXT, after_sha TEXT, status TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT, reverted_at TEXT,
 error TEXT
);
CREATE INDEX IF NOT EXISTS album_change_user ON album_changes(user_id,created_at);
CREATE TABLE IF NOT EXISTS album_preferences (
 user_id TEXT NOT NULL REFERENCES users(name),
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 release_id TEXT NOT NULL, auto_apply INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, PRIMARY KEY(user_id,source_id)
);
"""


class AlbumManager:
    def __init__(self, store, catalogue):
        self.store = store
        self.catalogue = catalogue
        self.backup_root = store.path.parent / "album-undo"

    def init(self):
        with self.store.db() as con:
            con.executescript(SCHEMA)

    def _require_completed_migration(self):
        journal = self.store.path.parent / "stable-path-migration.json"
        if journal.is_file():
            phase = json.loads(journal.read_text()).get("state")
            if phase != "COMPLETE":
                raise ValueError("Navidrome stable-path scan not acknowledged: " + str(phase))

    def require_migration_complete(self):
        """Refuse album retagging during the move-only Navidrome checkpoint."""
        journal = self.store.path.parent / "stable-path-migration.json"
        if journal.is_file():
            state = json.loads(journal.read_text(encoding="utf-8")).get("state")
            if state != "COMPLETE":
                raise ValueError(f"Stable-path migration is not acknowledged ({state}); album edits are disabled")

    def _select(self, user_id, source_ids, track_ids):
        source_ids = list(dict.fromkeys(source_ids))
        track_ids = list(dict.fromkeys(track_ids))
        if len(source_ids) > 20 or len(track_ids) > 500 or not (source_ids or track_ids):
            raise ValueError("Select at least one source or track (up to 20 sources or 500 tracks)")
        with self.store.db() as con:
            if source_ids:
                marks = ",".join("?" for _ in source_ids)
                existing = con.execute(f"SELECT id FROM sources WHERE user_id=? AND id IN ({marks})",
                                       (user_id, *source_ids)).fetchall()
                if len(existing) != len(source_ids):
                    raise LookupError("Selected source does not belong to this user")
                track_ids.extend(row["id"] for row in con.execute(f"""SELECT DISTINCT t.id
                    FROM tracks t JOIN track_origins o ON o.track_id=t.id
                    JOIN source_origins so ON so.origin_id=o.id
                    WHERE t.user_id=? AND so.source_id IN ({marks})""", (user_id, *source_ids)))
            track_ids = list(dict.fromkeys(track_ids))
            if len(track_ids) > 1000:
                raise ValueError("Select fewer tracks for a single album operation")
            marks = ",".join("?" for _ in track_ids)
            if not marks:
                return []
            rows = con.execute(f"""SELECT t.id,t.user_id,t.mbid,t.release_id,t.updated_at,
                t.current_asset_id,t.file_path,t.health,t.operation_state,
                a.path,a.sha256,a.state AS asset_state,
                CASE WHEN locks.track_id IS NULL THEN 0 ELSE 1 END AS album_locked
                FROM tracks t LEFT JOIN assets a ON a.id=t.current_asset_id
                LEFT JOIN album_track_locks locks ON locks.track_id=t.id AND locks.user_id=t.user_id
                WHERE t.user_id=? AND t.id IN ({marks})""", (user_id, *track_ids)).fetchall()
            if len(rows) != len(track_ids):
                raise LookupError("Some selected music does not belong to this library")
            return [dict(row) for row in rows]

    def preview(self, user_id, release_id, source_ids=(), track_ids=(), *, persist=True):
        self.require_migration_complete()
        self.store.require_user(user_id)
        self._require_completed_migration()
        try:
            release_id = str(uuid.UUID(release_id))
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid MusicBrainz release ID") from None
        release = self.catalogue.get("release", release_id)
        if not release["entity"] or not release["tracks"]:
            raise ValueError("The complete release tracklist is not cached. Open the release in Catalogue and sync it first.")
        tracks = self._select(user_id, source_ids, track_ids)
        results = []
        for track in tracks:
            tags = self.catalogue.release_assignment(release_id, track["mbid"]) if track["mbid"] else None
            path = Path(track["path"] or "").resolve() if track.get("path") else None
            user_root = (LIBRARY / user_id).resolve()
            stable = path and path.parent == (user_root / "audio") and path.name == track["id"] + ".opus"
            existing_job = self.store.one("""SELECT 1 FROM jobs WHERE kind IN ('track','album')
               AND user_id=? AND state IN ('PENDING','RUNNING') AND
               (target=? OR target IN (SELECT id FROM album_changes WHERE track_id=?)) LIMIT 1""",
               (user_id, track["id"], track["id"]))
            if not tags:
                status = "INCOMPATIBLE"
            elif track["release_id"] == release_id:
                status = "UNCHANGED"
            elif track["album_locked"]:
                status = "LOCKED"
            elif not track["mbid"] or track["health"] != "AVAILABLE" or track["operation_state"] != "IDLE" or existing_job:
                status = "BUSY"
            elif not stable:
                status = "NEEDS_STABLE_PATH_MIGRATION"
            elif not path or not path.is_file() or not track["sha256"] or track["asset_state"] != "CURRENT":
                status = "UNAVAILABLE"
            else:
                status = "COMPATIBLE"
            results.append({"track_id": track["id"], "recording_id": track["mbid"],
                            "old_release_id": track["release_id"], "status": status,
                            "revision": track["updated_at"], "asset_id": track["current_asset_id"],
                            "sha256": track["sha256"], "path": track["path"],
                            "release_tags": tags})
        token = uuid.uuid4().hex if persist else None
        if persist:
            with self.store.db() as con:
                con.execute("INSERT INTO album_previews VALUES (?,?,?,?,?,?,NULL)",
                            (token, user_id, release_id, json.dumps(results), utcnow(), time.time() + 1200))
                con.execute("DELETE FROM album_previews WHERE expires_at<? AND consumed_at IS NOT NULL", (time.time() - 3600,))
        return {"preview_token": token, "release_id": release_id,
                "album": release["entity"]["name"], "items": results,
                "can_apply": sum(row["status"] == "COMPATIBLE" for row in results),
                "note": "Only compatible tracks change. File paths and recording IDs stay unchanged."}

    def apply(self, user_id, preview_token):
        self.require_migration_complete()
        self.store.require_user(user_id)
        self._require_completed_migration()
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            preview = con.execute("SELECT * FROM album_previews WHERE token=? AND user_id=?",
                                  (preview_token, user_id)).fetchone()
            if not preview or preview["consumed_at"] or preview["expires_at"] < time.time():
                raise ValueError("Album preview expired or was already submitted; review again")
            items = json.loads(preview["items"])
            selected = [item for item in items if item["status"] == "COMPATIBLE"]
            if not selected:
                raise ValueError("No compatible album changes in this preview")
            for item in selected:
                row = con.execute("""SELECT t.id,t.updated_at,t.release_id,t.current_asset_id,
                    t.operation_state,t.health,a.sha256,a.path FROM tracks t
                    JOIN assets a ON a.id=t.current_asset_id AND a.state='CURRENT'
                    WHERE t.user_id=? AND t.id=?""", (user_id, item["track_id"])).fetchone()
                if con.execute(
                    "SELECT 1 FROM album_track_locks WHERE user_id=? AND track_id=?",
                    (user_id,item["track_id"]),
                ).fetchone():
                    raise ValueError("Album locked: unlock this recording before reassignment")
                if not row or row["updated_at"] != item["revision"] or row["release_id"] != item["old_release_id"] or row["current_asset_id"] != item["asset_id"] or row["sha256"] != item["sha256"] or row["path"] != item["path"] or row["operation_state"] != "IDLE" or row["health"] != "AVAILABLE":
                    raise ValueError("A track or its audio changed since preview; review again")
                if con.execute("""SELECT 1 FROM jobs j LEFT JOIN album_changes c ON j.kind='album' AND c.id=j.target
                    WHERE j.state IN ('PENDING','RUNNING') AND (j.kind='track' AND j.target=?
                    OR j.kind='album' AND c.track_id=?) LIMIT 1""", (item["track_id"], item["track_id"])).fetchone():
                    raise ValueError("One of these tracks has an active operation; review again")
                if not self.catalogue.release_assignment(preview["release_id"], item["recording_id"]):
                    raise ValueError("The cached release tracklist changed; review again")
                if not Path(row["path"]).is_file() or sha256_file(Path(row["path"])) != item["sha256"]:
                    raise ValueError("The current audio changed on disk; review again")
            required_space = sum(Path(item["path"]).stat().st_size for item in selected)
            free_space = shutil.disk_usage(self.backup_root.parent).free
            if free_space < required_space + max(256 * 1024 * 1024, required_space // 10):
                raise ValueError("Insufficient free SSD space for reversible album backups")
            group_id = uuid.uuid4().hex
            for item in selected:
                change_id = uuid.uuid4().hex
                con.execute("""INSERT INTO album_changes
                    (id,group_id,user_id,track_id,release_id,old_release_id,release_tags,asset_id,path,
                     expected_sha,status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,'QUEUED',?)""",
                    (change_id, group_id, user_id, item["track_id"], preview["release_id"],
                     item["old_release_id"], json.dumps(item["release_tags"]), item["asset_id"],
                     item["path"], item["sha256"], utcnow()))
                if not self.store.enqueue("album", change_id, user_id, {"mode": "apply"}, con=con):
                    raise ValueError("Could not queue an album change safely")
            con.execute("UPDATE album_previews SET consumed_at=? WHERE token=?", (utcnow(), preview_token))
        return {"group_id": group_id, "queued": len(selected), "skipped": len(items) - len(selected)}

    def preference(self, user_id, source_id, release_id=None, *, enabled=False, clear=False):
        """One explicit opt-in per source. Suggestions never silently create rules."""
        self.store.require_user(user_id)
        self.store.owned("sources", source_id, user_id)
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            if clear:
                con.execute("DELETE FROM album_preferences WHERE user_id=? AND source_id=?", (user_id, source_id))
                return {"source_id": source_id, "removed": True}
            release_id = str(uuid.UUID(release_id))
            details = self.catalogue.get("release", release_id)
            if not details["entity"] or not details["tracks"]:
                raise ValueError("Sync the complete MusicBrainz release before choosing it")
            con.execute("""INSERT INTO album_preferences VALUES (?,?,?,?,?)
                ON CONFLICT(user_id,source_id) DO UPDATE SET release_id=excluded.release_id,
                auto_apply=excluded.auto_apply,updated_at=excluded.updated_at""",
                (user_id, source_id, release_id, int(bool(enabled)), utcnow()))
        return {"source_id": source_id, "release_id": release_id, "auto_apply": enabled}

    def preferences(self, user_id):
        self.store.require_user(user_id)
        return self.store.rows("SELECT * FROM album_preferences WHERE user_id=? ORDER BY source_id", (user_id,))

    def preferred_release(self, user_id, origin_id, recording_id):
        """Apply only preauthorized exact editions containing the confirmed recording."""
        if not origin_id or not recording_id:
            return None
        rows = self.store.rows("""SELECT DISTINCT p.release_id FROM album_preferences p
            JOIN source_origins so ON so.source_id=p.source_id
            JOIN sources s ON s.id=so.source_id AND s.user_id=p.user_id
            WHERE so.origin_id=? AND p.user_id=? AND p.auto_apply=1 AND s.status NOT IN ('REMOVED','PURGING','PURGE_FAILED')""",
            (origin_id, user_id))
        releases = {row["release_id"] for row in rows}
        if len(releases) != 1:
            # Conflicting preferences are never resolved by arbitrary ordering.
            return None
        only = next(iter(releases))
        return only if self.catalogue.release_assignment(only, recording_id) else None

    def history(self, user_id, limit=100):
        self.store.require_user(user_id)
        return self.store.rows("""SELECT id,group_id,track_id,release_id,old_release_id,status,
          created_at,completed_at,reverted_at,error FROM album_changes WHERE user_id=?
          ORDER BY created_at DESC,id DESC LIMIT ?""", (user_id, min(200, max(1, limit))))

    def undo(self, user_id, change_id):
        self.store.require_user(user_id)
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM album_changes WHERE id=? AND user_id=?", (change_id, user_id)).fetchone()
            if not row:
                raise LookupError("Album change not found in this library")
            if row["status"] != "DONE":
                raise ValueError("Only completed, unreverted album changes can be undone")
            track = con.execute("SELECT release_id,current_asset_id FROM tracks WHERE user_id=? AND id=?", (user_id, row["track_id"])).fetchone()
            asset = con.execute("SELECT sha256,path FROM assets WHERE id=? AND state='CURRENT'", (row["asset_id"],)).fetchone()
            if not track or track["release_id"] != row["release_id"] or track["current_asset_id"] != row["asset_id"] or not asset or asset["sha256"] != row["after_sha"] or asset["path"] != row["path"]:
                raise ValueError("The track changed after this album update; cannot restore older tags safely")
            if con.execute("""SELECT 1 FROM jobs j LEFT JOIN album_changes c ON j.kind='album' AND c.id=j.target
                WHERE j.state IN ('PENDING','RUNNING') AND (j.kind='track' AND j.target=? OR j.kind='album' AND c.track_id=?) LIMIT 1""",
                (row["track_id"], row["track_id"])).fetchone():
                raise ValueError("An operation is already active on this track")
            if not self.store.enqueue("album", change_id, user_id, {"mode": "undo"}, con=con):
                raise ValueError("An album operation is already active")
        return {"queued": True, "change_id": change_id}

    def retry(self, user_id, change_id):
        """Crash/retry recovery never starts another destructive operation."""
        self.store.require_user(user_id)
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM album_changes WHERE id=? AND user_id=?", (change_id, user_id)).fetchone()
            if not row:
                raise LookupError("Album operation not found")
            if row["status"] not in {"QUEUED", "PREPARED", "UNDO_PREPARED"}:
                raise ValueError("This album operation is not retryable")
            mode = "undo" if row["status"] == "UNDO_PREPARED" else "apply"
            if not self.store.enqueue("album", change_id, user_id, {"mode": mode}, con=con):
                raise ValueError("The album operation is already queued or running")
            con.execute("UPDATE album_changes SET error=NULL WHERE id=?", (change_id,))
            return {"queued": True, "change_id": change_id, "mode": mode}

    def _load_change(self, change_id, user_id):
        change = self.store.one("SELECT * FROM album_changes WHERE id=? AND user_id=?", (change_id, user_id))
        if not change:
            raise LookupError("Album change not found")
        return change

    def _paths(self, change):
        destination = Path(change["path"]).resolve()
        root = (LIBRARY / change["user_id"]).resolve()
        if destination.parent != root / "audio" or destination.name != change["track_id"] + ".opus":
            raise ValueError("Album edits require the stable library layout")
        backup_dir = self.backup_root / change["user_id"] / change["id"]
        backup_dir.mkdir(parents=True, exist_ok=True)
        return destination, backup_dir / "previous.opus", backup_dir / "new.opus"

    @staticmethod
    def _retag(path, tags):
        from mutagen.oggopus import OggOpus
        audio = OggOpus(str(path))
        for key in ("album", "albumartist", "musicbrainz_albumid", "musicbrainz_albumartistid",
                    "musicbrainz_albumartistids", "musicbrainz_releasegroupid",
                    "musicbrainz_releasetrackid", "tracknumber", "discnumber", "date", "year",
                    "discsubtitle", "disctotal", "tracktotal", "releasestatus"):
            audio.pop(key, None)
        updates = {
            "album": tags["album"],
            # The confirmed release's canonical name survives English aliases.
            "originalalbum": tags.get("native_album") or tags["album"],
            "musicbrainz_albumid": tags["release_id"],
            "tracknumber": str(tags["position"]),
            "discnumber": str(tags["medium"]),
        }
        optional = {"albumartist": tags.get("album_artist"),
                    "originalalbumartist": tags.get("native_album_artist"),
                    "musicbrainz_albumartistid": tags.get("album_artist_ids") or [],
                    "date": tags.get("date"),
                    "musicbrainz_releasegroupid": tags.get("release_group_id"),
                    "musicbrainz_releasetrackid": tags.get("track_id")}
        updates.update({key: value for key, value in optional.items() if value})
        for key, value in updates.items():
            audio[key] = [str(part) for part in value] if isinstance(value, list) else [str(value)]
        audio.save()

    def prepare(self, change_id, user_id, mode):
        self._require_completed_migration()
        change = self._load_change(change_id, user_id)
        destination, backup, staged = self._paths(change)
        if self.store.one(
            "SELECT 1 FROM album_track_locks WHERE user_id=? AND track_id=?",
            (user_id,change["track_id"]),
        ):
            raise ValueError("Album locked: unlock this recording before changing its album")
        digest = sha256_file(destination) if destination.is_file() else None
        if mode == "apply":
            if change["status"] == "DONE":
                return change
            if change["status"] not in {"QUEUED", "PREPARED"}:
                raise ValueError("Album update is not in an applicable state")
            if change["status"] == "QUEUED":
                if digest != change["expected_sha"]:
                    raise ValueError("Track content changed; album operation refused")
                track = self.store.owned("tracks", change["track_id"], user_id)
                if track["release_id"] != change["old_release_id"] or track["current_asset_id"] != change["asset_id"]:
                    raise ValueError("Recording or album changed; operation refused")
                atomic_copy(destination, backup)
                atomic_copy(destination, staged)
                self._retag(staged, json.loads(change["release_tags"]))
                after_sha = sha256_file(staged)
                with self.store.db() as con:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute("""UPDATE album_changes SET status='PREPARED',backup_path=?,after_sha=?
                        WHERE id=? AND status='QUEUED'""", (str(backup), after_sha, change_id))
                change = self._load_change(change_id, user_id)
            if digest == change["expected_sha"]:
                if not staged.is_file() or sha256_file(staged) != change["after_sha"]:
                    raise ValueError("Prepared album metadata is missing or changed")
                atomic_copy(staged, destination)
            elif digest != change["after_sha"]:
                raise ValueError("External audio change detected during album update")
            return change
        if mode == "undo":
            if change["status"] == "UNDONE":
                return change
            if change["status"] not in {"DONE", "UNDO_PREPARED"}:
                raise ValueError("Album change is not eligible for undo")
            if not backup.is_file() or sha256_file(backup) != change["expected_sha"]:
                raise ValueError("Original audio backup is unavailable or corrupted")
            if digest == change["after_sha"]:
                track = self.store.owned("tracks", change["track_id"], user_id)
                if track["release_id"] != change["release_id"]:
                    raise ValueError("Album was changed again; cannot undo safely")
                with self.store.db() as con:
                    con.execute("UPDATE album_changes SET status='UNDO_PREPARED' WHERE id=?", (change_id,))
                atomic_copy(backup, destination)
            elif digest != change["expected_sha"]:
                raise ValueError("Audio changed after album approval; undo refused")
            return self._load_change(change_id, user_id)
        raise ValueError("Invalid album operation mode")

    def finalize(self, change_id, user_id, mode, beets_id):
        change = self._load_change(change_id, user_id)
        destination, backup, staged = self._paths(change)
        expected = change["after_sha"] if mode == "apply" else change["expected_sha"]
        if sha256_file(destination) != expected:
            raise ValueError("Audio differs from the prepared album operation")
        stat = destination.stat()
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (change["track_id"], user_id)).fetchone()
            asset = con.execute("SELECT * FROM assets WHERE id=? AND state='CURRENT'", (change["asset_id"],)).fetchone()
            if not track or not asset or track["current_asset_id"] != change["asset_id"] or asset["path"] != str(destination):
                raise ValueError("Current track or asset changed during album commit")
            if mode == "apply" and change["status"] == "DONE":
                return
            if mode == "undo" and change["status"] == "UNDONE":
                return
            if mode == "apply" and (track["release_id"] != change["old_release_id"] or asset["sha256"] != change["expected_sha"]):
                raise ValueError("Track album or asset changed during album update")
            if mode == "undo" and (track["release_id"] != change["release_id"] or asset["sha256"] != change["after_sha"]):
                raise ValueError("Track changed since album assignment; undo refused")
            now = utcnow()
            con.execute("UPDATE assets SET sha256=?,size_bytes=?,mtime_ns=? WHERE id=?",
                        (expected, stat.st_size, stat.st_mtime_ns, change["asset_id"]))
            con.execute("UPDATE tracks SET release_id=?,beets_id=?,updated_at=? WHERE id=?",
                        (change["release_id"] if mode == "apply" else change["old_release_id"],
                         beets_id if beets_id is not None else track["beets_id"], now, change["track_id"]))
            if mode == "apply":
                con.execute("UPDATE album_changes SET status='DONE',completed_at=?,error=NULL WHERE id=?",
                            (now, change_id))
            else:
                con.execute("UPDATE album_changes SET status='UNDONE',reverted_at=?,error=NULL WHERE id=?",
                            (now, change_id))
        if staged.is_file():
            staged.unlink(missing_ok=True)


async def process(pipeline, job):
    """Run on the durable job worker; restart resumes a prepared copy safely."""
    import asyncio
    manager = pipeline.album_manager
    payload = json.loads(job["payload"])
    mode = payload.get("mode")
    change = manager._load_change(job["target"], job["user_id"])
    await asyncio.to_thread(manager.prepare, change["id"], job["user_id"], mode)
    change = manager._load_change(change["id"], job["user_id"])
    directory = manager.backup_root / job["user_id"] / change["id"]
    track = pipeline.store.owned("tracks", change["track_id"], job["user_id"])
    # Beets refresh reads existing tags and updates the library without item.move().
    refreshed = await pipeline.bridge(job, track, directory, "album_refresh", path=change["path"])
    await asyncio.to_thread(manager.finalize, change["id"], job["user_id"], mode, refreshed.get("beets_id"))
