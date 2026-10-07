import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def migration(environment, tmp_path):
    tool_path = Path(__file__).resolve().parents[1] / "tools" / "import_legacy.py"
    spec = importlib.util.spec_from_file_location("legacy_tool_test", tool_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    olddb = tmp_path / "old.sqlite"
    oldroot = Path("/old/library")
    with sqlite3.connect(olddb) as con:
        con.executescript("""
        CREATE TABLE users(username TEXT);
        INSERT INTO users VALUES ('admin');
        CREATE TABLE tracks(track_uuid TEXT, source_url TEXT, user_id TEXT, title TEXT,
          discovery_date TEXT,status TEXT,file_path TEXT,matched_title TEXT,mbid TEXT);
        INSERT INTO tracks VALUES ('legacy-track','https://www.youtube.com/watch?v=abcdefghijk',
          'admin','Artist - Song','2021-04-17','COMPLETED','/old/library/admin/song.opus','Artist - Song',NULL);
        CREATE TABLE playlist_memberships(track_uuid TEXT,user_id TEXT,playlist_name TEXT);
        INSERT INTO playlist_memberships VALUES ('legacy-track','admin','Old Playlist');
        CREATE TABLE monitored_urls(user_id TEXT,url TEXT,label TEXT);
        INSERT INTO monitored_urls VALUES ('admin','https://youtube.com/playlist?list=PLtest','Online Playlist');
        INSERT INTO monitored_urls VALUES ('admin','https://open.spotify.com/playlist/example','Future provider');
        """)
    audio = environment[1] / "admin" / "song.opus"
    audio.parent.mkdir()
    audio.write_bytes(b"existing audio must not be modified during migration")
    args = SimpleNamespace(database=olddb, old_library_root=oldroot, old_working_directory=Path("/app/src"), apply=False, queue_reidentify=False)
    return module, args, audio


def test_legacy_dry_run_changes_nothing(migration, environment):
    tool, args, audio = migration
    before = args.database.read_bytes(), audio.read_bytes()
    report = tool.import_legacy(args)
    assert report["preserved_files"] == 1 and len(report["unsupported_sources"]) == 1
    assert not (environment[0] / "ingestor.sqlite").exists()
    assert (args.database.read_bytes(), audio.read_bytes()) == before


def test_legacy_import_preserves_audio_and_does_not_invent_time(migration, environment):
    from store import Store
    tool, args, audio = migration
    before = args.database.read_bytes(), audio.read_bytes()
    args.apply = True
    tool.import_legacy(args)
    store = Store()
    track = store.one("SELECT * FROM tracks")
    assert track["id"] == "legacy-track" and track["discovered_at"] is None
    assert track["discovery_basis"] == "legacy_date_only:2021-04-17"
    assert track["file_path"] == str(audio) and track["status"] == "COMPLETED"
    assert store.one("SELECT * FROM jobs WHERE kind='sync'")
    assert list((audio.parent / "000000-playlists").glob("*.m3u"))
    assert (args.database.read_bytes(), audio.read_bytes()) == before
    with pytest.raises(ValueError, match="already exists"):
        tool.import_legacy(args)


def test_legacy_reidentify_queues_existing_audio_without_download(migration):
    from store import Store
    tool, args, _ = migration
    args.apply = True
    args.queue_reidentify = True
    tool.import_legacy(args)
    assert '"mode": "reidentify"' in Store().one("SELECT * FROM jobs WHERE kind='track'")["payload"]
