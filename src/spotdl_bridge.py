"""Metadata-only spotDL adapter, executed by the isolated spotDL interpreter.

The application never imports spotDL. Count-complete public documents are checked
twice; larger collections use the SpotAPI reader shipped with spotDL's SpotipyFree
backend before its lossy formatting step. This preserves playlist occurrences,
dates and skipped items, and rejects incomplete snapshots rather than replacing
valid memberships with a preview.

No downloader, lyrics provider, login flow or user configuration is initialized.
All machine-readable output goes to --output, including controlled failures.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCHEMA_VERSION = 1
PAGE_SIZE = 100
MAX_ITEMS = 10000
MAX_PUBLIC_DOCUMENT = 8 * 1024 * 1024
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}\Z")
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


class BridgeError(Exception):
    def __init__(self, message, code="incomplete_snapshot", retryable=True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _PublicDocumentUnsupported(Exception):
    """The public document cannot prove it contains the whole collection."""


class _InitialState(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts = []
        self.found = False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "initialState":
            if self.found:
                raise _PublicDocumentUnsupported("Duplicate public entity state")
            self.capture = self.found = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def _entity_from_document(document, kind, identity):
    """Read only the public entity. Never use session, tokens or embedded code."""
    try:
        parser = _InitialState()
        parser.feed(document)
        if not parser.found:
            raise _PublicDocumentUnsupported("No public entity state")
        decoded = base64.b64decode("".join(parser.parts), validate=True)
        state = json.loads(decoded)
        entity = state["entities"]["items"][f"spotify:{kind}:{identity}"]
        if not isinstance(entity, dict) or entity.get("uri") != f"spotify:{kind}:{identity}":
            raise _PublicDocumentUnsupported("Missing public entity")
        return entity
    except (KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise _PublicDocumentUnsupported("Unsupported public entity document") from exc


def _public_entity(url):
    """A bounded ordinary HTTPS read; no web-player API, auth or script execution."""
    import requests

    kind, identity = canonical_spotify(url)
    with requests.get(url, timeout=(8, 20), stream=True, allow_redirects=False) as response:
        if response.status_code == 404:
            raise BridgeError("This Spotify collection is private or unavailable.", "unavailable", False)
        response.raise_for_status()
        if response.is_redirect or "text/html" not in response.headers.get("Content-Type", ""):
            raise _PublicDocumentUnsupported("Public document not available")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            body.extend(chunk)
            if len(body) > MAX_PUBLIC_DOCUMENT:
                raise _PublicDocumentUnsupported("Public document too large")
    return _entity_from_document(body.decode("utf-8"), kind, identity)


def _document_snapshot(entity, kind, identity):
    """Only accept an independently count-complete, non-paginated document."""
    if (entity.get("__typename") != ("Playlist" if kind == "playlist" else "Album")
            or entity.get("uri") != f"spotify:{kind}:{identity}" or not _name(entity.get("name"))):
        raise _PublicDocumentUnsupported("Public collection metadata missing")
    content = entity.get("content" if kind == "playlist" else "tracksV2")
    if not isinstance(content, dict):
        raise _PublicDocumentUnsupported("Public collection content missing")
    total, rows, paging = content.get("totalCount"), content.get("items"), content.get("pagingInfo")
    if (type(total) is not int or not 0 <= total <= MAX_ITEMS or not isinstance(rows, list)
            or len(rows) != total or not isinstance(paging, dict)
            or "nextOffset" not in paging or paging["nextOffset"] is not None):
        raise _PublicDocumentUnsupported("Public document contains only a preview or page")
    entries, normalized, skipped = [], [], 0
    occurrence_ids = set()
    for position, row in enumerate(rows):
        row = _mapping(row, "Spotify returned an invalid public occurrence.")
        uid = row.get("uid")
        if uid is not None:
            if not isinstance(uid, str) or not uid or uid in occurrence_ids:
                raise BridgeError("Spotify returned a repeated public occurrence.")
            occurrence_ids.add(uid)
        entry = _entry(row, position, kind=kind)
        if entry is None:
            skipped += 1
            # Explicit skipped items are part of stability, too. Unlike songs,
            # these rows cannot safely be reduced to a catalog identity.
            normalized.append({"position": position, "skipped": row})
        else:
            entries.append(entry)
            normalized.append({"entry": entry, "uid": uid})
    stable = {"uri": entity["uri"], "title": entity["name"], "total": total, "items": normalized}
    digest = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"provider": "spotify", "title": entity["name"], "complete": True,
            "revision": "html-sha256:" + digest, "total_items": total,
            "skipped": skipped, "entries": entries, "scan_method": "public_document"}


def _scan_public_document(url):
    kind, identity = canonical_spotify(url)
    first = _document_snapshot(_public_entity(url), kind, identity)
    try:
        second = _document_snapshot(_public_entity(url), kind, identity)
    except _PublicDocumentUnsupported as exc:
        raise BridgeError("Spotify changed or returned an incomplete second document; snapshot retained.") from exc
    if first["revision"] != second["revision"]:
        raise BridgeError("Spotify changed between complete public documents; snapshot retained.")
    return {**second, "url": url}


def canonical_spotify(url, *, track_only=False):
    """The parent normalizes input too; never trust an arbitrary process URL."""
    try:
        parts = urlsplit(url)
        match = re.fullmatch(r"/(track|playlist|album)/([A-Za-z0-9]{22})", parts.path)
        valid = (parts.scheme == "https" and parts.netloc == "open.spotify.com"
                 and not parts.query and not parts.fragment and match)
    except (TypeError, ValueError):
        valid = False
    if not valid or (track_only and match[1] != "track"):
        raise BridgeError("Expected a canonical Spotify track, album or playlist URL.",
                          "invalid_url", False)
    return match[1], match[2]


def canonical_youtube(url):
    try:
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if (parts.scheme != "https" or parts.username or parts.password
                or parts.port not in {None, 443} or parts.fragment):
            raise ValueError
        if parts.hostname in {"www.youtube.com", "youtube.com", "music.youtube.com"}:
            values = query.get("v", [])
            if parts.path != "/watch" or len(values) != 1 or "list" in query:
                raise ValueError
            video_id = values[0]
        elif parts.hostname == "youtu.be":
            video_id = parts.path.removeprefix("/")
        else:
            raise ValueError
        if not YOUTUBE_ID.fullmatch(video_id):
            raise ValueError
    except (TypeError, ValueError):
        raise BridgeError("spotDL returned an invalid YouTube audio match.",
                          "invalid_match", False) from None
    return "https://www.youtube.com/watch?v=" + video_id


def _mapping(value, message):
    if not isinstance(value, dict):
        raise BridgeError(message)
    return value


def _name(value):
    return value.strip() if isinstance(value, str) else ""


def _artists(value):
    if isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, list):
        return []
    result = []
    for artist in value:
        if not isinstance(artist, dict):
            continue
        artist = artist.get("data", artist)
        if not isinstance(artist, dict):
            continue
        profile = artist.get("profile") or {}
        name = _name(artist.get("name")) or _name(profile.get("name"))
        if name:
            result.append(name)
    return result


def _added_at(value):
    if isinstance(value, dict):
        value = value.get("isoString")
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError
        return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        raise BridgeError("Spotify returned an invalid playlist addition date.") from None


def _duration(*objects):
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        value = obj.get("duration_ms")
        if value is None:
            for key in ("trackDuration", "duration", "durationTrait"):
                nested = obj.get(key)
                if isinstance(nested, dict) and "totalMilliseconds" in nested:
                    value = nested["totalMilliseconds"]
                    break
        if value is not None:
            if (type(value) not in {int, float} or not math.isfinite(value)
                    or value < 0):
                raise BridgeError("Spotify returned an invalid track duration.")
            return value / 1000
    return None


def _entry(row, position, *, kind="playlist", direct_id=None):
    """Normalize a raw occurrence without throwing away unrecognized rows."""
    row = _mapping(row, "Spotify returned an invalid playlist occurrence.")
    added = _added_at(row.get("added_at", row.get("addedAt"))) if kind == "playlist" else None
    if row.get("is_local"):
        return None
    # Public web-player schema, with support for the prior Track schema and
    # the official API's track/item envelopes used in existing fixtures.
    v3 = row.get("itemV3")
    v2 = row.get("itemV2")
    if v3 is not None or v2 is not None:
        envelope = _mapping(v2 if v2 is not None else v3,
                            "Spotify returned an invalid track envelope.")
        if "data" not in envelope:
            raise BridgeError("Spotify omitted the track envelope data; snapshot retained.")
        obj = envelope["data"]
        identity = (v3.get("data") or {}) if isinstance(v3, dict) else {}
    elif "track" in row or "item" in row:
        obj = row.get("item") if "item" in row else row.get("track")
        identity = {}
    else:
        if kind == "playlist":
            raise BridgeError("Spotify omitted the playlist track envelope; snapshot retained.")
        obj, identity = row, {}
    if obj is None:
        return None  # Explicit unavailable/removed occurrence.
    obj = _mapping(obj, "Spotify returned an invalid track.")
    identity = _mapping(identity, "Spotify returned an invalid track identity.")
    uri = obj.get("uri") or identity.get("uri") or ""
    typename = obj.get("__typename")
    if (obj.get("is_local") or obj.get("type") in {"episode", "ad"}
            or typename in {"Episode", "Chapter", "Audiobook", "NotFound",
                            "RestrictedContent", "LocalTrack", "UnknownTypeWrapper"}
            or (isinstance(uri, str) and uri.startswith(("spotify:local:",
                                                        "spotify:episode:",
                                                        "spotify:chapter:")))):
        return None
    playability = obj.get("playability") or {}
    if not isinstance(playability, dict):
        raise BridgeError("Spotify returned invalid playability metadata.")
    if obj.get("is_playable") is False or playability.get("playable") is False:
        return None
    if not obj:
        raise BridgeError("Spotify returned an unrecognized empty track.")
    track_id = direct_id or (obj.get("linked_from") or {}).get("id") or obj.get("id")
    if track_id is None and isinstance(uri, str) and uri.startswith("spotify:track:"):
        track_id = uri.removeprefix("spotify:track:")
    if not isinstance(track_id, str) or not SPOTIFY_ID.fullmatch(track_id):
        raise BridgeError("Spotify returned a track without a valid identity.")
    trait = identity.get("identityTrait") or {}
    name = _name(obj.get("name")) or _name(trait.get("name"))
    artists = _artists(obj.get("artists")) or _artists(trait.get("contributors"))
    if not artists:
        artists = _artists(obj.get("firstArtist")) + _artists(obj.get("otherArtists"))
    if not name or not artists:
        raise BridgeError("Spotify returned incomplete track names; snapshot retained.")
    duration = _duration(obj, identity)
    if duration == 0:
        return None
    return {"key": track_id, "url": "https://open.spotify.com/track/" + track_id,
            "title": ", ".join(artists) + " - " + name, "name": name,
            "artists": artists, "duration": duration, "added_at": added,
            "basis": "playlist_added" if added else "first_seen",
            "entry_id": f"spotify:{position}", "position": position}


def _collection(response, kind, identity):
    response = _mapping(response, "Spotify returned an invalid response.")
    if response.get("errors"):
        raise BridgeError("Spotify could not return the complete collection.")
    data = _mapping(response.get("data"), "Spotify did not return collection data.")
    obj = data.get("playlistV2" if kind == "playlist" else "albumUnion")
    if obj is None or (isinstance(obj, dict) and obj.get("__typename") == "NotFound"):
        raise BridgeError("This Spotify collection is private or unavailable.", "unavailable", False)
    obj = _mapping(obj, "Spotify returned invalid collection metadata.")
    if obj.get("uri") != f"spotify:{kind}:{identity}" or not _name(obj.get("name")):
        raise BridgeError("Spotify returned an unexpected collection identity.")
    content = _mapping(obj.get("content" if kind == "playlist" else "tracksV2"),
                       "Spotify did not return the full collection content.")
    total = content.get("totalCount")
    if type(total) is not int or not 0 <= total <= MAX_ITEMS:
        raise BridgeError("Spotify returned an invalid or oversized collection count.")
    revision = obj.get("revisionId") or obj.get("snapshot_id")
    if kind == "playlist" and (not isinstance(revision, str) or not revision):
        raise BridgeError("Spotify did not return a playlist revision; snapshot retained.")
    return obj, content, total, revision


class SpotDLBackend:
    """Small wrapper around the public APIs supplied by the spotDL environment."""

    def __init__(self):
        # Importing spotDL's client also loads its SpotipyFree compatibility
        # fixes. No user authentication, cookies or config files are requested.
        from spotdl.utils.spotify import SpotifyClient
        from spotapi import PublicAlbum, PublicPlaylist

        self.client = SpotifyClient.init(client_id="", client_secret="", no_cache=True,
                                         headless=True, use_official_api=False)
        self._factories = {"playlist": PublicPlaylist, "album": PublicAlbum}
        self._readers = {}

    def page(self, kind, identity, offset, limit):
        key = (kind, identity)
        if key not in self._readers:
            self._readers[key] = self._factories[kind](identity)
        reader = self._readers[key]
        method = reader.get_playlist_info if kind == "playlist" else reader.get_album_info
        return method(limit=limit, offset=offset)

    def track(self, url):
        return self.client.track(url)

    @staticmethod
    def search(entry):
        from spotdl.providers.audio.ytmusic import YouTubeMusic
        from spotdl.types.song import Song

        class SearchOnly(YouTubeMusic):
            def __init__(self):
                # AudioProvider.__init__ creates a downloader and home-directory
                # config. A metadata-only adapter only needs the search client.
                self.client = self._create_client()
                self.search_query = None
                self.filter_results = True

            def get_best_result(self, results):
                # Use spotDL's matching score; do not open every candidate in
                # yt-dlp merely to use popularity as a tie-breaker.
                return max(results.items(), key=lambda pair: pair[1])

        if entry["duration"] is None:
            raise BridgeError("Spotify did not provide a duration for audio matching.")
        song = Song.from_missing_data(name=entry["name"], artists=entry["artists"],
                                      artist=entry["artists"][0], duration=entry["duration"],
                                      song_id=entry["key"], url=entry["url"],
                                      album_name="", isrc=None)
        return SearchOnly().search(song)


def scan(url, backend=None):
    kind, identity = canonical_spotify(url)
    if kind == "track":
        entry = _track_entry(url, identity, backend)
        if entry is None:
            raise BridgeError("This Spotify track is unavailable.", "unavailable", False)
        return {"provider": "spotify", "url": url, "title": entry["title"],
                "revision": None, "complete": True, "total_items": 1,
                "skipped": 0, "entries": [entry]}

    if backend is None:
        try:
            return _scan_public_document(url)
        except _PublicDocumentUnsupported:
            # Public HTML is often a preview of a large playlist. Only the
            # count-checked, paginated spotDL backend may complete that scan.
            backend = SpotDLBackend()

    offset, expected, revision, title = 0, None, None, None
    entries, skipped, occurrence_ids = [], 0, set()
    while True:
        obj, content, total, page_revision = _collection(
            backend.page(kind, identity, offset, PAGE_SIZE), kind, identity)
        if expected is None:
            expected, revision, title = total, page_revision, obj["name"]
        if total != expected or page_revision != revision:
            raise BridgeError("Spotify changed during pagination; previous snapshot retained.")
        rows = content.get("items")
        paging = content.get("pagingInfo")
        if (not isinstance(rows, list) or len(rows) > PAGE_SIZE
                or offset + len(rows) > expected
                or (isinstance(paging, dict) and "offset" in paging and paging["offset"] != offset)):
            raise BridgeError("Spotify returned inconsistent pagination; snapshot retained.")
        if not rows and offset < expected:
            raise BridgeError("Spotify pagination ended before all occurrences arrived.")
        for index, row in enumerate(rows):
            row = _mapping(row, "Spotify returned an invalid collection occurrence.")
            uid = row.get("uid")
            if uid is not None:
                if not isinstance(uid, str) or not uid or uid in occurrence_ids:
                    raise BridgeError("Spotify returned a repeated or invalid occurrence.")
                occurrence_ids.add(uid)
            entry = _entry(row, offset + index, kind=kind)
            if entry is not None:
                entries.append(entry)
            else:
                skipped += 1
        offset += len(rows)
        if offset == expected:
            break
    after, _, after_total, after_revision = _collection(
        backend.page(kind, identity, 0, 1), kind, identity)
    if after_total != expected or after_revision != revision or after["name"] != title:
        raise BridgeError("Spotify changed during synchronization; previous snapshot retained.")
    return {"provider": "spotify", "url": url, "title": title, "revision": revision,
            "complete": True, "total_items": expected, "skipped": skipped, "entries": entries,
            "scan_method": "spotdl_public_api"}


def _track_entry(url, identity, backend=None):
    if backend is None:
        try:
            return _entry(_public_entity(url), 0, kind="track", direct_id=identity)
        except _PublicDocumentUnsupported:
            backend = SpotDLBackend()
    return _entry(backend.track(url), 0, kind="track", direct_id=identity)


def resolve(url, backend=None):
    _, identity = canonical_spotify(url, track_only=True)
    entry = _track_entry(url, identity, backend)
    if entry is None:
        raise BridgeError("This Spotify track is unavailable.", "unavailable", False)
    match = backend.search(entry) if backend is not None else SpotDLBackend.search(entry)
    if not match:
        raise BridgeError("No suitable YouTube audio match was found; retry or choose a video manually.",
                          "no_match", False)
    return {"spotify_url": url, "youtube_url": canonical_youtube(match),
            "title": entry["title"], "name": entry["name"],
            "artists": entry["artists"], "duration": entry["duration"]}


def smoke():
    """Exercise every imported API and a real spotDL match without networking."""
    from spotdl.utils.spotify import SpotifyClient
    from spotapi import PublicAlbum, PublicPlaylist
    from spotdl.providers.audio.ytmusic import YouTubeMusic
    from spotdl.types.result import Result
    from spotdl.types.song import Song
    from spotdl.utils.matching import order_results

    for cls, method in ((SpotifyClient, "init"), (PublicPlaylist, "get_playlist_info"),
                        (PublicAlbum, "get_album_info"), (YouTubeMusic, "search"),
                        (Song, "from_missing_data")):
        if not callable(getattr(cls, method, None)):
            raise BridgeError("This spotDL release is incompatible with the adapter.",
                              "incompatible_runtime", False)
    song = Song.from_missing_data(name="Adapter fixture", artists=["Fixture artist"],
                                  artist="Fixture artist", duration=180, album_name="",
                                  song_id="0000000000000000000000", isrc=None)
    candidate = Result(source="YouTubeMusic", url="https://www.youtube.com/watch?v=abcdefghijk",
                       verified=True, name=song.name, duration=180, author=song.artist,
                       result_id="abcdefghijk", artists=tuple(song.artists))
    if candidate not in order_results([candidate], song):
        raise BridgeError("The spotDL matching smoke check failed.", "incompatible_runtime", False)
    json.dumps(song.json)
    return {"matching_checked": True}


def _version():
    try:
        return importlib.metadata.version("spotdl")
    except importlib.metadata.PackageNotFoundError:
        return None


def _write_output(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def _retry_after(exc):
    """Keep an upstream cooldown when exposed, without serializing the response."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return min(86400, max(1, seconds)) if math.isfinite(seconds) else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("scan", "resolve", "smoke"))
    parser.add_argument("--url")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    payload = {"schema_version": SCHEMA_VERSION, "mode": args.mode, "version": _version()}
    try:
        if payload["version"] is None:
            raise BridgeError("spotDL is not installed in the selected runtime.", "runtime_missing", False)
        if args.mode != "smoke" and not args.url:
            raise BridgeError("A Spotify URL is required.", "invalid_url", False)
        # Third-party informational prints must never corrupt the JSON result.
        with contextlib.redirect_stdout(sys.stderr):
            result = smoke() if args.mode == "smoke" else globals()[args.mode](args.url)
        payload.update(ok=True, **result)
    except BridgeError as exc:
        payload.update(ok=False, error=str(exc), code=exc.code, retryable=exc.retryable)
    except Exception as exc:
        # Upstream exceptions may contain request headers or response bodies.
        # Report only the exception class, never authentication/session values.
        payload.update(ok=False, error=(f"Spotify/spotDL request failed ({type(exc).__name__}). "
                                        "Check that the playlist is public, retry, or update spotDL."),
                       code="provider_error", retryable=True)
        if (delay := _retry_after(exc)) is not None:
            payload["retry_after"] = delay
    _write_output(args.output, payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
