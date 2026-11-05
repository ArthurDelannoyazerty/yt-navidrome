"""Durable source stops, bounded retries and safe existing-audio review."""
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import pipeline as pipeline_module
import runtime
from common import atomic_json, sha256_file, utcnow
from main import create_app
from pipeline import Pipeline, ReplacementIdentityMismatch
from providers import Entry, PROVIDERS, Snapshot, parse_source
from runtime import DeferredOperation, ProcessError, SourceAccessError, run_process
from store import JobCancelled, SCHEMA_VERSION, Store

CURRENT_MBID = "11111111-1111-4111-8111-111111111111"
OTHER_MBID = "22222222-2222-4222-8222-222222222222"
ALTERNATIVE = "https://www.youtube.com/watch?v=lmnopqrstuv"


def activate(db, track, environment, *, mbid=CURRENT_MBID, temporary=False):
    origin = db.one("SELECT * FROM track_origins WHERE track_id=?", (track["id"],))
    path = environment[1] / "admin" / "Artist" / (track["id"] + ".opus")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"existing-playable-audio")
    stat = path.stat()
    asset = {"path": str(path), "sha256": sha256_file(path), "size_bytes": stat.st_size,
             "mtime_ns": stat.st_mtime_ns, "source_url": origin["url"],
             "downloader_name": "yt-dlp", "downloader_version": "test"}
    result = {"file_path": str(path), "beets_id": 1, "matched_title": "Artist - Song",
              "mbid": mbid, "release_id": None}
    if temporary:
        db.commit_temporary_audio(track["id"], origin["id"], result, asset, "original-candidate")
        db.update("tracks", track["id"], operation_state="IDLE")
    else:
        plan = db.identity_plan(track["id"], origin["id"], {"mbid": mbid}, "ingest")
        db.commit_processed_identity(plan, result, asset, "ingest", "setup-" + track["id"])
    return db.owned("tracks", track["id"], "admin"), origin, path


def queue_ingest(db, track, origin):
    db.queue_track(track["id"], "admin", {"mode": "ingest", "origin_id": origin["id"]})
    return db.claim()


def test_schema_v4_upgrade_backs_up_and_preserves_data(db, track, environment):
    track, origin, audio = activate(db, track, environment)
    membership = db.rows("SELECT * FROM memberships")
    with db.db() as con:
        for column in ("download_enabled", "disabled_at", "disabled_reason"):
            con.execute(f"ALTER TABLE track_origins DROP COLUMN {column}")
        for column in ("download_failures", "cancel_requested", "stage"):
            con.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        con.execute("PRAGMA user_version=4")

    upgraded = Store(db.path)
    upgraded.init()
    assert upgraded.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION
    assert upgraded.owned("track_origins", origin["id"], "admin")["download_enabled"] == 1
    assert upgraded.current_asset(track["id"])["path"] == str(audio)
    assert upgraded.rows("SELECT * FROM memberships") == membership
    backups = list((environment[0] / "backups").glob("ingestor-schema-v4-*.sqlite"))
    assert backups
    with sqlite3.connect(backups[-1]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
        assert "download_enabled" not in {row[1] for row in con.execute("PRAGMA table_info(track_origins)")}
    upgraded.init()
    assert upgraded.owned("track_origins", origin["id"], "admin")["download_enabled"] == 1


def test_disabled_deferred_source_survives_sync_and_restart(db, track, environment):
    track, origin, audio = activate(db, track, environment)
    source = db.one("SELECT * FROM sources")
    memberships = db.rows("SELECT * FROM memberships")
    db.queue_track(track["id"], "admin", {"mode": "redownload", "origin_id": origin["id"]})
    job = db.claim()
    db.defer_job(job["id"], "network", download_failure=True)
    stopped = db.disable_origin(track["id"], origin["id"], "admin", "Age restriction")
    assert stopped["cancelled"]
    db.recover()
    db.apply_snapshot(source, Snapshot("A playlist", [
        Entry(origin["media_key"], origin["url"], "Updated source title",
              memberships[0]["added_at"], memberships[0]["entry_id"], 0, channel="Artist - Topic")
    ]))
    current = db.owned("tracks", track["id"], "admin")
    disabled = db.owned("track_origins", origin["id"], "admin")
    assert disabled["download_enabled"] == 0
    assert disabled["disabled_reason"] == "Age restriction"
    assert disabled["title"] == "Updated source title"
    assert current["operation_state"] == "STOPPED"
    assert current["health"] == "AVAILABLE"
    assert current["current_asset_id"] == track["current_asset_id"]
    assert audio.read_bytes() == b"existing-playable-audio"
    assert db.rows("SELECT * FROM memberships") == memberships
    assert not db.one("SELECT 1 FROM jobs WHERE kind='track' AND state IN ('PENDING','RUNNING')")
    assert not db.list_ignored("admin")


def test_disabling_playing_url_does_not_stop_another_origin(db, track, environment):
    track, old_origin, audio = activate(db, track, environment)
    result = db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    job = db.one("SELECT * FROM jobs WHERE kind='track' AND state='PENDING'")
    response = db.disable_origin(track["id"], old_origin["id"], "admin")
    assert not response["cancelled"] and not response["cancellation_pending"]
    assert not db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],))["cancel_requested"]
    row = db.dashboard_tracks("admin", 1, 50)["tracks"][0]
    assert row["active_origin_id"] == result["origin_id"]
    assert row["job_origin_id"] == result["origin_id"]
    assert row["current_origin_id"] == old_origin["id"]
    assert row["can_stop_download"]
    assert audio.is_file()


