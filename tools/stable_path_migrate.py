#!/usr/bin/env python3
"""Offline, resumable, move-only migration to <user>/audio/<track-id>.opus.

The importer MUST be stopped. An exclusive instance.lock prevents a concurrent
API startup. Navidrome must not scan during the file moves. This tool NEVER
changes audio tags, and its acknowledgement is an explicit human checkpoint.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from common import atomic_json, sha256_file, utcnow  # noqa: E402


class Migration:
    def __init__(self, state: Path, library: Path):
        self.state = state.resolve()
        self.library = library.resolve()
        self.journal = self.state / "stable-path-migration.json"
        self.database = self.state / "ingestor.sqlite"

    @contextmanager
    def locked(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with (self.state / "instance.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Stop the importer before migration; instance.lock is held") from None
            yield

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.database)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def resolve_beets_path(value, user_root: Path) -> Path:
        """Beets 2.14 can store paths relative to the configured user library."""
        stored = Path(os.fsdecode(value))
        return (stored if stored.is_absolute() else user_root / stored).resolve()

    def inspect(self):
        if not self.database.is_file():
            raise ValueError("No importer database at " + str(self.database))
        with self.db() as con:
            pending = con.execute("SELECT COUNT(*) FROM jobs WHERE state='RUNNING'").fetchone()[0]
            if pending:
                raise ValueError(f"{pending} running jobs exist; recover/quiesce them before moving files")
            # Queued work is safe while the instance lock is held: it cannot
            # publish or move files until the importer starts again.
            rows = con.execute("""SELECT t.id,t.user_id,t.file_path,t.beets_id,t.operation_state,
                t.current_asset_id,a.path,a.sha256 FROM tracks t
                JOIN assets a ON a.id=t.current_asset_id AND a.state='CURRENT'
                ORDER BY t.user_id,t.id""").fetchall()
        plans, skipped, dest_seen, old_seen = [], 0, set(), set()
        for row in rows:
            user = row["user_id"]
            raw_old = Path(row["path"])
            if raw_old.is_symlink():
                raise ValueError(f"Refusing symlink audio: {raw_old}")
            old = raw_old.resolve()
            user_root = (self.library / user).resolve()
            dest = user_root / "audio" / (row["id"] + ".opus")
            if old == dest:
                continue
            if row["operation_state"] == "NEEDS_APPROVAL" and old.parent == user_root / "Waiting for approval":
                skipped += 1  # temporary copies are removed when explicitly approved
                continue
            if not old.is_relative_to(user_root):
                raise ValueError(f"Unsafe path outside user library: {old}")
            if not old.is_file() or old.is_symlink():
                raise ValueError(f"Missing or symlink audio: {old}")
            if dest.exists() or dest in dest_seen or old in old_seen:
                raise ValueError(f"Target collision or duplicate source path: {dest}")
            dest_seen.add(dest)
            old_seen.add(old)
            digest = sha256_file(old)
            if row["sha256"] and digest != row["sha256"]:
                raise ValueError(f"File checksum no longer matches the importer database: {old}")
            beets_db = self.state / "beets" / user / "library.db"
            if not beets_db.is_file() or row["beets_id"] is None:
                raise ValueError(f"Missing beets item/database for {user}/{row['id']}")
            with sqlite3.connect(beets_db) as bcon:
                br = bcon.execute("SELECT path FROM items WHERE id=?", (row["beets_id"],)).fetchone()
                if br is None or self.resolve_beets_path(br[0], user_root) != old:
                    raise ValueError(f"Beets item path mismatch for {user}/{row['id']}")
            plans.append({"track_id": row["id"], "user": user,
                          "asset_id": row["current_asset_id"], "beets_id": row["beets_id"],
                          "source": str(old), "destination": str(dest),
                          "sha256": digest, "phase": "PENDING"})
        return {"version": 1, "state": "PLANNED", "created_at": utcnow(),
                "entries": plans, "temporary_waiting_skipped": skipped,
                "notes": "Move-only, no tag changes. Rescan Navidrome then verify track IDs/ratings/playlists."}

    def get(self):
        return json.loads(self.journal.read_text()) if self.journal.is_file() else None

    def save(self, plan):
        atomic_json(self.journal, plan)

    def backup_sqlite(self, file, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return
        with sqlite3.connect(file) as source, sqlite3.connect(dest) as destination:
            source.backup(destination)

    def backups(self, plan):
        folder = self.state / "backups" / ("stable-path-" + plan["created_at"].replace(":", "-").replace("Z", ""))
        self.backup_sqlite(self.database, folder / "ingestor.sqlite")
        for user in sorted({entry["user"] for entry in plan["entries"]}):
            self.backup_sqlite(self.state / "beets" / user / "library.db", folder / ("beets-" + user + ".sqlite"))
        plan["backup_dir"] = str(folder)
        self.save(plan)

    def migrate_entry(self, entry):
        old = Path(entry["source"])
        new = Path(entry["destination"])
        expected = entry["sha256"]
        if new.exists():
            if old.exists() or sha256_file(new) != expected:
                raise ValueError("Migration collision or modified destination: " + str(new))
        else:
            if not old.is_file() or sha256_file(old) != expected:
                raise ValueError("Original file disappeared or changed: " + str(old))
            new.parent.mkdir(parents=True, exist_ok=True)
            # Atomic rename on the existing music filesystem; refuse cross-device moves.
            os.replace(old, new)
        if sha256_file(new) != expected:
            raise ValueError("Audio hash changed during move")
        # Each database can be retried after a crash without moving the file again.
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT path FROM assets WHERE id=? AND state='CURRENT'", (entry["asset_id"],)).fetchone()
            if not row or row["path"] not in {entry["source"], entry["destination"]}:
                raise ValueError("Asset path changed concurrently")
            con.execute("UPDATE assets SET path=?,sha256=? WHERE id=?", (entry["destination"], expected, entry["asset_id"]))
            con.execute("UPDATE tracks SET file_path=? WHERE id=? AND user_id=?",
                        (entry["destination"], entry["track_id"], entry["user"]))
        beets_db = self.state / "beets" / entry["user"] / "library.db"
        with sqlite3.connect(beets_db) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT path FROM items WHERE id=?", (entry["beets_id"],)).fetchone()
            user_root = (self.library / entry["user"]).resolve()
            if not row or self.resolve_beets_path(row[0], user_root) not in {old, new}:
                raise ValueError("Beets item changed concurrently")
            # Preserve beets' existing path representation. Relative beets rows
            # are relative to this user's configured library, not /app/src.
            relative = not Path(os.fsdecode(row[0])).is_absolute()
            destination = new.relative_to(user_root) if relative else new
            con.execute("UPDATE items SET path=? WHERE id=?", (os.fsencode(destination), entry["beets_id"]))

    def rewrite_playlists(self):
        # Import after env has been configured, not before.
        import pipeline
        import common
        previous_pipeline, previous_common = pipeline.LIBRARY, common.LIBRARY
        try:
            pipeline.LIBRARY = self.library
            common.LIBRARY = self.library
            from store import Store
            store = Store(self.database)
            sources = store.rows("SELECT * FROM sources WHERE is_playlist=1 AND status NOT IN ('REMOVED','PURGING','PURGE_FAILED')")
            for source in sources:
                pipeline.write_playlist(store, source)
        finally:
            pipeline.LIBRARY, common.LIBRARY = previous_pipeline, previous_common

    def apply(self, *, backup_confirmed, canary_count=3):
        if not backup_confirmed:
            raise ValueError("Provide --music-backed-up after backing up music and Navidrome's database")
        if not 1 <= canary_count <= 50:
            raise ValueError("Choose between 1 and 50 canary files")
        plan = self.get()
        if not plan:
            plan = self.inspect()
            self.save(plan)
        if plan["state"] == "COMPLETE":
            return plan
        # The importer stays stopped until a person verifies the scanner's
        # persistent track IDs, playlist membership and favorites/ratings.
        if plan["state"] in {"AWAITING_CANARY_SCAN", "AWAITING_NAVIDROME_SCAN"}:
            return plan
        if plan["state"] not in {"PLANNED", "MOVING", "CANARY_VERIFIED"}:
            raise ValueError("Unknown migration state")
        self.backups(plan)
        if plan["state"] == "PLANNED":
            plan["canary_count"] = canary_count
            plan["goal"] = "CANARY" if len(plan["entries"]) > canary_count else "FULL"
        elif plan["state"] == "CANARY_VERIFIED":
            plan["goal"] = "FULL"
        goal = plan.get("goal", "FULL")  # older interrupted journals resume safely
        plan["state"] = "MOVING"
        self.save(plan)
        entries = (plan["entries"][:plan.get("canary_count", canary_count)]
                   if goal == "CANARY" else plan["entries"])
        for entry in entries:
            if entry["phase"] != "DONE":
                self.migrate_entry(entry)
                entry["phase"] = "DONE"
                self.save(plan)
        self.rewrite_playlists()
        plan["state"] = "AWAITING_CANARY_SCAN" if goal == "CANARY" else "AWAITING_NAVIDROME_SCAN"
        self.save(plan)
        return plan

    def acknowledge(self, *, navidrome_verified):
        plan = self.get()
        if not plan or plan["state"] not in {"AWAITING_CANARY_SCAN", "AWAITING_NAVIDROME_SCAN"}:
            raise ValueError("The move-only scan checkpoint has not been reached")
        if not navidrome_verified:
            raise ValueError("Verify Navidrome song IDs, playlists and ratings before acknowledgement")
        if plan["state"] == "AWAITING_CANARY_SCAN":
            plan["state"] = "CANARY_VERIFIED"
            plan["canary_verified_at"] = utcnow()
        else:
            plan["state"] = "COMPLETE"
            plan["verified_at"] = utcnow()
        self.save(plan)
        return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply", "status", "ack"))
    parser.add_argument("--state-dir", default=os.getenv("INGESTOR_STATE_DIR", "/data/state"))
    parser.add_argument("--library-dir", default=os.getenv("NAVIDROME_LIB_DIR", "/data/library"))
    parser.add_argument("--music-backed-up", action="store_true")
    parser.add_argument("--canary-count", type=int, default=3,
                        help="Number of files to migrate for the first Navidrome verification")
    parser.add_argument("--navidrome-verified", action="store_true")
    args = parser.parse_args(argv)
    migration = Migration(Path(args.state_dir), Path(args.library_dir))
    with migration.locked():
        if args.action == "plan":
            result = migration.get() or migration.inspect()
            # A plan does not mutate application DBs or music.
        elif args.action == "apply":
            result = migration.apply(backup_confirmed=args.music_backed_up,
                                     canary_count=args.canary_count)
        elif args.action == "ack":
            result = migration.acknowledge(navidrome_verified=args.navidrome_verified)
        else:
            result = migration.get() or {"state": "NOT_STARTED"}
    print(json.dumps({"state": result["state"], "entries": len(result.get("entries", [])),
                      "waiting_exempt": result.get("temporary_waiting_skipped", 0),
                      "backup_dir": result.get("backup_dir"), "journal": str(migration.journal)}, indent=2))


if __name__ == "__main__":
    main()
