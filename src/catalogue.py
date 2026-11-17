"""Persistent, rate-limited MusicBrainz catalogue and incremental relationship graph.

The public catalogue is shared between libraries. User-owned tracks and approved
album assignments remain exclusively in ingestor.sqlite. A scoped fetch is staged
page-by-page and published only when the final page is received.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from common import utcnow
from localized_names import preferred_english_name
from http_policy import ApiDeferred, HttpPolicy
from musicbrainz_search import API_ROOT, USER_AGENT, terms, valid_id

ENTITY_KINDS = frozenset({"artist", "work", "recording", "release", "release-group"})
BROWSE = frozenset({"release-group", "recording", "work", "release"})
TTL_DAYS = {"artist": 90, "work": 90, "recording": 90, "release": 90, "release-group": 30}
MAX_GRAPH_NODES = 180

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
 kind TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
 data TEXT NOT NULL, fetched_at TEXT NOT NULL, checked_at TEXT NOT NULL,
 changed_at TEXT NOT NULL, PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS entity_names ON entities(kind,name);
CREATE TABLE IF NOT EXISTS links (
 source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
 data TEXT NOT NULL DEFAULT '{}', checked_at TEXT NOT NULL, changed_at TEXT NOT NULL,
 PRIMARY KEY(source_kind,source_id,target_kind,target_id,relation)
);
CREATE INDEX IF NOT EXISTS links_reverse ON links(target_kind,target_id,source_kind);
CREATE TABLE IF NOT EXISTS release_tracks (
 release_id TEXT NOT NULL, medium INTEGER NOT NULL, position INTEGER NOT NULL,
 recording_id TEXT NOT NULL, track_id TEXT, title TEXT NOT NULL, duration_ms INTEGER,
 checked_at TEXT NOT NULL,
 PRIMARY KEY(release_id,medium,position)
);
CREATE INDEX IF NOT EXISTS release_track_recording ON release_tracks(recording_id,release_id);
CREATE TABLE IF NOT EXISTS scopes (
 scope_key TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 section TEXT NOT NULL, state TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 total INTEGER, requested_at TEXT NOT NULL, last_complete_at TEXT,
 last_error TEXT, retry_after REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS scope_queue ON scopes(state,retry_after,requested_at);
CREATE TABLE IF NOT EXISTS staged_entities (
 scope_key TEXT NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 data TEXT NOT NULL, PRIMARY KEY(scope_key,kind,entity_id)
);
CREATE TABLE IF NOT EXISTS scope_members (
 scope_key TEXT NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
 PRIMARY KEY(scope_key,kind,entity_id)
);
CREATE TABLE IF NOT EXISTS fetched_releases (
 release_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL
);
"""


