"""English-first MusicBrainz display names and canonical native-name Opus tags.

Aliases may be translations or transliterations. Never invent a translation.
Use ORIGINALTITLE/ORIGINALARTIST/ORIGINALALBUM for the canonical MusicBrainz
strings; these refer to the native/canonical *name*, not an older recording.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections import Counter

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


def english_release_language(value):
    """MusicBrainz release text-language codes (not release countries/scripts)."""
    code = str(value or "").strip().casefold().replace("_", "-")
    return code in {"en", "eng"} or code.startswith(("en-", "eng-"))


def _english_track_title(title, canonical):
    """A genuine Latin-script alternate title, never an invented translation.

    Require an English-labelled *release* separately. Latin letters alone are
    not proof of English; they only reject the obvious wrong-script candidate.
    """
    value = str(title or "").strip()[:1000]
    return (bool(value) and value != str(canonical or "").strip()
            and any(c.isalpha() and "LATIN" in unicodedata.name(c, "") for c in value)
            and not any(c.isalpha() and "LATIN" not in unicodedata.name(c, "")
                        for c in value))


def preferred_recording_title(con, recording_id, recording, *,
                              known_alias=None, assigned_release_id=None,
                              pseudo=None):
    """One shared, read-only title policy for Approval and Language.

    Prefer exact recording English aliases, then English titles on *official*
    releases with the SAME recording MBID, then English pseudo-release track
    titles. An unrelated work, album or text-similar recording is never used.

    Official fallback is only considered when the canonical title is not in
    Latin script: distinct official track titles are not automatically assumed
    to be translations of already-English recording titles.
    """
    canonical = str((recording or {}).get("title") or "").strip()[:1000]
    base = {"title": canonical, "source": "canonical",
            "release_id": None, "language": None}
    if not canonical or not recording_id:
        return base
    alias = str(known_alias or preferred_english_name(recording)).strip()[:1000]
    if alias and alias != canonical:
        return {"title": alias, "source": "recording_alias",
                "release_id": None, "language": "en"}

    # Only translate a non-Latin canonical title. A distinct English-language
    # version name of an English recording can denote a remix or alternate
    # title rather than a translation.
    non_latin = any(char.isalpha() and "LATIN" not in unicodedata.name(char, "")
                    for char in canonical)
    if non_latin:
        candidates = []
        # release_tracks is written atomically after a *complete* exact-release
        # lookup. Its (recording_id, release_id) index bounds lookup cost.
        rows = con.execute(
            """SELECT t.title,t.release_id,r.data FROM release_tracks AS t
               JOIN entities AS r ON r.kind='release' AND r.id=t.release_id
               WHERE t.recording_id=? ORDER BY t.release_id,t.medium,t.position
               LIMIT 240""", (recording_id,),
        ).fetchall()
        for row in rows:
            try:
                release = json.loads(row["data"])
                if str(release.get("status") or "").casefold() != "official":
                    continue
                language = (release.get("text-representation") or {}).get("language")
                if not english_release_language(language):
                    continue
                title = str(row["title"] or "").strip()[:1000]
                if _english_track_title(title, canonical):
                    candidates.append((title, str(row["release_id"]), language))
            except (ValueError, TypeError, AttributeError):
                continue
        if candidates:
            counts = Counter(title.casefold() for title, _, _ in candidates)
            title, release_id, language = min(
                candidates,
                key=lambda c: (c[1] != assigned_release_id,
                               -counts[c[0].casefold()], c[1], c[0]),
            )
            return {"title": title, "source": "official_release",
                    "release_id": release_id, "language": language}

    if pseudo is None:
        # Reuse the durable Approval pseudo-title cache (when initialized).
        # Language requires no network request during the read-only review.
        try:
            row = con.execute(
                """SELECT payload FROM approval_prefetch_cache
                   WHERE kind='pseudo_title' AND cache_key=?""",
                (recording_id,),
            ).fetchone()
            if row:
                pseudo = json.loads(row["payload"])
        except (sqlite3.OperationalError, TypeError, ValueError):
            pseudo = None
    if isinstance(pseudo, dict) and _english_track_title(pseudo.get("title"), canonical):
        release_id = str(pseudo.get("release_id") or "")
        if release_id:
            return {"title": str(pseudo["title"]).strip()[:1000],
                    "source": "pseudo_release", "release_id": release_id,
                    "language": str(pseudo.get("language") or "")}
    return base


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