@pytest.mark.parametrize("stage", ["publishing", "applying"])
def test_disable_during_publication_finishes_transaction_safely(db, track, stage):
    origin = db.one("SELECT * FROM track_origins")
    job = queue_ingest(db, track, origin)
    db.job_checkpoint(job["id"], stage)
    result = db.disable_origin(track["id"], origin["id"], "admin")
    assert result["publication_finishing"]
    assert not result["cancellation_pending"]
    assert not db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],))["cancel_requested"]
    assert db.owned("track_origins", origin["id"], "admin")["download_enabled"] == 0
    db.job_checkpoint(job["id"], stage)


def test_budget_counts_failed_downloads_not_provider_waits(db, track, environment, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_MAX_ATTEMPTS", "3")
    origin = db.one("SELECT * FROM track_origins")
    job = queue_ingest(db, track, origin)
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    candidate.write_bytes(b"retained-candidate")
    atomic_json(directory / "download-complete.json", {"source_url": origin["url"]})
    for _ in range(6):
        assert db.defer_job(job["id"], "Metadata or shared circuit wait") is not None
        assert db.one("SELECT download_failures FROM jobs WHERE id=?", (job["id"],))["download_failures"] == 0
        db.update("jobs", job["id"], not_before=0)
        db.claim()
    for attempt in range(1, 4):
        retry_at = db.defer_job(job["id"], "transient media error", download_failure=True)
        if attempt < 3:
            assert retry_at is not None
            db.update("jobs", job["id"], not_before=0)
            db.claim()
        else:
            assert retry_at is None
    failed = db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert failed["state"] == "FAILED"
    assert failed["download_failures"] == 3
    assert "3 failed download attempts" in failed["error"]
    assert db.defer_job(job["id"], "stale callback") is None
    assert candidate.read_bytes() == b"retained-candidate"
    assert not db.claim()


@pytest.mark.parametrize("returncode", [21, 24])
def test_unavailable_or_restricted_video_is_disabled_without_global_circuit(db, track, environment, monkeypatch, returncode):
    track, origin, audio = activate(db, track, environment)
    processor = Pipeline(db)
    monkeypatch.setattr(processor.downloader, "active", lambda: {"python": sys.executable, "version": "test"})

    async def restricted(*args, **kwargs):
        raise ProcessError(returncode, ["restricted"])

    monkeypatch.setattr(runtime, "run_process", restricted)
    db.queue_track(track["id"], "admin", {"mode": "redownload", "origin_id": origin["id"]})
    job = db.claim()
    with pytest.raises(SourceAccessError) as caught:
        asyncio.run(processor.process_track(job))
    db.fail_job(job, "redownload", str(caught.value), stopped=True)
    disabled = db.owned("track_origins", origin["id"], "admin")
    assert not disabled["download_enabled"]
    assert disabled["disabled_at"] and disabled["disabled_reason"]
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "STOPPED"
    assert not processor.downloader.circuit_status()["open"]
    assert processor.downloader.circuit_status()["failures"] == 0
    assert audio.read_bytes() == b"existing-playable-audio"
    with pytest.raises(ValueError, match="origin|disabled|video"):
        db.queue_track(track["id"], "admin", {"mode": "retry"})


@pytest.mark.parametrize("returncode", [20, 22])
def test_runtime_marks_only_actual_download_failures(db, environment, monkeypatch, returncode):
    downloader = runtime.DownloaderRuntime(db)
    monkeypatch.setattr(downloader, "active", lambda: {"python": sys.executable, "version": "test"})

    async def fail(*args, **kwargs):
        raise ProcessError(returncode, ["temporary"])

    monkeypatch.setattr(runtime, "run_process", fail)
    with pytest.raises(DeferredOperation) as caught:
        asyncio.run(downloader.download(ALTERNATIVE, "test", environment[0], lambda *args: None))
    assert caught.value.download_failure
    monkeypatch.setattr(downloader, "circuit_status", lambda: {"open": True, "until": 9999999999})
    with pytest.raises(DeferredOperation) as waiting:
        asyncio.run(downloader.download(ALTERNATIVE, "test", environment[0], lambda *args: None))
    assert not waiting.value.download_failure


def test_stop_wins_race_with_completed_download(db, track, environment, monkeypatch):
    processor = Pipeline(db)
    origin = db.one("SELECT * FROM track_origins")
    job = queue_ingest(db, track, origin)

    async def download(url, track_id, directory, report):
        candidate = directory / f"temp_{track_id}.opus"
        candidate.write_bytes(b"late-download")
        receipt = {"source_url": url}
        atomic_json(directory / "download-complete.json", receipt)
        db.disable_origin(track_id, origin["id"], "admin")
        return candidate, receipt

    async def forbidden(*args, **kwargs):
        pytest.fail("Cancelled audio must never be published or identified")

    monkeypatch.setattr(processor.downloader, "download", download)
    monkeypatch.setattr(processor, "bridge", forbidden)
    with pytest.raises(JobCancelled):
        asyncio.run(processor.process_track(job))
    db.finish_cancelled_job(job["id"])
    assert not db.current_asset(track["id"])
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "STOPPED"
    assert not db.one("SELECT 1 FROM origin_metadata")
    assert db.defer_job(job["id"], "late defer", download_failure=True) is None
    db.recover()
    assert not db.claim()


@pytest.mark.skipif(os.name != "posix", reason="Process groups are a POSIX runtime contract")
def test_running_stop_kills_downloader_group_and_worker_processes_next_job(db, track, environment, monkeypatch):
    processor = Pipeline(db)
    origin = db.one("SELECT * FROM track_origins")
    db.queue_track(track["id"], "admin", {"mode": "ingest", "origin_id": origin["id"]})
    db.enqueue("integrity", "admin", "admin", {"mode": "verify"})
    ready = environment[0] / "child-pid"
    next_processed = []
    program = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"pathlib.Path({str(ready)!r}).write_text(str(child.pid)); "
        "time.sleep(120)"
    )

    async def download(url, track_id, directory, report):
        await run_process([sys.executable, "-c", program], report, cwd=directory)
        raise AssertionError("A stopped downloader must not succeed")

    async def integrity(job):
        next_processed.append(job["id"])
        processor.stopping.set()

    monkeypatch.setattr(processor.downloader, "download", download)
    monkeypatch.setattr(processor, "process_integrity", integrity)

    async def exercise():
        worker = asyncio.create_task(processor.work())
        try:
            async with asyncio.timeout(10):
                while not ready.exists():
                    await asyncio.sleep(.02)
            result = db.disable_origin(track["id"], origin["id"], "admin")
            assert result["cancellation_pending"]
            assert processor.cancel_job(result["job_id"])
            await asyncio.wait_for(worker, 10)
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(exercise())
    assert next_processed
    assert db.one("SELECT state FROM jobs WHERE kind='track' ORDER BY id DESC")["state"] == "CANCELLED"
    assert db.one("SELECT state FROM jobs WHERE kind='integrity'")["state"] == "DONE"
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "STOPPED"
    pid = int(ready.read_text())
    status = Path(f"/proc/{pid}/stat")
    # The killed orphan may await init reaping; it must not remain a live child.
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_late_cancelled_callback_does_not_clobber_a_new_operation(db, track):
    origin = db.one("SELECT * FROM track_origins")
    job = queue_ingest(db, track, origin)
    db.disable_origin(track["id"], origin["id"], "admin")
    db.finish_cancelled_job(job["id"])
    db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    next_job = db.claim()
    db.finish_job(next_job["id"])
    db.complete_operation(track["id"])
    db.defer_job(job["id"], "old downloader callback", download_failure=True)
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "IDLE"
    assert db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))["state"] == "CANCELLED"


