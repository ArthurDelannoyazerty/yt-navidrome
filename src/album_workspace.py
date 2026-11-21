"""User-scoped multi-release album workspace and reversible assignment plans.

A release-to-recording relationship is evidence for compatibility, not proof of
which edition a person intended to own. No file is changed until plan apply.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from common import LIBRARY, sha256_file, utcnow

PLAN_MARKER = "album-assignment-plan-v1"
MAX_PLAN_TRACKS = 500
MAX_COMPARE_RELEASES = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS album_review_decisions (
 user_id TEXT NOT NULL REFERENCES users(name),
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 recording_id TEXT NOT NULL,
 reviewed_at TEXT NOT NULL,
 PRIMARY KEY(user_id,track_id)
);
"""


class AlbumWorkspace:
    def __init__(self, manager):
        self.manager = manager
        self.store = manager.store
        self.catalogue = manager.catalogue

    def init(self):
        with self.store.db() as con:
            con.executescript(SCHEMA)

    def tracks(self, user_id, *, source_id=None, include_assigned=False,
               query="", offset=0, limit=100):
        """One paginated local-library scope; never infer IDs from song titles."""
        self.store.require_user(user_id)
        offset = max(0, int(offset))
        limit = max(1, min(250, int(limit)))
        if source_id:
            self.store.owned("sources", source_id, user_id)
        where = ["t.user_id=?", "t.mbid IS NOT NULL", "t.mbid<>''",
                 "t.current_asset_id IS NOT NULL"]
        args = [user_id]
        if not include_assigned:
            where.append("t.release_id IS NULL")
        if source_id:
            membership = """EXISTS (SELECT 1 FROM track_origins o
               JOIN source_origins so ON so.origin_id=o.id
               WHERE so.source_id=? AND o.track_id=t.id AND o.user_id=t.user_id)"""
            if include_assigned:
                # Include previously assigned songs from *other* sources so a
                # larger or smaller release can be reviewed without blind moves.
                where.append("(t.release_id IS NOT NULL OR " + membership + ")")
            else:
                where.append(membership)
            args.append(source_id)
        query = str(query).strip()[:100]
        if query:
            escaped = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("""(COALESCE(t.matched_title,t.title) LIKE ? ESCAPE '\\' COLLATE NOCASE
                        OR t.mbid LIKE ? ESCAPE '\\')""")
            args.extend((escaped,escaped))
        condition = " AND ".join(where)
        with self.store.db() as con:
            total = con.execute("SELECT COUNT(*) FROM tracks t WHERE " + condition, args).fetchone()[0]
            rows = con.execute(
                """SELECT t.id,t.mbid,t.title,t.matched_title,t.release_id,
                          t.health,t.operation_state,t.updated_at,
                          d.recording_id AS not_found_recording
                   FROM tracks t LEFT JOIN album_review_decisions d
                     ON d.user_id=t.user_id AND d.track_id=t.id
                   WHERE """ + condition +
                """ ORDER BY CASE WHEN t.release_id IS NULL THEN 0 ELSE 1 END,
                         t.created_at DESC,t.id LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            ).fetchall()
        items = [self._item(row) for row in rows]
        self._fill_album_names(items)
        return {"total": total, "offset": offset, "limit": limit,
                "items": items, "has_more": offset + len(items) < total}

    @staticmethod
    def _item(row):
        values = dict(row)
        return {"id": values["id"], "recording_id": values["mbid"],
                "title": values["matched_title"] or values["title"],
                "current_release_id": values["release_id"],
                "not_found": not values["release_id"] and
                    values.get("not_found_recording") == values["mbid"],
                "health": values["health"], "operation_state": values["operation_state"]}

    def _fill_album_names(self, items):
        ids = sorted({item["current_release_id"] for item in items
                      if item.get("current_release_id")})
        if not ids:
            return
        names = {}
        with self.catalogue.db() as con:
            for start in range(0, len(ids), 400):
                part = ids[start:start+400]
                marks = ",".join("?" for _ in part)
                names.update({row["id"]: row["name"] for row in con.execute(
                    f"SELECT id,name FROM entities WHERE kind='release' AND id IN ({marks})",
                    part,
                )})
        for item in items:
            item["current_album"] = names.get(item.get("current_release_id"))

    def compare(self, user_id, release_ids, *, selected_track_ids=(),
                include_assigned=False):
        """Return actual release tracklists and exact-ID membership, not scores."""
        self.store.require_user(user_id)
        release_ids = list(dict.fromkeys(release_ids))
        if len(release_ids) > MAX_COMPARE_RELEASES:
            raise ValueError("Compare at most twelve releases in one request")
        if len(selected_track_ids) > MAX_PLAN_TRACKS:
            raise ValueError("Select fewer than 501 recordings")
        selected = self.manager._select(user_id, [], selected_track_ids) if selected_track_ids else []
        local_ids = {r["mbid"] for r in selected if r["mbid"]}
        output = []
        candidate_recordings = set()
        with self.catalogue.db() as con:
            for release_id in release_ids:
                try:
                    identity = str(uuid.UUID(release_id))
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("Invalid MusicBrainz release ID") from None
                entry = con.execute(
                    "SELECT name,data FROM entities WHERE kind='release' AND id=?",
                    (identity,),
                ).fetchone()
                fetched = con.execute("SELECT 1 FROM fetched_releases WHERE release_id=?",
                                      (identity,)).fetchone() is not None
                tracks = con.execute(
                    """SELECT recording_id,title,medium,position FROM release_tracks
                       WHERE release_id=? ORDER BY medium,position LIMIT 1000""",
                    (identity,),
                ).fetchall() if fetched else []
                # A large edition may have >1,000 entries. The compact visible
                # tracklist is capped, but exact membership must never be capped.
                all_ids = [row["recording_id"] for row in con.execute(
                    "SELECT DISTINCT recording_id FROM release_tracks WHERE release_id=?",
                    (identity,),
                )] if fetched else []
                all_count = con.execute("SELECT COUNT(*) FROM release_tracks WHERE release_id=?",
                                        (identity,)).fetchone()[0] if fetched else 0
                data = json.loads(entry["data"]) if entry else {}
                group = data.get("release-group") or {}
                ids = set(all_ids)
                candidate_recordings.update(ids)
                output.append({
                    "id": identity, "name": entry["name"] if entry else "Unknown release",
                    "group_id": group.get("id"),
                    "group_name": group.get("title") or entry["name"] if entry else "Unknown",
                    "date": data.get("date"), "country": data.get("country"),
                    "status": data.get("status"), "complete": fetched,
                    "track_count": all_count,
                    "tracks": [dict(r) for r in tracks],
                    "recording_ids": sorted(ids),
                    "matched_recording_ids": sorted(local_ids & ids),
                })
        extra = []
        if include_assigned and candidate_recordings:
            candidate_recordings = set(candidate_recordings)
            excluded = {row["id"] for row in selected}
            ids = sorted(candidate_recordings)
            with self.store.db() as con:
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    rows = con.execute(
                        f"""SELECT t.id,t.mbid,t.title,t.matched_title,t.release_id,
                                   t.health,t.operation_state,t.updated_at,
                                   d.recording_id AS not_found_recording
                            FROM tracks t LEFT JOIN album_review_decisions d
                              ON d.user_id=t.user_id AND d.track_id=t.id
                            WHERE t.user_id=? AND t.release_id IS NOT NULL
                            AND t.mbid IN ({marks}) LIMIT 501""",
                        (user_id,*chunk),
                    ).fetchall()
                    for row in rows:
                        if row["id"] not in excluded:
                            extra.append(self._item(row));excluded.add(row["id"])
                        if len(extra) >= 500: break
                    if len(extra) >= 500: break
        self._fill_album_names(extra)
        return {"releases": output, "additional_assigned_matches": extra,
                "note": "Only exact MusicBrainz recording IDs count as matches. "
                        "An incomplete tracklist is unknown, not a negative match."}

    def preview(self, user_id, decisions):
        self.store.require_user(user_id)
        self.manager.require_migration_complete()
        if not decisions or len(decisions) > MAX_PLAN_TRACKS:
            raise ValueError("Choose between one and 500 track decisions")
        seen = set()
        grouped = defaultdict(list)
        overrides = defaultdict(dict)
        targets = []
        not_found = []
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("Invalid album decision")
            tid = decision.get("track_id")
            if not tid or tid in seen:
                raise ValueError("Each local recording can have only one destination")
            seen.add(tid)
            action = decision.get("action")
            if action == "release":
                rid = decision.get("release_id")
                try:
                    rid = str(uuid.UUID(str(rid)))
                except (TypeError, ValueError, AttributeError):
                    raise ValueError("Invalid destination release") from None
                grouped[rid].append(tid)
                if decision.get("target_recording_id") is not None:
                    try:
                        target = str(uuid.UUID(str(decision["target_recording_id"])))
                    except (TypeError, ValueError, AttributeError):
                        raise ValueError("Invalid destination MusicBrainz recording ID") from None
                    overrides[rid][tid] = target
                    targets.append(target)
            elif action == "not_found":
                not_found.append(tid)
            else:
                raise ValueError("Unknown album decision")
        if len(targets) != len(set(targets)):
            raise ValueError("An official recording position can only receive one local track")
        result = []
        if grouped:
            with self.catalogue.db() as con:
                for rid in grouped:
                    if not con.execute(
                        "SELECT 1 FROM fetched_releases WHERE release_id=?", (rid,)
                    ).fetchone():
                        raise ValueError("Full release tracklist is not cached; sync the edition first")
        for rid, track_ids in grouped.items():
            # Reuse the existing album manager's compatibility and migration
            # checks instead of inventing a weaker file-safety path.
            preview = self.manager.preview(user_id, rid, track_ids=track_ids,
                                           persist=False, recording_overrides=overrides[rid])
            for item in preview["items"]:
                item = {**item, "action": "release", "target_release_id": rid,
                        "target_album": preview["album"]}
                result.append(item)
        if not_found:
            for track in self.manager._select(user_id, [], not_found):
                if not track["mbid"] or track["release_id"] is not None:
                    status = "INCOMPATIBLE"
                elif track["operation_state"] != "IDLE" or track["health"] != "AVAILABLE":
                    status = "BUSY"
                elif self.store.one(
                    "SELECT 1 FROM album_review_decisions WHERE user_id=? AND track_id=? AND recording_id=?",
                    (user_id,track["id"],track["mbid"]),
                ):
                    status = "UNCHANGED"
                else:
                    status = "MARK_NOT_FOUND"
                result.append({"track_id": track["id"], "recording_id": track["mbid"],
                               "old_release_id": track["release_id"],
                               "status": status, "revision": track["updated_at"],
                               "action": "not_found", "target_release_id": None,
                               "target_album": "No Album Found"})
        ids = [r["track_id"] for r in result]
        titles = {}
        if ids:
            with self.store.db() as con:
                for start in range(0,len(ids),400):
                    part = ids[start:start+400]
                    q = ",".join("?" for _ in part)
                    titles.update({r["id"]: r["matched_title"] or r["title"] for r in
                                   con.execute(f"SELECT id,title,matched_title FROM tracks WHERE id IN ({q})",part)})
        for row in result:
            row["title"] = titles.get(row["track_id"],row["track_id"])
        result.sort(key=lambda x:(x["title"].casefold(),x["track_id"]))
        # A release-to-release move can shrink the old album without moving
        # unrelated recordings. Show both album totals before confirmation.
        with self.store.db() as con:
            counts = {row["release_id"]: row["count"] for row in con.execute(
                """SELECT release_id,COUNT(*) AS count FROM tracks
                   WHERE user_id=? AND release_id IS NOT NULL GROUP BY release_id""",
                (user_id,),
            )}
        effects = {}
        for item in result:
            if item["action"]!="release" or item["status"]!="COMPATIBLE":
                continue
            old, new = item["old_release_id"], item["target_release_id"]
            if old:
                effects[old] = effects.get(old,counts.get(old,0))-1
            effects[new] = effects.get(new,counts.get(new,0))+1
        impact = [{"release_id": rid, "before": counts.get(rid,0), "after": after}
                  for rid,after in sorted(effects.items())]
        token = uuid.uuid4().hex
        with self.store.db() as con:
            con.execute(
                "INSERT INTO album_previews VALUES (?,?,?,?,?,?,NULL)",
                (token,user_id,PLAN_MARKER,json.dumps(result),utcnow(),time.time()+1200),
            )
        return {"preview_token": token, "items": result, "impact": impact,
                "can_apply": sum(row["status"]=="COMPATIBLE" for row in result),
                "can_mark": sum(row["status"]=="MARK_NOT_FOUND" for row in result),
                "note": "Each explicitly selected local track is assigned to its chosen "
                        "official edition position. A different recording MBID is updated "
                        "reversibly with the album tags; existing title/artist tags remain. "
                        "No Album Found is a reversible review marker, not an audio tag."}

    def apply(self, user_id, token):
        self.store.require_user(user_id)
        self.manager.require_migration_complete()
        self.manager._require_completed_migration()
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            preview = con.execute(
                "SELECT * FROM album_previews WHERE token=? AND user_id=? AND release_id=?",
                (token,user_id,PLAN_MARKER),
            ).fetchone()
            if not preview or preview["consumed_at"] or preview["expires_at"] < time.time():
                raise ValueError("Album plan expired or was already applied; review again")
            items = json.loads(preview["items"])
            changes = [r for r in items if r["action"]=="release" and r["status"]=="COMPATIBLE"]
            marks = [r for r in items if r["action"]=="not_found" and r["status"]=="MARK_NOT_FOUND"]
            if not changes and not marks:
                raise ValueError("Nothing eligible to apply")
            required_space = 0
            for item in changes:
                row = con.execute(
                    """SELECT t.id,t.updated_at,t.release_id,t.mbid,t.current_asset_id,t.operation_state,
                              t.health,a.sha256,a.path FROM tracks t
                       JOIN assets a ON a.id=t.current_asset_id AND a.state='CURRENT'
                       WHERE t.user_id=? AND t.id=?""",(user_id,item["track_id"]),
                ).fetchone()
                if con.execute(
                    "SELECT 1 FROM album_track_locks WHERE user_id=? AND track_id=?",
                    (user_id,item["track_id"]),
                ).fetchone():
                    raise ValueError("Album locked: unlock this recording before reassignment")
                if (not row or row["updated_at"]!=item["revision"] or
                    row["release_id"]!=item["old_release_id"] or
                    row["mbid"]!=item["recording_id"] or
                    row["current_asset_id"]!=item["asset_id"] or
                    row["sha256"]!=item["sha256"] or row["path"]!=item["path"] or
                    row["operation_state"]!="IDLE" or row["health"]!="AVAILABLE"):
                    raise ValueError("A track or its audio changed since preview; review again")
                if con.execute(
                    """SELECT 1 FROM jobs j LEFT JOIN album_changes c
                       ON j.kind='album' AND c.id=j.target
                       WHERE j.state IN ('PENDING','RUNNING')
                       AND (j.kind='track' AND j.target=? OR
                            j.kind='album' AND c.track_id=?) LIMIT 1""",
                    (item["track_id"],item["track_id"]),
                ).fetchone():
                    raise ValueError("An operation started since preview; review again")
                target = item.get("target_recording_id", item["recording_id"])
                if target != item["recording_id"] and con.execute(
                    "SELECT 1 FROM tracks WHERE user_id=? AND mbid=? AND id<>?",
                    (user_id,target,item["track_id"]),
                ).fetchone():
                    raise ValueError("Chosen official recording is already owned; review again")
                if not self.catalogue.release_assignment(item["target_release_id"], target):
                    raise ValueError("MusicBrainz edition position changed; review again")
                with self.catalogue.db() as cat_db:
                    if not cat_db.execute(
                        "SELECT 1 FROM fetched_releases WHERE release_id=?",
                        (item["target_release_id"],),
                    ).fetchone():
                        raise ValueError("Full release tracklist is no longer cached; review again")
                path = Path(row["path"])
                if not path.is_file() or sha256_file(path)!=item["sha256"]:
                    raise ValueError("The audio file changed since preview; review again")
                required_space += path.stat().st_size
            for item in marks:
                row=con.execute(
                    """SELECT id,mbid,release_id,updated_at,operation_state,health
                       FROM tracks WHERE user_id=? AND id=?""",
                    (user_id,item["track_id"]),
                ).fetchone()
                if (not row or row["mbid"]!=item["recording_id"] or
                    row["release_id"] is not None or row["updated_at"]!=item["revision"] or
                    row["operation_state"]!="IDLE" or row["health"]!="AVAILABLE"):
                    raise ValueError("A recording changed since review; review again")
                if con.execute(
                    """SELECT 1 FROM jobs j LEFT JOIN album_changes c
                       ON j.kind='album' AND c.id=j.target
                       WHERE j.state IN ('PENDING','RUNNING')
                       AND (j.kind='track' AND j.target=?
                            OR j.kind='album' AND c.track_id=?) LIMIT 1""",
                    (item["track_id"],item["track_id"]),
                ).fetchone():
                    raise ValueError("A recording has an active operation; review again")
            if changes:
                available = shutil.disk_usage(self.manager.backup_root.parent).free
                if available < required_space + max(256*1024*1024,required_space//10):
                    raise ValueError("Insufficient disk space for reversible album backups")
            group_id = uuid.uuid4().hex
            for item in changes:
                change_id = uuid.uuid4().hex
                con.execute(
                    """INSERT INTO album_changes
                       (id,group_id,user_id,track_id,release_id,old_release_id,
                        release_tags,asset_id,path,expected_sha,status,created_at,
                        old_recording_id,new_recording_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?,?)""",
                    (change_id,group_id,user_id,item["track_id"],
                     item["target_release_id"],item["old_release_id"],
                     json.dumps(item["release_tags"]),item["asset_id"],
                     item["path"],item["sha256"],utcnow(),item["recording_id"],
                     item.get("target_recording_id",item["recording_id"])),
                )
                if not self.store.enqueue("album",change_id,user_id,{"mode":"apply"},con=con):
                    raise ValueError("Could not queue an album change safely")
                con.execute("DELETE FROM album_review_decisions WHERE user_id=? AND track_id=?",
                            (user_id,item["track_id"]))
            for item in marks:
                con.execute(
                    """INSERT INTO album_review_decisions(user_id,track_id,recording_id,reviewed_at)
                       VALUES (?,?,?,?) ON CONFLICT(user_id,track_id)
                       DO UPDATE SET recording_id=excluded.recording_id,
                                     reviewed_at=excluded.reviewed_at""",
                    (user_id,item["track_id"],item["recording_id"],utcnow()),
                )
            con.execute("UPDATE album_previews SET consumed_at=? WHERE token=?",
                        (utcnow(),token))
        return {"group_id":group_id,"queued":len(changes),"marked_not_found":len(marks),
                "skipped":len(items)-len(changes)-len(marks)}

    def clear_not_found(self, user_id, track_id):
        self.store.require_user(user_id)
        self.store.owned("tracks",track_id,user_id)
        with self.store.db() as con:
            removed=con.execute(
                "DELETE FROM album_review_decisions WHERE user_id=? AND track_id=?",
                (user_id,track_id),
            ).rowcount
        return {"cleared":bool(removed)}
