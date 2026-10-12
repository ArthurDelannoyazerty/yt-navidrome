"""Regressions for MusicBrainz release membership mismatches on approved audio."""
from types import SimpleNamespace

import pytest

from beets_bridge import Bridge
from http_policy import ApiDeferred

RECORDING = "11111111-1111-4111-8111-111111111111"
OTHER_RECORDING = "22222222-2222-4222-8222-222222222222"
EARLY_RELEASE = "33333333-3333-4333-8333-333333333333"
LATER_RELEASE = "44444444-4444-4444-8444-444444444444"


class FakeTrack:
    def __init__(self, recording_id):
        self.track_id = recording_id

    def merge_with_album(self, album):
        return {"mb_trackid": self.track_id, "mb_albumid": album.id}


def bridge_with_releases(releases, album_tracks, *, deferred=None):
    browsed, fetched = [], []

    def get_json(url, params):
        browsed.append(params.copy())
        assert url == "https://musicbrainz.org/ws/2/release"
        return {"releases": releases, "release_count": len(releases)}

    def album_for_id(rid):
        fetched.append(rid)
        if deferred and rid == deferred:
            raise ApiDeferred("MusicBrainz service unavailable")
        recordings = album_tracks.get(rid)
        return None if recordings is None else SimpleNamespace(
            id=rid, tracks=[FakeTrack(rec) for rec in recordings]
        )

    fake = SimpleNamespace(
        mb_api=SimpleNamespace(api_root="https://musicbrainz.org/ws/2", get_json=get_json),
        album_for_id=album_for_id,
    )
    bridge = Bridge.__new__(Bridge)
    bridge.plugins = {"musicbrainz": fake}
    bridge.raise_if_deferred = lambda: None
    return bridge, browsed, fetched


def test_automatic_release_skips_incompatible_earliest_edition(capsys):
    bridge, browsed, fetched = bridge_with_releases(
        [{"id": LATER_RELEASE, "status": "Official", "date": "2018-01-01"},
         {"id": EARLY_RELEASE, "status": "Official", "date": "2012-01-01"}],
        {EARLY_RELEASE: [OTHER_RECORDING], LATER_RELEASE: [RECORDING.upper()]},
    )
    album, tags = bridge.release_metadata(RECORDING)
    assert album.id == LATER_RELEASE
    assert tags == {"mb_trackid": RECORDING.upper(), "mb_albumid": LATER_RELEASE}
    assert fetched == [EARLY_RELEASE, LATER_RELEASE]
    assert browsed == [{"recording": RECORDING, "limit": 100, "offset": 0}]
    assert "checking another official edition" in capsys.readouterr().err


def test_all_incompatible_official_releases_preserve_recording_without_album(capsys):
    bridge, _, fetched = bridge_with_releases(
        [{"id": EARLY_RELEASE, "status": "Official", "date": "2012"},
         {"id": LATER_RELEASE, "status": "Official", "date": "2018"}],
        {EARLY_RELEASE: [OTHER_RECORDING], LATER_RELEASE: [OTHER_RECORDING]},
    )
    assert bridge.release_metadata(RECORDING) == (None, None)
    assert fetched == [EARLY_RELEASE, LATER_RELEASE]
    assert "saving the confirmed recording without album/date" in capsys.readouterr().err


def test_missing_automatic_release_does_not_fail_import():
    bridge, _, fetched = bridge_with_releases(
        [{"id": EARLY_RELEASE, "status": "Official", "date": "2012"}],
        {},  # MusicBrainz browse lists it but beets cannot resolve it.
    )
    assert bridge.release_metadata(RECORDING) == (None, None)
    assert fetched == [EARLY_RELEASE]


def test_explicit_mismatch_is_rejected_without_silent_substitution():
    bridge, browsed, fetched = bridge_with_releases(
        [{"id": LATER_RELEASE, "status": "Official", "date": "2018"}],
        {EARLY_RELEASE: [OTHER_RECORDING], LATER_RELEASE: [RECORDING]},
    )
    with pytest.raises(ValueError, match="Select another edition or choose automatic"):
        bridge.release_metadata(RECORDING, explicit_release=EARLY_RELEASE)
    assert fetched == [EARLY_RELEASE]
    assert not browsed


def test_provider_errors_still_defer_instead_of_falling_back():
    bridge, _, fetched = bridge_with_releases(
        [{"id": EARLY_RELEASE, "status": "Official", "date": "2012"},
         {"id": LATER_RELEASE, "status": "Official", "date": "2018"}],
        {EARLY_RELEASE: [OTHER_RECORDING], LATER_RELEASE: [RECORDING]},
        deferred=EARLY_RELEASE,
    )
    with pytest.raises(ApiDeferred, match="service unavailable"):
        bridge.release_metadata(RECORDING)
    assert fetched == [EARLY_RELEASE]


