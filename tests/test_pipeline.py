import asyncio
import json
from pathlib import Path

import pytest
from common import atomic_json
from pipeline import Pipeline, write_playlist
from providers import Entry, Snapshot, parse_source


def enqueue(db, track, mode="auto"):
    db.enqueue("track", track["id"], track["user_id"], {"mode": mode})
    return db.claim()


def test_playlist_duplicates_relative_paths_and_empty_export(db, track, environment):
    file = environment[1] / "admin" / "Artist" / "Release" / "Song.opus"
    file.parent.mkdir(parents=True); file.write_bytes(b"audio")
    db.update("tracks", track["id"], file_path=str(file), status="COMPLETED")
    source = db.one("SELECT * FROM sources")
    entries = [Entry("abcdefghijk", track["url"], "Title", "2020-01-01T01:00:00Z", str(i), i) for i in range(2)]
    db.apply_snapshot(source, Snapshot("My playlist", entries))
    source = db.owned("sources", source["id"], "admin")
    write_playlist(db, source)
    output = Path(db.owned("sources", source["id"], "admin")["playlist_path"])
    assert output.read_text().count("../Artist/Release/Song.opus") == 2
    db.apply_snapshot(source, Snapshot("My playlist", []))
    write_playlist(db, source)
    assert output.read_text() == "#EXTM3U\n"


def test_playlist_cannot_escape_user_root(db, track, environment):
    outside = environment[1] / "guest" / "other.opus"
    outside.parent.mkdir(); outside.write_bytes(b"audio")
    db.update("tracks", track["id"], file_path=str(outside))
    with pytest.raises(ValueError, match="outside"):
        write_playlist(db, db.one("SELECT * FROM sources"))


def test_ambiguous_match_retains_audio_and_waits(db, track, environment, monkeypatch):
    pipeline = Pipeline(db)
    async def download(url, track_id, directory, report):
        file = directory / f"temp_{track_id}.opus"; file.write_bytes(b"audio"); return file
    async def bridge(job, track, directory, mode, **kwargs):
        assert mode == "identify"
        return {"automatic": False, "recommendation": "medium", "choices": [{"tags": {"title": "Candidate"}, "similarity": 91}]}
    monkeypatch.setattr(pipeline.downloader, "download", download)
    monkeypatch.setattr(pipeline, "bridge", bridge)
    asyncio.run(pipeline.process_track(enqueue(db, track)))
    updated = db.owned("tracks", track["id"], "admin")
    assert updated["status"] == "NEEDS_APPROVAL"
    assert Path(updated["temp_path"]).is_file()
    assert json.loads(updated["choices"])[0]["similarity"] == 91


def test_failed_redownload_keeps_old_file(db, track, environment, monkeypatch):
    old = environment[1] / "admin" / "old.opus"; old.parent.mkdir(); old.write_bytes(b"last good")
    db.update("tracks", track["id"], status="COMPLETED", file_path=str(old))
    pipeline = Pipeline(db)
    async def fail(*args, **kwargs):
        raise RuntimeError("blocked")
    monkeypatch.setattr(pipeline.downloader, "download", fail)
    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(pipeline.process_track(enqueue(db, track, "redownload")))
    assert old.read_bytes() == b"last good"


def test_success_updates_every_playlist_and_then_removes_old(db, track, environment, monkeypatch):
    old = environment[1] / "admin" / "old.opus"; old.parent.mkdir(); old.write_bytes(b"old")
    final = old.parent / "Artist" / "Release" / "new.opus"
    db.update("tracks", track["id"], status="COMPLETED", file_path=str(old))
    other = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(other, Snapshot("Other", [Entry("abcdefghijk", track["url"], track["title"], track["discovered_at"], "entry", 0)]))
    db.execute("UPDATE jobs SET state='DONE'")
    pipeline = Pipeline(db)
    async def bridge(job, track, directory, mode, **kwargs):
        assert mode == "apply"
        final.parent.mkdir(parents=True); final.write_bytes(b"new")
        return {"file_path": str(final), "beets_id": 1, "matched_title": "Artist - New", "mbid": None, "release_id": None}
    monkeypatch.setattr(pipeline, "bridge", bridge)
    asyncio.run(pipeline.process_track(enqueue(db, track, "retag")))
    assert final.exists() and not old.exists()
    for source in db.rows("SELECT * FROM sources"):
        assert "../Artist/Release/new.opus" in Path(source["playlist_path"]).read_text()


