"""English-first MusicBrainz display names and canonical native-name Opus tags.

Aliases may be translations or transliterations. Never invent a translation.
Use ORIGINALTITLE/ORIGINALARTIST/ORIGINALALBUM for the canonical MusicBrainz
strings; these refer to the native/canonical *name*, not an older recording.
"""
from __future__ import annotations

from mutagen.oggopus import OggOpus

LANGUAGE_POLICY = "english-first-v1"
NATIVE_KEYS = {"title": "ORIGINALTITLE", "artist": "ORIGINALARTIST",
               "album": "ORIGINALALBUM", "albumartist": "ORIGINALALBUMARTIST"}
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


def preferred_english_name(data, *, field="title"):
    """Use only a locale-marked English alias, not a guessed translation."""
    canonical = str(data.get(field) or "").strip()[:1000]
    aliases = data.get("aliases") or []
    suitable = []
    for alias in aliases:
        if not isinstance(alias, dict):
            continue
        locale = str(alias.get("locale") or "").replace("_", "-").lower()
        name = str(alias.get("name") or "").strip()[:1000]
        kind = str(alias.get("type") or "").casefold()
        if ((locale == "en" or locale.startswith("en-")) and name
                and kind not in {"legal name", "search hint", "misspelling"}):
            suitable.append((not bool(alias.get("primary")), locale != "en", name))
    return min(suitable)[2] if suitable else canonical


def native_names(recording, release=None):
    """Canonical MusicBrainz fields independent of preferred aliases."""
    names = {"title": str(recording.get("title") or "").strip()[:1000],
             "artist": artist_credit(recording), "album": ""}
    if release is not None:
        names["album"] = str(release.get("title") or "").strip()[:1000]
        names["albumartist"] = artist_credit(release)
    return names


def apply_to_opus(path, display: dict, native: dict, *, include_album=False,
                  clear_unassigned_album=False):
    """Update names only, preserving the existing identity and other tags."""
    audio = OggOpus(str(path))
    keys = ("title", "artist", "album", "albumartist") if include_album else ("title", "artist")
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
        audio.pop(NATIVE_KEYS["albumartist"], None)
    audio[POLICY_TAG] = [LANGUAGE_POLICY]
    audio.save()
    return {"title": (audio.get("title") or [""])[0],
            "artist": (audio.get("artist") or [""])[0],
            "album": (audio.get("album") or [""])[0],
            "originaltitle": (audio.get(NATIVE_KEYS["title"]) or [""])[0],
            "originalartist": (audio.get(NATIVE_KEYS["artist"]) or [""])[0],
            "originalalbum": (audio.get(NATIVE_KEYS["album"]) or [""])[0],
            "originalalbumartist": (audio.get(NATIVE_KEYS["albumartist"]) or [""])[0]}


def is_localized(path):
    try:
        return LANGUAGE_POLICY in (OggOpus(str(path)).get(POLICY_TAG) or [])
    except Exception:
        return False


def language_change_report(before_path, after_path):
    """Describe *actual* staged tag edits for Activity, not cache predictions.

    Only compare known display/native-name fields and the policy marker.
    Reading tags never alters the original or staged audio file.
    """
    old = OggOpus(str(before_path))
    new = OggOpus(str(after_path))

    def value(audio, key):
        return str((audio.get(key) or [""])[0]).strip()

    def short(value):
        return repr(value[:80] + ("…" if len(value) > 80 else ""))

    display = []
    for key in ("TITLE", "ARTIST", "ALBUM", "ALBUMARTIST"):
        a, b = value(old, key), value(new, key)
        if a != b:
            display.append(f"{key}: {short(a)} → {short(b)}")
    original = []
    for key in NATIVE_KEYS.values():
        if value(old, key) != value(new, key):
            original.append(key)

    if display:
        parts = ["Visible metadata changed: " + "; ".join(display)]
    else:
        parts = ["Visible title, artist and album names unchanged"]
    parts.append("Canonical tags updated: " + ", ".join(original)
                 if original else "Canonical tags already correct")
    if value(old, POLICY_TAG) != value(new, POLICY_TAG):
        parts.append("English-first policy marker set")
    return ". ".join(parts) + ". MusicBrainz IDs and file paths unchanged; no redownload."
