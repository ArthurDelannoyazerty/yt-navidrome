"""Durable application state for sources, logical tracks, origins and assets."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import STATE, utcnow, user_name

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(name),
 provider TEXT NOT NULL,
 source_key TEXT NOT NULL,
 url TEXT NOT NULL,
 title TEXT NOT NULL,
 is_playlist INTEGER NOT NULL,
 monitored INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'PENDING',
 error TEXT,
 synced_at TEXT,
 next_sync REAL NOT NULL DEFAULT 0,
 playlist_path TEXT,
 UNIQUE(user_id, provider, source_key)
);

CREATE TABLE IF NOT EXISTS tracks (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(name),
 title TEXT NOT NULL,
 first_seen_at TEXT,
 playlist_discovered_at TEXT,
 discovered_at TEXT,
 discovery_basis TEXT NOT NULL,
 health TEXT NOT NULL DEFAULT 'UNAVAILABLE',
 operation_state TEXT NOT NULL DEFAULT 'IDLE',
 operation_kind TEXT,
 operation_error TEXT,
 file_path TEXT,
 temp_path TEXT,
 choices TEXT NOT NULL DEFAULT '[]',
 selected TEXT,
 pending_operation TEXT,
 beets_id INTEGER,
 matched_title TEXT,
 mbid TEXT,
 release_id TEXT,
 current_origin_id TEXT,
 current_asset_id INTEGER,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_track_per_recording
 ON tracks(user_id, mbid) WHERE mbid IS NOT NULL AND mbid <> '';
CREATE INDEX IF NOT EXISTS track_user_health
 ON tracks(user_id, health, operation_state);

CREATE TABLE IF NOT EXISTS track_origins (
 id TEXT PRIMARY KEY,
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(name),
 provider TEXT NOT NULL,
 media_key TEXT NOT NULL,
 url TEXT NOT NULL,
 title TEXT NOT NULL,
 downloadable INTEGER NOT NULL DEFAULT 1,
 availability TEXT NOT NULL DEFAULT 'UNKNOWN',
 first_seen_at TEXT NOT NULL,
 playlist_discovered_at TEXT,
 last_seen_at TEXT NOT NULL,
 last_download_at TEXT,
 last_error TEXT,
 UNIQUE(user_id, provider, media_key)
);
CREATE INDEX IF NOT EXISTS origin_track ON track_origins(track_id);

CREATE TABLE IF NOT EXISTS memberships (
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 entry_id TEXT NOT NULL,
 origin_id TEXT NOT NULL REFERENCES track_origins(id) ON DELETE CASCADE,
 added_at TEXT,
 position INTEGER NOT NULL,
 PRIMARY KEY(source_id, entry_id)
);
CREATE INDEX IF NOT EXISTS membership_origin ON memberships(origin_id);

CREATE TABLE IF NOT EXISTS assets (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 origin_id TEXT REFERENCES track_origins(id) ON DELETE SET NULL,
 path TEXT NOT NULL,
 state TEXT NOT NULL,
 created_at TEXT NOT NULL,
 activated_at TEXT,
 replaced_at TEXT,
 sha256 TEXT,
 size_bytes INTEGER,
 mtime_ns INTEGER,
 downloader_name TEXT,
 downloader_version TEXT,
 source_url TEXT,
 error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_asset
 ON assets(track_id) WHERE state='CURRENT';
CREATE INDEX IF NOT EXISTS asset_track ON assets(track_id, state);

CREATE TABLE IF NOT EXISTS identity_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL,
 track_id TEXT,
 origin_id TEXT,
 origin_provider TEXT,
 origin_media_key TEXT,
 origin_url TEXT,
 previous_mbid TEXT,
 previous_release_id TEXT,
 previous_title TEXT,
 new_mbid TEXT,
 new_release_id TEXT,
 new_title TEXT,
 changed_at TEXT NOT NULL,
 reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS identity_history_track
 ON identity_history(user_id, track_id, changed_at);

CREATE TABLE IF NOT EXISTS ignored_tracks (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(name),
 mbid TEXT,
 title TEXT NOT NULL,
 created_at TEXT NOT NULL,
 reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ignored_recording
 ON ignored_tracks(user_id, mbid) WHERE mbid IS NOT NULL AND mbid <> '';

CREATE TABLE IF NOT EXISTS ignored_origins (
 user_id TEXT NOT NULL REFERENCES users(name),
 provider TEXT NOT NULL,
 media_key TEXT NOT NULL,
 url TEXT NOT NULL,
 title TEXT NOT NULL,
 ignored_track_id TEXT NOT NULL REFERENCES ignored_tracks(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL,
 PRIMARY KEY(user_id, provider, media_key)
);

CREATE TABLE IF NOT EXISTS integrity_issues (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL REFERENCES users(name),
 issue_key TEXT NOT NULL,
 track_id TEXT REFERENCES tracks(id) ON DELETE CASCADE,
 asset_id INTEGER REFERENCES assets(id) ON DELETE SET NULL,
 kind TEXT NOT NULL,
 severity TEXT NOT NULL,
 message TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '{}',
 detected_at TEXT NOT NULL,
 resolved_at TEXT,
 UNIQUE(user_id, issue_key)
);
CREATE INDEX IF NOT EXISTS integrity_active
 ON integrity_issues(user_id, resolved_at, severity);

CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL,
 kind TEXT NOT NULL,
 target TEXT NOT NULL,
 payload TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'PENDING',
 error TEXT,
 created_at TEXT NOT NULL,
 finished_at TEXT,
 not_before REAL NOT NULL DEFAULT 0,
 defer_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job
 ON jobs(kind, target) WHERE state IN ('PENDING', 'RUNNING');

CREATE TABLE IF NOT EXISTS operation_receipts (
 operation_id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL,
 source_track_id TEXT,
 target_track_id TEXT,
 action TEXT NOT NULL,
 result TEXT NOT NULL DEFAULT '{}',
 completed_at TEXT NOT NULL,
 finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS operation_receipt_user
 ON operation_receipts(user_id, completed_at);

CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 at TEXT NOT NULL,
 level TEXT NOT NULL,
 message TEXT NOT NULL,
 user_id TEXT,
 target TEXT
);

CREATE TABLE IF NOT EXISTS settings (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_clock (
 host TEXT PRIMARY KEY,
 next_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
 day TEXT PRIMARY KEY,
 used INTEGER NOT NULL
);
"""

DROP_ORDER = [
    "integrity_issues", "ignored_origins", "ignored_tracks", "identity_history",
    "assets", "memberships", "track_origins", "tracks", "operation_receipts",
    "jobs", "sources",
    "events", "settings", "api_clock", "quota", "users",
]


