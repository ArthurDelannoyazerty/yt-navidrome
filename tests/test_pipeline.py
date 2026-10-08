import asyncio
import json
from pathlib import Path

import pytest

from common import atomic_json
from pipeline import Pipeline, ReplacementIdentityMismatch, write_playlist


def activate(db, track, environment, mbid="11111111-1111-4111-8111-111111111111"):
    origin = db.one("SELECT * FROM track_origins")
    path = environment[1] / "admin" / "Artist" / "Song.opus"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old-audio")
    plan = db.identity_plan(track["id"], origin["id"], {"mbid": mbid, "tags": {}}, "ingest")
    result = {"file_path": str(path), "beets_id": 1, "matched_title": "Artist - Song", "mbid": mbid, "release_id": None}
    stat = path.stat()
    db.commit_processed_identity(plan, result, {
        "path": str(path), "sha256": "oldhash", "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns, "downloader_name": "yt-dlp",
        "downloader_version": "old", "source_url": origin["url"],
    }, "ingest", "setup-operation")
    return db.one("SELECT * FROM tracks"), origin, path


def test_failed_redownload_keeps_current_asset_and_health(db, track, environment, monkeypatch):
    track, origin, old_path = activate(db, track, environment)
    pipeline = Pipeline(db)

    async def fail(*args, **kwargs):
        raise RuntimeError("source removed")

    monkeypatch.setattr(pipeline.downloader, "download", fail)
    db.queue_track(track["id"], "admin", {"mode": "redownload", "origin_id": origin["id"]})
    job = db.claim()
    with pytest.raises(RuntimeError, match="source removed"):
        asyncio.run(pipeline.process_track(job))
    db.fail_operation(track["id"], "redownload", "source removed")
    current = db.one("SELECT * FROM tracks")
    assert current["health"] == "AVAILABLE"
    assert current["operation_state"] == "FAILED"
    assert old_path.read_bytes() == b"old-audio"
    assert db.current_asset(track["id"])["state"] == "CURRENT"


def test_redownload_refuses_identity_change_without_touching_old_file(db, track, environment, monkeypatch):
    track, origin, old_path = activate(db, track, environment)
    pipeline = Pipeline(db)
    candidate = environment[0] / "candidate.opus"
    candidate.write_bytes(b"new-audio")

    async def download(*args, **kwargs):
        target = kwargs.get("directory")
        return candidate, {"source_url": origin["url"], "downloader_name": "yt-dlp", "downloader_version": "new"}

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == "identify":
            return {
                "automatic": True, "recommendation": "strong",
                "choices": [{"mbid": "22222222-2222-4222-8222-222222222222", "tags": {}, "title": "Other"}],
            }
        raise AssertionError(mode)

    monkeypatch.setattr(pipeline, "bridge", bridge)
    identified = asyncio.run(pipeline._identification_choices(
        {"user_id": "admin", "target": track["id"]}, track,
        environment[0], candidate,
    ))[0]
    with pytest.raises(ReplacementIdentityMismatch):
        pipeline._select_automatic("redownload", track, identified)
    assert old_path.read_bytes() == b"old-audio"


def test_reprocess_different_strong_identity_requires_approval(db, track, environment, monkeypatch):
    track, origin, _ = activate(db, track, environment)
    pipeline = Pipeline(db)
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    candidate.write_bytes(b"new")
    receipt = {"source_url": origin["url"], "downloader_name": "yt-dlp", "downloader_version": "new"}
    atomic_json(directory / "download-complete.json", receipt)
    atomic_json(directory / "operation.json", {"operation": "op", "action": "reprocess", "origin_id": origin["id"]})

    async def bridge(job, current, workdir, mode, **kwargs):
        if mode == "identify":
            return {
                "automatic": True, "recommendation": "strong",
                "choices": [{
                    "mbid": "22222222-2222-4222-8222-222222222222", "tags": {},
                    "title": "Other", "artist": "Someone", "album": "", "similarity": 99,
                    "description": "", "kind": "candidate",
                }],
            }
        if mode == "metadata":
            return {"mbid": track["mbid"], "tags": {}, "title": "Song", "artist": "Artist", "kind": "current"}
        raise AssertionError(mode)

    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_track(track["id"], "admin", {"mode": "reprocess", "origin_id": origin["id"], "operation": "op"})
    job = db.claim()
    asyncio.run(pipeline.process_track(job))
    updated = db.one("SELECT * FROM tracks")
    assert updated["operation_state"] == "NEEDS_APPROVAL"
    assert json.loads(updated["pending_operation"])["action"] == "reprocess"
    assert updated["mbid"] == track["mbid"]


