import re
import requests
import yt_dlp
from datetime import datetime

class URLResolver:
    @staticmethod
    def is_spotify(url: str) -> bool:
        return "spotify.com" in url

    @staticmethod
    def is_youtube_playlist(url: str) -> bool:
        return ("youtube.com" in url or "youtu.be" in url) and ("list=" in url)

    @classmethod
    def resolve_url(cls, raw_url: str):
        """
        Takes any URL (YT video, YT playlist, Spotify track/playlist/album)
        and yields dicts: {"url": str, "title": str, "playlist_name": str, "discovery_date": str}
        """
        raw_url = raw_url.strip()
        if not raw_url:
            return []

        if cls.is_spotify(raw_url):
            return cls._resolve_spotify(raw_url)
        elif cls.is_youtube_playlist(raw_url):
            return cls._resolve_youtube_playlist(raw_url)
        else:
            # Single YouTube Video or raw search query
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

    @staticmethod
    def _resolve_youtube_playlist(url: str):
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'no_warnings': True,
            'skip_download': True
        }
        items = []
        today = datetime.now().strftime("%Y-%m-%d")
        
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
                items.append({
                    "url": video_url,
                    "title": entry.get("title", "Unknown Title"),
                    "playlist_name": playlist_title,
                    "discovery_date": today
                })
        return items

    @staticmethod
    def _get_spotify_token():
        """Fetches an anonymous guest access token from Spotify."""
        try:
            res = requests.get("https://open.spotify.com/get_access_token", timeout=5).json()
            return res.get("accessToken")
        except Exception:
            return None

    @classmethod
    def _resolve_spotify(cls, url: str):
        """Resolves Spotify tracks, albums, or playlists into YouTube search queries."""
        today = datetime.now().strftime("%Y-%m-%d")
        token = cls._get_spotify_token()
        if not token:
            print("Failed to get anonymous Spotify token.")
            return []

        headers = {"Authorization": f"Bearer {token}"}
        items = []

        # 1. Spotify Track
        track_match = re.search(r'spotify\.com/track/([a-zA-Z0-9]+)', url)
        if track_match:
            track_id = track_match.group(1)
            res = requests.get(f"https://api.spotify.com/v1/tracks/{track_id}", headers=headers).json()
            artists = ", ".join([a["name"] for a in res.get("artists", [])])
            title = res.get("name", "")
            search_query = f"ytmsearch1:{artists} - {title}"
            return [{
                "url": search_query,
                "title": f"{artists} - {title}",
                "playlist_name": None,
                "discovery_date": today
            }]

        # 2. Spotify Playlist
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
                    added_at = item.get("added_at", today)[:10] # ISO date to YYYY-MM-DD
                    
                    search_query = f"ytsearch1:{artists} - {title}"
                    items.append({
                        "url": search_query,
                        "title": f"{artists} - {title}",
                        "playlist_name": playlist_title,
                        "discovery_date": added_at
                    })
                tracks_url = res.get("next")
            return items

        return []