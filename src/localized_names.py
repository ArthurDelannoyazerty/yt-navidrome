"""English-first MusicBrainz display names and canonical native-name Opus tags.

Aliases may be translations or transliterations. Never invent a translation.
Use ORIGINALTITLE/ORIGINALARTIST/ORIGINALALBUM for the canonical MusicBrainz
strings; these refer to the native/canonical *name*, not an older recording.
"""
from __future__ import annotations

from mutagen.oggopus import OggOpus

LANGUAGE_POLICY = "english-first-v1"
NATIVE_KEYS = {"title": "ORIGINALTITLE", "artist": "ORIGINALARTIST",
               "album": "ORIGINALALBUM"}
POLICY_TAG = "INGESTOR_LANGUAGE_POLICY"


def artist_credit(data):
    """Prefer the actual credited name; do not replace it with a legal alias."""
    value = data.get("artist-credit") or data.get("artist_credit") or []
    if not isinstance(value, list):
        return ""
    parts = []
    for part in value:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            artist = part.get("artist") or {}
            parts.append(str(part.get("name") or artist.get("name") or ""))
            parts.append(str(part.get("joinphrase") or ""))
    return "".join(parts).strip()[:1000]


def native_names(recording, release=None):
    """Canonical MusicBrainz fields independent of preferred aliases."""
    names = {"title": str(recording.get("title") or "").strip()[:1000],
             "artist": artist_credit(recording), "album": ""}
    if release is not None:
        names["album"] = str(release.get("title") or "").strip()[:1000]
    return names


def apply_to_opus(path, display: dict, native: dict, *, include_album=False,
                  clear_unassigned_album=False):
    """Update names only, preserving the existing identity and other tags."""
    audio = OggOpus(str(path))
    keys = ("title", "artist", "album") if include_album else ("title", "artist")
    for key in keys:
        value = str(display.get(key) or "").strip()[:1000]
        if value:
            audio[key] = [value]
        original = str(native.get(key) or "").strip()[:1000]
        if original:
            audio[NATIVE_KEYS[key]] = [original]
        else:
            audio.pop(NATIVE_KEYS[key], None)
    if not include_album and clear_unassigned_album:
        audio.pop(NATIVE_KEYS["album"], None)
    audio[POLICY_TAG] = [LANGUAGE_POLICY]
    audio.save()
    return {"title": (audio.get("title") or [""])[0],
            "artist": (audio.get("artist") or [""])[0],
            "album": (audio.get("album") or [""])[0],
            "originaltitle": (audio.get(NATIVE_KEYS["title"]) or [""])[0],
            "originalartist": (audio.get(NATIVE_KEYS["artist"]) or [""])[0],
            "originalalbum": (audio.get(NATIVE_KEYS["album"]) or [""])[0]}


def is_localized(path):
    try:
        return LANGUAGE_POLICY in (OggOpus(str(path)).get(POLICY_TAG) or [])
    except Exception:
        return False
