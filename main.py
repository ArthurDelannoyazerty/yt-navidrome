import os
import json
import time
import shutil
import re
import requests
import base64
import subprocess
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
import yt_dlp
import acoustid
import musicbrainzngs
from mutagen.oggopus import OggOpus
from mutagen.flac import Picture

# --- LOAD ENVIRONMENT VARIABLES ---
load_dotenv(find_dotenv())

YT_API_KEY = os.getenv("YT_API_KEY")
PLAYLIST_ID = os.getenv("PLAYLIST_ID")
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")
NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")

# --- CONFIGURATION ---
LEDGER_FILE = "ledger.json"
FAILED_FILE = "unprocessed_urls.txt"
ACOUSTID_DELAY = 0.5  
MATCH_THRESHOLD = 0.6 

musicbrainzngs.set_useragent("YT-Navidrome-Pipeline", "1.4", "contact@example.com")

def setup_directories():
    os.makedirs(NAVIDROME_LIB_DIR, exist_ok=True)

def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, "r") as f:
            return json.load(f)
    return {}

def save_ledger(ledger_data):
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger_data, f, indent=4)

def sanitize_filename(name):
    # Removes invalid characters for file and folder paths
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

def get_playlist_metadata(api_key, playlist_id):
    url = "https://www.googleapis.com/youtube/v3/playlists"
    params = {"part": "snippet", "id": playlist_id, "key": api_key}
    try:
        res = requests.get(url, params=params).json()
        if "items" in res and len(res["items"]) > 0:
            return res["items"][0]["snippet"]["title"]
    except Exception as e:
        print(f"Error fetching playlist title: {e}")
    return "YouTube_Playlist"

def get_playlist_items(api_key, playlist_id):
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    items = []
    next_page_token = None
    
    print(f"Fetching playlist tracks from YouTube...")
    while True:
        params = {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": 50,
            "key": api_key
        }
        if next_page_token:
            params["pageToken"] = next_page_token
            
        response = requests.get(url, params=params).json()
        if "error" in response:
            print("YouTube API Error:", response["error"]["message"])
            break
            
        for item in response.get("items", []):
            items.append({
                "video_id": item["snippet"]["resourceId"]["videoId"],
                "title": item["snippet"]["title"],
                "raw_date": item["snippet"]["publishedAt"],
            })
            
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
            
    print(f"Found {len(items)} tracks in the playlist.")
    return items

def download_audio(video_id):
    url = f"https://youtu.be/{video_id}"
    temp_filename = f"temp_{video_id}"
    
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
    except Exception as e:
        print(f"Download Error for {video_id}: {e}")
        return None

def fingerprint_audio(file_path):
    time.sleep(ACOUSTID_DELAY)
    try:
        results = acoustid.match(ACOUSTID_API_KEY, file_path)
        for score, recording_id, title, artist in results:
            if score >= MATCH_THRESHOLD:
                print(f" -> AcoustID Match: '{title}' by '{artist}' (Confidence: {score*100:.1f}%)")
                mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases", "tags"])
                return mb_data['recording']
                
    except acoustid.NoBackendError:
        print(" -> Error: fpcalc not found! Please install chromaprint/fpcalc.")
    except Exception as e:
        print(f" -> Fingerprint API Error: {e}")
        
    print(" -> No reliable fingerprint match found.")
    return None

def fetch_cover_art(release_mbid):
    url = f"http://coverartarchive.org/release/{release_mbid}/front"
    try:
        res = requests.get(url, allow_redirects=True, timeout=10)
        if res.status_code == 200:
            return res.content
    except Exception as e:
        print(f" -> Failed to fetch cover art: {e}")
    return None

def calculate_replaygain(file_path):
    """Uses ffmpeg to calculate EBU R128 loudness and returns ReplayGain value."""
    try:
        cmd = ['ffmpeg', '-nostats', '-i', file_path, '-filter_complex', 'ebur128', '-f', 'null', '-']
        # ffmpeg logs to stderr
        result = subprocess.run(cmd, capture_output=True, text=True)
        for line in result.stderr.splitlines():
            if "I:" in line and "LUFS" in line:
                match = re.search(r"I:\s+([-+0-9.]+)\s+LUFS", line)
                if match:
                    lufs = float(match.group(1))
                    gain = -18.0 - lufs # Navidrome targets standard -18 LUFS for ReplayGain
                    return f"{gain:+.2f} dB"
    except Exception as e:
        print(f" -> ReplayGain calculation failed: {e}")
    return None

