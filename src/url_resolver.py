import os
import re
import time
import requests
import yt_dlp
from urllib.parse import quote_plus
from datetime import datetime

YT_API_KEY = os.getenv("YT_API_KEY", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
YTM_SEARCH_MODE = os.getenv("YTM_SEARCH_MODE", "music")  # "music" | "ytsearch"
HTTP_TIMEOUT = 12
USER_AGENT = "Navidrome-Ingestor/2.1 (personal homelab)"

# ---- Spotify token cache (Client Credentials, TTL 3600s) ----
_spotify_token_cache = {"token": None, "exp": 0.0}


class ResolverError(Exception):
    """Raised with a user-facing message; logged by the ingestion loop."""


class URLResolver:
    # ---------------- Dispatch ----------------

    @staticmethod
    def is_spotify(url: str) -> bool:
        return "spotify.com" in url

    @staticmethod
    def is_youtube_playlist(url: str) -> bool:
        return ("youtube.com" in url or "youtu.be" in url) and ("list=" in url)

    @classmethod
    def resolve_url(cls, raw_url: str):
        raw_url = (raw_url or "").strip()
        if not raw_url:
            return []
        if cls.is_spotify(raw_url):
            return cls._resolve_spotify(raw_url)
        elif cls.is_youtube_playlist(raw_url):
            return cls._resolve_youtube_playlist(raw_url)
        else:
            return cls._resolve_single_youtube(raw_url)

    # ---------------- Helpers ----------------

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _parse_iso_date(raw: str) -> str:
        """Handles both '2020-01-01T12:00:00Z' and '...00.000Z' variants."""
        if not raw:
            return URLResolver._today()
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return URLResolver._today()

    @staticmethod
    def _is_placeholder_video(title: str) -> bool:
        t = (title or "").strip().lower()
        return t.startswith("[private video]") or t.startswith("[deleted video]")

    @classmethod
    def _build_search_url(cls, artists: str, title: str) -> str:
        query = f"{artists} - {title}".strip(" -")
        if YTM_SEARCH_MODE == "ytsearch":
            return f"ytsearch1:{query}"
        # YouTube Music search, locked to the SONGS tab -> official/topic releases
        return f"https://music.youtube.com/search?q={quote_plus(query)}#songs"

    # ---------------- Single YouTube link ----------------

    @classmethod
    def _resolve_single_youtube(cls, url: str):
        return [{
            "url": url,
            "title": "YouTube Single",
            "playlist_name": None,
            "discovery_date": cls._today(),
        }]

    # ---------------- YouTube playlists ----------------

    @classmethod
    def _resolve_youtube_playlist(cls, url: str):
        match = re.search(r'[?&]list=([a-zA-Z0-9_-]+)', url)
        playlist_id = match.group(1) if match else None

        if playlist_id and playlist_id.startswith(("RD", "UL")):
            raise ResolverError("Radio mixes / auto-playlists are unbounded and unsupported.")

        if YT_API_KEY and playlist_id:
            try:
                items = cls._resolve_youtube_playlist_via_api(playlist_id)
                if items:
                    return items
            except ResolverError:
                raise
            except Exception as e:
                print(f"YouTube Data API failed ({e}). Falling back to yt-dlp...")

        return cls._resolve_youtube_playlist_via_ytdlp(url)

    @staticmethod
    def _resolve_youtube_playlist_via_api(playlist_id: str):
        # 1 unit | 50 items/page => 500-track playlist ~= 11 of 10,000 daily units
        pl_res = requests.get(
            "https://www.googleapis.com/youtube/v3/playlists",
            params={"part": "snippet", "id": playlist_id, "key": YT_API_KEY},
            timeout=HTTP_TIMEOUT).json()
        playlist_title = "YouTube_Playlist"
        if pl_res.get("items"):
            playlist_title = pl_res["items"][0]["snippet"]["title"]

        items, next_page_token = [], None
        while True:
            params = {"part": "snippet", "playlistId": playlist_id,
                      "maxResults": 50, "key": YT_API_KEY}
            if next_page_token:
                params["pageToken"] = next_page_token
            res = requests.get("https://www.googleapis.com/youtube/v3/playlistItems",
                               params=params, timeout=HTTP_TIMEOUT).json()
            if "error" in res:
                raise Exception(res["error"].get("message", "API error"))

            for item in res.get("items", []):
                snippet = item.get("snippet", {})
                vid_id = snippet.get("resourceId", {}).get("videoId")
                title = snippet.get("title", "")
                if not vid_id or URLResolver._is_placeholder_video(title):
                    continue  # private/deleted entries: don't waste a pipeline cycle
                items.append({
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "title": title,
                    "playlist_name": playlist_title,
                    "discovery_date": URLResolver._parse_iso_date(snippet.get("publishedAt")),
                })
            next_page_token = res.get("nextPageToken")
            if not next_page_token:
                break
        return items

    @staticmethod
    def _resolve_youtube_playlist_via_ytdlp(url: str):
        ydl_opts = {"extract_flat": True, "quiet": True, "no_warnings": True, "skip_download": True}
        items = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return []
                playlist_title = info.get("title", "YouTube_Playlist")
                for entry in info.get("entries", []):
                    if not entry or URLResolver._is_placeholder_video(entry.get("title")):
                        continue
                    video_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}"
                    raw_date = entry.get("upload_date") or entry.get("release_date")
                    if raw_date and len(str(raw_date)) == 8:
                        d = str(raw_date)
                        discovery_date = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    else:
                        discovery_date = URLResolver._today()
                    items.append({
                        "url": video_url,
                        "title": entry.get("title", "Unknown Title"),
                        "playlist_name": playlist_title,
                        "discovery_date": discovery_date,
                    })
            return items
        except Exception as e:
            raise ResolverError(f"YouTube/yt-dlp error: {e}")

    # ---------------- Spotify (Client Credentials, 2026-compliant) ----------------

    @classmethod
    def _get_spotify_token(cls) -> str:
        now = time.time()
        if cls._spotify_token_cache["token"] and now < cls._spotify_token_cache["exp"] - 60:
            return cls._spotify_token_cache["token"]

        if not (SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
            raise ResolverError(
                "Spotify: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not configured "
                "(the old open.spotify.com/get_access_token endpoint is dead).")

        res = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
            timeout=HTTP_TIMEOUT)
        if res.status_code == 401:
            raise ResolverError("Spotify rejected credentials (check Client ID/Secret; "
                                "Dev Mode requires the OWNER account to have Premium).")
        res.raise_for_status()
        data = res.json()
        cls._spotify_token_cache["token"] = data["access_token"]
        cls._spotify_token_cache["exp"] = now + int(data.get("expires_in", 3600))
        return cls._spotify_token_cache["token"]

    @classmethod
    def _spotify_request(cls, url: str, headers: dict, params: dict = None):
        """GET with 429/Retry-After compliance (rolling 30s window + quota buckets)."""
        for attempt in range(4):
            res = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            if res.status_code != 429:
                res.raise_for_status()
                return res.json()
            retry_after = int(res.headers.get("Retry-After", 5))
            body = {}
            try:
                body = res.json()
            except Exception:
                pass
            reason = body.get("error", {}).get("reason", "")
            if reason == "QUOTA_EXCEEDED":
                raise ResolverError("Spotify DEV-MODE QUOTA exhausted for today "
                                    "(per-account bucket, July 2026 rules). Try again tomorrow.")
            if attempt < 3:
                print(f"Spotify 429 rate-limited; sleeping {retry_after}s...")
                time.sleep(min(retry_after, 60))
        raise ResolverError("Spotify still rate-limiting after retries.")

    @classmethod
    def _resolve_spotify(cls, url: str):
        token = cls._get_spotify_token()
        headers = {"Authorization": f"Bearer {token}"}

        track_match = re.search(r'spotify\.com/track/([a-zA-Z0-9]+)', url)
        if track_match:
            res = cls._spotify_request(
                f"https://api.spotify.com/v1/tracks/{track_match.group(1)}", headers)
            artists = ", ".join(a["name"] for a in res.get("artists", []))
            title = res.get("name", "")
            return [{
                "url": cls._build_search_url(artists, title),
                "title": f"{artists} - {title}",
                "playlist_name": None,
                "discovery_date": cls._today(),
            }]

        playlist_match = re.search(r'spotify\.com/playlist/([a-zA-Z0-9]+)', url)
        if playlist_match:
            pid = playlist_match.group(1)
            pl = cls._spotify_request(f"https://api.spotify.com/v1/playlists/{pid}", headers)
            playlist_title = pl.get("name", "Spotify_Playlist")

            items, next_url = [], f"https://api.spotify.com/v1/playlists/{pid}/tracks?limit=100"
            while next_url:
                res = cls._spotify_request(next_url, headers)
                for entry in res.get("items", []):
                    track = entry.get("track")
                    if not track:
                        continue
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    title = track.get("name", "")
                    items.append({
                        "url": cls._build_search_url(artists, title),
                        "title": f"{artists} - {title}",
                        "playlist_name": playlist_title,
                        # Spotify gives the TRUE addition timestamp
                        "discovery_date": (entry.get("added_at") or cls._today())[:10],
                    })
                next_url = res.get("next")
            return items

        return []