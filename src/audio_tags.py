"""Bounded Opus tag inspection and playback metadata; never re-encode audio.

R128 gain is referenced to -23 LUFS. A peak-only ReplayGain compatibility tag
lets Navidrome apply its native R128 conversion with clipping protection.
A second conventional track-gain tag would take precedence, so is not written.
"""
from __future__ import annotations

import math
from pathlib import Path

from mutagen.oggopus import OggOpus

from common import discovery_comment

LOUDNESS_POLICY = "r128-minus23-truepeak-v1"
REPAIRABLE_ISSUES = (
    "DISCOVERY_COMMENT_MISMATCH", "LOUDNESS_MISSING", "LOUDNESS_INVALID",
    "LOUDNESS_POLICY_UNKNOWN", "LOUDNESS_CONFLICT",
)


def loudness_values(gain: float, peak: float) -> dict:
    gain, peak = float(gain), float(peak)
    if not math.isfinite(gain) or not math.isfinite(peak) or peak <= 0:
        raise ValueError("Loudness analysis returned invalid gain/peak (possibly silent audio)")
    quantized = round(gain * 256)
    if not -32768 <= quantized <= 32767:
        raise ValueError("Loudness gain is outside the Opus signed Q7.8 range")
    return {
        "r128_track_gain": quantized,
        "replaygain_track_peak": peak,
        "integrated_lufs": -23.0 - gain,
        "true_peak_dbfs": 20 * math.log10(peak),
        "navidrome_gain_db": quantized / 256.0 + 5.0,
    }


def write_loudness_tags(path: str | Path, values: dict) -> None:
    audio = OggOpus(str(path))
    # A single-track analysis cannot establish album gain. Do not retain stale
    # album values or a conflicting conventional track gain from the source.
    for key in (
        "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK",
        "REPLAYGAIN_REFERENCE_LOUDNESS", "R128_ALBUM_GAIN",
    ):
        audio.pop(key, None)
    audio["R128_TRACK_GAIN"] = [str(values["r128_track_gain"])]
    audio["REPLAYGAIN_TRACK_PEAK"] = [f"{values['replaygain_track_peak']:.9f}"]
    audio["INGESTOR_LOUDNESS_POLICY"] = [LOUDNESS_POLICY]
    audio["INGESTOR_INTEGRATED_LUFS"] = [f"{values['integrated_lufs']:.2f}"]
    audio["INGESTOR_TRUE_PEAK_DBFS"] = [f"{values['true_peak_dbfs']:.2f}"]
    audio.save()


def inspect_tags(path: str | Path, discovered_at: str | None) -> dict:
    """Read actual on-disk values, not just what the database expects to find."""
    expected = discovery_comment(discovered_at)
    result = {
        "expected_discovery": expected, "comment": None, "description": None,
        "r128_track_gain": None, "navidrome_gain_db": None,
        "replaygain_track_peak": None, "policy": None, "issues": [],
    }
    try:
        audio = OggOpus(str(path))
    except Exception as exc:
        # Mutagen's format-specific errors do not share ValueError as a base.
        result["issues"].append("UNREADABLE_TAGS")
        result["error"] = str(exc)
        return result
    first = lambda key: (audio.get(key) or [None])[0]
    result.update(
        comment=first("comment"), description=first("description"),
        policy=first("ingestor_loudness_policy"),
        has_artwork=bool(audio.get("metadata_block_picture")),
        has_lyrics=any(key.startswith("lyrics") and bool(value)
                       for key, value in audio.items()),
    )
    if result["comment"] != expected or result["description"] != expected:
        result["issues"].append("DISCOVERY_COMMENT_MISMATCH")
    raw_gain, raw_peak = first("r128_track_gain"), first("replaygain_track_peak")
    if raw_gain is None or raw_peak is None:
        result["issues"].append("LOUDNESS_MISSING")
    try:
        if raw_gain is not None:
            gain = int(raw_gain)
            if not -32768 <= gain <= 32767:
                raise ValueError("Gain outside Q7.8 range")
            result["r128_track_gain"] = gain
            result["navidrome_gain_db"] = gain / 256.0 + 5
        if raw_peak is not None:
            peak = float(raw_peak)
            if not math.isfinite(peak) or peak <= 0:
                raise ValueError("Peak must be positive and finite")
            result["replaygain_track_peak"] = peak
    except (ValueError, TypeError):
        result["issues"].append("LOUDNESS_INVALID")
    if first("replaygain_track_gain") is not None:
        result["issues"].append("LOUDNESS_CONFLICT")
    if result["policy"] != LOUDNESS_POLICY:
        result["issues"].append("LOUDNESS_POLICY_UNKNOWN")
    return result
