"""Read-only legacy migration into the versioned provider-aware schema.

The supplied legacy database is opened read-only. Date-only history is retained as
provenance and is not converted into a fabricated midnight timestamp.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common import LIBRARY, STATE, sha256_file, user_name, utcnow
from pipeline import write_playlist
from providers import parse_source
from store import Store


def import_legacy(args):
    source = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    tracks = [dict(row) for row in source.execute("SELECT * FROM tracks")]
    memberships = [dict(row) for row in source.execute("SELECT * FROM playlist_memberships")]
    monitored = [dict(row) for row in source.execute("SELECT * FROM monitored_urls")]
    users = [row[0] for row in source.execute("SELECT username FROM users")]
    source.close()

    for user in {*(users), *(track["user_id"] for track in tracks)}:
        user_name(user)

    prepared = []
    report = {
        "tracks": len(tracks),
        "preserved_files": 0,
        "unavailable_audio": [],
        "unsupported_sources": [],
        "date_precision": "Legacy dates remain provenance only; no time of day is invented.",
    }
    identities = set()
    for old in tracks:
        try:
            ref = parse_source(old["source_url"])
            if ref.is_playlist:
                raise ValueError("Legacy track contains a playlist URL")
            provider, key, url = ref.provider, ref.key.split(":", 1)[1], ref.url
        except ValueError:
            provider, key, url = "legacy", old["track_uuid"], old["source_url"]
        identity = (old["user_id"], provider, key)
        if identity in identities:
            raise ValueError(
                f"Canonical duplicate found: {identity}. Resolve it in a copy of the old DB first."
            )
        identities.add(identity)
        path = None
        if old.get("file_path"):
            recorded = Path(old["file_path"])
            recorded = (
                recorded if recorded.is_absolute()
                else args.old_working_directory / recorded
            ).resolve()
            if recorded.is_relative_to(args.old_library_root.resolve()):
                candidate = (LIBRARY / recorded.relative_to(args.old_library_root.resolve())).resolve()
                if candidate.is_relative_to((LIBRARY / old["user_id"]).resolve()) and candidate.is_file():
                    path = candidate
        if path:
            report["preserved_files"] += 1
        else:
            report["unavailable_audio"].append({
                "id": old["track_uuid"],
                "url": url,
                "old_status": old["status"],
                "action": "Re-add or redownload a supported source after migration.",
            })
        prepared.append((old, provider, key, url, path))

    monitored_refs = []
    for old in monitored:
        try:
            monitored_refs.append((old, parse_source(old["url"])))
        except ValueError as exc:
            report["unsupported_sources"].append({"url": old["url"], "reason": str(exc)})

    print(json.dumps(report, indent=2))
    if not args.apply:
        print("DRY RUN. No database, audio, or tags changed.")
        return report

    store = Store()
    if store.path.exists():
        raise ValueError("Destination database already exists. Use a fresh state directory.")
    STATE.mkdir(parents=True, exist_ok=True)
    store.init()
    queued = []
    try:
        with store.db() as con:
            con.executemany(
                "INSERT OR IGNORE INTO users VALUES (?)",
                [(user,) for user in {*(users), *(track["user_id"] for track in tracks)}],
            )
            origin_by_track = {}
            for old, provider, key, url, path in prepared:
                now = utcnow()
                origin_id = str(uuid.uuid4())
                health = "AVAILABLE" if path else "MISSING"
                con.execute(
                    """INSERT INTO tracks(
                       id,user_id,title,first_seen_at,playlist_discovered_at,
                       discovered_at,discovery_basis,health,operation_state,
                       operation_error,file_path,matched_title,mbid,current_origin_id,
                       created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        old["track_uuid"], old["user_id"], old["title"] or "Unknown title",
                        None, None, None,
                        "legacy_date_only:" + (old.get("discovery_date") or "unknown"),
                        health, "IDLE", None if path else "Legacy audio unavailable",
                        str(path) if path else None, old.get("matched_title"), old.get("mbid"),
                        origin_id, now, now,
                    ),
                )
                con.execute(
                    """INSERT INTO track_origins(
                       id,track_id,user_id,provider,media_key,url,title,downloadable,
                       availability,first_seen_at,last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        origin_id, old["track_uuid"], old["user_id"], provider, key, url,
                        old["title"] or "Unknown title", int(provider != "legacy"),
                        "AVAILABLE" if path else "UNKNOWN", now, now,
                    ),
                )
                origin_by_track[old["track_uuid"]] = origin_id
                if path:
                    stat = path.stat()
                    cursor = con.execute(
                        """INSERT INTO assets(
                           track_id,origin_id,path,state,created_at,activated_at,sha256,
                           size_bytes,mtime_ns,downloader_name,source_url
                        ) VALUES (?,?,?,'CURRENT',?,?,?,?,?,?,?)""",
                        (
                            old["track_uuid"], origin_id, str(path), now, now,
                            sha256_file(path), stat.st_size, stat.st_mtime_ns,
                            "legacy-import", url,
                        ),
                    )
                    con.execute(
                        "UPDATE tracks SET current_asset_id=? WHERE id=?",
                        (cursor.lastrowid, old["track_uuid"]),
                    )
                    if args.queue_reprocess and provider != "legacy":
                        queued.append((old["track_uuid"], old["user_id"], origin_id))

            groups = {}
            for membership in memberships:
                if membership["track_uuid"] not in origin_by_track:
                    continue
                groups.setdefault(
                    (membership["user_id"], membership["playlist_name"]), []
                ).append(membership["track_uuid"])
            for (user, title), track_ids in groups.items():
                source_id = str(uuid.uuid4())
                con.execute(
                    """INSERT INTO sources(
                       id,user_id,provider,source_key,url,title,is_playlist,status
                    ) VALUES (?,?,'legacy',?,?,?,1,'OK')""",
                    (source_id, user, source_id, "legacy:" + source_id, title + " (legacy)"),
                )
                for position, track_id in enumerate(track_ids):
                    con.execute(
                        "INSERT INTO memberships VALUES (?,?,?,?,?)",
                        (source_id, track_id, origin_by_track[track_id], None, position),
                    )

        for old, ref in monitored_refs:
            store.add_source(ref, old["user_id"], True, old["label"] or ref.key)
        for track_id, user, origin_id in queued:
            store.queue_track(track_id, user, {"mode": "reprocess", "origin_id": origin_id})
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            Path(str(store.path) + suffix).unlink(missing_ok=True)
        raise

    for source_row in store.rows("SELECT * FROM sources WHERE provider='legacy'"):
        write_playlist(store, source_row)
    (STATE / "legacy-import-report.json").write_text(json.dumps(report, indent=2) + "\n")
    store.event(
        "WARNING",
        "Legacy import completed. Date-only history remains unknown until an original playlist is synchronized.",
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--old-library-root", type=Path, required=True)
    parser.add_argument("--old-working-directory", type=Path, default=Path("/app/src"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--queue-reprocess", action="store_true")
    try:
        import_legacy(parser.parse_args())
    except (ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
