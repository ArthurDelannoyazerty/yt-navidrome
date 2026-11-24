"""User-scoped album-family workspaces based on MusicBrainz release groups.

A local recording can appear in several album families (including compilations)
without merging those families or connecting unrelated artists. Only exact
recording MBIDs are eligible for an album assignment.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import defaultdict, OrderedDict

from common import utcnow
from localized_names import preferred_english_name
from album_workspace import SCHEMA as REVIEW_SCHEMA

LOCK_SCHEMA = """
CREATE TABLE IF NOT EXISTS album_track_locks (
 user_id TEXT NOT NULL REFERENCES users(name),
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 locked_at TEXT NOT NULL,
 PRIMARY KEY(user_id,track_id)
);
"""
MAX_CLUSTER_EDITIONS = 220


def _chunks(items, size=350):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _canonical_name(value):
    data = value or {}
    return str(data.get("title") or data.get("name") or "")


def _title_key(value):
    """Punctuation/Unicode-insensitive title key for read-only suggestions."""
    plain = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(char for char in plain if char.isalnum() and not unicodedata.combining(char))


class AlbumClusters:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.catalogue = manager.catalogue
        # Read-only snapshots are library-scoped, short-lived, and never used by
        # the plan/apply validation path. Avoid reloading thousands of cached
        # MusicBrainz JSON blobs for each family/edition click.
        self._snapshots = OrderedDict()
        self._snapshot_lock = threading.Lock()
        self._last_prefetch = 0.0

    def _snapshot(self, user_id, include_assigned, *, use_cache=False, force_refresh=False,
                  timing=None):
        key = (user_id, bool(include_assigned))
        if use_cache and not force_refresh:
            with self._snapshot_lock:
                saved = self._snapshots.get(key)
                if saved and time.monotonic() - saved[0] < 8.0:
                    self._snapshots.move_to_end(key)
                    if timing is not None:
                        timing["snapshot_cache_hit"] = True
                        timing["local_db"] = 0.0
                        timing["catalogue_db"] = 0.0
                    return [dict(row) for row in saved[1]], saved[2]
        started = time.perf_counter()
        tracks = self._local_tracks(user_id, include_assigned)
        local_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        catalogue_data = self._catalogue_data(tracks) if tracks else None
        catalogue_ms = (time.perf_counter() - started) * 1000
        if timing is not None:
            timing["snapshot_cache_hit"] = False
            timing["local_db"] = local_ms
            timing["catalogue_db"] = catalogue_ms
        if use_cache:
            with self._snapshot_lock:
                self._snapshots[key] = (time.monotonic(), [dict(row) for row in tracks], catalogue_data)
                self._snapshots.move_to_end(key)
                while len(self._snapshots) > 4:
                    self._snapshots.popitem(last=False)
        return tracks, catalogue_data

    def init(self):
        with self.store.db() as con:
            con.executescript(REVIEW_SCHEMA + LOCK_SCHEMA)

    def locked(self, user_id, track_id):
        self.store.require_user(user_id)
        self.store.owned("tracks", track_id, user_id)
        return bool(self.store.one(
            "SELECT 1 FROM album_track_locks WHERE user_id=? AND track_id=?",
            (user_id, track_id),
        ))

    def toggle_lock(self, user_id, track_id, locked):
        self.store.require_user(user_id)
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            track = con.execute(
                "SELECT id,release_id,operation_state FROM tracks WHERE user_id=? AND id=?",
                (user_id, track_id),
            ).fetchone()
            if not track:
                raise LookupError("Track not found for this library")
            if track["operation_state"] in {"QUEUED", "RUNNING", "DEFERRED"} or con.execute(
                """SELECT 1 FROM jobs j LEFT JOIN album_changes ac
                     ON j.kind='album' AND ac.id=j.target
                   WHERE j.state IN ('PENDING','RUNNING') AND
                     (j.kind='track' AND j.target=? OR
                      j.kind='album' AND ac.track_id=?) LIMIT 1""",
                (track_id, track_id),
            ).fetchone():
                raise ValueError("Finish the active music or album operation before changing its lock")
            if locked:
                con.execute(
                    """INSERT INTO album_track_locks VALUES (?,?,?)
                       ON CONFLICT(user_id,track_id)
                       DO UPDATE SET locked_at=excluded.locked_at""",
                    (user_id, track_id, utcnow()),
                )
            else:
                con.execute(
                    "DELETE FROM album_track_locks WHERE user_id=? AND track_id=?",
                    (user_id, track_id),
                )
        return {"track_id": track_id, "locked": bool(locked)}

    def _local_tracks(self, user_id, include_assigned):
        self.store.require_user(user_id)
        where = ""  # All available local copies remain visible in every review scope.
        with self.store.db() as con:
            rows = con.execute(
                """SELECT t.id,t.mbid,t.title,t.matched_title,t.release_id,
                          t.health,t.operation_state,t.updated_at,
                          CASE WHEN l.track_id IS NULL THEN 0 ELSE 1 END AS album_locked,
                          d.recording_id AS reviewed_recording_id
                   FROM tracks t
                   LEFT JOIN album_track_locks l ON l.track_id=t.id AND l.user_id=t.user_id
                   LEFT JOIN album_review_decisions d ON d.track_id=t.id AND d.user_id=t.user_id
                   WHERE t.user_id=?
                     AND t.current_asset_id IS NOT NULL
                     AND t.health='AVAILABLE' """ + where +
                " ORDER BY t.id",
                (user_id,),
            ).fetchall()
        # Visibility is independent of eligibility to edit. Otherwise an album
        # appears incomplete whenever its already-assigned copies are hidden.
        return [{**dict(row), "in_scope": include_assigned or row["release_id"] is None}
                for row in rows]

    def _catalogue_data(self, tracks):
        rec_ids = sorted({row["mbid"] for row in tracks if row["mbid"]})
        release_rec = defaultdict(set)
        verified_rec = defaultdict(set)
        current_releases = {row["release_id"] for row in tracks if row["release_id"]}
        release_data, recording_data, groups = {}, {}, {}
        group_data, group_releases = {}, defaultdict(set)
        fetched, track_counts = set(), {}
        with self.catalogue.db() as con:
            for chunk in _chunks(rec_ids):
                qs = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"""SELECT release_id,recording_id FROM release_tracks
                        WHERE recording_id IN ({qs})""", chunk,
                ):
                    release_rec[row["release_id"]].add(row["recording_id"])
                    verified_rec[row["release_id"]].add(row["recording_id"])
                for row in con.execute(
                    f"""SELECT source_id,target_id FROM links WHERE
                        source_kind='recording' AND target_kind='release'
                        AND relation='appears-on' AND source_id IN ({qs})""", chunk,
                ):
                    release_rec[row["target_id"]].add(row["source_id"])
                for row in con.execute(
                    f"""SELECT id,data FROM entities WHERE kind='recording'
                        AND id IN ({qs})""", chunk,
                ):
                    recording_data[row["id"]] = json.loads(row["data"])

            def load_releases(ids):
                for chunk in _chunks(sorted(ids)):
                    qs = ",".join("?" for _ in chunk)
                    for row in con.execute(
                        f"SELECT id,data FROM entities WHERE kind='release' AND id IN ({qs})", chunk
                    ):
                        release_data[row["id"]] = json.loads(row["data"])
                    for row in con.execute(
                        f"""SELECT source_id,target_id FROM links
                            WHERE source_kind='release' AND target_kind='release-group'
                            AND relation='edition-of' AND source_id IN ({qs})""", chunk,
                    ):
                        groups[row["source_id"]] = row["target_id"]
                    for row in con.execute(
                        f"SELECT release_id FROM fetched_releases WHERE release_id IN ({qs})", chunk
                    ):
                        fetched.add(row["release_id"])
                    for row in con.execute(
                        f"""SELECT release_id,COUNT(*) AS n FROM release_tracks
                            WHERE release_id IN ({qs}) GROUP BY release_id""", chunk
                    ):
                        track_counts[row["release_id"]] = row["n"]
                for rid in ids:
                    group = (release_data.get(rid) or {}).get("release-group") or {}
                    if isinstance(group, dict) and group.get("id"):
                        groups.setdefault(rid, group["id"])

            direct_releases = set(release_rec) | current_releases
            load_releases(direct_releases)
            family_ids = set(groups.values())
            # Include sibling editions already discovered through the family
            # catalogue. A background release-group browse discovers more.
            for chunk in _chunks(sorted(family_ids)):
                qs = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"""SELECT id,data FROM entities WHERE kind='release-group'
                        AND id IN ({qs})""", chunk,
                ):
                    group_data[row["id"]] = json.loads(row["data"])
                for row in con.execute(
                    f"""SELECT source_id,target_id FROM links WHERE
                        source_kind='release' AND target_kind='release-group'
                        AND relation='edition-of' AND target_id IN ({qs})""", chunk,
                ):
                    groups[row["source_id"]] = row["target_id"]
                    group_releases[row["target_id"]].add(row["source_id"])
            sibling_ids = {rid for ids in group_releases.values() for rid in ids}
            load_releases(sibling_ids - direct_releases)
            for rid in direct_releases | sibling_ids:
                if groups.get(rid):
                    group_releases[groups[rid]].add(rid)
        return (release_rec, verified_rec, release_data, recording_data,
                groups, fetched, track_counts, group_data, group_releases)

    @staticmethod
    def _album(rid, ids, data, group, complete, track_count, track_map):
        original = _canonical_name(data) or "Unknown album"
        english = preferred_english_name(data) if data else original
        matched = sorted({track_id for rec in ids for track_id in track_map.get(rec, ())})
        return {
            "id": rid, "name": english or original,
            "original_title": original, "english_title": english or original,
            "release_group_id": group,
            "date": data.get("date"), "country": data.get("country"),
            "status": data.get("status"),
            "complete": complete, "track_count": track_count,
            "matched_track_ids": matched if complete else [],
            "linked_track_count": len(matched), "matched_count": len(matched) if complete else 0,
        }

    def list(self, user_id, *, include_assigned=False, query="", focus_id=None,
             background_tasks=None, use_cache=False, force_refresh=False, timing=None):
        started = time.perf_counter()
        tracks, metadata = self._snapshot(
            user_id, include_assigned, use_cache=use_cache,
            force_refresh=force_refresh, timing=timing,
        )
        if not tracks:
            if timing is not None:
                timing["grouping"] = 0.0
                timing["prefetch"] = 0.0
            return {"clusters": [], "total_clusters": 0, "total_tracks": 0, "detail": None}
        (release_rec, verified_rec, release_data, recording_data, groups, fetched,
         track_counts, group_data, group_releases) = metadata
        by_recording = defaultdict(set)
        by_id = {row["id"]: row for row in tracks}
        for row in tracks:
            if row["mbid"]:
                by_recording[row["mbid"]].add(row["id"])

        # A family is a release-group, NOT a connected component of recording
        # edges. Recordings that appear on compilations may occur in multiple
        # families without merging unrelated albums. Artist credits never union.
        family_members = defaultdict(set)
        family_editions = defaultdict(set)
        for rid in set(release_rec) | {r["release_id"] for r in tracks if r["release_id"]}:
            family = groups.get(rid) or rid
            family_editions[family].add(rid)
            for rec in release_rec.get(rid, ()):
                family_members[family].update(by_recording.get(rec, ()))
        for group, editions in group_releases.items():
            if group in family_members:
                family_editions[group].update(editions)
        for row in tracks:
            rid = row["release_id"]
            if rid:
                family = groups.get(rid) or rid
                family_members[family].add(row["id"])
                family_editions[family].add(rid)
        included = {tid for tids in family_members.values() for tid in tids}
        for row in tracks:
            if row["id"] not in included:
                # Unknown MusicBrainz relationships must not silently disappear.
                family_members[row["id"]].add(row["id"])

        for row in tracks:
            data = recording_data.get(row["mbid"]) or {}
            original = _canonical_name(data) or row["matched_title"] or row["title"]
            english = preferred_english_name(data) if data else row["matched_title"] or row["title"]
            row["original_title"] = original
            row["english_title"] = english or original
            row["display_title"] = row["matched_title"] or english or original
            row["current_release_id"] = row.pop("release_id")
            row["locked"] = bool(row.pop("album_locked"))
            row["unidentified"] = not bool(row["mbid"])
            reviewed = row.pop("reviewed_recording_id")
            row["not_found"] = bool(row["mbid"]) and reviewed == row["mbid"] and row["current_release_id"] is None

        summaries, details = [], {}
        for family, member_ids in family_members.items():
            members = [by_id[tid] for tid in member_ids]
            rec_map = defaultdict(set)
            for row in members:
                if row["mbid"]:
                    rec_map[row["mbid"]].add(row["id"])
            editions = []
            for rid in family_editions.get(family, ()):
                rec_ids = release_rec.get(rid, set())
                album = self._album(
                    rid, verified_rec.get(rid, set()) if rid in fetched else rec_ids,
                    release_data.get(rid) or {}, groups.get(rid),
                    rid in fetched, track_counts.get(rid, 0), rec_map,
                )
                album["linked_track_count"] = len({
                    tid for rec in rec_ids for tid in rec_map.get(rec, ())
                })
                editions.append(album)
            editions.sort(key=lambda a: (
                -a["matched_count"], -a["linked_track_count"],
                0 if a["status"] == "Official" else 1,
                -a["track_count"], a["name"].casefold(), a["id"],
            ))
            best = next((album for album in editions if album["matched_count"]), None)
            group_name = _canonical_name(group_data.get(family))
            title = (preferred_english_name(group_data[family]) or group_name
                     if family in group_data else None)
            title = title or (best["name"] if best else (
                members[0]["display_title"] if len(members) == 1 else
                members[0]["display_title"] + " + " + str(len(members) - 1)))
            pending = sum(not row["not_found"] and not row["locked"] and row["in_scope"]
                          for row in members)
            summary = {
                "id": family, "title": title,
                "original_title": group_name or (best["original_title"] if best else title),
                "english_title": title, "track_count": len(members),
                "unassigned": sum(row["current_release_id"] is None for row in members),
                "locked": sum(row["locked"] for row in members),
                "reviewed_not_found": sum(row["not_found"] for row in members),
                "candidate_count": len(editions), "best_match": best["matched_count"] if best else 0,
                "pending": pending, "release_group_id": family if family in group_releases else None,
            }
            summaries.append(summary)
            details[family] = (members, editions, member_ids)
        summaries.sort(key=lambda row: (
            -row["pending"], -row["track_count"], row["title"].casefold(), row["id"],
        ))
        query = str(query or "").strip().casefold()[:100]
        if query:
            visible = []
            for summary in summaries:
                members, editions, _ = details[summary["id"]]
                haystack = [summary["title"], summary["english_title"], summary["original_title"]]
                haystack.extend(album["name"] for album in editions)
                for track in members:
                    haystack.extend([track["display_title"], track["original_title"],
                                     track["english_title"], track["mbid"]])
                if any(query in str(value).casefold() for value in haystack):
                    visible.append(summary)
            summaries = visible
        if not any(row["id"] == focus_id for row in summaries):
            # Accept the former track-ID anchor for bookmarked API calls. A song
            # can be in multiple families; prefer its current assignment family.
            preferred = None
            if focus_id in by_id:
                assigned = by_id[focus_id].get("current_release_id")
                preferred = groups.get(assigned) or assigned
                if not any(row["id"] == preferred for row in summaries):
                    preferred = next((row["id"] for row in summaries
                                      if focus_id in details[row["id"]][2]), None)
            focus_id = preferred or (summaries[0]["id"] if summaries else None)
        detail = None
        if focus_id is not None:
            members, editions, member_ids = details[focus_id]
            members = sorted(members, key=lambda row: (
                row["not_found"], row["locked"], row["display_title"].casefold(), row["id"],
            ))
            detail = {
                "id": focus_id, "tracks": members,
                "releases": editions[:MAX_CLUSTER_EDITIONS],
                "total_releases": len(editions),
                "releases_truncated": len(editions) > MAX_CLUSTER_EDITIONS,
                "release_group_id": focus_id if focus_id in group_releases else None,
            }
        if timing is not None:
            timing["grouping"] = (time.perf_counter() - started) * 1000
        prefetch_started = time.perf_counter()
        all_releases = {rid for ids in family_editions.values() for rid in ids}
        focused_releases = family_editions.get(focus_id, set())
        families = set(group_releases) & set(family_members)
        if background_tasks is None:
            # Preserve synchronous semantics for internal callers and tests.
            if all_releases:
                self.catalogue.prefetch_album_tracklists(all_releases, focused_ids=focused_releases)
            if families:
                self.catalogue.prefetch_album_families(families, focused_ids=[focus_id])
        else:
            # The full 3,000+ family enqueue transaction used to be performed
            # *inside every GET*, delaying the first byte and each family click.
            # Queue only the focused family first; refresh the whole catalogue
            # at most once per minute, after the response has been sent.
            if focused_releases:
                background_tasks.add_task(
                    self.catalogue.prefetch_album_tracklists,
                    focused_releases, focused_ids=focused_releases)
            if focus_id in families:
                background_tasks.add_task(
                    self.catalogue.prefetch_album_families,
                    [focus_id], focused_ids=[focus_id])
            with self._snapshot_lock:
                full_scan_due = time.monotonic() - self._last_prefetch >= 60.0
                if full_scan_due:
                    self._last_prefetch = time.monotonic()
            if full_scan_due:
                if all_releases:
                    background_tasks.add_task(
                        self.catalogue.prefetch_album_tracklists,
                        all_releases, focused_ids=focused_releases)
                if families:
                    background_tasks.add_task(
                        self.catalogue.prefetch_album_families,
                        families, focused_ids=[focus_id])
        if timing is not None:
            timing["prefetch"] = (time.perf_counter() - prefetch_started) * 1000
        if detail is not None:
            focus = family_editions.get(focus_id, set())
            discovering = False
            if focus_id in families:
                with self.catalogue.db() as con:
                    status = con.execute(
                        """SELECT state FROM scopes WHERE kind='release-group'
                           AND entity_id=? AND section='release'""", (focus_id,)
                    ).fetchone()
                    discovering = status is None or status["state"] != "COMPLETE"
            detail["cache_progress"] = {
                "complete": len(focus & fetched), "total": len(focus),
                "discovering_editions": discovering,
            }
        return {"clusters": summaries, "total_clusters": len(summaries),
                "total_tracks": len(tracks), "detail": detail}

    def contradictions(self, user_id, *, limit=50, offset=0):
        """Only flag verified contradictions with a *complete* release tracklist.

        No MusicBrainz data from another library is joined to private track data.
        An incomplete release can never produce a false contradiction.
        """
        self.store.require_user(user_id)
        limit, offset = min(max(int(limit),1),100), max(0,int(offset))
        with self.store.db() as con:
            owned = [dict(row) for row in con.execute(
                """SELECT t.id,t.title,t.matched_title,t.mbid,t.release_id,
                       CASE WHEN l.track_id IS NULL THEN 0 ELSE 1 END AS locked
                   FROM tracks t LEFT JOIN album_track_locks l
                     ON l.user_id=t.user_id AND l.track_id=t.id
                   WHERE t.user_id=? AND t.release_id IS NOT NULL
                     AND t.mbid IS NOT NULL AND t.mbid<>'' AND t.current_asset_id IS NOT NULL
                   ORDER BY t.id""", (user_id,),
            )]
        conflicts = {}
        if owned:
            with self.catalogue.db() as con:
                for chunk in _chunks(owned,200):
                    placeholders=",".join("(?,?,?)" for _ in chunk)
                    args=[part for row in chunk for part in
                          (row["id"],row["release_id"],row["mbid"])]
                    for result in con.execute(
                        """WITH candidate(track_id,release_id,recording_id) AS
                           (VALUES """+placeholders+""")
                           SELECT c.track_id,e.name AS album_name
                           FROM candidate c
                           JOIN fetched_releases f ON f.release_id=c.release_id
                           LEFT JOIN release_tracks rt
                             ON rt.release_id=c.release_id
                            AND rt.recording_id=c.recording_id
                           LEFT JOIN entities e
                             ON e.kind='release' AND e.id=c.release_id
                           WHERE rt.recording_id IS NULL""",args,
                    ):
                        conflicts[result["track_id"]]=result["album_name"]
        items=[]
        for row in owned:
            if row["id"] not in conflicts:
                continue
            items.append({
                "track_id":row["id"],
                "title":row["matched_title"] or row["title"],
                "release_id":row["release_id"],
                "album":conflicts[row["id"]] or "Unknown release",
                "locked":bool(row["locked"]),
            })
        return {"total":len(items),"offset":offset,"limit":limit,
                "items":items[offset:offset+limit]}

    def edition(self, user_id, anchor_id, release_id, *, include_assigned=False,
                background_tasks=None):
        # Accept family-ID anchors and legacy track-ID anchors, but only within
        # the requesting user's own album family.
        result = self.list(
            user_id, include_assigned=include_assigned, focus_id=anchor_id,
            use_cache=background_tasks is not None, background_tasks=background_tasks,
        )
        detail = result["detail"]
        if not detail or (detail["id"] != anchor_id and
                          not any(t["id"] == anchor_id for t in detail["tracks"])):
            raise LookupError("This album family does not belong to the selected library scope")
        try:
            rid = str(uuid.UUID(release_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid release ID") from None
        track_map = defaultdict(set)
        for track in detail["tracks"]:
            if track["mbid"]:
                track_map[track["mbid"]].add(track["id"])
        with self.catalogue.db() as con:
            entity = con.execute(
                "SELECT data FROM entities WHERE kind='release' AND id=?", (rid,)
            ).fetchone()
            fetched = bool(con.execute(
                "SELECT 1 FROM fetched_releases WHERE release_id=?", (rid,)
            ).fetchone())
            release_rows = con.execute(
                """SELECT r.medium,r.position,r.recording_id,r.title,r.duration_ms,
                          e.data AS recording_data
                   FROM release_tracks r LEFT JOIN entities e
                     ON e.kind='recording' AND e.id=r.recording_id
                   WHERE r.release_id=? ORDER BY r.medium,r.position""",
                (rid,),
            ).fetchall() if fetched else []
            rec_ids = {r["recording_id"] for r in release_rows}
            # Position/duration evidence from sibling editions is only used
            # for read-only suggestions, never exact membership or validation.
            known = {t["current_release_id"] for t in detail["tracks"] if t["current_release_id"]}
            known.update(a["id"] for a in detail["releases"])
            positions = defaultdict(list)
            for chunk in _chunks(sorted(known)):
                qs = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"""SELECT recording_id,medium,position,duration_ms FROM release_tracks
                        WHERE release_id IN ({qs})""", chunk
                ):
                    positions[row["recording_id"]].append(row)
        if not fetched:
            self.catalogue.prefetch_album_tracklists([rid], focused_ids=[rid])
        if not entity:
            raise ValueError("Release is not cached yet; its tracklist is queued automatically.")
        data = json.loads(entity["data"])
        edition = self._album(rid, rec_ids, data, (data.get("release-group") or {}).get("id"),
                              fetched, len(release_rows), track_map)
        all_local = defaultdict(list)
        if rec_ids:
            with self.store.db() as user_db:
                for chunk in _chunks(sorted(rec_ids)):
                    marks = ",".join("?" for _ in chunk)
                    for copy in user_db.execute(
                        """SELECT t.id,t.mbid,t.title,t.matched_title,t.release_id,
                                  CASE WHEN l.track_id IS NULL THEN 0 ELSE 1 END AS locked
                           FROM tracks t LEFT JOIN album_track_locks l
                             ON l.user_id=t.user_id AND l.track_id=t.id
                           WHERE t.user_id=? AND t.current_asset_id IS NOT NULL
                             AND t.health='AVAILABLE' AND t.mbid IN (""" + marks + ")",
                        (user_id, *chunk),
                    ):
                        all_local[copy["mbid"]].append({
                            "id": copy["id"], "current_release_id": copy["release_id"],
                            "locked": bool(copy["locked"]),
                            "display_title": copy["matched_title"] or copy["title"],
                        })
        local_titles = {}
        for local in detail["tracks"]:
            local_titles[local["id"]] = {
                _title_key(name) for name in
                (local["display_title"], local["original_title"], local["english_title"])
                if _title_key(name)
            }
        official = []
        for row in release_rows:
            recording = json.loads(row["recording_data"]) if row["recording_data"] else {}
            original = row["title"] or _canonical_name(recording) or "Unknown track"
            english = preferred_english_name(recording) if recording else original
            official_names = {
                _title_key(name) for name in (original, english, _canonical_name(recording))
                if _title_key(name)
            }
            official_isrc = set(recording.get("isrcs") or [])
            official_duration = row["duration_ms"] or recording.get("length")
            possible = []
            # A same-title different-MBID recording is visible as a suggestion,
            # not a green/orange selection. Never infer ownership from public MB.
            if not all_local.get(row["recording_id"]):
                for local in detail["tracks"]:
                    if not local["mbid"] or local["mbid"] == row["recording_id"]:
                        continue
                    title_match = bool(official_names & local_titles[local["id"]])
                    metadata = positions.get(local["mbid"], ())
                    position_match = any(
                        p["medium"] == row["medium"] and p["position"] == row["position"]
                        for p in metadata
                    )
                    local_duration = next(
                        (p["duration_ms"] for p in metadata if p["duration_ms"]), None
                    )
                    # A different duration may indicate a remix/live version.
                    duration_ok = (not official_duration or not local_duration or
                                   abs(int(official_duration) - int(local_duration)) <= 10000)
                    if not title_match or not duration_ok:
                        continue
                    evidence = ["same normalized title"]
                    if position_match:
                        evidence.append("same disc/track position")
                    if official_duration and local_duration:
                        evidence.append("compatible duration")
                    possible.append({
                        "id": local["id"], "recording_id": local["mbid"],
                        "current_release_id": local["current_release_id"],
                        "display_title": local["display_title"],
                        "evidence": ", ".join(evidence),
                        "position_match": position_match,
                    })
                possible.sort(key=lambda a: (not a["position_match"], a["display_title"], a["id"]))
            official.append({
                "recording_id": row["recording_id"], "medium": row["medium"],
                "position": row["position"], "title": original,
                "original_title": original, "english_title": english or original,
                "local_track_ids": sorted(track_map.get(row["recording_id"], ())),
                "local_copies": all_local.get(row["recording_id"], []),
                "possible_local_matches": possible[:5],
            })
        edition["tracks"] = official
        edition["possible_match_count"] = len({
            c["id"] for t in official for c in t["possible_local_matches"]
        })
        edition["cluster_only"] = [
            local["id"] for local in detail["tracks"]
            if fetched and local["mbid"] not in rec_ids
        ]
        return edition
