"""Subprocess supervision and atomic, non-disruptive yt-dlp runtime updates."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path

from common import ROOT, STATE, atomic_json


async def run_process(args, report, *, cwd=None, timeout=1800):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args),
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        limit=1024 * 1024,
    )

    async def collect():
        tail: list[str] = []
        while raw := await proc.stdout.readline():
            line = raw.decode("utf-8", "replace").rstrip()
            tail.append(line)
            tail[:] = tail[-30:]
            level = next(
                (name for name in ("ERROR", "WARNING", "DEBUG", "INFO") if line.startswith(name)),
                "INFO",
            )
            report(level, line)
        code = await proc.wait()
        if code:
            raise RuntimeError(f"Process exited {code}: " + "\n".join(tail[-8:]))
        return "\n".join(tail)

    try:
        return await asyncio.wait_for(collect(), timeout)
    except BaseException:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), 5)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        raise


class DownloaderRuntime:
    def __init__(self, store):
        self.store = store
        self.root = STATE / "runtimes"
        self.active_file = self.root / "active.json"
        self.in_use: set[str] = set()
        self.lock = asyncio.Lock()

    def active(self):
        if self.active_file.exists():
            value = json.loads(self.active_file.read_text())
            if Path(value["python"]).is_file():
                return value
            raise RuntimeError(
                "Active downloader runtime is missing; use Update downloader to repair it"
            )
        return {
            "python": os.getenv("YTDLP_PYTHON", "/opt/ytdlp/bin/python"),
            "version": "bundled",
        }

    async def download(self, url, track_id, directory, report):
        runtime = self.active()
        python = runtime["python"]
        environment = str(Path(python).parent.parent)
        self.in_use.add(environment)
        try:
            await run_process(
                [python, ROOT / "downloader.py", url, track_id],
                report,
                cwd=directory,
            )
            output = directory / f"temp_{track_id}.opus"
            if not output.is_file():
                raise RuntimeError("Downloader exited successfully but no Opus file was produced")
            receipt = {
                "track_id": track_id,
                "python": python,
                "downloader_name": "yt-dlp",
                "downloader_version": runtime.get("version", "unknown"),
                "source_url": url,
            }
            atomic_json(directory / "download-complete.json", receipt)
            return output, receipt
        finally:
            self.in_use.discard(environment)

    async def update(self):
        async with self.lock:
            report = lambda level, text: self.store.event(
                level, text, target="downloader-update"
            )
            self.root.mkdir(parents=True, exist_ok=True)
            folder = self.root / ("yt-dlp-" + uuid.uuid4().hex)
            previous = None
            try:
                try:
                    previous = self.active()
                except (RuntimeError, ValueError, KeyError):
                    report("WARNING", "Replacing an invalid active downloader pointer")
                self.store.set_setting(
                    "downloader_update", {"status": "RUNNING", "at": time.time()}
                )
                report(
                    "INFO",
                    "Installing a fresh nightly yt-dlp runtime; active downloads remain unchanged",
                )
                uv = shutil.which("uv")
                if not uv:
                    raise RuntimeError("uv is required for isolated downloader updates")
                await run_process(
                    [uv, "venv", "--python", sys._base_executable, folder],
                    report,
                    timeout=120,
                )
                python = folder / "bin" / "python"
                await run_process(
                    [
                        uv,
                        "pip",
                        "install",
                        "--python",
                        python,
                        "--prerelease=allow",
                        "--upgrade",
                        "yt-dlp[default]",
                        "tenacity>=9,<10",
                    ],
                    report,
                    timeout=600,
                )
                version = await run_process(
                    [
                        python,
                        "-c",
                        "import yt_dlp,tenacity; from yt_dlp.version import __version__; print(__version__)",
                    ],
                    report,
                    timeout=30,
                )
                await run_process(
                    [
                        python,
                        "-c",
                        f"import runpy; runpy.run_path({str(ROOT / 'downloader.py')!r}, run_name='smoke')",
                    ],
                    report,
                    timeout=30,
                )
                active = {
                    "python": str(python),
                    "version": version.strip(),
                    "installed_at": time.time(),
                }
                if previous:
                    atomic_json(self.root / "previous.json", previous)
                atomic_json(self.active_file, active)
                self.store.set_setting("downloader_update", {"status": "OK", **active})
                report(
                    "INFO",
                    f"Downloader activated: {version.strip()}. API restart not required",
                )
            except Exception as exc:
                shutil.rmtree(folder, ignore_errors=True)
                self.store.set_setting(
                    "downloader_update",
                    {"status": "FAILED", "error": str(exc), "at": time.time()},
                )
                report(
                    "ERROR",
                    f"Downloader update failed; previous runtime retained: {exc}",
                )
                raise
            keep = self.in_use | {str(folder)}
            if previous:
                keep.add(str(Path(previous["python"]).parent.parent))
            for old in self.root.glob("yt-dlp-*"):
                if str(old) not in keep:
                    shutil.rmtree(old, ignore_errors=True)

    async def rollback(self):
        async with self.lock:
            previous_file = self.root / "previous.json"
            if not previous_file.is_file():
                raise ValueError("No previous downloader runtime is available")
            previous = json.loads(previous_file.read_text())
            if not Path(previous["python"]).is_file():
                raise ValueError(
                    "The previous runtime is missing; install a fresh update instead"
                )
            current = self.active()
            atomic_json(self.active_file, previous)
            atomic_json(previous_file, current)
            self.store.event(
                "WARNING",
                f"Downloader rolled back to {previous['version']}; active downloads unchanged",
            )
            self.store.set_setting(
                "downloader_update", {"status": "ROLLED_BACK", **previous}
            )
            return previous
