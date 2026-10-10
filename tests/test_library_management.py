"""User-scoped bulk operations, audit pagination and real file-tag inspection."""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from audio_tags import loudness_values, write_loudness_tags
from beets_bridge import Bridge
from common import sha256_file
from main import create_app
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from test_reliability import active_track


def many_tracks(db, user="admin", count=230):
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLbulk"), user)
    db.apply_snapshot(source, Snapshot("Bulk playlist", [
        Entry(f"media-{i}", f"https://youtube.com/watch?v={i:011d}", f"Track {i:05d}",
              "2021-01-01T00:00:00Z", f"entry-{i}", i)
        for i in range(count)
    ]))
    db.execute("UPDATE jobs SET state='DONE' WHERE user_id=?", (user,))
    db.execute("UPDATE tracks SET operation_state='IDLE' WHERE user_id=?", (user,))
    return db.rows("SELECT * FROM tracks WHERE user_id=? ORDER BY title", (user,))


def test_retry_all_failed_crosses_pages_without_touching_other_states_or_users(db):
    tracks = many_tracks(db, count=230)
    guest = many_tracks(db, "guest", 1)[0]
    for current in tracks[:225] + [guest]:
        db.fail_operation(current["id"], "ingest", "source error")
    for current, state in zip(tracks[225:229], ["QUEUED", "RUNNING", "DEFERRED", "NEEDS_APPROVAL"]):
        db.update("tracks", current["id"], operation_state=state)
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post("/api/batch", json={"user_id": "admin", "mode": "retry"})
        assert response.status_code == 200
        assert response.json()["queued"] == 225
        assert response.json()["skipped"] == 0
        assert response.json()["scope"] == "library_user"
        assert client.post("/api/batch", json={"user_id": "admin", "mode": "retry"}).json()["queued"] == 0
    assert db.owned("tracks", guest["id"], "guest")["operation_state"] == "FAILED"
    assert [db.owned("tracks", row["id"], "admin")["operation_state"] for row in tracks[225:]] == [
        "QUEUED", "RUNNING", "DEFERRED", "NEEDS_APPROVAL", "IDLE",
    ]
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE user_id='admin' AND state='PENDING'")["n"] == 225


def test_retry_preserves_failed_operation_receipt_id(db, track):
    origin = db.origins_for_track(track["id"])[0]
    payload = {"mode": "redownload", "origin_id": origin["id"], "operation": "same-operation"}
    db.enqueue("track", track["id"], "admin", payload)
    db.execute("UPDATE jobs SET state='FAILED' WHERE kind='track' AND state='PENDING'")
    db.fail_operation(track["id"], "redownload", "temporary interruption")
    assert db.queue_bulk("admin", "retry")["queued"] == 1
    assert json.loads(db.one("SELECT payload FROM jobs WHERE state='PENDING'")["payload"]) == payload


def test_global_counts_remain_global_and_attention_filter_matches_counter(db):
    tracks = many_tracks(db, count=120)
    db.execute("UPDATE tracks SET health='AVAILABLE'")
    for current in tracks[:5]:
        db.fail_operation(current["id"], "redownload", "test")
    db.update("tracks", tracks[5]["id"], health="MISSING")
    with TestClient(create_app(db, start_workers=False)) as client:
        data = client.get("/api/tracks", params={"user_id": "admin", "status": "FAILED", "q": "00000"}).json()
        assert data["total"] == 1
        assert data["stats"]["total"] == 120
        assert data["stats"]["failed"] == 5
        assert data["stats"]["available"] == 119
        assert data["stats_scope"] == "library_user"
        attention = client.get("/api/tracks", params={"user_id": "admin", "status": "ATTENTION"}).json()
        assert attention["total"] == attention["stats"]["attention"] == 6


def test_page_associations_use_bounded_database_queries_for_large_library(db, monkeypatch):
    many_tracks(db, count=2500)
    original = db.rows
    queries = []

    def rows(sql, args=()):
        queries.append(sql)
        return original(sql, args)

    monkeypatch.setattr(db, "rows", rows)
    result = db.dashboard_tracks("admin", 12, 100)
    assert len(result["tracks"]) == 100
    assert result["total"] == 2500
    assert len(queries) <= 10, "Page fetch must not open multiple queries for each track"
    assert all(len(track["origins"]) == 1 for track in result["tracks"])


def test_failure_export_is_complete_scoped_and_redacted(db):
    many_tracks(db, count=73)
    many_tracks(db, "guest", 1)
    for row in db.rows("SELECT id FROM tracks"):
        db.fail_operation(row["id"], "ingest", "test-api-key request failed")
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.get("/api/failures/export", params={"user_id": "admin"})
        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="failures-admin.json"'
        body = response.json()
        assert body["count"] == len(body["failures"]) == 73
        assert "test-api-key" not in response.text
        assert "[REDACTED]" in response.text
        assert all(item["operation_state"] == "FAILED" for item in body["failures"])


def test_audit_pagination_is_filtered_but_summary_is_global(db, track):
    issues = [{"issue_key": f"issue-{i}", "track_id": track["id"], "kind": "LOUDNESS_MISSING" if i < 110 else "HASH_MISMATCH",
               "severity": "WARNING", "message": "test issue"} for i in range(121)]
    db.replace_integrity_issues("admin", issues)
    with TestClient(create_app(db, start_workers=False)) as client:
        body = client.get("/api/integrity", params={"user_id": "admin", "kind": "LOUDNESS_MISSING", "page": 3}).json()
        assert len(body["issues"]) == 10
        assert body["total"] == 110
        assert body["summary"]["total"] == 121
        assert body["page"] == 3
        assert client.get("/api/integrity", params={"user_id": "admin", "limit": 1000}).status_code == 422


