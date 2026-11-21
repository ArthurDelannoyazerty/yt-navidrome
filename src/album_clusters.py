"""Clustered album organization from exact MusicBrainz release associations.

Clusters are connected components of the user's available recordings via
release editions and release groups. Shared public cache metadata never grants
access to another user's local tracks.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict

from common import utcnow
from localized_names import preferred_english_name

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


class AlbumClusters:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.catalogue = manager.catalogue

    def init(self):
        with self.store.db() as con:
            con.executescript(LOCK_SCHEMA)

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
        where = "" if include_assigned else "AND t.release_id IS NULL"
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
        return [dict(row) for row in rows]

    def _catalogue_data(self, tracks):
        rec_ids = sorted({row["mbid"] for row in tracks if row["mbid"]})
        release_rec = defaultdict(set)
        verified_rec = defaultdict(set)
        current_releases = {row["release_id"] for row in tracks if row["release_id"]}
        release_data = {}
        recording_data = {}
        groups = {}
        fetched = set()
        track_counts = {}
        with self.catalogue.db() as con:
            for chunk in _chunks(rec_ids):
                qs = ",".join("?" for _ in chunk)
                # Cached complete release tracks provide exact positive matches.
                for row in con.execute(
                    f"""SELECT release_id,recording_id FROM release_tracks
                        WHERE recording_id IN ({qs})""", chunk,
                ):
                    release_rec[row["release_id"]].add(row["recording_id"])
                    verified_rec[row["release_id"]].add(row["recording_id"])
                # Even before a full release tracklist is fetched, a recording's
                # explicit 'appears-on' edge can connect its release family.
                for row in con.execute(
                    f"""SELECT source_id,target_id FROM links WHERE
                        source_kind='recording' AND target_kind='release'
                        AND relation='appears-on' AND source_id IN ({qs})""",
                    chunk,
                ):
                    release_rec[row["target_id"]].add(row["source_id"])
                for row in con.execute(
                    f"""SELECT id,name,data FROM entities WHERE kind='recording'
                        AND id IN ({qs})""", chunk,
                ):
                    recording_data[row["id"]] = json.loads(row["data"])
            release_ids = sorted(set(release_rec) | current_releases)
            for chunk in _chunks(release_ids):
                qs = ",".join("?" for _ in chunk)
                for row in con.execute(
                    f"""SELECT id,name,data FROM entities WHERE kind='release'
                        AND id IN ({qs})""", chunk,
                ):
                    release_data[row["id"]] = json.loads(row["data"])
                for row in con.execute(
                    f"""SELECT source_id,target_id FROM links
                        WHERE source_kind='release' AND target_kind='release-group'
                        AND relation='edition-of' AND source_id IN ({qs})""", chunk,
                ):
                    groups[row["source_id"]] = row["target_id"]
                for row in con.execute(
                    f"SELECT release_id FROM fetched_releases WHERE release_id IN ({qs})",
                    chunk,
                ):
                    fetched.add(row["release_id"])
                for row in con.execute(
                    f"""SELECT release_id,COUNT(*) AS n FROM release_tracks
                        WHERE release_id IN ({qs}) GROUP BY release_id""",
                    chunk,
                ):
                    track_counts[row["release_id"]] = row["n"]
            for rid, data in release_data.items():
                group = data.get("release-group") or {}
                if isinstance(group, dict) and group.get("id"):
                    groups.setdefault(rid, group["id"])
        return release_rec, verified_rec, release_data, recording_data, groups, fetched, track_counts

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

    def list(self, user_id, *, include_assigned=False, query="", focus_id=None):
        tracks = self._local_tracks(user_id, include_assigned)
        if not tracks:
            return {"clusters": [], "total_clusters": 0, "total_tracks": 0, "detail": None}
        (release_rec, verified_rec, release_data, recording_data,
         groups, fetched, track_counts) = self._catalogue_data(tracks)
        by_recording = defaultdict(list)
        by_id = {row["id"]: row for row in tracks}
        for row in tracks:
            if row["mbid"]:
                by_recording[row["mbid"]].append(row["id"])
        parent = {row["id"]: row["id"] for row in tracks}

        def root(tid):
            while parent[tid] != tid:
                parent[tid] = parent[parent[tid]]
                tid = parent[tid]
            return tid

        def union(a, b):
            first, second = root(a), root(b)
            if first != second:
                parent[max(first, second)] = min(first, second)

        # One recording may belong to several editions; an edition may contain
        # several recordings, and related editions share a release group.
        group_first = {}
        for rid, record_ids in release_rec.items():
            local = [tid for rec in record_ids for tid in by_recording.get(rec, ())]
            if not local:
                continue
            first = local[0]
            for tid in local[1:]:
                union(first, tid)
            group = groups.get(rid)
            if group:
                if group in group_first:
                    union(first, group_first[group])
                else:
                    group_first[group] = first
        # Local album assignments are useful evidence if the public cache is
        # missing a recording-to-release relation.
        release_first = {}
        for row in tracks:
            rid = row["release_id"]
            if not rid:
                continue
            if rid in release_first:
                union(row["id"], release_first[rid])
            else:
                release_first[rid] = row["id"]
            gid = groups.get(rid)
            if gid:
                if gid in group_first:
                    union(row["id"], group_first[gid])
                else:
                    group_first[gid] = row["id"]

        component = defaultdict(list)
        for row in tracks:
            component[root(row["id"])].append(row)
        records = {}
        for anchor, members in component.items():
            records[anchor] = {row["mbid"] for row in members if row["mbid"]}
        release_by_component = defaultdict(dict)
        for rid, rec_ids in release_rec.items():
            roots = {root(tid) for rec in rec_ids for tid in by_recording.get(rec, ())}
            for anchor in roots:
                release_by_component[anchor][rid] = rec_ids & records[anchor]
        for row in tracks:
            rid = row["release_id"]
            if rid:
                release_by_component[root(row["id"])].setdefault(rid, set())

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
        summaries = []
        details = {}
        for anchor, members in component.items():
            member_ids = {row["id"] for row in members}
            rec_map = defaultdict(set)
            for row in members:
                if row["mbid"]:
                    rec_map[row["mbid"]].add(row["id"])
            editions = []
            for rid, rec_ids in release_by_component.get(anchor, {}).items():
                album = self._album(
                    rid, verified_rec.get(rid, set()) & records[anchor] if rid in fetched else rec_ids,
                    release_data.get(rid) or {},
                    groups.get(rid), rid in fetched, track_counts.get(rid, 0),
                    rec_map,
                )
                album["linked_track_count"] = len({
                    tid for rec in rec_ids for tid in rec_map.get(rec, ())
                })
                editions.append(album)
            editions.sort(key=lambda a:(
                -a["matched_count"], -a["linked_track_count"],
                0 if a["status"] == "Official" else 1,
                -a["track_count"], a["name"].casefold(), a["id"],
            ))
            best = next((album for album in editions if album["matched_count"]), None)
            title = best["name"] if best else (members[0]["display_title"] if len(members) == 1 else
                members[0]["display_title"] + " + " + str(len(members)-1))
            unassigned = sum(row["current_release_id"] is None for row in members)
            pending = sum(not row["not_found"] and not row["locked"] for row in members)
            summary = {
                "id": anchor, "title": title,
                "original_title": best["original_title"] if best else title,
                "english_title": best["english_title"] if best else title,
                "track_count": len(members), "unassigned": unassigned,
                "locked": sum(row["locked"] for row in members),
                "reviewed_not_found": sum(row["not_found"] for row in members),
                "candidate_count": len(editions),
                "best_match": best["matched_count"] if best else 0,
                "pending": pending,
            }
            summaries.append(summary)
            details[anchor] = (members, editions, member_ids)
        summaries.sort(key=lambda row:(-row["pending"],-row["track_count"],row["title"].casefold(),row["id"]))
        query = str(query or "").strip().casefold()[:100]
        if query:
            visible = []
            for summary in summaries:
                members, editions, _ = details[summary["id"]]
                haystack = [summary["title"], summary["english_title"], summary["original_title"]]
                for track in members:
                    haystack.extend([track["display_title"],track["original_title"],
                                     track["english_title"],track["mbid"]])
                if any(query in str(value).casefold() for value in haystack):
                    visible.append(summary)
            summaries = visible
        if not any(row["id"] == focus_id for row in summaries):
            focus_id = summaries[0]["id"] if summaries else None
        detail = None
        if focus_id:
            members, editions, member_ids = details[focus_id]
            members = sorted(members, key=lambda row:(
                row["not_found"], row["locked"],row["display_title"].casefold(),row["id"],
            ))
            detail = {
                "id": focus_id, "tracks": members,
                "releases": editions[:MAX_CLUSTER_EDITIONS],
                "total_releases": len(editions),
                "releases_truncated": len(editions)>MAX_CLUSTER_EDITIONS,
            }
        return {"clusters": summaries, "total_clusters": len(summaries),
                "total_tracks": len(tracks), "detail": detail}

    def edition(self, user_id, anchor_id, release_id, *, include_assigned=False):
        # User ownership and cluster identity are validated by generating the
        # user's scoped components. A foreign anchor reveals no metadata.
        result = self.list(user_id, include_assigned=include_assigned, focus_id=anchor_id)
        if not result["detail"] or result["detail"]["id"] != anchor_id:
            raise LookupError("This cluster does not belong to the selected library scope")
        try:
            rid = str(uuid.UUID(release_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid release ID") from None
        track_map = defaultdict(set)
        for row in result["detail"]["tracks"]:
            track_map[row["mbid"]].add(row["id"])
        with self.catalogue.db() as con:
            entity = con.execute(
                "SELECT data FROM entities WHERE kind='release' AND id=?", (rid,)
            ).fetchone()
            fetched = bool(con.execute(
                "SELECT 1 FROM fetched_releases WHERE release_id=?", (rid,)
            ).fetchone())
            rec_ids = {r["recording_id"] for r in con.execute(
                "SELECT DISTINCT recording_id FROM release_tracks WHERE release_id=?", (rid,)
            )} if fetched else set()
            count = con.execute("SELECT COUNT(*) FROM release_tracks WHERE release_id=?", (rid,)).fetchone()[0]
        if not entity:
            raise ValueError("Release is not cached. Search and synchronize the edition first.")
        data = json.loads(entity["data"])
        return self._album(rid, rec_ids, data, (data.get("release-group") or {}).get("id"),
                           fetched, count, track_map)
