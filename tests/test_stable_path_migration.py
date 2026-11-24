"""One-time offline path migration: no retagging, restart-safe, user-scoped."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from common import sha256_file
from test_catalogue_album import add_audio_track, RECORDING_4, RECORDING_4
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from stable_path_migrate import Migration


def fixture_legacy(db, environment):
    from pathlib import Path
    tid, old, digest = add_audio_track(db, environment, legacy=True)
    user = "admin"
    beets_dir = environment[0] / "beets" / user
    beets_dir.mkdir(parents=True, exist_ok=True)
    beets_file = beets_dir / "library.db"
    with sqlite3.connect(beets_file) as con:
        con.execute("CREATE TABLE items (id INTEGER PRIMARY KEY,path BLOB)")
        con.execute("INSERT INTO items VALUES (?,?)", (23, os.fsencode(old)))
    db.execute("UPDATE tracks SET beets_id=23 WHERE id=?", (tid,))
    return tid, old, digest, beets_file


def test_one_time_migration_preserves_file_bytes_and_beets_path(db, environment):
    tid, old, digest, beets_db = fixture_legacy(db, environment)
    migration = Migration(*environment)
    plan = migration.inspect()
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["sha256"] == digest
    assert old.exists()  # plan does not change bytes or state
    with migration.locked():
        applied = migration.apply(backup_confirmed=True)
    assert applied["state"] == "AWAITING_NAVIDROME_SCAN"
    dest = environment[1] / "admin" / "audio" / f"{tid}.opus"
    assert not old.exists() and dest.exists()
    assert sha256_file(dest) == digest
    assert db.one("SELECT file_path FROM tracks WHERE id=?", (tid,))["file_path"] == str(dest)
    assert db.one("SELECT path FROM assets WHERE track_id=? AND state='CURRENT'", (tid,))["path"] == str(dest)
    with sqlite3.connect(beets_db) as con:
        assert os.fsdecode(con.execute("SELECT path FROM items WHERE id=23").fetchone()[0]) == str(dest)
    assert Path(applied["backup_dir"]).is_dir()
    with pytest.raises(ValueError, match="Verify Navidrome"):
        migration.acknowledge(navidrome_verified=False)
    with migration.locked():
        complete = migration.acknowledge(navidrome_verified=True)
        assert complete["state"] == "COMPLETE"
        assert migration.apply(backup_confirmed=True)["state"] == "COMPLETE"


def test_interrupted_file_move_is_recovered_without_data_loss(db, environment):
    tid, old, digest, beets_db = fixture_legacy(db, environment)
    migration = Migration(*environment)
    plan = migration.inspect()
    migration.save(plan)
    new = Path(plan["entries"][0]["destination"])
    new.parent.mkdir(parents=True, exist_ok=True)
    old.replace(new)  # crash after physical move, before SQLite updates
    with migration.locked():
        applied = migration.apply(backup_confirmed=True)
    assert applied["state"] == "AWAITING_NAVIDROME_SCAN"
    assert sha256_file(new) == digest
    assert db.one("SELECT file_path FROM tracks WHERE id=?", (tid,))["file_path"] == str(new)
    with sqlite3.connect(beets_db) as con:
        assert os.fsdecode(con.execute("SELECT path FROM items").fetchone()[0]) == str(new)


def test_migration_aborts_on_target_collision_before_any_move(db, environment):
    tid, old, digest, _ = fixture_legacy(db, environment)
    destination = environment[1] / "admin" / "audio" / f"{tid}.opus"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"another person's audio")
    with pytest.raises(ValueError, match="collision"):
        Migration(*environment).inspect()
    assert old.exists() and sha256_file(old) == digest


def test_migration_refuses_mutation_while_importer_is_locked(db, environment):
    tid, old, digest, _ = fixture_legacy(db, environment)
    migration = Migration(*environment)
    with migration.locked():
        with pytest.raises(RuntimeError, match="Stop the importer"):
            with migration.locked():
                pass
    assert old.exists()


def test_migration_blocks_importer_restart_until_navidrome_checkpoint(db):
    from fastapi.testclient import TestClient
    from main import create_app
    journal = db.path.parent / "stable-path-migration.json"
    journal.write_text(json.dumps({"state": "AWAITING_NAVIDROME_SCAN"}))
    with pytest.raises(RuntimeError, match="verify Navidrome"):
        with TestClient(create_app(db, start_workers=False)):
            pass
    journal.write_text(json.dumps({"state": "COMPLETE"}))
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get("/").status_code == 200


def test_migration_rejects_symlinked_source_even_with_valid_target(db, environment):
    _, old, _, _ = fixture_legacy(db, environment)
    other = old.parent / "real-audio.opus"
    old.rename(other)
    old.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        Migration(*environment).inspect()


def test_migration_verifies_canary_before_moving_the_remaining_library(db, environment):
    first, old_first, first_digest, _ = fixture_legacy(db, environment)
    second, old_second, second_digest = add_audio_track(db, environment, RECORDING_4, legacy=True)
    # The fixture uses the first row's beets DB for this user's other imported file.
    beets_path = environment[0] / "beets" / "admin" / "library.db"
    with sqlite3.connect(beets_path) as con:
        con.execute("INSERT INTO items VALUES (?,?)", (24, os.fsencode(old_second)))
    db.execute("UPDATE tracks SET beets_id=24 WHERE id=?", (second,))
    migration = Migration(*environment)
    with migration.locked():
        canary = migration.apply(backup_confirmed=True, canary_count=1)
    assert canary["state"] == "AWAITING_CANARY_SCAN"
    assert sum(x["phase"] == "DONE" for x in canary["entries"]) == 1
    remaining = next(x for x in canary["entries"] if x["phase"] == "PENDING")
    assert Path(remaining["source"]).is_file()  # Nothing else moved yet.
    assert migration.apply(backup_confirmed=True)["state"] == "AWAITING_CANARY_SCAN"
    with migration.locked():
        assert migration.acknowledge(navidrome_verified=True)["state"] == "CANARY_VERIFIED"
        rest = migration.apply(backup_confirmed=True)
    assert rest["state"] == "AWAITING_NAVIDROME_SCAN"
    assert all(x["phase"] == "DONE" for x in rest["entries"])
    assert all(sha256_file(Path(x["destination"])) == x["sha256"] for x in rest["entries"])
    with migration.locked():
        assert migration.acknowledge(navidrome_verified=True)["state"] == "COMPLETE"


def test_new_image_refuses_to_mix_legacy_and_stable_path_schemes(db, environment):
    from fastapi.testclient import TestClient
    from main import create_app
    fixture_legacy(db, environment)
    with pytest.raises(RuntimeError, match="Legacy album-derived file paths"):
        with TestClient(create_app(db, start_workers=True)):
            pass


def test_temporary_waiting_audio_is_exempt_from_stable_path_startup_guard(db, environment):
    """Pending approval audio stays in its temporary directory, not the approved layout."""
    from fastapi.testclient import TestClient
    from main import create_app

    track_id, original, digest, beets_db = fixture_legacy(db, environment)
    waiting = environment[1] / "admin" / "Waiting for approval" / original.name
    waiting.parent.mkdir(parents=True, exist_ok=True)
    original.replace(waiting)
    db.execute("UPDATE assets SET path=? WHERE track_id=? AND state='CURRENT'",
               (str(waiting), track_id))
    db.execute("""UPDATE tracks SET file_path=?, temporary_candidate_sha256=?,
               operation_state='NEEDS_APPROVAL' WHERE id=?""", (str(waiting), digest, track_id))
    with sqlite3.connect(beets_db) as con:
        con.execute("UPDATE items SET path=? WHERE id=23", (os.fsencode(waiting),))

    migration = Migration(*environment)
    plan = migration.inspect()
    assert plan["entries"] == []
    assert plan["temporary_waiting_skipped"] == 1

    # The approved-path startup check must agree with the offline migration.
    with TestClient(create_app(db, start_workers=True)) as client:
        assert client.get("/").status_code == 200


def test_relative_beets_paths_resolve_from_user_library_and_stay_relative(db, environment):
    """Real beets rows use paths like Artist/Album/Artist - Song.opus."""
    tid, old, digest, beets_db = fixture_legacy(db, environment)
    user_root = environment[1] / "admin"
    relative = old.relative_to(user_root)
    with sqlite3.connect(beets_db) as con:
        con.execute("UPDATE items SET path=? WHERE id=23", (os.fsencode(relative),))

    migration = Migration(*environment)
    plan = migration.inspect()
    assert len(plan["entries"]) == 1
    assert plan["entries"][0]["source"] == str(old)
    with migration.locked():
        result = migration.apply(backup_confirmed=True)
    assert result["state"] == "AWAITING_NAVIDROME_SCAN"
    destination = user_root / "audio" / (tid + ".opus")
    assert not old.exists() and sha256_file(destination) == digest
    with sqlite3.connect(beets_db) as con:
        raw = con.execute("SELECT path FROM items WHERE id=23").fetchone()[0]
        assert os.fsdecode(raw) == "audio/" + tid + ".opus"
    assert db.one("SELECT file_path FROM tracks WHERE id=?", (tid,))["file_path"] == str(destination)


def test_relative_beets_path_survives_interrupted_move(db, environment):
    """A crash after moving audio must resume with a relative beets DB row."""
    tid, old, digest, beets_db = fixture_legacy(db, environment)
    user_root = environment[1] / "admin"
    with sqlite3.connect(beets_db) as con:
        con.execute("UPDATE items SET path=? WHERE id=23",
                    (os.fsencode(old.relative_to(user_root)),))
    migration = Migration(*environment)
    plan = migration.inspect()
    migration.save(plan)
    destination = Path(plan["entries"][0]["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    old.replace(destination)
    with migration.locked():
        result = migration.apply(backup_confirmed=True)
    assert result["state"] == "AWAITING_NAVIDROME_SCAN"
    assert sha256_file(destination) == digest
    with sqlite3.connect(beets_db) as con:
        row = con.execute("SELECT path FROM items WHERE id=23").fetchone()
        assert os.fsdecode(row[0]) == "audio/" + tid + ".opus"


def test_relative_beets_path_mismatch_still_blocks_migration(db, environment):
    """The fix must not silently accept an unrelated beets item path."""
    tid, old, _, beets_db = fixture_legacy(db, environment)
    with sqlite3.connect(beets_db) as con:
        con.execute("UPDATE items SET path=? WHERE id=23", (b"Elsewhere/Other Song.opus",))
    with pytest.raises(ValueError, match="Beets item path mismatch"):
        Migration(*environment).inspect()
    assert old.exists()


def waiting_fixture(db, environment):
    """Old image: current playable, still unapproved, in the legacy folder."""
    tid, old, digest, beets_db = fixture_legacy(db, environment)
    waiting = environment[1] / "admin" / "Waiting for approval" / (tid + ".opus")
    waiting.parent.mkdir(parents=True, exist_ok=True)
    old.rename(waiting)
    db.execute(
        """UPDATE tracks SET file_path=?,operation_state='NEEDS_APPROVAL',
           temporary_candidate_sha256=? WHERE id=?""",
        (str(waiting), digest, tid),
    )
    db.execute("UPDATE assets SET path=? WHERE track_id=? AND state='CURRENT'",
               (str(waiting), tid))
    with sqlite3.connect(beets_db) as con:
        con.execute("UPDATE items SET path=? WHERE id=23", (os.fsencode(waiting),))
    return tid, waiting, digest, beets_db


def test_waiting_migration_updates_all_three_paths_and_preserves_review(db, environment):
    tid, waiting, digest, beets_db = waiting_fixture(db, environment)
    migration = Migration(*environment, waiting_only=True)
    plan = migration.inspect()
    assert len(plan["entries"]) == 1 and plan["mode"] == "waiting"
    # A separate journal must not affect the original approved-path migration.
    assert migration.journal.name == "waiting-audio-migration.json"
    assert Migration(*environment).inspect()["entries"] == []
    original_token = db.approval_token(db.owned("tracks", tid, "admin"))
    with migration.locked():
        migrated = migration.apply(backup_confirmed=True)
    assert migrated["state"] == "AWAITING_NAVIDROME_SCAN"
    path = environment[1] / "admin" / "audio" / (tid + ".opus")
    assert path.is_file() and sha256_file(path) == digest
    assert not waiting.exists()
    row = db.owned("tracks", tid, "admin")
    assert row["file_path"] == str(path)
    assert row["temporary_candidate_sha256"] == digest
    assert row["operation_state"] == "NEEDS_APPROVAL"
    assert db.approval_token(row) == original_token
    assert db.current_asset(tid)["path"] == str(path)
    with sqlite3.connect(beets_db) as con:
        assert os.fsdecode(con.execute("SELECT path FROM items WHERE id=23").fetchone()[0]) == str(path)
    assert Path(migrated["backup_dir"]).is_dir()
    with pytest.raises(ValueError, match="Verify Navidrome"):
        migration.acknowledge(navidrome_verified=False)
    with migration.locked():
        assert migration.acknowledge(navidrome_verified=True)["state"] == "COMPLETE"


def test_waiting_migration_survives_crash_between_move_and_sqlite_commit(db, environment):
    tid, old, digest, beets_db = waiting_fixture(db, environment)
    migration = Migration(*environment, waiting_only=True)
    plan = migration.inspect()
    migration.save(plan)
    destination = Path(plan["entries"][0]["destination"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    old.replace(destination)
    with migration.locked():
        result = migration.apply(backup_confirmed=True)
    assert result["state"] == "AWAITING_NAVIDROME_SCAN"
    assert sha256_file(destination) == digest
    assert db.current_asset(tid)["path"] == str(destination)
    with sqlite3.connect(beets_db) as con:
        assert os.fsdecode(con.execute("SELECT path FROM items WHERE id=23").fetchone()[0]) == str(destination)


def test_waiting_migration_refuses_modified_audio_or_occupied_target(db, environment):
    tid, old, digest, _ = waiting_fixture(db, environment)
    target = environment[1] / "admin" / "audio" / (tid + ".opus")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"collision")
    with pytest.raises(ValueError, match="Target collision"):
        Migration(*environment, waiting_only=True).inspect()
    target.unlink()
    old.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        Migration(*environment, waiting_only=True).inspect()


def test_waiting_migration_blocks_importer_until_navidrome_ack(db, environment):
    from fastapi.testclient import TestClient
    from main import create_app
    waiting = db.path.parent / "waiting-audio-migration.json"
    waiting.write_text(json.dumps({"state": "AWAITING_NAVIDROME_SCAN"}))
    with pytest.raises(RuntimeError, match="waiting-audio-migration"):
        with TestClient(create_app(db, start_workers=False)):
            pass
