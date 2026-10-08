"""Small, source-independent helpers shared by the API and worker processes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def user_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
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


def next_nightly(now: datetime, at: str, zone: str) -> datetime:
    """Return the next configured local wall-clock time as an aware UTC datetime."""
    hour, minute = map(int, at.split(":"))
    tz = ZoneInfo(zone)
    local = now.astimezone(tz)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(UTC)
