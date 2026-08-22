import os
import re
import time
import shutil
import base64
import asyncio
import subprocess
import requests
import json
import difflib
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
MATCH_THRESHOLD = 0.75
ACOUSTID_DELAY = 0.5

musicbrainzngs.set_useragent("Navidrome-Ingestor", "2.0", "contact@homelab.local")

# Asynchronous log queue for Web UI live streaming
log_queue = asyncio.Queue()

async def log(msg: str):
    print(msg)
    await log_queue.put(msg)

# Custom Exceptions
class DownloadBotError(Exception): pass
class DownloadUnavailableError(Exception): pass
class DownloadNetworkError(Exception): pass

# --- AUDIO & METADATA HELPERS ---

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

def clean_title_and_artist(raw: str) -> tuple[str, str]:
    """Strips YouTube clutter and parses into (Artist, Title)."""
    # Remove bracketed junk: [Official Video], (Lyrics), [NCS Release], etc.
    cleaned = re.sub(
        r'[\(\[\{].*?(official|music video|video|lyrics|lyric|audio|hq|hd|4k|60fps|remastered|ncs release|visualizer|clip|full album|explicit)[\)\]\}]', 
        '', raw, flags=re.IGNORECASE
    )
    # Remove standalone junk words
    cleaned = re.sub(r'\b(official video|official music video|official audio|ncs release|lyrics|4k|hd|hq)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' -_[](){}:|')

    # Split by standard artist/title delimiters
    delimiters = [' - ', ' – ', ' — ', ' | ', ' // ', ': ']
    for d in delimiters:
        if d in cleaned:
            parts = cleaned.split(d, 1)
            return parts[0].strip(), parts[1].strip()
    
    return "", cleaned.strip()

def string_similarity(a: str, b: str) -> float:
    """Calculates fuzzy string similarity ratio between 0.0 and 1.0."""
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def is_duration_match(mb_length_ms, actual_sec: float, tolerance: int = 15) -> bool:
    """Returns False if the MusicBrainz recording length differs by more than tolerance seconds."""
    if not mb_length_ms:
        return True # Cannot rule out if duration not listed in MusicBrainz
    mb_sec = int(mb_length_ms) / 1000.0
    return abs(mb_sec - actual_sec) <= tolerance

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
        # 1. Exact match
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

# --- RESILIENT DOWNLOADER ---

def download_via_cobalt(url: str, track_uuid: str):
    """Fallback engine using the Cobalt API to bypass strict IP blocks."""
    headers = {
        "Accept": "application/json", 
        "Content-Type": "application/json",
        "User-Agent": "Navidrome-Ingestor/2.0"
    }
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "opus"
    }
    try:
        res = requests.post("https://api.cobalt.tools/", json=payload, headers=headers, timeout=15)
        if not res.ok:
            raise Exception(f"HTTP {res.status_code} - {res.text}")
            
        data = res.json()
        dl_url = data.get("url")
        if not dl_url:
            raise Exception(f"Cobalt API returned no URL: {data}")
            
        temp_filename = f"temp_{track_uuid}.opus"
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
    """Downloads audio via yt-dlp with tv,mweb client spoofing."""
    temp_filename = f"temp_{track_uuid}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{temp_filename}.%(ext)s',
        'noplaylist': True,
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
        if "sign in" in err or "403" in err or "429" in err or "bot" in err:
            print(f"[{track_uuid[:6]}] 🛑 yt-dlp blocked. Falling back to Cobalt API...")
            return download_via_cobalt(url, track_uuid)
        elif "unavailable" in err or "private" in err or "removed" in err:
            raise DownloadUnavailableError("Video unavailable or removed.")
        else:
            raise DownloadNetworkError(f"Network error during download: {e}")

# --- MULTI-STAGE CONSENSUS ENGINE ---

