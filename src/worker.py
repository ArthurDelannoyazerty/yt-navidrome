# File: /src/worker.py

import os
import re
import time
import json
import base64
import shutil
import difflib
import threading
import subprocess
import requests
import asyncio
from datetime import datetime
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from playlist_sync import sync_playlist_file

import yt_dlp
import musicbrainzngs
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

import database

# ================= CONFIGURATION =================

def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").split("#", 1)[0].strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        print(f"⚠️ Invalid {name}={raw!r}; using default {default}")
        return default

def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))

PIPELINE_SEMAPHORE = asyncio.Semaphore(_env_int("MAX_CONCURRENT_TRACKS", 3))

DOWNLOAD_SLEEP_MIN = _env_float("DOWNLOAD_SLEEP_MIN", 2)
DOWNLOAD_SLEEP_MAX = _env_float("DOWNLOAD_SLEEP_MAX", 6)
PLAYER_CLIENTS = ([c.strip() for c in os.getenv("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
                  or ["default"])
YT_COOKIES_FILE = os.getenv("YT_DLP_COOKIES_FILE", "")

COBALT_API_URL = os.getenv("COBALT_API_URL", "").rstrip("/")
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "")

ACOUSTID_MIN_INTERVAL = max(_env_float("ACOUSTID_MIN_INTERVAL", 0.4), 0.34)
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY", "")
NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")
MATCH_THRESHOLD = 0.75
RG_TARGET_LUFS = -18.0
LRCLIB_MIN_SIMILARITY = 0.60
USER_AGENT = "Navidrome-Ingestor/2.1 (personal homelab; contact@homelab.local)"

_acoustid_lock = threading.Lock()
_acoustid_last_call = [0.0]

musicbrainzngs.set_useragent("Navidrome-Ingestor", "2.1", "contact@homelab.local")
try:
    musicbrainzngs.set_rate_limit(True)
except Exception:
    pass

log_queue = asyncio.Queue()

async def log(msg: str):
    print(msg)
    await log_queue.put(msg)

class DownloadBotError(Exception): pass
class DownloadUnavailableError(Exception): pass
class DownloadNetworkError(Exception): pass

# ================= HELPERS =================

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

def clean_title_and_artist(raw: str) -> tuple[str, str]:
    cleaned = re.sub(
        r'[\(\[\{].*?(official|music video|video|lyrics|lyric|audio|hq|hd|4k|60fps|remastered|ncs release|visualizer|clip|full album|explicit)[\)\]\}]',
        '', raw, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(official video|official music video|official audio|ncs release|lyrics|4k|hd|hq)\b',
                     '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -_[](){}:|')
    for d in [' - ', ' – ', ' — ', ' | ', ' // ', ': ']:
        if d in cleaned:
            parts = cleaned.split(d, 1)
            return parts[0].strip(), parts[1].strip()
    return "", cleaned.strip()

def string_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower().strip(), (b or "").lower().strip()).ratio()

def is_duration_match(mb_length_ms, actual_sec: float, tolerance: int = 15) -> bool:
    if not mb_length_ms:
        return True
    return abs(int(mb_length_ms) / 1000.0 - actual_sec) <= tolerance

def _acoustid_throttle():
    with _acoustid_lock:
        wait = ACOUSTID_MIN_INTERVAL - (time.time() - _acoustid_last_call[0])
        if wait > 0:
            time.sleep(wait)
        _acoustid_last_call[0] = time.time()

def cleanup_empty_parent_dirs(start_path: str):
    lib_root = os.path.abspath(NAVIDROME_LIB_DIR)
    d = os.path.dirname(os.path.abspath(start_path))
    try:
        while d.startswith(lib_root + os.sep):
            if os.listdir(d):
                break
            os.rmdir(d)
            d = os.path.dirname(d)
    except OSError:
        pass

