"""Offline artwork ownership, ranking, framing, safe updates and recovery tests."""
import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.oggopus import OggOpus
from PIL import Image, ImageDraw

from artwork import Artwork, MAX_BYTES, decode, embedded, rank, square_content, transfer_origin
from artwork_jobs import queue, revision, queue_merge_followup
from common import sha256_file
from main import create_app
from pipeline import Pipeline
from providers import Entry, Snapshot, parse_source
from test_reliability import active_track


def picture(size=(600, 600), color=(30, 80, 160)):
    image = Image.new("RGB", size, color)
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((25, 25, size[0] - 25, size[1] - 25), outline=(220, 180, 70), width=10)
    drawing.line((0, 0, *size), fill=(220, 220, 220), width=8)
    output = io.BytesIO(); image.save(output, "PNG")
    return output.getvalue()


@pytest.fixture
def audio(db, track, environment):
    current, origin, path = active_track(db, track, environment)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "libopus", str(path)], check=True)
    tags = OggOpus(path); tags["title"] = ["My title"]; tags["r128_track_gain"] = ["0"]
    tags["comment"] = tags["description"] = ["Discovery Date: unknown\n\nSources:\n" + origin["url"]]; tags.save()
    stat = path.stat(); db.update("assets", current["current_asset_id"], sha256=sha256_file(path), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    return db.owned("tracks", current["id"], "admin"), origin, path


def token(db, track):
    with db.db() as con:
        return revision(con, con.execute("SELECT * FROM tracks WHERE id=?", (track["id"],)).fetchone())


def packets(path):
    return subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-c", "copy", "-f", "hash", "-"])


def fake_pipeline(db, monkeypatch, fail=False):
    pipeline = Pipeline(db); calls = []; failures = [fail]
    async def bridge(job, track, directory, mode, **kwargs):
        calls.append(mode)
        if mode == "artwork":
            return Artwork(db).apply(track, kwargs["path"], selection=kwargs.get("selection"), framing=kwargs["framing"], refresh=kwargs["refresh"])
        assert mode == "refresh"
        if failures[0]:
            failures[0] = False
            raise RuntimeError("Injected synchronization interruption")
        return {"beets_id": 1}
    async def never(*args, **kwargs):
        raise AssertionError("An artwork operation must not download audio")
    monkeypatch.setattr(pipeline, "bridge", bridge)
    monkeypatch.setattr(pipeline.downloader, "download", never)
    monkeypatch.setattr(Artwork, "collect", lambda *a, **k: [])
    return pipeline, calls


def test_rank_prefers_good_square_not_tiny_square():
    wide, square, tiny = [decode(picture(size)) for size in ((1280, 720), (600, 600), (128, 128))]
    assert rank(square, "youtube") > rank(wide, "youtube") > rank(tiny, "youtube")
    assert rank(wide, "coverart") > rank(square, "youtube")


def test_only_uniform_dark_border_is_automatically_trimmed():
    image = Image.new("RGB", (1600, 1000)); image.paste(decode(picture((1000, 1000))), (300, 0))
    box = square_content(image)
    assert box and 298 <= box[0] <= 302 and 1298 <= box[2] <= 1302
    assert square_content(decode(picture((1280, 720)))) is None
    assert square_content(decode(picture())) is None


@pytest.mark.parametrize("url", ["http://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg", "https://127.0.0.1/image.jpg",
    "https://i.ytimg.com.evil.test/vi/abcdefghijk/hqdefault.jpg", "https://user:pw@i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
    "https://i.ytimg.com:8443/vi/abcdefghijk/hqdefault.jpg", "https://i.ytimg.com/vi/other-video/hqdefault.jpg", "file:///etc/passwd"])
def test_thumbnail_network_boundary(url):
    assert not Artwork.thumbnail_url(url, "abcdefghijk")
    assert Artwork.thumbnail_url("https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg", "abcdefghijk")


@pytest.mark.parametrize("data", [b"not an image", b"<svg></svg>", b"x" * (MAX_BYTES + 1)])
def test_upload_rejects_invalid_content(data):
    with pytest.raises(ValueError): decode(data)


def test_framing_preserves_uncropped_image(db, audio):
    track, _, _ = audio; service = Artwork(db)
    candidate = service.cache(track, picture((800, 450)), "upload", "Wide")
    for framing, size in (("original", (800, 450)), ("fit", (800, 800)), ("crop", (450, 450))):
        assert Image.open(io.BytesIO(service.render(candidate, framing))).size == size
    assert Image.open(service.path(candidate["digest"])).size == (800, 450)


def test_pin_survives_source_reset_and_auto_can_release_it(db, audio, monkeypatch):
    track, origin, path = audio; service = Artwork(db)
    pin = service.cache(track, picture(), "upload", "Manual")
    fallback = service.cache(track, picture((700, 700), (150, 30, 100)), "youtube", "Source", origin=origin)
    monkeypatch.setattr(service, "collect", lambda *a, **k: [])
    result = service.apply(track, path, selection=pin["id"])
    with db.db() as con: service.record(con, track, result)
    assert service.apply({**track, "mbid": None}, path, source_reset=True)["candidate_id"] == pin["id"]
    assert service.apply(track, path, selection="auto")["candidate_id"] == fallback["id"]


def test_own_fallback_is_not_promoted_to_official_art(db, audio, monkeypatch):
    track, origin, path = audio; service = Artwork(db)
    candidate = service.cache(track, picture(), "youtube", "Source", origin=origin)
    monkeypatch.setattr(service, "collect", lambda *a, **k: [])
    result = service.apply(track, path)
    with db.db() as con: service.record(con, track, result)
    before = embedded(path)
    assert service.apply(track, path)["candidate_id"] == candidate["id"]
    assert len(service.candidates(track)) == 1 and embedded(path) == before
    assert service.candidates(track)[0]["source_type"] == "youtube"


def test_pending_review_and_encoded_audio_survive_image_edit(db, audio, environment, monkeypatch):
    track, origin, path = audio
    candidate_audio = environment[0] / "staging" / track["id"] / "candidate.opus"
    candidate_audio.parent.mkdir(parents=True); candidate_audio.write_bytes(b"pristine review bytes")
    pending = {"origin_id": origin["id"], "action": "ingest", "operation": "review", "candidate_sha256": "pristine"}
    db.pause_for_approval(track["id"], [{"kind": "source", "title": "Song"}], pending, str(candidate_audio))
    approval = db.approval_token(db.owned("tracks", track["id"], "admin"))
    db.update("tracks", track["id"], metadata_source="source")
    before = packets(path)
    image = Artwork(db).cache(track, picture(), "upload", "Cover")
    queue(db, track["id"], "admin", token(db, track), selection=image["id"])
    pipeline, calls = fake_pipeline(db, monkeypatch)
    job = db.claim(); asyncio.run(pipeline.process_track(job))
    final = db.owned("tracks", track["id"], "admin")
    assert final["operation_state"] == "NEEDS_APPROVAL" and final["metadata_source"] == "source"
    assert db.approval_token(final) == approval and final["mbid"] == track["mbid"]
    assert candidate_audio.read_bytes() == b"pristine review bytes"
    assert packets(path) == before and OggOpus(path)["title"] == ["My title"]
    assert db.current_asset(track["id"])["sha256"] == sha256_file(path)
    asyncio.run(pipeline.process_track(job))
    assert calls == ["artwork", "refresh"]
    assert not db.rows("PRAGMA foreign_key_check")


def test_crash_after_swap_recovers_once(db, audio, monkeypatch):
    track, _, path = audio; service = Artwork(db)
    candidate = service.cache(track, picture(), "upload", "Cover")
    queue(db, track["id"], "admin", token(db, track), selection=candidate["id"])
    job = db.claim(); pipeline, calls = fake_pipeline(db, monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match="Injected"):
        asyncio.run(pipeline.process_track(job))
    assert embedded(path) and service.preference(track) is None
    asyncio.run(pipeline.process_track(job))
    assert calls == ["artwork", "refresh", "refresh"]
    assert db.operation_receipt(json.loads(job["payload"])["operation"])["finalized_at"]


def test_external_file_change_is_never_overwritten(db, audio, monkeypatch):
    track, _, path = audio
    candidate = Artwork(db).cache(track, picture(), "upload", "Cover")
    queue(db, track["id"], "admin", token(db, track), selection=candidate["id"])
    path.write_bytes(b"external change")
    pipeline, calls = fake_pipeline(db, monkeypatch)
    with pytest.raises(ValueError, match="changed externally"):
        asyncio.run(pipeline.process_track(db.claim()))
    assert path.read_bytes() == b"external change" and not calls


def test_api_is_owned_gets_are_local_upload_is_preview_only(db, audio, monkeypatch):
    track, _, path = audio
    def forbidden(*a, **k): raise AssertionError("No network on gallery GET or upload")
    monkeypatch.setattr("requests.get", forbidden)
    before = path.read_bytes()
    with TestClient(create_app(db, start_workers=False)) as client:
        base = f"/api/tracks/{track['id']}/artwork"
        ctx = client.get(base, params={"user_id": "admin"}).json()
        assert client.get(base, params={"user_id": "guest"}).status_code == 404
        response = client.post(base + "/upload", params={"user_id": "admin", "revision_token": ctx["revision"]}, content=picture())
        assert response.status_code == 200
        candidate = response.json()["candidate_id"]
        assert path.read_bytes() == before and not db.rows("SELECT * FROM jobs WHERE state='PENDING'")
        assert client.get(base + f"/{candidate}/image", params={"user_id": "guest"}).status_code == 404
        assert client.get(base + f"/{candidate}/image", params={"user_id": "admin"}).headers["content-type"] == "image/jpeg"
        assert client.post(base, json={"user_id": "admin", "revision": "0" * 64, "selection": candidate}).status_code == 400
        assert client.post(base + "/upload", params={"user_id": "admin", "revision_token": ctx["revision"]}, content=b"bad").status_code == 400
        assert client.post(base + "/upload", params={"user_id": "admin", "revision_token": ctx["revision"]}, content=b"x" * (MAX_BYTES + 1)).status_code == 413


def test_all_associated_videos_compete_for_best_thumbnail(db, audio, monkeypatch):
    track, origin, path = audio
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(source, Snapshot("Other", [Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Square", None, "e", 0)]))
    second = db.one("SELECT * FROM track_origins WHERE media_key='lmnopqrstuv'")
    db.update("track_origins", second["id"], track_id=track["id"])
    db.execute("DELETE FROM tracks WHERE id=?", (second["track_id"],))
    wide, square = [f"https://i.ytimg.com/vi/{v}/maxresdefault.jpg" for v in ("abcdefghijk", "lmnopqrstuv")]
    calls = []
    class Reply:
        status_code = 200; headers = {}
        def __init__(self, url): self.url = url
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def json(self): return {"items": [{"id": v, "snippet": {"thumbnails": {"maxres": {"url": u, "width": w, "height": h}}}} for v,u,w,h in [("abcdefghijk",wide,1280,720),("lmnopqrstuv",square,600,600)]]}
        def iter_content(self, size): yield picture((1280, 720) if self.url == wide else (600, 600))
    def get(url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["timeout"]
        calls.append(url); return Reply(url)
    monkeypatch.setattr("requests.get", get)
    result = Artwork(db).apply(track, path)
    assert db.one("SELECT origin_id FROM artwork_candidates WHERE id=?", (result["candidate_id"],))["origin_id"] == second["id"]
    assert wide in calls and square in calls and len(calls) == 3


def test_pins_are_per_track_not_album_and_move_with_origin(db, audio):
    track, origin, _ = audio; service = Artwork(db)
    source = db.add_source(parse_source("https://youtube.com/playlist?list=PLother"), "admin")
    db.apply_snapshot(source, Snapshot("Other", [Entry("lmnopqrstuv", "https://youtu.be/lmnopqrstuv", "Other", None, "e", 0)]))
    other = db.one("SELECT * FROM tracks WHERE id<>?", (track["id"],))
    first = service.cache(track, picture(), "youtube", "First", origin=origin)
    second = service.cache(other, picture(color=(130, 30, 40)), "upload", "Second")
    with db.db() as con:
        for t,c in ((track,first),(other,second)):
            service.record(con,t,{"mode":"manual","candidate_id":c["id"],"framing":"original"})
        con.execute("UPDATE track_origins SET track_id=? WHERE id=?", (other["id"], origin["id"]))
        transfer_origin(con, track["id"], other["id"], origin["id"])
    assert service.preference(other)["candidate_id"] == second["id"]
    assert service.owned(first["id"],other)
    assert not db.rows("PRAGMA foreign_key_check")


def test_v6_migration_and_one_time_merge_followup(db, audio):
    track, _, _ = audio
    db.execute("DROP TABLE artwork_preferences"); db.execute("DROP TABLE artwork_candidates")
    db.execute("PRAGMA user_version=6"); db.init()
    assert list((db.path.parent / "backups").glob("ingestor-schema-v6-*.sqlite"))
    with db.db() as con:
        db._record_receipt_locked(con,"merge-art","admin","old",track["id"],"ingest",{"target_id":track["id"],"artwork_pending":True},finalized=True)
    receipt = db.operation_receipt("merge-art")
    assert queue_merge_followup(db,receipt) and not queue_merge_followup(db,receipt)


def test_real_beets_source_reset_retains_pin_and_source_comments(db, audio, environment):
    if not importlib.util.find_spec("beets"):
        if os.getenv("REQUIRE_BEETS") == "1": pytest.fail("Pinned beets required in CI")
        pytest.skip("Pinned beets not installed locally")
    track, origin, path = audio; service = Artwork(db)
    image = service.cache(track, picture(), "upload", "Manual cover")
    with db.db() as con: service.record(con,track,{"mode":"manual","candidate_id":image["id"],"framing":"fit"})
    before = packets(path)
    script = r'''
import json, sys
from pathlib import Path
from unittest.mock import patch
from mutagen.oggopus import OggOpus
from artwork import Artwork, embedded
from beets_bridge import Bridge
from common import music_comment
from source_metadata import snapshot, source_choice
from store import Store
store=Store(); track=store.owned("tracks",sys.argv[1],"admin"); origin=store.origins_for_track(track["id"])[0]
with patch("requests.sessions.Session.send",side_effect=AssertionError("Pinned art requires no network")):
    result=Bridge("admin").finalize(track,Path(track["file_path"]),source_choice(snapshot(store,origin)),operation="art-integration")
tags=OggOpus(result["file_path"])
assert tags["comment"] == tags["description"] == [music_comment(track["discovered_at"],[origin["url"]])]
assert result["mbid"] is None and result["artwork"]["mode"] == "manual"
service=Artwork(store)
assert embedded(Path(result["file_path"])) == service.render(service.owned(result["artwork"]["candidate_id"],track),"fit")
assert not any(k.startswith("metadata_source") for k in tags)
print("RESULT="+json.dumps(result))
'''
    result = subprocess.run([sys.executable,"-c",script,track["id"]],env=os.environ|{"PYTHONPATH":str(Path(__file__).parents[1]/"src")},capture_output=True,text=True,timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    final = json.loads(next(line[7:] for line in result.stdout.splitlines() if line.startswith("RESULT=")))
    assert packets(Path(final["file_path"])) == before
