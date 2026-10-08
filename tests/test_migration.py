import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "import_legacy.py"
    spec = importlib.util.spec_from_file_location("legacy_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_dry_run_and_apply(environment, tmp_path):
    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as con:
        con.executescript("""
        CREATE TABLE users(username TEXT);
        INSERT INTO users VALUES ('admin');
        CREATE TABLE tracks(track_uuid TEXT, source_url TEXT, user_id TEXT, title TEXT,
          discovery_date TEXT,status TEXT,file_path TEXT,matched_title TEXT,mbid TEXT);
        INSERT INTO tracks VALUES ('legacy-track','https://www.youtube.com/watch?v=abcdefghijk',
          'admin','Artist - Song','2021-04-17','COMPLETED','/old/library/admin/song.opus',
          'Artist - Song',NULL);
        CREATE TABLE playlist_memberships(track_uuid TEXT,user_id TEXT,playlist_name TEXT);
        INSERT INTO playlist_memberships VALUES ('legacy-track','admin','Old Playlist');
        CREATE TABLE monitored_urls(user_id TEXT,url TEXT,label TEXT);
        """)
    audio = environment[1] / "admin" / "song.opus"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    args = SimpleNamespace(
        database=old,
        old_library_root=Path("/old/library"),
        old_working_directory=Path("/app/src"),
        apply=False,
        queue_reprocess=False,
    )
    tool = load_tool()
    report = tool.import_legacy(args)
    assert report["preserved_files"] == 1
    assert not (environment[0] / "ingestor.sqlite").exists()
    args.apply = True
    tool.import_legacy(args)
    from store import Store
    db = Store()
    track = db.one("SELECT * FROM tracks")
    assert track["discovered_at"] is None
    assert track["discovery_basis"] == "legacy_date_only:2021-04-17"
    assert db.current_asset(track["id"])["path"] == str(audio)
