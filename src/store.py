"""Application state only. Music metadata and file organization belong to beets."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from common import STATE, utcnow, user_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(name),
 provider TEXT NOT NULL, source_key TEXT NOT NULL, url TEXT NOT NULL,
 title TEXT NOT NULL, is_playlist INTEGER NOT NULL, monitored INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'PENDING', error TEXT, synced_at TEXT,
 next_sync REAL NOT NULL DEFAULT 0, playlist_path TEXT,
 UNIQUE(user_id,provider,source_key));
CREATE TABLE IF NOT EXISTS tracks (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(name),
 provider TEXT NOT NULL, media_key TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
 discovered_at TEXT, discovery_basis TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', error TEXT, file_path TEXT, temp_path TEXT,
 choices TEXT NOT NULL DEFAULT '[]', selected TEXT, beets_id INTEGER,
 matched_title TEXT, mbid TEXT, release_id TEXT,
 UNIQUE(user_id,provider,media_key));
CREATE TABLE IF NOT EXISTS memberships (
 source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 entry_id TEXT NOT NULL, track_id TEXT NOT NULL REFERENCES tracks(id),
 added_at TEXT, position INTEGER NOT NULL,
 PRIMARY KEY(source_id,entry_id));
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
 kind TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL DEFAULT 'PENDING', error TEXT,
 created_at TEXT NOT NULL, finished_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(kind,target)
 WHERE state IN ('PENDING','RUNNING');
CREATE INDEX IF NOT EXISTS track_user ON tracks(user_id,status);
CREATE INDEX IF NOT EXISTS membership_track ON memberships(track_id);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
 level TEXT NOT NULL, message TEXT NOT NULL, user_id TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_clock (host TEXT PRIMARY KEY, next_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quota (day TEXT PRIMARY KEY, used INTEGER NOT NULL);
"""


