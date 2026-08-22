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
import json
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

def download_via_cobalt(url: str, track_uuid: str):
    """Fallback engine using the Cobalt API to bypass strict IP blocks."""
    headers = {
        "Accept": "application/json", 
        "Content-Type": "application/json",
        "User-Agent": "Navidrome-Ingestor/2.0"
    }
    # Updated to Cobalt's v11 API Schema
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "opus"
    }
    try:
        # Request a download tunnel/redirect from Cobalt
        res = requests.post("https://api.cobalt.tools/", json=payload, headers=headers, timeout=15)
        
        # Capture exact error text if Cobalt rejects us (instead of a generic 400 error)
        if not res.ok:
            raise Exception(f"HTTP {res.status_code} - {res.text}")
            
        data = res.json()
        
        dl_url = data.get("url")
        if not dl_url:
            raise Exception(f"Cobalt API returned no URL: {data}")
            
        temp_filename = f"temp_{track_uuid}.opus"
        # Download the actual file stream
        with requests.get(dl_url, stream=True) as r:
            r.raise_for_status()
            with open(temp_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return temp_filename
    except Exception as e:
        raise DownloadBotError(f"Cobalt fallback failed: {e}")


@retry(
    stop=stop_after_attempt(3), 
    wait=wait_exponential(multiplier=3, min=3, max=15),
    retry=retry_if_exception_type(DownloadNetworkError),
    reraise=True  
)
def download_audio_file(url: str, track_uuid: str):
    """Downloads audio via yt-dlp. If blocked by YouTube, falls back to Cobalt."""
    temp_filename = f"temp_{track_uuid}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{temp_filename}.%(ext)s',
        'noplaylist': True,
        # 'tv,mweb' is currently the most robust anti-403 bypass for yt-dlp
        'extractor_args': {'youtube': ['client=tv,mweb']},
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
        if "sign in" in err or "403" in err or "429" in err or "bot" in err or "oauth" in err:
            print(f"[{track_uuid[:6]}] 🛑 yt-dlp blocked. Falling back to Cobalt API...")
            return download_via_cobalt(url, track_uuid)
        elif "unavailable" in err or "private" in err or "removed" in err:
            raise DownloadUnavailableError("Video unavailable or removed.")
        else:
            raise DownloadNetworkError(f"Network error during download: {e}")


def get_consensus_metadata(file_path: str, query_title: str):
    """
    Returns a tuple: (is_consensus_reached: bool, metadata_result)
    metadata_result is either a single dict (if consensus) or a list of dicts (if conflicting).
    """
    time.sleep(ACOUSTID_DELAY)
    candidates = []
    
    # 1. Try AcoustID
    try:
        results = acoustid.match(ACOUSTID_API_KEY, file_path)
        for score, recording_id, title, artist in results:
            if score >= MATCH_THRESHOLD:
                mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases"])
                rec = mb_data.get('recording')
                if rec:
                    candidates.append({
                        "id": rec["id"],
                        "title": rec.get("title", title),
                        "artist": rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", artist),
                        "album": rec.get("release-list", [{}])[0].get("title", "Singles"),
                        "raw_data": rec
                    })
    except Exception as e:
        print(f"AcoustID error: {e}")

    # 2. Text-Based Fallback (if AcoustID fails or gives too many choices)
    try:
        search_res = musicbrainzngs.search_recordings(query=query_title, limit=2)
        for rec in search_res.get("recording-list", []):
            candidates.append({
                "id": rec["id"],
                "title": rec.get("title", "Unknown"),
                "artist": rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", "Unknown"),
                "album": rec.get("release-list", [{}])[0].get("title", "Singles"),
                "raw_data": rec
            })
    except Exception as e:
        print(f"Text-Search error: {e}")

    # Deduplicate candidates by MusicBrainz ID
    unique_candidates = {c["id"]: c for c in candidates}.values()
    final_list = list(unique_candidates)

    if len(final_list) == 1:
        return True, final_list[0]["raw_data"]
    elif len(final_list) > 1:
        return False, final_list # Needs human approval
    else:
        return True, None # No matches found anywhere, use fallback metadata

# --- PHASE 1: DOWNLOAD & FINGERPRINT ---
async def process_track(item: dict, track_uuid: str, user_id: str):
    url = item["url"]
    display_title = item.get("title", url)
    
    await log(f"[{track_uuid[:6]}] 📥 Starting: {display_title}")
    database.update_track_status(track_uuid, 'DOWNLOADING')

    temp_file = None
    try:
        temp_file = await asyncio.to_thread(download_audio_file, url, track_uuid)
        if not temp_file or not os.path.exists(temp_file):
            raise Exception("File extraction failed.")

        await log(f"[{track_uuid[:6]}] 🔍 Fingerprinting audio...")
        is_consensus, metadata_result = await asyncio.to_thread(get_consensus_metadata, temp_file, display_title)

        if not is_consensus:
            # Pause pipeline and wait for human
            await log(f"[{track_uuid[:6]}] ⚠️ Multiple metadata matches found. Pending approval.")
            choices_json = json.dumps(metadata_result)
            database.update_track_metadata_choices(track_uuid, choices_json, temp_file)
            return  # Stop execution here!

        # If consensus is reached, seamlessly proceed to Phase 2
        await process_track_phase_2(track_uuid, temp_file, metadata_result, item)

    except DownloadBotError as e:
        # We now log the exact error string so we can see why Cobalt failed!
        database.update_track_status(track_uuid, 'BOT_BLOCKED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] 🛑 {str(e)}")
    except DownloadUnavailableError:
        database.update_track_status(track_uuid, 'FAILED', error_msg="Unavailable / Private")
        await log(f"[{track_uuid[:6]}] ❌ Video is private or unavailable.")
    except Exception as e:
        database.update_track_status(track_uuid, 'FAILED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] ⚠️ Processing error: {e}")
        if temp_file and os.path.exists(temp_file):
            os.remove(temp_file)

# --- PHASE 2: TAG, NORMALIZE, AND MOVE ---
async def process_track_phase_2(track_uuid: str, temp_file: str, mb_metadata: dict, track_info: dict):
    try:
        user_id = track_info.get("user_id", "admin")
        discovery_date = track_info.get("discovery_date") or datetime.now().strftime("%Y-%m-%d")
        display_title = track_info.get("title", "Unknown Title")

        await log(f"[{track_uuid[:6]}] 🏷️ Applying Tags & ReplayGain...")
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

        # Apply chosen metadata
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

        # Write Vorbis tags
        audio["title"] = [parsed_info["title"]]
        audio["artist"] = [parsed_info["artist"]]
        audio["albumartist"] = [parsed_info["albumartist"]]
        audio["album"] = [parsed_info["album"]]
        audio["date"] = [parsed_info["date"]]
        audio["comment"] = [f"Discovery Date: {discovery_date}"]
        audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
        audio["DISCOVERY_DATE"] = [discovery_date]

        rg_gain = await asyncio.to_thread(calculate_replaygain, temp_file)
        if rg_gain:
            audio["replaygain_track_gain"] = [rg_gain]

        audio.save()

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

        # Set mtime
        try:
            dt_obj = datetime.strptime(discovery_date, "%Y-%m-%d")
            mtime = dt_obj.timestamp()
            os.utime(final_path, (mtime, mtime))
        except Exception:
            pass

        # Lyrics
        lyrics = await asyncio.to_thread(fetch_lyrics, parsed_info["title"], parsed_info["artist"], parsed_info["album"], duration)
        if lyrics:
            lrc_path = os.path.splitext(final_path)[0] + ".lrc"
            with open(lrc_path, "w", encoding="utf-8") as lf:
                lf.write(lyrics)

        database.update_track_status(track_uuid, 'COMPLETED', file_path=final_path)
        await log(f"[{track_uuid[:6]}] ✅ Saved: {parsed_info['artist']} - {parsed_info['title']}")

        # Playlist Sync
        playlist_name = track_info.get("playlist_name")
        if playlist_name:
            m3u_path = await asyncio.to_thread(sync_playlist_file, playlist_name, user_id)
            if m3u_path:
                await log(f"[{track_uuid[:6]}] 📋 Updated playlist: {os.path.basename(m3u_path)}")

    except Exception as e:
        database.update_track_status(track_uuid, 'FAILED', error_msg=f"Phase 2 error: {e}")
        await log(f"[{track_uuid[:6]}] ⚠️ Phase 2 Tagging error: {e}")
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass