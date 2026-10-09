"""FastAPI control plane. Run one worker process per state directory."""
from dotenv import load_dotenv
load_dotenv()

import asyncio
import fcntl
import hashlib
import json
import logging
import shutil
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from common import LIBRARY, ROOT
from pipeline import Pipeline
from providers import parse_source
from store import SCHEMA_VERSION, Store

STATIC_ROOT = ROOT / "static"
INDEX_TEMPLATE = (STATIC_ROOT / "index.html").read_text()
_asset_digest = hashlib.sha256()
for _asset_name in ("style.css", "app.js"):
    _asset_digest.update((STATIC_ROOT / _asset_name).read_bytes())
STATIC_VERSION = _asset_digest.hexdigest()[:12]


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
    origin_id: str | None = None
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
            tasks = [
                asyncio.create_task(pipeline.work(), name="worker"),
                asyncio.create_task(pipeline.schedule(), name="scheduler"),
            ]

            def report_task_failure(task):
                if not task.cancelled() and task.exception():
                    store.event(
                        "ERROR",
                        f"{task.get_name()} stopped unexpectedly: {task.exception()}",
                        target="server",
                    )

            for task in tasks:
                task.add_done_callback(report_task_failure)
            app.state.worker_tasks = tasks
        try:
            yield
        finally:
            pipeline.stopping.set()
            for task in tasks + list(pipeline.children):
                task.cancel()
            await asyncio.gather(
                *tasks, *list(pipeline.children), return_exceptions=True
            )
            logging.getLogger().removeHandler(handler)
            lock.close()

    app = FastAPI(title="Music Ingestor", lifespan=lifespan)
    app.state.store = store
    app.state.pipeline = pipeline
    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")

    @app.middleware("http")
    async def browser_security(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlsplit
            if urlsplit(origin).netloc != request.headers.get("host"):
                store.event("ERROR", "Rejected cross-origin write request", target="api")
                return JSONResponse(
                    {"error": "Cross-origin writes are not allowed"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
        # The HTML must always be revalidated so it can point at the current
        # content-addressed frontend assets. Versioned static URLs are immutable.
        if request.url.path == "/":
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
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

    for exception in (
        ValueError,
        LookupError,
        HTTPException,
        RequestValidationError,
        Exception,
    ):
        app.add_exception_handler(exception, error_response)

    @app.get("/")
    def index():
        return HTMLResponse(
            INDEX_TEMPLATE.replace("__STATIC_VERSION__", STATIC_VERSION)
        )

    @app.get("/healthz")
    def health():
        store.one("SELECT 1")
        if any(task.done() for task in getattr(app.state, "worker_tasks", [])):
            raise HTTPException(
                503, "A background worker stopped; inspect the event log and restart"
            )
        return {"ok": True}

    @app.get("/api/users")
    def users():
        return [row["name"] for row in store.rows("SELECT name FROM users ORDER BY name")]

    @app.post("/api/users")
    def add_user(data: UserInput):
        store.add_user(data.name)
        return {"name": data.name}

    @app.get("/api/sources")
    def sources(user_id: str):
        store.require_user(user_id)
        return store.rows(
            "SELECT * FROM sources WHERE user_id=? ORDER BY title", (user_id,)
        )

    @app.post("/api/sources")
    def add_sources(data: SourceInput):
        refs = [parse_source(url) for url in data.urls]
        return [
            store.add_source(ref, data.user_id, data.monitored, data.label)
            for ref in refs
        ]

    @app.post("/api/sources/{source_id}/retry")
    def retry_source(source_id: str, data: ActionInput):
        source = store.owned("sources", source_id, data.user_id)
        if not store.enqueue("sync", source_id, data.user_id):
            raise ValueError("This source already has an active sync")
        store.update("sources", source["id"], status="PENDING", error=None)
        return {"message": "Source sync queued"}

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str, user_id: str):
        source = store.owned("sources", source_id, user_id)
        with store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute(
                "SELECT 1 FROM jobs WHERE target=? AND state IN ('PENDING','RUNNING')",
                (source_id,),
            ).fetchone():
                raise ValueError("Wait until this source finishes syncing")
            con.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if source["playlist_path"]:
            path = Path(source["playlist_path"]).resolve()
            playlist_dir = (LIBRARY / user_id / "000000-playlists").resolve()
            if path.parent == playlist_dir:
                path.unlink(missing_ok=True)
        return {"message": "Source removed; music retained"}

    @app.get("/api/tracks")
    def tracks(
        user_id: str,
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=100),
        status: str = "ALL",
        q: str = Query("", max_length=200),
    ):
        return store.dashboard_tracks(user_id, page, limit, status, q)

    @app.get("/api/tracks/{track_id}")
    def track_details(track_id: str, user_id: str):
        return store.track_details(track_id, user_id)

    @app.post("/api/tracks/{track_id}/action")
    def track_action(track_id: str, data: ActionInput):
        allowed = {
            "retry", "approve", "redownload", "reprocess", "retag",
            "delete", "delete_ignore",
        }
        if data.mode not in allowed:
            raise ValueError("Unsupported track action")
        if set(data.overrides) - {"artist", "title", "album", "mbid", "release_id"}:
            raise ValueError("Unsupported metadata override")
        import uuid
        for key in ("mbid", "release_id"):
            if data.overrides.get(key):
                uuid.UUID(data.overrides[key])
        store.queue_track(
            track_id,
            data.user_id,
            data.model_dump(exclude={"user_id"}, exclude_none=True),
        )
        return {"message": "Track operation queued"}

    @app.post("/api/batch")
    def batch(data: ActionInput):
        if data.mode not in {"retry", "best", "original"}:
            raise ValueError("Unsupported batch action")
        store.require_user(data.user_id)
        state = "FAILED" if data.mode == "retry" else "NEEDS_APPROVAL"
        queued = 0
        for track in store.rows(
            "SELECT * FROM tracks WHERE user_id=? AND operation_state=?",
            (data.user_id, state),
        ):
            if data.mode == "retry":
                payload = {"mode": "retry"}
            else:
                choices = json.loads(track["choices"] or "[]")
                if not choices:
                    continue
                index = 0 if data.mode == "best" else len(choices) - 1
                payload = {"mode": "approve", "index": index}
            try:
                store.queue_track(track["id"], data.user_id, payload)
                queued += 1
            except ValueError:
                pass
        return {"message": f"Queued {queued} tracks"}

    @app.get("/api/events")
    def events(user_id: str, after: int = Query(0, ge=0)):
        store.require_user(user_id)
        if not after:
            return list(reversed(store.rows(
                "SELECT * FROM events WHERE user_id=? OR user_id IS NULL "
                "ORDER BY id DESC LIMIT 200",
                (user_id,),
            )))
        return store.rows(
            "SELECT * FROM events WHERE id>? AND (user_id=? OR user_id IS NULL) "
            "ORDER BY id LIMIT 500",
            (after, user_id),
        )

    @app.get("/api/integrity")
    def integrity(user_id: str):
        return store.integrity_report(user_id)

    @app.post("/api/integrity/run")
    def run_integrity(data: ActionInput):
        store.require_user(data.user_id)
        if not store.enqueue(
            "integrity", data.user_id, data.user_id, {"mode": "verify"}
        ):
            raise ValueError("A library verification is already queued or running")
        return {"message": "Library verification queued"}

    @app.get("/api/ignored")
    def ignored(user_id: str):
        return store.list_ignored(user_id)

    @app.delete("/api/ignored/{ignored_id}")
    def restore_ignored(ignored_id: str, user_id: str):
        store.restore_ignored(ignored_id, user_id)
        return {"message": "Ignored music restored; it can be imported again"}

    @app.get("/api/system")
    def system():
        try:
            active = pipeline.downloader.active()
        except Exception as exc:
            active = {"error": str(exc)}
        return {
            "downloader": active,
            "youtube_circuit": pipeline.downloader.circuit_status(),
            "last_update": store.setting("downloader_update"),
            "next_update": store.setting("next_downloader_update"),
            "beets": "2.14.1",
            "schema": SCHEMA_VERSION,
        }

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