def fetch_lyrics(title, artist, album, duration_sec):
    """Fetch synced or plain lyrics from LRCLIB."""
    headers = {"User-Agent": "YT-Navidrome-Pipeline/1.4"}
    
    # Attempt 1: Exact Match using duration
    try:
        url_get = "https://lrclib.net/api/get"
        params = {"track_name": title, "artist_name": artist, "album_name": album, "duration": int(duration_sec)}
        res = requests.get(url_get, params=params, headers=headers)
        
        if res.status_code == 200:
            data = res.json()
            return data.get("syncedLyrics") or data.get("plainLyrics")
            
        # Attempt 2: Fallback to searching if exact match fails
        url_search = "https://lrclib.net/api/search"
        search_params = {"q": f"{artist} {title}"}
        res_search = requests.get(url_search, params=search_params, headers=headers)
        if res_search.status_code == 200:
            results = res_search.json()
            if results and len(results) > 0:
                # Return the top result's lyrics
                return results[0].get("syncedLyrics") or results[0].get("plainLyrics")
    except Exception as e:
        print(f" -> Lyrics fetch error: {e}")
    return None

def tag_opus_file(file_path, metadata, yt_item):
    """Tags the opus file and returns an info dictionary to help organize the folder structure."""
    audio = OggOpus(file_path)
    audio.delete()
    
    duration = audio.info.length
    
    dt_obj = datetime.strptime(yt_item["raw_date"], "%Y-%m-%dT%H:%M:%SZ")
    formatted_date = dt_obj.strftime("%Y-%m-%d")
    fallback_year = dt_obj.strftime("%Y")
    
    info = {
        "title": yt_item["title"],
        "artists": ["Unknown Artist"],
        "albumartist": "Unknown Artist",
        "album": "Unknown Album",
        "duration": duration
    }
    
    if metadata:
        info["title"] = metadata.get("title", info["title"])
        
        if "artist-credit" in metadata and len(metadata["artist-credit"]) > 0:
            artists_list = []
            for credit in metadata["artist-credit"]:
                if isinstance(credit, dict) and "artist" in credit:
                    artists_list.append(credit["artist"]["name"])
            if artists_list:
                info["artists"] = artists_list
                info["albumartist"] = artists_list[0]
                 
        audio["title"] = info["title"]
        audio["artist"] = info["artists"]
        audio["albumartist"] = info["albumartist"]
        
        if "release-list" in metadata and len(metadata["release-list"]) > 0:
            release = metadata["release-list"][0]
            info["album"] = release.get("title", "Unknown Album")
            audio["album"] = info["album"]
            
            release_date = release.get("date", "")
            audio["date"] = release_date[:4] if release_date else fallback_year
            
            release_id = release.get("id")
            if release_id:
                cover_data = fetch_cover_art(release_id)
                if cover_data:
                    pic = Picture()
                    pic.type = 3
                    pic.mime = "image/jpeg"
                    pic.desc = "Front Cover"
                    pic.data = cover_data
                    pic_data = base64.b64encode(pic.write()).decode("ascii")
                    audio["metadata_block_picture"] = [pic_data]
        else:
            audio["date"] = fallback_year

        genres = []
        if "tag-list" in metadata:
            for tag in metadata["tag-list"][:2]: 
                genres.append(tag["name"].title())
        if genres:
            audio["genre"] = genres
            
    else:
        audio["title"] = info["title"]
        audio["artist"] = info["artists"]
        audio["albumartist"] = info["albumartist"]
        audio["album"] = info["album"]
        audio["date"] = fallback_year

    # Calculate and Inject ReplayGain Loudness Data
    print(" -> Analyzing Loudness (ReplayGain)...")
    rg_gain = calculate_replaygain(file_path)
    if rg_gain:
        audio["replaygain_track_gain"] = [rg_gain]

    audio["comment"] = f"YT Added: {formatted_date}"
    audio.save()
    
    return info

