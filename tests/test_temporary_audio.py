"""Listen-before-approval uses real Opus/beets files, without provider traffic."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
from mutagen.oggopus import OggOpus

from common import atomic_json, sha256_file
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from runtime import DeferredOperation
from store import Store

MBID = '11111111-1111-4111-8111-111111111111'


@pytest.fixture
def listening(db, track, environment, monkeypatch):
    fixture = environment[0] / 'source.opus'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=2', '-c:a', 'libopus', str(fixture)], check=True)
    audio = OggOpus(fixture)
    audio['artist'] = ['Source Artist']
    audio['title'] = ['Source Song']
    audio['album'] = ['Source Album']
    audio['date'] = ['2001']
    audio.save()
    pipeline = Pipeline(db)
    real_bridge = pipeline.bridge
    calls = []

    async def download(url, track_id, directory, report):
        calls.append(url)
        candidate = directory / f'temp_{track_id}.opus'
        shutil.copy2(fixture, candidate)
        receipt = {'source_url': url, 'downloader_name': 'fixture', 'downloader_version': '1'}
        atomic_json(directory / 'download-complete.json', receipt)
        return candidate, receipt

    async def bridge(job, current, directory, mode, **kwargs):
        if mode == 'identify':
            return {'automatic': True, 'recommendation': 'strong', 'choices': [
                {'mbid': MBID, 'title': 'Matched song', 'artist': 'Matched artist', 'tags': {}, 'similarity': 99},
            ]}
        return await real_bridge(job, current, directory, mode, **kwargs)

    monkeypatch.setattr(pipeline.downloader, 'download', download)
    monkeypatch.setattr(pipeline, 'bridge', bridge)
    return pipeline, fixture, calls


def run(db, pipeline):
    job = db.claim()
    assert job
    asyncio.run(pipeline.process_track(job))
    db.update('jobs', job['id'], state='DONE')
    return job


def import_track(db, pipeline, track):
    db.queue_track(track['id'], 'admin', {'mode': 'ingest'})
    run(db, pipeline)
    return db.owned('tracks', track['id'], 'admin')


def approve_source(db, pipeline, track):
    choices = json.loads(track['choices'])
    index = next(i for i, choice in enumerate(choices) if choice.get('kind') == 'source')
    db.queue_track(track['id'], 'admin', {'mode': 'approve', 'index': index})
    run(db, pipeline)
    return db.owned('tracks', track['id'], 'admin')


def test_new_strong_match_is_playable_before_approval(db, track, environment, listening):
    pipeline, fixture, downloads = listening
    current = import_track(db, pipeline, track)
    raw = OggOpus(current['file_path'])
    assert current['health'] == 'AVAILABLE' and current['operation_state'] == 'NEEDS_APPROVAL'
    assert current['mbid'] is None and current['release_id'] is None
    assert raw['album'] == ['Waiting for approval']
    assert raw['albumartist'] == ['Various Artists']
    assert raw['artist'] == ['Source Artist'] and raw['title'] == ['Source Song']
    assert not raw.get('date') and not any(k.startswith('musicbrainz') for k in raw)
    assert raw['comment'] == raw['description']
    assert raw.get('r128_track_gain') and raw.get('replaygain_track_peak')
    assert sha256_file(Path(current['temp_path'])) == sha256_file(fixture)
    assert db.current_asset(track['id'])['sha256'] == sha256_file(Path(current['file_path']))
    playlist = Path(db.one('SELECT playlist_path FROM sources')['playlist_path']).read_text()
    assert f"Waiting for approval/{track['id']}.opus" in playlist
    assert len(downloads) == 1
    assert not db.rows('PRAGMA foreign_key_check')
    # Opus metadata writes must not change decoded audio samples.
    def decoded(path):
        return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a', '-f', 'hash', '-hash', 'sha256', '-'])
    assert decoded(fixture) == decoded(current['file_path'])


def test_approval_replaces_temporary_copy_without_downloading(db, track, environment, listening):
    pipeline, _, downloads = listening
    waiting = import_track(db, pipeline, track)
    current = approve_source(db, pipeline, waiting)
    assert current['operation_state'] == 'IDLE' and current['health'] == 'AVAILABLE'
    assert current['temporary_candidate_sha256'] is None
    assert OggOpus(current['file_path'])['album'] == ['Source Album']
    assert not Path(waiting['file_path']).exists()
    assert not Path(waiting['temp_path']).exists()
    assert len(downloads) == 1
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE state='CURRENT'")['n'] == 1
    playlist = Path(db.one('SELECT playlist_path FROM sources')['playlist_path']).read_text()
    assert 'Waiting for approval' not in playlist and '#EXTINF' in playlist
    asyncio.run(pipeline.process_integrity({'id': 999, 'user_id': 'admin', 'target': 'admin'}))
    assert db.integrity_report('admin')['total'] == 0


def test_metadata_deferral_keeps_temporary_audio_available(db, track, listening, monkeypatch):
    pipeline, _, downloads = listening
    original = pipeline._identification_choices

    async def unavailable(*args):
        raise DeferredOperation('Metadata provider unavailable', retry_at=1)

    monkeypatch.setattr(pipeline, '_identification_choices', unavailable)
    db.queue_track(track['id'], 'admin', {'mode': 'ingest'})
    job = db.claim()
    with pytest.raises(DeferredOperation):
        asyncio.run(pipeline.process_track(job))
    current = db.owned('tracks', track['id'], 'admin')
    assert Path(current['file_path']).is_file() and current['health'] == 'AVAILABLE'
    db.defer_job(job['id'], 'Metadata provider unavailable', retry_at=1)
    db.update('jobs', job['id'], not_before=0)
    monkeypatch.setattr(pipeline, '_identification_choices', original)
    run(db, pipeline)
    assert len(downloads) == 1
    assert db.owned('tracks', track['id'], 'admin')['operation_state'] == 'NEEDS_APPROVAL'


def test_failed_approval_retains_temporary_file(db, track, listening, monkeypatch):
    pipeline, _, _ = listening
    waiting = import_track(db, pipeline, track)
    original = pipeline.bridge
    digest = sha256_file(Path(waiting['file_path']))

    async def fail(job, current, directory, mode, **kwargs):
        if mode == 'apply':
            raise RuntimeError('injected tag failure')
        return await original(job, current, directory, mode, **kwargs)

    monkeypatch.setattr(pipeline, 'bridge', fail)
    with pytest.raises(RuntimeError, match='tag failure'):
        approve_source(db, pipeline, waiting)
    assert sha256_file(Path(waiting['file_path'])) == digest
    assert db.current_asset(track['id'])['path'] == waiting['file_path']


def test_publication_retries_after_file_write_before_db_commit(db, track, listening, monkeypatch):
    pipeline, _, downloads = listening
    commit = db.commit_temporary_audio

    def fail(*args):
        raise RuntimeError('injected commit failure')

    monkeypatch.setattr(db, 'commit_temporary_audio', fail)
    db.queue_track(track['id'], 'admin', {'mode': 'ingest'})
    job = db.claim()
    with pytest.raises(RuntimeError, match='commit failure'):
        asyncio.run(pipeline.process_track(job))
    assert not db.current_asset(track['id'])
    monkeypatch.setattr(db, 'commit_temporary_audio', commit)
    asyncio.run(pipeline.process_track(job))
    assert len(downloads) == 1
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE state='CURRENT'")['n'] == 1
    assert db.owned('tracks', track['id'], 'admin')['operation_state'] == 'NEEDS_APPROVAL'


def test_upgrade_publishes_existing_approval_without_changing_review(db, track, environment, listening):
    pipeline, fixture, downloads = listening
    origin = db.origins_for_track(track['id'])[0]
    directory = environment[0] / 'staging' / track['id']
    directory.mkdir(parents=True)
    candidate = directory / f"temp_{track['id']}.opus"
    shutil.copy2(fixture, candidate)
    pending = {'action': 'ingest', 'origin_id': origin['id'], 'operation': 'legacy', 'candidate_sha256': sha256_file(candidate)}
    choices = [{'kind': 'asis', 'asis': True, 'title': 'Source Song'}]
    atomic_json(directory / 'operation.json', pending)
    atomic_json(directory / 'download-complete.json', {'source_url': origin['url']})
    db.pause_for_approval(track['id'], choices, pending, str(candidate))
    token = db.approval_token(db.owned('tracks', track['id'], 'admin'))
    db.recover()
    db.recover()
    assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE state='PENDING'")['n'] == 1
    run(db, pipeline)
    current = db.owned('tracks', track['id'], 'admin')
    assert current['operation_state'] == 'NEEDS_APPROVAL'
    assert db.approval_token(current) == token
    assert Path(current['file_path']).is_file() and not downloads
    assert sha256_file(candidate) == pending['candidate_sha256']
    db.queue_track(track['id'], 'admin', {'mode': 'approve', 'index': 0})
    run(db, pipeline)
    approved = db.owned('tracks', track['id'], 'admin')
    assert OggOpus(approved['file_path'])['album'] == ['Source Album']


def test_merge_after_approval_cleans_only_the_incoming_temporary_copy(db, track, environment, listening):
    pipeline, _, downloads = listening
    canonical = approve_source(db, pipeline, import_track(db, pipeline, track))
    db.update('tracks', track['id'], mbid=MBID)
    source = db.add_source(parse_source('https://youtube.com/playlist?list=PLanother'), 'admin')
    db.apply_snapshot(source, Snapshot('Second playlist', [Entry('lmnopqrstuv', 'https://youtu.be/lmnopqrstuv', 'Other upload', None, 'second', 0)]))
    # Ignore the source sync job; use only the already-resolved track snapshot.
    db.execute("UPDATE jobs SET state='DONE' WHERE kind='sync'")
    run(db, pipeline)
    incoming = db.one('SELECT * FROM tracks WHERE id<>?', (track['id'],))
    temporary = incoming['file_path']
    db.queue_track(incoming['id'], 'admin', {'mode': 'approve', 'index': 0})
    run(db, pipeline)
    assert not Path(temporary).exists()
    assert Path(canonical['file_path']).is_file()
    assert db.one('SELECT COUNT(*) AS n FROM tracks')['n'] == 1
    assert len(db.origins_for_track(track['id'])) == 2
    assert not db.rows('PRAGMA foreign_key_check')
    assert len(downloads) == 2
    for source in db.rows('SELECT * FROM sources'):
        assert 'Waiting for approval' not in Path(source['playlist_path']).read_text()


def test_deleting_temporary_audio_cleans_files_and_playlists(db, track, listening):
    pipeline, _, _ = listening
    waiting = import_track(db, pipeline, track)
    db.queue_track(track['id'], 'admin', {'mode': 'delete_ignore_urls'})
    run(db, pipeline)
    assert not Path(waiting['file_path']).exists()
    assert not db.one('SELECT * FROM tracks')
    assert '#EXTINF' not in Path(db.one('SELECT playlist_path FROM sources')['playlist_path']).read_text()
    assert not db.rows('PRAGMA foreign_key_check')


def test_schema_v2_migration_backs_up_and_preserves_tracks(db, track):
    db.execute('ALTER TABLE tracks DROP COLUMN temporary_candidate_sha256')
    db.execute('DROP TABLE origin_identity_rejections')
    db.execute('PRAGMA user_version=2')
    db.init()
    assert db.one('PRAGMA user_version')['user_version'] == 3
    assert db.owned('tracks', track['id'], 'admin')['temporary_candidate_sha256'] is None
    backups = list((db.path.parent / 'backups').glob('ingestor-schema-v2-*.sqlite'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA user_version').fetchone()[0] == 2
        assert backup.execute('SELECT id FROM tracks').fetchone()[0] == track['id']
    db.init()
    assert not db.rows('PRAGMA foreign_key_check')


def test_temporary_reprocess_updates_provenance_even_for_identical_audio(db, track, environment, listening):
    pipeline, _, _ = listening
    waiting = import_track(db, pipeline, track)
    source = db.add_source(parse_source('https://youtube.com/playlist?list=PLduplicate'), 'admin')
    db.apply_snapshot(source, Snapshot('Another source', [Entry('lmnopqrstuv', 'https://youtu.be/lmnopqrstuv', 'Same bytes', None, 'duplicate', 0)]))
    other = db.one('SELECT * FROM track_origins WHERE track_id<>?', (track['id'],))
    empty_id = other['track_id']
    db.update('track_origins', other['id'], track_id=track['id'])
    db.execute('DELETE FROM tracks WHERE id=?', (empty_id,))
    directory = Path(waiting['temp_path']).parent
    asyncio.run(pipeline.ensure_temporary_audio(
        {'user_id': 'admin', 'target': track['id']}, waiting, other,
        Path(waiting['temp_path']), {'source_url': other['url'], 'downloader_name': 'fixture', 'downloader_version': '2'}, directory,
    ))
    current = db.owned('tracks', track['id'], 'admin')
    assert current['current_origin_id'] == other['id']
    assert db.current_asset(track['id'])['source_url'] == other['url']
    assert db.current_asset(track['id'])['downloader_version'] == '2'
    assert db.one("SELECT COUNT(*) AS n FROM assets WHERE state='CURRENT'")['n'] == 1