def test_enabling_is_explicit_and_does_not_start_a_download(db, track):
    origin = db.one("SELECT * FROM track_origins")
    job = queue_ingest(db, track, origin)
    db.defer_job(job["id"], "temporary")
    db.disable_origin(track["id"], origin["id"], "admin")
    db.enable_origin(track["id"], origin["id"], "admin")
    assert not db.claim()
    db.queue_track(track["id"], "admin", {"mode": "retry"})
    retry = db.claim()
    assert json.loads(retry["payload"])["origin_id"] == origin["id"]
    assert not retry["cancel_requested"]


def test_source_control_api_is_user_scoped_and_replacement_is_single_video(db, track):
    origin = db.one("SELECT * FROM track_origins")
    with TestClient(create_app(db, start_workers=False)) as client:
        disable_url = f"/api/tracks/{track['id']}/origins/{origin['id']}/disable"
        assert client.post(disable_url, json={"user_id": "guest"}).status_code == 404
        disabled = client.post(disable_url, json={"user_id": "admin", "reason": "restricted"})
        assert disabled.status_code == 200
        listed = client.get("/api/disabled-origins", params={"user_id": "admin"}).json()
        assert listed[0]["id"] == origin["id"] and listed[0]["disabled_reason"] == "restricted"
        assert client.get("/api/disabled-origins", params={"user_id": "guest"}).json() == []
        replacement_url = f"/api/tracks/{track['id']}/replacement"
        assert client.post(replacement_url, json={"user_id": "guest", "url": ALTERNATIVE}).status_code == 404
        for url in ("https://youtube.com/playlist?list=PLtest", "http://127.0.0.1/secret",
                    "https://open.spotify.com/track/1234567890123456789012"):
            response = client.post(replacement_url, json={"user_id": "admin", "url": url})
            assert response.status_code == 400
        assert db.one("SELECT COUNT(*) AS n FROM track_origins")["n"] == 1
        queued = client.post(replacement_url, json={"user_id": "admin", "url": ALTERNATIVE})
        assert queued.status_code == 200
        assert db.one("SELECT COUNT(*) AS n FROM tracks")["n"] == 1
        assert db.one("SELECT COUNT(*) AS n FROM track_origins")["n"] == 2
        assert client.post(replacement_url, json={"user_id": "admin", "url": ALTERNATIVE}).status_code == 400


