"""Retained source metadata and identification evidence, independent of applied tags.

Only a pristine downloaded candidate may seed the archive. Existing library files
are never used to reconstruct original tags: old imports fall back explicitly to
the retained provider title. These additive v2 tables do not rebuild library data.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from mutagen.oggopus import OggOpus

from common import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS origin_metadata (
 origin_id TEXT PRIMARY KEY REFERENCES track_origins(id) ON DELETE CASCADE,
 source_title TEXT NOT NULL,
 tags TEXT NOT NULL,
 basis TEXT NOT NULL,
 captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identification_reports (
 origin_id TEXT PRIMARY KEY REFERENCES track_origins(id) ON DELETE CASCADE,
 report TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
"""
# Descriptive tags only. No URLs, credentials, fingerprints, artwork, lyrics or
# MusicBrainz identifiers from a previously applied match are restored.
SOURCE_TAGS = (
    "title", "artist", "albumartist", "album", "tracknumber", "tracktotal",
    "discnumber", "disctotal", "date", "originaldate", "genre",
)
TECHNICAL_TAGS = {"encoder", "encodedby", "encoding", "encodersettings"}


def title_tags(title: str) -> dict[str, list[str]]:
    artist, delimiter, song = title.partition(" - ")
    if delimiter and artist.strip() and song.strip():
        return {"artist": [artist.strip()], "title": [song.strip()]}
    return {"title": [title.strip() or "Unknown title"]}


def source_snapshot(con, origin: dict) -> dict:
    row = con.execute("SELECT * FROM origin_metadata WHERE origin_id=?", (origin["id"],)).fetchone()
    if row:
        return {"origin_id": origin["id"], "source_title": row["source_title"],
                "tags": json.loads(row["tags"]), "basis": row["basis"],
                "captured_at": row["captured_at"]}
    return {"origin_id": origin["id"], "source_title": origin["title"],
            "tags": title_tags(origin["title"]), "basis": "provider_title_only",
            "captured_at": None}


def snapshot(store, origin: dict) -> dict:
    with store.db() as con:
        return source_snapshot(con, origin)


def capture_source(store, origin: dict, path: Path) -> dict:
    """Archive the first pre-beets download, never overwrite it on reprocessing."""
    tags = {}
    basis = "provider_title_only"
    try:
        raw = OggOpus(path)
        # This is a guard against accidentally archiving an already-processed file.
        processed = any(k.startswith(("musicbrainz", "ingestor_")) for k in raw)
        if not processed:
            for key in SOURCE_TAGS:
                values = [str(v)[:1024] for v in raw.get(key, [])[:16] if str(v).strip()]
                if values:
                    tags[key] = values
            if tags:
                basis = "original_download_tags"
    except Exception:
        # Identification will report an unreadable audio file. Do not invent tags.
        pass
    fallback = title_tags(origin["title"])
    for key, value in fallback.items():
        tags.setdefault(key, value)
    with store.db() as con:
        con.execute(
            "INSERT OR IGNORE INTO origin_metadata VALUES (?,?,?,?,?)",
            (origin["id"], origin["title"], json.dumps(tags), basis, utcnow()),
        )
        return source_snapshot(con, origin)


def source_choice(value: dict) -> dict:
    tags = value["tags"]
    return {"kind": "source", "mbid": None, "title": tags["title"][0],
            "artist": "; ".join(tags.get("artist", [])),
            "album": "; ".join(tags.get("album", [])), "similarity": None,
            "description": "Use original source metadata; clear the applied MusicBrainz identity",
            "source": value}


def clear_descriptive_tags(path: str | Path, source: dict | None = None):
    """Clear stale identity-dependent tags on a staged copy without re-encoding."""
    audio = OggOpus(str(path))
    for key in list(audio):
        if key not in TECHNICAL_TAGS and not key.startswith(("r128_", "replaygain_", "ingestor_")):
            del audio[key]
    if source:
        for key in SOURCE_TAGS:
            if source["tags"].get(key):
                audio[key] = source["tags"][key]
    audio.save()


