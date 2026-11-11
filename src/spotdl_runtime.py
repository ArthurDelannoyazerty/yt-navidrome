"""Isolated Spotify scanning/search runtime; audio stays in the yt-dlp pipeline."""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from common import ROOT, atomic_json
from http_policy import ApiDeferred, HttpPolicy
from runtime import DeferredOperation, ProcessError, run_process


class SpotDLRuntime:
    """Pin each operation to its interpreter while updates switch future work."""

    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / "runtimes" / "spotdl"
        self.active_file = self.root / "active.json"
        self.lock = asyncio.Lock()
        self.in_use: dict[str, int] = {}

    def active(self):
        if self.active_file.is_file():
            value = json.loads(self.active_file.read_text())
            if (not isinstance(value, dict) or not isinstance(value.get("python"), str)
                    or not value["python"] or not Path(value["python"]).is_file()
                    or not isinstance(value.get("version"), str) or not value["version"]):
                raise RuntimeError("Active spotDL runtime is missing; use Maintenance → Update spotDL to repair it")
            return value
        return {"python": os.getenv("SPOTDL_PYTHON", "/opt/spotdl/bin/python"),
                "version": "4.6.0 (bundled)"}

    async def _request(self, mode, url, report, *, runtime=None):
        if mode != "smoke":
            cooldown = self.store.setting(f"spotdl_{mode}_cooldown", 0)
            if cooldown > time.time():
                raise DeferredOperation("Waiting for the shared Spotify provider cooldown", retry_at=cooldown)
        runtime = runtime or self.active()
        python = runtime["python"]
        if not Path(python).is_file():
            raise RuntimeError("spotDL is not installed. Use Maintenance → Update spotDL or rebuild the container")
        environment = str(Path(python).parent.parent)
        self.in_use[environment] = self.in_use.get(environment, 0) + 1
        directory = self.root / "operations" / uuid.uuid4().hex
        output = directory / "result.json"
        args = [python, ROOT / "spotdl_bridge.py", mode, "--output", output]
        if url:
            args += ["--url", url]
        failure = None
        try:
            directory.mkdir(parents=True)
            try:
                await run_process(args, report, cwd=directory,
                                  timeout=30 if mode == "smoke" else 600)
            except ProcessError as exc:
                failure = exc
            except asyncio.TimeoutError as exc:
                until = time.time() + 120
                self.store.set_setting(f"spotdl_{mode}_cooldown", until)
                raise DeferredOperation("Spotify request timed out; previous playlist contents were retained",
                                        retry_at=until) from exc
            if not output.is_file():
                raise RuntimeError("spotDL returned no result; update spotDL in Maintenance and retry") from failure
            if output.stat().st_size > 16 * 1024 * 1024:
                raise RuntimeError("spotDL returned an oversized result; previous playlist contents were retained")
            try:
                data = json.loads(output.read_text())
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("spotDL returned invalid JSON; update spotDL in Maintenance and retry") from exc
            if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("mode") != mode:
                raise RuntimeError("Unsupported spotDL bridge response; previous playlist contents were retained")
            if data.get("ok") is not True:
                message = str(data.get("error") or "Spotify request failed")[:2000]
                if data.get("retryable") is True:
                    retry_after = data.get("retry_after", 120)
                    if not isinstance(retry_after, (float, int)) or not math.isfinite(retry_after):
                        retry_after = 120
                    until = time.time() + max(120, min(retry_after, 86400))
                    self.store.set_setting(f"spotdl_{mode}_cooldown", until)
                    raise DeferredOperation(message, retry_at=until)
                raise ValueError(message)
            if failure:
                raise RuntimeError("spotDL exited unsuccessfully; its result was not accepted") from failure
            return data
        finally:
            remaining = self.in_use[environment] - 1
            if remaining:
                self.in_use[environment] = remaining
            else:
                self.in_use.pop(environment)
            shutil.rmtree(directory, ignore_errors=True)

    async def scan(self, source, report):
        from spotify_auth import SpotifyAuth
        from spotify_provider import Spotify

        # Public URL scanning is the default; a configured connection can still
        # read private/owned sources when the public endpoint denies access.
        try:
            data = await self._request("scan", source["url"], report)
            return Spotify.public_snapshot(source, data, report)
        except (ValueError, RuntimeError, ApiDeferred) as public_error:
            if not SpotifyAuth(self.store).status(source["user_id"])["connected"]:
                raise
            report("WARNING", "Public Spotify scan unavailable; trying this library's optional Spotify connection")
            try:
                result = await asyncio.to_thread(Spotify().resolve, source, HttpPolicy(self.store, report))
            except (ValueError, RuntimeError, ApiDeferred) as auth_error:
                report("WARNING", f"Connected Spotify scan also failed: {auth_error}")
                raise public_error from auth_error
            return replace(result, entries=[replace(entry, resolve_audio=True) for entry in result.entries])

    async def resolve_audio(self, url, report):
        from providers import parse_source

        source = parse_source(url)
        if source.provider != "spotify" or source.is_playlist:
            raise ValueError("Audio matching requires a single Spotify track")
        data = await self._request("resolve", source.url, report)
        if data.get("spotify_url") != source.url:
            raise ValueError("spotDL returned a different Spotify track; result rejected")
        target = parse_source(data.get("youtube_url", ""))
        if target.provider != "youtube" or target.is_playlist:
            raise ValueError("spotDL did not return a single YouTube audio source")
        artists = data.get("artists")
        return {"url": target.url, "title": data.get("title"),
                "channel": ", ".join(artists) if isinstance(artists, list) and all(isinstance(a, str) for a in artists) else None,
                "duration": data.get("duration"), "version": data.get("version", self.active().get("version"))}

    async def update(self):
        async with self.lock:
            report = lambda level, message: self.store.event(level, message, target="spotdl-update")
            self.root.mkdir(parents=True, exist_ok=True)
            folder = self.root / ("spotdl-" + uuid.uuid4().hex)
            previous = None
            activated = False
            try:
                try:
                    previous = self.active()
                    if not Path(previous["python"]).is_file():
                        previous = None
                except (ValueError, RuntimeError, KeyError):
                    report("WARNING", "Replacing an invalid active spotDL pointer")
                self.store.set_setting("spotdl_update", {"status": "RUNNING", "at": time.time()})
                report("INFO", "Installing a fresh spotDL runtime; active operations keep their current runtime")
                uv = shutil.which("uv")
                if not uv:
                    raise RuntimeError("uv is required for isolated spotDL updates")
                await run_process([uv, "venv", "--python", sys._base_executable, folder], report, timeout=120)
                python = folder / "bin" / "python"
                await run_process([uv, "pip", "install", "--python", python, "--upgrade", "spotdl>=4.6.0,<5"],
                                  report, timeout=600)
                smoke = await self._request("smoke", None, report, runtime={"python": str(python)})
                version = smoke.get("version")
                if not isinstance(version, str) or not version:
                    raise RuntimeError("spotDL smoke check did not return a version")
                active = {"python": str(python), "version": version, "installed_at": time.time()}
                if previous:
                    atomic_json(self.root / "previous.json", previous)
                atomic_json(self.active_file, active)
                activated = True
                self.store.set_setting("spotdl_update", {"status": "OK", **active})
                report("INFO", f"spotDL activated: {version}. API restart not required")
            except BaseException as exc:
                # Cancellation has the same atomicity guarantee as a failed install.
                if not activated:
                    shutil.rmtree(folder, ignore_errors=True)
                self.store.set_setting("spotdl_update", {"status": "FAILED", "error": str(exc) or "Update cancelled", "at": time.time()})
                report("ERROR", f"spotDL update failed; previous runtime retained: {exc}")
                raise
            keep = set(self.in_use) | {str(folder)}
            if previous:
                keep.add(str(Path(previous["python"]).parent.parent))
            for old in self.root.glob("spotdl-*"):
                if old.is_dir() and str(old) not in keep:
                    shutil.rmtree(old, ignore_errors=True)

    async def rollback(self):
        async with self.lock:
            previous_file = self.root / "previous.json"
            if not previous_file.is_file():
                raise ValueError("No previous spotDL runtime is available")
            previous = json.loads(previous_file.read_text())
            if (not isinstance(previous, dict) or not isinstance(previous.get("python"), str)
                    or not previous["python"] or not Path(previous["python"]).is_file()
                    or not isinstance(previous.get("version"), str) or not previous["version"]):
                raise ValueError("Previous spotDL runtime is missing; install a fresh update instead")
            current = self.active()
            atomic_json(self.active_file, previous)
            atomic_json(previous_file, current)
            self.store.set_setting("spotdl_update", {"status": "ROLLED_BACK", **previous})
            self.store.event("WARNING", f"spotDL rolled back to {previous['version']}; active operations unchanged", target="spotdl-update")
            return previous