def test_recover_move_before_ledger_commit_without_redownload(db, track, environment, monkeypatch):
    pipeline = Pipeline(db)
    job = enqueue(db, track)
    payload = json.loads(job["payload"])
    stage = environment[0] / "staging" / track["id"]; stage.mkdir(parents=True)
    final = environment[1] / "admin" / "recovered.opus"; final.parent.mkdir(); final.write_bytes(b"audio")
    atomic_json(stage / "operation.json", {"operation": payload["operation"], "previous_path": None})
    atomic_json(stage / "apply-request.json", {"operation": payload["operation"]})
    async def bridge(job, track, directory, mode, **kwargs):
        assert mode == "inspect"
        return {"found": True, "complete": True, "file_path": str(final), "beets_id": 1, "matched_title": "Artist - Recovered", "mbid": None, "release_id": None}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Must not redownload")
    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", forbidden)
    asyncio.run(pipeline.process_track(job))
    assert db.owned("tracks", track["id"], "admin")["file_path"] == str(final)


def test_source_failure_preserves_old_memberships(db, track, monkeypatch):
    from providers import PROVIDERS
    before = db.rows("SELECT * FROM memberships")
    source = db.one("SELECT * FROM sources")
    def fail(*args):
        raise RuntimeError("API outage")
    monkeypatch.setattr(PROVIDERS["youtube"], "resolve", fail)
    db.enqueue("sync", source["id"], "admin")
    with pytest.raises(RuntimeError, match="outage"):
        asyncio.run(Pipeline(db).process_source(db.claim()))
    assert db.rows("SELECT * FROM memberships") == before


def test_delete_track_removes_only_current_user_and_rewrites_playlist(db, track, environment, monkeypatch):
    admin_file = environment[1] / "admin" / "Artist" / "Release" / "Song.opus"
    admin_file.parent.mkdir(parents=True)
    admin_file.write_bytes(b"admin")
    db.update("tracks", track["id"], file_path=str(admin_file), status="COMPLETED", beets_id=1)
    admin_source = db.one("SELECT * FROM sources WHERE user_id='admin'")
    write_playlist(db, admin_source)
    admin_source = db.owned("sources", admin_source["id"], "admin")
    playlist = Path(admin_source["playlist_path"])
    assert "Song.opus" in playlist.read_text()

    guest_source = db.add_source(parse_source("https://youtube.com/playlist?list=PLguest"), "guest")
    entry = Entry("abcdefghijk", track["url"], track["title"], track["discovered_at"], "guest-entry", 0)
    db.apply_snapshot(guest_source, Snapshot("Guest playlist", [entry]))
    db.execute("UPDATE jobs SET state='DONE'")
    guest_track = db.one("SELECT * FROM tracks WHERE user_id='guest'")
    guest_file = environment[1] / "guest" / "Artist" / "Release" / "Song.opus"
    guest_file.parent.mkdir(parents=True)
    guest_file.write_bytes(b"guest")
    db.update("tracks", guest_track["id"], file_path=str(guest_file), status="COMPLETED", beets_id=2)

    pipeline = Pipeline(db)

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "delete"
        assert current["user_id"] == "admin"
        admin_file.unlink(missing_ok=True)
        return {"removed": True}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    db.queue_track(track["id"], "admin", {"mode": "delete"})
    job = db.claim()
    asyncio.run(pipeline.process_track(job))

    assert db.one("SELECT * FROM tracks WHERE id=?", (track["id"],)) is None
    assert db.one("SELECT * FROM tracks WHERE id=? AND user_id='guest'", (guest_track["id"],))
    assert guest_file.read_bytes() == b"guest"
    assert playlist.read_text() == "#EXTM3U\n"
