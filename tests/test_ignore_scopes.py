"""A URL-only exclusion must never poison unrelated recordings with the same MBID."""
import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import create_app
from pipeline import Pipeline
from providers import Snapshot
from test_origin_unmerge import merged


def _finish_detach(db, environment):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    result = db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    # A discovery-comment refresh can be queued on the original track.
    db.execute('UPDATE jobs SET state="DONE" WHERE target=?', (track['id'],))
    db.update('tracks', track['id'], operation_state='IDLE', operation_kind=None)
    return track, result['new_track_id'], bad, bad_source, audio, bad_entry


def test_detach_then_delete_ignore_only_url_never_blocks_musicbrainz(db, environment, monkeypatch):
    track, separate, bad, source, audio, entry = _finish_detach(db, environment)
    deleted = db.owned('tracks', separate, 'admin')
    assert deleted['mbid'] is None and deleted['current_asset_id'] is None
    with TestClient(create_app(db, start_workers=False)) as client:
        response = client.post(f'/api/tracks/{separate}/action',
                               json={'user_id': 'admin', 'mode': 'delete_ignore_urls'})
        assert response.status_code == 200, response.text
    job = db.claim()
    assert job['target'] == separate
    pipeline = Pipeline(db)

    async def bridge(job, track, directory, mode, **kwargs):
        assert mode == 'forget'
        return {}

    monkeypatch.setattr(pipeline, 'bridge', bridge)
    asyncio.run(pipeline.process_track(job))
    assert db.one('SELECT * FROM tracks WHERE id=?', (separate,)) is None
    assert db.owned('tracks', track['id'], 'admin')['mbid'] == track['mbid']
    assert audio.read_bytes() == b'original-audio'
    ignored = db.list_ignored('admin')
    assert len(ignored) == 1
    assert ignored[0]['mbid'] is None
    assert [origin['media_key'] for origin in ignored[0]['origins']] == [bad['media_key']]
    assert db.ignored_recording('admin', track['mbid']) is None
    good_origin = db.origins_for_track(track['id'])[0]
    assert db.identity_plan(track['id'], good_origin['id'],
                            {'mbid': track['mbid']}, 'reprocess')['ignored'] is None
    stats = db.apply_snapshot(source, Snapshot('Ignored album source', [entry]))
    assert stats['ignored'] == 1 and len(db.list_ignored('admin')) == 1


def test_url_only_delete_refuses_multi_origin_track(db, environment):
    track, good, bad, *_ = merged(db, environment)
    with pytest.raises(ValueError, match='requires exactly one origin'):
        db.delete_track_with_receipt(track['id'], 'admin', 'reject-multi', 'delete_ignore_urls')
    assert len(db.origins_for_track(track['id'])) == 2
    assert db.current_asset(track['id'])
    # Removing the unrelated origin leaves exactly one source and can be
    # handled with explicit audio-deleting confirmation (tested below).
    db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    assert len(db.origins_for_track(track['id'])) == 1
    assert db.current_asset(track['id'])


def test_convert_existing_global_block_preserves_ignored_urls(db, environment):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    db.create_tombstone(track['id'], 'admin', 'Incorrect full-album video')
    blocked = db.list_ignored('admin')[0]
    assert blocked['mbid'] == track['mbid']
    with TestClient(create_app(db, start_workers=False)) as client:
        forbidden = client.post(f'/api/ignored/{blocked["id"]}/allow-recording',
                                json={'user_id': 'guest'})
        assert forbidden.status_code == 404
        response = client.post(f'/api/ignored/{blocked["id"]}/allow-recording',
                               json={'user_id': 'admin'})
        assert response.status_code == 200, response.text
        assert response.json()['urls_still_ignored'] == 2
        assert response.json()['allowed_mbid'] == track['mbid']
        # Repeat is safe: user sees an explanatory error, no silent deletion.
        repeat = client.post(f'/api/ignored/{blocked["id"]}/allow-recording',
                             json={'user_id': 'admin'})
        assert repeat.status_code == 400
    restored = db.list_ignored('admin')[0]
    assert restored['mbid'] is None
    assert len(restored['origins']) == 2
    assert db.ignored_recording('admin', track['mbid']) is None
    source_stats = db.apply_snapshot(bad_source, Snapshot('Still ignored', [bad_entry]))
    assert source_stats['ignored'] == 1
    assert audio.read_bytes() == b'original-audio'


def test_global_only_block_is_removed_when_unblocked(db, environment):
    track, good, bad, *_ = merged(db, environment)
    db.execute('INSERT INTO ignored_tracks(id,user_id,mbid,title,created_at,reason) '
               "VALUES ('global-only','admin',?,'Music','now','test')", (track['mbid'],))
    response = db.allow_ignored_recording('global-only', 'admin')
    assert response == {'allowed_mbid': track['mbid'], 'urls_still_ignored': 0}
    assert db.list_ignored('admin') == []


