import asyncio
import sys
from pathlib import Path

import pytest

import runtime
from common import atomic_json
from runtime import DownloaderRuntime, run_process


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