def get_consensus_metadata(file_path: str, raw_title: str, audio_duration: float):
    """
    Cross-references AcoustID and Scoped Text Search using Duration Guards
    and ID Intersection to auto-approve matches.
    """
    clean_artist, clean_title = clean_title_and_artist(raw_title)
    if not clean_title:
        clean_title = raw_title

    acoustid_candidates = {}
    text_candidates = {}

    # 1. AcoustID Fingerprinting with Duration Guard
    if ACOUSTID_API_KEY:
        time.sleep(ACOUSTID_DELAY)
        try:
            results = acoustid.match(ACOUSTID_API_KEY, file_path)
            for score, recording_id, title, artist in results:
                if score >= MATCH_THRESHOLD:
                    mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases"])
                    rec = mb_data.get('recording')
                    if rec and is_duration_match(rec.get("length"), audio_duration):
                        artist_name = rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", artist or "Unknown")
                        album_name = rec.get("release-list", [{}])[0].get("title", "Singles") if rec.get("release-list") else "Singles"
                        acoustid_candidates[rec["id"]] = {
                            "id": rec["id"],
                            "title": rec.get("title", title),
                            "artist": artist_name,
                            "album": album_name,
                            "score": score,
                            "raw_data": rec
                        }
        except Exception as e:
            print(f"AcoustID lookup error: {e}")

    # 2. Scoped MusicBrainz Text Search with Duration Guard
    try:
        if clean_artist:
            query = f'recording:"{clean_title}" AND artist:"{clean_artist}"'
        else:
            query = f'recording:"{clean_title}"'
        
        search_res = musicbrainzngs.search_recordings(query=query, limit=3)
        for rec in search_res.get("recording-list", []):
            if is_duration_match(rec.get("length"), audio_duration):
                artist_name = rec.get("artist-credit", [{}])[0].get("artist", {}).get("name", "Unknown")
                album_name = rec.get("release-list", [{}])[0].get("title", "Singles") if rec.get("release-list") else "Singles"
                text_candidates[rec["id"]] = {
                    "id": rec["id"],
                    "title": rec.get("title", clean_title),
                    "artist": artist_name,
                    "album": album_name,
                    "raw_data": rec
                }
    except Exception as e:
        print(f"MusicBrainz text search error: {e}")

    # --- 3. CONSENSUS & INTERSECTION MATRIX ---

    # Case A: Direct MBID Intersection (Audio & Text search picked the exact same song!)
    intersecting_ids = set(acoustid_candidates.keys()).intersection(set(text_candidates.keys()))
    if intersecting_ids:
        chosen_id = list(intersecting_ids)[0]
        return True, acoustid_candidates[chosen_id]["raw_data"]

    # Case B: High AcoustID Confidence + Title String Similarity > 60%
    if acoustid_candidates:
        best_acoustic = max(acoustid_candidates.values(), key=lambda c: c["score"])
        sim = string_similarity(clean_title, best_acoustic["title"])
        if best_acoustic["score"] >= 0.85 and (sim >= 0.60 or not clean_artist):
            return True, best_acoustic["raw_data"]

    # Case C: High Text Search Confidence (AcoustID failed, but text match is strong)
    if not acoustid_candidates and text_candidates:
        best_text = list(text_candidates.values())[0]
        title_sim = string_similarity(clean_title, best_text["title"])
        artist_sim = string_similarity(clean_artist, best_text["artist"]) if clean_artist else 1.0
        if title_sim >= 0.85 and artist_sim >= 0.70:
            return True, best_text["raw_data"]

    # Case D: Ambiguity / Conflict -> Prepare clean choice list for UI
    all_unique = {**text_candidates, **acoustid_candidates}
    if all_unique:
        final_list = list(all_unique.values())[:3] # Limit to top 3
        return False, final_list

    # Case E: Zero matches anywhere -> Auto-approve with clean local tags
    return True, None


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

        # Read actual audio duration for the duration guard
        audio_check = OggOpus(temp_file)
        duration = audio_check.info.length

        await log(f"[{track_uuid[:6]}] 🔍 Running Consensus Identification...")
        is_consensus, metadata_result = await asyncio.to_thread(get_consensus_metadata, temp_file, display_title, duration)

        if not is_consensus:
            await log(f"[{track_uuid[:6]}] ⚠️ Ambiguous metadata. Pending approval.")
            choices_json = json.dumps(metadata_result)
            database.update_track_metadata_choices(track_uuid, choices_json, temp_file)
            return

        # Auto-approved! Proceed directly to Phase 2
        await process_track_phase_2(track_uuid, temp_file, metadata_result, item)

    except DownloadBotError as e:
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
        raw_title = track_info.get("title", "Unknown Title")
        
        # Clean artist and title for clean fallback tags
        clean_artist, clean_title = clean_title_and_artist(raw_title)

        await log(f"[{track_uuid[:6]}] 🏷️ Applying Tags & ReplayGain...")
        audio = OggOpus(temp_file)
        audio.delete()
        duration = audio.info.length

        parsed_info = {
            "title": clean_title or raw_title,
            "artist": clean_artist or "Unknown Artist",
            "albumartist": clean_artist or "Unknown Artist",
            "album": "Singles",
            "date": discovery_date[:4]
        }

        # Apply chosen MusicBrainz metadata if provided
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

        # Embed Vorbis tags
        audio["title"] = [parsed_info["title"]]
        audio["artist"] = [parsed_info["artist"]]
        audio["albumartist"] = [parsed_info["albumartist"]]
        audio["album"] = [parsed_info["album"]]
        audio["date"] = [parsed_info["date"]]
        audio["comment"] = [f"Discovery Date: {discovery_date}"]
        audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
        audio["DISCOVERY_DATE"] = [discovery_date]

        # Embed ReplayGain
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

        # Set filesystem mtime to Discovery Date
        try:
            dt_obj = datetime.strptime(discovery_date, "%Y-%m-%d")
            mtime = dt_obj.timestamp()
            os.utime(final_path, (mtime, mtime))
        except Exception:
            pass

        # Fetch and save Lyrics (.lrc)
        lyrics = await asyncio.to_thread(fetch_lyrics, parsed_info["title"], parsed_info["artist"], parsed_info["album"], duration)
        if lyrics:
            lrc_path = os.path.splitext(final_path)[0] + ".lrc"
            with open(lrc_path, "w", encoding="utf-8") as lf:
                lf.write(lyrics)

        # Extract MBID if present
        mbid = mb_metadata.get("id") if mb_metadata else None
        matched_title = f"{parsed_info['artist']} - {parsed_info['title']}"

        database.update_track_status(
            track_uuid, 
            'COMPLETED', 
            file_path=final_path, 
            matched_title=matched_title, 
            mbid=mbid
        )
        await log(f"[{track_uuid[:6]}] ✅ Saved: {matched_title}")

        # Sync Playlist
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

