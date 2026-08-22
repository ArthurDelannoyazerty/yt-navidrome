import os
import re
import time
import shutil
import base64
import asyncio
import subprocess
import requests
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from playlist_sync import sync_playlist_file

import yt_dlp
import acoustid
import musicbrainzngs
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

import database

# --- CONFIGURATION ---
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")
MATCH_THRESHOLD = 0.6
ACOUSTID_DELAY = 0.5

musicbrainzngs.set_useragent("Navidrome-Ingestor", "2.0", "contact@homelab.local")

# Asynchronous log queue for Web UI live streaming
log_queue = asyncio.Queue()

async def log(msg: str):
    print(msg)
    await log_queue.put(msg)

# Custom Exceptions for Granular Error Handling
class DownloadBotError(Exception): pass
class DownloadUnavailableError(Exception): pass
class DownloadNetworkError(Exception): pass

# --- AUDIO PROCESSING HELPERS ---

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

def fingerprint_audio(file_path: str):
    """Fingerprints the opus file using Chromaprint/AcoustID and fetches MusicBrainz data."""
    if not ACOUSTID_API_KEY:
        return None
    
    time.sleep(ACOUSTID_DELAY)
    try:
        results = acoustid.match(ACOUSTID_API_KEY, file_path)
        for score, recording_id, title, artist in results:
            if score >= MATCH_THRESHOLD:
                mb_data = musicbrainzngs.get_recording_by_id(
                    recording_id, 
                    includes=["artists", "releases", "tags"]
                )
                return mb_data.get('recording')
    except acoustid.NoBackendError:
        print("Warning: fpcalc (chromaprint) not found on system.")
    except Exception as e:
        print(f"Fingerprint lookup error: {e}")
    return None

def fetch_cover_art(release_mbid: str):
    """Fetches high-res front cover art from the Cover Art Archive."""
    url = f"https://coverartarchive.org/release/{release_mbid}/front"
    try:
        res = requests.get(url, allow_redirects=True, timeout=8)
        if res.status_code == 200:
            return res.content
    except Exception:
        pass
    return None

def calculate_replaygain(file_path: str):
    """Calculates EBU R128 integrated loudness via ffmpeg and converts to ReplayGain dB."""
    try:
        cmd = ['ffmpeg', '-nostats', '-i', file_path, '-filter_complex', 'ebur128', '-f', 'null', '-']
        result = subprocess.run(cmd, capture_output=True, text=True)
        for line in result.stderr.splitlines():
            if "I:" in line and "LUFS" in line:
                match = re.search(r"I:\s+([-+0-9.]+)\s+LUFS", line)
                if match:
                    lufs = float(match.group(1))
                    gain = -18.0 - lufs  # Navidrome/Subsonic standard target
                    return f"{gain:+.2f} dB"
    except Exception as e:
        print(f"ReplayGain calculation failed: {e}")
    return None

def fetch_lyrics(title: str, artist: str, album: str, duration_sec: float):
    """Retrieves synced or plain lyrics from LRCLIB."""
    headers = {"User-Agent": "Navidrome-Ingestor/2.0"}
    try:
        # 1. Exact match with duration
        url_get = "https://lrclib.net/api/get"
        params = {"track_name": title, "artist_name": artist, "album_name": album, "duration": int(duration_sec)}
        res = requests.get(url_get, params=params, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            return data.get("syncedLyrics") or data.get("plainLyrics")

        # 2. Search fallback
        url_search = "https://lrclib.net/api/search"
        search_params = {"q": f"{artist} {title}"}
        res_search = requests.get(url_search, params=search_params, headers=headers, timeout=5)
        if res_search.status_code == 200:
            results = res_search.json()
            if results:
                return results[0].get("syncedLyrics") or results[0].get("plainLyrics")
    except Exception:
        pass
    return None

# --- RESILIENT DOWNLOADER WITH TENACITY ---

@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=3, min=3, max=15),
    retry=retry_if_exception_type(DownloadNetworkError)
)
def download_audio_file(url: str, track_uuid: str):
    """Downloads audio via yt-dlp with network retries and error discrimination."""
    temp_filename = f"temp_{track_uuid}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{temp_filename}.%(ext)s',
        'noplaylist': True,
        'extractor_args': {'youtube': ['player_client=android']},
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'opus',
            'preferredquality': '160',
        }],
        'quiet': True,
        'no_warnings': True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return f"{temp_filename}.opus"
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "sign in to confirm you are not a bot" in err or "403" in err or "http error 429" in err:
            raise DownloadBotError("YouTube bot protection triggered.")
        elif "unavailable" in err or "private" in err or "removed" in err:
            raise DownloadUnavailableError("Video unavailable or removed.")
        else:
            raise DownloadNetworkError(f"Network error during download: {e}")

# --- MAIN TASK PROCESSING PIPELINE ---