def fetch_cover_art(release_mbid: str):
    if not release_mbid:
        return None, None
    url = f"https://coverartarchive.org/release/{release_mbid}/front"
    try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=(10, 30))
        if res.status_code == 200 and res.content:
            mime = res.headers.get("Content-Type", "image/jpeg").split(";")[0].strip().lower()
            if mime not in ("image/jpeg", "image/png"):
                mime = "image/jpeg"
            return res.content, mime
    except Exception:
        pass
    return None, None

def calculate_replaygain(file_path: str):
    try:
        cmd = ['ffmpeg', '-nostats', '-hide_banner', '-i', file_path, '-filter_complex', 'ebur128', '-f', 'null', '-']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        stderr = result.stderr
        pos = stderr.rfind("Summary:")
        segment = stderr[pos:] if pos != -1 else stderr
        matches = re.findall(r"I:\s+([-+]?\d+(?:\.\d+)?)\s+LUFS", segment)
        if not matches:
            return None
        lufs = float(matches[-1])
        gain = max(-51.0, min(51.0, RG_TARGET_LUFS - lufs))
        return f"{gain:+.2f} dB"
    except Exception as e:
        print(f"ReplayGain calculation failed: {e}")
    return None

def _lrclib_get(url: str, params: dict):
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(2):
        try:
            res = requests.get(url, params=params, headers=headers, timeout=(10, 20))
            if res.status_code == 429:
                retry_after = int(res.headers.get("Retry-After", 3))
                if attempt == 0 and retry_after <= 30:
                    time.sleep(retry_after)
                    continue
                return None
            if res.status_code == 200:
                return res.json()
            return None
        except Exception:
            return None
    return None

def fetch_lyrics(title: str, artist: str, album: str, duration_sec: float):
    try:
        data = _lrclib_get("https://lrclib.net/api/get", {
            "track_name": title, "artist_name": artist, "album_name": album, "duration": int(duration_sec)})
        if data:
            return data.get("syncedLyrics") or data.get("plainLyrics")

        results = _lrclib_get("https://lrclib.net/api/search", {"q": f"{artist} {title}"}) or []
        best, best_sim = None, 0.0
        for cand in results[:5]:
            sim = (string_similarity(title, cand.get("track_name", "")) +
                   string_similarity(artist, cand.get("artist_name", ""))) / 2.0
            if sim > best_sim:
                best, best_sim = cand, sim
        if best and best_sim >= LRCLIB_MIN_SIMILARITY:
            return best.get("syncedLyrics") or best.get("plainLyrics")
    except Exception:
        pass
    return None

# ================= RESILIENT DOWNLOADER =================

def download_via_cobalt(url: str, track_uuid: str):
    if not COBALT_API_URL:
        raise DownloadBotError("Blocked by YouTube and COBALT_API_URL not configured.")
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": USER_AGENT}
    if COBALT_API_KEY:
        headers["Authorization"] = f"Api-Key {COBALT_API_KEY}"
    payload = {"url": url, "downloadMode": "audio", "audioFormat": "opus"}
    try:
        res = requests.post(f"{COBALT_API_URL}/", json=payload, headers=headers, timeout=20)
        data = res.json() if res.text else {}
        if not res.ok or data.get("status") == "error":
            code = data.get("error", {}).get("code", f"HTTP {res.status_code}")
            raise DownloadBotError(f"Cobalt fallback failed: {code}")
        dl_url = data.get("url")
        if not dl_url:
            raise DownloadBotError(f"Cobalt returned no URL: {json.dumps(data)[:200]}")

        temp_filename = f"temp_{track_uuid}.opus"
        with requests.get(dl_url, stream=True, timeout=(15, 600)) as r:
            r.raise_for_status()
            with open(temp_filename, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return temp_filename
    except DownloadBotError:
        raise
    except Exception as e:
        raise DownloadBotError(f"Cobalt fallback failed: {e}")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=3, min=3, max=15),
    retry=retry_if_exception_type(DownloadNetworkError),
    reraise=True,
)
def download_audio_file(url: str, track_uuid: str):
    temp_filename = f"temp_{track_uuid}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{temp_filename}.%(ext)s',
        'noplaylist': True,
        'playlistend': 1,
        'extractor_args': {'youtube': {'player_client': PLAYER_CLIENTS}},
        'sleep_interval': DOWNLOAD_SLEEP_MIN,
        'max_sleep_interval': DOWNLOAD_SLEEP_MAX,
        'socket_timeout': 25,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'opus', 'preferredquality': '160'}],
        'quiet': True,
        'no_warnings': True,
    }
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        ydl_opts['cookiefile'] = YT_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return f"{temp_filename}.opus"
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if any(k in err for k in ("sign in", "403", "429", "bot", "not available", "try again later", "precondition check failed")):
            print(f"[{track_uuid[:6]}] 🛑 yt-dlp blocked. Falling back to Cobalt...")
            return download_via_cobalt(url, track_uuid)
        if any(k in err for k in ("unavailable", "private", "removed")):
            raise DownloadUnavailableError("Video unavailable or removed.")
        raise DownloadNetworkError(f"Network error during download: {e}")