def test_replacement_rejects_ignored_and_other_track_urls(db, track):
    origin = db.one("SELECT * FROM track_origins")
    db.create_tombstone(track["id"], "admin", "test")
    with pytest.raises(ValueError, match="ignored"):
        db.queue_replacement(track["id"], "admin", origin["url"])
    source = db.add_source(parse_source(ALTERNATIVE), "admin")
    db.apply_snapshot(source, Snapshot("Other", [
        Entry("lmnopqrstuv", ALTERNATIVE, "Another recording", utcnow(), "other", 0)
    ]))
    with pytest.raises(ValueError, match="another library track"):
        db.queue_replacement(track["id"], "admin", ALTERNATIVE)


@pytest.mark.parametrize("temporary", [False, True])
def test_replacement_review_keeps_playable_audio_and_original_memberships(db, track, environment, monkeypatch, temporary):
    track, origin, audio = activate(db, track, environment, mbid=None if temporary else CURRENT_MBID, temporary=temporary)
    memberships = db.rows("SELECT * FROM memberships")
    processor = Pipeline(db)
    db.disable_origin(track["id"], origin["id"], "admin")
    replacement = db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    monkeypatch.setattr(PROVIDERS["youtube"], "resolve", lambda source, http: Snapshot("New title", [
        Entry("lmnopqrstuv", ALTERNATIVE, "Actual replacement title", utcnow(), "alternative", 0,
              channel="Actual channel")
    ]))

    async def download(url, track_id, directory, report):
        candidate = directory / f"temp_{track_id}.opus"
        candidate.write_bytes(b"replacement-audio")
        receipt = {"source_url": url}
        atomic_json(directory / "download-complete.json", receipt)
        return candidate, receipt

    async def identify(job, seed, directory, mode, **kwargs):
        assert mode == "identify"
        assert seed["title"] == "Actual replacement title"
        assert seed["source_channel"] == "Actual channel"
        return {"automatic": True, "recommendation": "strong", "choices": [
            {"mbid": OTHER_MBID, "title": "Other recording", "artist": "Other artist",
             "tags": {}, "similarity": 99, "kind": "candidate"}
        ]}

    async def current_metadata(*args, **kwargs):
        return {"mbid": CURRENT_MBID, "title": "Current recording", "tags": {}, "kind": "current"}

    monkeypatch.setattr(processor.downloader, "download", download)
    monkeypatch.setattr(processor, "bridge", identify)
    monkeypatch.setattr(processor, "metadata_for_recording", current_metadata)
    job = db.claim()
    asyncio.run(processor.process_track(job))
    waiting = db.owned("tracks", track["id"], "admin")
    assert waiting["operation_state"] == "NEEDS_APPROVAL"
    assert waiting["current_asset_id"] == track["current_asset_id"]
    assert waiting["current_origin_id"] == origin["id"]
    assert audio.read_bytes() == b"existing-playable-audio"
    assert db.rows("SELECT * FROM memberships") == memberships
    assert not db.owned("track_origins", origin["id"], "admin")["download_enabled"]
    assert db.owned("track_origins", replacement["origin_id"], "admin")["track_id"] == track["id"]
    if not temporary:
        db.finish_job(job["id"])
        db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
        with pytest.raises(ReplacementIdentityMismatch, match="confirmed recording"):
            asyncio.run(processor.process_track(db.claim()))
        assert audio.read_bytes() == b"existing-playable-audio"
        assert db.rows("SELECT * FROM memberships") == memberships


