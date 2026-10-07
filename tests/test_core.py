import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from common import atomic_json, discovery_comment, next_nightly, safe_name, timestamp, user_name
from providers import Entry, Snapshot, parse_source


def test_timestamp_and_comment():
    assert timestamp("2021-04-17T23:36:42.123+02:00") == "2021-04-17T21:36:42Z"
    assert discovery_comment("2021-04-17T21:36:42Z") == "Discovery Date: 2021-04-17--21-36-42 UTC"
    assert discovery_comment(None) == "Discovery Date: unknown"
    assert discovery_comment("2020-12-31T23:59:59Z") < discovery_comment("2021-01-01T00:00:00Z")
    assert "youtube" not in discovery_comment("2021-04-17T21:36:42Z").lower()


@pytest.mark.parametrize("raw", ["2021-04-17", "2021-04-17T12:30:00", "bad", ""])
def test_no_invented_time(raw):
    with pytest.raises(ValueError):
        timestamp(raw)


@pytest.mark.parametrize("name", ["../guest", "..", "/tmp", "admin/guest", "a\\b", "", "a" * 65])
def test_user_paths(name):
    with pytest.raises(ValueError):
        user_name(name)


def test_safe_filename_and_atomic_json(tmp_path):
    assert "/" not in safe_name("../../<example>:mix")
    assert safe_name("...") == "Playlist"
    target = tmp_path / "nested" / "config.json"
    atomic_json(target, {"version": 1})
    atomic_json(target, {"version": 2})
    assert json.loads(target.read_text()) == {"version": 2}
    assert not list(target.parent.glob(".write-*"))


@pytest.mark.parametrize("now,expected", [
    ("2026-10-07T00:00:00+00:00", "2026-10-07T02:00:00+00:00"),
    ("2026-10-07T03:00:00+00:00", "2026-10-08T02:00:00+00:00"),
    ("2026-10-24T03:00:00+00:00", "2026-10-25T03:00:00+00:00"),
    ("2026-03-28T04:00:00+00:00", "2026-03-29T02:00:00+00:00"),
])
def test_nightly_timezone_and_dst(now, expected):
    assert next_nightly(datetime.fromisoformat(now), "04:00", "Europe/Paris").isoformat() == expected


def test_membership_times_users_and_duplicate_entries(db, track):
    same = Entry("abcdefghijk", track["url"], track["title"], "2019-10-01T10:11:12Z", "entry-a", 0)
    later = Entry("abcdefghijk", track["url"], track["title"], "2022-10-01T10:11:12Z", "entry-b", 1)
    other = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(other, Snapshot("Same song twice", [same, later]))
    assert len(db.rows("SELECT * FROM tracks WHERE user_id='admin'")) == 1
    assert len(db.rows("SELECT * FROM memberships")) == 3
    assert db.owned("tracks", track["id"], "admin")["discovered_at"] == same.added_at
    guest = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "guest")
    db.apply_snapshot(guest, Snapshot("Other user", [later]))
    assert len(db.rows("SELECT * FROM tracks")) == 2
    with pytest.raises(LookupError):
        db.owned("tracks", track["id"], "guest")


def test_snapshot_is_atomic_on_invalid_duplicate_entry(db, track):
    source = db.one("SELECT * FROM sources")
    before = db.rows("SELECT * FROM memberships")
    entry = Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "New", "2020-01-01T01:02:03Z", "dup", 0)
    with pytest.raises(sqlite3.IntegrityError):
        db.apply_snapshot(source, Snapshot("Broken", [entry, entry]))
    assert db.rows("SELECT * FROM memberships") == before
    assert len(db.rows("SELECT * FROM tracks")) == 1
    assert db.one("SELECT title FROM sources")["title"] == "A playlist"


def test_persistent_approval_selects_server_side_tags(db, track):
    choices = [{"mbid": "server-id", "tags": {"title": "Server candidate"}, "similarity": 96.3}]
    db.update("tracks", track["id"], status="NEEDS_APPROVAL", choices=json.dumps(choices), temp_path="kept.opus")
    with pytest.raises(ValueError):
        db.queue_track(track["id"], "admin", {"mode": "approve", "index": 9})
    db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
    selected = json.loads(db.owned("tracks", track["id"], "admin")["selected"])
    assert selected == choices[0]
    with pytest.raises(ValueError):
        db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
    job = db.claim()
    assert job and db.claim() is None
    db.recover()
    assert db.claim()["id"] == job["id"]
    assert db.one("SELECT temp_path FROM tracks")["temp_path"] == "kept.opus"


def test_retry_preserves_failed_operation(db, track):
    db.update("tracks", track["id"], status="COMPLETED", file_path="old.opus")
    db.queue_track(track["id"], "admin", {"mode": "retag", "overrides": {"title": "Edited"}})
    old = db.claim()
    db.update("jobs", old["id"], state="FAILED")
    db.update("tracks", track["id"], status="FAILED")
    db.queue_track(track["id"], "admin", {"mode": "retry"})
    new = db.claim()
    assert json.loads(new["payload"]) == json.loads(old["payload"])


def test_earlier_discovery_queues_comment_only(db, track, tmp_path):
    db.update("tracks", track["id"], file_path=str(tmp_path / "file.opus"), status="COMPLETED")
    source = db.one("SELECT * FROM sources")
    db.apply_snapshot(source, Snapshot("A playlist", [Entry("abcdefghijk", track["url"], track["title"], "2018-01-01T01:02:03Z", "e", 0)]))
    jobs = db.rows("SELECT * FROM jobs WHERE state='PENDING'")
    assert len(jobs) == 1
    assert json.loads(jobs[0]["payload"])["mode"] == "comment"


def test_shared_rate_clock(db):
    from store import Store
    second = Store(db.path)
    assert db.reserve_api("musicbrainz.org", 1.1) < .1
    assert second.reserve_api("musicbrainz.org", 1.1) >= 1.0


def test_secret_redaction(db, monkeypatch):
    monkeypatch.setenv("YT_API_KEY", "secret-value")
    db.event("ERROR", "key=secret-value URL?token=foo&sig=bar")
    message = db.one("SELECT message FROM events")["message"]
    assert "secret-value" not in message and "foo" not in message and "bar" not in message


def test_atomic_file_permissions_distinguish_state_and_public_playlists(tmp_path):
    from common import atomic_text, atomic_json
    import stat
    state, playlist = tmp_path / "state.json", tmp_path / "playlist.m3u"
    atomic_json(state, {"key": "private"})
    atomic_text(playlist, "#EXTM3U\n", mode=0o644)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(playlist.stat().st_mode) == 0o644
