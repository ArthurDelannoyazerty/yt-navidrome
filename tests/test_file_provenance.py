"""Portable comments, explicit metadata source, and tier-playlist consolidation."""
import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from mutagen.oggopus import OggOpus

from audio_tags import inspect_tags
from beets_bridge import Bridge
from common import canonical_source_urls, music_comment, sha256_file
from main import create_app
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from review_routes import review_queue
from test_reliability import active_track

YT = "https://www.youtube.com/watch?v=abcdefghijk"
SP = "https://open.spotify.com/track/" + "A" * 22


def test_portable_sources_are_canonical_private_parameters_never_written():
    urls = [YT + "&list=PLx&token=secret", "https://youtu.be/abcdefghijk?t=9", SP + "?si=secret",
            "https://user:pw@youtu.be/abcdefghijk", "https://youtube.com.evil.test/watch?v=abcdefghijk",
            "file:///etc/passwd", "https://youtube.com/playlist?list=PLx"]
    assert canonical_source_urls(urls) == [YT, SP]
    assert music_comment(None, urls) == "Discovery Date: unknown\n\nSources:\n" + YT + "\n" + SP
    assert music_comment(None, urls) == music_comment(None, list(reversed(urls)))


def test_source_comments_do_not_reencode_opus(tmp_path):
    path = tmp_path / "audio.opus"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", "-c:a", "libopus", str(path)], check=True)
    def packets():
        return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "hash", "-"])
    before = packets()
    expected = Bridge.write_discovery_tags(path, "2020-01-01T00:00:00Z", [YT, SP])
    assert OggOpus(path)["comment"] == OggOpus(path)["description"] == [expected]
    assert packets() == before
    assert "SOURCE_COMMENT_MISMATCH" not in inspect_tags(path, "2020-01-01T00:00:00Z", [SP, YT])["issues"]
    assert "SOURCE_COMMENT_MISMATCH" in inspect_tags(path, "2020-01-01T00:00:00Z", [YT])["issues"]
    assert "DISCOVERY_COMMENT_MISMATCH" not in inspect_tags(path, "2020-01-01T00:00:00Z")["issues"]


def test_source_filter_requires_evidence_not_just_missing_mbid(db, track, environment):
    current, _, _ = active_track(db, track, environment)
    db.update("tracks", current["id"], mbid=None, metadata_source="unknown")
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_METADATA")["total"] == 0
    db.update("tracks", current["id"], metadata_source="source", metadata_edited=1)
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_METADATA")["total"] == 1
    assert review_queue(db, "admin", "SOURCE_METADATA")["ids"] == [current["id"]]
    assert db.dashboard_tracks("guest", 1, 50, "SOURCE_METADATA")["total"] == 0
    assert db.dashboard_tracks("admin", 1, 50, "MANUAL_METADATA")["total"] == 1


def test_comment_only_backfill_is_scoped_and_does_not_download(db, track, environment, monkeypatch):
    current, _, path = active_track(db, track, environment)
    pipeline = Pipeline(db); calls = []
    async def bridge(job, current, directory, mode, **kwargs):
        calls.append(mode)
        if mode == "source_links":
            Path(kwargs["path"]).write_bytes(b"same-audio-new-comments")
            return {}
        assert mode == "refresh"
        return {"beets_id": 1}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Source comments must not download")
    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", forbidden)
    assert db.queue_bulk("guest", "source_links")["queued"] == 0
    assert db.queue_bulk("admin", "source_links")["queued"] == 1
    asyncio.run(pipeline.process_track(db.claim()))
    assert calls == ["source_links", "refresh"]
    assert db.current_asset(current["id"])["sha256"] == sha256_file(path)
    assert db.owned("tracks", current["id"], "admin")["metadata_source"] == "musicbrainz"


def test_all_reuses_exact_tier_origins_and_removal_retains_music_and_history(db):
    sources = {name: db.add_source(parse_source("https://youtube.com/playlist?list=PL" + name), "admin", True)
               for name in ("A", "B", "All")}
    a = Entry("abcdefghijk", YT, "Artist - A", "2020-01-01T00:00:00Z", "a", 0)
    b = Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Artist - B", "2021-01-01T00:00:00Z", "b", 1)
    db.apply_snapshot(sources["A"], Snapshot("A", [a]))
    db.apply_snapshot(sources["B"], Snapshot("B", [b]))
    previous = db.rows("SELECT * FROM tracks")
    db.execute("UPDATE jobs SET state='DONE'")
    db.execute("UPDATE tracks SET health='AVAILABLE',operation_state='IDLE',metadata_source='source'")
    later_a = Entry(a.key, a.url, a.title, "2026-01-01T00:00:00Z", "all-a", 0)
    new = Entry("12345678901", "https://youtu.be/12345678901", "New", "2026-01-01T00:00:00Z", "new", 2)
    assert db.apply_snapshot(sources["All"], Snapshot("All", [later_a, b, new]))["created"] == 1
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='track' AND state='PENDING'")["n"] == 1
    with TestClient(create_app(db, start_workers=False)) as client:
        for name in ("A", "B"):
            path = f"/api/sources/{sources[name]['id']}"
            assert client.post(path + "/monitoring", json={"user_id": "admin", "monitored": False}).status_code == 200
            assert client.delete(path, params={"user_id": "admin"}).status_code == 200
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 3
    for old in previous:
        current = db.owned("tracks", old["id"], "admin")
        assert current["discovered_at"] == old["discovered_at"]
        assert current["metadata_source"] == "source"
    assert db.one("SELECT COUNT(*) n FROM memberships")["n"] == 3
    assert db.apply_snapshot(sources["All"], Snapshot("All", [later_a, b, new]))["created"] == 0


def test_pause_resume_are_owned_and_keep_memberships(db, track):
    source = db.one("SELECT * FROM sources"); before = db.rows("SELECT * FROM memberships")
    with TestClient(create_app(db, start_workers=False)) as client:
        path = f"/api/sources/{source['id']}/monitoring"
        assert client.post(path, json={"user_id": "guest", "monitored": False}).status_code == 404
        assert client.post(path, json={"user_id": "admin", "monitored": False}).status_code == 200
        assert db.rows("SELECT * FROM memberships") == before
        assert client.post(path, json={"user_id": "admin", "monitored": True}).status_code == 200
        assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='sync' AND state='PENDING'")["n"] == 1


def test_v5_upgrade_backups_and_preserves_unknown_provenance(db, track):
    for column in ("metadata_source", "metadata_edited", "metadata_reviewed_at"):
        db.execute("ALTER TABLE tracks DROP COLUMN " + column)
    db.execute("PRAGMA user_version=5"); db.init()
    assert db.owned("tracks", track["id"], "admin")["metadata_source"] == "unknown"
    assert list((db.path.parent / "backups").glob("ingestor-schema-v5-*.sqlite"))
    db.init()
    assert not db.rows("PRAGMA foreign_key_check")
