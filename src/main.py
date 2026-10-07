"""FastAPI control plane. Run ONE worker process; durable jobs live in SQLite."""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import fcntl
import json
import logging
import os
import shutil
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common import ROOT
from pipeline import Pipeline
from providers import parse_source
from store import Store


class UserInput(BaseModel):
    name: str


class SourceInput(BaseModel):
    user_id: str
    urls: list[str] = Field(min_length=1, max_length=100)
    monitored: bool = False
    label: str | None = None


class ActionInput(BaseModel):
    user_id: str
    mode: str = "retry"
    index: int | None = None
    overrides: dict[str, str | None] = Field(default_factory=dict)


class EventHandler(logging.Handler):
    def __init__(self, store):
        super().__init__(logging.WARNING)
        self.store = store

    def emit(self, record):
        try:
            self.store.event(record.levelname, self.format(record), target="server")
        except Exception:
            self.handleError(record)


def create_app(store=None, start_workers=True):
    store = store or Store()
    pipeline = Pipeline(store)

    @asynccontextmanager
    async def lifespan(app):
        store.init()
        lock = (store.path.parent / "instance.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Run one API worker/replica per state directory")
        handler = EventHandler(store)
        logging.getLogger().addHandler(handler)
        tasks = []
        if start_workers:
            store.recover()
            for binary in ("ffmpeg", "fpcalc", "deno"):
                if not shutil.which(binary):
                    store.event("ERROR", f"Required binary not found: {binary}", target="startup")
            tasks = [asyncio.create_task(pipeline.work(), name="worker"),
                     asyncio.create_task(pipeline.schedule(), name="scheduler")]
            def report_task_failure(task):
                if not task.cancelled() and task.exception():
                    store.event("ERROR", f"{task.get_name()} stopped unexpectedly: {task.exception()}", target="server")
            for task in tasks:
                task.add_done_callback(report_task_failure)
            app.state.worker_tasks = tasks
        try:
            yield
        finally:
            pipeline.stopping.set()
            for task in tasks + list(pipeline.children):
                task.cancel()
            await asyncio.gather(*tasks, *list(pipeline.children), return_exceptions=True)
            logging.getLogger().removeHandler(handler)
            lock.close()

    app = FastAPI(title="Music Ingestor", lifespan=lifespan)
    app.state.store, app.state.pipeline = store, pipeline
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def browser_security(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get("host"):
                store.event("ERROR", "Rejected cross-origin write request", target="api")
                return JSONResponse({"error": "Cross-origin writes are not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        return response

    async def error_response(request, exc):
        status = 404 if isinstance(exc, LookupError) else 400
        if isinstance(exc, RequestValidationError):
            status = 422
        elif isinstance(exc, HTTPException):
            status = exc.status_code
        elif not isinstance(exc, (ValueError, LookupError)):
            status = 500
        detail = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        store.event("ERROR", f"{request.method} {request.url.path}: {detail}", target="api")
        if status == 500:
            store.event("ERROR", traceback.format_exc(), target="api")
            detail = "Internal server error. See the event log."
        return JSONResponse({"error": detail}, status_code=status)

    for exception in (ValueError, LookupError, HTTPException, RequestValidationError, Exception):
        app.add_exception_handler(exception, error_response)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/healthz")
    def health():
        store.one("SELECT 1")
        if any(t.done() for t in getattr(app.state, "worker_tasks", [])):
            raise HTTPException(503, "A background worker stopped; inspect the event log and restart")
        return {"ok": True}

    @app.get("/api/users")
    def users():
        return [r["name"] for r in store.rows("SELECT name FROM users ORDER BY name")]

    @app.post("/api/users")
    def add_user(data: UserInput):
        store.add_user(data.name)
        return {"name": data.name}

    @app.get("/api/sources")
    def sources(user_id: str):
        store.require_user(user_id)
        return store.rows("SELECT * FROM sources WHERE user_id=? ORDER BY title", (user_id,))

    @app.post("/api/sources")
    def add_sources(data: SourceInput):
        # Validate the whole input before persisting any work.
        refs = [parse_source(url) for url in data.urls]
        return [store.add_source(ref, data.user_id, data.monitored, data.label) for ref in refs]

    @app.post("/api/sources/{source_id}/retry")
    def retry_source(source_id: str, data: ActionInput):
        source = store.owned("sources", source_id, data.user_id)
        if not store.enqueue("sync", source_id, data.user_id):
            raise ValueError("This source already has an active sync")
        store.update("sources", source_id, status="PENDING")
        return {"message": "Source sync queued"}

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str, user_id: str):
        source = store.owned("sources", source_id, user_id)
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM jobs WHERE target=? AND state='RUNNING'", (source_id,)).fetchone():
                raise ValueError("Wait until this source finishes syncing")
            con.execute("DELETE FROM jobs WHERE target=? AND state='PENDING'", (source_id,))
            con.execute("DELETE FROM sources WHERE id=?", (source_id,))
        # Remove only the generated playlist; never delete music files.
        if source["playlist_path"]:
            from common import LIBRARY
            path = Path(source["playlist_path"]).resolve()
            if path.parent == (LIBRARY / user_id / "000000-playlists").resolve():
                path.unlink(missing_ok=True)
        return {"message": "Source removed; music retained"}

    @app.get("/api/tracks")
    def tracks(user_id: str, page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=100), status: str = "ALL"):
        store.require_user(user_id)
        params = [user_id]
        where = "user_id=?"
        if status != "ALL":
            where += " AND status=?"
            params.append(status)
        total = store.one(f"SELECT COUNT(*) AS n FROM tracks WHERE {where}", params)["n"]
        rows = store.rows(f"SELECT * FROM tracks WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?", (*params, limit, (page - 1) * limit))
        for row in rows:
            choices = json.loads(row.pop("choices"))
            # Only display data crosses the approval boundary; the server retains tag snapshots.
            row["choices"] = [{k: v for k, v in c.items() if k != "tags"} for c in choices]
            row.pop("selected", None)
            row["playlists"] = store.rows("""SELECT s.title,m.added_at FROM sources s JOIN memberships m
                ON s.id=m.source_id WHERE m.track_id=? AND s.is_playlist=1 ORDER BY m.added_at""", (row["id"],))
        counts = store.rows("SELECT status,COUNT(*) AS n FROM tracks WHERE user_id=? GROUP BY status", (user_id,))
        return {"tracks": rows, "total": total, "page": page, "limit": limit, "stats": {r["status"]: r["n"] for r in counts}}

    @app.post("/api/tracks/{track_id}/action")
    def track_action(track_id: str, data: ActionInput):
        if data.mode not in {"retry", "approve", "redownload", "retag", "reidentify"}:
            raise ValueError("Unsupported track action")
        if set(data.overrides) - {"artist", "title", "album", "mbid", "release_id"}:
            raise ValueError("Unsupported metadata override")
        import uuid
        for key in ("mbid", "release_id"):
            if data.overrides.get(key):
                uuid.UUID(data.overrides[key])
        store.queue_track(track_id, data.user_id, data.model_dump(exclude={"user_id"}))
        return {"message": "Track job queued"}

    @app.post("/api/batch")
    def batch(data: ActionInput):
        if data.mode not in {"retry", "best", "original"}:
            raise ValueError("Unsupported batch action")
        state = "FAILED" if data.mode == "retry" else "NEEDS_APPROVAL"
        queued = 0
        store.require_user(data.user_id)
        for track in store.rows("SELECT * FROM tracks WHERE user_id=? AND status=?", (data.user_id, state)):
            if data.mode == "best" and not json.loads(track["choices"]):
                continue
            payload = {"mode": "retry"} if data.mode == "retry" else {"mode": "approve", "index": 0 if data.mode == "best" else None}
            try:
                store.queue_track(track["id"], data.user_id, payload)
                queued += 1
            except ValueError:
                pass  # Concurrent request already queued this track.
        return {"message": f"Queued {queued} tracks"}

    @app.get("/api/events")
    def events(user_id: str, after: int = Query(0, ge=0)):
        store.require_user(user_id)
        if not after:
            return list(reversed(store.rows("SELECT * FROM events WHERE user_id=? OR user_id IS NULL ORDER BY id DESC LIMIT 200", (user_id,))))
        return store.rows("SELECT * FROM events WHERE id>? AND (user_id=? OR user_id IS NULL) ORDER BY id LIMIT 500", (after, user_id))

    @app.get("/api/system")
    def system():
        try:
            active = pipeline.downloader.active()
        except Exception as exc:
            active = {"error": str(exc)}
        return {"downloader": active, "last_update": store.setting("downloader_update"),
                "next_update": store.setting("next_downloader_update"), "beets": "2.14.1"}

    @app.post("/api/downloader/update")
    async def update_downloader_async():
        pipeline.start_update()
        return {"message": "Downloader update queued"}

    @app.post("/api/downloader/rollback")
    async def rollback_downloader():
        if pipeline.downloader.lock.locked():
            raise ValueError("Wait for the active downloader update to finish")
        previous = await pipeline.downloader.rollback()
        return {"message": f"Downloader rolled back to {previous['version']}"}

    return app


app = create_app()
