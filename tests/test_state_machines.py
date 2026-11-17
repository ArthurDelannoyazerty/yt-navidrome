"""Executable workflow contracts and durable race/cancellation regressions."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from pipeline import Pipeline, write_playlist
from providers import Entry, Snapshot, parse_source
from source_purge import archive, commit, queue
from state_machines import (
    MACHINES, JOB, SOURCE, InvalidTransition, JobPhase, SwapPhase,
    circuit_state, credential_state, job_phase, publication_phase,
    resolution_state, swap_phase, transition_setting, UPDATE,
)
from store import JobCancelled, SCHEMA_VERSION, Store


@pytest.mark.parametrize('name', list(MACHINES))
def test_every_declared_transition_is_valid_and_unknown_events_are_rejected(name):
    spec = MACHINES[name]
    assert spec.initial in spec.transitions
    for state, events in spec.transitions.items():
        for event, target in events.items():
            assert spec.send(state, event) == target
            assert spec.require(state, target) == target
        with pytest.raises(InvalidTransition):
            spec.send(state, 'not-a-real-event')
    with pytest.raises(TypeError):
        spec.transitions[spec.initial]['injected'] = spec.initial


@pytest.mark.parametrize('terminal', ['DONE', 'FAILED', 'CANCELLED'])
def test_terminal_job_cannot_restart(terminal):
    with pytest.raises(InvalidTransition):
        JOB.require(terminal, 'PENDING')


def setup_source(db, user='admin', suffix='one', media='abcdefghijk'):
    src = db.add_source(parse_source('https://youtube.com/playlist?list=PL' + suffix), user, True)
    db.apply_snapshot(src, Snapshot('Test playlist', [
        Entry(media, 'https://youtube.com/watch?v=' + media, 'Test song',
              '2024-01-01T00:00:00Z', 'entry-' + media, 0)
    ]))
    return src, db.one('SELECT * FROM tracks WHERE user_id=? ORDER BY created_at DESC', (user,))


def test_db_guard_blocks_direct_sql_and_rolls_back_audit_log(db):
    src, _ = setup_source(db)
    before = db.rows('SELECT * FROM state_transition_log')
    with pytest.raises(sqlite3.IntegrityError, match='Invalid source transition'):
        with db.db() as con:
            con.execute("UPDATE sources SET status='REMOVED' WHERE id=?", (src['id'],))
            con.execute("UPDATE sources SET status='OK' WHERE id=?", (src['id'],))
    assert db.owned('sources', src['id'], 'admin')['status'] == 'OK'
    assert db.rows('SELECT * FROM state_transition_log') == before
    with pytest.raises(sqlite3.IntegrityError, match='Unknown job stage'):
        db.execute("UPDATE jobs SET stage='imaginary'")
    with pytest.raises(sqlite3.IntegrityError, match='Invalid permission'):
        db.execute('UPDATE sources SET monitored=42')


def test_schema9_upgrade_preserves_state_and_installs_guards(db):
    src, track = setup_source(db)
    db.execute('PRAGMA user_version=9')
    db.init()
    assert db.one('PRAGMA user_version')['user_version'] == SCHEMA_VERSION
    assert db.owned('tracks', track['id'], 'admin')['title'] == track['title']
    assert list((db.path.parent / 'backups').glob('ingestor-schema-v9-*.sqlite'))
    assert db.one("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='fsm_sources_status_update'")


@pytest.mark.parametrize('action', ['archive', 'purge'])
def test_remove_cancels_exclusive_queued_and_delayed_work_not_shared_or_other_users(db, action):
    src, exclusive = setup_source(db)
    # A second origin is still on source one, but is also used by source two.
    shared_entry = Entry('lmnopqrstuv', 'https://youtube.com/watch?v=lmnopqrstuv',
                         'Shared', '2024-01-01T00:00:00Z', 'shared', 1)
    second = db.add_source(parse_source('https://youtube.com/playlist?list=PLtwo'), 'admin')
    db.apply_snapshot(src, Snapshot('Both', [
        Entry('abcdefghijk', 'https://youtube.com/watch?v=abcdefghijk', 'Exclusive',
              '2024-01-01T00:00:00Z', 'exclusive', 0), shared_entry]))
    db.apply_snapshot(second, Snapshot('Shared', [shared_entry]))
    guest, other_track = setup_source(db, 'guest', 'guest')
    shared = db.one("SELECT t.* FROM tracks t JOIN track_origins o ON o.track_id=t.id WHERE o.media_key='lmnopqrstuv'")
    db.execute("UPDATE jobs SET not_before=? WHERE kind='track' AND target=?", (time.time()+9999, exclusive['id']))
    other_jobs = db.rows('SELECT * FROM jobs WHERE user_id=? OR target IN (?,?)',
                        ('guest', second['id'], shared['id']))
    result = archive(db, src['id'], 'admin') if action == 'archive' else queue(db, src['id'], 'admin', src['url'])
    assert result['cancelled_jobs'] == 2  # source sync + exclusive delayed import
    assert result['shared_tracks'] == 1
    assert db.one("SELECT state FROM jobs WHERE kind='track' AND target=?", (exclusive['id'],))['state'] == 'CANCELLED'
    assert db.owned('tracks', exclusive['id'], 'admin')['operation_state'] == 'STOPPED'
    assert db.rows('SELECT * FROM jobs WHERE user_id=? OR target IN (?,?)',
                   ('guest', second['id'], shared['id'])) == other_jobs
    db.recover()
    assert db.one("SELECT state FROM jobs WHERE kind='track' AND target=?", (exclusive['id'],))['state'] == 'CANCELLED'
    assert db.owned('tracks', other_track['id'], 'guest')


def running_import(db):
    src, track = setup_source(db)
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    job = db.claim()
    assert job['state'] == 'RUNNING'
    db.job_checkpoint(job['id'], 'downloading')
    return src, track, job


def test_running_download_cancels_on_owner_event_loop_even_from_api_thread(db):
    src, track, job = running_import(db)
    async def run():
        pipe = Pipeline(db)
        task = asyncio.create_task(asyncio.sleep(60))
        pipe.job_tasks[job['id']] = task
        result = queue(db, src['id'], 'admin', src['url'])
        assert result['cancelling_jobs'] == 1
        assert await asyncio.to_thread(pipe.cancel_job, job['id']) is True
        with pytest.raises(asyncio.CancelledError):
            await task
        db.finish_cancelled_job(job['id'])
        assert db.one('SELECT state FROM jobs WHERE id=?', (job['id'],))['state'] == 'CANCELLED'
    asyncio.run(run())


@pytest.mark.parametrize('stage', ['applying', 'publishing'])
def test_publication_is_drained_then_cancelled_at_next_safe_boundary(db, stage):
    src, track, job = running_import(db)
    db.job_checkpoint(job['id'], stage)
    result = queue(db, src['id'], 'admin', src['url'])
    assert result['finishing_commits'] == 1 and not result['interrupt_job_ids']
    current = db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    assert current['cancel_requested'] == 0
    assert job_phase(current, time.time()) == JobPhase.DRAINING
    db.job_checkpoint(job['id'], stage)  # commit proceeds safely
    with pytest.raises(JobCancelled, match='safe boundary'):
        db.job_checkpoint(job['id'], 'identifying')
    db.finish_cancelled_job(job['id'])
    assert db.one('SELECT state FROM jobs WHERE id=?', (job['id'],))['state'] == 'CANCELLED'


def test_archived_source_rejects_late_snapshot_and_stale_playlist_writer(db, environment):
    src, track = setup_source(db)
    write_playlist(db, src)
    src = db.owned('sources', src['id'], 'admin')
    playlist = Path(src['playlist_path'])
    assert playlist.exists()
    archive(db, src['id'], 'admin')
    assert not playlist.exists()
    with pytest.raises(JobCancelled):
        db.apply_snapshot(src, Snapshot('Late network response', []))
    write_playlist(db, src)  # stale snapshot must not recreate the file
    assert not playlist.exists()
    assert db.owned('sources', src['id'], 'admin')['status'] == 'REMOVED'
    assert db.owned('tracks', track['id'], 'admin')


def test_purge_failure_has_explicit_retry_and_blocks_new_exclusive_work(db):
    src, track = setup_source(db)
    queue(db, src['id'], 'admin', src['url'])
    job = db.claim()
    db.fail_job(job, 'purge_source', 'permission denied')
    assert db.owned('sources', src['id'], 'admin')['status'] == 'PURGE_FAILED'
    with pytest.raises(ValueError, match='cleanup'):
        db.enqueue('track', track['id'], 'admin')
    result = queue(db, src['id'], 'admin', src['url'])
    assert result['job_id'] != job['id']
    assert db.owned('sources', src['id'], 'admin')['status'] == 'PURGING'


def test_idle_shared_track_keeps_independent_origin_after_purge(db):
    src, track = setup_source(db)
    db.execute("UPDATE jobs SET state='DONE'")
    db.update('tracks', track['id'], operation_state='IDLE')
    origin = db.origins_for_track(track['id'])[0]
    with db.db() as con:
        con.execute('''INSERT INTO track_origins(id,track_id,user_id,provider,media_key,url,title,first_seen_at,last_seen_at)
                       VALUES (?,?,?,?,?,?,?,?,?)''',
                    ('independent', track['id'], 'admin', 'youtube', 'lmnopqrstuv',
                     'https://youtube.com/watch?v=lmnopqrstuv', 'Independent', origin['first_seen_at'], origin['last_seen_at']))
    commit(db, src['id'], 'admin', src['url'], 'purge:' + src['id'])
    assert db.one("SELECT id FROM track_origins WHERE id='independent'")
    assert db.owned('tracks', track['id'], 'admin')


def test_clock_and_receipt_projections_are_deterministic():
    now = 1000
    assert circuit_state({'until': 1001}, now) == 'OPEN'
    assert circuit_state({'until': 1000}, now) == 'HALF_OPEN'
    assert circuit_state({}, now) == 'CLOSED'
    assert credential_state({'refresh_token': 'private', 'expires_at': 1200}, now) == 'CONNECTED'
    assert credential_state({'refresh_token': 'private', 'expires_at': 1060}, now) == 'EXPIRED'
    assert credential_state(None, now) == 'DISCONNECTED'
    assert swap_phase('new', 'old', 'new') == SwapPhase.PUBLISHED
    assert swap_phase('other', 'old', 'new') == SwapPhase.CONFLICT
    assert publication_phase({'finalized_at': None}) == 'COMMITTED'
    assert publication_phase({'finalized_at': 'now'}) == 'FINALIZED'
    assert resolution_state({'resolved_audio': {'url': 'example'}}) == 'RESOLVED'


def test_runtime_settings_reject_invalid_phase_and_can_recover(db):
    key = 'downloader_update'
    assert transition_setting(db, key, UPDATE, 'start')['phase'] == 'INSTALLING'
    with pytest.raises(InvalidTransition):
        transition_setting(db, key, UPDATE, 'activated')
    transition_setting(db, key, UPDATE, 'recover')
    assert db.setting(key)['phase'] == 'INTERRUPTED'
    assert transition_setting(db, key, UPDATE, 'start')['phase'] == 'INSTALLING'


def test_runtime_contracts_are_inspectable_via_api(db):
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.get('/api/state-machines')
    assert response.status_code == 200
    body = response.json()
    assert body['schema'] == SCHEMA_VERSION
    assert {item['name'] for item in body['machines']} == set(MACHINES)


def test_pending_commit_receipt_drains_before_purge_and_survives_restart(db):
    src, track, job = running_import(db)
    db.job_checkpoint(job['id'], 'applying')
    operation = json.loads(job['payload'])['operation']
    with db.db() as con:
        db._record_receipt_locked(con, operation, 'admin', track['id'], track['id'], 'retag', {})
    db.execute("UPDATE jobs SET state='PENDING' WHERE id=?", (job['id'],))
    queue(db, src['id'], 'admin', src['url'])
    db.recover()
    draining = db.claim()
    assert draining['id'] == job['id']
    # Recovery may start from a queued stage; receipt cleanup must still finish.
    db.job_checkpoint(job['id'], 'queued')
    db.finalize_receipt(operation)
    with pytest.raises(JobCancelled):
        db.job_checkpoint(job['id'], 'identifying')
    db.finish_cancelled_job(job['id'])
    assert db.claim()['kind'] == 'purge_source'


def test_purge_waits_for_deferred_committed_cleanup_instead_of_deleting_its_job(db):
    from runtime import DeferredOperation
    src, track, job = running_import(db)
    db.job_checkpoint(job['id'], 'publishing')
    db.execute("UPDATE jobs SET state='PENDING',not_before=? WHERE id=?", (time.time()+100, job['id']))
    queue(db, src['id'], 'admin', src['url'])
    with pytest.raises(DeferredOperation, match='committed track cleanup'):
        commit(db, src['id'], 'admin', src['url'], 'purge:' + src['id'])
    assert db.owned('tracks', track['id'], 'admin')
    assert db.one('SELECT state FROM jobs WHERE id=?', (job['id'],))['state'] == 'PENDING'


def test_server_lifecycle_handles_start_stop_and_can_restart_same_app(db):
    app = create_app(db, start_workers=False)
    assert app.state.pipeline.lifecycle == 'NEW'
    for _ in range(2):
        with TestClient(app) as client:
            assert app.state.pipeline.lifecycle == 'RUNNING'
            assert client.get('/healthz').status_code == 200
        assert app.state.pipeline.lifecycle == 'STOPPED'


def test_cache_and_schedule_deadlines_have_explicit_states():
    from state_machines import cache_state, schedule_state
    assert cache_state(None, False, 100) == 'EMPTY'
    assert cache_state(None, True, 100) == 'FETCHING'
    assert cache_state((99, {}), False, 100) == 'STALE'
    assert cache_state((101, {}), False, 100) == 'FRESH'
    assert schedule_state(None, 100) == 'UNSCHEDULED'
    assert schedule_state(100, 100) == 'DUE'
    assert schedule_state(101, 100) == 'WAITING'
    assert schedule_state(99, 100, enabled=False) == 'DISABLED'


def test_cancelled_runtime_install_leaves_old_pointer_and_explicit_terminal_state(db, monkeypatch):
    import sys
    import runtime
    from common import atomic_json
    from runtime import DownloaderRuntime
    downloader = DownloaderRuntime(db)
    old = {'python': sys.executable, 'version': 'previous'}
    atomic_json(downloader.active_file, old)
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: '/usr/bin/uv')
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(runtime, 'run_process', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(downloader.update())
    assert downloader.active() == old
    assert db.setting('downloader_update')['phase'] == 'CANCELLED'


def test_failure_after_downloader_pointer_swap_never_removes_active_directory(db, monkeypatch):
    import sys
    import runtime
    from common import atomic_json
    from runtime import DownloaderRuntime
    downloader = DownloaderRuntime(db)
    atomic_json(downloader.active_file, {'python': sys.executable, 'version': 'previous'})
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: '/usr/bin/uv')
    async def install(args, *a, **kw):
        if str(args[1]) == 'venv':
            binary = Path(args[-1]) / 'bin' / 'python'
            binary.parent.mkdir(parents=True)
            binary.touch()
        return 'next-version' if '-c' in args else ''
    original = runtime.transition_setting
    def fail_after_swap(store, key, spec, event, **data):
        if event == 'activated':
            raise RuntimeError('commit log interrupted')
        return original(store, key, spec, event, **data)
    monkeypatch.setattr(runtime, 'run_process', install)
    monkeypatch.setattr(runtime, 'transition_setting', fail_after_swap)
    with pytest.raises(RuntimeError, match='commit log interrupted'):
        asyncio.run(downloader.update())
    assert downloader.active()['version'] == 'next-version'
    assert Path(downloader.active()['python']).is_file()


def test_purge_keeps_merged_recording_and_job_claimed_by_another_source(db, environment):
    from test_origin_unmerge import merged
    track, good, bad, good_source, bad_source, audio, *_ = merged(db, environment)
    db.queue_track(track['id'], 'admin', {'mode': 'redownload', 'origin_id': good['id']})
    work = db.one("SELECT * FROM jobs WHERE target=? AND kind='track' AND state='PENDING'", (track['id'],))
    queued = queue(db, bad_source['id'], 'admin', bad_source['url'])
    assert queued['shared_tracks'] == 1
    result = commit(db, bad_source['id'], 'admin', bad_source['url'], 'purge:' + bad_source['id'])
    assert result['exclusive_tracks'] == 0
    assert audio.is_file()
    assert db.owned('tracks', track['id'], 'admin')
    assert db.owned('sources', good_source['id'], 'admin')
    assert db.one('SELECT state FROM jobs WHERE id=?', (work['id'],))['state'] == 'PENDING'


def test_closed_loop_keeps_durable_cancel_for_restart(db):
    from types import SimpleNamespace
    src, track, job = running_import(db)
    queue(db, src['id'], 'admin', src['url'])
    def closed(callback):
        raise RuntimeError('Event loop is closed')
    pipe = Pipeline(db)
    pipe.job_tasks[job['id']] = SimpleNamespace(
        done=lambda: False, get_loop=lambda: SimpleNamespace(call_soon_threadsafe=closed))
    assert pipe.cancel_job(job['id']) is False
    assert db.one('SELECT cancel_requested FROM jobs WHERE id=?', (job['id'],))['cancel_requested'] == 1
    db.recover()
    assert db.one('SELECT state FROM jobs WHERE id=?', (job['id'],))['state'] == 'CANCELLED'
