"""Small, source-independent helpers shared by the API and worker processes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
STATE = Path(os.getenv("INGESTOR_STATE_DIR", os.getenv("STATE_DIR", "/data/state")))
LIBRARY = Path(os.getenv("NAVIDROME_LIB_DIR", "/data/library"))


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value: str) -> str:
    """Reject missing/naive dates: midnight is not a substitute for an unknown time."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("The source did not supply a timezone-aware addition timestamp")
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def discovery_comment(value: str | None) -> str:
    if not value:
        return "Discovery Date: unknown"
    dt = datetime.fromisoformat(timestamp(value).replace("Z", "+00:00"))
    return "Discovery Date: " + dt.strftime("%Y-%m-%d--%H-%M-%S") + " UTC"


def canonical_source_urls(urls) -> list[str]:
    """Stable public track URLs only; discard credentials and tracking queries."""
    result = set()
    for value in urls or []:
        try:
            parts = urlsplit(str(value))
            if parts.scheme not in {"https", "http"} or parts.username or parts.password:
                continue
            if parts.port not in {None, 80, 443}:
                continue
            host, identity = (parts.hostname or "").lower(), None
            if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}:
                if host == "youtu.be":
                    identity = parts.path.strip("/")
                elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
                    identity = parts.path.split("/")[2]
                elif parts.path == "/watch":
                    identity = parse_qs(parts.query).get("v", [""])[0]
                if identity and re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
                    result.add("https://www.youtube.com/watch?v=" + identity)
            elif host == "open.spotify.com":
                match = re.fullmatch(r"/(?:intl-[A-Za-z-]+/)?track/([A-Za-z0-9]{22})/?", parts.path)
                if match:
                    result.add("https://open.spotify.com/track/" + match[1])
        except (ValueError, TypeError, IndexError):
            continue
    return sorted(result, key=lambda url: ("spotify.com" in url, url))


def music_comment(value: str | None, source_urls=()) -> str:
    """Preserve the discovery line and append the portable source provenance."""
    comment = discovery_comment(value)
    urls = canonical_source_urls(source_urls)
    if urls:
        comment += "\n\nSources:\n" + "\n".join(urls)
    return comment


def user_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("User names must be 1-64 letters, digits, underscores or hyphens")
    return value


def safe_name(value: str) -> str:
    value = re.sub(r'[\x00-\x1f\\/*?:"<>|]', "_", value).strip(" .")
    return value[:120] or "Playlist"


def atomic_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), mode)
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy into the destination filesystem, then atomically replace one file."""
    fd, temporary = tempfile.mkstemp(prefix=".repair-", dir=destination.parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def next_nightly(now: datetime, at: str, zone: str) -> datetime:
    """Return the next configured local wall-clock time as an aware UTC datetime."""
    hour, minute = map(int, at.split(":"))
    tz = ZoneInfo(zone)
    local = now.astimezone(tz)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC)