def redact(message):
    """Remove configured API secrets and common sensitive URL parameters."""
    message = str(message)
    for key in ("YT_API_KEY", "ACOUSTID_API_KEY"):
        if os.getenv(key):
            message = message.replace(os.environ[key], "[REDACTED]")
    return re.sub(r"(?i)([?&](?:key|token|sig|signature|expire|client)=)[^&\s]+",
                  r"\1[REDACTED]", message)


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE / "ingestor.sqlite"

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    def init(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            con.executemany("INSERT OR IGNORE INTO users VALUES (?)", [("admin",), ("guest",)])

    def rows(self, sql, args=()):
        with self.db() as con:
            return [dict(r) for r in con.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.db() as con:
            return con.execute(sql, args).rowcount

    def update(self, table, row_id, **values):
        if table not in {"tracks", "sources", "jobs"}:
            raise ValueError("Invalid table")
        if values.get("error"):
            values["error"] = redact(values["error"])
        # Column names are internal constants, never request data.
        assignments = ",".join(f"{k}=?" for k in values)
        self.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*values.values(), row_id))

    def add_user(self, name):
        self.execute("INSERT OR IGNORE INTO users VALUES (?)", (user_name(name),))

    def require_user(self, name):
        user_name(name)
        if not self.one("SELECT name FROM users WHERE name=?", (name,)):
            raise ValueError("Unknown user")
        return name

    def owned(self, table, target, user):
        if table not in {"sources", "tracks", "jobs"}:
            raise ValueError("Invalid table")
        row = self.one(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (target, user))
        if not row:
            raise LookupError("Item not found for this user")
        return row

    def event(self, level, message, user=None, target=None):
        message = redact(message)
        self.execute("INSERT INTO events(at,level,message,user_id,target) VALUES (?,?,?,?,?)",
                     (utcnow(), level, message[:16000], user, target))
        print(f"{level} {message}", flush=True)

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))

    def enqueue(self, kind, target, user, payload=None, con=None):
        payload = dict(payload or {})
        if kind == "track":
            payload.setdefault("operation", uuid.uuid4().hex)
        args = (user, kind, target, json.dumps(payload), utcnow())
        sql = "INSERT OR IGNORE INTO jobs(user_id,kind,target,payload,created_at) VALUES (?,?,?,?,?)"
        if con is not None:
            return con.execute(sql, args).rowcount > 0
        return self.execute(sql, args) > 0

    def claim(self):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE state='PENDING' ORDER BY id LIMIT 1").fetchone()
            if row:
                con.execute("UPDATE jobs SET state='RUNNING',error=NULL WHERE id=?", (row["id"],))
                return dict(row)
        return None

    def recover(self):
        # Interrupted work is retryable; approval snapshots and downloaded files survive.
        self.execute("UPDATE jobs SET state='PENDING' WHERE state='RUNNING'")
        self.execute("UPDATE tracks SET status='PENDING' WHERE status='PROCESSING'")
        self.execute("UPDATE sources SET status='PENDING' WHERE status='SYNCING'")

    def add_source(self, ref, user, monitored=False, label=None):
        self.require_user(user)
        with self.db() as con:
            con.execute("""INSERT INTO sources(id,user_id,provider,source_key,url,title,is_playlist,monitored)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(user_id,provider,source_key)
              DO UPDATE SET monitored=MAX(monitored,excluded.monitored)""",
              (str(uuid.uuid4()), user, ref.provider, ref.key, ref.url, label or ref.key,
               ref.is_playlist, int(monitored)))
            row = con.execute("SELECT * FROM sources WHERE user_id=? AND provider=? AND source_key=?",
                              (user, ref.provider, ref.key)).fetchone()
            self.enqueue("sync", row["id"], user, con=con)
            return dict(row)

    def apply_snapshot(self, source, snapshot):
        """Commit only a COMPLETE provider response. Keep addition times per membership."""
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM memberships WHERE source_id=?", (source["id"],))
            for entry in snapshot.entries:
                track = con.execute("SELECT * FROM tracks WHERE user_id=? AND provider=? AND media_key=?",
                                    (source["user_id"], source["provider"], entry.key)).fetchone()
                if track is None:
                    tid = str(uuid.uuid4())
                    con.execute("""INSERT INTO tracks(id,user_id,provider,media_key,url,title,discovered_at,discovery_basis)
                      VALUES (?,?,?,?,?,?,?,?)""", (tid, source["user_id"], source["provider"], entry.key,
                        entry.url, entry.title, entry.added_at, entry.basis))
                    self.enqueue("track", tid, source["user_id"], con=con)
                else:
                    tid = track["id"]
                    # Earliest known discovery is stable even if a playlist entry is later removed.
                    if entry.added_at and (not track["discovered_at"] or entry.added_at < track["discovered_at"]):
                        con.execute("UPDATE tracks SET discovered_at=?,discovery_basis=? WHERE id=?",
                                    (entry.added_at, entry.basis, tid))
                        if track["file_path"]:
                            self.enqueue("track", tid, source["user_id"], {"mode": "comment"}, con=con)
                con.execute("INSERT INTO memberships VALUES (?,?,?,?,?)", (source["id"], entry.entry_id,
                            tid, entry.added_at, entry.position))
            con.execute("UPDATE sources SET title=?,status='OK',error=NULL,synced_at=? WHERE id=?",
                        (snapshot.title, utcnow(), source["id"]))

    def queue_track(self, tid, user, payload):
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM tracks WHERE id=? AND user_id=?", (tid, user)).fetchone()
            if not row:
                raise LookupError("Track not found for this user")
            if con.execute("SELECT 1 FROM jobs WHERE kind='track' AND target=? AND state IN ('PENDING','RUNNING')", (tid,)).fetchone():
                raise ValueError("This track already has an active job")
            payload = dict(payload)
            mode = payload.get("mode", "retry")
            if mode == "retry":
                if row["status"] != "FAILED":
                    raise ValueError("Only failed tracks can be retried")
                previous = con.execute("SELECT payload FROM jobs WHERE kind='track' AND target=? AND state='FAILED' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                if previous:
                    payload = json.loads(previous["payload"])
                else:
                    payload = {"mode": "auto"}
            elif mode in {"retag", "reidentify", "redownload"}:
                allowed = {"COMPLETED", "FAILED"} if mode == "redownload" else {"COMPLETED"}
                if row["status"] not in allowed:
                    raise ValueError("This action is not permitted in the current track state")
                if mode in {"retag", "reidentify"} and not row["file_path"]:
                    raise ValueError("This action requires an existing audio file")
            if mode == "approve":
                if row["status"] != "NEEDS_APPROVAL":
                    raise ValueError("Track is not awaiting approval")
                index = payload.get("index")
                choices = json.loads(row["choices"])
                if index is not None and (not isinstance(index, int) or index < 0 or index >= len(choices)):
                    raise ValueError("Invalid metadata candidate")
                selected = choices[index] if index is not None else {"asis": True}
                con.execute("UPDATE tracks SET selected=? WHERE id=?", (json.dumps(selected), tid))
            self.enqueue("track", tid, user, payload, con=con)
            con.execute("UPDATE tracks SET status='PENDING',error=NULL WHERE id=?", (tid,))

    def reserve_api(self, host, interval):
        """A shared disk-backed clock also covers separate beets processes and restarts."""
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT next_at FROM api_clock WHERE host=?", (host,)).fetchone()
            now = time.time()
            at = max(now, row[0] if row else now)
            con.execute("INSERT INTO api_clock VALUES (?,?) ON CONFLICT(host) DO UPDATE SET next_at=excluded.next_at",
                        (host, at + interval))
        return max(0, at - now)

    def defer_api(self, host, until):
        self.execute("INSERT INTO api_clock VALUES (?,?) ON CONFLICT(host) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
                     (host, until))