@pytest.mark.parametrize("staged", [False, True])
def test_refresh_suggestions_reuses_audio_and_source_hints_without_download(db, track, environment, monkeypatch, staged):
    track, origin, audio = activate(db, track, environment)
    db.update("track_origins", origin["id"], channel="末廣健一郎 - Topic", title="誓いの旋律")
    db.disable_origin(track["id"], origin["id"], "admin")
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    if staged:
        (directory / f"temp_{track['id']}.opus").write_bytes(b"pristine-audio")
        atomic_json(directory / "download-complete.json", {"source_url": origin["url"]})
    processor = Pipeline(db)
    seen = []

    async def forbidden(*args, **kwargs):
        pytest.fail("Refreshing suggestions must not download")

    def no_capture(*args, **kwargs):
        pytest.fail("Processed audio must never replace the source metadata archive")

    async def identify(job, seed, workdir, mode, **kwargs):
        assert mode == "identify"
        seen.append(Path(kwargs["path"]).read_bytes())
        assert seed["source_title"] == "誓いの旋律"
        assert seed["source_channel"] == "末廣健一郎 - Topic"
        assert seed["source_tags"]["title"] == ["誓いの旋律"]
        return {"automatic": True, "recommendation": "strong", "choices": [
            {"mbid": CURRENT_MBID, "title": "Reviewed title", "artist": "Artist",
             "tags": {}, "similarity": 99, "kind": "candidate"}
        ]}

    monkeypatch.setattr(processor.downloader, "download", forbidden)
    monkeypatch.setattr(processor, "bridge", identify)
    monkeypatch.setattr(pipeline_module, "capture_source", no_capture)
    db.queue_track(track["id"], "admin", {"mode": "refresh_suggestions"})
    job = db.claim()
    asyncio.run(processor.process_track(job))
    current = db.owned("tracks", track["id"], "admin")
    assert seen == [b"pristine-audio" if staged else b"existing-playable-audio"]
    assert current["operation_state"] == "NEEDS_APPROVAL"
    assert json.loads(current["pending_operation"])["action"] == "refresh_suggestions"
    assert current["current_asset_id"] == track["current_asset_id"]
    assert audio.read_bytes() == b"existing-playable-audio"
    assert db.one("SELECT download_failures FROM jobs WHERE id=?", (job["id"],))["download_failures"] == 0
    db.finish_job(job["id"])
    # Disabling future URL access must not prevent approving its retained audio.
    db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0})
    assert json.loads(db.claim()["payload"])["action"] == "refresh_suggestions"