def test_batch_repair_only_queues_audited_safe_idle_files(db, environment):
    tracks = many_tracks(db, count=5)
    guest = many_tracks(db, "guest", 1)[0]
    for row in tracks + [guest]:
        path = environment[1] / row["user_id"] / f'{row["id"]}.opus'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"fixture")
        db.update("tracks", row["id"], health="AVAILABLE", file_path=str(path))
    db.update("tracks", tracks[2]["id"], health="MISSING")
    db.update("tracks", tracks[3]["id"], operation_state="NEEDS_APPROVAL")
    db.replace_integrity_issues("admin", [
        {"issue_key": row["id"], "track_id": row["id"], "kind": "LOUDNESS_MISSING", "severity": "WARNING", "message": "missing peak"}
        for row in tracks[:4]
    ] + [{"issue_key": "changed", "track_id": tracks[1]["id"], "kind": "HASH_MISMATCH", "severity": "WARNING", "message": "changed"}])
    db.replace_integrity_issues("guest", [{"issue_key": "guest", "track_id": guest["id"], "kind": "LOUDNESS_MISSING", "severity": "WARNING", "message": "missing"}])
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.get("/api/integrity", params={"user_id": "admin"}).json()["repairable_tracks"] == 1
        assert client.post("/api/batch", json={"user_id": "admin", "mode": "repair"}).json()["queued"] == 1
    jobs = db.rows("SELECT * FROM jobs WHERE state='PENDING'")
    assert len(jobs) == 1 and jobs[0]["target"] == tracks[0]["id"]
    assert db.owned("tracks", guest["id"], "guest")["operation_state"] == "IDLE"


@pytest.mark.parametrize("failure", ["none", "analysis", "after_swap"])
def test_repair_is_atomic_and_resumable_without_downloading(db, track, environment, monkeypatch, failure):
    track, _, path = active_track(db, track, environment)
    old_asset = db.current_asset(track["id"])
    pipeline = Pipeline(db)
    calls = []
    should_fail = [failure]

    async def bridge(job, current, directory, mode, **kwargs):
        calls.append(mode)
        if mode == "repair":
            if should_fail[0] == "analysis":
                raise RuntimeError("analysis failed")
            assert path.read_bytes() == b"original-audio"
            Path(kwargs["path"]).write_bytes(b"same-audio-new-tags")
        elif mode == "refresh":
            if should_fail[0] == "after_swap":
                should_fail[0] = "none"
                raise RuntimeError("beets synchronization failed")
            return {"beets_id": 1}
        else:
            raise AssertionError(mode)

    async def no_download(*args, **kwargs):
        raise AssertionError("Repair must not download audio")

    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", no_download)
    db.queue_track(track["id"], "admin", {"mode": "repair", "operation": "repair-op"})
    job = db.claim()
    if failure != "none":
        with pytest.raises(RuntimeError):
            asyncio.run(pipeline.process_track(job))
        if failure == "analysis":
            assert path.read_bytes() == b"original-audio"
            assert db.current_asset(track["id"])["sha256"] == old_asset["sha256"]
            return
        assert path.read_bytes() == b"same-audio-new-tags"
    asyncio.run(pipeline.process_track(job))
    assert calls.count("repair") == 1
    current = db.owned("tracks", track["id"], "admin")
    asset = db.current_asset(track["id"])
    assert current["mbid"] == track["mbid"]
    assert current["file_path"] == track["file_path"]
    assert asset["id"] == old_asset["id"]
    assert asset["sha256"] == sha256_file(path)
    assert db.operation_receipt("repair-op")["finalized_at"]
    asyncio.run(pipeline.process_track(job))
    assert calls.count("repair") == 1


def test_actual_file_tags_are_exposed_without_guessing_from_database(db, track, environment):
    track, _, path = active_track(db, track, environment)
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "libopus", str(path)], check=True, timeout=20)
    Bridge.write_discovery_tags(path, "2018-02-03T04:05:06Z")
    write_loudness_tags(path, loudness_values(-5, .25))
    with TestClient(create_app(db, start_workers=False)) as client:
        body = client.get(f'/api/tracks/{track["id"]}', params={"user_id": "admin"}).json()
        tags = body["file_tags"]
        assert tags["comment"] == tags["description"] == "Discovery Date: 2018-02-03--04-05-06 UTC"
        assert "DISCOVERY_COMMENT_MISMATCH" in tags["issues"]
        assert tags["expected_discovery"] != tags["comment"]
        assert tags["replaygain_track_peak"] == .25
        assert client.get(f'/api/tracks/{track["id"]}', params={"user_id": "guest"}).status_code == 404


def test_tag_inspection_never_reads_a_path_outside_the_users_library(db, track, environment):
    track, _, _ = active_track(db, track, environment)
    db.update("assets", db.current_asset(track["id"])["id"], path="/etc/passwd")
    with TestClient(create_app(db, start_workers=False)) as client:
        data = client.get(f'/api/tracks/{track["id"]}', params={"user_id": "admin"}).json()
    assert data["file_tags"]["issues"] == ["PATH_OUTSIDE_LIBRARY"]
    assert "root:" not in str(data["file_tags"])


def test_second_instance_cannot_migrate_before_lock(db, monkeypatch):
    called = []
    with TestClient(create_app(db, start_workers=False)):
        original = db.init
        monkeypatch.setattr(db, "init", lambda: called.append(True) or original())
        with pytest.raises(RuntimeError, match="one API"):
            with TestClient(create_app(db, start_workers=False)):
                pass
    assert called == []
