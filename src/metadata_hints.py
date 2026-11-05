"""Conservative query hints, separate from retained source/display metadata."""
from __future__ import annotations

import math
import re
import unicodedata

# Remove only complete video-only decorations. Version labels remain evidence.
_DECORATION = re.compile(
    r"^(?:official\s+(?:music\s+)?(?:video|audio|visuali[sz]er)|"
    r"(?:music\s+)?video\s+official|official\s+mv|mv|pv|hd|hq|[48]k|1080p|720p)$", re.I,
)
_BRACKETS = re.compile(r"\([^()]*\)|\[[^\[\]]*\]|【[^【】]*】")
_SUFFIX = re.compile(r"\s+(?:[-–—|:]\s*)?(official\s+(?:music\s+)?(?:video|audio|visuali[sz]er)|official\s+mv|[48]k|1080p|720p)\s*$", re.I)
_VERSION_PATTERNS = {
    "live": r"\blive\b|ライブ|ライヴ",
    "remix": r"\bremix\b|リミックス",
    "instrumental": r"\binstrumental\b|\boff[ -]?vocal\b|インスト(?:ゥルメンタル)?|カラオケ|\bkaraoke\b",
    "cover": r"\bcover\b|カバー|歌ってみた",
    "TV size": r"\btv[ ._-]?(?:size|ver(?:sion)?)\b|テレビサイズ",
    "acoustic": r"\bacoustic\b|アコースティック",
    "sped up": r"\bsped[ -]?up\b|\bnightcore\b",
    "slowed": r"\bslowed\b",
    "full album": r"\bfull[ -]?album\b|全曲|全アルバム",
}


def normalized_words(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def alias_key(value: str) -> str:
    return normalized_words(value).casefold()


def clean_search_text(value: str) -> str:
    text = normalized_words(value)
    text = _BRACKETS.sub(lambda m: " " if _DECORATION.fullmatch(m.group()[1:-1].strip()) else m.group(), text)
    previous = None
    while text != previous:
        previous, text = text, _SUFFIX.sub("", text).strip()
    return " ".join(text.split()).strip(" |–—")


def version_clues(value: str) -> list[str]:
    return [name for name, pattern in _VERSION_PATTERNS.items() if re.search(pattern, normalized_words(value), re.I)]


def search_hints(source: dict, origin: dict | None = None, confirmed_alias: dict | None = None) -> dict:
    """Never infer the performer from an arbitrary uploader/channel."""
    origin = origin or {}
    tags = source.get("tags") or {}
    raw_title = str(source.get("source_title") or origin.get("title") or "")
    title = str((tags.get("title") or [raw_title])[0])
    artist = "; ".join(str(x) for x in tags.get("artist", []) if x)
    artist_basis = "original_tags" if artist and source.get("basis") == "original_download_tags" else "source_title" if artist else "unknown"
    channel = normalized_words(origin.get("channel") or "")
    notes = []
    if not artist:
        # Explicit title separators are hints, not automatic identity approvals.
        split = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
        if len(split) == 2 and all(x.strip() for x in split):
            artist, title = split
            artist_basis = "source_title"
        elif channel.endswith(" - Topic") and channel[:-8].strip():
            artist = channel[:-8].strip()
            artist_basis = "topic_channel"
            notes.append("Artist suggested by the Topic channel; confirm the MusicBrainz artist.")
    clean_title, clean_artist = clean_search_text(title), normalized_words(artist)
    if clean_title != title:
        notes.append("Video-only decorations/spacing were removed for searching; original tags are unchanged.")
    identity = None
    if confirmed_alias:
        identity = confirmed_alias["artist_id"]
        clean_artist = confirmed_alias["artist_name"]
        artist_basis = "confirmed_alias"
        notes.append("Artist identity uses your confirmed alias.")
    return {
        "title": clean_title[:200], "artist": clean_artist[:200], "artist_id": identity,
        "artist_basis": artist_basis, "raw_title": raw_title, "channel": channel,
        "version_clues": version_clues(raw_title), "notes": notes,
    }


def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def evidence_for_candidate(item, candidate, source_title: str) -> dict:
    local = finite_number(item.length)
    length = finite_number(candidate.info.length)
    source_versions = version_clues(source_title)
    candidate_versions = version_clues(" ".join(str(x or "") for x in (
        candidate.info.title, candidate.disambig_string,
    )))
    penalties = [{"field": key, "penalty": round(float(value), 4)} for key, value in candidate.distance.items()]
    source_distinct = set(source_versions) - {"full album"}
    candidate_distinct = set(candidate_versions) - {"full album"}
    return {
        "local_duration_seconds": round(local, 3) if local is not None else None,
        "candidate_duration_seconds": round(length, 3) if length is not None else None,
        "duration_delta_seconds": round(abs(local - length), 3) if local is not None and length is not None else None,
        "source_version_clues": source_versions, "version_clues": candidate_versions,
        "version_conflict": bool(source_distinct != candidate_distinct and (source_distinct or candidate_distinct)),
        "penalties": penalties,
        "artist_ids": list(getattr(candidate.info, "artists_ids", []) or []),
        "score_note": "Weighted beets match score, not a probability of correctness",
    }
