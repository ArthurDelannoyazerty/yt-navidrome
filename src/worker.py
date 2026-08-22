import os
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
import yt_dlp
from mutagen.oggopus import OggOpus
import database

# This queue passes log messages to the Web UI in real-time
log_queue = asyncio.Queue()

async def log(msg):
    print(msg)
    await log_queue.put(msg)

class DownloadBotError(Exception): pass
class DownloadUnavailableError(Exception): pass

# Retry up to 3 times, wait 5s, then 10s, then 20s for network issues
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=5, min=5, max=20))
def download_audio(url, track_uuid):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'temp_{track_uuid}.%(ext)s',
        'noplaylist': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'opus', 'preferredquality': '160'}],
        'quiet': True,
        'extractor_args': {'youtube': ['player_client=android']} # Bypasses many initial bot checks
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return f'temp_{track_uuid}.opus'
    
    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e).lower()
        if "sign in to confirm you are not a bot" in error_msg or "403" in error_msg:
            raise DownloadBotError("Bot detection triggered.")
        elif "unavailable" in error_msg or "private" in error_msg:
            raise DownloadUnavailableError("Video is private or unavailable.")
        else:
            raise Exception(f"Network or unknown error: {e}")

async def process_track(url, track_uuid, user_id):
    await log(f"[{track_uuid[:6]}] Starting processing for {url}")
    database.update_track_status(track_uuid, 'DOWNLOADING')
    
    try:
        # 1. Download
        await log(f"[{track_uuid[:6]}] Downloading...")
        temp_file = download_audio(url, track_uuid)
        
        # 2. Fingerprint (Simplified for brevity, insert your AcoustID code here)
        await log(f"[{track_uuid[:6]}] Fingerprinting...")
        # ... fetch metadata ...
        
        # 3. HQ Upgrade Hook (Free Soulseek/Slskd concept)
        # If AcoustID finds the track is e.g. "Pink Floyd - Time", we could 
        # ping a local slskd API here to download the FLAC instead, and delete the opus file.
        
        # 4. Tagging & Tracking ID
        await log(f"[{track_uuid[:6]}] Tagging...")
        audio = OggOpus(temp_file)
        audio["title"] = "Downloaded Song" # Replace with real meta
        # CRITICAL: This custom tag allows us to track the file even if you move it
        audio["NAVIDROME_PIPELINE_ID"] = track_uuid 
        audio.save()
        
        # 5. Move to User Folder
        final_dir = os.path.join("./navidrome_library", user_id)
        os.makedirs(final_dir, exist_ok=True)
        final_path = os.path.join(final_dir, f"{track_uuid}.opus")
        os.rename(temp_file, final_path)
        
        database.update_track_status(track_uuid, 'COMPLETED', file_path=final_path)
        await log(f"[{track_uuid[:6]}] ✅ Completed and saved.")
        
    except DownloadBotError:
        database.update_track_status(track_uuid, 'BOT_BLOCKED', error_msg="Bot Detected")
        await log(f"[{track_uuid[:6]}] 🛑 Bot check hit. Pausing queue.")
        # Logic to pause queue could be triggered here
    except DownloadUnavailableError:
        database.update_track_status(track_uuid, 'FAILED', error_msg="Unavailable/Private")
        await log(f"[{track_uuid[:6]}] ❌ Video missing or private.")
    except Exception as e:
        database.update_track_status(track_uuid, 'FAILED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] ⚠️ Failed: {e}")
