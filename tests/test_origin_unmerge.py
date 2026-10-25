"""Split/ignore one incorrectly merged YouTube origin, not the whole recording."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common import sha256_file
from main import create_app
from pipeline import Pipeline, write_playlist
from providers import Entry, Snapshot, parse_source
from test_reliability import active_track


def merged(db, environment):
    """Two distinct videos were incorrectly approved as the same recording."""
    good_source = db.add_source(parse_source('https://youtube.com/playlist?list=PLgood'), 'admin')
    bad_source = db.add_source(parse_source('https://youtube.com/playlist?list=PLbad'), 'admin')
    good = Entry('MSb_31SUEXE', 'https://www.youtube.com/watch?v=MSb_31SUEXE',
                 'LORN - Perfekt Dark', '2021-01-01T00:00:00Z', 'good-entry', 0)
    bad = Entry('X3azfIj_JW8', 'https://www.youtube.com/watch?v=X3azfIj_JW8',
                'LORN - Full album upload', '2018-01-01T00:00:00Z', 'bad-entry', 0)
    db.apply_snapshot(good_source, Snapshot('Good source', [good]))
    db.apply_snapshot(bad_source, Snapshot('Bad source', [bad]))
    db.execute("UPDATE jobs SET state='DONE'")
    db.execute("UPDATE tracks SET operation_state='IDLE',operation_kind=NULL")
    good_origin = db.one("SELECT * FROM track_origins WHERE media_key=?", (good.key,))
    bad_origin = db.one("SELECT * FROM track_origins WHERE media_key=?", (bad.key,))
    original = db.owned('tracks', good_origin['track_id'], 'admin')
    old_bad_track = bad_origin['track_id']
    db.execute('UPDATE track_origins SET track_id=? WHERE id=?', (original['id'], bad_origin['id']))
    db.execute('DELETE FROM tracks WHERE id=?', (old_bad_track,))
    path = environment[1] / 'admin' / 'song.opus'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'original-audio')
    selected_mbid = '11111111-1111-4111-8111-111111111111'
    plan = db.identity_plan(original['id'], good_origin['id'], {'mbid': selected_mbid}, 'ingest')
    stat = path.stat()
    db.commit_processed_identity(plan, {
        'file_path': str(path), 'beets_id': 1,
        'matched_title': 'LORN - Perfekt Dark', 'mbid': selected_mbid, 'release_id': None,
    }, {'path': str(path), 'sha256': sha256_file(path),
        'size_bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns}, 'ingest', 'fixture-current')
    original = db.owned('tracks', original['id'], 'admin')
    # Mimic historical merged discovery from the older playlist video.
    db.update('tracks', original['id'], first_seen_at='2018-01-01T00:00:00Z',
              playlist_discovered_at='2018-01-01T00:00:00Z',
              discovered_at='2018-01-01T00:00:00Z')
    good_source = db.owned('sources', good_source['id'], 'admin')
    bad_source = db.owned('sources', bad_source['id'], 'admin')
    write_playlist(db, good_source)
    write_playlist(db, bad_source)
    return original, good_origin, bad_origin, good_source, bad_source, path, good, bad


def test_detach_keeps_audio_and_playlist_origins_separate(db, environment):
    track, good, bad, good_source, bad_source, audio, *_ = merged(db, environment)
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(f'/api/tracks/{track["id"]}/origins/{bad["id"]}/detach',
                               json={'user_id': 'admin'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['new_track_id'] and data['comment_queued']
        assert not data['playlist_refresh_failed']
    original = db.owned('tracks', track['id'], 'admin')
    detached = db.owned('tracks', data['new_track_id'], 'admin')
    assert original['mbid'] == track['mbid'] and original['file_path'] == str(audio)
    assert db.current_asset(track['id'])['origin_id'] == good['id']
    assert audio.read_bytes() == b'original-audio'
    assert detached['mbid'] is None and detached['current_asset_id'] is None
    assert detached['health'] == 'MISSING' and detached['title'] == bad['title']
    assert db.origins_for_track(track['id'])[0]['id'] == good['id']
    assert db.origins_for_track(detached['id'])[0]['id'] == bad['id']
    assert db.one('SELECT COUNT(*) AS n FROM memberships')['n'] == 2
    assert db.one('SELECT COUNT(*) AS n FROM tracks WHERE user_id=?', ('admin',))['n'] == 2
    assert original['discovered_at'] == '2021-01-01T00:00:00Z'
    assert db.one('SELECT payload FROM jobs WHERE target=? AND state="PENDING"', (track['id'],)) is not None
    assert '#EXTINF' in Path(db.owned('sources', good_source['id'], 'admin')['playlist_path']).read_text()
    assert '#EXTINF' not in Path(db.owned('sources', bad_source['id'], 'admin')['playlist_path']).read_text()


def test_remove_and_ignore_only_wrong_url_and_prevent_readd(db, environment):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(f'/api/tracks/{track["id"]}/origins/{bad["id"]}/ignore',
                               json={'user_id': 'admin'})
        assert response.status_code == 200, response.text
        assert not response.json()['playlist_refresh_failed']
    assert db.owned('tracks', track['id'], 'admin')['mbid'] == track['mbid']
    assert len(db.origins_for_track(track['id'])) == 1
    assert audio.read_bytes() == b'original-audio'
    assert db.one('SELECT COUNT(*) AS n FROM memberships')['n'] == 1
    assert db.one('SELECT COUNT(*) AS n FROM ignored_origins')['n'] == 1
    assert db.one('SELECT mbid FROM ignored_tracks')['mbid'] is None
    assert db.one('SELECT COUNT(*) AS n FROM ignored_tracks')['n'] == 1
    assert '#EXTINF' in Path(db.owned('sources', good_source['id'], 'admin')['playlist_path']).read_text()
    assert '#EXTINF' not in Path(db.owned('sources', bad_source['id'], 'admin')['playlist_path']).read_text()
    stats = db.apply_snapshot(bad_source, Snapshot('Bad source', [bad_entry]))
    assert stats['ignored'] == 1
    assert db.one('SELECT COUNT(*) AS n FROM track_origins')['n'] == 1
    assert len(db.list_ignored('admin')) == 1
    db.restore_ignored(db.list_ignored('admin')[0]['id'], 'admin')
    stats = db.apply_snapshot(bad_source, Snapshot('Bad source', [bad_entry]))
    assert stats['created'] == 1
    assert db.one('SELECT COUNT(*) AS n FROM track_origins')['n'] == 2


def test_protect_active_audio_and_only_origin(db, environment):
    track, good, bad, *rest = merged(db, environment)
    with pytest.raises(ValueError, match='current audio'):
        db.manage_origin(track['id'], good['id'], 'admin', 'ignore')
    with pytest.raises(ValueError, match='current audio'):
        db.manage_origin(track['id'], good['id'], 'admin', 'detach')
    assert len(db.origins_for_track(track['id'])) == 2
    db.manage_origin(track['id'], bad['id'], 'admin', 'ignore')
    db.execute("UPDATE jobs SET state='DONE' WHERE target=?", (track['id'],))
    db.update('tracks', track['id'], operation_state='IDLE', operation_kind=None)
    with pytest.raises(ValueError, match='only origin'):
        db.manage_origin(track['id'], good['id'], 'admin', 'ignore')
    assert db.owned('tracks', track['id'], 'admin')['mbid'] == track['mbid']


def test_reject_wrong_user_and_current_work(db, environment):
    track, good, bad, *rest = merged(db, environment)
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(f'/api/tracks/{track["id"]}/origins/{bad["id"]}/ignore',
                               json={'user_id': 'guest'})
        assert response.status_code == 404
    db.update('tracks', track['id'], operation_state='NEEDS_APPROVAL')
    with pytest.raises(ValueError, match='Finish the current'):
        db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    db.update('tracks', track['id'], operation_state='IDLE')
    db.enqueue('track', track['id'], 'admin', {'mode': 'comment'})
    with pytest.raises(ValueError, match='Finish the current'):
        db.manage_origin(track['id'], bad['id'], 'admin', 'ignore')
    assert len(db.origins_for_track(track['id'])) == 2


def test_reject_stale_origin_and_invalid_mode(db, environment):
    track, good, bad, *rest = merged(db, environment)
    with pytest.raises(LookupError):
        db.manage_origin(track['id'], 'no-such-origin', 'admin', 'detach')
    with pytest.raises(ValueError, match='Unsupported'):
        db.manage_origin(track['id'], bad['id'], 'admin', 'delete_all')
    with TestClient(create_app(db, start_workers=False)) as client:
        assert client.post(f'/api/tracks/{track["id"]}/origins/{bad["id"]}/oops',
                           json={'user_id': 'admin'}).status_code == 400
    assert len(db.origins_for_track(track['id'])) == 2


def test_ignore_is_transactional_on_tombstone_insert_failure(db, environment):
    track, good, bad, *rest = merged(db, environment)
    # Force a PK conflict on the ignored origin without violating the FK.
    ignored = 'existing-group'
    db.execute('INSERT INTO ignored_tracks(id,user_id,mbid,title,created_at,reason) '
               "VALUES (?,'admin',NULL,'already ignored','now','test')", (ignored,))
    db.execute('INSERT INTO ignored_origins(user_id,provider,media_key,url,title,ignored_track_id,created_at) '
               "VALUES ('admin',?,?,?,'duplicate',?,'now')",
               (bad['provider'], bad['media_key'], bad['url'], ignored))
    with pytest.raises(Exception):
        db.manage_origin(track['id'], bad['id'], 'admin', 'ignore')
    assert len(db.origins_for_track(track['id'])) == 2
    assert db.one('SELECT COUNT(*) AS n FROM memberships')['n'] == 2


def test_separation_queues_audio_and_does_not_suggest_rejected_identity(db, environment, monkeypatch):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    result = db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    separated = db.owned('tracks', result['new_track_id'], 'admin')
    assert separated['operation_state'] == 'QUEUED'
    assert db.one("SELECT 1 FROM jobs WHERE target=? AND state='PENDING'", (separated['id'],))
    assert db.one('SELECT mbid FROM origin_identity_rejections WHERE origin_id=?', (bad['id'],))['mbid'] == track['mbid']
    db.apply_snapshot(bad_source, Snapshot('Bad source', [bad_entry]))
    assert db.owned('track_origins', bad['id'], 'admin')['track_id'] == separated['id']
    pipeline = Pipeline(db)

    async def identify(*args, **kwargs):
        return {'automatic': True, 'recommendation': 'strong', 'choices': [
            {'mbid': track['mbid'], 'title': 'Wrong match'},
            {'mbid': '22222222-2222-4222-8222-222222222222', 'title': 'Another match'},
        ]}

    monkeypatch.setattr(pipeline, 'bridge', identify)
    identified, choices = asyncio.run(pipeline._identification_choices(
        {'user_id': 'admin', 'target': separated['id']}, separated, environment[0], audio, bad,
    ))
    assert not identified['automatic']
    assert all(choice.get('mbid') != track['mbid'] for choice in choices)
    assert db.one('SELECT COUNT(*) AS n FROM memberships')['n'] == 2
    assert db.current_asset(track['id'])['origin_id'] == good['id']
    assert not db.rows('PRAGMA foreign_key_check')
    assert db.one('PRAGMA integrity_check')['integrity_check'] == 'ok'


def test_separation_rolls_back_if_queueing_fails(db, environment, monkeypatch):
    track, good, bad, *rest = merged(db, environment)

    def fail(*args, **kwargs):
        raise RuntimeError('queue unavailable')

    monkeypatch.setattr(db, 'enqueue', fail)
    with pytest.raises(RuntimeError, match='queue unavailable'):
        db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    assert db.owned('track_origins', bad['id'], 'admin')['track_id'] == track['id']
    assert not db.rows('SELECT * FROM origin_identity_rejections')
    assert db.one('SELECT COUNT(*) AS n FROM tracks')['n'] == 1
    assert not db.rows('PRAGMA foreign_key_check')