def test_failed_approval_rechecks_saved_plan_after_user_unblocks(db, environment, tmp_path, monkeypatch):
    track, good, bad, *_ = merged(db, environment)
    db.create_tombstone(track['id'], 'admin', 'accidental global block')
    plan = db.identity_plan(track['id'], good['id'], {'mbid': track['mbid']}, 'reprocess')
    assert plan['ignored']
    blocked = db.list_ignored('admin')[0]
    db.allow_ignored_recording(blocked['id'], 'admin')
    pipeline = Pipeline(db)

    class ReachedApply(Exception):
        pass

    async def bridge(*args, **kwargs):
        raise ReachedApply()

    monkeypatch.setattr(pipeline, 'bridge', bridge)
    with pytest.raises(ReachedApply):
        asyncio.run(pipeline._apply_candidate(
            {'user_id': 'admin', 'target': track['id']}, track, good,
            {'mbid': track['mbid']}, tmp_path / 'candidate.opus', {},
            'ingest', 'retry-old-plan', tmp_path, persisted_plan=plan,
        ))


def test_new_global_ignore_rejects_previous_unblocked_plan(db, environment, tmp_path):
    track, good, bad, *_ = merged(db, environment)
    plan = db.identity_plan(track['id'], good['id'], {'mbid': track['mbid']}, 'reprocess')
    assert plan['ignored'] is None
    db.create_tombstone(track['id'], 'admin', 'global block added later')
    with pytest.raises(ValueError, match='ignored-music list'):
        asyncio.run(Pipeline(db)._apply_candidate(
            {'user_id': 'admin', 'target': track['id']}, track, good,
            {'mbid': track['mbid']}, tmp_path / 'candidate.opus', {},
            'reprocess', 'stale-allow-plan', tmp_path, persisted_plan=plan,
        ))


def test_selectively_restore_good_url_but_keep_album_upload_ignored(db, environment):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    db.create_tombstone(track['id'], 'admin', 'accidentally blocked same recording')
    blocked = db.list_ignored('admin')[0]
    with TestClient(create_app(db, start_workers=False)) as client:
        request = client.delete(
            f'/api/ignored/{blocked["id"]}/origins/{good["provider"]}/{good["media_key"]}',
            params={'user_id': 'guest'},
        )
        assert request.status_code == 404
        request = client.delete(
            f'/api/ignored/{blocked["id"]}/origins/{good["provider"]}/fake-video',
            params={'user_id': 'admin'},
        )
        assert request.status_code == 404
        request = client.post(f'/api/ignored/{blocked["id"]}/allow-recording',
                              json={'user_id': 'admin'})
        assert request.status_code == 200
        request = client.delete(
            f'/api/ignored/{blocked["id"]}/origins/{good["provider"]}/{good["media_key"]}',
            params={'user_id': 'admin'},
        )
        assert request.status_code == 200
        assert request.json()['urls_still_ignored'] == 1
        assert request.json()['mbid_still_blocked'] is False
    groups = db.list_ignored('admin')
    assert len(groups) == 1 and groups[0]['mbid'] is None
    assert [o['media_key'] for o in groups[0]['origins']] == [bad['media_key']]
    assert db.ignored_recording('admin', track['mbid']) is None
    stats = db.apply_snapshot(bad_source, Snapshot('Wrong album', [bad_entry]))
    assert stats['ignored'] == 1
    stats = db.apply_snapshot(good_source, Snapshot('Correct song', [good_entry]))
    assert stats['ignored'] == 0


def test_url_only_delete_can_clean_single_origin_even_with_its_own_audio(db, environment):
    track, good, bad, good_source, bad_source, audio, good_entry, bad_entry = merged(db, environment)
    # Detach the unrelated bad upload; the good origin now belongs to an
    # independently deletable one-origin track WITH its own audio.
    result = db.manage_origin(track['id'], bad['id'], 'admin', 'detach')
    db.delete_track_with_receipt(track['id'], 'admin', 'url-only-with-audio', 'delete_ignore_urls')
    ignored = db.list_ignored('admin')
    assert len(ignored) == 1 and ignored[0]['mbid'] is None
    assert [o['media_key'] for o in ignored[0]['origins']] == [good['media_key']]
    assert db.owned('tracks', result['new_track_id'], 'admin')
    assert db.one('SELECT * FROM tracks WHERE id=?', (track['id'],)) is None
    assert db.ignored_recording('admin', track['mbid']) is None
    # The deletion receipt keeps the audio path so the cleanup worker can
    # safely remove it; the file is not erased before the transaction commits.
    receipt = db.operation_receipt('url-only-with-audio')
    assert receipt['result']['paths'] == [str(audio)]
    assert audio.is_file()


def test_last_url_restore_cleans_empty_url_only_ignore_group(db, environment):
    track, good, bad, *_ = merged(db, environment)
    db.manage_origin(track['id'], bad['id'], 'admin', 'ignore')
    group = db.list_ignored('admin')[0]
    assert group['mbid'] is None and len(group['origins']) == 1
    result = db.restore_ignored_origin(group['id'], 'admin', bad['provider'], bad['media_key'])
    assert result == {'mbid_still_blocked': False, 'urls_still_ignored': 0}
    assert db.list_ignored('admin') == []


def test_restoring_last_url_must_not_unblock_globally_ignored_recording(db, environment):
    track, good, bad, *_ = merged(db, environment)
    db.create_tombstone(track['id'], 'admin', 'block entire recording deliberately')
    group = db.list_ignored('admin')[0]
    for origin in group['origins']:
        result = db.restore_ignored_origin(group['id'], 'admin', origin['provider'], origin['media_key'])
        assert result['mbid_still_blocked']
    assert db.ignored_recording('admin', track['mbid'])
    assert db.list_ignored('admin')[0]['origins'] == []