# ================= MULTI-STAGE CONSENSUS ENGINE =================

def get_consensus_metadata(file_path: str, raw_title: str, audio_duration: float):
    clean_artist, clean_title = clean_title_and_artist(raw_title)
    if not clean_title:
        clean_title = raw_title

    acoustid_candidates, text_candidates = {}, {}

    if ACOUSTID_API_KEY:
        try:
            # 1. Run fpcalc manually. CPU bound, so it doesn't need the HTTP throttle limit.
            out = subprocess.check_output(['fpcalc', '-json', file_path], timeout=15)
            fp_data = json.loads(out.decode('utf-8'))
            duration_fp = fp_data.get("duration")
            fingerprint = fp_data.get("fingerprint")

            if duration_fp and fingerprint:
                # 2. Issue the API lookup (with throttle)
                _acoustid_throttle()
                url = "https://api.acoustid.org/v2/lookup"
                params = {
                    'client': ACOUSTID_API_KEY,
                    'meta': 'recordings releasegroups',
                    'duration': int(duration_fp),
                    'fingerprint': fingerprint
                }
                res = requests.get(url, params=params, timeout=10)
                res_json = res.json()

                if res_json.get("status") == "ok":
                    for result in res_json.get("results", []):
                        score = result.get("score", 0)
                        if score >= MATCH_THRESHOLD and "recordings" in result:
                            for rec in result["recordings"]:
                                recording_id = rec.get("id")
                                mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases"])
                                mb_rec = mb_data.get('recording')
                                if mb_rec and is_duration_match(mb_rec.get("length"), audio_duration):
                                    artist_name = mb_rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", "Unknown")
                                    album_name = mb_rec["release-list"][0]["title"] if mb_rec.get("release-list") else "Singles"
                                    acoustid_candidates[mb_rec["id"]] = {
                                        "id": mb_rec["id"], "title": mb_rec.get("title", ""),
                                        "artist": artist_name, "album": album_name,
                                        "score": round(score, 3), "raw_data": mb_rec,
                                    }
                else:
                    print(f"AcoustID API Error: {res_json.get('error', {}).get('message')}")
        except Exception as e:
            print(f"AcoustID lookup error: {e}")

    try:
        query = (f'recording:"{clean_title}" AND artist:"{clean_artist}"'
                 if clean_artist else f'recording:"{clean_title}"')
        for rec in musicbrainzngs.search_recordings(query=query, limit=3).get("recording-list", []):
            if is_duration_match(rec.get("length"), audio_duration):
                artist_name = rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", "Unknown")
                album_name = rec["release-list"][0]["title"] if rec.get("release-list") else "Singles"
                t_sim = string_similarity(clean_title, rec.get("title", ""))
                a_sim = string_similarity(clean_artist, artist_name) if clean_artist else 1.0
                text_candidates[rec["id"]] = {
                    "id": rec["id"], "title": rec.get("title", clean_title),
                    "artist": artist_name, "album": album_name,
                    "score": round(0.65 * t_sim + 0.35 * a_sim, 3),
                    "raw_data": rec,
                }
    except Exception as e:
        print(f"MusicBrainz text search error: {e}")

    intersecting = set(acoustid_candidates) & set(text_candidates)
    if intersecting:
        chosen_id = sorted(intersecting, key=lambda i: acoustid_candidates[i]["score"], reverse=True)[0]
        return True, acoustid_candidates[chosen_id]["raw_data"]

    if acoustid_candidates:
        best = max(acoustid_candidates.values(), key=lambda c: c["score"])
        if best["score"] >= 0.85 and (string_similarity(clean_title, best["title"]) >= 0.60 or not clean_artist):
            return True, best["raw_data"]

    if not acoustid_candidates and text_candidates:
        best_text = max(text_candidates.values(), key=lambda c: c["score"])
        if best_text["score"] >= 0.80:
            return True, best_text["raw_data"]

    all_unique = {**text_candidates, **acoustid_candidates}
    if all_unique:
        final_list = sorted(all_unique.values(), key=lambda c: c["score"], reverse=True)[:3]
        return False, final_list

    return True, None

