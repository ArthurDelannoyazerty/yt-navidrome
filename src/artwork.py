"""Bounded, per-track artwork candidates; identity and artist spelling are separate."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import tempfile
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import requests
from mutagen.flac import Picture
from mutagen.oggopus import OggOpus
from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from common import utcnow

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_CANDIDATES = 120
FRAMINGS = {"original", "trim", "fit", "crop"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS artwork_candidates (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(name),
 track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
 origin_id TEXT REFERENCES track_origins(id) ON DELETE SET NULL,
 digest TEXT NOT NULL,
 source_type TEXT NOT NULL,
 source_url TEXT NOT NULL DEFAULT '',
 label TEXT NOT NULL,
 width INTEGER NOT NULL,
 height INTEGER NOT NULL,
 crop TEXT,
 score REAL NOT NULL,
 recording_id TEXT,
 release_id TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artwork_track ON artwork_candidates(user_id,track_id);
CREATE TABLE IF NOT EXISTS artwork_preferences (
 track_id TEXT PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(name),
 mode TEXT NOT NULL DEFAULT 'auto',
 candidate_id TEXT REFERENCES artwork_candidates(id) ON DELETE SET NULL,
 framing TEXT NOT NULL DEFAULT 'original',
 updated_at TEXT NOT NULL
);
"""


def decode(data: bytes) -> Image.Image:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Use a JPEG, PNG or WebP image of at most 8 MiB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "is_animated", False):
                    raise ValueError("Use a still JPEG, PNG or WebP image")
                if source.width * source.height > MAX_PIXELS or min(source.size) < 32:
                    raise ValueError("Image must be at least 32 pixels and at most 20 megapixels")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", image.size, (24, 24, 24, 255))
                background.alpha_composite(image)
                result = background.convert("RGB")
                result.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                return result
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("The image is unreadable or too large") from exc


def square_content(image):
    """Only trim a uniform dark border revealing a large nearly square image."""
    w, h = image.size
    if min(w, h) / max(w, h) >= .96:
        return None
    corners = [image.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    if any(max(c) > 28 for c in corners):
        return None
    channels = ImageChops.difference(image, Image.new("RGB", image.size, corners[0])).split()
    difference = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
    box = difference.point(lambda v: 255 if v > 18 else 0).getbbox()
    if not box:
        return None
    cw, ch = box[2] - box[0], box[3] - box[1]
    if min(cw, ch) < 256 or min(cw, ch) / max(cw, ch) < .98 or cw * ch < w * h * .4:
        return None
    return [max(0, box[0] - 2), max(0, box[1] - 2), min(w, box[2] + 2), min(h, box[3] + 2)]


def rank(image, source_type, crop=None):
    image = image.crop(tuple(crop)) if crop else image
    w, h = image.size
    gray = ImageOps.grayscale(image)
    gray.thumbnail((192, 192))
    sharpness = min(20.0, math.log1p(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]))
    priority = {"coverart": 600, "embedded": 500, "upload": 450, "youtube": 100}[source_type]
    return priority + (100 if min(w, h) >= 256 and min(w, h) / max(w, h) >= .96 else 0) + min(min(w, h), 1600) / 40 + sharpness


def embedded(path):
    for value in OggOpus(str(path)).get("metadata_block_picture", [])[:8]:
        if len(value) > MAX_BYTES * 4 // 3 + 1024:
            continue
        try:
            picture = Picture(base64.b64decode(value, validate=True))
            if picture.type in {0, 3} and len(picture.data) <= MAX_BYTES:
                return picture.data
        except Exception:
            continue  # A malformed optional picture must not break playable audio.
    return None


