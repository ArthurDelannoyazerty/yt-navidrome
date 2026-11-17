"""Real beets/ffmpeg integration with fixture metadata and no remote traffic."""
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
    assert config["languages"] == ["en"]
    assert config["lyrics"]["synced"] is True
    assert config["lyrics"]["sources"] == ["lrclib"]


@pytest.mark.parametrize("user", ["admin", "guest"])
def test_real_beets_adapter_and_raw_discovery_tags(environment, db, user, tmp_path):
    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1":
            pytest.fail("CI must install the pinned beets and configured plugins")
        pytest.skip("beets is not installed in this execution environment")
    assert shutil.which("ffmpeg"), "ffmpeg is required for the integration smoke test"

    audio = tmp_path / f"fixture-{user}.opus"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=4", "-c:a", "libopus", str(audio),
        ],
        check=True,
    )

    script = r'''
import os, sys
from pathlib import Path
from shutil import copyfile
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from mediafile import MediaFile
from mutagen.oggopus import OggOpus
from beets.autotag import AlbumInfo, TrackInfo, Distance, TrackMatch, Proposal
from beets.autotag.match import _recommendation
from beets.library import Item
from beets.util.lyrics import Lyrics
from beets_bridge import Bridge

user, path = sys.argv[1], Path(sys.argv[2])
b = Bridge(user)

def no_network(*args, **kwargs):
    raise AssertionError("Integration test unexpectedly attempted a network request")

with patch("requests.sessions.Session.send", no_network):
    fixture_track = TrackInfo(
        title="Fixture Song",
        artists=["Fixture Artist"],
        track_id="11111111-1111-4111-8111-111111111111",
        data_source="MusicBrainz",
        index=1,
        medium=1,
        medium_index=1,
    )
    fixture_album = AlbumInfo(
        tracks=[fixture_track],
        album="Real Artist Album",
        artists=["Fixture Artist"],
        album_id="22222222-2222-4222-8222-222222222222",
        year=2004,
        month=5,
        day=6,
        original_year=1999,
        original_month=3,
        original_day=4,
        data_source="MusicBrainz",
    )

    # Verify our web adapter preserves beets' own recommendation behavior.
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
    b.release_metadata = lambda *args, **kwargs: (
        fixture_album,
        fixture_track.merge_with_album(fixture_album),
    )
    # MusicBrainz canonical spellings are obtained separately from beets'
    # English-preferred display data. No network is used in this fixture.
    b.canonical_names = lambda recording, release=None: {
        "title": "原曲の題名", "artist": "原作家",
        "album": "元のアルバム" if release else "",
    }
    b.plugins["fetchart"].art_for_album = lambda *args, **kwargs: SimpleNamespace(
        path=os.fsencode(cover)
    )
    b.plugins["lyrics"].find_lyrics = lambda *args, **kwargs: Lyrics(
        "[00:00.000]This is a test song\n[00:02.000]These words have timing",
        "lrclib",
        "https://example.test/fixture",
        language="EN",
    )

    track = {
        "id": "fixture-" + user,
        "user_id": user,
        "title": "Fixture Artist - Fixture Song",
        "source_url": "https://example.test/track",
        "discovered_at": "2021-04-17T21:36:42Z",
    }
    selected = {
        "mbid": fixture_track.track_id,
        "tags": fixture_track.item_data,
    }
    # Album metadata now requires an explicitly approved edition; recording approval alone
    # deliberately leaves release-specific dates and album tags unresolved.
    result = b.finalize(track, path, selected, operation="test-operation",
                        overrides={"release_id": fixture_album.album_id})
    final = Path(result["file_path"])
    assert final.is_relative_to(Path(os.environ["NAVIDROME_LIB_DIR"]) / user)

    tags = MediaFile(str(final))
    raw = OggOpus(final)
    expected = "Discovery Date: 2021-04-17--21-36-42 UTC"
    assert tags.year == 1999 and tags.month == 3 and tags.day == 4
    assert tags.album == "Real Artist Album"
    assert raw["originaltitle"] == ["原曲の題名"]
    assert raw["originalartist"] == ["原作家"]
    assert raw["originalalbum"] == ["元のアルバム"]
    assert raw["ingestor_language_policy"] == ["english-first-v1"]
    assert tags.comments == expected
    assert raw["comment"] == [expected]
    assert raw["description"] == [expected]
    assert "[00:02.000]" in tags.lyrics
    assert tags.images, "Artwork was not embedded"
    assert tags.r128_track_gain is not None
    assert not final.with_suffix(".lrc").exists()

    # Reapproving an existing recording must never let beets rename its
    # occupied permanent path to audio/<track-id>.1.opus. This was observed
    # in production after the move-only stable-path migration.
    track["beets_id"] = result["beets_id"]
    track["file_path"] = str(final)
    candidate = path.with_name("reapprove-" + user + ".opus")
    copyfile(final, candidate)
    replacement = b.finalize(track, candidate, selected,
                             operation="test-reapproval",
                             overrides={"release_id": fixture_album.album_id})
    assert replacement["file_path"] == str(final)
    assert final.exists() and not final.with_name(final.stem + ".1.opus").exists()
    assert Path(b.find_existing(track).path.decode()) == final

    # A different or unregistered asset must not be allowed to overwrite an
    # occupied slot; it must fail instead of allocating a numbered path.
    invalid = {**track, "file_path": None}
    try:
        b.finalize(invalid, candidate, selected, operation="test-collision")
    except ValueError as error:
        assert "Stable audio destination is already occupied" in str(error)
    else:
        raise AssertionError("Unowned audio destination was overwritten")

    # Updating discovery history must preserve all music metadata and both raw tags.
    track["discovered_at"] = "2019-01-02T03:04:05Z"
    b.finalize(track, final, None, mode="comment")
    tags = MediaFile(str(final))
    raw = OggOpus(final)
    expected = "Discovery Date: 2019-01-02--03-04-05 UTC"
    assert tags.year == 1999 and tags.album == "Real Artist Album"
    assert tags.comments == expected
    assert raw["comment"] == [expected]
    assert raw["description"] == [expected]
    assert "[00:02.000]" in tags.lyrics

    audit = b.audit_many([track])
    assert audit[track["id"]]["found"] is True
    assert Path(audit[track["id"]]["path"]) == final

    removed = b.delete(track)
    assert removed["removed"] is True
    assert not final.exists()
    assert b.find_existing(track) is None

print("Real beets adapter integration passed")
'''
    env = os.environ | {"PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", script, user, str(audio)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
