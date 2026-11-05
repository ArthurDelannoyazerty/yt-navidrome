"""Interactive MusicBrainz search, independent of automatic fingerprint matching.

Public metadata is cached briefly. All network attempts use the same persistent
host limiter as beets; the browser's 500 ms debounce is not a server rate limit.
"""
from __future__ import annotations

import asyncio
import copy
import os
import re
import time
import uuid
from collections import OrderedDict

import requests

from http_policy import ApiDeferred, HttpPolicy
from metadata_hints import clean_search_text, normalized_words

API_ROOT = "https://musicbrainz.org/ws/2/"
PAGE_SIZE = 20
USER_AGENT = "Music-Ingestor/0.4 (+https://github.com/ArthurDelannoyazerty/yt-navidrome)"
_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/|&])')


def terms(value: str) -> str:
    """Literal user words, not arbitrary Lucene syntax or field injection."""
    words = value.split()
    # Quoted individual terms preserve AND/OR/NOT as names, not operators.
    return " AND ".join('"' + _SPECIAL.sub(r"\\\1", word) + '"' for word in words)


def query_for(entity: str, title: str, artist: str, artist_id: str | None) -> str:
    if entity not in {"recording", "artist"}:
        raise ValueError("Search recordings or artists")
    if max(len(title), len(artist)) > 200:
        raise ValueError("Search fields must contain at most 200 characters")
    if entity == "artist":
        if len(artist.strip()) < 2:
            raise ValueError("Enter at least two characters for the artist")
        return f"artist:({terms(artist)})"
    filters = []
    if title.strip():
        filters.append(f"recording:({terms(title)})")
    if artist_id:
        filters.append("arid:" + str(uuid.UUID(artist_id)))
    elif artist.strip():
        filters.append(f"artist:({terms(artist)})")
    if not artist_id and len(title.strip()) + len(artist.strip()) < 2:
        raise ValueError("Enter a song title or artist (at least two characters)")
    return " AND ".join(filters)


def valid_id(value) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def artist_credit(values) -> str:
    return "".join(
        (value.get("name") or value.get("artist", {}).get("name") or "") + (value.get("joinphrase") or "")
        for value in values or [] if isinstance(value, dict)
    )[:1000]



def aliases(values) -> list[dict]:
    return [{"name": str(v.get("name") or "")[:200], "sort_name": str(v.get("sort-name") or "")[:200],
             "locale": v.get("locale"), "primary": bool(v.get("primary")), "type": v.get("type")}
            for v in (values or [])[:30] if isinstance(v, dict) and v.get("name")]


def artist_details(values) -> list[dict]:
    results = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        artist = value.get("artist") or {}
        if identity := valid_id(artist.get("id")):
            results.append({"id": identity, "name": str(artist.get("name") or value.get("name") or "")[:200],
                            "credited_name": str(value.get("name") or artist.get("name") or "")[:200],
                            "sort_name": str(artist.get("sort-name") or "")[:200],
                            "aliases": aliases(artist.get("aliases"))})
    return results

def normalize(data: dict, entity: str, page: int) -> dict:
    plural = "recordings" if entity == "recording" else "artists"
    raw = data.get(plural)
    if not isinstance(raw, list) or not isinstance(data.get("count"), int):
        raise ValueError("MusicBrainz returned an invalid search response")
    items = []
    for value in raw[:PAGE_SIZE]:
        if not isinstance(value, dict) or not (identity := valid_id(value.get("id"))):
            continue
        item = {"id": identity, "score": value.get("score"),
                "disambiguation": str(value.get("disambiguation") or "")[:1000]}
        if entity == "artist":
            item.update(name=str(value.get("name") or "Unknown artist")[:1000],
                        country=value.get("country"), type=value.get("type"),
                        sort_name=str(value.get("sort-name") or "")[:200], aliases=aliases(value.get("aliases")))
        else:
            releases = []
            for release in (value.get("releases") or [])[:20]:
                if rid := valid_id(release.get("id")):
                    releases.append({"id": rid, "title": str(release.get("title") or "Untitled")[:1000],
                                     "date": release.get("date"), "country": release.get("country"),
                                     "status": release.get("status")})
            item.update(title=str(value.get("title") or "Unknown title")[:1000],
                        artist=artist_credit(value.get("artist-credit")), artists=artist_details(value.get("artist-credit")),
                        length_ms=value.get("length"), first_release_date=value.get("first-release-date"),
                        releases=releases)
        items.append(item)
    return {"items": items, "total": max(0, data["count"]), "page": page, "limit": PAGE_SIZE,
            "entity": entity, "score_note": "Text-search relevance, not audio-match confidence"}