def transfer_origin(con, source, target, origin):
    """Transfer image provenance atomically with origin merge/separation."""
    if source == target:
        return
    orphan = not con.execute("SELECT 1 FROM track_origins WHERE track_id=?", (source,)).fetchone()
    if orphan:
        con.execute("UPDATE artwork_candidates SET track_id=? WHERE track_id=?", (target, source))
    else:
        con.execute("UPDATE artwork_candidates SET track_id=? WHERE track_id=? AND origin_id=?", (target, source, origin))
    old = con.execute("SELECT * FROM artwork_preferences WHERE track_id=?", (source,)).fetchone()
    current = con.execute("SELECT * FROM artwork_preferences WHERE track_id=?", (target,)).fetchone()
    if old and old["mode"] == "manual" and (not current or current["mode"] != "manual"):
        if con.execute("SELECT 1 FROM artwork_candidates WHERE id=? AND track_id=?", (old["candidate_id"], target)).fetchone():
            con.execute("""INSERT INTO artwork_preferences VALUES (?,?,?,?,?,?) ON CONFLICT(track_id)
                DO UPDATE SET mode=excluded.mode,candidate_id=excluded.candidate_id,
                framing=excluded.framing,updated_at=excluded.updated_at""",
                (target, old["user_id"], "manual", old["candidate_id"], old["framing"], utcnow()))
    if not orphan:
        con.execute("DELETE FROM artwork_preferences WHERE track_id=? AND candidate_id IN (SELECT id FROM artwork_candidates WHERE track_id=?)", (source, target))


