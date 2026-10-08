"""Provider boundary: downstream code consumes source-neutral snapshots."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from common import timestamp, utcnow
from http_policy import ApiDeferred, HttpPolicy


@dataclass(frozen=True)
class SourceRef:
    provider: str
    key: str
    url: str
    is_playlist: bool


@dataclass(frozen=True)
class Entry:
    key: str
    url: str
    title: str
    added_at: str | None
    entry_id: str
    position: int
    basis: str = "playlist_added"
    downloadable: bool = True


@dataclass(frozen=True)
class Snapshot:
    title: str
    entries: list[Entry]


class YouTube:
    name = "youtube"
    hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

    def parse(self, url: str) -> SourceRef | None:
        parts = urlsplit(url)
        if parts.hostname not in self.hosts:
            return None
        if parts.scheme not in {"http", "https"} or parts.username or parts.port not in {None, 80, 443}:
            raise ValueError("Invalid source URL")
        query = parse_qs(parts.query)
        if playlist := query.get("list", [None])[0]:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", playlist) or playlist.startswith(("RD", "UL")):
                raise ValueError("Radio mixes and unbounded auto-playlists are not supported")
            return SourceRef(self.name, "playlist:" + playlist,
                             "https://www.youtube.com/playlist?list=" + playlist, True)
        video = query.get("v", [None])[0]
        if parts.hostname == "youtu.be":
            video = parts.path.strip("/")
        elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
            video = parts.path.split("/")[2]
        if not video or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            raise ValueError("Use a video or playlist URL, not a channel or search URL")
        return SourceRef(self.name, "video:" + video,
                         "https://www.youtube.com/watch?v=" + video, False)

    def resolve(self, source: dict, http: HttpPolicy) -> Snapshot:
        key = os.getenv("YT_API_KEY", "").strip()
        if not key:
            raise ApiDeferred("YT_API_KEY is not configured; no date fallback is permitted")

        def get(resource, **params):
            return http.get_json(
                "https://www.googleapis.com/youtube/v3/" + resource,
                params=params,
                headers={
                    "X-Goog-Api-Key": key,
                    "User-Agent": os.getenv("HTTP_USER_AGENT", "Music-Ingestor/0.3"),
                },
            )

        identity = source["source_key"].split(":", 1)[1]
        if not source["is_playlist"]:
            data = get("videos", part="snippet", id=identity)
            if not data.get("items"):
                raise ApiDeferred("Video is missing, private, or unavailable to the API")
            title = data["items"][0]["snippet"]["title"]
            observed = utcnow()
            return Snapshot(title, [Entry(identity, source["url"], title, observed,
                                          identity, 0, "first_seen", True)])

        metadata = get("playlists", part="snippet", id=identity)
        if not metadata.get("items"):
            raise ApiDeferred("Playlist is missing, private, or unavailable to this API key")
        title = metadata["items"][0]["snippet"]["title"]
        entries: list[Entry] = []
        token = None
        seen: set[str] = set()
        while True:
            params = {"part": "snippet", "playlistId": identity, "maxResults": 50}
            if token:
                params["pageToken"] = token
            page = get("playlistItems", **params)
            for obj in page.get("items", []):
                snippet = obj["snippet"]
                name = snippet.get("title", "")
                video = snippet.get("resourceId", {}).get("videoId")
                if not video or name.lower().strip("[]") in {"private video", "deleted video"}:
                    http.report("WARNING", f"Unavailable playlist entry skipped: {name}")
                    continue
                added = timestamp(snippet["publishedAt"])
                entries.append(Entry(
                    video,
                    "https://www.youtube.com/watch?v=" + video,
                    name,
                    added,
                    obj["id"],
                    int(snippet["position"]),
                    "playlist_added",
                    True,
                ))
            token = page.get("nextPageToken")
            if not token:
                return Snapshot(title, entries)
            if token in seen:
                raise ApiDeferred("Source API repeated a page token; incomplete snapshot rejected")
            seen.add(token)


PROVIDERS = {"youtube": YouTube()}


def parse_source(url: str) -> SourceRef:
    for provider in PROVIDERS.values():
        if ref := provider.parse(url.strip()):
            return ref
    raise ValueError(
        "Unsupported source. Additional providers can implement parse() and resolve(); "
        "Spotify is not enabled yet."
    )