def generate_m3u_playlist(playlist_name, playlist_items, ledger):
    """Generate a playlist file pointing to the new organized folder structure."""
    safe_playlist_name = sanitize_filename(playlist_name)
    m3u_path = os.path.join(NAVIDROME_LIB_DIR, f"{safe_playlist_name}.m3u")
    
    with open(m3u_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for item in playlist_items:
            vid_id = item["video_id"]
            if vid_id in ledger:
                # The relative path from the ledger (e.g. Artist/Album/Song.opus)
                # Force forward slash for universal compatibility in M3U
                rel_path = ledger[vid_id]["rel_path"].replace("\\", "/") 
                title = ledger[vid_id]["yt_title"]
                
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(f"{rel_path}\n")
                
    print(f"\nGenerated Navidrome Playlist: '{safe_playlist_name}.m3u'")

def main():
    setup_directories()
    ledger = load_ledger()
    
    yt_playlist_name = get_playlist_metadata(YT_API_KEY, PLAYLIST_ID)
    print(f"--- Playlist: {yt_playlist_name} ---")
    
    playlist_items = get_playlist_items(YT_API_KEY, PLAYLIST_ID)
    
    for index, item in enumerate(playlist_items, start=1):
        if index > 4: # Remove this line to do the whole playlist
            break
            
        vid_id = item["video_id"]
        yt_title = item["title"]
        
        print(f"[{index}/{len(playlist_items)}] Processing: {yt_title}")
        
        if vid_id in ledger:
            print(f" -> Already processed. Skipping. (Path: {ledger[vid_id]['rel_path']})")
            continue
            
        print(" -> Downloading audio...")
        temp_file = download_audio(vid_id)
        
        if not temp_file or not os.path.exists(temp_file):
            print(" -> Download failed, logging to unprocessed_urls.txt")
            with open(FAILED_FILE, "a") as f:
                f.write(f"https://youtu.be/{vid_id} - {yt_title}\n")
            continue
            
        print(" -> Fingerprinting...")
        mb_metadata = fingerprint_audio(temp_file)
        
        print(" -> Tagging...")
        tagged_info = tag_opus_file(temp_file, mb_metadata, item)
        
        # --- Create Folder Hierarchy ---
        folder_artist = sanitize_filename(tagged_info["albumartist"])
        folder_album = sanitize_filename(tagged_info["album"])
        file_title = sanitize_filename(tagged_info["title"])
        file_artist = sanitize_filename(", ".join(tagged_info["artists"]))
        
        target_filename = f"{file_artist} - {file_title}.opus"
        
        # Path relative to the Library root (used for M3U playlist and saving)
        rel_path = os.path.join(folder_artist, folder_album, target_filename)
        target_path = os.path.join(NAVIDROME_LIB_DIR, rel_path)
        
        # Avoid overriding existing files
        if os.path.exists(target_path):
            base, ext = os.path.splitext(target_filename)
            target_filename = f"{base}_{vid_id}{ext}"
            rel_path = os.path.join(folder_artist, folder_album, target_filename)
            target_path = os.path.join(NAVIDROME_LIB_DIR, rel_path)
            
        # Ensure target subdirectories exist
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
        print(f" -> Moving to library: {rel_path}")
        shutil.move(temp_file, target_path)
        
        # --- Fetch and Save Lyrics ---
        print(" -> Searching for Lyrics...")
        lyrics = fetch_lyrics(tagged_info["title"], tagged_info["albumartist"], tagged_info["album"], tagged_info["duration"])
        if lyrics:
            # Save the .lrc file right next to the .opus file
            lrc_path = os.path.splitext(target_path)[0] + ".lrc"
            with open(lrc_path, "w", encoding="utf-8") as lf:
                lf.write(lyrics)
            print(" -> Lyrics found and saved!")
        else:
            print(" -> No lyrics found.")
        
        # Update and save ledger
        ledger[vid_id] = {
            "yt_title": yt_title,
            "processed_at": datetime.now().isoformat(),
            "rel_path": rel_path,
            "mbid_matched": bool(mb_metadata)
        }
        save_ledger(ledger)
        print("-" * 50)
        
    generate_m3u_playlist(yt_playlist_name, playlist_items, ledger)
    print("Pipeline Execution Complete! 🚀")

if __name__ == "__main__":
    main()