class Artwork:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / "artwork"

    def path(self, digest):
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid artwork cache identity")
        return self.root / (digest + ".jpg")

    def preference(self, track):
        return self.store.one("SELECT * FROM artwork_preferences WHERE track_id=? AND user_id=?", (track["id"], track["user_id"]))

    def candidates(self, track):
        if track.get("artwork_split"):
            return self.store.rows("SELECT * FROM artwork_candidates WHERE origin_id=? AND user_id=? ORDER BY score DESC,id", (track["artwork_origin_id"], track["user_id"]))
        return self.store.rows("SELECT * FROM artwork_candidates WHERE (track_id=? OR origin_id=?) AND user_id=? ORDER BY score DESC,id", (track["id"], track.get("artwork_extra_origin_id"), track["user_id"]))

    def owned(self, identity, track):
        candidate = self.store.one("SELECT * FROM artwork_candidates WHERE id=? AND track_id=? AND user_id=?", (identity, track["id"], track["user_id"]))
        if not candidate:
            raise LookupError("Artwork is no longer available on this track")
        return candidate

    def cache(self, track, data, source_type, label, *, origin=None, url="", recording=None, release=None):
        image = decode(data)
        if source_type == "youtube" and (min(image.size) < 160 or ImageOps.grayscale(image).entropy() < .05):
            raise ValueError("Thumbnail is a placeholder or too small")
        output = io.BytesIO()
        image.save(output, "JPEG", quality=92, optimize=True)
        content = output.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        owner = origin["track_id"] if origin else track["id"]
        identity = hashlib.sha256(json.dumps([owner, origin["id"] if origin else None, digest, source_type, recording, release]).encode()).hexdigest()
        crop = square_content(image)
        # Hold the limit check and insertion together; normalized data only goes to disk.
        with self.store.db() as con:
            con.execute("BEGIN IMMEDIATE")
            exists = con.execute("SELECT 1 FROM artwork_candidates WHERE id=?", (identity,)).fetchone()
            count = con.execute("SELECT COUNT(*) FROM artwork_candidates WHERE track_id=? AND user_id=?", (owner, track["user_id"])).fetchone()[0]
            if count >= MAX_CANDIDATES and not exists:
                raise ValueError("This track has reached its artwork candidate limit")
            self.root.mkdir(parents=True, exist_ok=True)
            destination = self.path(digest)
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                fd, temporary = tempfile.mkstemp(prefix=".image-", dir=self.root)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            con.execute("INSERT OR IGNORE INTO artwork_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, track["user_id"], owner, origin["id"] if origin else None, digest, source_type, url,
                 label[:200], image.width, image.height, json.dumps(crop) if crop else None,
                 rank(image, source_type, crop), recording, release, utcnow()))
        return self.store.one("SELECT * FROM artwork_candidates WHERE id=?", (identity,))

    def capture(self, track, path, *, origin=None, source_type="embedded"):
        data = embedded(path)
        if not data:
            return None
        previous = self.preference(track)
        if previous and previous["candidate_id"]:
            try:
                candidate = self.owned(previous["candidate_id"], track)
                if data == self.render(candidate, previous["framing"]):
                    return candidate  # Do not relabel our own fallback or recompress it repeatedly.
            except (LookupError, ValueError, OSError):
                pass
        return self.cache(track, data, source_type, "Album cover" if source_type == "coverart" else "Existing embedded cover",
            origin=origin, recording=track.get("mbid"), release=track.get("release_id"))

    @staticmethod
    def thumbnail_url(url, video):
        try:
            parsed = urlsplit(url)
            return bool(parsed.scheme == "https" and parsed.hostname in {"i.ytimg.com", "i1.ytimg.com", "i2.ytimg.com", "i3.ytimg.com", "i4.ytimg.com"}
                and not parsed.username and not parsed.password and parsed.port in {None, 443}
                and not parsed.query and not parsed.fragment
                and re.fullmatch(r"/vi(?:_webp)?/" + re.escape(video) + r"/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)", parsed.path))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def download(url):
        with requests.get(url, timeout=(5, 15), stream=True, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200 or int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
                raise ValueError("Thumbnail is unavailable or too large")
            content = bytearray()
            for chunk in response.iter_content(65536):
                if len(content) + len(chunk) > MAX_BYTES:
                    raise ValueError("Thumbnail exceeds 8 MiB")
                content.extend(chunk)
            return bytes(content)

    def collect(self, track, force=False):
        """Only called in the isolated worker with the shared provider HTTP policy."""
        key = os.getenv("YT_API_KEY", "").strip()
        if not key:
            return ["YouTube cover lookup needs the configured API key"]
        origins = self.store.origins_for_track(track["id"], track["user_id"])
        extra = self.store.one("SELECT * FROM track_origins WHERE id=? AND user_id=?", (track.get("artwork_extra_origin_id"), track["user_id"]))
        if extra and not any(o["id"] == extra["id"] for o in origins):
            origins.append(extra)
        if track.get("artwork_split"):
            origins = [o for o in origins if o["id"] == track["artwork_origin_id"]]
        origins = [o for o in origins if o["provider"] == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", o["media_key"])]
        if not origins:
            return []
        messages = ["Thumbnail comparison is limited to the first 50 associated videos"] if len(origins) > 50 else []
        ids = {o["media_key"]: o for o in origins[:50]}
        try:
            with requests.get("https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet", "id": ",".join(ids)},
                headers={"X-Goog-Api-Key": key, "User-Agent": os.getenv("HTTP_USER_AGENT", "Music-Ingestor/0.3")},
                timeout=(5, 20), allow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("Unexpected thumbnail lookup response")
                videos = response.json().get("items", [])
            if not isinstance(videos, list):
                raise ValueError("Incomplete video response")
            for video in videos[:50]:
                origin = ids.get(video.get("id"))
                if not origin:
                    continue
                variants = list((video.get("snippet", {}).get("thumbnails") or {}).values())
                def size(t):
                    w, h = int(t.get("width") or 0), int(t.get("height") or 0)
                    return (min(w, h) >= 256 and abs(w - h) <= max(w, h) * .04, w * h)
                successes = 0
                for thumbnail in sorted(variants, key=size, reverse=True)[:4]:
                    url = thumbnail.get("url", "")
                    if not self.thumbnail_url(url, origin["media_key"]):
                        continue
                    cached = self.store.one("SELECT * FROM artwork_candidates WHERE origin_id=? AND source_url=? ORDER BY created_at DESC LIMIT 1", (origin["id"], url))
                    try:
                        if force or not cached:
                            self.cache(track, self.download(url), "youtube", origin["title"], origin=origin, url=url)
                        else:
                            self.render(cached, "original")
                        successes += 1
                    except (requests.RequestException, ValueError, OSError):
                        continue
                    if successes >= 2:
                        break
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
            messages.append("YouTube covers could not be refreshed; cached images and audio were retained")
        return messages

    def render(self, candidate, framing="original"):
        if framing not in FRAMINGS:
            raise ValueError("Unknown image framing")
        path = self.path(candidate["digest"])
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError("Cached image is missing; find covers again or upload a replacement")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != candidate["digest"]:
            raise ValueError("Cached image changed; refusing to apply it")
        image = decode(raw)
        if framing == "trim" and candidate["crop"]:
            image = image.crop(tuple(json.loads(candidate["crop"])))
        elif framing == "crop":
            side = min(image.size); image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
        elif framing == "fit":
            side = max(image.size); image = ImageOps.pad(image, (side, side), color=(24, 24, 24))
        image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        output = io.BytesIO(); image.save(output, "JPEG", quality=92, optimize=True)
        return output.getvalue()

    def apply(self, track, path, *, selection=None, framing="original", refresh=False, source_reset=False):
        previous = self.preference(track)
        pinned = bool(previous and previous["mode"] == "manual")
        if pinned and track.get("artwork_split"):
            pinned = any(c["id"] == previous["candidate_id"] for c in self.candidates(track))
        chosen, messages, mode = None, [], "auto"
        if selection and selection != "auto":
            chosen, mode = self.owned(selection, track), "manual"
        elif pinned and selection != "auto" and not refresh:
            chosen, framing, mode = self.owned(previous["candidate_id"], track), previous["framing"], "manual"
        else:
            if not source_reset:
                origin = self.store.one("SELECT * FROM track_origins WHERE id=?", (track.get("artwork_origin_id"),)) if track.get("artwork_split") else None
                self.capture(track, path, origin=origin)
            def compatible(c):
                return c["source_type"] != "upload" and (not c["recording_id"] or c["recording_id"] == track.get("mbid")) and (not c["release_id"] or c["release_id"] == track.get("release_id"))
            eligible = [c for c in self.candidates(track) if compatible(c)]
            if refresh or not any(c["source_type"] in {"coverart", "embedded"} for c in eligible):
                messages.extend(self.collect(track, force=refresh))
                eligible = [c for c in self.candidates(track) if compatible(c)]
            if pinned and selection != "auto":
                chosen, framing, mode = self.owned(previous["candidate_id"], track), previous["framing"], "manual"
            else:
                for candidate in eligible:
                    if candidate["source_type"] == "youtube" and not candidate["origin_id"]:
                        continue
                    candidate_framing = "trim" if candidate["crop"] else "original"
                    try:
                        self.render(candidate, candidate_framing)
                        chosen, framing = candidate, candidate_framing
                        break
                    except (ValueError, OSError):
                        continue
        if chosen:
            data = self.render(chosen, framing)
            picture = Picture(); picture.type = 3; picture.mime = "image/jpeg"; picture.desc = "Front cover"
            with Image.open(io.BytesIO(data)) as image:
                picture.width, picture.height = image.size
            picture.depth, picture.data = 24, data
            audio = OggOpus(str(path))
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
            audio.save()
        else:
            messages.append("No usable cover found. The current image, if any, was retained; manual upload is available.")
        return {"candidate_id": chosen["id"] if chosen else None, "mode": mode, "framing": framing, "warnings": messages}

    @staticmethod
    def record(con, track, value):
        con.execute("""INSERT INTO artwork_preferences VALUES (?,?,?,?,?,?) ON CONFLICT(track_id)
            DO UPDATE SET mode=excluded.mode,candidate_id=excluded.candidate_id,framing=excluded.framing,
            updated_at=excluded.updated_at""", (track["id"], track["user_id"], value["mode"], value.get("candidate_id"), value["framing"], utcnow()))
