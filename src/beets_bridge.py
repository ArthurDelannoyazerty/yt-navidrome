"""Isolated adapter around the pinned beets APIs used by the web workflow."""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from common import LIBRARY, ROOT, STATE, atomic_json, discovery_comment, user_name
from http_policy import ApiDeferred, HttpPolicy
from store import Store


class Bridge:
    def __init__(self, user: str):
        import beets
        from beets import config, plugins
        from beets.library import Library

        if beets.__version__ != "2.14.1":
            raise RuntimeError(
                f"This adapter requires beets 2.14.1; found {beets.__version__}"
            )
        self.store = Store()
        self.http = HttpPolicy(
            self.store,
            lambda level, text: print(
                f"{level} {text}", file=sys.stderr, flush=True
            ),
        )
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
        self.plugins = {plugin.name: plugin for plugin in plugins.find_plugins()}
        expected = set(config["plugins"].as_str_seq())
        if missing := expected - self.plugins.keys():
            raise RuntimeError(
                f"Required beets plugins did not load: {', '.join(sorted(missing))}"
            )
        import acoustid
        acoustid.set_base_url("https://api.acoustid.org/v2/")
        self.lib = Library(str(dbdir / "library.db"), directory=str(directory))
        plugins.send("library_opened", lib=self.lib)
        self.directory = directory

    @staticmethod
    def seed(item, title):
        if not item.title:
            artist, delimiter, song = title.partition(" - ")
            item.title = song if delimiter else title
            if delimiter and not item.artist:
                item.artist = artist
                item.artists = [artist]

    def find_existing(self, track):
        from beets.dbcore.query import MatchQuery

        item = self.lib.get_item(track["beets_id"]) if track.get("beets_id") else None
        return item or self.lib.items(
            MatchQuery("pipeline_id", track["id"], fast=False)
        ).get()

    @staticmethod
    def write_discovery_tags(path: str | Path, value: str | None):
        """Write both common Opus spellings; MediaFile alone uses DESCRIPTION."""
        from mutagen.oggopus import OggOpus

        comment = discovery_comment(value)
        audio = OggOpus(str(path))
        audio["COMMENT"] = [comment]
        audio["DESCRIPTION"] = [comment]
        audio.save()
        return comment

    def delete(self, track):
        item = self.find_existing(track)
        if not item:
            return {"removed": False}
        candidate = Path(os.fsdecode(item.path)).resolve()
        if not candidate.is_relative_to(self.directory):
            raise ValueError(
                "Refusing to delete a beets item outside this user's library"
            )
        item.remove(delete=candidate.is_file(), with_album=True)
        return {"removed": True}

    def audit_many(self, tracks: list[dict]):
        result = {}
        for track in tracks:
            item = self.find_existing(track)
            result[track["id"]] = {
                "found": bool(item),
                "path": os.fsdecode(item.path) if item else None,
                "beets_id": item.id if item else None,
            }
        return result

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
        self.raise_if_deferred()
        if self.http.failures:
            raise RuntimeError("; ".join(self.http.failures))
        plugins.send("import_task_apply", session=session, task=task)
        choices = [
            {
                "title": candidate.info.title,
                "artist": candidate.info.artist or "",
                "album": candidate.info.album or "",
                "mbid": candidate.info.track_id,
                "similarity": round(100 * (1 - float(candidate.distance)), 1),
                "description": candidate.disambig_string,
                "tags": candidate.info.item_data,
                "kind": "candidate",
            }
            for candidate in candidates
        ]
        fingerprint = {
            key: item.get(key)
            for key in ("acoustid_id", "acoustid_fingerprint")
            if item.get(key)
        }
        for choice in choices:
            choice["tags"].update(fingerprint)
        return {
            "choices": choices,
            "automatic": recommendation == Recommendation.strong,
            "recommendation": recommendation.name,
        }

    def raise_if_deferred(self):
        if self.http.deferred_failures:
            raise ApiDeferred(
                "; ".join(self.http.deferred_failures),
                retry_at=self.http.deferred_until or None,
            )

    def metadata_for_recording(self, recording_id: str):
        mb = self.plugins["musicbrainz"]
        info = mb.track_for_id(recording_id)
        self.raise_if_deferred()
        if not info:
            raise ValueError("MusicBrainz recording was not found")
        return {
            "title": info.title,
            "artist": info.artist or "",
            "album": info.album or "",
            "mbid": info.track_id,
            "similarity": 100.0,
            "description": "Current confirmed identity",
            "tags": info.item_data,
            "kind": "current",
        }

    def release_metadata(self, recording_id, explicit_release=None):
        mb = self.plugins["musicbrainz"]
        release_id = explicit_release
        if not release_id:
            releases, offset = [], 0
            while True:
                data = mb.mb_api.get_json(
                    mb.mb_api.api_root + "/release",
                    params={"recording": recording_id, "limit": 100, "offset": offset},
                )
                page = data.get("releases", [])
                releases.extend(page)
                offset += len(page)
                if offset >= data.get("release_count", offset) or not page:
                    break
            official = [release for release in releases if release.get("status") == "Official"]
            if not official:
                return None, None
            release_id = min(
                official,
                key=lambda release: (release.get("date") or "9999", release["id"]),
            )["id"]
        album = mb.album_for_id(release_id)
        self.raise_if_deferred()
        if not album:
            raise ValueError("MusicBrainz release was not found")
        matches = [track for track in album.tracks if track.track_id == recording_id]
        if not matches:
            raise ValueError("The selected release does not contain this recording")
        return album, matches[0].merge_with_album(album)

    def finalize(self, track, path, selected, *, mode="apply", overrides=None,
                 operation=None):
        from beets.dbcore.query import MatchQuery
        from beets.library import Item
        from beetsplug._utils import art

        overrides = overrides or {}
        existing = self.find_existing(track)
        if mode == "comment":
            if not existing:
                existing = Item.from_path(str(path))
                existing.pipeline_id = track["id"]
                existing.add(self.lib)
            if track.get("discovered_at"):
                existing.comments = discovery_comment(track["discovered_at"])
            existing.write()
            self.write_discovery_tags(os.fsdecode(existing.path), track.get("discovered_at"))
            existing.store()
            return self.result(existing)

        item = existing or Item.from_path(str(path))
        item.path = os.fsencode(str(Path(path).resolve()))
        self.seed(item, track["title"])
        album_info = None
        if selected and not selected.get("asis"):
            for field in (
                "album", "albumartist", "mb_albumid", "mb_releasegroupid",
                "mb_releasetrackid",
            ):
                item[field] = ""
            for field in (
                "year", "month", "day", "original_year", "original_month",
                "original_day", "track", "tracktotal", "disc", "disctotal",
            ):
                item[field] = 0
            item.albumartists = []
            item.album_id = None
            item.lyrics = ""
            item.update(selected["tags"])
            recording = selected.get("mbid") or item.mb_trackid
            if recording:
                album_info, data = self.release_metadata(
                    recording, overrides.get("release_id")
                )
                if data:
                    item.update(data)
                else:
                    print(
                        "WARNING No official release found; album/date left unfilled rather than invented",
                        file=sys.stderr,
                    )

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
        item.source_url = track.get("source_url", "")
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

        item.write()
        item.store()
        item.move(with_album=False)
        final_path = Path(os.fsdecode(item.path))
        if not final_path.is_file():
            raise RuntimeError("beets did not produce a final audio file")
        warnings: list[str] = []

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
                art.embed_item(
                    self.plugins["embedart"]._log,
                    item,
                    album.artpath,
                    0,
                    None,
                    0,
                    False,
                )

        optional("Artwork", artwork)
        optional("Lyrics", lambda: self.plugins["lyrics"].add_item_lyrics(item, True))
        optional("ReplayGain", lambda: self.plugins["replaygain"].handle_track(item, True))
        # Plugins may write the file again, so force both raw comment keys last.
        self.write_discovery_tags(final_path, track.get("discovered_at"))
        item.pipeline_complete = "yes"
        item.store()
        warnings.extend(self.http.failures)
        if not item.year:
            warnings.append("Release date is unknown; no discovery/upload date was substituted")
        if not item.lyrics:
            warnings.append("No lyrics were available from the configured lyrics source")
        if not item.get("r128_track_gain") and item.get("r128_track_gain") != 0:
            warnings.append("No R128 track gain was stored; inspect ReplayGain logs")
        return self.result(item) | {"warnings": warnings}

    @staticmethod
    def result(item):
        return {
            "file_path": os.fsdecode(item.path),
            "beets_id": item.id,
            "matched_title": f"{item.artist} - {item.title}",
            "mbid": item.mb_trackid or None,
            "release_id": item.mb_albumid or None,
        }


