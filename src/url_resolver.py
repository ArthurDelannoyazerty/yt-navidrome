import os
import re
import requests
import yt_dlp
from datetime import datetime

YT_API_KEY = os.getenv("YT_API_KEY", "")

class URLResolver:
    @staticmethod
    def is_spotify(url: str) -> bool:
        return "spotify.com" in url

    @staticmethod
    def is_youtube_playlist(url: str) -> bool:
        return ("youtube.com" in url or "youtu.be" in url) and ("list=" in url)

    @classmethod
    def resolve_url(cls, raw_url: str):
        raw_url = raw_url.strip()
        if not raw_url:
            return []

        if cls.is_spotify(raw_url):
            return cls._resolve_spotify(raw_url)
        elif cls.is_youtube_playlist(raw_url):
            return cls._resolve_youtube_playlist(raw_url)
        else:
            return cls._resolve_single_youtube(raw_url)

    @staticmethod
    def _resolve_single_youtube(url: str):
        today = datetime.now().strftime("%Y-%m-%d")
        return [{
            "url": url,
            "title": "YouTube Single",
            "playlist_name": None,
            "discovery_date": today
        }]

    @classmethod
    def _resolve_youtube_playlist(cls, url: str):
        """Uses official YouTube API v3 if key exists for exact added_at date; falls back to yt-dlp."""
        match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
        playlist_id = match.group(1) if match else None

        if YT_API_KEY and playlist_id:
            try:
                items = cls._resolve_youtube_playlist_via_api(playlist_id, YT_API_KEY)
                if items:
                    return items
            except Exception as e:
                print(f"YouTube Data API failed ({e}). Falling back to yt-dlp...")

        return cls._resolve_youtube_playlist_via_ytdlp(url)

    @staticmethod
    def _resolve_youtube_playlist_via_api(playlist_id: str, api_key: str):
        """Fetches items with exact playlist added_at timestamps using Google YouTube API v3."""
        # 1. Fetch Playlist Title
        pl_url = "https://www.googleapis.com/youtube/v3/playlists"
        pl_params = {"part": "snippet", "id": playlist_id, "key": api_key}
        pl_res = requests.get(pl_url, params=pl_params, timeout=8).json()
        playlist_title = "YouTube_Playlist"
        if "items" in pl_res and len(pl_res["items"]) > 0:
            playlist_title = pl_res["items"][0]["snippet"]["title"]

        # 2. Fetch All Items with snippet.publishedAt (Addition Date)
        items_url = "https://www.googleapis.com/youtube/v3/playlistItems"
        items = []
        next_page_token = None
        today = datetime.now().strftime("%Y-%m-%d")

        while True:
            params = {
                "part": "snippet",
                "playlistId": playlist_id,
                "maxResults": 50,
                "key": api_key
            }
            if next_page_token:
                params["pageToken"] = next_page_token

            res = requests.get(items_url, params=params, timeout=10).json()
            if "error" in res:
                raise Exception(res["error"]["message"])

            for item in res.get("items", []):
                snippet = item.get("snippet", {})
                vid_id = snippet.get("resourceId", {}).get("videoId")
                if not vid_id:
                    continue

                raw_date = snippet.get("publishedAt") # Exact ISO 8601 date added to playlist
                if raw_date:
                    try:
                        dt_obj = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%SZ")
                        discovery_date = dt_obj.strftime("%Y-%m-%d")
                    except ValueError:
                        discovery_date = today
                else:
                    discovery_date = today

                items.append({
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "title": snippet.get("title", "Unknown Title"),
                    "playlist_name": playlist_title,
                    "discovery_date": discovery_date
                })

            next_page_token = res.get("nextPageToken")
            if not next_page_token:
                break

        return items

    @staticmethod
    def _resolve_youtube_playlist_via_ytdlp(url: str):
        """Fallback flat-extractor via yt-dlp."""
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'skip_download': True
        }
        items = []
        today = datetime.now().strftime("%Y-%m-%d")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return []
                
                playlist_title = info.get("title", "YouTube_Playlist")
                entries = info.get("entries", [])
                
                for entry in entries:
                    if not entry:
                        continue
                    video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
                    
                    raw_date = entry.get("upload_date") or entry.get("release_date")
                    if raw_date and len(str(raw_date)) == 8:
                        d_str = str(raw_date)
                        discovery_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"
                    else:
                        discovery_date = today

                    items.append({
                        "url": video_url,
                        "title": entry.get("title", "Unknown Title"),
                        "playlist_name": playlist_title,
                        "discovery_date": discovery_date
                    })
            return items
        except Exception as e:
            raise Exception(f"YouTube/yt-dlp error: {str(e)}")

    @staticmethod
    def _get_spotify_token():
        try:
            res = requests.get("https://open.spotify.com/get_access_token", timeout=5).json()
            return res.get("accessToken")
        except Exception:
            return None

    @classmethod
    def _resolve_spotify(cls, url: str):
        today = datetime.now().strftime("%Y-%m-%d")
        token = cls._get_spotify_token()
        if not token:
            return []

        headers = {"Authorization": f"Bearer {token}"}
        items = []

        track_match = re.search(r'spotify\.com/track/([a-zA-Z0-9]+)', url)
        if track_match:
            track_id = track_match.group(1)
            res = requests.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=headers).json()
            artists = ", ".join([a["name"] for a in res.get("artists", [])])
            title = res.get("name", "")
            return [{
                "url": f"ytmsearch1:{artists} - {title}",
                "title": f"{artists} - {title}",
                "playlist_name": None,
                "discovery_date": today
            }]

        playlist_match = re.search(r'spotify\.com/playlist/([a-zA-Z0-9]+)', url)
        if playlist_match:
            playlist_id = playlist_match.group(1)
            pl_info = requests.get(f"https://api.spotify.com/v1/playlists/{playlist_id}", headers=headers).json()
            playlist_title = pl_info.get("name", "Spotify_Playlist")
            
            tracks_url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=100"
            while tracks_url:
                res = requests.get(tracks_url, headers=headers).json()
                for item in res.get("items", []):
                    track = item.get("track")
                    if not track:
                        continue
                    artists = ", ".join([a["name"] for a in track.get("artists", [])])
                    title = track.get("name", "")
                    added_at = item.get("added_at", today)[:10] # Spotify's true addition date
                    
                    items.append({
                        "url": f"ytmsearch1:{artists} - {title}",
                        "title": f"{artists} - {title}",
                        "playlist_name": playlist_title,
                        "discovery_date": added_at
                    })
                tracks_url = res.get("next")
            return items

        return []