def test_refresh_and_replacement_preserve_unfinished_apply_recovery(db, track, environment):
    track, origin, audio = activate(db, track, environment)
    directory = environment[0] / "staging" / track["id"]
    directory.mkdir(parents=True)
    request = directory / "apply-request.json"
    request.write_text('{"operation":"unfinished"}')
    with pytest.raises(ValueError, match="Retry the previous"):
        db.queue_track(track["id"], "admin", {"mode": "refresh_suggestions"})
    with pytest.raises(ValueError, match="Retry the previous"):
        db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    assert request.read_text() == '{"operation":"unfinished"}'
    assert audio.read_bytes() == b"existing-playable-audio"


def test_alternative_identity_merge_is_rejected_without_losing_memberships(db, track, environment):
    origin = db.one("SELECT * FROM track_origins")
    other = dict(track)
    other["id"] = str(uuid.uuid4())
    with db.db() as con:
        con.execute(
            """INSERT INTO tracks(id,user_id,title,first_seen_at,discovered_at,discovery_basis,
               mbid,created_at,updated_at) VALUES (?,?,?,?,?,'first_seen',?,?,?)""",
            (other["id"], "admin", "Existing recording", utcnow(), utcnow(), CURRENT_MBID, utcnow(), utcnow()),
        )
    replacement = db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    job = db.claim()
    selected_origin = db.owned("track_origins", replacement["origin_id"], "admin")
    candidate = environment[0] / "candidate.opus"
    candidate.write_bytes(b"new")
    memberships = db.rows("SELECT * FROM memberships")
    with pytest.raises(ReplacementIdentityMismatch, match="already exists"):
        asyncio.run(Pipeline(db)._apply_candidate(
            job, track, selected_origin, {"mbid": CURRENT_MBID, "tags": {}}, candidate,
            {"source_url": ALTERNATIVE}, "replace_source", "attempt", environment[0],
        ))
    assert db.owned("track_origins", origin["id"], "admin")["track_id"] == track["id"]
    assert db.owned("track_origins", replacement["origin_id"], "admin")["track_id"] == track["id"]
    assert db.rows("SELECT * FROM memberships") == memberships


