"""Real beets/ffmpeg smoke tests, using fixtures instead of remote services.

Skipped only when local beets is absent. REQUIRE_BEETS=1 makes absence a CI failure.
These are adapter integration tests, not a claim that remote APIs were tested.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_config_keeps_native_match_thresholds():
    config = yaml.safe_load((ROOT / "beets.yaml").read_text())
    assert "match" not in config
    assert config["original_date"] is True
    assert config["lyrics"]["synced"] is True
    assert config["lyrics"]["sources"] == ["lrclib"]


@pytest.mark.parametrize("user", ["admin", "guest"])
def test_real_beets_adapter_with_local_fixtures(environment, db, user, tmp_path):
    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1":
            pytest.fail("CI must install the pinned beets and its configured plugin dependencies")
        pytest.skip("beets is not installed in this execution environment")
    assert shutil.which("ffmpeg"), "ffmpeg is required for the integration smoke test"
    audio = tmp_path / "fixture.opus"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:a", "libopus", str(audio)], check=True)
    script = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from mediafile import MediaFile
from beets import config
from beets.autotag import AlbumInfo, TrackInfo, Distance, TrackMatch, Proposal
from beets.autotag.match import _recommendation
from beets.library import Item
from beets.util.lyrics import Lyrics
from beets_bridge import Bridge

user, path = sys.argv[1], Path(sys.argv[2])
b = Bridge(user)
def no_network(*args, **kwargs):
    raise AssertionError("Integration smoke test unexpectedly attempted a network request")
# All external lookups below are supplied as fixtures. The library, tags, file moves,
# original-date translation, artwork embedding, lyrics writing and gain remain real.
with patch("requests.sessions.Session.send", no_network):
    fixture_track = TrackInfo(title="Fixture Song", artists=["Fixture Artist"], track_id="11111111-1111-4111-8111-111111111111", data_source="MusicBrainz", index=1, medium=1, medium_index=1)
    fixture_album = AlbumInfo(tracks=[fixture_track], album="Real Artist Album", artists=["Fixture Artist"], album_id="22222222-2222-4222-8222-222222222222", year=2004, month=5, day=6, original_year=1999, original_month=3, original_day=4, data_source="MusicBrainz")
    item = Item.from_path(str(path))
    for penalty in (0.01, 0.25):
        distance = Distance()
        distance.add("track_title", penalty)
        match = TrackMatch(distance, fixture_track, item)
        proposal = Proposal([match], _recommendation([match]))
        with patch("beets.autotag.tag_item", return_value=proposal), patch("beets.plugins.send"):
            identified = b.identify(path, "Fixture Artist - Fixture Song")
        assert identified["recommendation"] == proposal.recommendation.name
        assert identified["automatic"] == (proposal.recommendation.name == "strong")
        assert identified["choices"][0]["similarity"] == round((1 - penalty) * 100, 1)
    cover = path.with_suffix(".jpg")
    Image.new("RGB", (64, 64), (10, 20, 30)).save(cover)
    b.release_metadata = lambda *a, **k: (fixture_album, fixture_track.merge_with_album(fixture_album))
    b.plugins["fetchart"].art_for_album = lambda *a, **k: SimpleNamespace(path=os.fsencode(cover))
    b.plugins["lyrics"].find_lyrics = lambda *a, **k: Lyrics("[00:00.000]This is a test song\n[00:02.000]These words have timing", "lrclib", "https://example.test/fixture", language="EN")
    track = {"id": "fixture-" + user, "user_id": user, "title": "Fixture Artist - Fixture Song", "url": "https://example.test/track", "discovered_at": "2021-04-17T21:36:42Z"}
    result = b.finalize(track, path, {"mbid": fixture_track.track_id, "tags": fixture_track.item_data}, operation="test-operation")
    final = Path(result["file_path"])
    assert final.is_relative_to(Path(os.environ["NAVIDROME_LIB_DIR"]) / user)
    tags = MediaFile(str(final))
    assert tags.year == 1999 and tags.month == 3 and tags.day == 4
    assert tags.album == "Real Artist Album"
    assert tags.comments == "Discovery Date: 2021-04-17--21-36-42 UTC"
    assert "[00:02.000]" in tags.lyrics
    assert tags.images, "Artwork was not embedded"
    assert tags.r128_track_gain is not None
    assert not final.with_suffix(".lrc").exists()
    assert final.stat().st_mtime > 1700000000, "File mtime must not be forged to discovery time"
    # A later comment update must retain the recording/release tags and lyrics.
    track["beets_id"] = result["beets_id"]
    track["discovered_at"] = "2020-01-02T03:04:05Z"
    b.finalize(track, final, None, mode="comment")
    tags = MediaFile(str(final))
    assert tags.year == 1999 and tags.album == "Real Artist Album"
    assert tags.comments == "Discovery Date: 2020-01-02--03-04-05 UTC"
    assert "[00:02.000]" in tags.lyrics
print("Real beets adapter smoke test passed")
'''
    env = os.environ | {"PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-c", script, user, str(audio)], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
