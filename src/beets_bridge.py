"""Small adapter to beets 2.14.1, executed in an isolated process per job.

Beets owns matching/scoring, MusicBrainz translation, fingerprinting, music tags,
lyrics, gain analysis, artwork and destination paths. The adapter supplies the
web approval boundary and release context that singleton imports otherwise lack.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from common import LIBRARY, ROOT, STATE, atomic_json, discovery_comment, user_name
from http_policy import HttpPolicy
from store import Store


class Bridge:
    def __init__(self, user: str):
        import beets
        from beets import config, plugins
        from beets.library import Library

        if beets.__version__ != "2.14.1":
            raise RuntimeError(f"This adapter requires beets 2.14.1; found {beets.__version__}")
        self.store = Store()
        self.http = HttpPolicy(self.store, lambda level, text: print(f"{level} {text}", file=sys.stderr, flush=True))
        self.http.install()
        user_name(user)
        directory = (LIBRARY / user).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        dbdir = STATE / "beets" / user
        dbdir.mkdir(parents=True, exist_ok=True)
        config.read(user=False)
        config.set_file(os.getenv("BEETS_CONFIG", str(ROOT.parent / "beets.yaml")))
        config["directory"] = str(directory)
        config["library"] = str(dbdir / "library.db")
        config["statefile"] = str(dbdir / "state.pickle")
        plugins.load_plugins()
        self.plugins = {p.name: p for p in plugins.find_plugins()}
        expected = set(config["plugins"].as_str_seq())
        if missing := expected - self.plugins.keys():
            raise RuntimeError(f"Required beets plugins did not load: {', '.join(sorted(missing))}")
        # pyacoustid defaults to an HTTP endpoint; use the service's HTTPS endpoint.
        import acoustid
        acoustid.set_base_url("https://api.acoustid.org/v2/")
        self.lib = Library(str(dbdir / "library.db"), directory=str(directory))
        plugins.send("library_opened", lib=self.lib)
        self.directory = directory

    @staticmethod
    def seed(item, title):
        # Seed text only. No custom matching algorithm and no playlist->album mapping.
        if not item.title:
            artist, delimiter, song = title.partition(" - ")
            item.title = song if delimiter else title
            if delimiter and not item.artist:
                item.artist = artist
                item.artists = [artist]

    def find_existing(self, track):
        from beets.dbcore.query import MatchQuery
        item = self.lib.get_item(track["beets_id"]) if track.get("beets_id") else None
        return item or self.lib.items(MatchQuery("pipeline_id", track["id"], fast=False)).get()

    def identify(self, path, title):
        from beets import config, plugins
        from beets.autotag import Recommendation, Source, tag_item
        from beets.importer import SingletonImportTask
        from beets.library import Item

        item = Item.from_path(str(path))
        self.seed(item, title)
        task = SingletonImportTask(None, item)
        session = SimpleNamespace(config=config["import"])
        plugins.send("import_task_start", session=session, task=task)
        candidates, recommendation = tag_item(Source.from_item(item))
        # Beets can suppress some service exceptions; a failed HTTP operation must
        # not masquerade as a confident no-match or trigger an automatic import.
        if self.http.failures:
            raise RuntimeError("; ".join(self.http.failures))
        plugins.send("import_task_apply", session=session, task=task)
        choices = [{"title": c.info.title, "artist": c.info.artist or "", "album": c.info.album or "",
                    "mbid": c.info.track_id, "similarity": round(100 * (1 - float(c.distance)), 1),
                    "description": c.disambig_string, "tags": c.info.item_data}
                   for c in candidates]
        fingerprint = {k: item.get(k) for k in ("acoustid_id", "acoustid_fingerprint") if item.get(k)}
        for choice in choices:
            choice["tags"].update(fingerprint)
        return {"choices": choices, "automatic": recommendation == Recommendation.strong,
                "recommendation": recommendation.name}

    def release_metadata(self, recording_id, explicit_release=None):
        """Select an actual official release, never an ingestion playlist.

        Recording matching keeps beets' default score. Release association is a
        separate, deterministic earliest-known-official-release policy, overridable
        by an explicit MusicBrainz release ID in the UI.
        """
        mb = self.plugins["musicbrainz"]
        release_id = explicit_release
        if not release_id:
            releases, offset = [], 0
            while True:
                data = mb.mb_api.get_json(mb.mb_api.api_root + "/release", params={
                    "recording": recording_id, "limit": 100, "offset": offset})
                page = data.get("releases", [])
                releases.extend(page)
                offset += len(page)
                if offset >= data.get("release_count", offset) or not page:
                    break
            official = [r for r in releases if r.get("status") == "Official"]
            if not official:
                return None, None
            release_id = min(official, key=lambda r: (r.get("date") or "9999", r["id"]))["id"]
        album = mb.album_for_id(release_id)
        if not album:
            raise ValueError("MusicBrainz release was not found")
        matches = [t for t in album.tracks if t.track_id == recording_id]
        if not matches:
            raise ValueError("The selected release does not contain this recording")
        return album, matches[0].merge_with_album(album)

    def finalize(self, track, path, selected, *, mode="apply", overrides=None, operation=None):
        from beets.dbcore.query import MatchQuery
        from beets.library import Item
        from beetsplug._utils import art

        overrides = overrides or {}
        existing = self.find_existing(track)
        if mode == "comment":
            if not existing:
                # Adopt an existing legacy file without guessing missing metadata.
                existing = Item.from_path(str(path))
                existing.pipeline_id = track["id"]
                existing.add(self.lib)
            if track.get("discovered_at"):
                existing.comments = discovery_comment(track["discovered_at"])
            existing.write()
            existing.store()
            return self.result(existing)
        item = existing or Item.from_path(str(path))
        # Staging path belongs to this job. Never delete the previous good file here.
        item.path = os.fsencode(str(Path(path).resolve()))
        self.seed(item, track["title"])
        if selected and not selected.get("asis"):
            # A newly selected recording must not inherit an unrelated old release.
            # Do not clear the complete file: preserve unrelated tags and artwork.
            for field in ("album", "albumartist", "mb_albumid", "mb_releasegroupid", "mb_releasetrackid"):
                item[field] = ""
            for field in ("year", "month", "day", "original_year", "original_month", "original_day", "track", "tracktotal", "disc", "disctotal"):
                item[field] = 0
            item.albumartists = []
            item.album_id = None
            item.lyrics = ""  # Old lyrics must not survive identification as a different song.
            item.update(selected["tags"])
            recording = selected.get("mbid") or item.mb_trackid
            if recording:
                _, data = self.release_metadata(recording, overrides.get("release_id"))
                if data:
                    item.update(data)
                else:
                    print("WARNING No official release found; album/date left unfilled rather than invented", file=sys.stderr)
        if overrides.get("album") is not None and overrides["album"] != item.album:
            item.mb_albumid = ""
            item.mb_releasegroupid = ""
            item.album_id = None
        for key in ("artist", "title", "album"):
            if overrides.get(key) is not None:
                setattr(item, key, overrides[key])
        if overrides.get("artist") is not None:
            item.albumartist = overrides["artist"]
            item.artists = [overrides["artist"]]
            item.albumartists = [overrides["artist"]]
        if track.get("discovered_at"):
            item.comments = discovery_comment(track["discovered_at"])
        item.pipeline_id = track["id"]
        item.source_url = track["url"]
        item.discovered_at = track.get("discovered_at") or ""
        item.pipeline_operation = operation or ""
        item.pipeline_complete = "no"
        if not item.id:
            item.add(self.lib)
        album = None
        if item.mb_albumid and item.album:
            album = self.lib.albums(MatchQuery("mb_albumid", item.mb_albumid)).get()
            if album:
                item.album_id = album.id
                item.store()
            else:
                album = self.lib.add_album([item])
        elif not item.mb_albumid:
            item.album_id = None
        # Writes retain real date tags; filesystem times are not forged.
        item.write()
        item.store()
        item.move(with_album=False)
        if not Path(os.fsdecode(item.path)).is_file():
            raise RuntimeError("beets did not produce a final audio file")
        warnings = []

        def optional(label, action):
            try:
                action()
            except Exception as exc:
                warnings.append(f"{label}: {exc}")
                print(f"ERROR {label}: {exc}", file=sys.stderr)

        def artwork():
            if not album:
                return
            fetcher = self.plugins["fetchart"]
            if not album.artpath or not Path(os.fsdecode(album.artpath)).is_file():
                candidate = fetcher.art_for_album(album, [], local_only=False)
                if candidate and candidate.path:
                    album.set_art(candidate.path)
                    album.store()
            if album.artpath:
                art.embed_item(self.plugins["embedart"]._log, item, album.artpath, 0, None, 0, False)

        optional("Artwork", artwork)
        optional("Lyrics", lambda: self.plugins["lyrics"].add_item_lyrics(item, True))
        optional("ReplayGain", lambda: self.plugins["replaygain"].handle_track(item, True))
        item.pipeline_complete = "yes"
        item.store()
        warnings.extend(self.http.failures)
        if not item.year:
            warnings.append("Release date is unknown; no discovery/upload date was substituted")
        if not item.lyrics:
            warnings.append("No lyrics were available from the configured lyrics source")
        if not item.get("r128_track_gain") and item.get("r128_track_gain") != 0:
            # r128_track_gain is nullable; a legitimate zero is not an error.
            warnings.append("No R128 track gain was stored; inspect ReplayGain logs")
        return self.result(item) | {"warnings": warnings}

    @staticmethod
    def result(item):
        return {"file_path": os.fsdecode(item.path), "beets_id": item.id,
                "matched_title": f"{item.artist} - {item.title}", "mbid": item.mb_trackid or None,
                "release_id": item.mb_albumid or None}


def main(request):
    bridge = Bridge(request["track"]["user_id"])
    track, path, mode = request["track"], request["path"], request["mode"]
    if mode == "inspect":
        item = bridge.find_existing(track)
        if item and item.get("pipeline_operation") == request.get("operation"):
            candidate = Path(os.fsdecode(item.path)).resolve()
            if candidate.is_file() and candidate.is_relative_to(bridge.directory):
                return {"found": True, "complete": item.get("pipeline_complete") == "yes", **bridge.result(item)}
        return {"found": False}
    if mode == "identify":
        return bridge.identify(path, track["title"])
    selected = request.get("selected")
    overrides = request.get("overrides", {})
    if overrides.get("mbid") or overrides.get("release_id"):
        recording_id = overrides.get("mbid") or track.get("mbid")
        if not recording_id:
            raise ValueError("Specify a recording ID when assigning a release to an unmatched track")
        mb = bridge.plugins["musicbrainz"]
        info = mb.track_for_id(recording_id)
        if not info:
            raise ValueError("MusicBrainz recording was not found")
        selected = {"mbid": info.track_id, "tags": info.item_data}
    return bridge.finalize(track, path, selected, mode=mode, overrides=overrides,
                           operation=request.get("operation"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("beets").setLevel(logging.DEBUG)
    try:
        request = json.loads(Path(sys.argv[1]).read_text())
        result = main(request)
        atomic_json(Path(sys.argv[2]), result)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