# beets may write empty/obsolete album values back into a reused media file
# despite resetting its Item fields. Remove release-specific tags after the last
# write, without dropping the selected recording identity, lyrics or loudness.
RELEASE_TAGS = {
    "album", "albumartist", "albumartists", "date", "year", "originaldate",
    "originalyear", "original_year", "tracknumber", "tracktotal",
    "discnumber", "disctotal", "discsubtitle", "media", "releasestatus",
    "releasetype", "releasecountry", "label", "catalognum", "barcode", "asin",
    "musicbrainz_releasetrackid", "musicbrainz_releasegroupid",
}


def clear_release_tags(path: str | Path, album_override: str | None = None):
    """Strip unverified edition tags but honor an explicit manual album title."""
    audio = OggOpus(str(path))
    for key in list(audio):
        if key in RELEASE_TAGS or key.startswith("musicbrainz_album"):
            del audio[key]
    if album_override and album_override.strip():
        audio["album"] = [album_override.strip()]
    audio.save()


def correction_revision(track) -> str:
    fields = ("id", "user_id", "current_asset_id", "current_origin_id", "mbid",
              "release_id", "matched_title", "discovered_at", "operation_state", "pending_operation", "choices", "updated_at")
    return hashlib.sha256(json.dumps([track[k] for k in fields]).encode()).hexdigest()


def prepare_correction(con, track, payload: dict) -> dict:
    """Validate a reviewed source restore / MB selection inside the queue lock."""
    mode = payload["mode"]
    if payload.get("revision") != correction_revision(track):
        raise ValueError("This track changed; reopen the correction dialog and review it again")
    waiting = track["operation_state"] == "NEEDS_APPROVAL"
    has_audio = track["health"] == "AVAILABLE" and track["file_path"]
    if not waiting and not has_audio:
        raise ValueError("Download audio before correcting its metadata")
    pending = json.loads(track["pending_operation"] or "{}") if waiting else {}
    # During approval, correct the staged candidate, not the still-playing old asset.
    origin_id = pending.get("origin_id") if waiting else track["current_origin_id"]
    if payload.get("origin_id") and payload["origin_id"] != origin_id:
        raise ValueError("Restore the source for the current audio/candidate, not another origin")
    origin = con.execute("SELECT * FROM track_origins WHERE id=? AND track_id=? AND user_id=?",
                         (origin_id, track["id"], track["user_id"])).fetchone()
    if not origin:
        raise ValueError("This audio has no retained source origin")
    if mode == "restore_source":
        selected = source_choice(source_snapshot(con, dict(origin)))
        overrides = {}
    else:
        mbid = str(uuid.UUID(payload.get("mbid") or ""))
        overrides = {"mbid": mbid}
        if payload.get("release_id"):
            overrides["release_id"] = str(uuid.UUID(payload["release_id"]))
        selected = {"mbid": mbid, "manual": True}
    if waiting:
        if not pending.get("operation") or not pending.get("origin_id"):
            raise ValueError("The pending candidate has no recovery information; reprocess it first")
        return {**pending, "mode": "resume", "selected": selected, "overrides": overrides,
                "reviewed_correction": True, "selection_id": uuid.uuid4().hex}
    return {"mode": "restore_source" if mode == "restore_source" else "retag",
            "origin_id": origin_id, "selected": selected, "overrides": overrides,
            "reviewed_correction": True, "selection_id": uuid.uuid4().hex}


def save_identification_report(store, origin, track, identified, digest):
    """Keep bounded evidence so a failed match is diagnosable after staging cleanup."""
    report = {"at": utcnow(), "source_title": origin["title"], "origin_id": origin["id"], "source_url": origin["url"],
              "previous_mbid": track.get("mbid"), "previous_title": track.get("matched_title"),
              "candidate_sha256": digest,
              "recommendation": identified.get("recommendation"),
              "automatic": bool(identified.get("automatic")),
              "diagnostics": identified.get("diagnostics", {}),
              "candidate_count": len(identified.get("choices", [])),
              "candidates": [{k: c.get(k) for k in ("mbid", "title", "artist", "similarity", "description")}
                             for c in identified.get("choices", [])[:20]]}
    store.execute(
        "INSERT INTO identification_reports VALUES (?,?,?) ON CONFLICT(origin_id) "
        "DO UPDATE SET report=excluded.report,updated_at=excluded.updated_at",
        (origin["id"], json.dumps(report), report["at"]),
    )