def test_playlist_export_uses_one_current_asset_for_repeated_memberships(db, track, environment):
    track, _, path = activate(db, track, environment)
    source = db.one("SELECT * FROM sources")
    origin = db.one("SELECT * FROM track_origins")
    with db.db() as con:
        con.execute("INSERT INTO memberships VALUES (?,?,?,?,?)", (source["id"], "second", origin["id"], "2022-01-01T00:00:00Z", 1))
    write_playlist(db, source)
    exported = Path(db.one("SELECT playlist_path FROM sources")["playlist_path"]).read_text()
    assert exported.count("Song.opus") == 2


def test_initial_ingest_creates_current_asset_and_completes(db, track, environment, monkeypatch):
    pipeline = Pipeline(db)
    origin = db.one("SELECT * FROM track_origins")
    final = environment[1] / "admin" / "Artist" / "Album" / "Song.opus"

    async def download(url, track_id, directory, report):
        candidate = directory / f"temp_{track_id}.opus"
        candidate.write_bytes(b"candidate")
        receipt = {
            "source_url": url,
            "downloader_name": "yt-dlp",
            "downloader_version": "test",
        }
        atomic_json(directory / "download-complete.json", receipt)
        return candidate, receipt

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == "identify":
            return {
                "automatic": True,
                "recommendation": "strong",
                "choices": [{
                    "mbid": "11111111-1111-4111-8111-111111111111",
                    "tags": {}, "title": "Song", "artist": "Artist",
                    "album": "Album", "similarity": 99.0,
                    "description": "", "kind": "candidate",
                }],
            }
        if mode == "apply":
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(Path(kwargs["path"]).read_bytes())
            return {
                "file_path": str(final), "beets_id": 1,
                "matched_title": "Artist - Song",
                "mbid": "11111111-1111-4111-8111-111111111111",
                "release_id": None, "warnings": [],
            }
        raise AssertionError(mode)

    monkeypatch.setattr(pipeline.downloader, "download", download)
    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.enqueue("track", track["id"], "admin", {"mode": "ingest", "origin_id": origin["id"]})
    job = db.claim()
    asyncio.run(pipeline.process_track(job))
    updated = db.one("SELECT * FROM tracks")
    assert updated["health"] == "AVAILABLE"
    assert updated["operation_state"] == "IDLE"
    assert updated["file_path"] == str(final)
    assert db.current_asset(track["id"])["downloader_version"] == "test"


def test_integrity_reports_missing_file_without_deleting_track(db, track, environment, monkeypatch):
    track, _, path = activate(db, track, environment)
    path.unlink()
    pipeline = Pipeline(db)

    async def audit(job, directory, name, request, timeout=1800):
        return {track["id"]: {"found": True, "path": str(path), "beets_id": 1}}

    monkeypatch.setattr(pipeline, "bridge_request", audit)
    job = {
        "id": 999,
        "user_id": "admin",
        "target": "admin",
        "kind": "integrity",
        "payload": '{"mode":"verify"}',
    }
    asyncio.run(pipeline.process_integrity(job))
    report = db.integrity_report("admin")
    assert any(issue["kind"] == "MISSING_FILE" for issue in report["issues"])
    assert db.one("SELECT health FROM tracks")["health"] == "MISSING"
    assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 1


