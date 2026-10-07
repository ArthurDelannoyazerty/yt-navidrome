"""Read-only legacy migration. Requires a COPY of the library, never modifies the old DB.

Default is dry-run. File moves/tag changes happen only later in the application,
not in this tool. Old date-only values are retained as provenance, not fabricated
midnight timestamps. Old approval candidate JSON is deliberately not trusted.
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
from common import LIBRARY, STATE, user_name, utcnow
from providers import parse_source
from store import Store


def import_legacy(args):
    source = sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    tracks = [dict(r) for r in source.execute("SELECT * FROM tracks")]
    memberships = [dict(r) for r in source.execute("SELECT * FROM playlist_memberships")]
    monitored = [dict(r) for r in source.execute("SELECT * FROM monitored_urls")]
    users = [r[0] for r in source.execute("SELECT username FROM users")]
    source.close()
    for user in {*(users), *(t["user_id"] for t in tracks)}:
        user_name(user)  # Abort rather than silently renaming legacy users/folders.
    prepared, report = [], {"tracks": len(tracks), "preserved_files": 0, "unavailable_audio": [], "unsupported_sources": [], "date_precision": "Legacy dates have no recoverable time of day"}
    identities = set()
    for old in tracks:
        try:
            ref = parse_source(old["source_url"])
            if ref.is_playlist:
                raise ValueError("Legacy track contains a playlist URL")
            provider, key, url = ref.provider, ref.key, ref.url
        except ValueError:
            provider, key, url = "legacy", old["track_uuid"], old["source_url"]
        identity = (old["user_id"], provider, key)
        if identity in identities:
            raise ValueError(f"Canonical duplicate found: {identity}. Resolve it in a copy of the old database first")
        identities.add(identity)
        path = None
        if old.get("file_path"):
            recorded = Path(old["file_path"])
            recorded = (recorded if recorded.is_absolute() else args.old_working_directory / recorded).resolve()
            if recorded.is_relative_to(args.old_library_root.resolve()):
                candidate = (LIBRARY / recorded.relative_to(args.old_library_root.resolve())).resolve()
                if candidate.is_relative_to((LIBRARY / old["user_id"]).resolve()) and candidate.is_file():
                    path = str(candidate)
        if path:
            report["preserved_files"] += 1
        else:
            report["unavailable_audio"].append({"id": old["track_uuid"], "url": url, "old_status": old["status"], "action": "Retry supported URLs in the new UI; old approval candidates are not migrated"})
        prepared.append((old, provider, key, url, path))
    monitored_refs = []
    for old in monitored:
        try:
            monitored_refs.append((old, parse_source(old["url"])))
        except ValueError as exc:
            report["unsupported_sources"].append({"url": old["url"], "reason": str(exc)})
    print(json.dumps(report, indent=2))
    if not args.apply:
        print("DRY RUN. No database, audio or tags changed. Use --apply only after reviewing this report.")
        return report
    store = Store()
    if store.path.exists():
        raise ValueError("Destination database already exists. Use a fresh state directory for migration")
    STATE.mkdir(parents=True, exist_ok=True)
    store.init()
    try:
        with store.db() as con:
            con.executemany("INSERT OR IGNORE INTO users VALUES (?)", [(u,) for u in {*(users), *(t["user_id"] for t in tracks)}])
            for old, provider, key, url, path in prepared:
                con.execute("""INSERT INTO tracks(id,user_id,provider,media_key,url,title,discovered_at,
                  discovery_basis,status,error,file_path,matched_title,mbid) VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?)""",
                  (old["track_uuid"], old["user_id"], provider, key, url, old["title"] or "Unknown title",
                   "legacy_date_only:" + (old.get("discovery_date") or "unknown"),
                   "COMPLETED" if path else "FAILED", None if path else "Legacy audio unavailable: retry or re-add a supported source URL",
                   path, old.get("matched_title"), old.get("mbid")))
                if path and args.queue_reidentify:
                    store.enqueue("track", old["track_uuid"], old["user_id"], {"mode": "reidentify"}, con=con)
                    con.execute("UPDATE tracks SET status='PENDING' WHERE id=?", (old["track_uuid"],))
            # Preserve memberships under explicitly labelled, non-monitored legacy sources.
            # Do not guess which similarly-named online playlist they came from.
            groups = {}
            track_ids = {t[0]["track_uuid"] for t in prepared}
            for m in memberships:
                if m["track_uuid"] not in track_ids:
                    continue
                group = (m["user_id"], m["playlist_name"])
                groups.setdefault(group, []).append(m["track_uuid"])
            for (user, title), members in groups.items():
                sid = str(uuid.uuid4())
                con.execute("""INSERT INTO sources(id,user_id,provider,source_key,url,title,is_playlist,status)
                  VALUES (?,?,'legacy',?,?,?,1,'OK')""", (sid, user, sid, "legacy:" + sid, title + " (legacy)"))
                for position, tid in enumerate(members):
                    con.execute("INSERT INTO memberships VALUES (?,?,?,NULL,?)", (sid, tid, tid, position))
            for old, ref in monitored_refs:
                sid = str(uuid.uuid4())
                con.execute("""INSERT OR IGNORE INTO sources(id,user_id,provider,source_key,url,title,is_playlist,monitored)
                  VALUES (?,?,?,?,?,?,?,1)""", (sid, old["user_id"], ref.provider, ref.key, ref.url,
                    old["label"] or ref.key, int(ref.is_playlist)))
                actual = con.execute("SELECT id FROM sources WHERE user_id=? AND provider=? AND source_key=?",
                                     (old["user_id"], ref.provider, ref.key)).fetchone()[0]
                store.enqueue("sync", actual, old["user_id"], con=con)
    except Exception:
        # The newly created DB is ours alone. Never touch the supplied legacy DB.
        for suffix in ("", "-wal", "-shm"):
            Path(str(store.path) + suffix).unlink(missing_ok=True)
        raise
    from pipeline import write_playlist
    for legacy in store.rows("SELECT * FROM sources WHERE provider='legacy'"):
        write_playlist(store, legacy)
    store.event("WARNING", "Legacy import: date-only history retained without invented times. Sync original playlists to backfill exact timestamps.")
    (STATE / "legacy-import-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--old-library-root", type=Path, required=True, help="Library root as recorded in the old database, not the copied mount")
    parser.add_argument("--old-working-directory", type=Path, default=Path("/app/src"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--queue-reidentify", action="store_true", help="Queue metadata identification from copied audio, without redownloading")
    try:
        import_legacy(parser.parse_args())
    except (ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