class MusicBrainzSearch:
    def __init__(self, store):
        self.store = store
        self.cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self.pending: dict[tuple, asyncio.Task] = {}

    def request(self, entity: str, query: str, page: int) -> dict:
        policy = HttpPolicy(self.store)
        with requests.Session() as session:
            prepared = session.prepare_request(requests.Request(
                "GET", API_ROOT + entity,
                params={"query": query, "fmt": "json", "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
                headers={"User-Agent": os.getenv("HTTP_USER_AGENT") or USER_AGENT, "Accept": "application/json"},
            ))
            response = policy.send(requests.sessions.Session.send, session, prepared,
                                   timeout=(5, 15), allow_redirects=False)
            try:
                if response.status_code != 200:
                    raise ApiDeferred(f"MusicBrainz search returned HTTP {response.status_code}", retry_at=time.time() + 60)
                return normalize(response.json(), entity, page)
            finally:
                response.close()

    def request_artist(self, artist_id: str) -> dict:
        """Resolve IDs server-side before storing an explicitly confirmed alias."""
        policy = HttpPolicy(self.store)
        with requests.Session() as session:
            prepared = session.prepare_request(requests.Request(
                "GET", API_ROOT + "artist/" + artist_id, params={"fmt": "json", "inc": "aliases"},
                headers={"User-Agent": os.getenv("HTTP_USER_AGENT") or USER_AGENT, "Accept": "application/json"},
            ))
            response = policy.send(requests.sessions.Session.send, session, prepared,
                                   timeout=(5, 15), allow_redirects=False)
            try:
                if response.status_code == 404:
                    raise ValueError("MusicBrainz artist was not found")
                if response.status_code != 200:
                    raise ApiDeferred(f"MusicBrainz artist lookup returned HTTP {response.status_code}", retry_at=time.time() + 60)
                value = response.json()
                if valid_id(value.get("id")) != artist_id or not value.get("name"):
                    raise ValueError("MusicBrainz returned an invalid artist")
                return {"id": artist_id, "name": str(value["name"])[:200], "aliases": aliases(value.get("aliases"))}
            finally:
                response.close()

    async def _cached(self, key, request):
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        if key not in self.pending:
            if len(self.pending) >= 4:
                raise ApiDeferred("MusicBrainz search is busy; retry shortly", retry_at=time.time() + 2)

            async def fetch():
                try:
                    result = await asyncio.to_thread(request)
                    self.cache[key] = (time.monotonic() + 120, result)
                    self.cache.move_to_end(key)
                    while len(self.cache) > 128:
                        self.cache.popitem(last=False)
                    return result
                finally:
                    self.pending.pop(key, None)

            task = asyncio.create_task(fetch())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self.pending[key] = task
        return copy.deepcopy(await asyncio.shield(self.pending[key]))

    async def artist(self, artist_id):
        identity = valid_id(artist_id)
        if not identity:
            raise ValueError("Invalid MusicBrainz artist ID")
        return await self._cached(("artist_lookup", identity, 0), lambda: self.request_artist(identity))

    async def search(self, entity="recording", title="", artist="", artist_id=None, page=1):
        if type(page) is not int or not 1 <= page <= 100:
            raise ValueError("Search page must be between 1 and 100")
        # Validate raw inputs before normalization so a long request cannot evade limits.
        query_for(entity, title.strip(), artist.strip(), artist_id)
        title, artist = normalized_words(title), normalized_words(artist)
        variants = [("exact", title, artist)]
        cleaned = clean_search_text(title)
        if entity == "recording" and cleaned and cleaned != title:
            variants.append(("cleaned", cleaned, artist))
        if entity == "recording" and cleaned and artist and not artist_id:
            variants.append(("title_only", cleaned, ""))
        tried, seen = [], set()
        for strategy, actual_title, actual_artist in variants:
            query = query_for(entity, actual_title, actual_artist, artist_id)
            if query in seen:
                continue
            seen.add(query)
            # Always determine fallback from the first page. Pagination therefore
            # cannot silently drop an artist just because a later page is empty.
            key = (entity, query, 1)
            result = await self._cached(key, lambda q=query: self.request(entity, q, 1))
            tried.append(strategy)
            if result.get("items") or result.get("total", 0) > 0:
                break
        if page != 1:
            result = await self._cached((entity, query, page), lambda: self.request(entity, query, page))
        result["query_used"] = {"title": actual_title, "artist": actual_artist, "artist_id": artist_id}
        result["query_strategy"] = strategy
        result["fallbacks_tried"] = tried
        result["query_note"] = ("No results with the artist constraint; showing title-only results. Verify the artist."
                                if strategy == "title_only" else
                                "Video-only decorations were removed from the search, not from your metadata."
                                if strategy == "cleaned" else "")
        return result
