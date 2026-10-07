"""One durable worker replaces background-task fan-out and duplicated state machines."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from common import LIBRARY, ROOT, STATE, atomic_json, atomic_text, next_nightly, safe_name, utcnow
from http_policy import HttpPolicy
from providers import PROVIDERS
from runtime import DownloaderRuntime, run_process


def write_playlist(store, source):
    if not source["is_playlist"]:
        return
    user_root = (LIBRARY / source["user_id"]).resolve()
    folder = user_root / "000000-playlists"
    destination = folder / f"{safe_name(source['title'])}--{source['id'][:8]}.m3u"
    order = "m.position,m.entry_id" if os.getenv("PLAYLIST_ORDER", "source") == "source" else "m.added_at,m.position,m.entry_id"
    tracks = store.rows(f"""SELECT t.file_path,t.title,t.matched_title FROM memberships m
      JOIN tracks t ON t.id=m.track_id WHERE m.source_id=? AND t.user_id=?
      AND t.file_path IS NOT NULL ORDER BY {order}""", (source["id"], source["user_id"]))
    lines = ["#EXTM3U"]
    for track in tracks:
        path = Path(track["file_path"]).resolve()
        if not path.is_relative_to(user_root):
            raise ValueError("Refusing a playlist path outside this user's library")
        if path.is_file():
            title = (track["matched_title"] or track["title"]).replace("\n", " ").replace("\r", " ")
            lines += [f"#EXTINF:-1,{title}", os.path.relpath(path, folder).replace(os.sep, "/")]
    atomic_text(destination, "\n".join(lines) + "\n", mode=0o644)
    if source.get("playlist_path") and source["playlist_path"] != str(destination):
        old = Path(source["playlist_path"]).resolve()
        if old.parent == folder:
            old.unlink(missing_ok=True)
    store.update("sources", source["id"], playlist_path=str(destination))


class Pipeline:
    def __init__(self, store):
        self.store = store
        self.downloader = DownloaderRuntime(store)
        self.stopping = asyncio.Event()
        self.children = set()

    def report(self, job):
        return lambda level, message: self.store.event(level, message, job["user_id"], job["target"])

    async def bridge(self, job, track, directory, mode, **kwargs):
        request = directory / (mode + "-request.json")
        result = directory / (mode + "-result.json")
        result.unlink(missing_ok=True)
        atomic_json(request, {"track": track, "mode": mode, **kwargs})
        await run_process([sys.executable, ROOT / "beets_bridge.py", request, result], self.report(job), timeout=1800)
        if not result.exists():
            raise RuntimeError("Metadata worker returned no result")
        return json.loads(result.read_text())

    async def process_track(self, job):
        payload = json.loads(job["payload"])
        mode = payload.get("mode", "auto")
        operation = payload.get("operation", str(job["id"]))
        track = self.store.owned("tracks", job["target"], job["user_id"])
        self.store.update("tracks", track["id"], status="PROCESSING", error=None)
        directory = STATE / "staging" / track["id"]
        directory.mkdir(parents=True, exist_ok=True)
        plan_file = directory / "operation.json"
        plan = json.loads(plan_file.read_text()) if plan_file.exists() else {}
        first_attempt = plan.get("operation") != operation
        old_value = track.get("file_path") if first_attempt else plan.get("previous_path")
        old = Path(old_value) if old_value else None
        selected = json.loads(track["selected"]) if track.get("selected") else None
        path = Path(track["temp_path"]) if track.get("temp_path") else None
        if first_attempt:
            plan = {"operation": operation, "previous_path": old_value}
            for name in ("apply-request.json", "apply-result.json"):
                (directory / name).unlink(missing_ok=True)
            if mode == "redownload":
                if path and path.resolve().is_relative_to(directory.resolve()):
                    path.unlink(missing_ok=True)
                (directory / f"temp_{track['id']}.opus").unlink(missing_ok=True)
                (directory / "download-complete.json").unlink(missing_ok=True)
                path, selected = None, None
                self.store.update("tracks", track["id"], temp_path=None, selected=None)
            if mode in {"retag", "reidentify", "adopt"}:
                if not old or not old.is_file():
                    raise ValueError("No existing audio file to retag")
                path = directory / "retag.opus"
                await asyncio.to_thread(shutil.copy2, old, path)
                selected = None if mode == "reidentify" else {"asis": True}
                self.store.update("tracks", track["id"], temp_path=str(path),
                                  selected=json.dumps(selected) if selected else None)
            atomic_json(plan_file, plan)
        # Resume a completed beets move even if the server died before committing
        # its result to the application ledger. The operation token prevents an
        # intentional new redownload from adopting a previous operation's result.
        result = None
        if (directory / "apply-request.json").is_file():
            if (directory / "apply-result.json").is_file():
                result = json.loads((directory / "apply-result.json").read_text())
            else:
                recovered = await self.bridge(job, track, directory, "inspect", path="", operation=operation)
                if recovered.pop("found"):
                    complete = recovered.pop("complete")
                    if complete:
                        result = recovered
                    else:
                        path = Path(recovered["file_path"])
        if result is None and mode == "comment" and old and old.is_file():
            result = await self.bridge(job, track, directory, "comment", path=str(old))
        if result is None:
            if not path or not path.is_file():
                # Reuse only a file for which the downloader exited successfully.
                # A partial Opus output left by a killed FFmpeg is not a receipt.
                path = directory / f"temp_{track['id']}.opus"
                if not (directory / "download-complete.json").is_file() or not path.is_file():
                    path.unlink(missing_ok=True)
                    path = await self.downloader.download(track["url"], track["id"], directory, self.report(job))
                if not path.is_file():
                    raise RuntimeError("Downloader finished but produced no Opus file")
                self.store.update("tracks", track["id"], temp_path=str(path))
            if selected is None:
                identified = await self.bridge(job, track, directory, "identify", path=str(path))
                self.store.update("tracks", track["id"], choices=json.dumps(identified["choices"]))
                if not identified["automatic"] or not identified["choices"]:
                    self.store.update("tracks", track["id"], status="NEEDS_APPROVAL")
                    self.report(job)("INFO", f"Metadata needs approval (beets: {identified['recommendation']})")
                    return
                selected = identified["choices"][0]
                self.store.update("tracks", track["id"], selected=json.dumps(selected))
            result = await self.bridge(job, track, directory, "apply", path=str(path), selected=selected,
                                       operation=operation, overrides=payload.get("overrides", {}))
        user_root = (LIBRARY / track["user_id"]).resolve()
        final = Path(result["file_path"]).resolve()
        if not final.is_relative_to(user_root) or not final.is_file():
            raise RuntimeError("Metadata worker returned an invalid destination")
        warnings = result.pop("warnings", [])
        self.store.update("tracks", track["id"], **result, status="COMPLETED", temp_path=None,
                          error="; ".join(warnings) or None)
        for warning in warnings:
            self.report(job)("WARNING", warning)
        for source in self.store.rows("""SELECT DISTINCT s.* FROM sources s JOIN memberships m
            ON s.id=m.source_id WHERE m.track_id=?""", (track["id"],)):
            await asyncio.to_thread(write_playlist, self.store, source)
        if old and old.resolve() != final and old.resolve().is_relative_to(user_root):
            old.unlink(missing_ok=True)
        # Remove work files only after the library and every playlist are committed.
        shutil.rmtree(directory, ignore_errors=True)
        self.report(job)("INFO", f"Saved: {result['matched_title']}")

    async def process_source(self, job):
        source = self.store.owned("sources", job["target"], job["user_id"])
        self.store.update("sources", source["id"], status="SYNCING", error=None)
        http = HttpPolicy(self.store, self.report(job))
        snapshot = await asyncio.to_thread(PROVIDERS[source["provider"]].resolve, source, http)
        self.store.apply_snapshot(source, snapshot)
        source = self.store.owned("sources", source["id"], source["user_id"])
        await asyncio.to_thread(write_playlist, self.store, source)
        self.report(job)("INFO", f"Source synchronized: {snapshot.title} ({len(snapshot.entries)} entries)")

    async def work(self):
        while not self.stopping.is_set():
            job = self.store.claim()
            if not job:
                await asyncio.sleep(0.5)
                continue
            try:
                if job["kind"] == "sync":
                    await self.process_source(job)
                elif job["kind"] == "track":
                    await self.process_track(job)
                else:
                    raise ValueError("Unknown job type")
                self.store.update("jobs", job["id"], state="DONE", finished_at=utcnow())
            except asyncio.CancelledError:
                # Startup recovery will retry this durable job, not lose it.
                raise
            except Exception as exc:
                self.report(job)("ERROR", traceback.format_exc())
                self.store.update("jobs", job["id"], state="FAILED", error=str(exc), finished_at=utcnow())
                table = "sources" if job["kind"] == "sync" else "tracks"
                self.store.update(table, job["target"], status="FAILED", error=str(exc))
            finally:
                if job["kind"] == "sync":
                    delay = max(0.1, float(os.getenv("SYNC_INTERVAL_HOURS", "6"))) * 3600
                    self.store.update("sources", job["target"], next_sync=time.time() + delay)
            # Extra boundary spacing protects libraries that have their own per-process limiter.
            await asyncio.sleep(1.1)

    async def update_downloader(self):
        try:
            await self.downloader.update()
        except Exception:
            pass  # The runtime updater already persists and displays the full failure.

    def start_update(self):
        if self.downloader.lock.locked() or any(not t.done() for t in self.children):
            raise ValueError("A downloader update is already queued or running")
        task = asyncio.create_task(self.update_downloader())
        self.children.add(task)
        task.add_done_callback(self.children.discard)

    async def schedule(self):
        while not self.stopping.is_set():
            try:
                now = time.time()
                if float(os.getenv("SYNC_INTERVAL_HOURS", "6")) > 0:
                    for source in self.store.rows("SELECT * FROM sources WHERE monitored=1 AND next_sync<=?", (now,)):
                        self.store.enqueue("sync", source["id"], source["user_id"])
                if os.getenv("YTDLP_UPDATE_ENABLED", "true").lower() == "true":
                    next_at = self.store.setting("next_downloader_update")
                    if next_at is None or next_at <= now:
                        future = next_nightly(datetime.now(UTC), os.getenv("YTDLP_UPDATE_TIME", "04:00"),
                                              os.getenv("TZ", "Europe/Paris"))
                        self.store.set_setting("next_downloader_update", future.timestamp())
                        if next_at is not None:
                            self.start_update()
                self.store.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-20000 FROM events)")
                self.store.execute("DELETE FROM jobs WHERE state='DONE' AND id < (SELECT COALESCE(MAX(id),0)-2000 FROM jobs)")
            except Exception:
                self.store.event("ERROR", traceback.format_exc(), target="scheduler")
            await asyncio.sleep(30)