async def retag_existing_track(track_uuid: str, mbid: str = None, custom_artist: str = None, custom_title: str = None, custom_album: str = None):
    """Re-tags an existing file on disk using a specific MBID or manual text, and moves it to the right folder."""
    track = database.get_track_by_uuid(track_uuid)
    if not track or not track.get("file_path") or not os.path.exists(track["file_path"]):
        await log(f"[{track_uuid[:6]}] ❌ Cannot retag: file not found on disk.")
        return False

    old_file_path = track["file_path"]
    user_id = track.get("user_id", "admin")
    discovery_date = track.get("discovery_date") or datetime.now().strftime("%Y-%m-%d")

    await log(f"[{track_uuid[:6]}] 🛠️ Manually updating metadata...")

    parsed_info = {
        "title": custom_title or track.get("title") or "Unknown Title",
        "artist": custom_artist or "Unknown Artist",
        "albumartist": custom_artist or "Unknown Artist",
        "album": custom_album or "Singles",
        "date": discovery_date[:4]
    }

    cover_data = None
    # 1. If MusicBrainz ID is provided, fetch official data & art
    if mbid:
        try:
            mb_data = await asyncio.to_thread(musicbrainzngs.get_recording_by_id, mbid.strip(), includes=["artists", "releases"])
            rec = mb_data.get('recording')
            if rec:
                parsed_info["title"] = rec.get("title", parsed_info["title"])
                if "artist-credit" in rec and rec["artist-credit"]:
                    credit = rec["artist-credit"][0]
                    if isinstance(credit, dict) and "artist" in credit:
                        parsed_info["artist"] = credit["artist"]["name"]
                        parsed_info["albumartist"] = credit["artist"]["name"]

                if "release-list" in rec and rec["release-list"]:
                    release = rec["release-list"][0]
                    parsed_info["album"] = release.get("title", "Singles")
                    if release.get("date"):
                        parsed_info["date"] = release["date"][:4]
                    
                    # Fetch Cover Art
                    release_id = release.get("id")
                    if release_id:
                        cover_data = await asyncio.to_thread(fetch_cover_art, release_id)
        except Exception as e:
            await log(f"[{track_uuid[:6]}] ⚠️ MusicBrainz ID lookup failed: {e}")

    # Override with manual text if explicitly passed
    if custom_title: parsed_info["title"] = custom_title
    if custom_artist: 
        parsed_info["artist"] = custom_artist
        parsed_info["albumartist"] = custom_artist
    if custom_album: parsed_info["album"] = custom_album

    try:
        # 2. Write Tags into Opus File
        audio = OggOpus(old_file_path)
        audio.delete()
        duration = audio.info.length

        audio["title"] = [parsed_info["title"]]
        audio["artist"] = [parsed_info["artist"]]
        audio["albumartist"] = [parsed_info["albumartist"]]
        audio["album"] = [parsed_info["album"]]
        audio["date"] = [parsed_info["date"]]
        audio["comment"] = [f"Discovery Date: {discovery_date}"]
        audio["NAVIDROME_PIPELINE_ID"] = [track_uuid]
        audio["DISCOVERY_DATE"] = [discovery_date]

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = cover_data
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]

        # Recalculate ReplayGain
        rg_gain = await asyncio.to_thread(calculate_replaygain, old_file_path)
        if rg_gain:
            audio["replaygain_track_gain"] = [rg_gain]

        audio.save()

        # 3. Move File to New Hierarchy: user / Artist / Album / Track.opus
        folder_artist = sanitize_filename(parsed_info["albumartist"])
        folder_album = sanitize_filename(parsed_info["album"])
        file_name = sanitize_filename(f"{parsed_info['artist']} - {parsed_info['title']}.opus")

        user_dir = os.path.join(NAVIDROME_LIB_DIR, sanitize_filename(user_id))
        target_dir = os.path.join(user_dir, folder_artist, folder_album)
        os.makedirs(target_dir, exist_ok=True)

        new_final_path = os.path.join(target_dir, file_name)
        if old_file_path != new_final_path:
            shutil.move(old_file_path, new_final_path)

        # Move/Rename .lrc lyrics if exists
        old_lrc = os.path.splitext(old_file_path)[0] + ".lrc"
        new_lrc = os.path.splitext(new_final_path)[0] + ".lrc"
        if os.path.exists(old_lrc):
            shutil.move(old_lrc, new_lrc)
        else:
            # Try fetching lyrics with updated tags
            lyrics = await asyncio.to_thread(fetch_lyrics, parsed_info["title"], parsed_info["artist"], parsed_info["album"], duration)
            if lyrics:
                with open(new_lrc, "w", encoding="utf-8") as lf:
                    lf.write(lyrics)

        # Set filesystem mtime
        try:
            dt_obj = datetime.strptime(discovery_date, "%Y-%m-%d")
            mtime = dt_obj.timestamp()
            os.utime(new_final_path, (mtime, mtime))
        except Exception:
            pass

        # 4. Update Database
        matched_title = f"{parsed_info['artist']} - {parsed_info['title']}"
        actual_mbid = mbid.strip() if mbid else None

        conn = database.sqlite3.connect(database.DB_FILE)
        c = conn.cursor()
        c.execute('''
            UPDATE tracks 
            SET matched_title=?, mbid=?, file_path=?, status='COMPLETED', error_msg=NULL 
            WHERE track_uuid=?
        ''', (matched_title, actual_mbid, new_final_path, track_uuid))
        conn.commit()
        conn.close()

        await log(f"[{track_uuid[:6]}] ✨ Tags updated & file relocated: {parsed_info['artist']} - {parsed_info['title']}")

        # 5. Playlist Sync
        playlist_name = track.get("playlist_name")
        if playlist_name:
            await asyncio.to_thread(sync_playlist_file, playlist_name, user_id)

        return True
    except Exception as e:
        await log(f"[{track_uuid[:6]}] ❌ Retagging error: {e}")
        return False