def test_apply_result_recovery_activates_without_downloading_again(db, track, environment, monkeypatch):
    pipeline = Pipeline(db)
    origin = db.one("SELECT * FROM track_origins")
    operation = "recover-operation"
    db.enqueue("track", track["id"], "admin", {
        "mode": "ingest", "origin_id": origin["id"], "operation": operation,
    })
    job = db.claim()
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    atomic_json(directory / "operation.json", {
        "operation": operation, "action": "ingest", "origin_id": origin["id"],
    })
    selected = {
        "mbid": "11111111-1111-4111-8111-111111111111", "tags": {},
        "title": "Song", "artist": "Artist", "album": "Album",
    }
    plan = db.identity_plan(track["id"], origin["id"], selected, "ingest")
    plan["action"] = "ingest"
    atomic_json(directory / "identity-plan.json", {"operation": operation, "plan": plan})
    final = environment[1] / "admin" / "Artist" / "Album" / "Song.opus"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"already-applied")
    atomic_json(directory / "apply-request.json", {
        "mode": "apply", "track": plan["working_track"], "selected": selected,
        "operation": operation, "overrides": {},
    })
    atomic_json(directory / "apply-result.json", {
        "file_path": str(final), "beets_id": 1, "matched_title": "Artist - Song",
        "mbid": selected["mbid"], "release_id": None, "warnings": [],
    })
    atomic_json(directory / "download-complete.json", {
        "source_url": origin["url"], "downloader_name": "yt-dlp",
        "downloader_version": "test",
    })

    async def forbidden(*args, **kwargs):
        raise AssertionError("recovery must not download again")

    monkeypatch.setattr(pipeline.downloader, "download", forbidden)
    asyncio.run(pipeline.process_track(job))
    current = db.one("SELECT * FROM tracks")
    assert current["file_path"] == str(final)
    receipt = db.operation_receipt(operation)
    assert receipt["finalized_at"]
    assert db.one("SELECT COUNT(*) AS n FROM assets")["n"] == 1

    # Re-running the recovered durable job is idempotent.
    asyncio.run(pipeline.process_track(job))
    assert db.one("SELECT COUNT(*) AS n FROM assets")["n"] == 1


def test_integrity_skips_valid_initial_ingestion_state(db, track, environment, monkeypatch):
    pipeline = Pipeline(db)

    async def audit(job, directory, name, request, timeout=1800):
        return {track["id"]: {"found": False, "path": None, "beets_id": None}}

    monkeypatch.setattr(pipeline, "bridge_request", audit)
    job = {
        "id": 1000, "user_id": "admin", "target": "admin", "kind": "integrity",
        "payload": '{"mode":"verify"}',
    }
    asyncio.run(pipeline.process_integrity(job))
    assert db.integrity_report("admin")["issues"] == []


def test_receipt_retry_clears_postcommit_failure_without_redownload(db, track, environment, monkeypatch):
    current, origin, old_path = activate(db, track, environment)
    operation = "postcommit-operation"
    plan = db.identity_plan(
        current["id"], origin["id"],
        {"mbid": current["mbid"], "tags": {}},
        "redownload",
    )
    new_path = environment[1] / "admin" / "Artist" / "Song-new.opus"
    new_path.write_bytes(b"new-audio")
    stat = new_path.stat()
    db.commit_processed_identity(
        plan,
        {
            "file_path": str(new_path),
            "beets_id": 2,
            "matched_title": "Artist - Song",
            "mbid": current["mbid"],
            "release_id": current["release_id"],
        },
        {
            "path": str(new_path),
            "sha256": "newhash",
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "downloader_name": "yt-dlp",
            "downloader_version": "new",
            "source_url": origin["url"],
        },
        "redownload",
        operation,
    )
    db.fail_operation(current["id"], "redownload", "playlist rewrite failed")
    assert db.one("SELECT operation_state FROM tracks")["operation_state"] == "FAILED"

    pipeline = Pipeline(db)
    rewrites = []

    async def rewrite(track_id):
        rewrites.append(track_id)

    monkeypatch.setattr(pipeline, "rewrite_track_playlists", rewrite)
    directory = environment[0] / "staging" / current["id"]
    directory.mkdir(parents=True)
    receipt = db.operation_receipt(operation)
    job = {"user_id": "admin", "target": current["id"]}
    asyncio.run(pipeline.finish_operation_receipt(job, receipt, directory))

    updated = db.one("SELECT * FROM tracks")
    assert updated["operation_state"] == "IDLE"
    assert updated["operation_error"] is None
    assert updated["health"] == "AVAILABLE"
    assert updated["file_path"] == str(new_path)
    assert new_path.read_bytes() == b"new-audio"
    assert not old_path.exists()
    assert rewrites == [current["id"]]
    assert db.operation_receipt(operation)["finalized_at"]