def redact(message: Any) -> str:
    """Remove configured API secrets and common sensitive URL parameters."""
    message = str(message)
    for key in ("YT_API_KEY", "ACOUSTID_API_KEY"):
        if os.getenv(key):
            message = message.replace(os.environ[key], "[REDACTED]")
    return re.sub(
        r"(?i)([?&](?:key|token|sig|signature|expire|client)=)[^&\s]+",
        r"\1[REDACTED]",
        message,
    )


def _earliest(*values: str | None) -> str | None:
    present = [value for value in values if value]
    return min(present) if present else None


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE / "ingestor.sqlite"

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def _tables(self, con) -> set[str]:
        return {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.execute("PRAGMA journal_mode=WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            tables = self._tables(con)

            if version == 0 and tables:
                # Version 0 is the pre-provider-aware schema. The user explicitly
                # chose a fresh start; refuse to silently destroy a populated DB.
                populated = 0
                for table in ("tracks", "sources", "memberships"):
                    if table in tables:
                        populated += con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if populated:
                    raise RuntimeError(
                        "The unversioned database contains data. Back it up and migrate or "
                        "start with an empty state directory before using schema v2."
                    )
                con.execute("PRAGMA foreign_keys=OFF")
                for table in DROP_ORDER:
                    con.execute(f"DROP TABLE IF EXISTS {table}")
                con.execute("PRAGMA foreign_keys=ON")
                tables = set()

            if version not in (0, 1, SCHEMA_VERSION):
                raise RuntimeError(
                    f"Unsupported database schema {version}; expected {SCHEMA_VERSION}. "
                    "Restore a backup or run a supported migration."
                )

            if version == 1:
                # v2 only extends the durable job queue. Back up the live SQLite
                # database before touching it so an image rollback has a recovery path.
                backup_dir = self.path.parent / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                destination = backup_dir / f"ingestor-schema-v1-{int(time.time())}.sqlite"
                target = sqlite3.connect(destination)
                try:
                    con.backup(target)
                finally:
                    target.close()

                con.execute(
                    "ALTER TABLE jobs ADD COLUMN not_before REAL NOT NULL DEFAULT 0"
                )
                con.execute(
                    "ALTER TABLE jobs ADD COLUMN defer_count INTEGER NOT NULL DEFAULT 0"
                )

                # v1 could accidentally requeue a track that was already waiting for
                # approval. If its staged candidate is still present, restore the
                # approval instead of letting the duplicate ingest delete that work.
                for row in con.execute(
                    """SELECT id,pending_operation,temp_path,choices
                       FROM tracks
                       WHERE operation_state IN ('QUEUED','RUNNING')
                         AND pending_operation IS NOT NULL
                         AND temp_path IS NOT NULL
                         AND choices <> '[]'"""
                ).fetchall():
                    if not Path(row["temp_path"]).is_file():
                        continue
                    try:
                        pending = json.loads(row["pending_operation"] or "{}")
                    except (TypeError, ValueError):
                        continue
                    action = str(pending.get("action") or "ingest").upper()
                    con.execute(
                        """UPDATE jobs SET state='CANCELLED',finished_at=?,error=?
                           WHERE kind='track' AND target=?
                             AND state IN ('PENDING','RUNNING')""",
                        (
                            utcnow(),
                            "Cancelled during schema v2 migration: preserved staged approval candidate",
                            row["id"],
                        ),
                    )
                    con.execute(
                        """UPDATE tracks SET operation_state='NEEDS_APPROVAL',
                           operation_kind=?,operation_error=NULL,updated_at=?
                           WHERE id=?""",
                        (action, utcnow(), row["id"]),
                    )
                con.execute("PRAGMA user_version=2")
                version = 2

            con.executescript(SCHEMA)
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            con.executemany(
                "INSERT OR IGNORE INTO users VALUES (?)",
                [("admin",), ("guest",)],
            )

    def backup_database(self, label: str) -> Path:
        """Create a consistent SQLite backup for future explicit migrations."""
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        destination = backup_dir / f"ingestor-{label}-{int(time.time())}.sqlite"
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def rows(self, sql, args=()):
        with self.db() as con:
            return [dict(row) for row in con.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.db() as con:
            return con.execute(sql, args).rowcount

    def update(self, table, row_id, **values):
        if table not in {
            "tracks", "sources", "jobs", "track_origins", "assets",
            "integrity_issues",
        }:
            raise ValueError("Invalid table")
        for field in ("error", "operation_error", "last_error"):
            if values.get(field):
                values[field] = redact(values[field])
        if not values:
            return 0
        assignments = ",".join(f"{key}=?" for key in values)
        return self.execute(
            f"UPDATE {table} SET {assignments} WHERE id=?",
            (*values.values(), row_id),
        )

    def add_user(self, name):
        self.execute("INSERT OR IGNORE INTO users VALUES (?)", (user_name(name),))

    def require_user(self, name):
        user_name(name)
        if not self.one("SELECT name FROM users WHERE name=?", (name,)):
            raise ValueError("Unknown user")
        return name

    def owned(self, table, target, user):
        if table not in {"sources", "tracks", "jobs", "track_origins"}:
            raise ValueError("Invalid table")
        row = self.one(
            f"SELECT * FROM {table} WHERE id=? AND user_id=?",
            (target, user),
        )
        if not row:
            raise LookupError("Item not found for this user")
        return row

    def event(self, level, message, user=None, target=None):
        message = redact(message)
        self.execute(
            "INSERT INTO events(at,level,message,user_id,target) VALUES (?,?,?,?,?)",
            (utcnow(), level, message[:16000], user, target),
        )
        print(f"{level} {message}", flush=True)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.execute(
            "INSERT INTO settings VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def enqueue(self, kind, target, user, payload=None, con=None):
        payload = dict(payload or {})
        if kind == "track":
            payload.setdefault("operation", uuid.uuid4().hex)
        args = (user, kind, target, json.dumps(payload), utcnow())
        sql = (
            "INSERT OR IGNORE INTO jobs(user_id,kind,target,payload,created_at) "
            "VALUES (?,?,?,?,?)"
        )
        if con is not None:
            return con.execute(sql, args).rowcount > 0
        return self.execute(sql, args) > 0

    def defer_job(self, job_id: int, reason: str, *, retry_at: float | None = None,
                  base_delay: float = 60.0) -> float:
        """Return a running job to the durable queue without losing staged work."""
        reason = redact(reason)
        now = time.time()
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise LookupError("Job not found")
            count = int(job["defer_count"] or 0) + 1
            if retry_at is None:
                retry_at = now + min(1800.0, base_delay * (2 ** min(count - 1, 5)))
            retry_at = max(float(retry_at), now + 1.0)
            con.execute(
                """UPDATE jobs SET state='PENDING',error=?,finished_at=NULL,
                   not_before=?,defer_count=? WHERE id=?""",
                (reason, retry_at, count, job_id),
            )
            if job["kind"] == "track":
                con.execute(
                    """UPDATE tracks SET operation_state='DEFERRED',
                       operation_error=?,updated_at=? WHERE id=?""",
                    (reason, utcnow(), job["target"]),
                )
            elif job["kind"] == "sync":
                con.execute(
                    "UPDATE sources SET status='DEFERRED',error=? WHERE id=?",
                    (reason, job["target"]),
                )
        return retry_at

    def defer_pending_youtube_jobs(self, retry_at: float, reason: str) -> int:
        """Pause queued YouTube downloads when the shared circuit breaker opens."""
        reason = redact(reason)
        changed = 0
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            pending = con.execute(
                "SELECT id,target,payload,not_before FROM jobs "
                "WHERE kind='track' AND state='PENDING'"
            ).fetchall()
            for job in pending:
                try:
                    origin_id = json.loads(job["payload"] or "{}").get("origin_id")
                except (TypeError, ValueError):
                    continue
                if not origin_id:
                    continue
                origin = con.execute(
                    "SELECT provider FROM track_origins WHERE id=?",
                    (origin_id,),
                ).fetchone()
                if not origin or origin["provider"] != "youtube":
                    continue
                con.execute(
                    "UPDATE jobs SET not_before=MAX(not_before,?),error=? WHERE id=?",
                    (retry_at, reason, job["id"]),
                )
                con.execute(
                    """UPDATE tracks SET operation_state='DEFERRED',
                       operation_error=?,updated_at=?
                       WHERE id=? AND operation_state='QUEUED'""",
                    (reason, utcnow(), job["target"]),
                )
                changed += 1
        return changed

    def operation_receipt(self, operation_id: str | None):
        if not operation_id:
            return None
        row = self.one(
            "SELECT * FROM operation_receipts WHERE operation_id=?",
            (operation_id,),
        )
        if row:
            row["result"] = json.loads(row["result"] or "{}")
        return row

    @staticmethod
    def _record_receipt_locked(con, operation_id: str, user: str,
                               source_track_id: str | None,
                               target_track_id: str | None,
                               action: str, result: dict,
                               finalized: bool = False):
        con.execute(
            """INSERT INTO operation_receipts(
               operation_id,user_id,source_track_id,target_track_id,action,result,
               completed_at,finalized_at
            ) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(operation_id) DO NOTHING""",
            (
                operation_id, user, source_track_id, target_track_id,
                action.upper(), json.dumps(result), utcnow(),
                utcnow() if finalized else None,
            ),
        )

    def finalize_receipt(self, operation_id: str):
        self.execute(
            "UPDATE operation_receipts SET finalized_at=COALESCE(finalized_at,?) "
            "WHERE operation_id=?",
            (utcnow(), operation_id),
        )

    def receipts_with_followups(self):
        rows = self.rows(
            "SELECT * FROM operation_receipts WHERE finalized_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 2000"
        )
        result = []
        for row in rows:
            row["result"] = json.loads(row["result"] or "{}")
            if row["result"].get("reingest_origin_id"):
                result.append(row)
        return result

    def claim(self):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT * FROM jobs
                   WHERE state='PENDING' AND not_before<=?
                   ORDER BY id LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET state='RUNNING',error=NULL WHERE id=?", (row["id"],))
            if row["kind"] == "track":
                con.execute(
                    """UPDATE tracks SET operation_state='RUNNING',
                       operation_error=NULL,updated_at=? WHERE id=?""",
                    (utcnow(), row["target"]),
                )
            return dict(row)

    def recover(self):
        self.execute("UPDATE jobs SET state='PENDING' WHERE state='RUNNING'")
        self.execute(
            "UPDATE tracks SET operation_state='QUEUED',updated_at=? "
            "WHERE operation_state='RUNNING'",
            (utcnow(),),
        )
        self.execute("UPDATE sources SET status='PENDING' WHERE status='SYNCING'")

    def add_source(self, ref, user, monitored=False, label=None):
        self.require_user(user)
        with self.db() as con:
            con.execute(
                """INSERT INTO sources(
                    id,user_id,provider,source_key,url,title,is_playlist,monitored
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(user_id,provider,source_key)
                DO UPDATE SET monitored=MAX(monitored,excluded.monitored)""",
                (
                    str(uuid.uuid4()), user, ref.provider, ref.key, ref.url,
                    label or ref.key, int(ref.is_playlist), int(monitored),
                ),
            )
            row = con.execute(
                "SELECT * FROM sources WHERE user_id=? AND provider=? AND source_key=?",
                (user, ref.provider, ref.key),
            ).fetchone()
            self.enqueue("sync", row["id"], user, con=con)
            return dict(row)

    @staticmethod
    def _origin_ignored(con, user: str, provider: str, media_key: str) -> bool:
        return bool(con.execute(
            "SELECT 1 FROM ignored_origins WHERE user_id=? AND provider=? AND media_key=?",
            (user, provider, media_key),
        ).fetchone())

    @staticmethod
    def _recompute_discovery_locked(con, track_id: str) -> bool:
        track = con.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not track:
            return False
        origin_first = con.execute(
            "SELECT MIN(first_seen_at) FROM track_origins WHERE track_id=?",
            (track_id,),
        ).fetchone()[0]
        origin_playlist = con.execute(
            "SELECT MIN(playlist_discovered_at) FROM track_origins WHERE track_id=? "
            "AND playlist_discovered_at IS NOT NULL",
            (track_id,),
        ).fetchone()[0]
        membership_playlist = con.execute(
            """SELECT MIN(m.added_at)
               FROM memberships m
               JOIN sources s ON s.id=m.source_id
               JOIN track_origins o ON o.id=m.origin_id
               WHERE o.track_id=? AND s.is_playlist=1 AND m.added_at IS NOT NULL""",
            (track_id,),
        ).fetchone()[0]
        first_seen = _earliest(track["first_seen_at"], origin_first) or utcnow()
        playlist_seen = _earliest(
            track["playlist_discovered_at"], origin_playlist, membership_playlist
        )
        discovered = playlist_seen or first_seen
        basis = "playlist_added" if playlist_seen else "first_seen"
        changed = (
            discovered != track["discovered_at"]
            or basis != track["discovery_basis"]
            or first_seen != track["first_seen_at"]
            or playlist_seen != track["playlist_discovered_at"]
        )
        if changed:
            con.execute(
                """UPDATE tracks SET first_seen_at=?,playlist_discovered_at=?,
                   discovered_at=?,discovery_basis=?,updated_at=? WHERE id=?""",
                (first_seen, playlist_seen, discovered, basis, utcnow(), track_id),
            )
        return changed

    def apply_snapshot(self, source, snapshot):
        """Commit a complete provider snapshot; one origin is downloaded at most once."""
        now = utcnow()
        stats = {"entries": len(snapshot.entries), "created": 0, "ignored": 0}
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM memberships WHERE source_id=?", (source["id"],))
            touched: set[str] = set()
            for entry in snapshot.entries:
                if self._origin_ignored(
                    con, source["user_id"], source["provider"], entry.key
                ):
                    stats["ignored"] += 1
                    continue

                origin = con.execute(
                    """SELECT o.*,t.health,t.file_path,t.operation_state
                       FROM track_origins o JOIN tracks t ON t.id=o.track_id
                       WHERE o.user_id=? AND o.provider=? AND o.media_key=?""",
                    (source["user_id"], source["provider"], entry.key),
                ).fetchone()
                playlist_added = entry.added_at if entry.basis == "playlist_added" else None
                if origin is None:
                    track_id = str(uuid.uuid4())
                    origin_id = str(uuid.uuid4())
                    first_seen = entry.added_at if entry.basis == "first_seen" else now
                    discovered = playlist_added or first_seen
                    basis = "playlist_added" if playlist_added else "first_seen"
                    operation_state = "QUEUED" if entry.downloadable else "IDLE"
                    operation_kind = "INGEST" if entry.downloadable else None
                    con.execute(
                        """INSERT INTO tracks(
                           id,user_id,title,first_seen_at,playlist_discovered_at,
                           discovered_at,discovery_basis,health,operation_state,
                           operation_kind,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,'UNAVAILABLE',?,?,?,?)""",
                        (
                            track_id, source["user_id"], entry.title, first_seen,
                            playlist_added, discovered, basis, operation_state,
                            operation_kind, now, now,
                        ),
                    )
                    con.execute(
                        """INSERT INTO track_origins(
                           id,track_id,user_id,provider,media_key,url,title,downloadable,
                           availability,first_seen_at,playlist_discovered_at,last_seen_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            origin_id, track_id, source["user_id"], source["provider"],
                            entry.key, entry.url, entry.title, int(entry.downloadable),
                            "UNKNOWN", first_seen, playlist_added, now,
                        ),
                    )
                    if entry.downloadable:
                        self.enqueue(
                            "track", track_id, source["user_id"],
                            {"mode": "ingest", "origin_id": origin_id}, con=con,
                        )
                    stats["created"] += 1
                else:
                    track_id = origin["track_id"]
                    origin_id = origin["id"]
                    first_seen = _earliest(
                        origin["first_seen_at"],
                        entry.added_at if entry.basis == "first_seen" else now,
                    )
                    origin_playlist = _earliest(
                        origin["playlist_discovered_at"], playlist_added
                    )
                    con.execute(
                        """UPDATE track_origins SET url=?,title=?,downloadable=?,
                           first_seen_at=?,playlist_discovered_at=?,last_seen_at=?
                           WHERE id=?""",
                        (
                            entry.url, entry.title, int(entry.downloadable), first_seen,
                            origin_playlist, now, origin_id,
                        ),
                    )
                    # Source refreshes must never steal a track from an explicit
                    # operation state. NEEDS_APPROVAL has no current asset yet, so
                    # health remains UNAVAILABLE while its candidate waits in staging.
                    if (
                        entry.downloadable
                        and origin["health"] in {"UNAVAILABLE", "MISSING"}
                        and origin["operation_state"] == "IDLE"
                    ):
                        queued = self.enqueue(
                            "track", track_id, source["user_id"],
                            {"mode": "ingest", "origin_id": origin_id}, con=con,
                        )
                        if queued:
                            con.execute(
                                """UPDATE tracks SET operation_state='QUEUED',
                                   operation_kind='INGEST',operation_error=NULL,updated_at=?
                                   WHERE id=?""",
                                (now, track_id),
                            )

                con.execute(
                    "INSERT INTO memberships VALUES (?,?,?,?,?)",
                    (source["id"], entry.entry_id, origin_id, entry.added_at, entry.position),
                )
                touched.add(track_id)

            for track_id in touched:
                changed = self._recompute_discovery_locked(con, track_id)
                track = con.execute(
                    "SELECT file_path,operation_state FROM tracks WHERE id=?",
                    (track_id,),
                ).fetchone()
                if (
                    changed
                    and track
                    and track["file_path"]
                    and track["operation_state"] == "IDLE"
                ):
                    if self.enqueue(
                        "track", track_id, source["user_id"], {"mode": "comment"}, con=con
                    ):
                        con.execute(
                            """UPDATE tracks SET operation_state='QUEUED',
                               operation_kind='COMMENT',operation_error=NULL,updated_at=?
                               WHERE id=?""",
                            (now, track_id),
                        )

            con.execute(
                "UPDATE sources SET title=?,status='OK',error=NULL,synced_at=? WHERE id=?",
                (snapshot.title, now, source["id"]),
            )
        return stats

    def origins_for_track(self, track_id: str, user: str | None = None):
        if user is None:
            return self.rows(
                "SELECT * FROM track_origins WHERE track_id=? ORDER BY last_download_at DESC,id",
                (track_id,),
            )
        return self.rows(
            "SELECT * FROM track_origins WHERE track_id=? AND user_id=? "
            "ORDER BY last_download_at DESC,id",
            (track_id, user),
        )

    def current_asset(self, track_id: str):
        return self.one(
            "SELECT * FROM assets WHERE track_id=? AND state='CURRENT'",
            (track_id,),
        )

    @staticmethod
    def _select_origin_locked(con, track_id: str, user: str, origin_id: str | None):
        if origin_id:
            origin = con.execute(
                "SELECT * FROM track_origins WHERE id=? AND track_id=? AND user_id=?",
                (origin_id, track_id, user),
            ).fetchone()
        else:
            track = con.execute(
                "SELECT current_origin_id FROM tracks WHERE id=? AND user_id=?",
                (track_id, user),
            ).fetchone()
            origin = None
            if track and track["current_origin_id"]:
                origin = con.execute(
                    "SELECT * FROM track_origins WHERE id=? AND track_id=? AND user_id=?",
                    (track["current_origin_id"], track_id, user),
                ).fetchone()
            if origin is None:
                origin = con.execute(
                    """SELECT * FROM track_origins
                       WHERE track_id=? AND user_id=? AND downloadable=1
                       ORDER BY last_download_at IS NULL, last_download_at DESC, id LIMIT 1""",
                    (track_id, user),
                ).fetchone()
        if not origin:
            raise ValueError("No matching download origin exists for this track")
        if not origin["downloadable"]:
            raise ValueError("The selected origin provides discovery metadata but no downloadable audio")
        return origin

    def queue_track(self, track_id: str, user: str, payload: dict):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track = con.execute(
                "SELECT * FROM tracks WHERE id=? AND user_id=?",
                (track_id, user),
            ).fetchone()
            if not track:
                raise LookupError("Track not found for this user")
            if con.execute(
                "SELECT 1 FROM jobs WHERE kind='track' AND target=? "
                "AND state IN ('PENDING','RUNNING')",
                (track_id,),
            ).fetchone():
                raise ValueError("This track already has an active job")

            payload = dict(payload)
            mode = payload.get("mode", "retry")
            if mode == "retry":
                if track["operation_state"] != "FAILED":
                    raise ValueError("Only a failed operation can be retried")
                previous = con.execute(
                    "SELECT payload FROM jobs WHERE kind='track' AND target=? "
                    "AND state='FAILED' ORDER BY id DESC LIMIT 1",
                    (track_id,),
                ).fetchone()
                payload = json.loads(previous["payload"]) if previous else {"mode": "ingest"}
                mode = payload.get("mode", "ingest")
            elif mode == "approve":
                if track["operation_state"] != "NEEDS_APPROVAL":
                    raise ValueError("Track is not awaiting approval")
                pending = json.loads(track["pending_operation"] or "{}")
                choices = json.loads(track["choices"] or "[]")
                index = payload.get("index")
                if not isinstance(index, int) or index < 0 or index >= len(choices):
                    raise ValueError("Invalid metadata candidate")
                selected = choices[index]
                payload = {**pending, "mode": "resume", "selected": selected}
                mode = pending.get("action", "reprocess")
                con.execute(
                    "UPDATE tracks SET selected=? WHERE id=?",
                    (json.dumps(selected), track_id),
                )
            elif mode in {"redownload", "reprocess"}:
                if track["health"] not in {"AVAILABLE", "MISSING"}:
                    raise ValueError("This action requires an existing logical track")
                origin = self._select_origin_locked(
                    con, track_id, user, payload.get("origin_id")
                )
                payload["origin_id"] = origin["id"]
            elif mode == "retag":
                if track["health"] != "AVAILABLE" or not track["file_path"]:
                    raise ValueError("Metadata editing requires an available audio file")
            elif mode not in {"delete", "delete_ignore", "comment", "ingest"}:
                raise ValueError("Unsupported track action")

            if mode == "ingest":
                origin = self._select_origin_locked(
                    con, track_id, user, payload.get("origin_id")
                )
                payload["origin_id"] = origin["id"]

            if not self.enqueue("track", track_id, user, payload, con=con):
                raise ValueError("This track already has an active job")
            con.execute(
                """UPDATE tracks SET operation_state='QUEUED',operation_kind=?,
                   operation_error=NULL,pending_operation=CASE WHEN ?='approve'
                   THEN pending_operation ELSE NULL END,updated_at=? WHERE id=?""",
                (mode.upper(), payload.get("mode"), utcnow(), track_id),
            )

    def set_operation(self, track_id: str, state: str, kind: str | None = None,
                      error: str | None = None):
        values: dict[str, Any] = {
            "operation_state": state,
            "operation_error": redact(error) if error else None,
            "updated_at": utcnow(),
        }
        if kind is not None:
            values["operation_kind"] = kind.upper()
        self.update("tracks", track_id, **values)

    def pause_for_approval(self, track_id: str, choices: list[dict], pending: dict,
                           temp_path: str):
        self.update(
            "tracks",
            track_id,
            choices=json.dumps(choices),
            selected=None,
            pending_operation=json.dumps(pending),
            temp_path=temp_path,
            operation_state="NEEDS_APPROVAL",
            operation_kind=pending.get("action", "reprocess").upper(),
            operation_error=None,
            updated_at=utcnow(),
        )

    def complete_operation(self, track_id: str, *, health: str | None = None):
        values: dict[str, Any] = {
            "operation_state": "IDLE",
            "operation_error": None,
            "operation_kind": None,
            "pending_operation": None,
            "choices": "[]",
            "selected": None,
            "temp_path": None,
            "updated_at": utcnow(),
        }
        if health:
            values["health"] = health
        self.update("tracks", track_id, **values)

    def fail_operation(self, track_id: str, kind: str, error: str):
        if self.one("SELECT id FROM tracks WHERE id=?", (track_id,)):
            self.update(
                "tracks",
                track_id,
                operation_state="FAILED",
                operation_kind=kind.upper(),
                operation_error=redact(error),
                updated_at=utcnow(),
            )

    def identity_plan(self, track_id: str, origin_id: str, selected: dict,
                      action: str):
        with self.db() as con:
            track = con.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
            origin = con.execute(
                "SELECT * FROM track_origins WHERE id=? AND track_id=?",
                (origin_id, track_id),
            ).fetchone()
            if not track or not origin:
                raise LookupError("Track or origin disappeared while processing")
            mbid = selected.get("mbid") or selected.get("tags", {}).get("mb_trackid")
            ignored = None
            if mbid:
                ignored = con.execute(
                    "SELECT * FROM ignored_tracks WHERE user_id=? AND mbid=?",
                    (track["user_id"], mbid),
                ).fetchone()
            existing = None
            if mbid:
                existing = con.execute(
                    "SELECT * FROM tracks WHERE user_id=? AND mbid=? AND id<>?",
                    (track["user_id"], mbid, track_id),
                ).fetchone()
            count = con.execute(
                "SELECT COUNT(*) FROM track_origins WHERE track_id=?",
                (track_id,),
            ).fetchone()[0]
            route = "in_place"
            working = dict(track)
            if existing:
                route = "existing"
                working = dict(existing)
                first_seen = _earliest(existing["first_seen_at"], origin["first_seen_at"])
                playlist_seen = _earliest(
                    existing["playlist_discovered_at"],
                    origin["playlist_discovered_at"],
                )
                working.update({
                    "first_seen_at": first_seen,
                    "playlist_discovered_at": playlist_seen,
                    "discovered_at": playlist_seen or first_seen,
                    "discovery_basis": "playlist_added" if playlist_seen else "first_seen",
                })
            elif mbid and track["mbid"] and mbid != track["mbid"] and count > 1:
                route = "split"
                new_id = str(uuid.uuid4())
                first_seen = origin["first_seen_at"]
                playlist_seen = origin["playlist_discovered_at"]
                working = {
                    "id": new_id,
                    "user_id": track["user_id"],
                    "title": origin["title"],
                    "first_seen_at": first_seen,
                    "playlist_discovered_at": playlist_seen,
                    "discovered_at": playlist_seen or first_seen,
                    "discovery_basis": "playlist_added" if playlist_seen else "first_seen",
                    "health": "UNAVAILABLE",
                    "operation_state": "RUNNING",
                    "operation_kind": action.upper(),
                    "operation_error": None,
                    "file_path": None,
                    "temp_path": track["temp_path"],
                    "choices": "[]",
                    "selected": None,
                    "pending_operation": None,
                    "beets_id": None,
                    "matched_title": None,
                    "mbid": None,
                    "release_id": None,
                    "current_origin_id": None,
                    "current_asset_id": None,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
            return {
                "route": route,
                "source_track": dict(track),
                "working_track": working,
                "origin": dict(origin),
                "selected_mbid": mbid,
                "ignored": dict(ignored) if ignored else None,
            }

    @staticmethod
    def _insert_history_locked(con, plan: dict, result: dict, reason: str):
        previous = plan["source_track"]
        origin = plan["origin"]
        new_mbid = result.get("mbid")
        if not previous.get("mbid") or previous.get("mbid") == new_mbid:
            return
        con.execute(
            """INSERT INTO identity_history(
               user_id,track_id,origin_id,origin_provider,origin_media_key,origin_url,
               previous_mbid,previous_release_id,previous_title,new_mbid,new_release_id,
               new_title,changed_at,reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                previous["user_id"], plan["working_track"]["id"], origin["id"],
                origin["provider"], origin["media_key"], origin["url"],
                previous.get("mbid"), previous.get("release_id"),
                previous.get("matched_title") or previous.get("title"),
                new_mbid, result.get("release_id"), result.get("matched_title"),
                utcnow(), reason,
            ),
        )

    @staticmethod
    def _detach_origin_assets_locked(con, source_id: str, target_id: str,
                                     origin_id: str, now: str):
        """Move provenance for an origin and invalidate a source asset based on it."""
        if source_id == target_id:
            return {"old_assets": [], "reingest_origin_id": None}
        source = con.execute("SELECT * FROM tracks WHERE id=?", (source_id,)).fetchone()
        if not source:
            return {"old_assets": [], "reingest_origin_id": None}
        moved = [dict(row) for row in con.execute(
            "SELECT * FROM assets WHERE track_id=? AND origin_id=? ORDER BY id",
            (source_id, origin_id),
        )]
        moved_ids = {asset["id"] for asset in moved}
        current_moved = (
            source["current_origin_id"] == origin_id
            or source["current_asset_id"] in moved_ids
        )
        if moved_ids:
            placeholders = ",".join("?" for _ in moved_ids)
            con.execute(
                f"UPDATE assets SET state='REPLACED',replaced_at=COALESCE(replaced_at,?) "
                f"WHERE id IN ({placeholders}) AND state='CURRENT'",
                (now, *moved_ids),
            )
            con.execute(
                f"UPDATE assets SET track_id=? WHERE id IN ({placeholders})",
                (target_id, *moved_ids),
            )
        reingest_origin_id = None
        if current_moved:
            con.execute(
                """UPDATE tracks SET file_path=NULL,current_asset_id=NULL,
                   current_origin_id=NULL,health='UNAVAILABLE',beets_id=NULL,
                   updated_at=? WHERE id=?""",
                (now, source_id),
            )
            remaining = con.execute(
                """SELECT id FROM track_origins
                   WHERE track_id=? AND id<>? AND downloadable=1
                   ORDER BY last_download_at IS NULL,last_download_at DESC,id LIMIT 1""",
                (source_id, origin_id),
            ).fetchone()
            reingest_origin_id = remaining["id"] if remaining else None
        return {
            "old_assets": moved,
            "reingest_origin_id": reingest_origin_id,
        }

    def commit_existing_identity(self, plan: dict, reason: str, operation_id: str):
        """Attach one origin to an already-available canonical recording."""
        source_id = plan["source_track"]["id"]
        target_id = plan["working_track"]["id"]
        origin_id = plan["origin"]["id"]
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE track_origins SET track_id=?,last_error=NULL WHERE id=?",
                (target_id, origin_id),
            )
            detached = self._detach_origin_assets_locked(
                con, source_id, target_id, origin_id, utcnow()
            )
            self._recompute_discovery_locked(con, source_id)
            self._recompute_discovery_locked(con, target_id)
            result = {
                "mbid": plan["working_track"].get("mbid"),
                "release_id": plan["working_track"].get("release_id"),
                "matched_title": plan["working_track"].get("matched_title"),
            }
            self._insert_history_locked(con, plan, result, reason)
            orphan = not con.execute(
                "SELECT 1 FROM track_origins WHERE track_id=?", (source_id,)
            ).fetchone()
            receipt = {
                "kind": "DEDUPLICATED",
                "source_id": source_id,
                "target_id": target_id,
                "source_orphan": bool(orphan),
                "old_assets": detached["old_assets"],
                "reingest_origin_id": detached["reingest_origin_id"],
                "new_path": plan["working_track"].get("file_path"),
            }
            self._record_receipt_locked(
                con, operation_id, plan["source_track"]["user_id"], source_id,
                target_id, reason, receipt,
            )
        return receipt

    def commit_processed_identity(self, plan: dict, result: dict, asset: dict,
                                  reason: str, operation_id: str):
        source_id = plan["source_track"]["id"]
        target_id = plan["working_track"]["id"]
        origin_id = plan["origin"]["id"]
        now = utcnow()
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            if plan["route"] == "split":
                working = plan["working_track"]
                con.execute(
                    """INSERT INTO tracks(
                       id,user_id,title,first_seen_at,playlist_discovered_at,
                       discovered_at,discovery_basis,health,operation_state,
                       operation_kind,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,'UNAVAILABLE','RUNNING',?,?,?)""",
                    (
                        working["id"], working["user_id"], working["title"],
                        working["first_seen_at"], working["playlist_discovered_at"],
                        working["discovered_at"], working["discovery_basis"],
                        reason.upper(), working["created_at"], now,
                    ),
                )
            detached = {"old_assets": [], "reingest_origin_id": None}
            if plan["route"] in {"split", "existing"}:
                con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (target_id, origin_id))
                detached = self._detach_origin_assets_locked(
                    con, source_id, target_id, origin_id, now
                )

            self._recompute_discovery_locked(con, source_id)
            self._recompute_discovery_locked(con, target_id)
            old = con.execute(
                "SELECT * FROM assets WHERE track_id=? AND state='CURRENT'",
                (target_id,),
            ).fetchone()
            if old:
                con.execute(
                    "UPDATE assets SET state='REPLACED',replaced_at=? WHERE id=?",
                    (now, old["id"]),
                )
            cursor = con.execute(
                """INSERT INTO assets(
                   track_id,origin_id,path,state,created_at,activated_at,sha256,
                   size_bytes,mtime_ns,downloader_name,downloader_version,source_url
                ) VALUES (?,?,?,'CURRENT',?,?,?,?,?,?,?,?)""",
                (
                    target_id, origin_id, asset["path"], now, now,
                    asset.get("sha256"), asset.get("size_bytes"), asset.get("mtime_ns"),
                    asset.get("downloader_name"), asset.get("downloader_version"),
                    asset.get("source_url"),
                ),
            )
            asset_id = cursor.lastrowid
            con.execute(
                """UPDATE tracks SET title=?,file_path=?,beets_id=?,matched_title=?,
                   mbid=?,release_id=?,current_origin_id=?,current_asset_id=?,
                   health='AVAILABLE',operation_state='IDLE',operation_kind=NULL,
                   operation_error=NULL,temp_path=NULL,choices='[]',selected=NULL,
                   pending_operation=NULL,updated_at=? WHERE id=?""",
                (
                    plan["working_track"].get("title") or result.get("matched_title") or "Unknown",
                    asset["path"], result.get("beets_id"), result.get("matched_title"),
                    result.get("mbid"), result.get("release_id"), origin_id, asset_id,
                    now, target_id,
                ),
            )
            con.execute(
                """UPDATE track_origins SET availability='AVAILABLE',last_download_at=?,
                   last_error=NULL WHERE id=?""",
                (now, origin_id),
            )
            self._insert_history_locked(con, plan, result, reason)
            orphan = source_id != target_id and not con.execute(
                "SELECT 1 FROM track_origins WHERE track_id=?", (source_id,)
            ).fetchone()
            receipt = {
                "kind": "ACTIVATED",
                "source_id": source_id,
                "target_id": target_id,
                "asset_id": asset_id,
                "old_assets": ([dict(old)] if old else []) + detached["old_assets"],
                "source_orphan": bool(orphan),
                "reingest_origin_id": detached["reingest_origin_id"],
                "new_path": asset["path"],
            }
            self._record_receipt_locked(
                con, operation_id, plan["source_track"]["user_id"], source_id,
                target_id, reason, receipt,
            )
        return receipt

    def create_tombstone(self, track_id: str, user: str, reason: str):
        """Create one idempotent logical-song tombstone plus all known origins."""
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track = con.execute(
                "SELECT * FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            ).fetchone()
            if not track:
                raise LookupError("Track not found for this user")
            origins = list(con.execute(
                "SELECT * FROM track_origins WHERE track_id=?", (track_id,)
            ))
            ignored = None
            if track["mbid"]:
                ignored = con.execute(
                    "SELECT * FROM ignored_tracks WHERE user_id=? AND mbid=?",
                    (user, track["mbid"]),
                ).fetchone()
            if not ignored and origins:
                placeholders = ",".join("?" for _ in origins)
                ignored = con.execute(
                    f"""SELECT it.* FROM ignored_tracks it
                        JOIN ignored_origins io ON io.ignored_track_id=it.id
                        WHERE it.user_id=? AND io.provider || ':' || io.media_key
                        IN ({placeholders}) LIMIT 1""",
                    (user, *(f"{origin['provider']}:{origin['media_key']}" for origin in origins)),
                ).fetchone()
            ignored_id = ignored["id"] if ignored else track_id
            con.execute(
                """INSERT INTO ignored_tracks(id,user_id,mbid,title,created_at,reason)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     mbid=COALESCE(ignored_tracks.mbid,excluded.mbid),
                     title=excluded.title,reason=excluded.reason""",
                (
                    ignored_id, user, track["mbid"],
                    track["matched_title"] or track["title"], utcnow(), reason,
                ),
            )
            for origin in origins:
                con.execute(
                    """INSERT INTO ignored_origins(
                       user_id,provider,media_key,url,title,ignored_track_id,created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,provider,media_key) DO UPDATE SET
                      ignored_track_id=excluded.ignored_track_id,
                      url=excluded.url,title=excluded.title""",
                    (
                        user, origin["provider"], origin["media_key"], origin["url"],
                        origin["title"], ignored_id, utcnow(),
                    ),
                )
            return ignored_id

    def ignore_identified_origin(self, origin_id: str, mbid: str, title: str,
                                 operation_id: str):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            origin = con.execute(
                "SELECT * FROM track_origins WHERE id=?", (origin_id,)
            ).fetchone()
            if not origin:
                return None
            ignored = con.execute(
                "SELECT * FROM ignored_tracks WHERE user_id=? AND mbid=?",
                (origin["user_id"], mbid),
            ).fetchone()
            if not ignored:
                return None
            con.execute(
                """INSERT OR REPLACE INTO ignored_origins(
                   user_id,provider,media_key,url,title,ignored_track_id,created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    origin["user_id"], origin["provider"], origin["media_key"],
                    origin["url"], title or origin["title"], ignored["id"], utcnow(),
                ),
            )
            track_id = origin["track_id"]
            receipt = {
                "kind": "IGNORED",
                "source_id": track_id,
                "target_id": None,
                "source_orphan": False,
                "old_asset": None,
                "new_path": None,
            }
            self._record_receipt_locked(
                con, operation_id, origin["user_id"], track_id, None,
                "INGEST", receipt, finalized=True,
            )
            con.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            return track_id

    def list_ignored(self, user: str):
        self.require_user(user)
        rows = self.rows(
            "SELECT * FROM ignored_tracks WHERE user_id=? ORDER BY created_at DESC",
            (user,),
        )
        for row in rows:
            row["origins"] = self.rows(
                "SELECT * FROM ignored_origins WHERE ignored_track_id=? ORDER BY provider,title",
                (row["id"],),
            )
        return rows

    def restore_ignored(self, ignored_id: str, user: str):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id FROM ignored_tracks WHERE id=? AND user_id=?",
                (ignored_id, user),
            ).fetchone()
            if not row:
                raise LookupError("Ignored music entry not found for this user")
            con.execute("DELETE FROM ignored_tracks WHERE id=?", (ignored_id,))

    def delete_track(self, track_id: str, user: str):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            ).fetchone()
            if not row:
                raise LookupError("Track not found for this user")
            con.execute("DELETE FROM tracks WHERE id=?", (track_id,))

    def delete_track_with_receipt(self, track_id: str, user: str,
                                  operation_id: str, action: str):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id FROM tracks WHERE id=? AND user_id=?", (track_id, user)
            ).fetchone()
            if not row:
                existing = con.execute(
                    "SELECT 1 FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing:
                    return
                raise LookupError("Track not found for this user")
            receipt = {
                "kind": "DELETED",
                "source_id": track_id,
                "target_id": None,
                "source_orphan": False,
                "old_asset": None,
                "new_path": None,
            }
            self._record_receipt_locked(
                con, operation_id, user, track_id, None, action, receipt,
                finalized=True,
            )
            con.execute("DELETE FROM tracks WHERE id=?", (track_id,))

    def track_has_origins(self, track_id: str) -> bool:
        return bool(self.one(
            "SELECT 1 AS ok FROM track_origins WHERE track_id=? LIMIT 1",
            (track_id,),
        ))

    def dashboard_tracks(self, user: str, page: int, limit: int,
                         status: str = "ALL", query: str = ""):
        self.require_user(user)
        clauses = ["t.user_id=?"]
        params: list[Any] = [user]
        search = query.strip()
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(t.title LIKE ? ESCAPE '\\' OR COALESCE(t.matched_title,'') LIKE ? ESCAPE '\\' "
                "OR EXISTS(SELECT 1 FROM track_origins so WHERE so.track_id=t.id "
                "AND so.title LIKE ? ESCAPE '\\'))"
            )
            params.extend((pattern, pattern, pattern))
        filters = {
            "AVAILABLE": "t.health='AVAILABLE'",
            "MISSING": "t.health='MISSING'",
            "UNAVAILABLE": "t.health='UNAVAILABLE'",
            "WORKING": "t.operation_state IN ('QUEUED','RUNNING')",
            "DEFERRED": "t.operation_state='DEFERRED'",
            "NEEDS_APPROVAL": "t.operation_state='NEEDS_APPROVAL'",
            "FAILED": "t.operation_state='FAILED'",
        }
        if status != "ALL":
            if status not in filters:
                raise ValueError("Unknown track filter")
            clauses.append(filters[status])
        where = " AND ".join(clauses)
        total = self.one(f"SELECT COUNT(*) AS n FROM tracks t WHERE {where}", params)["n"]
        rows = self.rows(
            f"""SELECT t.*,a.sha256,a.size_bytes,a.mtime_ns,a.downloader_name,
                       a.downloader_version,a.source_url AS asset_source_url,
                       o.provider AS current_provider,o.url AS current_url
                FROM tracks t
                LEFT JOIN assets a ON a.id=t.current_asset_id
                LEFT JOIN track_origins o ON o.id=t.current_origin_id
                WHERE {where}
                ORDER BY t.updated_at DESC,t.id
                LIMIT ? OFFSET ?""",
            (*params, limit, (page - 1) * limit),
        )
        for row in rows:
            row["choices"] = [
                {key: value for key, value in choice.items() if key != "tags"}
                for choice in json.loads(row.pop("choices") or "[]")
            ]
            row.pop("selected", None)
            row.pop("pending_operation", None)
            row["origins"] = self.origins_for_track(row["id"], user)
            active_job = self.one(
                """SELECT not_before,defer_count FROM jobs
                   WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')
                   ORDER BY id DESC LIMIT 1""",
                (row["id"],),
            )
            row["retry_at"] = active_job["not_before"] if active_job else None
            row["defer_count"] = int(active_job["defer_count"] or 0) if active_job else 0
            row["playlists"] = self.rows(
                """SELECT s.id,s.title,MIN(m.added_at) AS added_at
                   FROM memberships m
                   JOIN sources s ON s.id=m.source_id
                   JOIN track_origins o ON o.id=m.origin_id
                   WHERE o.track_id=? AND s.is_playlist=1
                   GROUP BY s.id,s.title ORDER BY added_at""",
                (row["id"],),
            )
            row["issue_count"] = self.one(
                "SELECT COUNT(*) AS n FROM integrity_issues "
                "WHERE track_id=? AND resolved_at IS NULL",
                (row["id"],),
            )["n"]

        stats_rows = self.rows(
            """SELECT
               COUNT(*) AS total,
               SUM(health='AVAILABLE') AS available,
               SUM(operation_state='QUEUED') AS queued,
               SUM(operation_state='RUNNING') AS running,
               SUM(operation_state IN ('QUEUED','RUNNING')) AS working,
               SUM(operation_state='DEFERRED') AS deferred,
               SUM(operation_state='NEEDS_APPROVAL') AS approval,
               SUM(operation_state='FAILED') AS failed,
               SUM(health='MISSING' OR operation_state='FAILED') AS attention
               FROM tracks WHERE user_id=?""",
            (user,),
        )[0]
        return {
            "tracks": rows,
            "total": total,
            "page": page,
            "limit": limit,
            "stats": {key: int(value or 0) for key, value in stats_rows.items()},
        }

    def track_details(self, track_id: str, user: str):
        track = self.owned("tracks", track_id, user)
        track["origins"] = self.origins_for_track(track_id, user)
        track["assets"] = self.rows(
            "SELECT * FROM assets WHERE track_id=? ORDER BY id DESC", (track_id,)
        )
        track["identity_history"] = self.rows(
            "SELECT * FROM identity_history WHERE user_id=? AND track_id=? ORDER BY id DESC",
            (user, track_id),
        )
        track["integrity_issues"] = self.rows(
            "SELECT * FROM integrity_issues WHERE user_id=? AND track_id=? "
            "AND resolved_at IS NULL ORDER BY severity DESC,id",
            (user, track_id),
        )
        return track

    def replace_integrity_issues(self, user: str, issues: list[dict]):
        now = utcnow()
        keys = {issue["issue_key"] for issue in issues}
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            if keys:
                placeholders = ",".join("?" for _ in keys)
                con.execute(
                    f"UPDATE integrity_issues SET resolved_at=? WHERE user_id=? "
                    f"AND resolved_at IS NULL AND issue_key NOT IN ({placeholders})",
                    (now, user, *keys),
                )
            else:
                con.execute(
                    "UPDATE integrity_issues SET resolved_at=? WHERE user_id=? AND resolved_at IS NULL",
                    (now, user),
                )
            for issue in issues:
                con.execute(
                    """INSERT INTO integrity_issues(
                       user_id,issue_key,track_id,asset_id,kind,severity,message,
                       details,detected_at,resolved_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,NULL)
                    ON CONFLICT(user_id,issue_key) DO UPDATE SET
                      track_id=excluded.track_id,asset_id=excluded.asset_id,
                      kind=excluded.kind,severity=excluded.severity,
                      message=excluded.message,details=excluded.details,
                      detected_at=excluded.detected_at,resolved_at=NULL""",
                    (
                        user, issue["issue_key"], issue.get("track_id"),
                        issue.get("asset_id"), issue["kind"], issue["severity"],
                        issue["message"], json.dumps(issue.get("details", {})), now,
                    ),
                )

    def integrity_report(self, user: str):
        self.require_user(user)
        issues = self.rows(
            "SELECT * FROM integrity_issues WHERE user_id=? AND resolved_at IS NULL "
            "ORDER BY CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END,id",
            (user,),
        )
        for issue in issues:
            issue["details"] = json.loads(issue["details"] or "{}")
            if issue["track_id"]:
                issue["track"] = self.one(
                    "SELECT id,title,matched_title,health FROM tracks WHERE id=?",
                    (issue["track_id"],),
                )
        running = self.one(
            "SELECT * FROM jobs WHERE kind='integrity' AND target=? "
            "AND state IN ('PENDING','RUNNING') ORDER BY id DESC LIMIT 1",
            (user,),
        )
        return {
            "issues": issues,
            "summary": {
                "total": len(issues),
                "errors": sum(issue["severity"] == "ERROR" for issue in issues),
                "warnings": sum(issue["severity"] == "WARNING" for issue in issues),
            },
            "running": bool(running),
            "last_run": self.setting(f"integrity_last:{user}"),
        }

    def reserve_api(self, host, interval):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT next_at FROM api_clock WHERE host=?", (host,)).fetchone()
            now = time.time()
            at = max(now, row[0] if row else now)
            con.execute(
                "INSERT INTO api_clock VALUES (?,?) "
                "ON CONFLICT(host) DO UPDATE SET next_at=excluded.next_at",
                (host, at + interval),
            )
        return max(0, at - now)

    def defer_api(self, host, until):
        self.execute(
            "INSERT INTO api_clock VALUES (?,?) "
            "ON CONFLICT(host) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
            (host, until),
        )