@pytest.mark.parametrize("mode", ["retag", "refresh_suggestions"])
def test_metadata_actions_reject_provided_stale_revision(db, track, environment, mode):
    from source_metadata import correction_revision

    track, origin, audio = activate(db, track, environment)
    revision = correction_revision(track)
    db.update("tracks", track["id"], matched_title="Changed since the dialog opened")
    with pytest.raises(ValueError, match="track changed"):
        db.queue_track(track["id"], "admin", {"mode": mode, "revision": revision})
    assert not db.one("SELECT 1 FROM jobs WHERE kind='track' AND state IN ('PENDING','RUNNING')")
    current = db.owned("tracks", track["id"], "admin")
    db.queue_track(track["id"], "admin", {"mode": mode, "revision": correction_revision(current)})
    assert db.claim()
    assert audio.read_bytes() == b"existing-playable-audio"


@pytest.mark.parametrize("payload", [
    {"mode": "refresh_suggestions"},
    {"mode": "resume", "action": "refresh_suggestions"},
    {"mode": "resume", "action": "restore_source"},
    {"mode": "resume", "action": "ingest", "reviewed_correction": True},
])
def test_disabling_network_source_does_not_cancel_existing_audio_review(db, track, environment, payload):
    track, origin, audio = activate(db, track, environment)
    payload = {**payload, "origin_id": origin["id"]}
    db.enqueue("track", track["id"], "admin", payload)
    job = db.claim()
    db.job_checkpoint(job["id"], "identifying")
    before = db.dashboard_tracks("admin", 1, 50)["tracks"][0]
    assert not before["can_stop_download"]
    assert not db.track_details(track["id"], "admin")["can_stop_download"]
    result = db.disable_origin(track["id"], origin["id"], "admin")
    assert not result["cancelled"] and not result["cancellation_pending"]
    assert not db.one("SELECT cancel_requested FROM jobs WHERE id=?", (job["id"],))["cancel_requested"]
    db.job_checkpoint(job["id"], "applying")
    assert audio.read_bytes() == b"existing-playable-audio"


@pytest.mark.parametrize("mode", ["retag", "refresh_suggestions"])
@pytest.mark.parametrize("action", ["replace_source", "reprocess"])
def test_current_audio_actions_cannot_steal_another_origins_candidate(db, track, environment, mode, action):
    track, origin, audio = activate(db, track, environment)
    replacement = db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    job = db.claim()
    candidate = environment[0] / "candidate.opus"
    candidate.write_bytes(b"unreviewed-other-audio")
    pending = {"action": action, "origin_id": replacement["origin_id"], "operation": "pending",
               "candidate_sha256": sha256_file(candidate)}
    db.pause_for_approval(track["id"], [{"mbid": OTHER_MBID}], pending, str(candidate))
    db.finish_job(job["id"])
    before = db.owned("tracks", track["id"], "admin")
    with pytest.raises(ValueError, match="pending alternative-audio review"):
        db.queue_track(track["id"], "admin", {"mode": mode})
    after = db.owned("tracks", track["id"], "admin")
    assert after["pending_operation"] == before["pending_operation"]
    assert after["current_origin_id"] == origin["id"]
    assert candidate.read_bytes() == b"unreviewed-other-audio"
    assert audio.read_bytes() == b"existing-playable-audio"


def test_explicit_retry_can_choose_another_enabled_associated_origin(db, track):
    old_origin = db.one("SELECT * FROM track_origins")
    replacement = db.queue_replacement(track["id"], "admin", ALTERNATIVE)
    association_job = db.claim()
    db.finish_job(association_job["id"])
    db.complete_operation(track["id"])
    original = queue_ingest(db, track, old_origin)
    original_operation = json.loads(original["payload"])["operation"]
    db.disable_origin(track["id"], old_origin["id"], "admin")
    db.finish_cancelled_job(original["id"])
    db.queue_track(track["id"], "admin", {"mode": "retry"})
    new_job = db.claim()
    payload = json.loads(new_job["payload"])
    assert payload["origin_id"] == replacement["origin_id"]
    assert payload["operation"] != original_operation
    assert not payload.get("selected")
    assert db.owned("track_origins", old_origin["id"], "admin")["download_enabled"] == 0
