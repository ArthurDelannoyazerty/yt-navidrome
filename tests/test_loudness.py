"""Opus loudness compatibility, forced measurement and encoded-audio invariants."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from mutagen.oggopus import OggOpus

from audio_tags import LOUDNESS_POLICY, inspect_tags, loudness_values, write_loudness_tags
from beets_bridge import Bridge


def opus(path, volume=1):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        f"sine=frequency=440:duration=2,volume={volume}", "-c:a", "libopus", str(path),
    ], check=True, timeout=20)
    return path


def encoded_hash(path):
    return subprocess.check_output([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-c", "copy", "-f", "hash", "-",
    ], timeout=20)


def test_raw_gain_peak_and_discovery_without_reencoding(tmp_path):
    path = opus(tmp_path / "audio.opus")
    before = encoded_hash(path)
    raw = OggOpus(path)
    raw["REPLAYGAIN_TRACK_GAIN"] = ["+99 dB"]
    raw["R128_ALBUM_GAIN"] = ["123"]
    raw.save()
    write_loudness_tags(path, loudness_values(-5.0, 0.25))
    Bridge.write_discovery_tags(path, "2020-01-02T03:04:05Z")
    result = inspect_tags(path, "2020-01-02T03:04:05Z")
    assert result["issues"] == []
    assert result["r128_track_gain"] == -1280
    assert result["navidrome_gain_db"] == 0.0
    assert result["replaygain_track_peak"] == 0.25
    assert result["policy"] == LOUDNESS_POLICY
    assert result["comment"] == result["description"] == "Discovery Date: 2020-01-02--03-04-05 UTC"
    assert "replaygain_track_gain" not in OggOpus(path)
    assert "r128_album_gain" not in OggOpus(path)
    assert encoded_hash(path) == before


@pytest.mark.parametrize("gain,peak", [
    (float("nan"), 1), (0, 0), (0, float("inf")), (128, 1),
])
def test_invalid_or_silent_measurement_is_rejected(gain, peak):
    with pytest.raises(ValueError):
        loudness_values(gain, peak)


def test_audit_is_read_only_and_detects_missing_peak(tmp_path):
    path = opus(tmp_path / "old.opus")
    raw = OggOpus(path)
    raw["R128_TRACK_GAIN"] = ["-2662"]
    raw.save()
    before = path.read_bytes()
    audit = inspect_tags(path, None)
    assert audit["navidrome_gain_db"] == -5.3984375
    assert "LOUDNESS_MISSING" in audit["issues"]
    assert "LOUDNESS_POLICY_UNKNOWN" in audit["issues"]
    assert path.read_bytes() == before


def test_real_beets_quiet_and_loud_measurements_keep_audio(environment, db, tmp_path):
    """Pinned-beets integration, with networking forbidden and a 20 dB input gap."""
    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1":
            pytest.fail("CI must install pinned beets")
        pytest.skip("beets is not installed")
    loud = opus(tmp_path / "loud.opus", 1)
    quiet = opus(tmp_path / "quiet.opus", 0.1)
    before = [encoded_hash(path) for path in (loud, quiet)]
    script = '''
import sys
from unittest.mock import patch
from beets.library import Item
from beets_bridge import Bridge
from audio_tags import write_loudness_tags, inspect_tags
b = Bridge("admin")
values = []
with patch("requests.sessions.Session.send", side_effect=AssertionError("No network allowed")):
    for path in sys.argv[1:]:
        item = Item.from_path(path)
        item.r128_track_gain = -99
        result = b.measure_loudness(item)
        values.append(result)
        item.write()
        b.write_discovery_tags(path, None)
        write_loudness_tags(path, result)
        assert not inspect_tags(path, None)["issues"]
        assert abs(result["integrated_lufs"] + result["navidrome_gain_db"] + 18) < 0.01
assert 19.5 < values[1]["navidrome_gain_db"] - values[0]["navidrome_gain_db"] < 20.5
assert values[1]["replaygain_track_peak"] < values[0]["replaygain_track_peak"]
'''
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, str(loud), str(quiet)],
        env=os.environ | {"PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [encoded_hash(path) for path in (loud, quiet)] == before
