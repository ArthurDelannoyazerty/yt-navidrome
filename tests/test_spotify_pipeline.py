"""URL-only Spotify ingestion reuses the durable audio pipeline and provenance."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from common import atomic_json, sha256_file
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from runtime import DeferredOperation
from spotify_library import cancel_waiting, process_resolution
from store import SCHEMA_VERSION, Store
from test_reliability import MBID, active_track


SID, PID = "S" * 22, "P" * 22
VIDEO = "12345678901"


def spotify_source(db, *, key=SID, user="admin", automatic=True, added=None):
    source = db.add_source(parse_source("spotify:playlist:" + PID), user)
    entry = Entry(key, "https://open.spotify.com/track/" + key, "Artist - Song", added,
                  "spotify:0", 0, "playlist_added" if added else "first_seen", False,
                  "Artist", resolve_audio=automatic)
    db.apply_snapshot(source, Snapshot("Spotify playlist", [entry]))
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    current = db.one("SELECT t.* FROM tracks t JOIN track_origins o ON o.track_id=t.id WHERE o.user_id=? AND o.provider='spotify' AND o.media_key=?", (user, key))
    return current, source, entry


def resolver(pipeline, monkeypatch, video=VIDEO):
    calls = []

    async def resolve(url, report):
        calls.append(url)
        return {"url": "https://www.youtube.com/watch?v=" + video,
                "title": "Artist - Song", "channel": "Artist", "duration": 180, "version": "4.6.0"}

    monkeypatch.setattr(pipeline.spotdl, "resolve_audio", resolve)
    return calls


def comments(pipeline, monkeypatch, *, fail_first=False):
    calls = []

    async def bridge(job, current, directory, mode, **kwargs):
        assert mode == "comment"
        calls.append(current["id"])
        if fail_first and len(calls) == 1:
            raise OSError("Interrupted tag update")
        Path(current["file_path"]).write_bytes(b"updated-source-links")
        return {}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    return calls


def test_public_scan_queues_one_job_and_preserves_unknown_addition_dates(db):
    track, source, entry = spotify_source(db)
    assert track["operation_state"] == "QUEUED"
    assert track["operation_kind"] == "SPOTIFY_RESOLVE"
    assert track["first_seen_at"] and track["discovered_at"] == track["first_seen_at"]
    assert track["discovery_basis"] == "first_seen"
    assert track["playlist_discovered_at"] is None
    assert db.one("SELECT added_at FROM memberships")["added_at"] is None
    original_job = db.one("SELECT * FROM jobs WHERE kind='track'")
    assert json.loads(original_job["payload"])["mode"] == "spotify_resolve"
    db.apply_snapshot(source, Snapshot("Spotify playlist", [entry, Entry(
        entry.key, entry.url, entry.title, None, "spotify:1", 1, "first_seen", False, resolve_audio=True,
    )]))
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM memberships")["n"] == 2
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='track'")["n"] == 1
    current = db.owned("tracks", track["id"], "admin")
    assert current["discovered_at"] == track["discovered_at"]
    assert current["discovery_basis"] == "first_seen"


@pytest.mark.parametrize("state", ["FAILED", "STOPPED", "DEFERRED", "NEEDS_APPROVAL"])
def test_rescan_does_not_replace_existing_operations(db, state):
    track, source, entry = spotify_source(db)
    db.execute("UPDATE jobs SET state='FAILED' WHERE kind='track'")
    db.update("tracks", track["id"], operation_state=state)
    db.apply_snapshot(source, Snapshot("Spotify playlist", [entry]))
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == state
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='track'")["n"] == 1


def test_legacy_idle_spotify_can_start_search_without_an_account(db):
    track, _, _ = spotify_source(db, automatic=False)
    assert track["operation_state"] == "IDLE"
    db.queue_track(track["id"], "admin", {"mode": "retry"})
    job = db.claim()
    assert json.loads(job["payload"])["mode"] == "spotify_resolve"
    assert db.owned("tracks", track["id"], "admin")["operation_kind"] == "SPOTIFY_RESOLVE"


def test_cancelled_search_stays_stopped_across_rescan_until_explicit_retry(db):
    track, source, entry = spotify_source(db)
    cancel_waiting(db, track["id"], "admin")
    db.apply_snapshot(source, Snapshot("Spotify playlist", [entry]))
    assert db.owned("tracks", track["id"], "admin")["operation_state"] == "STOPPED"
    assert not db.one("SELECT 1 FROM jobs WHERE kind='track' AND state IN ('PENDING','RUNNING')")
    db.queue_track(track["id"], "admin", {"mode": "retry"})
    assert json.loads(db.claim()["payload"])["mode"] == "spotify_resolve"


def test_resolution_durably_uses_one_youtube_origin_without_changing_import_provenance(db, monkeypatch):
    track, source, entry = spotify_source(db)
    pipeline = Pipeline(db)
    calls = resolver(pipeline, monkeypatch)
    job = db.claim()
    original = json.loads(job["payload"])
    updated = asyncio.run(process_resolution(pipeline, job, db.path.parent / "staging" / track["id"]))
    payload = json.loads(updated["payload"])
    assert payload["mode"] == "ingest" and payload["operation"] == original["operation"]
    assert updated["id"] == job["id"]
    origins = db.origins_for_track(track["id"])
    discovery = next(o for o in origins if o["provider"] == "spotify")
    audio = next(o for o in origins if o["provider"] == "youtube")
    assert discovery["downloadable"] == 0 and discovery["is_discovery"] == 1
    assert audio["downloadable"] == 1 and audio["is_discovery"] == 0
    assert audio["id"] == payload["origin_id"]
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_SPOTIFY")["total"] == 1
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_YOUTUBE")["total"] == 0
    link = db.track_details(track["id"], "admin")["spotify_audio_links"][0]
    assert link["resolver_version"] == "4.6.0"
    assert json.loads(link["resolved_metadata"])["duration"] == 180
    # The durable payload, rather than an old in-memory copy, wins after restart.
    db.recover()
    retried = asyncio.run(process_resolution(pipeline, {**job, "payload": json.dumps(original)}, db.path.parent / "unused"))
    assert json.loads(retried["payload"])["mode"] == "ingest"
    assert len(calls) == 1
    db.apply_snapshot(source, Snapshot("Spotify playlist", [entry]))
    assert len(db.origins_for_track(track["id"])) == 2
    # Explicit discovery of that same video adds YouTube provenance, no audio copy.
    youtube = db.add_source(parse_source(audio["url"]), "admin")
    db.apply_snapshot(youtube, Snapshot("Video", [Entry(VIDEO, audio["url"], "Video title", None, VIDEO, 0)]))
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 1
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_YOUTUBE")["total"] == 1
    assert db.track_details(track["id"], "admin")["source_providers"] == ["spotify", "youtube"]


def test_resolved_audio_runs_normal_download_and_requires_hash_bound_approval(db, environment, monkeypatch):
    track, _, _ = spotify_source(db)
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    downloads = []

    async def download(url, track_id, directory, report):
        downloads.append(url)
        candidate = directory / f"temp_{track_id}.opus"
        candidate.write_bytes(b"candidate-audio")
        receipt = {"source_url": url, "downloader_name": "yt-dlp", "downloader_version": "fixture"}
        atomic_json(directory / "download-complete.json", receipt)
        return candidate, receipt

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == "temporary":
            target = environment[1] / "admin" / "Waiting for approval" / (track["id"] + ".opus")
            target.parent.mkdir(parents=True)
            target.write_bytes(Path(kwargs["path"]).read_bytes())
            return {"file_path": str(target), "beets_id": 1}
        if mode == "identify":
            return {"automatic": True, "recommendation": "strong", "choices": [
                {"title": "Song", "artist": "Artist", "album": "Album", "mbid": MBID,
                 "kind": "candidate", "similarity": 100, "tags": {}},
            ]}
        raise AssertionError(mode)

    monkeypatch.setattr(pipeline.downloader, "download", download)
    monkeypatch.setattr(pipeline, "bridge", bridge)
    job = db.claim()
    asyncio.run(pipeline.process_track(job))
    current = db.owned("tracks", track["id"], "admin")
    assert current["operation_state"] == "NEEDS_APPROVAL"
    assert current["health"] == "AVAILABLE" and current["mbid"] is None
    pending = json.loads(current["pending_operation"])
    assert pending["action"] == "ingest"
    assert pending["candidate_sha256"] == sha256_file(Path(current["temp_path"]))
    assert downloads == ["https://www.youtube.com/watch?v=" + VIDEO]
    assert db.current_asset(track["id"])["downloader_name"] == "yt-dlp"
    db.finish_job(job["id"])
    db.queue_track(track["id"], "admin", {"mode": "approve", "index": 0, "approval_token": db.approval_token(current)})
    resumed = json.loads(db.claim()["payload"])
    assert resumed["mode"] == "resume" and resumed["candidate_sha256"] == pending["candidate_sha256"]


def test_existing_exact_video_reuses_audio_and_retries_link_finalization(db, track, environment, monkeypatch):
    target, audio_origin, audio = active_track(db, track, environment)
    imported, source, _ = spotify_source(db, added="2019-01-02T03:04:05Z")
    pipeline = Pipeline(db)
    searches = resolver(pipeline, monkeypatch, audio_origin["media_key"])
    writes = comments(pipeline, monkeypatch, fail_first=True)
    job = db.claim()
    with pytest.raises(DeferredOperation, match="finalization"):
        asyncio.run(pipeline.process_track(job))
    with pytest.raises(ValueError, match="Spotify link"):
        db.queue_track(target["id"], "admin", {"mode": "delete"})
    db.recover()
    asyncio.run(pipeline.process_track(db.claim()))
    assert len(searches) == 1 and len(writes) == 2
    assert db.one("SELECT COUNT(*) n FROM tracks")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM assets")["n"] == 1
    current = db.owned("tracks", target["id"], "admin")
    assert current["mbid"] == target["mbid"] and current["current_asset_id"] == target["current_asset_id"]
    assert current["discovered_at"] == "2019-01-02T03:04:05Z"
    assert current["operation_state"] == "IDLE"
    assert db.current_asset(target["id"])["sha256"] == sha256_file(audio)
    assert "song.opus" in Path(db.owned("sources", source["id"], "admin")["playlist_path"]).read_text()
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_SPOTIFY")["total"] == 1
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_YOUTUBE")["total"] == 1
    assert not db.one("SELECT * FROM tracks WHERE id=?", (imported["id"],))


def test_waiting_for_existing_approval_does_not_repeat_search_or_download(db, track, monkeypatch):
    origin = db.origins_for_track(track["id"])[0]
    db.update("tracks", track["id"], operation_state="NEEDS_APPROVAL")
    imported, _, _ = spotify_source(db)
    pipeline = Pipeline(db)
    calls = resolver(pipeline, monkeypatch, origin["media_key"])
    job = db.claim()
    for _ in range(2):
        with pytest.raises(DeferredOperation, match="approval"):
            asyncio.run(pipeline.process_track(job))
    assert len(calls) == 1
    assert len(db.origins_for_track(imported["id"])) == 1
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='track' AND state IN ('PENDING','RUNNING')")["n"] == 1


def test_search_observes_download_circuit_without_consuming_failure_budget(db, monkeypatch):
    track, _, _ = spotify_source(db)
    pipeline = Pipeline(db)
    calls = resolver(pipeline, monkeypatch)
    until = time.time() + 600
    monkeypatch.setattr(pipeline.downloader, "circuit_status", lambda: {"open": True, "until": until})
    job = db.claim()
    with pytest.raises(DeferredOperation) as exc:
        asyncio.run(pipeline.process_track(job))
    assert exc.value.retry_at == until and not exc.value.download_failure
    assert calls == []
    assert db.one("SELECT download_failures FROM jobs WHERE id=?", (job["id"],))["download_failures"] == 0


@pytest.mark.parametrize("restriction", ["ignored", "disabled"])
def test_resolved_url_honors_existing_source_controls(db, track, monkeypatch, restriction):
    origin = db.origins_for_track(track["id"])[0]
    db.update("tracks", track["id"], operation_state="IDLE")
    if restriction == "ignored":
        db.create_tombstone(track["id"], "admin", "Ignored URL", include_recording=False)
    else:
        db.update("track_origins", origin["id"], download_enabled=0)
    imported, _, _ = spotify_source(db)
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch, origin["media_key"])
    with pytest.raises(ValueError, match=restriction):
        asyncio.run(pipeline.process_track(db.claim()))
    assert len(db.origins_for_track(imported["id"])) == 1
    assert db.one("SELECT COUNT(*) n FROM spotify_audio_links")["n"] == 0


def test_recording_dedup_moves_spotify_memberships_with_its_selected_audio(db, track, environment, monkeypatch):
    target, _, _ = active_track(db, track, environment)
    imported, source, _ = spotify_source(db, added="2018-01-01T00:00:00Z")
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    updated = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    origin_id = json.loads(updated["payload"])["origin_id"]
    plan = db.identity_plan(imported["id"], origin_id, {"mbid": target["mbid"]}, "ingest")
    assert plan["route"] == "existing"
    assert plan["working_track"]["discovered_at"] == "2018-01-01T00:00:00Z"
    assert plan["associated_source_urls"] == ["https://open.spotify.com/track/" + SID]
    receipt = db.commit_existing_identity(plan, "ingest", "merge-spotify")
    assert receipt["source_orphan"] is True
    membership = db.one("SELECT o.track_id FROM memberships m JOIN track_origins o ON o.id=m.origin_id WHERE m.source_id=?", (source["id"],))
    assert membership["track_id"] == target["id"]
    assert len(db.origins_for_track(imported["id"])) == 0
    assert {o["provider"] for o in db.origins_for_track(target["id"])} == {"spotify", "youtube"}
    assert db.owned("tracks", target["id"], "admin")["discovered_at"] == "2018-01-01T00:00:00Z"


def test_separating_incorrect_audio_keeps_its_spotify_playlist_on_the_new_track(db, track, environment, monkeypatch):
    target, _, audio = active_track(db, track, environment)
    imported, source, _ = spotify_source(db, added="2017-01-01T00:00:00Z")
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    updated = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    origin_id = json.loads(updated["payload"])["origin_id"]
    plan = db.identity_plan(imported["id"], origin_id, {"mbid": target["mbid"]}, "ingest")
    db.commit_existing_identity(plan, "ingest", "merge-spotify")
    separated = db.manage_origin(target["id"], origin_id, "admin", "detach")
    new_id = separated["new_track_id"]
    assert source["id"] in separated["source_ids"]
    assert {o["provider"] for o in db.origins_for_track(new_id)} == {"spotify", "youtube"}
    assert db.current_asset(new_id) is None
    assert db.owned("tracks", new_id, "admin")["discovered_at"] == "2017-01-01T00:00:00Z"
    assert db.owned("tracks", target["id"], "admin")["file_path"] == str(audio)
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_SPOTIFY")["tracks"][0]["id"] == new_id
    assert db.one("SELECT o.track_id FROM memberships m JOIN track_origins o ON o.id=m.origin_id WHERE m.source_id=?", (source["id"],))["track_id"] == new_id


def test_replacing_missing_canonical_audio_applies_spotify_links_and_dates_before_commit(db, track, environment, monkeypatch):
    target, _, audio = active_track(db, track, environment)
    audio.unlink()
    imported, _, _ = spotify_source(db, added="2016-01-01T00:00:00Z")
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    job = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    origin = db.owned("track_origins", json.loads(job["payload"])["origin_id"], "admin")
    directory = environment[0] / "staging" / imported["id"]
    directory.mkdir(parents=True)
    candidate = directory / "candidate.opus"
    candidate.write_bytes(b"candidate")
    applied = []

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == "apply":
            applied.append(current)
            audio.write_bytes(b"replacement")
            return {"file_path": str(audio), "beets_id": 1, "matched_title": "Artist - Song", "mbid": MBID}
        assert mode in {"forget", "comment"}
        return {}

    monkeypatch.setattr(pipeline, "bridge", bridge)
    asyncio.run(pipeline._apply_candidate(
        job, db.owned("tracks", imported["id"], "admin"), origin,
        {"title": "Song", "mbid": MBID, "tags": {}}, candidate,
        {"downloader_name": "yt-dlp", "source_url": origin["url"]}, "ingest",
        json.loads(job["payload"])["operation"], directory,
    ))
    assert applied[0]["discovered_at"] == "2016-01-01T00:00:00Z"
    assert "https://open.spotify.com/track/" + SID in applied[0]["source_urls"]
    assert db.owned("tracks", target["id"], "admin")["discovered_at"] == applied[0]["discovered_at"]


def test_correcting_one_spotify_import_does_not_split_its_transport_from_metadata(db, monkeypatch):
    imported, _, _ = spotify_source(db)
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    job = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    origin_id = json.loads(job["payload"])["origin_id"]
    db.update("tracks", imported["id"], mbid=MBID)
    plan = db.identity_plan(imported["id"], origin_id, {"mbid": "22222222-2222-4222-8222-222222222222"}, "reprocess")
    assert plan["route"] == "in_place"
    assert plan["working_track"]["id"] == imported["id"]


@pytest.mark.parametrize("existing_audio", [False, True])
def test_accepted_replacement_rebinds_spotify_provenance_only_after_commit(db, environment, monkeypatch, existing_audio):
    imported, _, _ = spotify_source(db, added="2015-01-01T00:00:00Z")
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    old_job = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    old_origin = db.owned("track_origins", json.loads(old_job["payload"])["origin_id"], "admin")
    folder = environment[1] / "admin"
    folder.mkdir(parents=True)
    if existing_audio:
        path = folder / "original.opus"
        path.write_bytes(b"approved-audio")
        plan = db.identity_plan(imported["id"], old_origin["id"], {"mbid": MBID}, "ingest")
        db.commit_processed_identity(plan, {"file_path": str(path), "mbid": MBID, "matched_title": "Artist - Song"},
                                     {"path": str(path), "sha256": sha256_file(path)}, "ingest", "original-accepted")
    else:
        db.update("tracks", imported["id"], operation_state="FAILED")
    db.finish_job(old_job["id"])
    replacement_url = "https://www.youtube.com/watch?v=ZZZZZZZZZZZ"
    replacement = db.queue_replacement(imported["id"], "admin", replacement_url)
    plan = db.identity_plan(imported["id"], replacement["origin_id"], {"mbid": MBID}, "replace_source", replacement=True)
    # A queued or merely planned alternative has not replaced the old association.
    assert db.one("SELECT audio_origin_id FROM spotify_audio_links")["audio_origin_id"] == old_origin["id"]
    assert plan["associated_source_urls"] == ["https://open.spotify.com/track/" + SID]
    path = folder / "replacement.opus"
    path.write_bytes(b"new-accepted-audio")
    db.commit_processed_identity(plan, {"file_path": str(path), "mbid": MBID, "matched_title": "Artist - Song"},
                                 {"path": str(path), "sha256": sha256_file(path)}, "replace_source", "replacement-accepted")
    link = db.one("SELECT * FROM spotify_audio_links")
    assert link["audio_origin_id"] == replacement["origin_id"]
    evidence = json.loads(link["resolved_metadata"])
    assert evidence["selected_by"] == "user" and evidence["url"] == replacement_url
    assert evidence["previous_url"] == old_origin["url"]
    corrected = db.identity_plan(imported["id"], replacement["origin_id"],
                                 {"mbid": "22222222-2222-4222-8222-222222222222"}, "reprocess")
    assert corrected["associated_source_urls"] == ["https://open.spotify.com/track/" + SID]
    assert corrected["working_track"]["discovered_at"] == "2015-01-01T00:00:00Z"


def test_ignored_identity_retains_spotify_tombstone_before_discarding_import(db, track, environment, monkeypatch):
    target, _, _ = active_track(db, track, environment)
    db.create_tombstone(target["id"], "admin", "Blocked recording")
    imported, source, entry = spotify_source(db)
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch)
    job = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    origin_id = json.loads(job["payload"])["origin_id"]
    db.ignore_identified_origin(origin_id, MBID, "Artist - Song", "ignore-spotify")
    assert not db.one("SELECT 1 FROM tracks WHERE id=?", (imported["id"],))
    assert db.one("SELECT 1 FROM ignored_origins WHERE provider='spotify' AND media_key=?", (SID,))
    assert db.apply_snapshot(source, Snapshot("Spotify playlist", [entry]))["ignored"] == 1
    assert db.one("SELECT COUNT(*) n FROM spotify_audio_links")["n"] == 0


def test_matching_same_video_never_reuses_another_users_track(db, track, environment, monkeypatch):
    target, origin, _ = active_track(db, track, environment)
    imported, _, _ = spotify_source(db, user="guest")
    pipeline = Pipeline(db)
    resolver(pipeline, monkeypatch, origin["media_key"])
    updated = asyncio.run(process_resolution(pipeline, db.claim(), db.path.parent / "unused"))
    guest_origin = db.owned("track_origins", json.loads(updated["payload"])["origin_id"], "guest")
    assert guest_origin["track_id"] == imported["id"]
    assert db.origins_for_track(target["id"])[0]["id"] == origin["id"]
    assert db.dashboard_tracks("admin", 1, 50, "SOURCE_SPOTIFY")["total"] == 0
    assert db.dashboard_tracks("guest", 1, 50, "SOURCE_SPOTIFY")["total"] == 1


def test_schema7_migration_preserves_existing_origins_as_discovery(db, track):
    origin_id = db.origins_for_track(track["id"])[0]["id"]
    with db.db() as con:
        con.execute("DROP TABLE spotify_audio_links")
        con.execute("ALTER TABLE track_origins DROP COLUMN is_discovery")
        con.execute("PRAGMA user_version=7")
    Store(db.path).init()
    assert db.one("PRAGMA user_version")["user_version"] == SCHEMA_VERSION == 8
    assert db.owned("track_origins", origin_id, "admin")["is_discovery"] == 1
    backups = list((db.path.parent / "backups").glob("*schema-v7*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 7
        assert con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1


def test_spotdl_and_ytdlp_updates_can_run_independently(db, monkeypatch):
    async def run():
        pipeline = Pipeline(db)
        started = []
        finish = asyncio.Event()

        async def updating(name):
            started.append(name)
            await finish.wait()

        monkeypatch.setattr(pipeline.downloader, "update", lambda: updating("yt-dlp"))
        monkeypatch.setattr(pipeline.spotdl, "update", lambda: updating("spotdl"))
        pipeline.start_update()
        pipeline.start_spotdl_update()
        with pytest.raises(ValueError, match="spotDL update"):
            pipeline.start_spotdl_update()
        await asyncio.sleep(0)
        assert set(started) == {"yt-dlp", "spotdl"}
        finish.set()
        await asyncio.gather(*pipeline.children)

    asyncio.run(run())
