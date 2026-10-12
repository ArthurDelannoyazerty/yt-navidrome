"""Spotify catalog/playlist snapshots; no Spotify audio is downloaded."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from common import timestamp, utcnow
from http_policy import ApiDeferred
from spotify_auth import SpotifyClient

ID = r"[A-Za-z0-9]{22}"
MAX_ITEMS = 10000


class Spotify:
    name = "spotify"

    def parse(self, url):
        from providers import SourceRef
        if url.startswith("spotify:"):
            match = re.fullmatch(rf"spotify:(track|playlist|album):({ID})", url)
            if not match:
                raise ValueError("Use a Spotify track, playlist or album URI")
            kind, identity = match.groups()
        else:
            parts = urlsplit(url)
            if parts.hostname != "open.spotify.com":
                if parts.hostname in {"spotify.link", "spotify.app.link"}:
                    raise ValueError("Open the Spotify short link, then copy its full open.spotify.com URL")
                return None
            if parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}:
                raise ValueError("Invalid Spotify URL")
            match = re.fullmatch(rf"/(?:intl-[A-Za-z-]+/)?(track|playlist|album)/({ID})/?", parts.path)
            if not match:
                raise ValueError("Use a Spotify track, playlist or album URL; artists, podcasts and local files are not supported")
            kind, identity = match.groups()
        return SourceRef(self.name, f"{kind}:{identity}",
                         f"https://open.spotify.com/{kind}/{identity}", kind != "track")

    @staticmethod
    def _page_items(client, path):
        """Construct pagination ourselves; never send bearer tokens to next URLs."""
        offset, expected = 0, None
        while True:
            page = client.get(path, limit=50, offset=offset)
            items, total = page.get("items"), page.get("total")
            if (not isinstance(items, list) or len(items) > 50
                    or type(total) is not int or not 0 <= total <= MAX_ITEMS
                    or page.get("offset") != offset or "next" not in page):
                raise ApiDeferred("Spotify returned an incomplete or oversized page; previous memberships were retained")
            if expected is None:
                expected = total
            if total != expected or offset + len(items) > total:
                raise ApiDeferred("Spotify collection changed during pagination; retrying a complete snapshot")
            yield offset, items
            offset += len(items)
            next_url = page["next"]
            if next_url is None:
                if offset != total:
                    raise ApiDeferred("Spotify pagination ended before all items arrived")
                return
            if not items or offset >= total or not isinstance(next_url, str):
                raise ApiDeferred("Spotify returned inconsistent pagination")
            parts = urlsplit(next_url)
            query = parse_qs(parts.query)
            if (parts.scheme != "https" or parts.netloc != "api.spotify.com"
                    or parts.path != "/v1/" + path or parts.fragment
                    or query.get("offset") != [str(offset)]):
                raise ApiDeferred("Spotify returned an unsafe or repeated pagination link")

    @staticmethod
    def _entry(obj, position, added_at, *, direct_id=None):
        from providers import Entry
        if obj is None:
            return None
        if not isinstance(obj, dict):
            raise ApiDeferred("Spotify returned an invalid item")
        if obj.get("is_local") or obj.get("type") in {"episode", "ad"}:
            return None
        if obj.get("type") != "track":
            raise ApiDeferred("Spotify returned an unsupported item type")
        identity = direct_id or (obj.get("linked_from") or {}).get("id") or obj.get("id")
        if identity is None:  # Removed/local catalog entry without a stable ID.
            return None
        if not isinstance(identity, str) or not re.fullmatch(ID, identity):
            raise ApiDeferred("Spotify returned an invalid track identity")
        artists = obj.get("artists")
        if not obj.get("name") or not isinstance(artists, list) or not artists:
            return None
        names = [artist.get("name") for artist in artists if isinstance(artist, dict) and artist.get("name")]
        if not names:
            return None
        added = None
        if added_at:
            try:
                added = timestamp(added_at)
            except (ValueError, TypeError, AttributeError):
                raise ApiDeferred("Spotify returned an invalid addition timestamp; no replacement date was invented") from None
        title = ", ".join(names) + " - " + obj["name"]
        # Position is an occurrence ID within the full snapshot, not track identity.
        # Duplicate playlist occurrences remain separate memberships.
        return Entry(identity, f"https://open.spotify.com/track/{identity}", title,
                     added, f"spotify:{position}", position, "playlist_added", False)

    def resolve(self, source, http):
        from providers import Entry, Snapshot
        kind, identity = source["source_key"].split(":", 1)
        if kind not in {"track", "playlist", "album"} or not re.fullmatch(ID, identity):
            raise ValueError("Invalid Spotify source")
        client = SpotifyClient(source["user_id"], http)
        if kind == "track":
            obj = client.get("tracks/" + identity)
            entry = self._entry(obj, 0, None, direct_id=identity)
            if not entry:
                raise ValueError("This Spotify track is no longer available as catalog metadata")
            observed = Entry(entry.key, entry.url, entry.title, utcnow(), entry.entry_id,
                             0, "first_seen", False)
            return Snapshot(entry.title, [observed])
        resource = ("playlists/" if kind == "playlist" else "albums/") + identity
        metadata = client.get(resource)
        title = metadata.get("name")
        if not isinstance(title, str) or not title:
            raise ApiDeferred("Spotify did not return source metadata")
        revision = metadata.get("snapshot_id")
        if kind == "playlist" and not revision:
            raise ApiDeferred("Spotify did not return a playlist revision; incomplete snapshot rejected")
        entries, skipped = [], 0
        path = resource + ("/items" if kind == "playlist" else "/tracks")
        for offset, page in self._page_items(client, path):
            for index, row in enumerate(page):
                if kind == "playlist":
                    if not isinstance(row, dict):
                        raise ApiDeferred("Spotify returned an invalid playlist occurrence")
                    obj = row.get("item") if "item" in row else row.get("track")
                    obj = None if row.get("is_local") else obj
                    added = row.get("added_at")
                else:
                    obj, added = row, None  # Album release dates are not discovery.
                entry = self._entry(obj, offset + index, added)
                if entry:
                    entries.append(entry)
                else:
                    skipped += 1
        if kind == "playlist":
            after = client.get(resource, fields="snapshot_id")
            if after.get("snapshot_id") != revision:
                raise ApiDeferred("Spotify playlist changed during synchronization; previous snapshot retained")
        if skipped:
            http.report("WARNING", f"Skipped {skipped} Spotify local, podcast, removed or incomplete item(s)")
        return Snapshot(title, entries)