def main(request):
    mode = request["mode"]
    user = request.get("user") or request.get("track", {}).get("user_id")
    bridge = Bridge(user)
    if mode == "audit_many":
        return bridge.audit_many(request["tracks"])
    if mode == "metadata":
        return bridge.metadata_for_recording(request["mbid"])

    track = request["track"]
    path = request.get("path", "")
    if mode == "delete":
        return bridge.delete(track)
    if mode == "inspect":
        item = bridge.find_existing(track)
        if item and item.get("pipeline_operation") == request.get("operation"):
            candidate = Path(os.fsdecode(item.path)).resolve()
            if candidate.is_file() and candidate.is_relative_to(bridge.directory):
                return {
                    "found": True,
                    "complete": item.get("pipeline_complete") == "yes",
                    **bridge.result(item),
                }
        return {"found": False}
    if mode == "identify":
        return bridge.identify(path, track["title"])

    selected = request.get("selected")
    overrides = request.get("overrides", {})
    if overrides.get("mbid") or overrides.get("release_id"):
        recording_id = overrides.get("mbid") or track.get("mbid")
        if not recording_id:
            raise ValueError(
                "Specify a recording ID when assigning a release to an unmatched track"
            )
        selected = bridge.metadata_for_recording(recording_id)
    return bridge.finalize(
        track,
        path,
        selected,
        mode=mode,
        overrides=overrides,
        operation=request.get("operation"),
    )


if __name__ == "__main__":
    try:
        request = json.loads(Path(sys.argv[1]).read_text())
        result = main(request)
        atomic_json(Path(sys.argv[2]), result)
    except ApiDeferred as exc:
        print(
            "WARNING DEFERRED " + json.dumps({
                "message": str(exc),
                "retry_at": getattr(exc, "retry_at", None),
            }),
            file=sys.stderr,
            flush=True,
        )
        sys.exit(75)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