async def process_track(item: dict, track_uuid: str, user_id: str):
    url = item["url"]
    display_title = item.get("title", url)
    discovery_date = item.get("discovery_date") or datetime.now().strftime("%Y-%m-%d")

    await log(f"[{track_uuid[:6]}] 📥 Starting: {display_title}")
    database.update_track_status(track_uuid, 'DOWNLOADING')

    temp_file = None
    try:
        # Run blocking download in default executor
        temp_file = await asyncio.to_thread(download_audio_file, url, track_uuid)
        
        if not temp_file or not os.path.exists(temp_file):
            raise Exception("File extraction failed; no output file generated.")

        await log(f"[{track_uuid[:6]}] 🔍 Fingerprinting audio...")
        mb_metadata = await asyncio.to_thread(fingerprint_audio, temp_file)

        # Tag and read metadata
        await log(f"[{track_uuid[:6]}] 🏷️ Writing metadata & calculating ReplayGain...")
        audio = OggOpus(temp_file)
        audio.delete()
        duration = audio.info.length

        parsed_info = {
            "title": display_title,
            "artist": "Unknown Artist",
            "albumartist": "Unknown Artist",
            "album": "Singles",
            "date": discovery_date[:4]
        }

        # Apply MusicBrainz metadata if matched
        if mb_metadata:
            parsed_info["title"] = mb_metadata.get("title", parsed_info["title"])
            if "artist-credit" in mb_metadata and mb_metadata["artist-credit"]:
                credit = mb_metadata["artist-credit"][0]
                if isinstance(credit, dict) and "artist" in credit:
                    parsed_info["artist"] = credit["artist"]["name"]
                    parsed_info["albumartist"] = credit["artist"]["name"]

            if "release-list" in mb_metadata and mb_metadata["release-list"]:
                release = mb_metadata["release-list"][0]
                parsed_info["album"] = release.get("title", "Singles")
                if release.get("date"):
                    parsed_info["date"] = release["date"][:4]

                # Fetch Cover Art
                release_id = release.get("id")
                if release_id:
                    cover_data = await asyncio.to_thread(fetch_cover_art, release_id)
                    if cover_data:
                        pic = Picture()
                        pic.type = 3
                        pic.mime = "image/jpeg"
                        pic.desc = "Front Cover"
                        pic.data = cover_data
                        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

        # Write core Vorbis tags
        audio["title"] = [parsed_info["title"]]
        audio["artist"] = [parsed_info["artist"]]
        audio["albumartist"] = [parsed_info["albumartist"]]
        audio["album"] = [parsed_info["album"]]
        audio["date"] = [parsed_info["date"]]
        audio["comment"] = [f"Discovery Date: {discovery_date}"]
        
        # Ingestion & Tracking ID (prevents losing the file when manually moved)
        audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
        audio["DISCOVERY_DATE"] = [discovery_date]

        # Calculate ReplayGain
        rg_gain = await asyncio.to_thread(calculate_replaygain, temp_file)
        if rg_gain:
            audio["replaygain_track_gain"] = [rg_gain]

        audio.save()

        # Build Organized Folder Hierarchy: library / user / Artist / Album / Track.opus
        folder_artist = sanitize_filename(parsed_info["albumartist"])
        folder_album = sanitize_filename(parsed_info["album"])
        file_name = sanitize_filename(f"{parsed_info['artist']} - {parsed_info['title']}.opus")

        user_dir = os.path.join(NAVIDROME_LIB_DIR, sanitize_filename(user_id))
        target_dir = os.path.join(user_dir, folder_artist, folder_album)
        os.makedirs(target_dir, exist_ok=True)

        final_path = os.path.join(target_dir, file_name)
        if os.path.exists(final_path):
            base, ext = os.path.splitext(file_name)
            final_path = os.path.join(target_dir, f"{base}_{track_uuid[:4]}{ext}")

        shutil.move(temp_file, final_path)

        # Set file system timestamp (mtime) to discovery date so Navidrome sorts by it
        try:
            dt_obj = datetime.strptime(discovery_date, "%Y-%m-%d")
            mtime = dt_obj.timestamp()
            os.utime(final_path, (mtime, mtime))
        except Exception:
            pass

        # Fetch and save Lyrics (.lrc alongside the .opus file)
        lyrics = await asyncio.to_thread(
            fetch_lyrics, 
            parsed_info["title"], 
            parsed_info["artist"], 
            parsed_info["album"], 
            duration
        )
        if lyrics:
            lrc_path = os.path.splitext(final_path)[0] + ".lrc"
            with open(lrc_path, "w", encoding="utf-8") as lf:
                lf.write(lyrics)

        database.update_track_status(track_uuid, 'COMPLETED', file_path=final_path)
        await log(f"[{track_uuid[:6]}] ✅ Saved: {parsed_info['artist']} - {parsed_info['title']}")

        # --- AUTO PLAYLIST SYNC ---
        playlist_name = item.get("playlist_name")
        if playlist_name:
            m3u_path = await asyncio.to_thread(sync_playlist_file, playlist_name, user_id)
            if m3u_path:
                await log(f"[{track_uuid[:6]}] 📋 Updated playlist: {os.path.basename(m3u_path)}")

    except DownloadBotError:
        database.update_track_status(track_uuid, 'BOT_BLOCKED', error_msg="YouTube Bot Block")
        await log(f"[{track_uuid[:6]}] 🛑 Anti-bot protection encountered.")
    except DownloadUnavailableError:
        database.update_track_status(track_uuid, 'FAILED', error_msg="Unavailable / Private")
        await log(f"[{track_uuid[:6]}] ❌ Video is private or unavailable.")
    except Exception as e:
        database.update_track_status(track_uuid, 'FAILED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] ⚠️ Processing error: {e}")
    finally:
        # Cleanup temporary files if any remain
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
