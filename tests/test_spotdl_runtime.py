"""The Spotify subprocess boundary cannot partially publish or change yt-dlp."""
import asyncio
import sys
from pathlib import Path

import pytest

from common import atomic_json
from http_policy import ApiDeferred
from runtime import DeferredOperation, ProcessError
from spotify_provider import Spotify
from spotdl_runtime import SpotDLRuntime
import spotdl_runtime

PLAYLIST = "https://open.spotify.com/playlist/" + "P" * 22
TRACK = "https://open.spotify.com/track/" + "T" * 22
SOURCE = {"url": PLAYLIST, "source_key": "playlist:" + "P" * 22, "user_id": "admin"}


def snapshot_data(**overrides):
    return {"schema_version": 1, "ok": True, "mode": "scan", "provider": "spotify",
            "url": PLAYLIST, "title": "Playlist", "revision": "revision-1", "complete": True,
            "total_items": 1, "skipped": 0, "entries": [
                {"key": "T" * 22, "url": TRACK, "title": "Artist - Song", "artists": ["Artist"],
                 "added_at": None, "basis": "first_seen", "entry_id": "spotify:0", "position": 0},
            ], **overrides}


def test_public_snapshot_keeps_missing_dates_unknown_and_enters_audio_pipeline():
    result = Spotify.public_snapshot(SOURCE, snapshot_data(), lambda *args: None)
    entry = result.entries[0]
    assert entry.added_at is None and entry.basis == "first_seen"
    assert entry.resolve_audio and not entry.downloadable
    assert entry.channel == "Artist"


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"total_items": 2}, {"skipped": -1}, {"total_items": True},
    {"revision": None}, {"url": "https://evil.example/playlist"},
    {"entries": [None]},
])
def test_partial_or_malformed_snapshots_are_never_committed(changes):
    with pytest.raises(ApiDeferred):
        Spotify.public_snapshot(SOURCE, snapshot_data(**changes), lambda *args: None)


def test_repeated_songs_keep_separate_occurrences():
    data = snapshot_data(total_items=2)
    data["entries"].append({**data["entries"][0], "position": 1, "entry_id": "spotify:1"})
    result = Spotify.public_snapshot(SOURCE, data, lambda *args: None)
    assert [entry.key for entry in result.entries] == ["T" * 22] * 2
    assert len({entry.entry_id for entry in result.entries}) == 2
    data["entries"][1]["position"] = 0
    with pytest.raises(ApiDeferred):
        Spotify.public_snapshot(SOURCE, data, lambda *args: None)


def test_direct_track_response_cannot_substitute_another_spotify_id():
    source = {**SOURCE, "url": TRACK, "source_key": "track:" + "T" * 22}
    data = snapshot_data(url=TRACK)
    assert Spotify.public_snapshot(source, data, lambda *args: None).entries[0].key == "T" * 22
    data["entries"][0].update(key="U" * 22, url="https://open.spotify.com/track/" + "U" * 22)
    with pytest.raises(ApiDeferred):
        Spotify.public_snapshot(source, data, lambda *args: None)


