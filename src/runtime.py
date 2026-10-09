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


class ProcessError(RuntimeError):
    def __init__(self, returncode: int, output: list[str]):
        self.returncode = returncode
        self.output = list(output)
        super().__init__(f"Process exited {returncode}: " + "\n".join(self.output[-8:]))


class DeferredOperation(RuntimeError):
    def __init__(self, message: str, *, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


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
            raise ProcessError(code, tail)
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

    @staticmethod
    def _env_number(name, default, cast=float):
        raw = (os.getenv(name) or "").split("#", 1)[0].strip()
        try:
            return cast(raw) if raw else default
        except (TypeError, ValueError):
            return default

    def _circuit_failure_times(self, state, now, window):
        raw = state.get("failure_times")
        if isinstance(raw, list):
            values = raw
        else:
            # Compatibility with the first v2 circuit state shape.
            legacy_count = max(0, int(state.get("failures") or 0))
            legacy_at = float(state.get("last_failure_at") or 0)
            values = [legacy_at] * legacy_count if legacy_at else []
        result = []
        for value in values:
            try:
                timestamp = float(value)
            except (TypeError, ValueError):
                continue
            if 0 <= now - timestamp <= window:
                result.append(timestamp)
        return result

    def circuit_status(self):
        now = time.time()
        state = dict(self.store.setting("youtube_download_circuit", {}) or {})
        window = max(30.0, self._env_number("YT_CIRCUIT_WINDOW_SECONDS", 300.0))
        failures = self._circuit_failure_times(state, now, window)
        until = float(state.get("until") or 0)
        state["failure_times"] = failures
        state["failures"] = len(failures)
        state["until"] = until
        state["open"] = until > now
        state.setdefault("opens", 0)
        return state

    def _record_download_block(self, reason: str):
        now = time.time()
        previous = dict(self.store.setting("youtube_download_circuit", {}) or {})
        window = max(30.0, self._env_number("YT_CIRCUIT_WINDOW_SECONDS", 300.0))
        threshold = max(1, self._env_number("YT_CIRCUIT_FAILURES", 3, int))
        base = max(60.0, self._env_number("YT_CIRCUIT_COOLDOWN_SECONDS", 900.0))
        maximum = max(base, self._env_number("YT_CIRCUIT_MAX_COOLDOWN_SECONDS", 3600.0))

        failure_times = self._circuit_failure_times(previous, now, window)
        failure_times.append(now)
        failures = len(failure_times)
        opens = int(previous.get("opens") or 0)
        previous_until = float(previous.get("until") or 0)
        until = previous_until if previous_until > now else 0.0
        if failures >= threshold and not until:
            opens += 1
            until = now + min(maximum, base * (2 ** min(opens - 1, 4)))

        state = {
            "failure_times": failure_times,
            "failures": failures,
            "opens": opens,
            "last_failure_at": now,
            "until": until,
            "reason": reason,
            "last_success_at": previous.get("last_success_at"),
        }
        self.store.set_setting("youtube_download_circuit", state)
        paused = 0
        if until and until != previous_until:
            paused = self.store.defer_pending_youtube_jobs(until, reason)
        return state, paused

    def _record_download_success(self):
        now = time.time()
        current = dict(self.store.setting("youtube_download_circuit", {}) or {})
        window = max(30.0, self._env_number("YT_CIRCUIT_WINDOW_SECONDS", 300.0))
        failures = self._circuit_failure_times(current, now, window)
        until = float(current.get("until") or 0)
        opens = int(current.get("opens") or 0)
        if not failures and until <= now:
            opens = 0
        current.update(
            {
                "failure_times": failures,
                "failures": len(failures),
                "opens": opens,
                "until": until,
                "last_success_at": now,
            }
        )
        self.store.set_setting("youtube_download_circuit", current)

    async def download(self, url, track_id, directory, report):
        circuit = self.circuit_status()
        if circuit["open"]:
            raise DeferredOperation(
                "YouTube downloads are temporarily paused after repeated bot/403 blocks.",
                retry_at=circuit["until"],
            )

        runtime = self.active()
        python = runtime["python"]
        environment = str(Path(python).parent.parent)
        self.in_use.add(environment)
        try:
            try:
                await run_process(
                    [python, ROOT / "downloader.py", url, track_id],
                    report,
                    cwd=directory,
                )
            except ProcessError as exc:
                if exc.returncode == 20:
                    reason = (
                        "YouTube temporarily blocked media downloads "
                        "(bot verification, HTTP 403/429, or equivalent playback restriction)."
                    )
                    state, paused = self._record_download_block(reason)
                    if state["until"]:
                        report(
                            "WARNING",
                            f"YouTube circuit breaker opened; paused {paused} queued "
                            f"download(s) until {state['until']:.0f}.",
                        )
                        retry_at = state["until"]
                    else:
                        retry_at = time.time() + 120
                        report(
                            "WARNING",
                            f"YouTube download blocked ({state['failures']} recent failure(s)); "
                            "this track will retry later.",
                        )
                    raise DeferredOperation(reason, retry_at=retry_at) from exc
                if exc.returncode == 21:
                    raise RuntimeError("Video unavailable, private, or removed.") from exc
                if exc.returncode == 22:
                    raise DeferredOperation(
                        "YouTube download failed with a transient network error.",
                        retry_at=time.time() + 60,
                    ) from exc
                raise

            output = directory / f"temp_{track_id}.opus"
            if not output.is_file():
                raise RuntimeError("Downloader exited successfully but no Opus file was produced")
            self._record_download_success()
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
