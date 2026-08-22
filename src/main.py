from fastapi import FastAPI, Request, Form, WebSocket, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os
import asyncio
import json

import database
from url_resolver import URLResolver
from worker import process_track, log_queue, log

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

@app.on_event("startup")
def startup():
    database.init_db()

@app.get("/", response_class=HTMLResponse)
async def get_ui(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

async def unpack_and_enqueue(raw_urls: list[str], user_id: str):
    """Background task to resolve playlists and push individual tracks to worker."""
    for raw_url in raw_urls:
        await log(f"🔍 Analyzing link: {raw_url}")
        
        try:
            resolved_items = URLResolver.resolve_url(raw_url)
        except Exception as e:
            await log(f"❌ Cannot resolve {raw_url}: {e}")
            continue
        
        if not resolved_items:
            await log(f"⚠️ Could not extract tracks from: {raw_url}")
            continue

        await log(f"📦 Found {len(resolved_items)} track(s). Adding to queue...")
        for item in resolved_items:
            track_uuid = database.add_track_to_queue(item, user_id)
            if track_uuid:
                # Dispatch audio download & tagging
                asyncio.create_task(process_track(item, track_uuid, user_id))
            else:
                await log(f"⏭️ Skipping already known track: {item['title']}")

@app.post("/ingest")
async def ingest_urls(background_tasks: BackgroundTasks, urls: str = Form(...), user_id: str = Form("1")):
    url_list = [u.strip() for u in urls.split('\n') if u.strip()]
    if not url_list:
        return {"message": "No valid URLs provided."}

    # Asynchronously unpack URLs so the Web UI responds instantly
    background_tasks.add_task(unpack_and_enqueue, url_list, user_id)
    return {"message": f"Processing {len(url_list)} URL(s) in the background. Check logs."}


@app.get("/api/tracks")
async def get_tracks(user_id: str = None):
    tracks = database.get_dashboard_tracks(user_id=user_id)
    # Compute summary stats
    stats = {"total": len(tracks), "completed": 0, "downloading": 0, "failed": 0}
    for t in tracks:
        status = t.get("status")
        if status == "COMPLETED":
            stats["completed"] += 1
        elif status in ("DOWNLOADING", "PENDING"):
            stats["downloading"] += 1
        elif status in ("FAILED", "BOT_BLOCKED"):
            stats["failed"] += 1
    return {"tracks": tracks, "stats": stats}

@app.post("/api/retry/{track_uuid}")
async def retry_track(track_uuid: str, background_tasks: BackgroundTasks):
    track = database.get_track_by_uuid(track_uuid)
    if not track:
        return {"error": "Track not found."}
    
    item = {
        "url": track["source_url"],
        "title": track["title"],
        "playlist_name": track["playlist_name"],
        "discovery_date": track["discovery_date"]
    }
    background_tasks.add_task(process_track, item, track_uuid, track["user_id"])
    return {"message": f"Retrying {track['title']}"}

@app.post("/api/force-retry/{track_uuid}")
async def force_retry_track(track_uuid: str, background_tasks: BackgroundTasks):
    """Deletes existing local file (if any) and forces a fresh pipeline run."""
    track = database.get_track_by_uuid(track_uuid)
    if not track:
        return {"error": "Track not found."}
    
    # Clean up old file if it exists
    if track.get("file_path") and os.path.exists(track["file_path"]):
        try:
            os.remove(track["file_path"])
        except OSError:
            pass

    # Reset DB status to PENDING
    database.reset_track_for_redownload(track_uuid)
    
    item = {
        "url": track["source_url"],
        "title": track["title"],
        "playlist_name": track["playlist_name"],
        "discovery_date": track["discovery_date"]
    }
    background_tasks.add_task(process_track, item, track_uuid, track["user_id"])
    return {"message": f"Force retrying {track['title']}"}

@app.post("/api/retry-all-failed")
async def retry_all_failed(background_tasks: BackgroundTasks, user_id: str = None):
    failed_tracks = database.get_failed_tracks(user_id=user_id)
    for track in failed_tracks:
        item = {
            "url": track["source_url"],
            "title": track["title"],
            "playlist_name": track["playlist_name"],
            "discovery_date": track["discovery_date"]
        }
        background_tasks.add_task(process_track, item, track["track_uuid"], track["user_id"])
    return {"message": f"Retrying {len(failed_tracks)} failed tracks."}

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

@app.post("/api/approve/{track_uuid}")
async def approve_track(track_uuid: str, background_tasks: BackgroundTasks, request: Request):
    """Receives chosen metadata from the UI and resumes Phase 2 of the worker."""
    track = database.get_track_by_uuid(track_uuid)
    if not track or track["status"] != "NEEDS_APPROVAL":
        return {"error": "Track not pending approval."}
    
    # Parse the incoming JSON metadata choice
    chosen_metadata = await request.json()
    
    # We import phase2 here to avoid circular imports if necessary
    from worker import process_track_phase_2
    
    background_tasks.add_task(
        process_track_phase_2, 
        track_uuid, 
        track["file_path"], 
        chosen_metadata, 
        track
    )
    
    database.update_track_status(track_uuid, 'DOWNLOADING', error_msg="Applying Tags...")
    return {"message": "Approval accepted. Tagging and moving track."}


@app.post("/api/batch-approve-best")
async def batch_approve_best(background_tasks: BackgroundTasks, user_id: str = None):
    """Auto-approves all pending tracks using their highest-scoring candidate."""
    conn = database.sqlite3.connect(database.DB_FILE)
    conn.row_factory = database.sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tracks WHERE status = 'NEEDS_APPROVAL'")
    tracks = [dict(r) for r in c.fetchall()]
    conn.close()

    from worker import process_track_phase_2
    count = 0
    for t in tracks:
        try:
            choices = json.loads(t.get("metadata_choices") or "[]")
            chosen_metadata = choices[0]["raw_data"] if choices else None
            background_tasks.add_task(process_track_phase_2, t["track_uuid"], t["file_path"], chosen_metadata, t)
            database.update_track_status(t["track_uuid"], 'DOWNLOADING', error_msg="Batch Approving...")
            count += 1
        except Exception:
            pass

    return {"message": f"Approving {count} track(s) with best match."}

@app.post("/api/batch-approve-original")
async def batch_approve_original(background_tasks: BackgroundTasks, user_id: str = None):
    """Bypasses MusicBrainz for all pending tracks and applies original clean YouTube titles."""
    conn = database.sqlite3.connect(database.DB_FILE)
    conn.row_factory = database.sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tracks WHERE status = 'NEEDS_APPROVAL'")
    tracks = [dict(r) for r in c.fetchall()]
    conn.close()

    from worker import process_track_phase_2
    for t in tracks:
        background_tasks.add_task(process_track_phase_2, t["track_uuid"], t["file_path"], None, t)
        database.update_track_status(t["track_uuid"], 'DOWNLOADING', error_msg="Batch Approving...")

    return {"message": f"Bypassing MusicBrainz for {len(tracks)} track(s)."}


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            msg = await log_queue.get()
            await websocket.send_text(msg)
    except Exception:
        pass