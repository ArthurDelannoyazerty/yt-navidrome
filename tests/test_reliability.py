"""Failure-injection regressions for durable operations, without remote traffic."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from common import atomic_json, sha256_file
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from runtime import DeferredOperation

MBID = "11111111-1111-4111-8111-111111111111"


def active_track(db, track, environment):
    origin = db.origins_for_track(track["id"])[0]
    path = environment[1] / track["user_id"] / "song.opus"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"original-audio")
    plan = db.identity_plan(track["id"], origin["id"], {"mbid": MBID}, "ingest")
    stat = path.stat()
    db.commit_processed_identity(
        plan,
        {"file_path": str(path), "beets_id": 1, "matched_title": "Artist - Song", "mbid": MBID, "release_id": None},
        {"path": str(path), "sha256": sha256_file(path), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "ingest", "fixture-active",
    )
    return db.owned("tracks", track["id"], track["user_id"]), origin, path


def test_comment_refreshes_current_fingerprint(db, track, environment, monkeypatch):
    track, _, path = active_track(db, track, environment)
    pipeline = Pipeline(db)

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "comment"
        path.write_bytes(b"audio-with-new-discovery-tags")
        return {}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_track(track["id"], "admin", {"mode": "comment"})
    asyncio.run(pipeline.process_track(db.claim()))
    asset = db.current_asset(track["id"])
    assert asset["sha256"] == sha256_file(path)
    assert asset["size_bytes"] == path.stat().st_size
    assert asset["mtime_ns"] == path.stat().st_mtime_ns


def test_dedup_comment_refreshes_current_fingerprint(db, track, environment, monkeypatch):
    track, _, path = active_track(db, track, environment)
    pipeline = Pipeline(db)

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "comment"
        path.write_bytes(b"updated-comment-after-deduplication")

    monkeypatch.setattr(pipeline, "bridge", bridge)
    receipt = {
        "operation_id": "dedup", "user_id": "admin", "finalized_at": None,
        "result": {"kind": "DEDUPLICATED", "target_id": track["id"], "source_id": track["id"], "new_path": str(path)},
    }
    asyncio.run(pipeline.finish_operation_receipt(
        {"user_id": "admin", "target": track["id"]}, receipt, environment[0] / "staging" / "dedup",
    ))
    assert db.current_asset(track["id"])["sha256"] == sha256_file(path)


@pytest.mark.parametrize("state", ["missing", "empty", "changed", "healthy"])
def test_dedup_requires_real_unchanged_audio(db, track, environment, state):
    track, _, path = active_track(db, track, environment)
    asset = db.current_asset(track["id"])
    if state == "missing":
        path.unlink()
    elif state == "empty":
        path.write_bytes(b"")
    elif state == "changed":
        path.write_bytes(b"externally-corrupted")
    assert asyncio.run(Pipeline(db).usable_asset(track, asset)) == (state == "healthy")


def test_missing_canonical_asset_uses_new_candidate(db, track, environment, monkeypatch):
    canonical, _, missing = active_track(db, track, environment)
    missing.unlink()
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(source, Snapshot("Other", [Entry(
        "lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Another upload", "2022-01-01T00:00:00Z", "other-entry", 0,
    )]))
    incoming = db.one("SELECT * FROM tracks WHERE id<>?", (canonical["id"],))
    origin = db.origins_for_track(incoming["id"])[0]
    directory = environment[0] / "staging" / incoming["id"]
    directory.mkdir(parents=True)
    candidate = directory / "candidate.opus"
    candidate.write_bytes(b"fresh-audio")
    pipeline = Pipeline(db)
    applied = []

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == "apply":
            applied.append(current["id"])
            missing.write_bytes(candidate.read_bytes())
            return {"file_path": str(missing), "mbid": MBID, "matched_title": "Artist - Song", "beets_id": 1}
        assert mode == "delete"
        return {"removed": False}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    asyncio.run(pipeline._apply_candidate(
        {"user_id": "admin", "target": incoming["id"]}, incoming, origin,
        {"mbid": MBID}, candidate, {}, "ingest", "restore-canonical", directory,
    ))
    assert applied == [canonical["id"]]
    assert missing.read_bytes() == b"fresh-audio"
    assert db.current_asset(canonical["id"])["sha256"] == sha256_file(missing)
    assert db.owned("tracks", canonical["id"], "admin")["health"] == "AVAILABLE"


@pytest.mark.parametrize("binding", ["missing", "changed", "redownloaded", "same"])
def test_approval_is_bound_to_candidate_bytes(db, track, environment, monkeypatch, binding):
    pipeline = Pipeline(db)
    origin = db.origins_for_track(track["id"])[0]
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    candidate.write_bytes(b"reviewed-audio")
    pending = {"action": "ingest", "origin_id": origin["id"], "operation": "approval"}
    if binding != "missing":
        pending["candidate_sha256"] = sha256_file(candidate)
    if binding == "changed":
        candidate.write_bytes(b"different-audio")
    if binding == "redownloaded":
        candidate.unlink()
    atomic_json(directory / "operation.json", pending)
    atomic_json(directory / "download-complete.json", {"source_url": origin["url"]})
    choice = {"mbid": MBID, "title": "Song", "tags": {}}
    db.pause_for_approval(track["id"], [choice], pending, str(candidate))
    db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
    identified, applied = [], []

    async def download(url, track_id, workdir, report):
        candidate.write_bytes(b"newly-downloaded-audio")
        return candidate, {"source_url": url}

    async def identify(*args):
        identified.append(True)
        return {"choices": [choice], "automatic": True, "recommendation": "strong"}, [choice]

    async def apply(*args, **kwargs):
        applied.append(True)

    monkeypatch.setattr(pipeline.downloader, "download", download)
    monkeypatch.setattr(pipeline, "_identification_choices", identify)
    monkeypatch.setattr(pipeline, "_apply_candidate", apply)
    asyncio.run(pipeline.process_track(db.claim()))
    if binding == "same":
        assert applied == [True]
        assert not identified
    else:
        assert identified == [True]
        assert not applied
        current = db.owned("tracks", track["id"], "admin")
        assert current["operation_state"] == "NEEDS_APPROVAL"
        assert json.loads(current["pending_operation"])["candidate_sha256"] == sha256_file(candidate)


def test_delete_commit_failure_preserves_file_and_ignore_rules(db, track, environment, monkeypatch):
    track, _, path = active_track(db, track, environment)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected full disk")

    monkeypatch.setattr(db, "_record_receipt_locked", fail)
    with pytest.raises(sqlite3.OperationalError):
        db.delete_track_with_receipt(track["id"], "admin", "delete-op", "delete_ignore")
    assert path.read_bytes() == b"original-audio"
    assert db.owned("tracks", track["id"], "admin")
    assert not db.list_ignored("admin")
    assert db.current_asset(track["id"])


def test_delete_cleanup_is_resumable_after_track_row_is_gone(db, track, environment, monkeypatch):
    track, _, path = active_track(db, track, environment)
    pipeline = Pipeline(db)
    calls = []

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "forget"
        calls.append(mode)
        if len(calls) == 1:
            raise OSError("injected beets failure")
        return {"removed": True}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_track(track["id"], "admin", {"mode": "delete_ignore", "operation": "delete-op"})
    job = db.claim()
    with pytest.raises(DeferredOperation):
        asyncio.run(pipeline.process_track(job))
    assert not db.one("SELECT id FROM tracks WHERE id=?", (track["id"],))
    assert path.exists()
    assert db.list_ignored("admin")
    assert not db.operation_receipt("delete-op")["finalized_at"]
    db.recover()
    asyncio.run(pipeline.process_track(db.claim()))
    assert not path.exists()
    assert db.operation_receipt("delete-op")["finalized_at"]
    asyncio.run(pipeline.process_track(job))
    assert calls == ["forget", "forget"]


def test_delete_rejects_paths_outside_user_library_before_commit(db, track, environment):
    track, _, path = active_track(db, track, environment)
    outside = environment[1] / "guest" / "other.opus"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"other-user")
    db.update("assets", db.current_asset(track["id"])["id"], path=str(outside))
    with pytest.raises(ValueError, match="outside"):
        db.delete_track_with_receipt(track["id"], "admin", "unsafe-delete", "delete_ignore")
    assert outside.read_bytes() == b"other-user"
    assert path.exists()
    assert not db.operation_receipt("unsafe-delete")
    assert not db.list_ignored("admin")


def make_v1(db, partial=False):
    with db.db() as con:
        con.execute("ALTER TABLE jobs DROP COLUMN defer_count")
        if not partial:
            con.execute("ALTER TABLE jobs DROP COLUMN not_before")
        con.execute("PRAGMA user_version=1")


@pytest.mark.parametrize("after_first_column", [True, False])
def test_migration_ddl_and_version_roll_back_together(db, monkeypatch, after_first_column):
    make_v1(db)
    original = db._migrate_v1

    def fail(con):
        if after_first_column:
            con.execute("ALTER TABLE jobs ADD COLUMN not_before REAL NOT NULL DEFAULT 0")
        else:
            original(con)
        raise RuntimeError("injected interruption")

    monkeypatch.setattr(db, "_migrate_v1", fail)
    with pytest.raises(RuntimeError):
        db.init()
    assert db.one("PRAGMA user_version")["user_version"] == 1
    assert "not_before" not in {row["name"] for row in db.rows("PRAGMA table_info(jobs)")}
    monkeypatch.setattr(db, "_migrate_v1", original)
    db.init()
    assert db.one("PRAGMA user_version")["user_version"] == 2


def test_migration_recovers_an_old_partial_alter(db):
    make_v1(db, partial=True)
    db.init()
    assert {"defer_count", "not_before"} <= {row["name"] for row in db.rows("PRAGMA table_info(jobs)")}
    assert db.one("PRAGMA user_version")["user_version"] == 2
