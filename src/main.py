from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv()) 

import os
import glob
import json
import shutil
import asyncio
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

import database
from url_resolver import URLResolver
from worker import process_track, process_track_phase_2, retag_existing_track, log_queue, log



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

WS_CLIENTS: set = set()               # all connected browsers receive EVERY line
LOG_HISTORY = deque(maxlen=500)       # replay backlog to newly opened tabs
ACTIVE_TASKS: set = set()             # strong refs so fire-and-forget tasks aren't GC'd


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    ACTIVE_TASKS.add(task)
    task.add_done_callback(ACTIVE_TASKS.discard)
    return task


async def log_broadcaster():
    """Single queue consumer -> fans out to every connected WebSocket client."""
    while True:
        msg = await log_queue.get()
        LOG_HISTORY.append(msg)
        dead = []
        for ws in list(WS_CLIENTS):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            WS_CLIENTS.discard(ws)


async def sync_scheduler(interval_hours: float):
    """Missing Piece #2: poll monitored playlists for ALL users while you sleep."""
    while True:
        try:
            targets = database.get_all_monitored_urls()
            await log(f"⏰ Scheduled sync started ({len(targets)} monitored playlist(s))...")
            for t in targets:
                try:
                    await unpack_and_enqueue([t["url"]], t["user_id"])
                except Exception as e:
                    await log(f"❌ Scheduled sync failed for {t['label']}: {e}")
            await log("⏰ Scheduled sync finished.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await log(f"⚠️ Scheduler error: {e}")
        await asyncio.sleep(interval_hours * 3600)


async def unpack_and_enqueue(raw_urls: list, user_id: str):
    """Resolves playlists OFF the event loop, then queues individual tracks."""
    for raw_url in raw_urls:
        await log(f"🔍 Analyzing link: {raw_url}")
        try:
            resolved_items = await asyncio.to_thread(URLResolver.resolve_url, raw_url)
        except Exception as e:
            await log(f"❌ Cannot resolve {raw_url}: {e}")
            continue

        if not resolved_items:
            await log(f"⚠️ Could not extract tracks from: {raw_url}")
            continue

        await log(f"📦 Found {len(resolved_items)} track(s). Adding to queue...")
        for item in resolved_items:
            item["user_id"] = user_id  # C7 fix: survives into Phase 2 folder routing
            track_uuid = database.add_track_to_queue(item, user_id)
            if track_uuid:
                spawn(process_track(item, track_uuid, user_id))
            else:
                await log(f"⏭️ Skipping already known track: {item['title']}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()

    recovered = database.fail_interrupted()
    if recovered:
        await log(f"♻️ Recovered {recovered} interrupted track(s) -> marked FAILED (use Retry).")

    # Temp files referenced by NEEDS_APPROVAL rows are Phase 2 inputs — protect them.
    protected = set()
    for r in database.get_tracks_by_status("NEEDS_APPROVAL"):
        p = r.get("file_path")
        if p:
            protected.add(p)
            protected.add(os.path.splitext(p)[0])  # pre-extraction original (.webm/.m4a)
    for f in glob.glob(os.path.join(BASE_DIR, "temp_*")):
        if f in protected:
            continue
        try:
            os.remove(f)
        except OSError:
            pass

    if not shutil.which("ffmpeg"):
        await log("⚠️ ffmpeg not found in PATH — downloads & ReplayGain WILL FAIL.")
    if not shutil.which("fpcalc"):
        await log("⚠️ fpcalc (chromaprint) not found — AcoustID fingerprinting disabled.")

    broadcaster = spawn(log_broadcaster())

    scheduler_task = None
    try:
        interval = float((os.getenv("SYNC_INTERVAL_HOURS") or "0").split("#", 1)[0].strip() or 0)
    except ValueError:
        interval = 0.0
    if interval > 0:
        scheduler_task = spawn(sync_scheduler(interval))
        await log(f"⏰ Auto-sync scheduler active: every {interval}h.")

    yield

    for t in (broadcaster, scheduler_task):
        if t and not t.done():
            t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def get_ui(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/ingest")
async def ingest_urls(background_tasks: BackgroundTasks, urls: str = Form(...), user_id: str = Form("1")):
    url_list = [u.strip() for u in urls.split('\n') if u.strip()]
    if not url_list:
        return {"message": "No valid URLs provided."}
    background_tasks.add_task(unpack_and_enqueue, url_list, user_id)
    return {"message": f"Processing {len(url_list)} URL(s) in the background. Check logs."}


@app.get("/api/tracks")
async def get_tracks(user_id: str = None):
    tracks = database.get_dashboard_tracks(user_id=user_id)
    return {"tracks": tracks, "stats": database.get_status_stats(user_id=user_id)}

@app.post("/api/retag/{track_uuid}")
async def api_retag_track(track_uuid: str, background_tasks: BackgroundTasks, request: Request):
    track = database.get_track_by_uuid(track_uuid)
    if not track:
        return {"error": "Track not found."}
    if track["status"] != "COMPLETED":
        return {"error": "Manual fixes are only allowed on COMPLETED tracks."}
    data = await request.json()
    background_tasks.add_task(
        retag_existing_track,
        track_uuid,
        mbid=data.get("mbid"),
        custom_artist=data.get("artist"),
        custom_title=data.get("title"),
        custom_album=data.get("album"),
    )
    return {"message": "Retag task dispatched."}


def _build_retry_item(track: dict) -> dict:
    return {
        "url": track["source_url"],
        "title": track["title"],
        "playlist_name": track["playlist_name"],
        "discovery_date": track["discovery_date"],
        "user_id": track["user_id"],           # C7 fix
    }

@app.get("/api/users")
async def api_get_users():
    return {"users": database.get_users()}


@app.post("/api/users")
async def api_add_user(username: str = Form(...)):
    clean_name = database.add_user(username)
    if not clean_name:
        return {"error": "Invalid username."}
    return {"message": f"User '{clean_name}' created.", "username": clean_name}


@app.post("/api/retry/{track_uuid}")
async def retry_track(track_uuid: str, background_tasks: BackgroundTasks):
    # Atomic claim: double-clicks cannot dispatch twice.
    if not database.claim_track(track_uuid, ["FAILED", "BOT_BLOCKED"], "DOWNLOADING", "Retrying..."):
        return {"error": "Track is not in a retryable state."}
    track = database.get_track_by_uuid(track_uuid)
    background_tasks.add_task(process_track, _build_retry_item(track), track_uuid, track["user_id"])
    return {"message": f"Retrying {track['title']}"}


@app.post("/api/force-retry/{track_uuid}")
async def force_retry_track(track_uuid: str, background_tasks: BackgroundTasks):
    if not database.claim_track(track_uuid, ["COMPLETED", "FAILED", "BOT_BLOCKED"],
                                "DOWNLOADING", "Force retrying..."):
        return {"error": "Track cannot be force-retried right now."}
    track = database.get_track_by_uuid(track_uuid)

    for victim in filter(None, {track.get("file_path"),
                                os.path.splitext(track.get("file_path") or "")[0] + ".lrc"}):
        if os.path.exists(victim):
            try:
                os.remove(victim)
            except OSError:
                pass

    database.reset_track_for_redownload(track_uuid)
    background_tasks.add_task(process_track, _build_retry_item(track), track_uuid, track["user_id"])
    return {"message": f"Force retrying {track['title']}"}


@app.post("/api/retry-all-failed")
async def retry_all_failed(background_tasks: BackgroundTasks, user_id: str = None):
    count = 0
    for track in database.get_failed_tracks(user_id=user_id):
        if database.claim_track(track["track_uuid"], ["FAILED", "BOT_BLOCKED"],
                                "DOWNLOADING", "Bulk retrying..."):
            background_tasks.add_task(process_track, _build_retry_item(track),
                                      track["track_uuid"], track["user_id"])
            count += 1
    return {"message": f"Retrying {count} failed track(s)."}


@app.post("/api/approve/{track_uuid}")
async def approve_track(track_uuid: str, background_tasks: BackgroundTasks, request: Request):
    try:
        chosen_metadata = await request.json()  # may legitimately be null (Keep Original)
    except Exception:
        chosen_metadata = None

    track = database.get_track_by_uuid(track_uuid)
    if not track or track["status"] != "NEEDS_APPROVAL":
        return {"error": "Track not pending approval."}

    # Atomic gate: second click on ✅ is rejected here.
    if not database.claim_track(track_uuid, ["NEEDS_APPROVAL"], "DOWNLOADING", "Applying Tags..."):
        return {"error": "Track was already approved."}

    background_tasks.add_task(process_track_phase_2, track_uuid,
                              track["file_path"], chosen_metadata, track)
    return {"message": "Approval accepted. Tagging and moving track."}


@app.post("/api/batch-approve-best")
async def batch_approve_best(background_tasks: BackgroundTasks, user_id: str = None):
    count = 0
    for t in database.get_tracks_by_status("NEEDS_APPROVAL", user_id=user_id):
        try:
            choices = json.loads(t.get("metadata_choices") or "[]")
            chosen = choices[0]["raw_data"] if choices else None  # [0] = highest score (sorted)
        except Exception:
            chosen = None
        if database.claim_track(t["track_uuid"], ["NEEDS_APPROVAL"], "DOWNLOADING", "Batch approving..."):
            background_tasks.add_task(process_track_phase_2, t["track_uuid"],
                                      t["file_path"], chosen, t)
            count += 1
    return {"message": f"Approving {count} track(s) with best match."}


@app.post("/api/batch-approve-original")
async def batch_approve_original(background_tasks: BackgroundTasks, user_id: str = None):
    count = 0
    for t in database.get_tracks_by_status("NEEDS_APPROVAL", user_id=user_id):
        if database.claim_track(t["track_uuid"], ["NEEDS_APPROVAL"], "DOWNLOADING", "Batch approving..."):
            background_tasks.add_task(process_track_phase_2, t["track_uuid"], t["file_path"], None, t)
            count += 1
    return {"message": f"Bypassing MusicBrainz for {count} track(s)."}


@app.get("/api/monitored-urls/{user_id}")
async def api_get_monitored_urls(user_id: str):
    return {"urls": database.get_monitored_urls(user_id)}


@app.post("/api/monitored-urls")
async def api_add_monitored_url(user_id: str = Form(...), url: str = Form(...), label: str = Form("Playlist")):
    database.add_monitored_url(user_id, url.strip(), label.strip())
    return {"message": "Saved"}


@app.delete("/api/monitored-urls/{url_id}")
async def api_delete_monitored_url(url_id: int):
    database.delete_monitored_url(url_id)
    return {"message": "Deleted"}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        for line in list(LOG_HISTORY):       # catch up on missed lines
            await websocket.send_text(line)
        WS_CLIENTS.add(websocket)
        while True:
            await websocket.receive_text()   # hold open until client disconnects
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        WS_CLIENTS.discard(websocket)