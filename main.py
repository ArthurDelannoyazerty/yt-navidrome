import os
import json
import time
import shutil
import re
import requests
from datetime import datetime
from dotenv import load_dotenv, find_dotenv
import yt_dlp
import acoustid
import musicbrainzngs
from mutagen.oggopus import OggOpus

# --- LOAD ENVIRONMENT VARIABLES ---
load_dotenv(find_dotenv())

YT_API_KEY = os.getenv("YT_API_KEY")
PLAYLIST_ID = os.getenv("PLAYLIST_ID")
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")
ND_URL = os.getenv("ND_URL")
ND_USER = os.getenv("ND_USER")
ND_PASS = os.getenv("ND_PASS")
NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")

# --- CONFIGURATION ---
LEDGER_FILE = "ledger.json"
FAILED_FILE = "unprocessed_urls.txt"
ACOUSTID_DELAY = 0.5  # Respects the 3 requests/sec rate limit safely (2 req/sec)
MATCH_THRESHOLD = 0.6 # Minimum confidence (60%) to accept an AcoustID match

# Initialize MusicBrainz
musicbrainzngs.set_useragent("YT-Navidrome-Pipeline", "1.0", "contact@example.com")

def setup_directories():
    """Ensure library directory exists."""
    os.makedirs(NAVIDROME_LIB_DIR, exist_ok=True)

def load_ledger():
    """Load the JSON ledger mapping video IDs to process status."""
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE, "r") as f:
            return json.load(f)
    return {}

def save_ledger(ledger_data):
    """Save ledger to disk."""
    with open(LEDGER_FILE, "w") as f:
        json.dump(ledger_data, f, indent=4)

def sanitize_filename(name):
    """Remove invalid characters from filenames."""
    return re.sub(r'[\\/*?:"<>|]', "", name)

def get_playlist_items(api_key, playlist_id):
    """Fetch all items from the YouTube playlist (handles pagination)."""
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    items = []
    next_page_token = None
    
    print(f"Fetching playlist {playlist_id} from YouTube...")
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
                "raw_date": item["snippet"]["publishedAt"], # ISO 8601
                "playlist_name": "My_YouTube_Archive" # Can be dynamic if you query the playlist metadata
            })
            
        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break
            
    print(f"Found {len(items)} tracks in the playlist.")
    return items

def download_audio(video_id):
    """Download highest quality Opus audio using yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"
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
    """Fingerprint audio using AcoustID and fetch MusicBrainz metadata."""
    # Sleep to respect rate limits (Max 3 req/sec for AcoustID)
    time.sleep(ACOUSTID_DELAY)
    
    try:
        results = acoustid.match(ACOUSTID_API_KEY, file_path)
        for score, recording_id, title, artist in results:
            if score >= MATCH_THRESHOLD:
                print(f" -> AcoustID Match: '{title}' by '{artist}' (Confidence: {score*100:.1f}%)")
                mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases"])
                return mb_data['recording']
                
    except acoustid.NoBackendError:
        print(" -> Error: fpcalc not found! Please install chromaprint/fpcalc.")
    except Exception as e:
        print(f" -> Fingerprint API Error: {e}")
        
    print(" -> No reliable fingerprint match found.")
    return None

def tag_opus_file(file_path, metadata, track_index, yt_item):
    """Apply Vorbis Comments to the .opus file using Mutagen."""
    audio = OggOpus(file_path)
    audio.delete() # Clear YouTube messy tags
    
    # Chronological format logic
    dt_obj = datetime.strptime(yt_item["raw_date"], "%Y-%m-%dT%H:%M:%SZ")
    formatted_date = dt_obj.strftime("%d-%m-%Y")
    
    # Default Fallback (YouTube Data)
    final_title = yt_item["title"]
    final_artist = "Unknown Artist"
    
    if metadata:
        # Successful MusicBrainz Match
        final_title = metadata.get("title", final_title)
        
        # safely extract artist name
        if "artist-credit" in metadata and len(metadata["artist-credit"]) > 0:
             if isinstance(metadata["artist-credit"][0], dict) and "artist" in metadata["artist-credit"][0]:
                 final_artist = metadata["artist-credit"][0]["artist"]["name"]
                 
        audio["title"] = final_title
        audio["artist"] = final_artist
        
        if "release-list" in metadata and len(metadata["release-list"]) > 0:
            audio["album"] = metadata["release-list"][0].get("title", "")
            
        audio["musicbrainz_trackid"] = metadata.get("id", "")
        audio["mbid_status"] = "Yes"
    else:
        # Unrecognized logic
        audio["title"] = final_title
        audio["artist"] = final_artist
        audio["mbid_status"] = "No"

    # Custom Pipeline Tags (10-year Playlist order preservation)
    audio["tracknumber"] = str(track_index).zfill(4)
    audio["grouping"] = yt_item["playlist_name"]
    audio["yt_date_added"] = formatted_date
    
    audio.save()
    
    # Return formatted filename to use when moving the file
    safe_artist = sanitize_filename(final_artist)
    safe_title = sanitize_filename(final_title)
    return f"{safe_artist} - {safe_title}.opus"

def trigger_navidrome_scan():
    """Trigger a Quick Scan on the Navidrome server using the Subsonic API."""
    if not ND_URL:
        print("Skipping Navidrome sync (ND_URL not set).")
        return
        
    print("Triggering Navidrome Library Scan...")
    url = f"{ND_URL}/rest/startScan.view"
    params = {
        "u": ND_USER,
        "p": ND_PASS,  # NOTE: Use plain text password or token/salt depending on your Navidrome config
        "v": "1.16.1",
        "c": "yt-navidrome-pipeline",
        "f": "json"
    }
    
    try:
        response = requests.get(url, params=params)
        res_data = response.json()
        if res_data.get("subsonic-response", {}).get("status") == "ok":
            print("Navidrome scan started successfully!")
        else:
            print("Navidrome scan trigger failed:", res_data)
    except Exception as e:
        print(f"Failed to communicate with Navidrome: {e}")

def main():
    setup_directories()
    ledger = load_ledger()
    playlist_items = get_playlist_items(YT_API_KEY, PLAYLIST_ID)
    
    print("-" * 50)
    
    # Track index 1 to N to preserve the exact chronological order of the playlist
    for index, item in enumerate(playlist_items, start=1):
        vid_id = item["video_id"]
        yt_title = item["title"]
        
        print(f"[{index}/{len(playlist_items)}] Processing: {yt_title}")
        
        if vid_id in ledger:
            print(f" -> Already processed. Skipping. (Path: {ledger[vid_id]['path']})")
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
        target_filename = tag_opus_file(temp_file, mb_metadata, index, item)
        target_path = os.path.join(NAVIDROME_LIB_DIR, target_filename)
        
        # Prevent overwriting if a file with the exact same Artist - Title exists
        if os.path.exists(target_path):
            base, ext = os.path.splitext(target_filename)
            target_filename = f"{base}_{vid_id}{ext}"
            target_path = os.path.join(NAVIDROME_LIB_DIR, target_filename)
            
        print(f" -> Moving to library: {target_filename}")
        shutil.move(temp_file, target_path)
        
        # Update and save ledger
        ledger[vid_id] = {
            "yt_title": yt_title,
            "processed_at": datetime.now().isoformat(),
            "path": target_path,
            "mbid_matched": bool(mb_metadata)
        }
        save_ledger(ledger)
        print("-" * 50)
        
    # After loop finishes, sync to Navidrome
    trigger_navidrome_scan()
    print("Pipeline Execution Complete! 🚀")

if __name__ == "__main__":
    main()