def _payload(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(value):
    return valid_id(value) if value else None


def _safe_name(value):
    return str(value or "Unknown")[:1000]


def _iso_age(days):
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


class Catalogue:
    def __init__(self, store, path: Path | None = None):
        self.store = store
        self.path = Path(path or store.path.parent / "musicbrainz-catalogue.sqlite")

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def init(self):
        with self.db() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            # Interrupted workers resume their saved cursor on startup.
            con.execute("UPDATE scopes SET state='PENDING' WHERE state='RUNNING'")

    @staticmethod
    def upsert_entity(con, kind, identity, data, *, now=None):
        if kind not in ENTITY_KINDS or not (identity := _id(identity)):
            return False
        now = now or utcnow()
        previous = con.execute("SELECT data,changed_at FROM entities WHERE kind=? AND id=?", (kind, identity)).fetchone()
        # A browse result often has fewer fields than an earlier full lookup.
        # Never downgrade detailed cached metadata to a sparse search snippet.
        combined = {**(json.loads(previous["data"]) if previous else {}), **data}
        content = _payload(combined)
        name = _safe_name(combined.get("title") or combined.get("name"))
        changed = not previous or previous["data"] != content
        con.execute("""INSERT INTO entities(kind,id,name,data,fetched_at,checked_at,changed_at)
                       VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET
                       name=excluded.name, data=excluded.data, fetched_at=excluded.fetched_at,
                       checked_at=excluded.checked_at, changed_at=excluded.changed_at""",
                    (kind, identity, name, content, now, now, now if changed else previous["changed_at"]))
        return True

    @staticmethod
    def upsert_link(con, source_kind, source_id, target_kind, target_id, relation, data=None, *, now=None):
        if source_kind not in ENTITY_KINDS or target_kind not in ENTITY_KINDS:
            return False
        if not (source_id := _id(source_id)) or not (target_id := _id(target_id)):
            return False
        now = now or utcnow()
        content = _payload(data or {})
        previous = con.execute("""SELECT data,changed_at FROM links WHERE
            source_kind=? AND source_id=? AND target_kind=? AND target_id=? AND relation=?""",
            (source_kind, source_id, target_kind, target_id, relation)).fetchone()
        con.execute("""INSERT INTO links VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(source_kind,source_id,target_kind,target_id,relation)
            DO UPDATE SET data=excluded.data,checked_at=excluded.checked_at,
                          changed_at=excluded.changed_at""",
            (source_kind, source_id, target_kind, target_id, relation, content, now,
             now if not previous or previous["data"] != content else previous["changed_at"]))
        return True

    @classmethod
    def ingest(cls, con, kind, data, *, now=None):
        """Save one MusicBrainz entity and directly evidenced typed relations."""
        if not isinstance(data, dict):
            return
        identity = _id(data.get("id"))
        if not identity or not cls.upsert_entity(con, kind, identity, data, now=now):
            return
        for credit in data.get("artist-credit") or []:
            if not isinstance(credit, dict) or not isinstance(credit.get("artist"), dict):
                continue
            artist = credit["artist"]
            cls.upsert_entity(con, "artist", artist.get("id"), artist, now=now)
            cls.upsert_link(con, "artist", artist.get("id"), kind, identity, "credited", now=now)
        group = data.get("release-group") or {}
        if kind == "release" and isinstance(group, dict) and group.get("id"):
            cls.upsert_entity(con, "release-group", group.get("id"), group, now=now)
            cls.upsert_link(con, "release", identity, "release-group", group.get("id"), "edition-of", now=now)
        if kind == "recording":
            for release in data.get("releases") or []:
                if not isinstance(release, dict):
                    continue
                cls.upsert_entity(con, "release", release.get("id"), release, now=now)
                cls.upsert_link(con, "recording", identity, "release", release.get("id"), "appears-on", now=now)
        for relation in data.get("relations") or []:
            if not isinstance(relation, dict) or not relation.get("target-type"):
                continue
            target_kind = relation.get("target-type")
            if target_kind not in ENTITY_KINDS:
                continue
            target = relation.get(target_kind) or {}
            if isinstance(target, dict):
                cls.upsert_entity(con, target_kind, target.get("id"), target, now=now)
                cls.upsert_link(con, kind, identity, target_kind, target.get("id"),
                                str(relation.get("type") or "related")[:80], now=now)

    def request(self, endpoint, params):
        """All provider requests share the ingress HTTP limiter and retry policy."""
        if not endpoint.startswith("/") or ".." in endpoint or "?" in endpoint:
            raise ValueError("Invalid MusicBrainz API path")
        policy = HttpPolicy(self.store)
        with requests.Session() as session:
            prepared = session.prepare_request(requests.Request(
                "GET", API_ROOT.rstrip("/") + endpoint,
                params={**params, "fmt": "json"},
                headers={"User-Agent": os.getenv("HTTP_USER_AGENT") or USER_AGENT,
                         "Accept": "application/json"},
            ))
            response = policy.send(requests.sessions.Session.send, session, prepared,
                                   timeout=(5, 20), allow_redirects=False)
            try:
                if response.status_code == 404:
                    raise LookupError("MusicBrainz entry was not found")
                if response.status_code != 200:
                    raise ApiDeferred(f"MusicBrainz returned HTTP {response.status_code}",
                                      retry_at=time.time() + 90)
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("MusicBrainz returned unexpected data")
                return data
            finally:
                response.close()

    def locally_available_ids(self, user_id, kind):
        """Cached MusicBrainz IDs linked to approved audio in this user's library.

        IDs are not inferred from artist names: unrelated public artists may
        have identical names. Unindexed relationships remain unknown.
        """
        if kind not in ENTITY_KINDS:
            raise ValueError("Unknown catalogue entity type")
        self.store.require_user(user_id)
        owned = {_id(row["mbid"]) for row in self.store.rows(
            """SELECT DISTINCT mbid FROM tracks WHERE user_id=? AND mbid IS NOT NULL
               AND current_asset_id IS NOT NULL AND health='AVAILABLE'
               AND temporary_candidate_sha256 IS NULL""", (user_id,))
                 if _id(row["mbid"])}
        if kind == "recording" or not owned:
            return owned if kind == "recording" else set()
        ids = {entity_kind: set() for entity_kind in ENTITY_KINDS}
        ids["recording"] = owned

        def batches(values):
            values = sorted(values)
            for start in range(0, len(values), 400):
                yield values[start:start + 400]

        with self.db() as con:
            for batch in batches(owned):
                marks = ",".join("?" for _ in batch)
                for link in con.execute(
                    f"""SELECT source_kind,source_id,target_kind,target_id FROM links
                        WHERE (source_kind='recording' AND source_id IN ({marks}))
                           OR (target_kind='recording' AND target_id IN ({marks}))""",
                    (*batch, *batch),
                ):
                    ids[link["source_kind"]].add(link["source_id"])
                    ids[link["target_kind"]].add(link["target_id"])
                # Release lookups may contain tracklists before recording browse.
                for row in con.execute(
                    f"SELECT DISTINCT release_id FROM release_tracks WHERE recording_id IN ({marks})",
                    batch,
                ):
                    ids["release"].add(row["release_id"])
            if kind in {"artist", "release-group"}:
                for batch in batches(ids["release"]):
                    marks = ",".join("?" for _ in batch)
                    for link in con.execute(
                        f"""SELECT source_kind,source_id,target_kind,target_id FROM links
                            WHERE (source_kind='release' AND source_id IN ({marks}))
                               OR (target_kind='release' AND target_id IN ({marks}))""",
                        (*batch, *batch),
                    ):
                        ids[link["source_kind"]].add(link["source_id"])
                        ids[link["target_kind"]].add(link["target_id"])
        return ids[kind]

    def search(self, kind, query, *, remote=False, limit=40, user_id=None, local_only=False):
        if kind not in ENTITY_KINDS:
            raise ValueError("Unknown catalogue entity type")
        query = str(query).strip()[:180]
        owned_ids = (self.locally_available_ids(user_id, kind) if local_only else None)
        if len(query) < 2:
            return {"items": [], "remote": bool(remote), "local_only": bool(local_only)}
        if remote:
            expression = terms(query)
            raw = self.request("/" + kind, {"query": expression, "limit": min(limit, 50)})
            plural = kind + "s"
            items = [item for item in raw.get(plural) or [] if isinstance(item, dict)]
            # Cache public search metadata, even if this specific user hides it.
            with self.db() as con:
                for item in items:
                    self.ingest(con, kind, item)
            filtered = [item for item in items if _id(item.get("id"))
                        and (owned_ids is None or _id(item["id"]) in owned_ids)]
            return {"items": [{"kind": kind, "id": item["id"],
                               "name": _safe_name(item.get("title") or item.get("name")),
                               "data": item, "cached": False}
                              for item in filtered[:limit]],
                    "total": raw.get("count", len(items)), "remote": True,
                    "local_only": bool(local_only)}
        if local_only and not owned_ids:
            return {"items": [], "remote": False, "local_only": True}
        with self.db() as con:
            # Filter BEFORE the result cap. A global top-40 followed by a local
            # filter would omit legitimate local artists with common names.
            matches = con.execute("""SELECT kind,id,name,data,checked_at FROM entities
                WHERE kind=? AND name LIKE ? ESCAPE '\\' COLLATE NOCASE
                ORDER BY name""", (kind, "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"))
            results = []
            for row in matches:
                if owned_ids is not None and row["id"] not in owned_ids:
                    continue
                results.append({"kind": row["kind"], "id": row["id"], "name": row["name"],
                                "data": json.loads(row["data"]), "checked_at": row["checked_at"],
                                "cached": True})
                if len(results) >= limit:
                    break
            return {"items": results, "remote": False, "local_only": bool(local_only)}

    def get(self, kind, identity):
        if kind not in ENTITY_KINDS or not (identity := _id(identity)):
            raise ValueError("Invalid MusicBrainz entity")
        with self.db() as con:
            row = con.execute("SELECT * FROM entities WHERE kind=? AND id=?", (kind, identity)).fetchone()
            scopes = con.execute("""SELECT section,state,cursor,total,last_complete_at,last_error
                FROM scopes WHERE kind=? AND entity_id=? ORDER BY section""", (kind, identity)).fetchall()
            related = con.execute("""SELECT l.target_kind AS kind,l.target_id AS id,l.relation,e.name
                FROM links l LEFT JOIN entities e ON e.kind=l.target_kind AND e.id=l.target_id
                WHERE l.source_kind=? AND l.source_id=? LIMIT 150""", (kind, identity)).fetchall()
            if kind == "release":
                tracks = con.execute("""SELECT medium,position,track_id,recording_id,title,duration_ms
                    FROM release_tracks WHERE release_id=? ORDER BY medium,position LIMIT 250""", (identity,)).fetchall()
            else:
                tracks = []
            return {"kind": kind, "id": identity,
                    "entity": {**dict(row), "data": json.loads(row["data"])} if row else None,
                    "scopes": [dict(r) for r in scopes], "relations": [dict(r) for r in related],
                    "tracks": [dict(r) for r in tracks]}

    def _queue_scope(self, con, kind, identity, section, *, force=False):
        """Add a single follow-up to the durable paginated queue, transactionally."""
        key = f"{kind}:{identity}:{section}"
        existing = con.execute("SELECT * FROM scopes WHERE scope_key=?", (key,)).fetchone()
        if existing and existing["state"] in {"PENDING", "RUNNING"}:
            return False
        if (existing and not force and existing["last_complete_at"]
                and existing["last_complete_at"] >= _iso_age(TTL_DAYS[kind])):
            return False
        con.execute("""INSERT INTO scopes(scope_key,kind,entity_id,section,state,cursor,total,requested_at,retry_after)
            VALUES (?,?,?,?,'PENDING',0,NULL,?,0)
            ON CONFLICT(scope_key) DO UPDATE SET state='PENDING',cursor=0,total=NULL,
            requested_at=excluded.requested_at,last_error=NULL,retry_after=0""",
            (key, kind, identity, section, utcnow()))
        con.execute("DELETE FROM staged_entities WHERE scope_key=?", (key,))
        return True

    def enqueue(self, kind, identity, sections=None, *, force=False):
        if kind not in ENTITY_KINDS or not (identity := _id(identity)):
            raise ValueError("Invalid MusicBrainz entity")
        if sections is None:
            sections = {"artist": ("lookup", "release-group", "recording", "work"),
                        "release-group": ("lookup", "release"),
                        "release": ("lookup",),
                        "recording": ("lookup", "release"),
                        "work": ("lookup",)}[kind]
        sections = tuple(dict.fromkeys(sections))
        for section in sections:
            if section != "lookup" and section not in BROWSE:
                raise ValueError("Unsupported artist catalogue section")
            if section == "work" and kind != "artist":
                raise ValueError("Work browsing requires an artist")
            if section == "release-group" and kind != "artist":
                raise ValueError("Release-group browsing requires an artist")
            if section == "recording" and kind != "artist":
                raise ValueError("Recording browsing requires an artist")
            if section == "release" and kind not in {"recording", "release-group"}:
                raise ValueError("Release browsing requires a recording or release group")
        with self.db() as con:
            for section in sections:
                self._queue_scope(con, kind, identity, section, force=force)
        return self.get(kind, identity)

    def index_recordings(self, recording_ids):
        """Queue a whole library in one SQLite transaction without HTTP calls."""
        valid = sorted({_id(value) for value in recording_ids if _id(value)})
        queued = 0
        with self.db() as con:
            for identity in valid:
                for section in ("lookup", "release"):
                    queued += self._queue_scope(con, "recording", identity, section)
        return {"recordings": len(valid), "new_requests": queued,
                "note": "One request at a time, cached and resumable; no library-wide recomputation."}

    def claim(self):
        with self.db() as con:
            row = con.execute("""SELECT * FROM scopes WHERE state='PENDING' AND retry_after<=?
                              ORDER BY requested_at,scope_key LIMIT 1""", (time.time(),)).fetchone()
            if row:
                con.execute("UPDATE scopes SET state='RUNNING' WHERE scope_key=?", (row["scope_key"],))
            return dict(row) if row else None

    def process_page(self, scope):
        kind, identity, section = scope["kind"], scope["entity_id"], scope["section"]
        page = scope["cursor"]
        if section == "lookup":
            includes = {"artist": "aliases+artist-rels+work-rels",
                        "recording": "artist-credits+releases+work-rels+isrcs",
                        "release": "recordings+artist-credits+release-groups",
                        "release-group": "artist-credits", "work": "artist-rels+recording-rels"}
            data = self.request(f"/{kind}/{identity}", {"inc": includes[kind]})
            records = [data]
            completed = True
            total = 1
        else:
            # MusicBrainz browse endpoints may return fewer than the requested
            # 100 items (notably releases with large tracklists). Offset by the
            # number actually returned, never by a fixed page size.
            params = {"limit": 100, "offset": page}
            params["artist" if kind == "artist" else kind] = identity
            data = self.request("/" + section, params)
            # MusicBrainz uses 'release-count', 'release-group-count', etc.
            plural = section + "s"
            records = data.get(plural, [])
            if not isinstance(records, list):
                raise ValueError("Invalid MusicBrainz browse page")
            total = int(data.get(section + "-count", len(records)))
            if not records and page < total:
                raise ValueError("MusicBrainz browse returned an incomplete empty page")
            completed = page + len(records) >= total
        with self.db() as con:
            current = con.execute("SELECT state,cursor FROM scopes WHERE scope_key=?", (scope["scope_key"],)).fetchone()
            if not current or current["state"] != "RUNNING" or current["cursor"] != page:
                return
            if section == "lookup":
                self.ingest(con, kind, data)
                if kind == "release":
                    self._store_release_tracks(con, data)
            else:
                # Stage every page without publishing partial discographies or
                # recording-to-release associations. Existing completed edges
                # remain available until the final page commits atomically.
                for record in records:
                    if _id(record.get("id")):
                        con.execute("""INSERT INTO staged_entities(scope_key,kind,entity_id,data)
                            VALUES (?,?,?,?) ON CONFLICT(scope_key,kind,entity_id)
                            DO UPDATE SET data=excluded.data""",
                            (scope["scope_key"], section, record["id"], _payload(record)))
            if completed:
                if section != "lookup":
                    previous = {(r["kind"], r["entity_id"]) for r in con.execute(
                        "SELECT kind,entity_id FROM scope_members WHERE scope_key=?", (scope["scope_key"],))}
                    staged = con.execute("""SELECT kind,entity_id,data FROM staged_entities
                        WHERE scope_key=?""", (scope["scope_key"],)).fetchall()
                    fresh = {(r["kind"], r["entity_id"]) for r in staged}
                    for record in staged:
                        self.ingest(con, section, json.loads(record["data"]))
                        record_id = record["entity_id"]
                        if kind == "recording" and section == "release":
                            self.upsert_link(con, "recording", identity, "release", record_id, "appears-on")
                        elif kind == "release-group" and section == "release":
                            self.upsert_link(con, "release", record_id, "release-group", identity, "edition-of")
                        elif kind == "artist":
                            self.upsert_link(con, "artist", identity, section, record_id,
                                             "discography" if section != "work" else "contributor")
                        # Full artist catalogue requests expand incrementally
                        # through families -> editions -> complete release tracklists.
                        # Do not expand individual recording searches into every
                        # compilation containing that recording.
                        if kind == "artist" and section == "release-group":
                            self._queue_scope(con, "release-group", record_id, "release")
                        elif kind == "release-group" and section == "release":
                            self._queue_scope(con, "release", record_id, "lookup")
                        elif kind == "artist" and section == "work":
                            self._queue_scope(con, "work", record_id, "lookup")
                    # Remove only edges owned by this particular complete scope;
                    # do not erase links independently supplied by another scope.
                    if kind == "artist":
                        for old_kind, old_id in previous - fresh:
                            con.execute("""DELETE FROM links WHERE source_kind='artist'
                                AND source_id=? AND target_kind=? AND target_id=?
                                AND relation IN ('discography','contributor')""",
                                (identity, old_kind, old_id))
                    con.execute("DELETE FROM scope_members WHERE scope_key=?", (scope["scope_key"],))
                    con.execute("""INSERT INTO scope_members(scope_key,kind,entity_id)
                        SELECT scope_key,kind,entity_id FROM staged_entities WHERE scope_key=?""",
                        (scope["scope_key"],))
                    con.execute("DELETE FROM staged_entities WHERE scope_key=?", (scope["scope_key"],))
                con.execute("""UPDATE scopes SET state='COMPLETE',cursor=?,total=?,last_complete_at=?,
                    last_error=NULL,retry_after=0 WHERE scope_key=?""",
                    (page + len(records) if section != "lookup" else 1,
                     total, utcnow(), scope["scope_key"]))
            else:
                con.execute("UPDATE scopes SET state='PENDING',cursor=?,total=? WHERE scope_key=?",
                            (page + len(records), total, scope["scope_key"]))

    def _store_release_tracks(self, con, data):
        identity = _id(data.get("id"))
        if not identity:
            return
        rows = []
        for medium_idx, medium in enumerate(data.get("media") or [], 1):
            disc = int(medium.get("position") or medium_idx)
            for position, track in enumerate(medium.get("tracks") or [], 1):
                recording = track.get("recording") or {}
                rec_id = _id(recording.get("id"))
                if not rec_id:
                    continue
                self.ingest(con, "recording", recording)
                track_id = _id(track.get("id"))
                position_num = int(track.get("position") or position)
                rows.append((identity, disc, position_num, rec_id, track_id,
                             _safe_name(track.get("title") or recording.get("title")),
                             track.get("length") or recording.get("length"), utcnow()))
                self.upsert_link(con, "recording", rec_id, "release", identity, "appears-on")
        # Refresh the release tracklist atomically only after a complete release lookup.
        con.execute("DELETE FROM release_tracks WHERE release_id=?", (identity,))
        con.executemany("INSERT INTO release_tracks VALUES (?,?,?,?,?,?,?,?)", rows)
        con.execute("INSERT INTO fetched_releases VALUES (?,?) ON CONFLICT(release_id) DO UPDATE SET checked_at=excluded.checked_at", (identity, utcnow()))

    def fail(self, scope, exc):
        delay = getattr(exc, "retry_at", None) or time.time() + 60
        with self.db() as con:
            con.execute("""UPDATE scopes SET state='PENDING',last_error=?,retry_after=?
                WHERE scope_key=? AND state='RUNNING'""", (str(exc)[:1000], delay, scope["scope_key"]))

    def album_recommendations(self, user_id, source_ids=(), track_ids=()):
        self.store.require_user(user_id)
        source_ids = list(dict.fromkeys(source_ids))[:20]
        track_ids = list(dict.fromkeys(track_ids))[:500]
        if not source_ids and not track_ids:
            raise ValueError("Select a source or some music to organize")
        tracks = {}
        if source_ids:
            marks = ",".join("?" for _ in source_ids)
            sources = self.store.rows(f"SELECT id FROM sources WHERE user_id=? AND id IN ({marks})", (user_id, *source_ids))
            if len(sources) != len(source_ids):
                raise LookupError("Source not found in this library")
            for row in self.store.rows(f"""SELECT DISTINCT t.id,t.mbid,t.matched_title,t.release_id
              FROM tracks t JOIN track_origins o ON o.track_id=t.id
              JOIN source_origins s ON s.origin_id=o.id
              WHERE t.user_id=? AND s.source_id IN ({marks}) AND t.mbid IS NOT NULL""", (user_id, *source_ids)):
                tracks[row["id"]] = row
        if track_ids:
            marks = ",".join("?" for _ in track_ids)
            rows = self.store.rows(f"SELECT id,mbid,matched_title,release_id FROM tracks WHERE user_id=? AND id IN ({marks})", (user_id, *track_ids))
            if len(rows) != len(track_ids):
                raise LookupError("Track not found in this library")
            for row in rows:
                if row["mbid"]:
                    tracks[row["id"]] = row
        recording_ids = sorted({r["mbid"] for r in tracks.values() if _id(r["mbid"])})
        if not recording_ids:
            return {"track_count": 0, "candidates": [], "missing_cache": []}
        marks = ",".join("?" for _ in recording_ids)
        with self.db() as con:
            matched = con.execute(f"""SELECT DISTINCT source_id, target_id
                FROM links WHERE source_kind='recording' AND target_kind='release'
                  AND relation='appears-on' AND source_id IN ({marks})""", recording_ids).fetchall()
            per_release = defaultdict(set)
            for row in matched:
                per_release[row["target_id"]].add(row["source_id"])
            result = []
            for release_id, compatible in per_release.items():
                release = con.execute("SELECT data,name FROM entities WHERE kind='release' AND id=?", (release_id,)).fetchone()
                group = con.execute("""SELECT target_id FROM links WHERE source_kind='release'
                    AND source_id=? AND target_kind='release-group' AND relation='edition-of' LIMIT 1""", (release_id,)).fetchone()
                count = con.execute("SELECT COUNT(*) FROM release_tracks WHERE release_id=?", (release_id,)).fetchone()[0]
                detail = json.loads(release["data"]) if release else {}
                result.append({"release_id": release_id, "release_group_id": group["target_id"] if group else None,
                               "album": release["name"] if release else "Unknown release",
                               "country": detail.get("country"), "date": detail.get("date"),
                               "status": detail.get("status"), "matched": len(compatible),
                               "unmatched": len(recording_ids) - len(compatible),
                               "release_tracks_cached": count,
                               "coverage": round(len(compatible) / len(recording_ids), 3)})
            result.sort(key=lambda x: (-x["matched"], x["unmatched"], -x["release_tracks_cached"], x["album"], x["release_id"]))
            present = set(row["source_id"] for row in matched)
        return {"track_count": len(tracks), "recording_count": len(recording_ids),
                "candidates": result[:60], "missing_cache": sorted(set(recording_ids) - present),
                "note": "Coverage is descriptive evidence, not probability or permission to alter existing tags."}

    def release_assignment(self, release_id, recording_id):
        with self.db() as con:
            row = con.execute("""SELECT t.*, r.name AS album,r.data AS release_json
                FROM release_tracks t JOIN entities r ON r.kind='release' AND r.id=t.release_id
                WHERE t.release_id=? AND t.recording_id=? ORDER BY t.medium,t.position LIMIT 1""",
                (release_id, recording_id)).fetchone()
            if not row:
                return None
            release = json.loads(row["release_json"])
            return {"release_id": release_id, "recording_id": recording_id,
                    "album": preferred_english_name(release),
                    "native_album": row["album"], "album_artist": "".join(
                        str(item.get("name") or item.get("artist", {}).get("name") or "") + str(item.get("joinphrase") or "")
                        for item in release.get("artist-credit") or [] if isinstance(item, dict)),
                    "date": release.get("date"), "release_group_id": (release.get("release-group") or {}).get("id"),
                    "album_artist_ids": [credit["artist"]["id"] for credit in release.get("artist-credit") or []
                                         if isinstance(credit, dict) and isinstance(credit.get("artist"), dict)
                                         and _id(credit["artist"].get("id"))],
                    "medium": row["medium"], "position": row["position"], "track_id": row["track_id"]}

    def graph(self, user_id, *, view="downloaded", kinds=None, query="", focus_id=None, limit=180):
        self.store.require_user(user_id)
        if view not in {"downloaded", "catalogue"}:
            raise ValueError("Unknown graph view")
        kinds = set(ENTITY_KINDS if kinds is None else kinds) & ENTITY_KINDS
        if not kinds:
            return {"nodes": [], "edges": [], "truncated": False}
        limit = max(1, min(int(limit), MAX_GRAPH_NODES))
        owned = {row["mbid"]: row for row in self.store.rows(
            "SELECT id,mbid,matched_title FROM tracks WHERE user_id=? AND mbid IS NOT NULL", (user_id,))}
        query = query.casefold().strip()[:100]
        with self.db() as con:
            rows = con.execute("SELECT kind,id,name FROM entities ORDER BY kind,name LIMIT 10000").fetchall()
            names = {(r["kind"], r["id"]): r["name"] for r in rows}
            links = con.execute("""SELECT source_kind,source_id,target_kind,target_id,relation FROM links
                                  ORDER BY source_kind,source_id LIMIT 30000""").fetchall()
            edges = [(r["source_kind"], r["source_id"], r["target_kind"], r["target_id"], r["relation"]) for r in links]
            # Old cached releases can be linked through the detailed tracklist as well.
            tracks = con.execute("SELECT DISTINCT recording_id,release_id FROM release_tracks LIMIT 15000").fetchall()
            edge_keys = set(edges)
            for track in tracks:
                edge = ("recording", track["recording_id"], "release", track["release_id"], "appears-on")
                if edge not in edge_keys:
                    edges.append(edge)
            adjacency = defaultdict(list)
            for a, ai, b, bi, relation in edges:
                adjacency[(a, ai)].append((b, bi))
                adjacency[(b, bi)].append((a, ai))
            focus = _id(focus_id) if focus_id else None
            if focus:
                seed = [key for key in names if key[1] == focus]
            elif query:
                seed = [key for key, name in names.items() if query in name.casefold()]
                seed += [("recording", rid) for rid, row in owned.items()
                         if query in (row.get("matched_title") or "").casefold()]
            elif view == "downloaded":
                # Prefer already-linked owned recordings. The former UUID-only
                # sample could display 45 disconnected nodes even when the
                # catalogue contained relationships for other downloaded songs.
                seed = sorted((("recording", rid) for rid in owned),
                              key=lambda key: (-len(adjacency.get(key, ())), key[1]))[:45]
            else:
                seed = sorted(names, key=lambda key: (-len(adjacency.get(key, [])), key))[:60]
            # In downloaded mode, search results are restricted to owned recordings
            # and their public-MusicBrainz neighbors, never other users' files.
            if view == "downloaded":
                if not query and not focus:
                    pass  # already selected most-connected owned recordings above
                elif focus:
                    seed = [s for s in seed if s[0] != "recording" or s[1] in owned]
                else:
                    seed = [s for s in seed if s[0] != "recording" or s[1] in owned]
            # Traverse through hidden node types, then project onto the types
            # selected by the user. An artist-only or release-only graph must
            # still be reachable through an owned recording that is hidden.
            seen, traversed = set(), set()
            frontier = deque((key, 0) for key in seed)
            depth_limit = 3 if focus else 2
            while frontier and len(seen) < limit and len(traversed) < max(1000, limit * 20):
                key, depth = frontier.popleft()
                if key in traversed:
                    continue
                traversed.add(key)
                if key[0] in kinds:
                    seen.add(key)
                if depth < depth_limit:
                    for adjacent in adjacency.get(key, []):
                        if (view == "catalogue" or adjacent[0] != "recording"
                                or adjacent[1] in owned):
                            frontier.append((adjacent, depth + 1))
            nodes = [{"id": f"{kind}:{identity}", "kind": kind,
                      "label": names.get((kind, identity)) or owned.get(identity, {}).get("matched_title") or identity,
                      "owned": kind == "recording" and identity in owned} for kind, identity in sorted(seen)]
            graph_edges = [{"source": f"{a}:{ai}", "target": f"{b}:{bi}", "relation": relation}
                           for a, ai, b, bi, relation in edges if (a, ai) in seen and (b, bi) in seen][:700]
            return {"nodes": nodes, "edges": graph_edges, "truncated": bool(frontier),
                    "owned_recording_count": len(owned), "limit": limit}
