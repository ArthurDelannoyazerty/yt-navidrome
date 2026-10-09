import asyncio
import sys
from pathlib import Path

import pytest

import runtime
from common import atomic_json
from runtime import DownloaderRuntime, ProcessError, run_process


def test_subprocess_stdout_and_stderr_are_visible():
    messages = []
    asyncio.run(run_process(
        [sys.executable, "-c", "import sys; print('normal'); print('ERROR broken',file=sys.stderr)"],
        lambda level, message: messages.append((level, message)),
    ))
    assert ("INFO", "normal") in messages
    assert ("ERROR", "ERROR broken") in messages


def test_subprocess_timeout_is_bounded():
    async def task():
        with pytest.raises(asyncio.TimeoutError):
            await run_process(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                lambda *args: None,
                timeout=.05,
            )
    asyncio.run(task())


def test_failed_update_preserves_active_runtime(db, environment, monkeypatch):
    downloader = DownloaderRuntime(db)
    previous = {"python": sys.executable, "version": "old"}
    atomic_json(downloader.active_file, previous)
    monkeypatch.setattr(runtime.shutil, "which", lambda command: "/usr/bin/uv")

    async def fail(*args, **kwargs):
        raise RuntimeError("install failed")

    monkeypatch.setattr(runtime, "run_process", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(downloader.update())
    assert downloader.active() == previous
    assert db.setting("downloader_update")["status"] == "FAILED"
    assert any("install failed" in event["message"] for event in db.rows("SELECT * FROM events"))


def test_atomic_update_and_rollback_no_restart(db, environment, monkeypatch):
    downloader = DownloaderRuntime(db)
    atomic_json(downloader.active_file, {"python": sys.executable, "version": "old"})
    monkeypatch.setattr(runtime.shutil, "which", lambda command: "/usr/bin/uv")
    calls = []

    async def run(args, report, **kwargs):
        calls.append(list(map(str, args)))
        if str(args[1]) == "venv":
            folder = Path(args[-1])
            (folder / "bin").mkdir(parents=True)
            (folder / "bin" / "python").touch()
        return "new-version" if "-c" in args and "__version__" in args[-1] else ""

    monkeypatch.setattr(runtime, "run_process", run)
    asyncio.run(downloader.update())
    assert downloader.active()["version"] == "new-version"
    assert "--prerelease=allow" in calls[1]
    assert "yt-dlp[default]" in calls[1]
    assert not any("uvicorn" in word for command in calls for word in command)
    asyncio.run(downloader.rollback())
    assert downloader.active()["version"] == "old"


def test_invalid_active_pointer_can_be_repaired(db, monkeypatch):
    downloader = DownloaderRuntime(db)
    atomic_json(downloader.active_file, {"python": "/nonexistent/python", "version": "broken"})
    monkeypatch.setattr(runtime.shutil, "which", lambda command: "/usr/bin/uv")

    async def run(args, report, **kwargs):
        if str(args[1]) == "venv":
            file = Path(args[-1]) / "bin" / "python"
            file.parent.mkdir(parents=True)
            file.touch()
        return "new" if "-c" in args and "__version__" in args[-1] else ""

    monkeypatch.setattr(runtime, "run_process", run)
    asyncio.run(downloader.update())
    assert downloader.active()["version"] == "new"



def test_process_error_exposes_subprocess_exit_code():
    with pytest.raises(ProcessError) as exc:
        asyncio.run(run_process(
            [sys.executable, "-c", "import sys; print('WARNING temporary'); sys.exit(75)"],
            lambda *args: None,
        ))
    assert exc.value.returncode == 75
    assert "WARNING temporary" in exc.value.output


def test_youtube_circuit_breaker_defers_pending_downloads(db, monkeypatch):
    from providers import Entry, Snapshot, parse_source

    monkeypatch.setenv("YT_CIRCUIT_FAILURES", "3")
    monkeypatch.setenv("YT_CIRCUIT_WINDOW_SECONDS", "300")
    monkeypatch.setenv("YT_CIRCUIT_COOLDOWN_SECONDS", "900")
    monkeypatch.setenv("YT_CIRCUIT_MAX_COOLDOWN_SECONDS", "3600")
    now = [1800000000.0]
    monkeypatch.setattr(runtime.time, "time", lambda: now[0])

    source = db.add_source(
        parse_source("https://youtube.com/playlist?list=PLbreaker"), "admin"
    )
    entries = [
        Entry(
            key, f"https://youtube.com/watch?v={key}", f"Song {index}",
            "2020-01-01T00:00:00Z", f"entry-{index}", index,
        )
        for index, key in enumerate(("abcdefghijk", "lmnopqrstuv", "12345678901"))
    ]
    db.apply_snapshot(source, Snapshot("Breaker", entries))
    downloader = DownloaderRuntime(db)

    first, paused = downloader._record_download_block("blocked")
    second, _ = downloader._record_download_block("blocked")
    third, paused = downloader._record_download_block("blocked")

    assert not first["until"]
    assert not second["until"]
    assert third["until"] == now[0] + 900
    assert paused == 3
    assert downloader.circuit_status()["open"]
    assert all(
        row["not_before"] >= third["until"]
        for row in db.rows("SELECT not_before FROM jobs WHERE kind='track' AND state='PENDING'")
    )
    assert {
        row["operation_state"] for row in db.rows("SELECT operation_state FROM tracks")
    } == {"DEFERRED"}

    downloader._record_download_success()
    assert not downloader.circuit_status()["open"]
    assert downloader.circuit_status()["failures"] == 0



def test_youtube_circuit_does_not_delay_staged_metadata_retry(db, track, environment):
    origin = db.one("SELECT * FROM track_origins")
    operation = "metadata-only-retry"
    db.enqueue(
        "track", track["id"], "admin",
        {"mode": "ingest", "origin_id": origin["id"], "operation": operation},
    )
    job = db.one(
        "SELECT * FROM jobs WHERE kind='track' AND target=? AND state='PENDING'",
        (track["id"],),
    )
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    (directory / f"temp_{track['id']}.opus").write_bytes(b"candidate")
    atomic_json(
        directory / "operation.json",
        {"operation": operation, "action": "ingest", "origin_id": origin["id"]},
    )
    atomic_json(
        directory / "download-complete.json",
        {"source_url": origin["url"], "downloader_name": "yt-dlp"},
    )

    metadata_retry_at = 1234567890.0
    db.update(
        "jobs", job["id"],
        not_before=metadata_retry_at,
        error="musicbrainz.org temporarily unavailable",
    )
    db.update(
        "tracks", track["id"],
        operation_state="DEFERRED",
        operation_error="musicbrainz.org temporarily unavailable",
    )

    changed = db.defer_pending_youtube_jobs(9999999999.0, "YouTube paused")
    refreshed = db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    refreshed_track = db.one("SELECT * FROM tracks WHERE id=?", (track["id"],))

    assert changed == 0
    assert refreshed["not_before"] == metadata_retry_at
    assert refreshed["error"] == "musicbrainz.org temporarily unavailable"
    assert refreshed_track["operation_state"] == "DEFERRED"
    assert refreshed_track["operation_error"] == "musicbrainz.org temporarily unavailable"