def test_automatic_release_lookups_are_bounded():
    releases = [{"id": f"00000000-0000-4000-8000-{i:012d}", "status": "Official", "date": f"20{i:02d}"}
                for i in range(20)]
    bridge, _, fetched = bridge_with_releases(
        releases, {release["id"]: [OTHER_RECORDING] for release in releases}
    )
    assert bridge.release_metadata(RECORDING) == (None, None)
    assert len(fetched) == 5


def test_unofficial_releases_are_not_selected_automatically():
    bridge, _, fetched = bridge_with_releases(
        [{"id": EARLY_RELEASE, "status": "Bootleg", "date": "2012"}],
        {EARLY_RELEASE: [RECORDING]},
    )
    assert bridge.release_metadata(RECORDING) == (None, None)
    assert not fetched


def test_real_beets_applies_recording_even_if_no_compatible_official_release(environment, db, tmp_path):
    """The user's failing path: downloaded audio must remain usable after approval."""
    import importlib.util
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys

    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1":
            pytest.fail("CI must provide the pinned beets integration dependency")
        pytest.skip("Pinned beets integration is not installed locally")
    audio = tmp_path / "release-mismatch.opus"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=432:duration=4", "-c:a", "libopus", str(audio),
    ], check=True)
    script = r'''
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from mutagen.oggopus import OggOpus
from beets.autotag import AlbumInfo, TrackInfo
from beets_bridge import Bridge

path = Path(sys.argv[1]); b = Bridge("admin")
recording = "11111111-1111-4111-8111-111111111111"
wrong_release = "33333333-3333-4333-8333-333333333333"
wrong_track = TrackInfo(title="Other Song", artists=["Other Artist"],
                        track_id="22222222-2222-4222-8222-222222222222", data_source="MusicBrainz")
wrong_album = AlbumInfo(tracks=[wrong_track], album="Wrong Album", artists=["Other Artist"],
                        album_id=wrong_release, data_source="MusicBrainz")
mb = SimpleNamespace(
    mb_api=SimpleNamespace(api_root="https://musicbrainz.org/ws/2", get_json=lambda *a, **k: {
        "releases": [{"id": wrong_release, "status": "Official", "date": "2016-01-01"}],
        "release_count": 1,
    }),
    album_for_id=lambda rid: wrong_album if rid == wrong_release else None,
)
b.plugins["musicbrainz"] = mb
b.plugins["lyrics"].add_item_lyrics = lambda *a, **k: None
track = {"id": "release-regression", "user_id": "admin", "title": "Ganja White Night - Example",
         "discovered_at": "2021-01-02T03:04:05Z"}
selected = {"mbid": recording, "kind": "candidate", "tags": {
    "mb_trackid": recording, "artist": "Ganja White Night", "title": "Example",
    "mb_albumid": wrong_release, "album": "Stale Album", "year": 1990,
}}
with patch("requests.sessions.Session.send", side_effect=AssertionError("No remote requests in integration test")):
    result = b.finalize(track, str(path), selected, operation="release-mismatch-test")
raw = OggOpus(result["file_path"])
assert result["mbid"] == recording
assert result["release_id"] is None
assert raw["musicbrainz_trackid"] == [recording]
assert not raw.get("musicbrainz_albumid") and not raw.get("album")
assert not raw.get("date")
assert any("Recording identity was applied without album" in w for w in result["warnings"])
assert raw.get("r128_track_gain") and raw.get("replaygain_track_peak")
assert raw["comment"] == raw["description"]
print("Recording-only fallback with real beets passed")
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(audio)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_tag_cleanup_keeps_recording_and_playback_metadata(tmp_path):
    """A fallback clears stale edition tags without destroying useful tags."""
    import subprocess
    from mutagen.oggopus import OggOpus
    from source_metadata import clear_release_tags

    audio = tmp_path / "release-tags.opus"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=432:duration=1", "-c:a", "libopus", str(audio),
    ], check=True)
    tags = OggOpus(audio)
    tags["musicbrainz_trackid"] = [RECORDING]
    tags["musicbrainz_albumid"] = [EARLY_RELEASE]
    tags["musicbrainz_releasegroupid"] = [LATER_RELEASE]
    tags["artist"] = ["Ganja White Night"]
    tags["title"] = ["Example"]
    tags["album"] = ["Wrong compilation"]
    tags["date"] = ["1990"]
    tags["lyrics"] = ["A lyric"]
    tags["r128_track_gain"] = ["-1200"]
    tags["replaygain_track_peak"] = ["0.3"]
    tags["comment"] = ["Discovery Date: 2021-01-02--03-04-05 UTC"]
    tags.save()
    clear_release_tags(audio)
    result = OggOpus(audio)
    assert not any(key in result for key in (
        "album", "date", "musicbrainz_albumid", "musicbrainz_releasegroupid"
    ))
    assert result["musicbrainz_trackid"] == [RECORDING]
    assert result["artist"] == ["Ganja White Night"]
    assert result["title"] == ["Example"]
    assert result["lyrics"] == ["A lyric"]
    assert result["r128_track_gain"] == ["-1200"]
    assert result["replaygain_track_peak"] == ["0.3"]
    assert result["comment"] == ["Discovery Date: 2021-01-02--03-04-05 UTC"]
