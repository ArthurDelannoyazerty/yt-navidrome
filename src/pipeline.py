"""Durable provider-aware ingestion, replacement and integrity workflows."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from audio_tags import inspect_tags
from common import (
    LIBRARY, ROOT, STATE, atomic_copy, atomic_json, atomic_text,
    discovery_comment, next_nightly, safe_name, sha256_file, utcnow,
)
from http_policy import ApiDeferred, HttpPolicy
from providers import PROVIDERS
from runtime import DeferredOperation, DownloaderRuntime, ProcessError, run_process


class ReplacementIdentityMismatch(RuntimeError):
    pass


def write_playlist(store, source):
    if not source["is_playlist"]:
        return
    user_root = (LIBRARY / source["user_id"]).resolve()
    folder = user_root / "000000-playlists"
    destination = folder / f"{safe_name(source['title'])}--{source['id'][:8]}.m3u"
    order = "m.position,m.entry_id" if os.getenv("PLAYLIST_ORDER", "source") == "source" else "m.added_at,m.position,m.entry_id"
    tracks = store.rows(
        f"""SELECT a.path,t.title,t.matched_title FROM memberships m
            JOIN track_origins o ON o.id=m.origin_id JOIN tracks t ON t.id=o.track_id
            JOIN assets a ON a.id=t.current_asset_id AND a.state='CURRENT'
            WHERE m.source_id=? AND t.user_id=? ORDER BY {order}""",
        (source["id"], source["user_id"]),
    )
    lines = ["#EXTM3U"]
    for track in tracks:
        path = Path(track["path"]).resolve()
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
        self.children: set[asyncio.Task] = set()

    def report(self, job):
        return lambda level, message: self.store.event(level, message, job["user_id"], job["target"])

    async def bridge_request(self, job, directory: Path, name: str, request: dict, timeout=1800):
        request_path = directory / f"{name}-request.json"
        result_path = directory / f"{name}-result.json"
        result_path.unlink(missing_ok=True)
        atomic_json(request_path, request)
        try:
            await run_process(
                [sys.executable, ROOT / "beets_bridge.py", request_path, result_path],
                self.report(job), timeout=timeout,
            )
        except ProcessError as exc:
            if exc.returncode != 75:
                raise
            marker = "WARNING DEFERRED "
            payload = None
            for line in reversed(exc.output):
                if line.startswith(marker):
                    try:
                        payload = json.loads(line[len(marker):])
                    except (TypeError, ValueError):
                        payload = None
                    break
            message = payload.get("message") if isinstance(payload, dict) and payload.get("message") else "Metadata provider temporarily unavailable"
            retry_at = payload.get("retry_at") if isinstance(payload, dict) else None
            raise DeferredOperation(message, retry_at=retry_at) from exc
        if not result_path.exists():
            raise RuntimeError("Metadata worker returned no result")
        return json.loads(result_path.read_text())

    async def bridge(self, job, track, directory, mode, **kwargs):
        return await self.bridge_request(job, directory, mode,
                                         {"track": track, "user": track["user_id"], "mode": mode, **kwargs})

    async def metadata_for_recording(self, job, track, directory, mbid):
        return await self.bridge_request(job, directory, "metadata",
                                         {"mode": "metadata", "user": track["user_id"], "mbid": mbid})

    def _sources_for_track(self, track_id: str):
        return self.store.rows(
            """SELECT DISTINCT s.* FROM sources s JOIN memberships m ON m.source_id=s.id
               JOIN track_origins o ON o.id=m.origin_id WHERE o.track_id=?""", (track_id,),
        )

    async def rewrite_track_playlists(self, track_id: str):
        for source in self._sources_for_track(track_id):
            await asyncio.to_thread(write_playlist, self.store, source)

    @staticmethod
    def _backup_sqlite(source: Path, destination: Path):
        if not source.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(source)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    @staticmethod
    def _restore_sqlite(source: Path, destination: Path):
        if not source.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(source)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    async def backup_current(self, track: dict, directory: Path):
        path = Path(track["file_path"]).resolve() if track.get("file_path") else None
        if not path or not path.is_file():
            return None
        backup_file = directory / "previous-current.opus"
        await asyncio.to_thread(shutil.copy2, path, backup_file)
        beets_db = STATE / "beets" / track["user_id"] / "library.db"
        beets_backup = directory / "beets-before.sqlite"
        await asyncio.to_thread(self._backup_sqlite, beets_db, beets_backup)
        return {"path": str(path), "file": str(backup_file),
                "beets_db": str(beets_db), "beets_backup": str(beets_backup)}

    async def restore_current(self, backup):
        if not backup:
            return
        backup_file = Path(backup["file"])
        destination = Path(backup["path"])
        if backup_file.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, backup_file, destination)
        await asyncio.to_thread(self._restore_sqlite, Path(backup["beets_backup"]), Path(backup["beets_db"]))

    async def refresh_asset_fingerprint(self, track_id: str):
        asset = self.store.current_asset(track_id)
        if not asset:
            raise ValueError("No current asset exists to refresh")
        path = Path(asset["path"])
        before = path.stat()
        digest = await asyncio.to_thread(sha256_file, path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Audio changed while its fingerprint was being updated")
        self.store.update("assets", asset["id"], sha256=digest,
                          size_bytes=after.st_size, mtime_ns=after.st_mtime_ns)

    async def usable_asset(self, track: dict, asset: dict | None) -> bool:
        if not asset:
            return False
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to((LIBRARY / track["user_id"]).resolve()):
            return False
        try:
            if not path.is_file() or path.stat().st_size == 0:
                return False
            digest = await asyncio.to_thread(sha256_file, path)
            return bool(asset.get("sha256") and digest == asset["sha256"])
        except OSError:
            return False

    async def finish_delete(self, job, receipt, directory):
        """Retryable post-commit cleanup; never unlink a newly reused asset path."""
        result = receipt["result"]
        directory.mkdir(parents=True, exist_ok=True)
        try:
            await self.bridge(job, result["track"], directory, "forget")
            root = (LIBRARY / receipt["user_id"]).resolve()
            for recorded in result.get("paths", []):
                path = Path(recorded).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Refusing deletion outside this user's library")
                reused = self.store.one("SELECT 1 FROM assets WHERE path=? AND state='CURRENT'", (str(path),))
                if not reused:
                    path.unlink(missing_ok=True)
            for source_id in result.get("sources", []):
                source = self.store.one("SELECT * FROM sources WHERE id=? AND user_id=?", (source_id, receipt["user_id"]))
                if source:
                    await asyncio.to_thread(write_playlist, self.store, source)
            self.store.finalize_receipt(receipt["operation_id"])
            shutil.rmtree(directory, ignore_errors=True)
        except Exception as exc:
            # The track row is gone. Keep cleanup in the durable queue rather
            # than creating an inaccessible FAILED track action.
            raise DeferredOperation(f"Deletion committed; cleanup will retry: {exc}") from exc
        return result

    async def finish_operation_receipt(self, job, receipt: dict, directory: Path):
        """Finish idempotent filesystem/playlist work after a transactional commit."""
        if receipt.get("finalized_at"):
            shutil.rmtree(directory, ignore_errors=True)
            return receipt["result"]
        result = receipt["result"]
        if result.get("kind") == "DELETED" and result.get("track"):
            return await self.finish_delete(job, receipt, directory)
        target_id = result.get("target_id")
        source_id = result.get("source_id")
        if target_id:
            await self.rewrite_track_playlists(target_id)
        if source_id and source_id != target_id:
            await self.rewrite_track_playlists(source_id)
        if result.get("kind") == "DEDUPLICATED" and target_id:
            current = self.store.one("SELECT * FROM tracks WHERE id=?", (target_id,))
            if current and current.get("file_path") and Path(current["file_path"]).is_file():
                comment_dir = STATE / "staging" / f"comment-{target_id}"
                comment_dir.mkdir(parents=True, exist_ok=True)
                await self.bridge(job, current, comment_dir, "comment", path=current["file_path"])
                await self.refresh_asset_fingerprint(target_id)
                shutil.rmtree(comment_dir, ignore_errors=True)
        old_assets = list(result.get("old_assets") or [])
        if result.get("old_asset"):
            old_assets.append(result["old_asset"])
        new_path = Path(result["new_path"]).resolve() if result.get("new_path") else None
        user_root = (LIBRARY / receipt["user_id"]).resolve()
        seen_old_paths: set[Path] = set()
        for old_asset in old_assets:
            if not old_asset or not old_asset.get("path"):
                continue
            old_path = Path(old_asset["path"]).resolve()
            if old_path in seen_old_paths:
                continue
            seen_old_paths.add(old_path)
            if old_path != new_path and old_path.is_relative_to(user_root):
                old_path.unlink(missing_ok=True)
        if result.get("source_orphan") and source_id:
            await self.cleanup_orphan_track(job, source_id)
        elif source_id and source_id != target_id:
            if self.store.one("SELECT id FROM tracks WHERE id=?", (source_id,)):
                self.store.complete_operation(source_id)
        if target_id and self.store.one("SELECT id FROM tracks WHERE id=?", (target_id,)):
            asset = self.store.current_asset(target_id)
            health = "AVAILABLE" if asset and Path(asset["path"]).is_file() else "MISSING"
            self.store.complete_operation(target_id, health=health)
        self.store.finalize_receipt(receipt["operation_id"])
        shutil.rmtree(directory, ignore_errors=True)
        return result

    async def activate_result(self, job, plan: dict, result: dict, receipt: dict,
                              operation: str, directory: Path):
        result = dict(result)
        final = Path(result["file_path"]).resolve()
        user_root = (LIBRARY / plan["working_track"]["user_id"]).resolve()
        if not final.is_relative_to(user_root) or not final.is_file():
            raise RuntimeError("Metadata worker returned an invalid destination")
        stat = final.stat()
        asset = {
            "path": str(final), "sha256": await asyncio.to_thread(sha256_file, final),
            "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "downloader_name": receipt.get("downloader_name"),
            "downloader_version": receipt.get("downloader_version"), "source_url": plan["origin"]["url"],
        }
        warnings = result.pop("warnings", [])
        committed = self.store.commit_processed_identity(plan, result, asset, plan.get("action") or "process", operation)
        for warning in warnings:
            self.report(job)("WARNING", warning)
        await self.finish_operation_receipt(job, self.store.operation_receipt(operation), directory)
        self.report(job)("INFO", f"Saved: {result['matched_title']}")
        return committed

    async def recover_apply_state(self, job, source_track: dict, directory: Path, operation: str):
        """Resume after beets moved/wrote a file but before activation completed."""
        plan_path = directory / "identity-plan.json"
        request_path = directory / "apply-request.json"
        if not plan_path.is_file() or not request_path.is_file():
            return None
        saved = json.loads(plan_path.read_text())
        if saved.get("operation") != operation:
            return None
        plan = saved["plan"]
        request = json.loads(request_path.read_text())
        receipt_path = directory / "download-complete.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {
            "downloader_name": "unknown", "downloader_version": "unknown", "source_url": plan["origin"]["url"],
        }
        result_path = directory / "apply-result.json"
        result = json.loads(result_path.read_text()) if result_path.is_file() else None
        candidate = None
        if result is None:
            recovered = await self.bridge(job, request["track"], directory, "inspect", path="", operation=operation)
            if recovered.pop("found", False):
                complete = recovered.pop("complete", False)
                if complete:
                    result = recovered
                else:
                    candidate = Path(recovered["file_path"])
        if result is not None:
            await self.activate_result(job, plan, result, receipt, operation, directory)
            return {"completed": True}
        if candidate and candidate.is_file():
            return {"completed": False, "plan": plan, "candidate": candidate,
                    "selected": request.get("selected"), "receipt": receipt, "overrides": request.get("overrides", {})}
        return None

    def queue_receipt_followup(self, receipt: dict | None):
        if not receipt:
            return False
        result = receipt.get("result") or {}
        source_id = result.get("source_id")
        origin_id = result.get("reingest_origin_id")
        if not source_id or not origin_id:
            return False
        track = self.store.one("SELECT * FROM tracks WHERE id=?", (source_id,))
        if not track or track["health"] != "UNAVAILABLE" or track["operation_state"] != "IDLE":
            return False
        try:
            self.store.queue_track(source_id, receipt["user_id"], {"mode": "ingest", "origin_id": origin_id})
            self.store.event("INFO", "Queued remaining origin after identity split", receipt["user_id"], source_id)
            return True
        except (ValueError, LookupError):
            return False

    async def cleanup_orphan_track(self, job, track_id: str):
        if self.store.track_has_origins(track_id):
            return
        track = self.store.one("SELECT * FROM tracks WHERE id=?", (track_id,))
        if not track:
            return
        directory = STATE / "staging" / f"cleanup-{track_id}"
        directory.mkdir(parents=True, exist_ok=True)
        try:
            await self.bridge(job, track, directory, "delete", path=track.get("file_path") or "")
        except Exception as exc:
            self.report(job)("WARNING", f"Could not remove orphan beets item cleanly: {exc}")
        path = Path(track["file_path"]).resolve() if track.get("file_path") else None
        user_root = (LIBRARY / track["user_id"]).resolve()
        if path and path.is_relative_to(user_root):
            path.unlink(missing_ok=True)
        self.store.delete_track(track_id, track["user_id"])
        shutil.rmtree(directory, ignore_errors=True)

    async def process_delete(self, job, track, ignore: bool, operation: str):
        directory = STATE / "staging" / track["id"]
        self.store.delete_track_with_receipt(track["id"], track["user_id"], operation,
                                             "delete_ignore" if ignore else "delete")
        await self.finish_operation_receipt(job, self.store.operation_receipt(operation), directory)
        suffix = " and ignored" if ignore else ""
        display = track.get("matched_title") or track["title"]
        self.report(job)("INFO", f"Deleted{suffix}: {display}")

    async def _download_candidate(self, job, track, origin, directory, operation):
        receipt_path = directory / "download-complete.json"
        candidate = directory / f"temp_{track['id']}.opus"
        if candidate.is_file() and receipt_path.is_file():
            receipt = json.loads(receipt_path.read_text())
            if receipt.get("source_url") == origin["url"]:
                return candidate, receipt
        candidate.unlink(missing_ok=True)
        receipt_path.unlink(missing_ok=True)
        try:
            candidate, receipt = await self.downloader.download(origin["url"], track["id"], directory, self.report(job))
        except DeferredOperation as exc:
            self.store.update("track_origins", origin["id"], availability="DEFERRED", last_error=str(exc))
            raise
        except Exception as exc:
            self.store.update("track_origins", origin["id"], availability="UNAVAILABLE", last_error=str(exc))
            raise
        self.store.update("track_origins", origin["id"], availability="AVAILABLE", last_error=None, last_download_at=utcnow())
        self.store.update("tracks", track["id"], temp_path=str(candidate))
        self.report(job)("INFO", f"Downloaded candidate from {origin['provider']} for {operation}")
        return candidate, receipt

    async def _identification_choices(self, job, track, directory, candidate):
        identified = await self.bridge(job, track, directory, "identify", path=str(candidate))
        choices = list(identified["choices"])
        if track.get("mbid"):
            if not any(choice.get("mbid") == track["mbid"] for choice in choices):
                try:
                    current = await self.metadata_for_recording(job, track, directory, track["mbid"])
                    choices.append(current)
                except DeferredOperation:
                    raise
                except Exception as exc:
                    self.report(job)("WARNING", f"Could not load current identity metadata: {exc}")
        choices.append({
            "title": track.get("matched_title") or track["title"], "artist": "", "album": "",
            "mbid": track.get("mbid"), "similarity": 0.0,
            "description": "Keep the currently stored/source metadata", "kind": "asis", "asis": True,
        })
        return identified, choices

    def _select_automatic(self, action: str, track: dict, identified: dict):
        choices = identified["choices"]
        top = choices[0] if choices else None
        if action == "redownload" and track.get("mbid"):
            if not identified["automatic"] or not top or top.get("mbid") != track["mbid"]:
                raise ReplacementIdentityMismatch(
                    "The replacement audio did not strongly match the current confirmed recording. "
                    "The existing file was retained. Use Reprocess to review an identity change."
                )
            return top
        if action == "reprocess" and track.get("mbid") and top:
            if top.get("mbid") != track["mbid"]:
                return None
        if identified["automatic"] and top:
            return top
        return None

    async def _apply_candidate(self, job, source_track, origin, selected, candidate,
                               receipt, action, operation_id, directory,
                               overrides=None, persisted_plan=None):
        plan_path = directory / "identity-plan.json"
        if persisted_plan is not None:
            plan = persisted_plan
        elif plan_path.is_file():
            saved = json.loads(plan_path.read_text())
            plan = saved["plan"] if saved.get("operation") == operation_id else None
        else:
            plan = None
        if plan is None:
            plan = self.store.identity_plan(source_track["id"], origin["id"], selected, action)
            plan["action"] = action
            atomic_json(plan_path, {"operation": operation_id, "plan": plan})
        if plan["ignored"]:
            if action == "ingest":
                self.store.ignore_identified_origin(origin["id"], plan["selected_mbid"],
                                                     selected.get("title") or source_track["title"], operation_id)
                shutil.rmtree(directory, ignore_errors=True)
                self.report(job)("INFO", "Ignored music was rediscovered and skipped")
                return {"ignored": True, "target_id": None}
            raise ValueError("The selected identity is in this user's ignored-music list")
        target_track = dict(plan["working_track"])
        target_track["source_url"] = origin["url"]
        target_asset = self.store.current_asset(target_track["id"])
        if plan["route"] == "existing" and await self.usable_asset(target_track, target_asset):
            self.store.commit_existing_identity(plan, action, operation_id)
            operation_receipt = self.store.operation_receipt(operation_id)
            await self.finish_operation_receipt(job, operation_receipt, directory)
            return {"target_id": operation_receipt["result"]["target_id"], "deduplicated": True}
        backup = None
        result = None
        if plan["route"] == "in_place" and source_track.get("file_path"):
            backup = await self.backup_current(source_track, directory)
        try:
            result_path = directory / "apply-result.json"
            if result_path.is_file():
                result = json.loads(result_path.read_text())
            else:
                result = await self.bridge(job, target_track, directory, "apply", path=str(candidate),
                                           selected=selected, operation=operation_id, overrides=overrides or {})
            return await self.activate_result(job, plan, result, receipt, operation_id, directory)
        except Exception:
            if not self.store.operation_receipt(operation_id):
                if result and result.get("file_path"):
                    try:
                        await self.bridge(job, target_track, directory, "delete", path=result["file_path"])
                    except Exception as cleanup_error:
                        self.report(job)("WARNING", f"Could not remove an uncommitted replacement cleanly: {cleanup_error}")
                        final = Path(result["file_path"]).resolve()
                        user_root = (LIBRARY / target_track["user_id"]).resolve()
                        old_path = Path(backup["path"]).resolve() if backup else None
                        if final.is_relative_to(user_root) and final != old_path:
                            final.unlink(missing_ok=True)
                await self.restore_current(backup)
            raise

    async def repair_tags(self, job, track, directory, operation):
        """Repair a staged copy, then swap atomically; retry never re-downloads."""
        asset = self.store.current_asset(track["id"])
        if not asset:
            raise ValueError("No current audio asset to repair")
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to((LIBRARY / track["user_id"]).resolve()):
            raise ValueError("Refusing repair outside this user's library")
        manifest_file = directory / "repair-plan.json"
        if manifest_file.exists():
            manifest = json.loads(manifest_file.read_text())
            if manifest["asset_id"] != asset["id"] or manifest["path"] != str(path):
                raise ValueError("Current asset changed since repair began")
        else:
            before = await asyncio.to_thread(sha256_file, path)
            if asset.get("sha256") and before != asset["sha256"]:
                raise ValueError("Audio hash changed externally; review or redownload it before tag repair")
            staged = directory / "repaired.opus"
            await asyncio.to_thread(shutil.copy2, path, staged)
            await self.bridge(job, track, directory, "repair", path=str(staged))
            manifest = {"asset_id": asset["id"], "path": str(path), "staged": str(staged),
                        "before": before, "after": await asyncio.to_thread(sha256_file, staged)}
            atomic_json(manifest_file, manifest)
        actual = await asyncio.to_thread(sha256_file, path)
        if actual == manifest["before"]:
            staged = Path(manifest["staged"])
            if await asyncio.to_thread(sha256_file, staged) != manifest["after"]:
                raise ValueError("Staged repair changed; refusing replacement")
            await asyncio.to_thread(atomic_copy, staged, path)
        elif actual != manifest["after"]:
            raise ValueError("Audio changed while repair was pending; original was not overwritten")
        refreshed = await self.bridge(job, track, directory, "refresh", path=str(path))
        stat = path.stat()
        digest = await asyncio.to_thread(sha256_file, path)
        if digest != manifest["after"]:
            raise ValueError("Audio changed during beets synchronization")
        self.store.commit_repair(track["id"], asset["id"],
                                 {"sha256": digest, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns},
                                 refreshed.get("beets_id"), operation)
        shutil.rmtree(directory, ignore_errors=True)
        self.report(job)("INFO", "Repaired discovery and loudness tags without redownloading or changing identity")

    async def process_track(self, job):
        payload = json.loads(job["payload"])
        mode = payload.get("mode", "ingest")
        operation = payload.get("operation", str(job["id"]))
        directory = STATE / "staging" / job["target"]
        completed = self.store.operation_receipt(operation)
        if completed:
            await self.finish_operation_receipt(job, completed, directory)
            return
        track = self.store.owned("tracks", job["target"], job["user_id"])
        action = payload.get("action") or payload.get("pending_action") or mode
        if mode in {"delete", "delete_ignore"}:
            await self.process_delete(job, track, mode == "delete_ignore", operation)
            return
        self.store.set_operation(track["id"], "RUNNING", action)
        directory.mkdir(parents=True, exist_ok=True)
        plan_file = directory / "operation.json"
        previous_plan = json.loads(plan_file.read_text()) if plan_file.exists() else {}
        first_attempt = previous_plan.get("operation") != operation
        if first_attempt:
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
            atomic_json(plan_file, {"operation": operation, "action": action, "origin_id": payload.get("origin_id")})
        if mode == "repair":
            await self.repair_tags(job, track, directory, operation)
            return
        if mode == "comment":
            asset = self.store.current_asset(track["id"])
            if not asset or not Path(asset["path"]).is_file():
                raise ValueError("No current audio file exists for the discovery comment")
            await self.bridge(job, track, directory, "comment", path=asset["path"])
            await self.refresh_asset_fingerprint(track["id"])
            self.store.complete_operation(track["id"], health="AVAILABLE")
            shutil.rmtree(directory, ignore_errors=True)
            return
        recovery = await self.recover_apply_state(job, track, directory, operation)
        if recovery:
            if recovery["completed"]:
                return
            plan = recovery["plan"]
            await self._apply_candidate(job, track, plan["origin"], recovery["selected"], recovery["candidate"],
                                        recovery["receipt"], plan.get("action") or action, operation, directory,
                                        recovery.get("overrides", {}), persisted_plan=plan)
            return
        if mode == "retag":
            asset = self.store.current_asset(track["id"])
            if not asset or not Path(asset["path"]).is_file():
                raise ValueError("No current audio file exists to retag")
            candidate = directory / f"temp_{track['id']}.opus"
            if not candidate.is_file():
                await asyncio.to_thread(shutil.copy2, asset["path"], candidate)
            origin = self.store.one("SELECT * FROM track_origins WHERE id=?", (track["current_origin_id"],))
            if not origin:
                raise ValueError("The current audio has no origin")
            receipt = {"downloader_name": asset.get("downloader_name"), "downloader_version": asset.get("downloader_version"), "source_url": origin["url"]}
            atomic_json(directory / "download-complete.json", receipt)
            selected = {"asis": True, "mbid": track.get("mbid")}
            overrides = payload.get("overrides", {})
            if overrides.get("mbid"):
                selected = await self.metadata_for_recording(job, track, directory, overrides["mbid"])
            await self._apply_candidate(job, track, origin, selected, candidate, receipt, "retag", operation, directory, overrides)
            return
        origin = self.store.owned("track_origins", payload["origin_id"], track["user_id"])
        if origin["track_id"] != track["id"]:
            raise ValueError("Selected origin no longer belongs to this track")
        candidate, receipt = await self._download_candidate(job, track, origin, directory, action)
        selected = payload.get("selected") if mode == "resume" else None
        candidate_digest = await asyncio.to_thread(sha256_file, candidate)
        review_again = mode == "resume" and (
            not payload.get("candidate_sha256") or payload["candidate_sha256"] != candidate_digest
        )
        if review_again:
            selected = None
            # A failed pre-apply attempt may have saved an identity plan for the
            # previous audio. Fresh approval must not reuse that obsolete route.
            for name in ("identity-plan.json", "apply-request.json", "apply-result.json"):
                (directory / name).unlink(missing_ok=True)
            self.report(job)("WARNING", "Approval no longer matches the staged audio. Fresh review is required.")
        if selected is None:
            identified, choices = await self._identification_choices(job, track, directory, candidate)
            selected = None if review_again else self._select_automatic(action, track, identified)
            if selected is None:
                pending = {"action": action, "origin_id": origin["id"], "operation": operation,
                           "candidate_sha256": candidate_digest}
                self.store.pause_for_approval(track["id"], choices, pending, str(candidate))
                self.report(job)("INFO", f"Metadata needs approval (beets: {identified['recommendation']})")
                return
        await self._apply_candidate(job, track, origin, selected, candidate, receipt, action,
                                    operation, directory, payload.get("overrides", {}))

    async def process_source(self, job):
        source = self.store.owned("sources", job["target"], job["user_id"])
        self.store.update("sources", source["id"], status="SYNCING", error=None)
        http = HttpPolicy(self.store, self.report(job))
        snapshot = await asyncio.to_thread(PROVIDERS[source["provider"]].resolve, source, http)
        stats = self.store.apply_snapshot(source, snapshot)
        source = self.store.owned("sources", source["id"], source["user_id"])
        await asyncio.to_thread(write_playlist, self.store, source)
        self.report(job)("INFO", f"Source synchronized: {snapshot.title} ({stats['entries']} entries, {stats['ignored']} ignored)")

    async def process_integrity(self, job):
        user = self.store.require_user(job["user_id"])
        tracks = self.store.rows("SELECT * FROM tracks WHERE user_id=?", (user,))
        directory = STATE / "staging" / f"integrity-{user}"
        directory.mkdir(parents=True, exist_ok=True)
        audit = await self.bridge_request(job, directory, "audit-many",
                                          {"mode": "audit_many", "user": user, "tracks": tracks}, timeout=1800) if tracks else {}
        issues: list[dict] = []
        expected_paths: set[Path] = set()
        user_root = (LIBRARY / user).resolve()
        for track in tracks:
            asset = self.store.current_asset(track["id"])
            if not asset:
                if track["health"] == "UNAVAILABLE" and track["operation_state"] in {"QUEUED", "RUNNING", "DEFERRED", "NEEDS_APPROVAL"}:
                    continue
                issues.append({"issue_key": f"track:{track['id']}:no-current-asset", "track_id": track["id"],
                               "kind": "NO_CURRENT_ASSET", "severity": "ERROR", "message": "No current audio asset is recorded."})
                self.store.update("tracks", track["id"], health="MISSING")
                continue
            path = Path(asset["path"]).resolve()
            expected_paths.add(path)
            if not path.is_relative_to(user_root):
                issues.append({"issue_key": f"track:{track['id']}:outside-library", "track_id": track["id"],
                               "asset_id": asset["id"], "kind": "PATH_OUTSIDE_LIBRARY", "severity": "ERROR",
                               "message": "The current asset path is outside this user's library.", "details": {"path": str(path)}})
                self.store.update("tracks", track["id"], health="MISSING")
                continue
            if not path.is_file():
                issues.append({"issue_key": f"track:{track['id']}:missing-file", "track_id": track["id"],
                               "asset_id": asset["id"], "kind": "MISSING_FILE", "severity": "ERROR",
                               "message": "The current audio file is missing.", "details": {"path": str(path)}})
                self.store.update("tracks", track["id"], health="MISSING")
                continue
            stat = path.stat()
            tags = await asyncio.to_thread(inspect_tags, path, track.get("discovered_at"))
            messages = {
                "DISCOVERY_COMMENT_MISMATCH": "COMMENT or DESCRIPTION differs from the expected discovery date.",
                "LOUDNESS_MISSING": "Opus gain or playback peak is missing; Navidrome may skip normalization.",
                "LOUDNESS_INVALID": "Loudness metadata contains invalid gain or peak values.",
                "LOUDNESS_POLICY_UNKNOWN": "Loudness reference has not been verified by the current pipeline.",
                "LOUDNESS_CONFLICT": "A conventional gain tag can override the Opus R128 gain.",
                "UNREADABLE_TAGS": "The Opus metadata could not be read.",
            }
            for kind in tags["issues"]:
                issues.append({"issue_key": f"track:{track['id']}:{kind.lower()}", "track_id": track["id"],
                               "asset_id": asset["id"], "kind": kind,
                               "severity": "ERROR" if kind == "UNREADABLE_TAGS" else "WARNING",
                               "message": messages[kind], "details": {"path": str(path)}})
            digest = asset.get("sha256")
            if not digest or asset.get("size_bytes") != stat.st_size or asset.get("mtime_ns") != stat.st_mtime_ns:
                actual = await asyncio.to_thread(sha256_file, path)
                if digest and actual != digest:
                    issues.append({"issue_key": f"track:{track['id']}:hash-mismatch", "track_id": track["id"],
                                   "asset_id": asset["id"], "kind": "HASH_MISMATCH", "severity": "WARNING",
                                   "message": "The audio file changed after it was activated.", "details": {"path": str(path)}})
                else:
                    self.store.update("assets", asset["id"], sha256=actual, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
            beets = audit.get(track["id"], {})
            if not beets.get("found"):
                issues.append({"issue_key": f"track:{track['id']}:beets-missing", "track_id": track["id"],
                               "asset_id": asset["id"], "kind": "BEETS_MISSING", "severity": "WARNING",
                               "message": "The track is missing from this user's beets library."})
            elif Path(beets["path"]).resolve() != path:
                issues.append({"issue_key": f"track:{track['id']}:beets-path", "track_id": track["id"],
                               "asset_id": asset["id"], "kind": "BEETS_PATH_MISMATCH", "severity": "WARNING",
                               "message": "Beets points to a different path than the active asset.",
                               "details": {"asset": str(path), "beets": beets["path"]}})
            self.store.update("tracks", track["id"], health="AVAILABLE")
        historical_assets = self.store.rows(
            "SELECT a.* FROM assets a JOIN tracks t ON t.id=a.track_id WHERE t.user_id=? AND a.state<>'CURRENT'", (user,),
        )
        for historical in historical_assets:
            historical_path = Path(historical["path"]).resolve()
            if historical_path.is_file() and historical_path.is_relative_to(user_root):
                expected_paths.add(historical_path)
                current = self.store.current_asset(historical["track_id"])
                if not current or Path(current["path"]).resolve() != historical_path:
                    issues.append({"issue_key": f"asset:{historical['id']}:retained-replacement", "track_id": historical["track_id"],
                                   "asset_id": historical["id"], "kind": "REPLACED_ASSET_RETAINED", "severity": "WARNING",
                                   "message": "A replaced audio file is still present on disk.", "details": {"path": str(historical_path)}})
        if user_root.exists():
            for path in user_root.rglob("*.opus"):
                resolved = path.resolve()
                if resolved not in expected_paths:
                    issues.append({"issue_key": f"orphan:{resolved}", "kind": "ORPHAN_FILE", "severity": "WARNING",
                                   "message": "An Opus file is not referenced by the application database.", "details": {"path": str(resolved)}})
        self.store.replace_integrity_issues(user, issues)
        self.store.set_setting(f"integrity_last:{user}", utcnow())
        shutil.rmtree(directory, ignore_errors=True)
        self.report(job)("INFO", f"Library verification finished: {len(issues)} issue(s)")

    async def work(self):
        while not self.stopping.is_set():
            job = self.store.claim()
            if not job:
                await asyncio.sleep(0.5)
                continue
            payload = json.loads(job["payload"])
            kind = payload.get("action") or payload.get("mode") or job["kind"]
            try:
                if job["kind"] == "sync":
                    await self.process_source(job)
                elif job["kind"] == "track":
                    await self.process_track(job)
                elif job["kind"] == "integrity":
                    await self.process_integrity(job)
                else:
                    raise ValueError("Unknown job type")
                self.store.update("jobs", job["id"], state="DONE", finished_at=utcnow(), error=None)
                if job["kind"] == "track":
                    self.queue_receipt_followup(self.store.operation_receipt(payload.get("operation")))
            except asyncio.CancelledError:
                raise
            except (DeferredOperation, ApiDeferred) as exc:
                retry_at = self.store.defer_job(job["id"], str(exc), retry_at=getattr(exc, "retry_at", None))
                when = datetime.fromtimestamp(retry_at, UTC).isoformat()
                self.report(job)("WARNING", f"{str(kind).upper()} deferred until {when}: {exc}")
            except Exception as exc:
                self.report(job)("ERROR", traceback.format_exc())
                self.store.update("jobs", job["id"], state="FAILED", error=str(exc), finished_at=utcnow())
                if job["kind"] == "sync":
                    self.store.update("sources", job["target"], status="FAILED", error=str(exc))
                elif job["kind"] == "track":
                    self.store.fail_operation(job["target"], str(kind), str(exc))
            finally:
                if job["kind"] == "sync":
                    interval = float(os.getenv("SYNC_INTERVAL_HOURS", "6"))
                    next_sync = time.time() + interval * 3600 if interval > 0 else 0
                    self.store.update("sources", job["target"], next_sync=next_sync)
            await asyncio.sleep(1.1)

    async def update_downloader(self):
        try:
            await self.downloader.update()
        except Exception:
            pass

    def start_update(self):
        if self.downloader.lock.locked() or any(not task.done() for task in self.children):
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
                        future = next_nightly(datetime.now(UTC), os.getenv("YTDLP_UPDATE_TIME", "04:00"), os.getenv("TZ", "Europe/Paris"))
                        self.store.set_setting("next_downloader_update", future.timestamp())
                        if next_at is not None:
                            self.start_update()
                for receipt in self.store.receipts_with_followups():
                    self.queue_receipt_followup(receipt)
                integrity_hours = float(os.getenv("INTEGRITY_INTERVAL_HOURS", "24"))
                if integrity_hours > 0:
                    for user in self.store.rows("SELECT name FROM users"):
                        key = f"integrity_next:{user['name']}"
                        if self.store.setting(key, 0) <= now:
                            self.store.enqueue("integrity", user["name"], user["name"], {"mode": "verify"})
                            self.store.set_setting(key, now + integrity_hours * 3600)
                self.store.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-20000 FROM events)")
                self.store.execute("DELETE FROM jobs WHERE state='DONE' AND id < (SELECT COALESCE(MAX(id),0)-2000 FROM jobs)")
            except Exception:
                self.store.event("ERROR", traceback.format_exc(), target="scheduler")
            await asyncio.sleep(30)
