"""Persistent, non-blocking MusicBrainz metadata prefetch for Approval.

The catalogue SQLite database is shared across library users and already holds
public MusicBrainz entities. This adds a small durable name/search cache and a
resumable queue, never track modifications or inferred recording identities.

Only the dedicated background worker makes prefetch HTTP requests, using the
same HttpPolicy MusicBrainz portal/cooldown as interactive search and Beets.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

from http_policy import ApiDeferred, is_musicbrainz_transient
from localized_names import (
    artist_credit, english_release_language, has_non_latin_letters,
    preferred_english_name, preferred_recording_title,
)
from metadata_hints import alias_key, normalized_words, search_hints
from musicbrainz_search import PAGE_SIZE, query_for, valid_id
from source_metadata import source_snapshot

NAME_TTL = 30 * 86400
SEARCH_TTL = 7 * 86400
NEGATIVE_TTL = 7 * 86400
SWEEP_INTERVAL = 300
MAX_BATCH_IDS = 200
NORMAL_FAILURE_BUDGET = 3
TAIL_FAILURE_BUDGET = 3
MAX_PREFETCH_FAILURES = NORMAL_FAILURE_BUDGET + TAIL_FAILURE_BUDGET
TAIL_QUEUE_PRIORITY = 1000


def failed_job_id(kind, key):
    """Stable opaque ID for failed jobs; do not expose private search keys."""
    return hashlib.sha256(_json([kind, key]).encode("utf-8")).hexdigest()[:32]


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
CREATE TABLE IF NOT EXISTS approval_prefetch_search_refs (
 cache_key TEXT NOT NULL, track_id TEXT NOT NULL, PRIMARY KEY(cache_key,track_id)
);
CREATE INDEX IF NOT EXISTS approval_prefetch_search_track
 ON approval_prefetch_search_refs(track_id);
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
            "artist": artist_credit(value), "artist_credits": credits}


class ApprovalPrefetch:
    def __init__(self, store, catalogue, searcher):
        self.store, self.catalogue, self.searcher = store, catalogue, searcher
        self.workers_enabled = False
        self.last_seed_at = 0.0
        self.last_seed_count = 0
        self.pause_until = 0.0
        self._cleanup_lock = threading.RLock()

    def init(self):
        with self.catalogue.db() as con:
            con.executescript(SCHEMA)
            con.execute("UPDATE approval_prefetch_jobs SET state='PENDING' WHERE state='RUNNING'")
            # Older workers incremented attempts even while waiting for a
            # shared limiter. Such waits must not exhaust the new budget.
            con.execute("""UPDATE approval_prefetch_jobs SET attempts=0
                           WHERE state='PENDING' AND
                           last_error LIKE 'musicbrainz.org: waiting for shared provider cooldown%'""")
            con.execute("""UPDATE approval_prefetch_jobs
                           SET state='FAILED', retry_at=0
                           WHERE state='PENDING' AND attempts>=?""",
                        (MAX_PREFETCH_FAILURES,))
            con.execute("""UPDATE approval_prefetch_jobs SET priority=?
                           WHERE state='PENDING' AND attempts>=?""",
                        (TAIL_QUEUE_PRIORITY, NORMAL_FAILURE_BUDGET))

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
    def _catalogue_complete(con, kind, identity):
        """A fresh full Catalogue lookup with explicit alias fields is sufficient.

        Sparse credited-artist/search snippets do not include aliases, so they
        can be displayed provisionally but still require a full fetch.
        """
        if kind not in {"recording", "artist"}:
            return False
        row = con.execute(
            "SELECT data,checked_at FROM entities WHERE kind=? AND id=?",
            (kind, identity),
        ).fetchone()
        if not row:
            return False
        try:
            data = json.loads(row["data"])
            checked = datetime.fromisoformat(row["checked_at"].replace("Z", "+00:00"))
            return ("aliases" in data and bool(data.get("title" if kind == "recording" else "name"))
                    and (kind != "recording" or "artist-credit" in data)
                    and (datetime.now(UTC) - checked).total_seconds() < NAME_TTL)
        except (ValueError, TypeError, AttributeError):
            return False

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
                if fresh or self._catalogue_complete(con, kind, key):
                    continue
                previous = con.execute(
                    "SELECT priority FROM approval_prefetch_jobs WHERE kind=? AND cache_key=?",
                    (kind, key),
                ).fetchone()
                con.execute(
                    """INSERT INTO approval_prefetch_jobs
                       (kind,cache_key,payload,priority,requested_at) VALUES (?,?,?,?,?)
                       ON CONFLICT(kind,cache_key) DO UPDATE SET
                       priority=CASE
                         WHEN approval_prefetch_jobs.attempts>=?
                           OR approval_prefetch_jobs.state='FAILED'
                         THEN approval_prefetch_jobs.priority
                         ELSE MIN(approval_prefetch_jobs.priority,excluded.priority)
                       END""",
                    (kind, key, payload, priority, now + index * 0.0001, NORMAL_FAILURE_BUDGET),
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

    def enqueue_releases(self, recordings=(), *, page=1, priority=12):
        if type(page) is not int or not 1 <= page <= 20:
            raise ValueError("Release page must be between 1 and 20")
        jobs = []
        for identity in dict.fromkeys(filter(None, (valid_id(v) for v in recordings))):
            jobs.append(self._job("releases", f"{identity}:{page}",
                                  {"id": identity, "page": page}, priority))
        return self._enqueue_rows(jobs)

    def cached_releases(self, recording_id, page=1):
        identity = valid_id(recording_id)
        if identity is None or type(page) is not int or not 1 <= page <= 20:
            raise ValueError("Invalid MusicBrainz recording or release page")
        key = f"{identity}:{page}"
        with self.catalogue.db() as con:
            value, fresh = self._cached(con, "releases", key)
        if value is None:
            return None
        if not fresh and self.workers_enabled:
            self.enqueue_releases([identity], page=page, priority=1)
        return {**value, "cache_status": "fresh" if fresh else "stale"}

    def enqueue_pseudo_releases(self, releases, *, priority=15):
        jobs = []
        for release in releases:
            if (str(release.get("status") or "").casefold() == "pseudo-release"
                    and (identity := valid_id(release.get("id")))):
                jobs.append(self._job("pseudo_release", identity,
                                      {"id": identity}, priority))
        return self._enqueue_rows(jobs)

    def enqueue_official_releases(self, recording_id, releases, *, priority=16):
        """Bound extra lookups to two English editions of a non-Latin recording.

        The release-browse response already carries its title-language code.
        Don't fetch every regional/compilation edition or compete with the
        already busy shared MusicBrainz host for English-native recordings.
        """
        with self.catalogue.db() as con:
            exact, _ = self._cached(con, "recording", recording_id)
            catalogue_raw = self._catalogue_entity(con, "recording", recording_id)
            raw = catalogue_raw or exact or {}
            native = str(raw.get("title") or "")
            if not has_non_latin_letters(native):
                return 0
            known = (exact or {}).get("preferred_title")
            existing = preferred_recording_title(
                con, recording_id, raw, known_alias=known)
            # No additional MusicBrainz requests if either a recording alias
            # or an official English track is already available locally.
            if existing["source"] in {"recording_alias", "official_release"}:
                return 0
        english = [
            rel for rel in releases
            if (str(rel.get("status") or "").casefold() == "official"
                and english_release_language(rel.get("language"))
                and valid_id(rel.get("id")))
        ]
        if not english:
            # Older cached browse results omitted release text language.
            # Probe at most ONE plausible Latin-named official release; the
            # full response still MUST prove language=eng and contain the
            # identical Recording ID before any title is accepted.
            english = [
                rel for rel in releases
                if (str(rel.get("status") or "").casefold() == "official"
                    and not rel.get("language") and valid_id(rel.get("id"))
                    and any(ch.isalpha() for ch in str(rel.get("title") or ""))
                    and not has_non_latin_letters(rel.get("title")))
            ][:1]
        # Recording IDs, not title similarity, identify every candidate.
        ids = list(dict.fromkeys(rel["id"] for rel in english))[:2]
        return self._enqueue_rows([
            self._job("official_release", identity, {"id": identity}, priority)
            for identity in ids
        ])

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
        query_for(entity, str(title), str(artist), artist_id)
        if type(page) is not int or not 1 <= page <= 100:
            raise ValueError("Search page must be between 1 and 100")
        key = search_key(entity, title, artist, artist_id, page)
        with self.catalogue.db() as con:
            payload, fresh = self._cached(con, "search", key)
        if payload is None:
            return None
        if not fresh:
            self.enqueue_search(entity, title, artist, artist_id, page, priority=1)
        return {**payload, "cache_status": "fresh" if fresh else "stale"}

    def remember_search_owner(self, track_id, entity, title, artist, artist_id=None, page=1):
        """Associate *manual* Approval searches with a waiting track for cleanup.

        Track ownership/state is checked by the API. Keys alone are public
        MusicBrainz requests; this table stores no private source metadata.
        """
        key = search_key(entity, title, artist, artist_id, page)
        with self.catalogue.db() as con:
            con.execute("INSERT OR IGNORE INTO approval_prefetch_search_refs VALUES (?,?)",
                        (key, track_id))

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
        names = self.enqueue_names(recordings, artists, priority=priority)
        return names + self.enqueue_releases(recordings, priority=priority + 12)

    def _store(self, kind, key, payload, ttl, *, expected_job=False):
        now = time.time()
        with self.catalogue.db() as con:
            # A cleanup may remove a job while its provider request is still
            # in flight. Never resurrect deleted entries after the response.
            if expected_job and not con.execute(
                "SELECT 1 FROM approval_prefetch_jobs WHERE kind=? AND cache_key=? AND state='RUNNING'",
                (kind, key),
            ).fetchone():
                return False
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
        return True

    def _artist(self, con, identity):
        exact, fresh = self._cached(con, "artist", identity)
        full_catalogue = self._catalogue_complete(con, "artist", identity)
        data = exact if exact and not exact.get("unavailable") else self._catalogue_entity(con, "artist", identity)
        if not data or not data.get("name"):
            return None, fresh if exact else full_catalogue
        native = str(data["name"])[:200]
        english = preferred_english_name(data, field="name")
        return {"id": identity, "name": native,
                "preferred_name": english if english != native else None}, fresh or full_catalogue

    @staticmethod
    def _valid_pseudo_title(title, canonical):
        """Do not mistake romanization or an unchanged track name for an English alias."""
        title = str(title or "").strip()[:1000]
        return (bool(re.search(r"[A-Za-z]", title))
                and not bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", title))
                and title.casefold() != str(canonical or "").casefold())

    def _pseudo_title(self, con, identity, canonical):
        value, _ = self._cached(con, "pseudo_title", identity)
        if (value and self._valid_pseudo_title(value.get("title"), canonical)
                and valid_id(value.get("release_id"))):
            return value
        # Reuse exact recording/track/release relationships already indexed by
        # Catalogue before doing any new remote lookups.
        rows = con.execute(
            """SELECT t.title,t.release_id,r.data FROM release_tracks AS t
               JOIN entities AS r ON r.kind='release' AND r.id=t.release_id
               WHERE t.recording_id=? ORDER BY t.release_id LIMIT 100""",
            (identity,),
        ).fetchall()
        for row in rows:
            try:
                release = json.loads(row["data"])
                if str(release.get("status") or "").casefold() != "pseudo-release":
                    continue
                if not self._valid_pseudo_title(row["title"], canonical):
                    continue
                lang = str((release.get("text-representation") or {}).get("language") or "").lower()
                return {"title": row["title"], "release_id": row["release_id"],
                        "language": lang, "source": "pseudo_release"}
            except (ValueError, TypeError):
                continue
        return None

    def _pending_pseudo(self, con, identity):
        """Keep the UI polling while a verified alternative track title is loading."""
        page = con.execute(
            "SELECT 1 FROM approval_prefetch_jobs WHERE kind='releases' AND cache_key LIKE ? LIMIT 1",
            (identity + ":%",),
        ).fetchone()
        if page:
            return True
        pages = con.execute(
            "SELECT payload FROM approval_prefetch_cache "
            "WHERE kind='releases' AND cache_key LIKE ?", (identity + ":%",)
        )
        for item in pages:
            try:
                releases = json.loads(item["payload"]).get("releases") or []
            except (TypeError, ValueError):
                continue
            pseudo = [valid_id(r.get("id")) for r in releases
                      if str(r.get("status") or "").casefold() == "pseudo-release"]
            official = [valid_id(r.get("id")) for r in releases
                        if str(r.get("status") or "").casefold() == "official"
                        and (english_release_language(r.get("language"))
                             or (not r.get("language")
                                 and not has_non_latin_letters(r.get("title"))))]
            for kind, keys in (("pseudo_release", [rid for rid in pseudo if rid]),
                               ("official_release", [rid for rid in official if rid])):
                for batch in self._chunks(keys):
                    marks = ",".join("?" for _ in batch)
                    if con.execute(
                        "SELECT 1 FROM approval_prefetch_jobs "
                        f"WHERE kind=? AND cache_key IN ({marks}) "
                        "AND state IN ('PENDING','RUNNING') LIMIT 1",
                        (kind, *batch),
                    ).fetchone():
                        return True
        return False

    def _recording(self, con, identity):
        exact, fresh = self._cached(con, "recording", identity)
        full_catalogue = self._catalogue_complete(con, "recording", identity)
        unavailable = bool(exact and exact.get("unavailable"))
        if unavailable:
            exact = None
        item = (exact if exact else _recording_from_catalogue(
            self._catalogue_entity(con, "recording", identity), identity))
        if not item or not item.get("title"):
            # 404s are negatively cached: do not display an endless pending
            # spinner or requeue an ID MusicBrainz explicitly cannot resolve.
            return None, fresh if unavailable else full_catalogue, []
        item = dict(item)
        native = item.get("title") or ""
        raw = self._catalogue_entity(con, "recording", identity) or {"title": native}
        # The exact-name cache has verified recording aliases even when a
        # sparse Catalogue entry does not. The shared resolver then considers
        # complete English official tracklists before pseudo-releases.
        chosen = preferred_recording_title(
            con, identity, raw, known_alias=item.get("preferred_title"),
            pseudo=self._pseudo_title(con, identity, native),
        )
        item["preferred_title"] = chosen["title"] if chosen["title"] != native else None
        if item["preferred_title"]:
            item["preferred_title_source"] = chosen["source"]
            if chosen["release_id"]:
                item["preferred_title_release_id"] = chosen["release_id"]
                item["preferred_title_language"] = chosen["language"] or ""
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
        title_wait = ((not item.get("preferred_title") or
                       item.get("preferred_title_source") == "pseudo_release") and
                      has_non_latin_letters(native) and
                      self._pending_pseudo(con, identity))
        return (item,
                (((fresh or full_catalogue) and not need_artists and not title_wait)
                 or (unavailable and fresh)),
                need_artists)

    def names_batch(self, recordings=(), artists=()):
        """One local DB read, no provider/network waiting on the UI thread."""
        rids = list(dict.fromkeys(filter(None, (valid_id(x) for x in recordings))))
        aids = list(dict.fromkeys(filter(None, (valid_id(x) for x in artists))))
        if len(rids) > MAX_BATCH_IDS or len(aids) > MAX_BATCH_IDS:
            raise ValueError("Too many MusicBrainz IDs in a single batch")
        result_r, result_a, pending_r, pending_a = {}, {}, [], []
        missing_r, missing_a = [], list(aids)
        # Schedule first so the same read sees pending release lookups and
        # keeps polling until an English official/pseudo title arrives.
        if self.workers_enabled:
            self.enqueue_releases(rids, priority=8)
            # Backfill lists cached *before* official-title support existed:
            # those fresh snapshots would otherwise never be processed again
            # until their 30-day TTL expires. All calls are local SQLite;
            # only the bounded worker fetches selected exact editions.
            with self.catalogue.db() as con:
                legacy = []
                for rid in rids:
                    cached, _ = self._cached(con, "releases", f"{rid}:1")
                    if cached:
                        legacy.append((rid, cached.get("releases") or []))
            for rid, releases in legacy:
                self.enqueue_official_releases(rid, releases, priority=16)
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
        identities = [item.get("mbid") for item in context.get("choices") or []
                      if isinstance(item, dict)]
        self.enqueue_names(identities, priority=priority)
        self.enqueue_releases(identities, priority=priority + 6)
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
                   ON o.id=COALESCE(
                       CASE WHEN json_valid(t.pending_operation)
                            THEN json_extract(t.pending_operation,'$.origin_id') END,
                       t.current_origin_id)
                   WHERE t.operation_state='NEEDS_APPROVAL'
                   ORDER BY t.updated_at DESC LIMIT 10000"""
            ).fetchall()
            tracks = len(rows)
            for row in rows:
                try:
                    choices = json.loads(row["choices"] or "[]")
                    if not isinstance(choices, list):
                        choices = []
                except (ValueError, TypeError):
                    choices = []
                for item in choices:
                    if isinstance(item, dict) and (mid := valid_id(item.get("mbid"))):
                        seeds.append(self._job("recording", mid, {"id": mid}, 20))
                        seeds.append(self._job("releases", f"{mid}:1",
                                               {"id": mid, "page": 1}, 32))
                if not row["origin_id"]:
                    continue
                origin = {"id": row["origin_id"], "title": row["origin_title"],
                          "channel": row["origin_channel"], "user_id": row["user_id"]}
                try:
                    source = source_snapshot(con, origin)
                    hint = search_hints(source, origin)
                except (ValueError, TypeError, KeyError):
                    # A malformed retained source must not block every other
                    # waiting track from being prefetched.
                    continue
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
        # Existing search pages may have been cached before the release/pseudo
        # resolver was installed. Backfill *every* result of pending reviews,
        # without re-fetching their already cached MusicBrainz search.
        search_keys = {key for kind, key, _, _ in seeds if kind == "search"}
        with self.catalogue.db() as con:
            for batch in self._chunks(tuple(search_keys)):
                marks = ",".join("?" for _ in batch)
                for row in con.execute(
                    "SELECT payload FROM approval_prefetch_cache WHERE kind='search' "
                    f"AND cache_key IN ({marks})", batch
                ):
                    try:
                        result = json.loads(row["payload"])
                        for item in result.get("items") or []:
                            if (result.get("entity") == "recording" and
                                    (rid := valid_id(item.get("id")))):
                                seeds.append(self._job("releases", f"{rid}:1",
                                                       {"id": rid, "page": 1}, 35))
                    except (ValueError, TypeError, AttributeError):
                        continue
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

    def has_ready_work(self):
        with self.catalogue.db() as con:
            return bool(con.execute(
                """SELECT 1 FROM approval_prefetch_jobs
                   WHERE state='PENDING' AND retry_at<=? LIMIT 1""",
                (time.time(),),
            ).fetchone())

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
        """After three failures, move to tail; after six, disable the item.

        Shared slot waits do not consume the retry budget. Only provider-wide
        cooldowns pause all requests; a bad release lookup must not do so.
        """
        now = time.time()
        error = str(exc)[:400]
        provider_wait = isinstance(exc, ApiDeferred)
        global_pause = provider_wait and error.startswith("musicbrainz.org:")
        shared_outage = global_pause and is_musicbrainz_transient(error)
        attempt = int(job["attempts"]) + (0 if shared_outage else 1)
        tail = attempt >= NORMAL_FAILURE_BUDGET
        disabled = attempt >= MAX_PREFETCH_FAILURES
        when = (max(now + 2, float(exc.retry_at or now + 30)) if provider_wait else
                now + min(1800, 10 * 2 ** min(attempt, 7)))
        with self.catalogue.db() as con:
            updated = con.execute(
                """UPDATE approval_prefetch_jobs
                   SET state=?,attempts=?,priority=?,requested_at=?,retry_at=?,last_error=?
                   WHERE kind=? AND cache_key=? AND state='RUNNING'""",
                ("FAILED" if disabled else "PENDING", attempt,
                 max(TAIL_QUEUE_PRIORITY, int(job["priority"])) if tail else int(job["priority"]),
                 now if tail and int(job["attempts"]) < NORMAL_FAILURE_BUDGET else job["requested_at"],
                 0 if disabled else when, error, job["kind"], job["cache_key"]),
            ).rowcount
        if not updated:
            return  # The job was removed while its HTTP request was in flight.
        if disabled:
            self.store.event(
                "WARNING", f"Approval prefetch disabled one {job['kind']} request "
                f"after {MAX_PREFETCH_FAILURES} failed executions. Retry it manually in Catalogue.",
                target="approval-prefetch",
            )
        elif attempt == NORMAL_FAILURE_BUDGET:
            self.store.event(
                "WARNING", f"Approval prefetch moved one {job['kind']} request to the back "
                f"of the queue after {NORMAL_FAILURE_BUDGET} failures.",
                target="approval-prefetch",
            )
        elif not provider_wait and attempt in {1, 4}:
            logging.warning("Approval prefetch %s failed (%s): %s",
                            job["kind"], attempt, error[:180])
        if global_pause:
            previous = self.pause_until
            self.pause_until = max(previous, when)
            if previous < time.time() and self.pause_until > time.time() + 3:
                self.store.event(
                    "WARNING",
                    f"Approval MusicBrainz prefetch paused for provider cooldown "
                    f"({int(self.pause_until - time.time())} s). Queued jobs are retained.",
                    target="approval-prefetch",
                )

    def failed_jobs(self, user_id, *, limit=50, offset=0):
        """Paginated failures, with private search terms masked across users."""
        self.store.require_user(user_id)
        limit, offset = max(1, min(100, int(limit))), max(0, int(offset))
        with self.catalogue.db() as con:
            total = con.execute(
                "SELECT COUNT(*) FROM approval_prefetch_jobs WHERE state='FAILED'"
            ).fetchone()[0]
            rows = con.execute(
                """SELECT kind,cache_key,payload,attempts,last_error
                   FROM approval_prefetch_jobs WHERE state='FAILED'
                   ORDER BY requested_at,kind,cache_key LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            search_keys = [row["cache_key"] for row in rows if row["kind"] == "search"]
            refs = []
            if search_keys:
                marks = ",".join("?" for _ in search_keys)
                refs = con.execute(
                    "SELECT cache_key,track_id FROM approval_prefetch_search_refs "
                    f"WHERE cache_key IN ({marks})", search_keys,
                ).fetchall()
        own_tracks = {row["id"] for row in self.store.rows(
            "SELECT id FROM tracks WHERE user_id=?", (user_id,)
        )} if refs else set()
        own_searches = {row["cache_key"] for row in refs if row["track_id"] in own_tracks}
        items = []
        for row in rows:
            kind, key = row["kind"], row["cache_key"]
            identity = failed_job_id(kind, key)
            if kind == "search":
                label = "Search request #" + identity[:10]
                if key in own_searches:
                    try:
                        payload = json.loads(row["payload"])
                        label = (str(payload.get("title") or "Search")[:110] +
                                 (" — " + str(payload["artist"])[:90]
                                  if payload.get("artist") else ""))
                    except (ValueError, TypeError, AttributeError, KeyError):
                        pass
            else:
                label = kind.replace("_", " ").title() + " · " + key[:100]
            error = (row["last_error"] or "")[:250]
            if kind == "search" and key not in own_searches:
                # Network failures can contain the full URL/query string.
                # A shared-cache job must not reveal another user's searches.
                status_code = re.search(r"\bHTTP\s+[0-9]{3}\b", error, re.IGNORECASE)
                error = ("MusicBrainz " + status_code.group(0).upper()
                         if status_code else "Search failure; details private to its library.")
            items.append({
                "job_id": identity, "kind": kind, "label": label,
                "attempts": row["attempts"], "last_error": error,
            })
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    def retry_failed(self, user_id, job_id):
        """Manually give one disabled item three more low-priority attempts."""
        self.store.require_user(user_id)
        with self.catalogue.db() as con:
            con.execute("BEGIN IMMEDIATE")
            match = next(
                (row for row in con.execute(
                    "SELECT kind,cache_key FROM approval_prefetch_jobs WHERE state='FAILED'"
                ) if failed_job_id(row["kind"], row["cache_key"]) == job_id),
                None,
            )
            if match is None:
                return False
            updated = con.execute(
                """UPDATE approval_prefetch_jobs
                   SET state='PENDING',attempts=?,priority=?,requested_at=?,
                       retry_at=0,last_error=NULL
                   WHERE kind=? AND cache_key=? AND state='FAILED'""",
                (NORMAL_FAILURE_BUDGET, TAIL_QUEUE_PRIORITY, time.time(),
                 match["kind"], match["cache_key"]),
            ).rowcount
        return bool(updated)

    async def process(self, job):
        kind = job["kind"]
        args = json.loads(job["payload"])
        if kind == "recording":
            value = await asyncio.to_thread(self.searcher.request_recording_names, args["id"])
            if value["id"] != args["id"]:
                raise ValueError("MusicBrainz returned a different recording identity")
            with self._cleanup_lock:
                if self._store(kind, args["id"], value, NAME_TTL, expected_job=True):
                    self.enqueue_names(artists=[v["id"] for v in value.get("artist_credits") or []
                                                if v.get("id")], priority=15)
        elif kind == "releases":
            value = await asyncio.to_thread(
                self.searcher.request_releases, args["id"], args["page"])
            if value["recording_id"] != args["id"] or value["page"] != args["page"]:
                raise ValueError("MusicBrainz returned an unrelated release list")
            with self._cleanup_lock:
                if self._store(kind, job["cache_key"], value, NAME_TTL, expected_job=True):
                    self.enqueue_pseudo_releases(value["releases"], priority=14)
                    # Only the first browse page may add English official
                    # lookups. Even recordings on hundreds of compilations
                    # must never create an unbounded API work multiplier.
                    if args["page"] == 1:
                        self.enqueue_official_releases(args["id"], value["releases"], priority=16)
                    if args["page"] < 20 and args["page"] * 100 < value["total"]:
                        self.enqueue_releases([args["id"]], page=args["page"] + 1, priority=28)
        elif kind == "official_release":
            value = await asyncio.to_thread(
                self.searcher.request_official_release_tracklist, args["id"])
            if value["id"] != args["id"]:
                raise ValueError("MusicBrainz returned an unrelated official release")
            with self._cleanup_lock:
                with self.catalogue.db() as con:
                    current = con.execute(
                        "SELECT 1 FROM approval_prefetch_jobs "
                        "WHERE kind='official_release' AND cache_key=? AND state='RUNNING'",
                        (args["id"],),
                    ).fetchone()
                    if not current:
                        return
                    if value.get("accepted"):
                        release = value["release"]
                        self.catalogue.ingest(con, "release", release)
                        self.catalogue._store_release_tracks(con, release)
                self._store(kind, args["id"],
                            {"id": args["id"], "accepted": bool(value.get("accepted"))},
                            NAME_TTL, expected_job=True)
        elif kind == "pseudo_release":
            value = await asyncio.to_thread(
                self.searcher.request_pseudo_release_titles, args["id"])
            if value.get("id") != args["id"]:
                raise ValueError("MusicBrainz returned an unrelated pseudo-release")
            with self._cleanup_lock:
                if self._store(kind, job["cache_key"], value, NAME_TTL, expected_job=True):
                    for title in value.get("titles") or []:
                        identity = valid_id(title.get("recording_id"))
                        candidate = {"title": title.get("title"),
                                     "release_id": args["id"],
                                     "language": title.get("language") or "",
                                     "source": "pseudo_release"}
                        if not identity:
                            continue
                        with self.catalogue.db() as con:
                            previous, _ = self._cached(con, "pseudo_title", identity)
                        if not self._valid_pseudo_title(candidate["title"], None):
                            continue
                        # Prefer verified English pseudo-releases over ones
                        # with unspecified tracklist language. Stable tie-break.
                        rank = lambda d: (d.get("language") in {"eng", "en"},
                                          str(d.get("release_id") or ""))
                        if previous and rank(previous) >= rank(candidate):
                            continue
                        self._store("pseudo_title", identity, candidate, NAME_TTL)
        elif kind == "artist":
            value = await asyncio.to_thread(self.searcher.request_artist, args["id"])
            if value["id"] != args["id"]:
                raise ValueError("MusicBrainz returned a different artist identity")
            with self._cleanup_lock:
                self._store(kind, args["id"], value, NAME_TTL, expected_job=True)
        elif kind == "search":
            result = await self.searcher.search(
                args["entity"], args["title"], args["artist"], args["artist_id"],
                args["page"], bypass_prefetch=True,
            )
            with self._cleanup_lock:
                if self._store(kind, job["cache_key"], result, SEARCH_TTL, expected_job=True):
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
                remaining_pause = self.pause_until - time.time()
                if remaining_pause > 0:
                    await asyncio.sleep(min(10, remaining_pause))
                    continue
                if not await asyncio.to_thread(self.has_ready_work):
                    await asyncio.sleep(1.5)
                    continue
                wait = await asyncio.to_thread(self.store.reserve_musicbrainz_background)
                if wait > 0:
                    await asyncio.sleep(min(5.0, wait))
                    continue
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
                        with self._cleanup_lock:
                            self._store(job["kind"], job["cache_key"],
                                        {"id": job["cache_key"], "unavailable": True},
                                        NEGATIVE_TTL, expected_job=True)
                    else:
                        await asyncio.to_thread(self.defer, job, exc)
                except Exception as exc:
                    await asyncio.to_thread(self.defer, job, exc)
                await asyncio.sleep(0.15)
        finally:
            self.workers_enabled = False

    @staticmethod
    def _chunks(values, width=250):
        values = list(values)
        for start in range(0, len(values), width):
            yield values[start:start + width]

    def _retained_keys(self, owner_con, cache_con):
        """Protect ALL users' local approved music and pending approval matches.

        Do not infer an approved identity from a video, a text search or a
        similarity score. Only a stored track MBID, an explicit Beets candidate,
        or a cached search owned by a *still-pending* approval is retained.
        """
        retain = {"recording": set(), "artist": set(), "search": set()}
        waiting = set()
        rows = owner_con.execute(
            """SELECT t.id,t.user_id,t.mbid,t.health,t.operation_state,t.choices,
                      t.pending_operation,
                      o.id AS origin_id,o.title AS origin_title,o.channel AS origin_channel
               FROM tracks AS t LEFT JOIN track_origins AS o
               ON o.id=COALESCE(
                  CASE WHEN json_valid(t.pending_operation)
                       THEN json_extract(t.pending_operation,'$.origin_id') END,
                  t.current_origin_id)
               WHERE t.mbid IS NOT NULL OR t.operation_state='NEEDS_APPROVAL'"""
        ).fetchall()
        for row in rows:
            if rid := valid_id(row["mbid"]):
                retain["recording"].add(rid)
            if row["operation_state"] != "NEEDS_APPROVAL":
                continue
            waiting.add(row["id"])
            try:
                items = json.loads(row["choices"] or "[]")
                if not isinstance(items, list):
                    items = []
            except (ValueError, TypeError):
                items = []
            for choice in items:
                if isinstance(choice, dict) and (rid := valid_id(choice.get("mbid"))):
                    retain["recording"].add(rid)
            if not row["origin_id"]:
                continue
            origin = {"id": row["origin_id"], "user_id": row["user_id"],
                      "title": row["origin_title"], "channel": row["origin_channel"]}
            try:
                source = source_snapshot(owner_con, origin)
                hint = search_hints(source, origin)
                if hint["artist"]:
                    alias = owner_con.execute(
                        "SELECT * FROM metadata_artist_aliases WHERE user_id=? AND alias_key=?",
                        (row["user_id"], alias_key(hint["artist"])),
                    ).fetchone()
                    if alias:
                        hint = search_hints(source, origin, dict(alias))
                query_for("recording", hint["title"], hint["artist"], hint["artist_id"])
                retain["search"].add(search_key(
                    "recording", hint["title"], hint["artist"], hint["artist_id"], 1))
            except (ValueError, TypeError, KeyError):
                continue

        # Confirmed aliases are user choices and must never be discarded as
        # unrelated prefetch clutter (even if the original recording is gone).
        for row in owner_con.execute("SELECT DISTINCT artist_id FROM metadata_artist_aliases"):
            if aid := valid_id(row["artist_id"]):
                retain["artist"].add(aid)

        # User-initiated searches may not match the default title/artist hint.
        # Keep those search pages while the associated track still awaits review.
        for row in cache_con.execute("SELECT cache_key,track_id FROM approval_prefetch_search_refs"):
            if row["track_id"] in waiting:
                retain["search"].add(row["cache_key"])

        # All results of a pending Approval search are still relevant. Preserve
        # their exact recording and credited-artist metadata, not just the
        # first/highest-ranked suggestion.
        for keys in self._chunks(retain["search"]):
            markers = ",".join("?" for _ in keys)
            for row in cache_con.execute(
                "SELECT payload FROM approval_prefetch_cache WHERE kind='search' "
                f"AND cache_key IN ({markers})", keys
            ):
                try:
                    result = json.loads(row["payload"])
                except (TypeError, ValueError):
                    continue
                for item in result.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    kind = result.get("entity")
                    if kind == "recording" and (rid := valid_id(item.get("id"))):
                        retain["recording"].add(rid)
                    elif kind == "artist" and (aid := valid_id(item.get("id"))):
                        retain["artist"].add(aid)
                    for artist in item.get("artists") or []:
                        if isinstance(artist, dict) and (aid := valid_id(artist.get("id"))):
                            retain["artist"].add(aid)

        # Artist links are shared by many recordings. Gather the artist credits
        # from both exact name lookups and Catalogue's persistent graph.
        for ids in self._chunks(retain["recording"]):
            markers = ",".join("?" for _ in ids)
            for table, kind in (("approval_prefetch_cache", "recording"),
                                ("entities", "recording")):
                for row in cache_con.execute(
                    f"SELECT payload AS data FROM {table} WHERE kind=? AND cache_key IN ({markers})"
                    if table == "approval_prefetch_cache" else
                    f"SELECT data FROM entities WHERE kind=? AND id IN ({markers})",
                    (kind, *ids),
                ):
                    try:
                        data = json.loads(row["data"])
                    except (ValueError, TypeError):
                        continue
                    for credit in (data.get("artist_credits") or data.get("artist-credit") or []):
                        if not isinstance(credit, dict):
                            continue
                        artist = credit.get("artist") or credit
                        if isinstance(artist, dict) and (aid := valid_id(artist.get("id"))):
                            retain["artist"].add(aid)
            for row in cache_con.execute(
                "SELECT source_id FROM links WHERE source_kind='artist' "
                f"AND target_kind='recording' AND target_id IN ({markers})", ids
            ):
                if aid := valid_id(row["source_id"]):
                    retain["artist"].add(aid)
        # Release-list snapshots and pseudo-title provenance are retained
        # alongside their exact recording identity. Do not throw away a
        # translation just because it was discovered on a pseudo-release.
        retained_rec = retain["recording"]
        retain["pseudo_title"] = set(retained_rec)
        all_lists = cache_con.execute(
            """SELECT kind,cache_key,payload FROM approval_prefetch_cache
               WHERE kind IN ('releases','pseudo_release','official_release')"""
        ).fetchall()
        all_jobs = cache_con.execute(
            """SELECT kind,cache_key FROM approval_prefetch_jobs
               WHERE kind IN ('releases','pseudo_release','official_release')"""
        ).fetchall()
        pseudo_releases = set()
        official_releases = set()
        for row in all_lists:
            if row["kind"] == "releases":
                rid = valid_id(row["cache_key"].split(":")[0])
                if rid not in retained_rec:
                    continue
                retain.setdefault("releases", set()).add(row["cache_key"])
                try:
                    for rel in json.loads(row["payload"]).get("releases") or []:
                        if (release_id := valid_id(rel.get("id"))):
                            if str(rel.get("status") or "").casefold() == "pseudo-release":
                                pseudo_releases.add(release_id)
                            elif (str(rel.get("status") or "").casefold() == "official"
                                  and english_release_language(rel.get("language"))):
                                official_releases.add(release_id)
                except (ValueError, TypeError):
                    continue
            elif row["kind"] == "pseudo_release":
                try:
                    ids = {valid_id(x.get("recording_id")) for x in
                           (json.loads(row["payload"]).get("titles") or [])}
                    if ids.intersection(retained_rec):
                        pseudo_releases.add(row["cache_key"])
                except (ValueError, TypeError):
                    continue
        for row in all_jobs:
            if row["kind"] == "releases":
                if valid_id(row["cache_key"].split(":")[0]) in retained_rec:
                    retain.setdefault("releases", set()).add(row["cache_key"])
        retain["pseudo_release"] = pseudo_releases
        retain["official_release"] = official_releases
        return retain, waiting

    @staticmethod
    def _cleanup_plan(cache_con, retain, waiting):
        cached = cache_con.execute(
            "SELECT kind,cache_key,length(CAST(payload AS BLOB)) AS bytes "
            "FROM approval_prefetch_cache"
        ).fetchall()
        queued = cache_con.execute(
            "SELECT kind,cache_key FROM approval_prefetch_jobs"
        ).fetchall()
        remove_cache = [(row["kind"], row["cache_key"]) for row in cached
                        if row["cache_key"] not in retain.get(row["kind"], set())]
        remove_jobs = [(row["kind"], row["cache_key"]) for row in queued
                       if row["cache_key"] not in retain.get(row["kind"], set())]
        removed_set = set(remove_cache)
        reclaim = sum(row["bytes"] or 0 for row in cached
                      if (row["kind"], row["cache_key"]) in removed_set)
        stale_refs = [(row["cache_key"], row["track_id"]) for row in cache_con.execute(
            "SELECT cache_key,track_id FROM approval_prefetch_search_refs"
        ) if row["track_id"] not in waiting]
        return {
            "remove_cache": remove_cache, "remove_jobs": remove_jobs,
            "remove_refs": stale_refs,
            "remove_entries": len(remove_cache),
            "remove_queued": len(remove_jobs),
            "reclaim_bytes": reclaim,
            "keep_entries": len(cached) - len(remove_cache),
            "keep_queued": len(queued) - len(remove_jobs),
            "protected_recordings": len(retain["recording"]),
            "protected_artists": len(retain["artist"]),
            "protected_searches": len(retain["search"]),
            "pending_tracks": len(waiting),
            "scope": "approval_prefetch_only",
        }

    @staticmethod
    def _public_cleanup(plan):
        return {key: value for key, value in plan.items()
                if key not in {"remove_cache", "remove_jobs", "remove_refs"}}

    def cleanup_preview(self):
        with self.store.db() as owner_con:
            with self.catalogue.db() as cache_con:
                retain, waiting = self._retained_keys(owner_con, cache_con)
                return self._public_cleanup(self._cleanup_plan(cache_con, retain, waiting))

    def cleanup_unused(self):
        """Remove obsolete Approval cache/jobs atomically; NEVER alter Catalogue.

        Holding the owners DB write lock while planning prevents a recording
        becoming approved between retention analysis and the cache deletion.
        """
        with self._cleanup_lock:
            with self.store.db() as owner_con:
                owner_con.execute("BEGIN IMMEDIATE")
                with self.catalogue.db() as cache_con:
                    cache_con.execute("BEGIN IMMEDIATE")
                    retain, waiting = self._retained_keys(owner_con, cache_con)
                    plan = self._cleanup_plan(cache_con, retain, waiting)
                    cache_con.executemany(
                        "DELETE FROM approval_prefetch_cache WHERE kind=? AND cache_key=?",
                        plan["remove_cache"])
                    cache_con.executemany(
                        "DELETE FROM approval_prefetch_jobs WHERE kind=? AND cache_key=?",
                        plan["remove_jobs"])
                    cache_con.executemany(
                        "DELETE FROM approval_prefetch_search_refs WHERE cache_key=? AND track_id=?",
                        plan["remove_refs"])
        # The on-disk Catalogue database is shared with Graph, Language and
        # other features. Do not VACUUM it during ingestion or drop its entities.
        summary = self._public_cleanup(plan)
        self.store.event(
            "INFO", "Approval cache cleanup: "
            f"{summary['remove_entries']} cached records and "
            f"{summary['remove_queued']} obsolete jobs removed; "
            f"{summary['keep_entries']} entries and "
            f"{summary['keep_queued']} jobs retained for local or pending music. "
            "Catalogue graph and music files untouched.",
            target="approval-prefetch",
        )
        return summary

    def status(self):
        """Durable counts. The denominator grows when new search matches appear."""
        now = time.time()
        with self.store.db() as con:
            waiting_tracks = con.execute(
                "SELECT COUNT(*) FROM tracks WHERE operation_state='NEEDS_APPROVAL'"
            ).fetchone()[0]
        with self.catalogue.db() as con:
            counts = {row["state"]: row["n"] for row in con.execute(
                "SELECT state,COUNT(*) AS n FROM approval_prefetch_jobs GROUP BY state"
            )}
            kinds = {row["kind"]: row["n"] for row in con.execute(
                "SELECT kind,COUNT(*) AS n FROM approval_prefetch_cache GROUP BY kind"
            )}
            cache_stats = con.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(length(CAST(payload AS BLOB))),0) AS bytes,
                          SUM(CASE WHEN expires_at>? THEN 1 ELSE 0 END) AS fresh
                   FROM approval_prefetch_cache""", (now,)
            ).fetchone()
            completed = con.execute(
                """SELECT COUNT(*) FROM approval_prefetch_cache AS c
                   WHERE NOT EXISTS (
                     SELECT 1 FROM approval_prefetch_jobs AS j
                     WHERE j.kind=c.kind AND j.cache_key=c.cache_key)"""
            ).fetchone()[0]
            delayed = con.execute(
                "SELECT COUNT(*) FROM approval_prefetch_jobs WHERE state='PENDING' AND retry_at>?",
                (now,)
            ).fetchone()[0]
            queued = counts.get("PENDING", 0) + counts.get("RUNNING", 0)
            due = queued - delayed - counts.get("RUNNING", 0)
            by_kind = {row["kind"]: row["n"] for row in con.execute(
                "SELECT kind,COUNT(*) AS n FROM approval_prefetch_jobs GROUP BY kind"
            )}
            failed = counts.get("FAILED", 0)
            tail_queued = con.execute(
                "SELECT COUNT(*) FROM approval_prefetch_jobs "
                "WHERE state IN ('PENDING','RUNNING') AND attempts>=?",
                (NORMAL_FAILURE_BUDGET,),
            ).fetchone()[0]
            total = completed + queued + failed
            page_count = con.execute("PRAGMA page_count").fetchone()[0]
            page_size = con.execute("PRAGMA page_size").fetchone()[0]
        return {
            "waiting_tracks": waiting_tracks,
            "cached": cache_stats["fresh"] or 0,
            "cache_entries": cache_stats["n"], "cache_by_kind": kinds,
            "cache_bytes": cache_stats["bytes"],
            "catalogue_db_bytes": page_count * page_size,
            "completed": completed, "total": total,
            "queued": queued, "queued_by_kind": by_kind,
            "failed": failed, "tail_queued": tail_queued,
            "due": max(0, due), "deferred": delayed,
            "running": counts.get("RUNNING", 0),
            "cooldown_seconds": max(0, int(self.pause_until - now + 0.999)),
            "background": self.workers_enabled,
            "note": "Total counts distinct cached/queued/disabled tasks; disabled requests require manual retry.",
        }
