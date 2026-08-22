from fastapi import FastAPI, Request, Form, WebSocket, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import os
import asyncio

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
        resolved_items = URLResolver.resolve_url(raw_url)
        
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

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            msg = await log_queue.get()
            await websocket.send_text(msg)
    except Exception:
        pass