# ================= PHASE 1: DOWNLOAD & IDENTIFY =================

async def process_track(item: dict, track_uuid: str, user_id: str):
    async with PIPELINE_SEMAPHORE:
        await _process_track_inner(item, track_uuid, user_id)

async def _process_track_inner(item: dict, track_uuid: str, user_id: str):
    url = item["url"]
    display_title = item.get("title", url)

    await log(f"[{track_uuid[:6]}] 📥 Starting: {display_title}")
    await asyncio.to_thread(database.update_track_status, track_uuid, 'DOWNLOADING')

    temp_file = None
    try:
        temp_file = await asyncio.to_thread(download_audio_file, url, track_uuid)
        if not temp_file or not os.path.exists(temp_file):
            raise Exception("File extraction failed.")

        duration = await asyncio.to_thread(lambda: OggOpus(temp_file).info.length)

        await log(f"[{track_uuid[:6]}] 🔍 Running Consensus Identification...")
        is_consensus, metadata_result = await asyncio.to_thread(
            get_consensus_metadata, temp_file, display_title, duration)

        if not is_consensus:
            await log(f"[{track_uuid[:6]}] ⚠️ Ambiguous metadata. Pending approval.")
            await asyncio.to_thread(database.update_track_metadata_choices, track_uuid, json.dumps(metadata_result), temp_file)
            return

        await _phase_2_inner(track_uuid, temp_file, metadata_result, item)

    except DownloadBotError as e:
        await asyncio.to_thread(database.update_track_status, track_uuid, 'BOT_BLOCKED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] 🛑 {e}")
    except DownloadUnavailableError:
        await asyncio.to_thread(database.update_track_status, track_uuid, 'FAILED', error_msg="Unavailable / Private")
        await log(f"[{track_uuid[:6]}] ❌ Video is private or unavailable.")
    except Exception as e:
        await asyncio.to_thread(database.update_track_status, track_uuid, 'FAILED', error_msg=str(e))
        await log(f"[{track_uuid[:6]}] ⚠️ Processing error: {e}")
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

# ================= PHASE 2: TAG, NORMALIZE, MOVE =================

def _apply_tags_and_move_sync(temp_file, target_dir, file_name, track_uuid, parsed_info, track_info, discovery_date, rg_gain, cover_data, cover_mime, lyrics):
    # This fully offloads blocking disk operations
    audio = OggOpus(temp_file)
    existing_pic = audio.get("metadata_block_picture")
    audio.delete()

    audio["title"] = [parsed_info["title"]]
    audio["artist"] = [parsed_info["artist"]]
    audio["albumartist"] = [parsed_info["albumartist"]]
    audio["album"] = [parsed_info["album"]]
    audio["date"] = [parsed_info["date"]]
    audio["comment"] = [f"Discovery Date: {discovery_date}"]
    audio["DISCOVERY_DATE"] = [discovery_date]
    audio["DISCOVERY_TIMESTAMP"] = [f"{discovery_date}T00:00:00Z"]
    audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
    audio["SOURCE_URL"] = [track_info.get("url") or track_info.get("source_url") or ""]
    
    if track_info.get("playlist_name"):
        audio["SOURCE_PLAYLIST"] = [track_info["playlist_name"]]

    if rg_gain:
        audio["replaygain_track_gain"] = [rg_gain]

    if cover_data:
        pic = Picture()
        pic.type = 3
        pic.mime = cover_mime
        pic.desc = "Front Cover"
        pic.data = cover_data
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    elif existing_pic:
        audio["metadata_block_picture"] = existing_pic

    audio.save()

    # Move logic
    os.makedirs(target_dir, exist_ok=True)
    final_path = os.path.join(target_dir, file_name)
    if os.path.exists(final_path):
        base, ext = os.path.splitext(file_name)
        final_path = os.path.join(target_dir, f"{base}_{track_uuid[:4]}{ext}")

    shutil.move(temp_file, final_path)

    try:
        mtime = datetime.strptime(discovery_date, "%Y-%m-%d").timestamp()
        os.utime(final_path, (mtime, mtime))
    except Exception:
        pass

    if lyrics:
        with open(os.path.splitext(final_path)[0] + ".lrc", "w", encoding="utf-8") as lf:
            lf.write(lyrics)
            
    return final_path


async def process_track_phase_2(track_uuid: str, temp_file: str, mb_metadata: dict, track_info: dict):
    async with PIPELINE_SEMAPHORE:
        await _phase_2_inner(track_uuid, temp_file, mb_metadata, track_info)


async def _phase_2_inner(track_uuid: str, temp_file: str, mb_metadata: dict, track_info: dict):
    try:
        user_id = track_info.get("user_id") or "admin"
        discovery_date = track_info.get("discovery_date") or datetime.now().strftime("%Y-%m-%d")
        raw_title = track_info.get("title", "Unknown Title")
        clean_artist, clean_title = clean_title_and_artist(raw_title)

        await log(f"[{track_uuid[:6]}] 🏷️ Applying Tags & ReplayGain...")
        duration = await asyncio.to_thread(lambda: OggOpus(temp_file).info.length)
        
        # Structure Fix: Let the missing matches fallback securely grouping to the Playlist Name.
        playlist_name = track_info.get("playlist_name")
        default_album = sanitize_filename(playlist_name) if playlist_name else "Unknown Album"

        parsed_info = {
            "title": clean_title or raw_title,
            "artist": clean_artist or "Unknown Artist",
            "albumartist": clean_artist or "Unknown Artist",
            "album": default_album,
            "date": discovery_date[:4],
        }

        cover_data, cover_mime = None, None
        if mb_metadata:
            parsed_info["title"] = mb_metadata.get("title", parsed_info["title"])
            if mb_metadata.get("artist-credit"):
                credit = mb_metadata["artist-credit"][0]
                if isinstance(credit, dict) and "artist" in credit:
                    parsed_info["artist"] = credit["artist"]["name"]
                    parsed_info["albumartist"] = credit["artist"]["name"]
            if mb_metadata.get("release-list"):
                release = mb_metadata["release-list"][0]
                parsed_info["album"] = release.get("title", default_album)
                if release.get("date"):
                    parsed_info["date"] = release["date"][:4]
                cover_data, cover_mime = await asyncio.to_thread(fetch_cover_art, release.get("id"))

        rg_gain = await asyncio.to_thread(calculate_replaygain, temp_file)
        lyrics = await asyncio.to_thread(fetch_lyrics, parsed_info["title"], parsed_info["artist"], parsed_info["album"], duration)

        # Structure Fix: Drop the user_id intermediary directory and map directly to Artist/Album.
        folder_artist = sanitize_filename(parsed_info["albumartist"])
        folder_album = sanitize_filename(parsed_info["album"])
        file_name = sanitize_filename(f"{parsed_info['artist']} - {parsed_info['title']}.opus")
        target_dir = os.path.join(NAVIDROME_LIB_DIR, folder_artist, folder_album)

        final_path = await asyncio.to_thread(
            _apply_tags_and_move_sync, temp_file, target_dir, file_name, track_uuid,
            parsed_info, track_info, discovery_date, rg_gain, cover_data, cover_mime, lyrics
        )

        mbid = mb_metadata.get("id") if mb_metadata else None
        matched_title = f"{parsed_info['artist']} - {parsed_info['title']}"

        await asyncio.to_thread(database.update_track_status, track_uuid, 'COMPLETED', file_path=final_path, matched_title=matched_title, mbid=mbid)
        await log(f"[{track_uuid[:6]}] ✅ Saved: {matched_title}")

        if playlist_name:
            m3u_path = await asyncio.to_thread(sync_playlist_file, playlist_name, user_id)
            if m3u_path:
                await log(f"[{track_uuid[:6]}] 📋 Updated playlist: {os.path.basename(m3u_path)}")

    except Exception as e:
        await asyncio.to_thread(database.update_track_status, track_uuid, 'FAILED', error_msg=f"Phase 2 error: {e}")
        await log(f"[{track_uuid[:6]}] ⚠️ Phase 2 Tagging error: {e}")
    finally:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass

# ================= MANUAL RETAG / OVERRIDE =================

def _retag_disk_ops_sync(old_file_path, target_dir, file_name, track_uuid, parsed_info, source_url, discovery_date, cover_data, cover_mime):
    audio = OggOpus(old_file_path)
    existing_pic = audio.get("metadata_block_picture")
    audio.delete()
    duration = audio.info.length

    audio["title"] = [parsed_info["title"]]
    audio["artist"] = [parsed_info["artist"]]
    audio["albumartist"] = [parsed_info["albumartist"]]
    audio["album"] = [parsed_info["album"]]
    audio["date"] = [parsed_info["date"]]
    audio["comment"] = [f"Discovery Date: {discovery_date}"]
    audio["DISCOVERY_DATE"] = [discovery_date]
    audio["DISCOVERY_TIMESTAMP"] = [f"{discovery_date}T00:00:00Z"]
    audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
    audio["SOURCE_URL"] = [source_url or ""]

    if cover_data:
        pic = Picture()
        pic.type = 3
        pic.mime = cover_mime
        pic.desc = "Front Cover"
        pic.data = cover_data
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    elif existing_pic:
        audio["metadata_block_picture"] = existing_pic

    rg_gain = calculate_replaygain(old_file_path)
    if rg_gain:
        audio["replaygain_track_gain"] = [rg_gain]

    audio.save()

    os.makedirs(target_dir, exist_ok=True)
    new_final_path = os.path.join(target_dir, file_name)
    if os.path.exists(new_final_path) and old_file_path != new_final_path:
        base, ext = os.path.splitext(file_name)
        new_final_path = os.path.join(target_dir, f"{base}_{track_uuid[:4]}{ext}")
        
    if old_file_path != new_final_path:
        shutil.move(old_file_path, new_final_path)
        cleanup_empty_parent_dirs(old_file_path)

    old_lrc = os.path.splitext(old_file_path)[0] + ".lrc"
    new_lrc = os.path.splitext(new_final_path)[0] + ".lrc"
    if os.path.exists(old_lrc):
        shutil.move(old_lrc, new_lrc)
    elif not os.path.exists(new_lrc):
        lyrics = fetch_lyrics(parsed_info["title"], parsed_info["artist"], parsed_info["album"], duration)
        if lyrics:
            with open(new_lrc, "w", encoding="utf-8") as lf:
                lf.write(lyrics)

    try:
        mtime = datetime.strptime(discovery_date, "%Y-%m-%d").timestamp()
        os.utime(new_final_path, (mtime, mtime))
    except Exception:
        pass

    return new_final_path

async def retag_existing_track(track_uuid: str, mbid: str = None, custom_artist: str = None, custom_title: str = None, custom_album: str = None):
    async with PIPELINE_SEMAPHORE:
        return await _retag_inner(track_uuid, mbid, custom_artist, custom_title, custom_album)

async def _retag_inner(track_uuid, mbid, custom_artist, custom_title, custom_album):
    track = await asyncio.to_thread(database.get_track_by_uuid, track_uuid)
    if not track or not track.get("file_path") or not os.path.exists(track["file_path"]):
        await log(f"[{track_uuid[:6]}] ❌ Cannot retag: file not found on disk.")
        return False

    old_file_path = track["file_path"]
    user_id = track.get("user_id") or "admin"
    discovery_date = track.get("discovery_date") or datetime.now().strftime("%Y-%m-%d")

    await log(f"[{track_uuid[:6]}] 🛠️ Manually updating metadata...")

    parsed_info = {
        "title": custom_title or track.get("title") or "Unknown Title",
        "artist": custom_artist or "Unknown Artist",
        "albumartist": custom_artist or "Unknown Artist",
        "album": custom_album or "Singles",
        "date": discovery_date[:4],
    }

    cover_data, cover_mime = None, "image/jpeg"
    if mbid:
        try:
            mb_data = await asyncio.to_thread(musicbrainzngs.get_recording_by_id, mbid.strip(), includes=["artists", "releases"])
            rec = mb_data.get('recording')
            if rec:
                parsed_info["title"] = rec.get("title", parsed_info["title"])
                if rec.get("artist-credit"):
                    credit = rec["artist-credit"][0]
                    if isinstance(credit, dict) and "artist" in credit:
                        parsed_info["artist"] = credit["artist"]["name"]
                        parsed_info["albumartist"] = credit["artist"]["name"]
                if rec.get("release-list"):
                    release = rec["release-list"][0]
                    parsed_info["album"] = release.get("title", "Singles")
                    if release.get("date"):
                        parsed_info["date"] = release["date"][:4]
                    cover_data, cover_mime = await asyncio.to_thread(fetch_cover_art, release.get("id"))
        except Exception as e:
            await log(f"[{track_uuid[:6]}] ⚠️ MusicBrainz ID lookup failed: {e}")

    if custom_title: parsed_info["title"] = custom_title
    if custom_artist:
        parsed_info["artist"] = custom_artist
        parsed_info["albumartist"] = custom_artist
    if custom_album: parsed_info["album"] = custom_album

    try:
        folder_artist = sanitize_filename(parsed_info["albumartist"])
        folder_album = sanitize_filename(parsed_info["album"])
        file_name = sanitize_filename(f"{parsed_info['artist']} - {parsed_info['title']}.opus")
        target_dir = os.path.join(NAVIDROME_LIB_DIR, folder_artist, folder_album)

        new_final_path = await asyncio.to_thread(
            _retag_disk_ops_sync, old_file_path, target_dir, file_name, track_uuid,
            parsed_info, track.get("source_url"), discovery_date, cover_data, cover_mime
        )

        matched_title = f"{parsed_info['artist']} - {parsed_info['title']}"
        await asyncio.to_thread(database.update_retag_result, track_uuid, matched_title, mbid.strip() if mbid else None, new_final_path)

        await log(f"[{track_uuid[:6]}] ✨ Tags updated & file relocated: {matched_title}")

        if track.get("playlist_name"):
            await asyncio.to_thread(sync_playlist_file, track["playlist_name"], user_id)
        return True
    except Exception as e:
        await log(f"[{track_uuid[:6]}] ❌ Retagging error: {e}")
        return False