def test_public_url_scan_needs_no_oauth_settings(db, monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_REDIRECT_URI", raising=False)
    runtime = SpotDLRuntime(db)
    async def request(mode, url, report):
        assert mode == "scan" and url == PLAYLIST
        return snapshot_data()
    monkeypatch.setattr(runtime, "_request", request)
    assert asyncio.run(runtime.scan(SOURCE, lambda *args: None)).entries[0].resolve_audio


def test_failed_update_preserves_active_runtime(db, monkeypatch):
    runtime = SpotDLRuntime(db)
    previous = {"python": sys.executable, "version": "old"}
    atomic_json(runtime.active_file, previous)
    monkeypatch.setattr(spotdl_runtime.shutil, "which", lambda command: "/usr/bin/uv")
    async def fail(*args, **kwargs):
        raise RuntimeError("install failed")
    monkeypatch.setattr(spotdl_runtime, "run_process", fail)
    with pytest.raises(RuntimeError, match="install failed"):
        asyncio.run(runtime.update())
    assert runtime.active() == previous
    assert db.setting("spotdl_update")["status"] == "FAILED"
    assert not list(runtime.root.glob("spotdl-*"))


@pytest.mark.parametrize("pointer", [{"python": None}, {"python": sys.executable}, []])
def test_invalid_active_pointer_has_an_actionable_repair_error(db, pointer):
    runtime = SpotDLRuntime(db)
    atomic_json(runtime.active_file, pointer)
    with pytest.raises(RuntimeError, match="Update spotDL"):
        runtime.active()


def test_update_validates_bridge_then_switches_and_rolls_back(db, monkeypatch):
    runtime = SpotDLRuntime(db)
    previous = {"python": sys.executable, "version": "old"}
    atomic_json(runtime.active_file, previous)
    held = runtime.root / "spotdl-still-in-use"
    held.mkdir()
    runtime.in_use[str(held)] = 1
    monkeypatch.setattr(spotdl_runtime.shutil, "which", lambda command: "/usr/bin/uv")
    calls = []
    async def run(args, report, **kwargs):
        calls.append([str(arg) for arg in args])
        if args[1] == "venv":
            path = Path(args[-1]) / "bin" / "python"
            path.parent.mkdir(parents=True)
            path.touch()
        elif "smoke" in args:
            assert runtime.active() == previous
            atomic_json(Path(args[-1]), {"schema_version": 1, "ok": True, "mode": "smoke", "version": "4.6.1"})
    monkeypatch.setattr(spotdl_runtime, "run_process", run)
    asyncio.run(runtime.update())
    assert runtime.active()["version"] == "4.6.1"
    assert db.setting("spotdl_update")["status"] == "OK"
    assert held.is_dir()
    assert "spotdl>=4.6.0,<5" in calls[1]
    assert all("yt-dlp[default]" not in call for call in calls)
    assert not (db.path.parent / "runtimes" / "active.json").exists()
    asyncio.run(runtime.rollback())
    assert runtime.active() == previous


def test_incompatible_updated_bridge_does_not_activate(db, monkeypatch):
    runtime = SpotDLRuntime(db)
    previous = {"python": sys.executable, "version": "old"}
    atomic_json(runtime.active_file, previous)
    monkeypatch.setattr(spotdl_runtime.shutil, "which", lambda command: "/usr/bin/uv")
    async def run(args, report, **kwargs):
        if args[1] == "venv":
            path = Path(args[-1]) / "bin" / "python"
            path.parent.mkdir(parents=True)
            path.touch()
        elif "smoke" in args:
            atomic_json(Path(args[-1]), {"schema_version": 2, "ok": True, "mode": "smoke", "version": "5.0"})
    monkeypatch.setattr(spotdl_runtime, "run_process", run)
    with pytest.raises(RuntimeError, match="Unsupported"):
        asyncio.run(runtime.update())
    assert runtime.active() == previous


def test_retryable_bridge_failure_releases_runtime_and_temporary_files(db, monkeypatch):
    runtime = SpotDLRuntime(db)
    atomic_json(runtime.active_file, {"python": sys.executable, "version": "test"})
    async def fail(args, report, **kwargs):
        output = Path(args[args.index("--output") + 1])
        atomic_json(output, {"schema_version": 1, "ok": False, "mode": "scan",
                             "retryable": True, "error": "Playlist changed"})
        raise ProcessError(75, [])
    monkeypatch.setattr(spotdl_runtime, "run_process", fail)
    with pytest.raises(DeferredOperation, match="Playlist changed"):
        asyncio.run(runtime.scan(SOURCE, lambda *args: None))
    assert not runtime.in_use
    assert not list((runtime.root / "operations").iterdir())
    with pytest.raises(DeferredOperation, match="shared Spotify provider cooldown"):
        asyncio.run(runtime._request("scan", PLAYLIST, lambda *args: None))


@pytest.mark.parametrize("youtube_url", [PLAYLIST, "https://youtube.com/playlist?list=PLtest", "https://evil.example/watch?v=abcdefghijk"])
def test_audio_search_accepts_only_single_youtube_targets(db, monkeypatch, youtube_url):
    runtime = SpotDLRuntime(db)
    async def request(*args, **kwargs):
        return {"spotify_url": TRACK, "youtube_url": youtube_url}
    monkeypatch.setattr(runtime, "_request", request)
    with pytest.raises(ValueError):
        asyncio.run(runtime.resolve_audio(TRACK, lambda *args: None))
