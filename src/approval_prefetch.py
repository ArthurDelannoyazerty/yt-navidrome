"""Persistent, non-blocking MusicBrainz metadata prefetch for Approval.

The catalogue SQLite database is shared across library users and already holds
public MusicBrainz entities. This adds a small durable name/search cache and a
resumable queue, never track modifications or inferred recording identities.

Only the dedicated background worker makes prefetch HTTP requests, using the
same HttpPolicy MusicBrainz portal/cooldown as interactive search and Beets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from http_policy import ApiDeferred
from localized_names import artist_credit, preferred_english_name
from metadata_hints import alias_key, normalized_words, search_hints
from musicbrainz_search import PAGE_SIZE, query_for, valid_id
from source_metadata import source_snapshot

NAME_TTL = 30 * 86400
SEARCH_TTL = 7 * 86400
NEGATIVE_TTL = 7 * 86400
SWEEP_INTERVAL = 300
MAX_BATCH_IDS = 200

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_prefetch_cache (
 kind TEXT NOT NULL, cache_key TEXT NOT NULL,
 payload TEXT NOT NULL, fetched_at REAL NOT NULL, expires_at REAL NOT NULL,
 PRIMARY KEY(kind,cache_key)
);
CREATE TABLE IF NOT EXISTS approval_prefetch_jobs (
 kind TEXT NOT NULL, cache_key TEXT NOT NULL, payload TEXT NOT NULL,
 priority INTEGER NOT NULL DEFAULT 20, state TEXT NOT NULL DEFAULT 'PENDING',
 retry_at REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
 requested_at REAL NOT NULL, last_error TEXT,
 PRIMARY KEY(kind,cache_key)
);
CREATE INDEX IF NOT EXISTS approval_prefetch_due
 ON approval_prefetch_jobs(state,retry_at,priority,requested_at);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def search_key(entity, title, artist, artist_id, page):
    """Canonicalize exact user-visible search inputs, preserving page size."""
    return _json([entity, normalized_words(title), normalized_words(artist),
                  valid_id(artist_id) if artist_id else None, int(page)])


def _recording_from_catalogue(value, identity):
    if not isinstance(value, dict) or not value.get("title"):
        return None
    title = str(value["title"])[:1000]
    preferred = preferred_english_name(value)
    credits = []
    for credit in value.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        credits.append({"id": valid_id(artist.get("id")),
                        "name": str(credit.get("name") or artist.get("name") or "")[:200],
                        "joinphrase": str(credit.get("joinphrase") or "")})
    return {"id": identity, "title": title,
            "preferred_title": preferred if preferred != title else None,
            "artist": artist_credit(value.get("artist-credit")), "artist_credits": credits}


class ApprovalPrefetch:
    def __init__(self, store, catalogue, searcher):
        self.store, self.catalogue, self.searcher = store, catalogue, searcher
        self.workers_enabled = False
        self.last_seed_at = 0.0
        self.last_seed_count = 0

    def init(self):
        with self.catalogue.db() as con:
            con.executescript(SCHEMA)
            con.execute("UPDATE approval_prefetch_jobs SET state='PENDING' WHERE state='RUNNING'")

    @staticmethod
    def _cached(con, kind, key):
        row = con.execute(
            "SELECT payload,expires_at FROM approval_prefetch_cache WHERE kind=? AND cache_key=?",
            (kind, key),
        ).fetchone()
        if not row:
            return None, False
        try:
            return json.loads(row["payload"]), row["expires_at"] > time.time()
        except (TypeError, ValueError):
            return None, False

    @staticmethod
    def _catalogue_entity(con, kind, identity):
        row = con.execute("SELECT data FROM entities WHERE kind=? AND id=?", (kind, identity)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["data"])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _job(kind, key, payload, priority):
        return kind, key, _json(payload), int(priority)

    def _enqueue_rows(self, rows):
        if not rows:
            return 0
        now = time.time()
        inserted = 0
        with self.catalogue.db() as con:
            for index, (kind, key, payload, priority) in enumerate(rows):
                cached, fresh = self._cached(con, kind, key)
                if fresh:
                    continue
                previous = con.execute(
                    "SELECT priority FROM approval_prefetch_jobs WHERE kind=? AND cache_key=?",
                    (kind, key),
                ).fetchone()
                con.execute(
                    """INSERT INTO approval_prefetch_jobs
                       (kind,cache_key,payload,priority,requested_at) VALUES (?,?,?,?,?)
                       ON CONFLICT(kind,cache_key) DO UPDATE SET
                       priority=MIN(approval_prefetch_jobs.priority,excluded.priority)""",
                    (kind, key, payload, priority, now + index * 0.0001),
                )
                inserted += not bool(previous)
        return inserted

    def enqueue_names(self, recordings=(), artists=(), *, priority=0):
        jobs = []
        seen = set()
        for kind, ids in (("recording", recordings), ("artist", artists)):
            for raw in ids:
                identity = valid_id(raw)
                if identity and (kind, identity) not in seen:
                    seen.add((kind, identity))
                    jobs.append(self._job(kind, identity, {"id": identity}, priority))
        return self._enqueue_rows(jobs)

    def enqueue_search(self, entity, title, artist, artist_id=None, page=1, *, priority=0):
        # Always enforce the same search validation and no broader Lucene input
        # than the normal interactive endpoint.
        query_for(entity, str(title), str(artist), artist_id)
        if type(page) is not int or not 1 <= page <= 100:
            raise ValueError("Search page must be between 1 and 100")
        key = search_key(entity, title, artist, artist_id, page)
        return self._enqueue_rows([self._job("search", key, {
            "entity": entity, "title": str(title)[:200], "artist": str(artist)[:200],
            "artist_id": valid_id(artist_id) if artist_id else None,
            "page": page,
        }, priority)])

    def cached_search(self, entity, title, artist, artist_id=None, page=1):
        key = search_key(entity, title, artist, artist_id, page)
        with self.catalogue.db() as con:
            payload, fresh = self._cached(con, "search", key)
        if payload is None:
            return None
        if not fresh:
            self.enqueue_search(entity, title, artist, artist_id, page, priority=1)
        return {**payload, "cache_status": "fresh" if fresh else "stale"}

    def remember_search(self, entity, title, artist, artist_id, page, result):
        key = search_key(entity, title, artist, artist_id, page)
        self._store("search", key, result, SEARCH_TTL)
        # All returned candidates are queued, not only three or twelve.
        self.enqueue_result_names(result, priority=5)

    def enqueue_result_names(self, result, *, priority=5):
        recordings, artists = [], []
        entity = result.get("entity")
        for item in result.get("items") or []:
            if entity == "artist":
                artists.append(item.get("id"))
            elif entity == "recording":
                recordings.append(item.get("id"))
                artists.extend(credit.get("id") for credit in item.get("artists") or []
                               if isinstance(credit, dict))
        return self.enqueue_names(recordings, artists, priority=priority)

    def _store(self, kind, key, payload, ttl):
        now = time.time()
        with self.catalogue.db() as con:
            con.execute(
                """INSERT INTO approval_prefetch_cache
                   (kind,cache_key,payload,fetched_at,expires_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(kind,cache_key) DO UPDATE SET
                   payload=excluded.payload,fetched_at=excluded.fetched_at,
                   expires_at=excluded.expires_at""",
                (kind, key, _json(payload), now, now + ttl),
            )
            con.execute("DELETE FROM approval_prefetch_jobs WHERE kind=? AND cache_key=?", (kind, key))

    def _artist(self, con, identity):
        exact, fresh = self._cached(con, "artist", identity)
        data = exact if exact and not exact.get("unavailable") else self._catalogue_entity(con, "artist", identity)
        if not data or not data.get("name"):
            return None, fresh if exact else False
        native = str(data["name"])[:200]
        english = preferred_english_name(data, field="name")
        return {"id": identity, "name": native,
                "preferred_name": english if english != native else None}, fresh

    def _recording(self, con, identity):
        exact, fresh = self._cached(con, "recording", identity)
        if exact and exact.get("unavailable"):
            exact = None
        item = (exact if exact else _recording_from_catalogue(
            self._catalogue_entity(con, "recording", identity), identity))
        if not item or not item.get("title"):
            return None, fresh if exact else False, []
        item = dict(item)
        credits = item.get("artist_credits") or []
        english = []
        has_alias = False
        need_artists = []
        for piece in credits:
            aid = valid_id(piece.get("id"))
            artist, ready = self._artist(con, aid) if aid else (None, True)
            if aid and not ready:
                need_artists.append(aid)
            alias = artist.get("preferred_name") if artist else None
            has_alias = has_alias or bool(alias)
            english.append((alias or piece.get("name") or "") + str(piece.get("joinphrase") or ""))
        item["preferred_artist"] = "".join(english)[:1000] if has_alias else None
        item.pop("artist_credits", None)  # Internal wiring, not additional public metadata.
        return item, fresh and not need_artists, need_artists

    def names_batch(self, recordings=(), artists=()):
        """One local DB read, no provider/network waiting on the UI thread."""
        rids = list(dict.fromkeys(filter(None, (valid_id(x) for x in recordings))))
        aids = list(dict.fromkeys(filter(None, (valid_id(x) for x in artists))))
        if len(rids) > MAX_BATCH_IDS or len(aids) > MAX_BATCH_IDS:
            raise ValueError("Too many MusicBrainz IDs in a single batch")
        result_r, result_a, pending_r, pending_a = {}, {}, [], []
        missing_r, missing_a = [], list(aids)
        with self.catalogue.db() as con:
            for identity in rids:
                label, complete, credited = self._recording(con, identity)
                if label:
                    result_r[identity] = label
                if not complete:
                    pending_r.append(identity)
                    exact, fresh = self._cached(con, "recording", identity)
                    if not fresh:
                        missing_r.append(identity)
                missing_a.extend(credited)
            for identity in aids:
                label, complete = self._artist(con, identity)
                if label:
                    result_a[identity] = label
                if not complete:
                    pending_a.append(identity)
        if self.workers_enabled:
            self.enqueue_names(missing_r, missing_a, priority=0)
        return {"recordings": result_r, "artists": result_a,
                "pending_recordings": pending_r, "pending_artists": pending_a,
                "cached": len(result_r) + len(result_a),
                "remaining": len(pending_r) + len(pending_a)}

    def schedule_context(self, context, *, priority=0):
        self.enqueue_names(
            [item.get("mbid") for item in context.get("choices") or [] if isinstance(item, dict)],
            priority=priority,
        )
        hints = context.get("search_hints") or {}
        try:
            return self.enqueue_search("recording", hints.get("title") or "",
                                       hints.get("artist") or "",
                                       hints.get("artist_id"), priority=priority)
        except (ValueError, TypeError):
            return 0

    def seed_pending_approvals(self):
        """Schedule every waiting track's search *and* all existing Beets matches.

        No audio reads and no provider calls in this producer. Metadata hints
        reuse the same retained source snapshot as the Approval page.
        """
        from source_metadata import source_snapshot

        seeds = []
        tracks = 0
        with self.store.db() as con:
            rows = con.execute(
                """SELECT t.id,t.user_id,t.choices,t.current_origin_id,t.pending_operation,
                          o.id AS origin_id,o.title AS origin_title,o.channel AS origin_channel
                   FROM tracks t LEFT JOIN track_origins o
                   ON o.id=COALESCE(json_extract(t.pending_operation,'$.origin_id'),
                                    t.current_origin_id)
                   WHERE t.operation_state='NEEDS_APPROVAL'
                   ORDER BY t.updated_at DESC LIMIT 10000"""
            ).fetchall()
            tracks = len(rows)
            for row in rows:
                for item in json.loads(row["choices"] or "[]"):
                    if isinstance(item, dict) and (mid := valid_id(item.get("mbid"))):
                        seeds.append(self._job("recording", mid, {"id": mid}, 20))
                if not row["origin_id"]:
                    continue
                origin = {"id": row["origin_id"], "title": row["origin_title"],
                          "channel": row["origin_channel"], "user_id": row["user_id"]}
                source = source_snapshot(con, origin)
                hint = search_hints(source, origin)
                if hint["artist"]:
                    alias = con.execute(
                        "SELECT * FROM metadata_artist_aliases WHERE user_id=? AND alias_key=?",
                        (row["user_id"], alias_key(hint["artist"])),
                    ).fetchone()
                    if alias:
                        hint = search_hints(source, origin, dict(alias))
                title, artist, aid = hint["title"], hint["artist"], hint["artist_id"]
                try:
                    query_for("recording", title, artist, aid)
                except (ValueError, TypeError):
                    continue
                payload = {"entity": "recording", "title": title, "artist": artist,
                           "artist_id": aid, "page": 1}
                seeds.append(self._job("search", search_key("recording", title, artist, aid, 1),
                                       payload, 20))
        scheduled = self._enqueue_rows(seeds)
        self.last_seed_at = time.time()
        self.last_seed_count = tracks
        if scheduled:
            self.store.event(
                "INFO", f"Approval MusicBrainz prefetch: indexed {tracks} waiting tracks; "
                f"{scheduled} distinct name/search jobs queued. Background only; no audio changes.",
                target="approval-prefetch",
            )
        return {"tracks": tracks, "new_jobs": scheduled}

    def claim(self):
        now = time.time()
        with self.catalogue.db() as con:
            row = con.execute(
                """SELECT * FROM approval_prefetch_jobs
                   WHERE state='PENDING' AND retry_at<=?
                   ORDER BY priority,requested_at,kind,cache_key LIMIT 1""",
                (now,),
            ).fetchone()
            if row:
                con.execute(
                    """UPDATE approval_prefetch_jobs SET state='RUNNING'
                       WHERE kind=? AND cache_key=?""",
                    (row["kind"], row["cache_key"]),
                )
            return dict(row) if row else None

    def defer(self, job, exc):
        attempt = int(job["attempts"]) + 1
        if isinstance(exc, ApiDeferred):
            when = max(time.time() + 2, float(exc.retry_at or time.time() + 30))
        else:
            when = time.time() + min(1800, 10 * 2 ** min(attempt, 7))
        with self.catalogue.db() as con:
            con.execute(
                """UPDATE approval_prefetch_jobs SET state='PENDING',attempts=?,
                   retry_at=?,last_error=? WHERE kind=? AND cache_key=?""",
                (attempt, when, str(exc)[:400], job["kind"], job["cache_key"]),
            )
        if attempt in {1, 5, 15}:
            logging.warning(
                "Approval MusicBrainz prefetch %s deferred (attempt %s): %s",
                job["kind"], attempt, str(exc)[:180],
            )

    async def process(self, job):
        kind = job["kind"]
        args = json.loads(job["payload"])
        if kind == "recording":
            value = await asyncio.to_thread(self.searcher.request_recording_names, args["id"])
            if value["id"] != args["id"]:
                raise ValueError("MusicBrainz returned a different recording identity")
            self._store(kind, args["id"], value, NAME_TTL)
            self.enqueue_names(artists=[v["id"] for v in value.get("artist_credits") or []
                                        if v.get("id")], priority=15)
        elif kind == "artist":
            value = await asyncio.to_thread(self.searcher.request_artist, args["id"])
            if value["id"] != args["id"]:
                raise ValueError("MusicBrainz returned a different artist identity")
            self._store(kind, args["id"], value, NAME_TTL)
        elif kind == "search":
            result = await self.searcher.search(
                args["entity"], args["title"], args["artist"], args["artist_id"],
                args["page"], bypass_prefetch=True,
            )
            self._store(kind, job["cache_key"], result, SEARCH_TTL)
            self.enqueue_result_names(result, priority=20)
        else:
            raise ValueError("Unsupported Approval prefetch operation")

    async def work(self, stopping):
        self.workers_enabled = True
        try:
            while not stopping.is_set():
                now = time.time()
                if now - self.last_seed_at > SWEEP_INTERVAL:
                    try:
                        await asyncio.to_thread(self.seed_pending_approvals)
                    except Exception as exc:
                        logging.warning("Approval prefetch library scan deferred: %s", exc)
                        self.last_seed_at = time.time()
                job = await asyncio.to_thread(self.claim)
                if not job:
                    await asyncio.sleep(1.5)
                    continue
                try:
                    await self.process(job)
                except asyncio.CancelledError:
                    raise
                except (ValueError, LookupError) as exc:
                    if any(word in str(exc).lower() for word in ("not found", "was not found")):
                        self._store(job["kind"], job["cache_key"],
                                    {"id": job["cache_key"], "unavailable": True},
                                    NEGATIVE_TTL)
                    else:
                        await asyncio.to_thread(self.defer, job, exc)
                except Exception as exc:
                    await asyncio.to_thread(self.defer, job, exc)
                await asyncio.sleep(0.15)
        finally:
            self.workers_enabled = False

    def status(self):
        with self.catalogue.db() as con:
            counts = {row["state"]: row["n"] for row in con.execute(
                "SELECT state,COUNT(*) AS n FROM approval_prefetch_jobs GROUP BY state"
            )}
            ready = con.execute(
                "SELECT COUNT(*) FROM approval_prefetch_cache WHERE expires_at>?",
                (time.time(),),
            ).fetchone()[0]
        return {"waiting_tracks": self.last_seed_count, "cached": ready,
                "queued": counts.get("PENDING", 0) + counts.get("RUNNING", 0),
                "background": self.workers_enabled}
