"""Isolated adapter around the pinned beets APIs used by the web workflow."""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

from audio_tags import loudness_values, write_loudness_tags
from source_metadata import clear_descriptive_tags, clear_release_tags
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
        artist, delimiter, song = title.partition(" - ")
        if not item.title:
            item.title = song if delimiter else title
        if delimiter and not item.artist and item.title in {song, title}:
            item.artist = artist
            item.artists = [artist]

    def find_existing(self, track):
        from beets.dbcore.query import MatchQuery

        item = self.lib.get_item(track["beets_id"]) if track.get("beets_id") else None
        if item:
            owner = item.get("pipeline_id")
            recorded = track.get("file_path")
            if (owner and owner != track["id"]) or (
                not owner and recorded
                and Path(os.fsdecode(item.path)).resolve() != Path(recorded).resolve()
            ):
                item = None
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

    def delete(self, track, *, delete_file=True):
        item = self.find_existing(track)
        if not item:
            return {"removed": False}
        candidate = Path(os.fsdecode(item.path)).resolve()
        if not candidate.is_relative_to(self.directory):
            raise ValueError(
                "Refusing to delete a beets item outside this user's library"
            )
        item.remove(delete=delete_file and candidate.is_file(), with_album=True)
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
        # Never let an embedded, previously assigned MBID short-circuit a fresh
        # candidate search. Fingerprinting and title/artist search start anew.
        ignored_mbid = item.mb_trackid or None
        item.mb_trackid = ""
        item.mb_albumid = ""
        item.acoustid_id = ""
        item.acoustid_fingerprint = ""
        seed = {"title": item.title, "artist": item.artist, "length_seconds": item.length,
                "ignored_embedded_mbid": ignored_mbid}
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
            "diagnostics": {"search_seed": seed,
                            "fingerprint_generated": bool(item.get("acoustid_fingerprint")),
                            "acoustid_id_present": bool(item.get("acoustid_id")),
                            "candidate_count": len(choices)},
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
        """Enrich a confirmed recording only with a release containing that MBID.

        MusicBrainz's release browse results are associations, not a guarantee
        that beets will expose the recording in its filtered album track list
        (e.g. video/data tracks). Never fail an import just because the earliest
        *automatically* chosen edition cannot be used for enrichment.
        """
        mb = self.plugins["musicbrainz"]

        def compatible_release(release_id):
            album = mb.album_for_id(release_id)
            # A provider cooldown must still defer the entire operation; it is
            # not evidence that the recording has no matching release.
            self.raise_if_deferred()
            if not album:
                return None
            for track in album.tracks:
                if str(track.track_id).lower() == str(recording_id).lower():
                    return album, track.merge_with_album(album)
            return None

        if explicit_release:
            matched = compatible_release(explicit_release)
            if matched is None:
                raise ValueError(
                    f"MusicBrainz release {explicit_release} is missing or does not "
                    f"contain recording {recording_id} in its usable tracks. "
                    "Select another edition or choose automatic release selection."
                )
            return matched

        releases, offset = [], 0
        while True:
            data = mb.mb_api.get_json(
                mb.mb_api.api_root + "/release",
                params={"recording": recording_id, "limit": 100, "offset": offset},
            )
            self.raise_if_deferred()
            page = data.get("releases", [])
            releases.extend(page)
            offset += len(page)
            if offset >= data.get("release_count", offset) or not page:
                break
        official = sorted(
            (release for release in releases
             if release.get("status") == "Official" and release.get("id")),
            key=lambda release: (release.get("date") or "9999", release["id"]),
        )
        # A popular recording can occur on hundreds of compilations. Keep the
        # lookup bounded instead of holding the ingestion worker for minutes.
        max_lookups = 5
        for release in official[:max_lookups]:
            matched = compatible_release(release["id"])
            if matched is not None:
                return matched
            print(
                f"WARNING MusicBrainz release {release['id']} does not expose "
                f"recording {recording_id}; checking another official edition",
                file=sys.stderr,
            )
        if official:
            print(
                f"WARNING No usable official MusicBrainz release found for recording "
                f"{recording_id} among {min(len(official), max_lookups)} checked; "
                "saving the confirmed recording without album/date enrichment",
                file=sys.stderr,
            )
        return None, None

    def measure_loudness(self, item):
        """Force fresh true-peak analysis, including when source gain tags exist.

        The pinned R128Task intentionally drops peak data. The regular task can
        measure both without storing a conflicting conventional gain tag.
        """
        from beetsplug.replaygain import PeakMethod, RgTask

        plugin = self.plugins["replaygain"]
        if plugin.backend_name != "ffmpeg":
            raise ValueError("This pipeline requires the ffmpeg ReplayGain backend")
        task = RgTask([item], None, 84, PeakMethod.true, "ffmpeg", plugin._log)
        task = plugin.backend_instance.compute_track_gain(task)
        if not task.track_gains or len(task.track_gains) != 1:
            raise ValueError("ReplayGain produced no track measurement")
        measured = task.track_gains[0]
        values = loudness_values(measured.gain, measured.peak)
        item.r128_track_gain = values["r128_track_gain"] / 256.0
        item.rg_track_gain = None
        item.rg_track_peak = values["replaygain_track_peak"]
        item.r128_album_gain = None
        item.rg_album_gain = None
        item.rg_album_peak = None
        return values

    def repair_file(self, track, path):
        """Only touch the staged copy; no identify, download, move or DB writes."""
        from beets.library import Item

        item = Item.from_path(str(path))
        values = self.measure_loudness(item)
        item.write()
        self.write_discovery_tags(path, track.get("discovered_at"))
        write_loudness_tags(path, values)
        return {"file_path": str(path), "loudness": values}

    def refresh_item(self, track):
        from beets.library import Item

        path = Path(track["file_path"]).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise ValueError("Current audio is missing or outside this user's library")
        item = self.find_existing(track)
        if item:
            item.path = os.fsencode(path)
            item.read()
            item.store()
        else:
            item = Item.from_path(str(path))
            item.pipeline_id = track["id"]
            item.add(self.lib)
        return self.result(item)

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
            if Path(os.fsdecode(existing.path)).resolve() != Path(path).resolve():
                raise ValueError("Beets path differs from the current asset; audit before changing tags")
            # A discovery-only update must not rewrite unrelated tags from the
            # beets row (including empty legacy identity/album/lyrics fields).
            existing.comments = discovery_comment(track.get("discovered_at"))
            self.write_discovery_tags(os.fsdecode(existing.path), track.get("discovered_at"))
            existing.store()
            return self.result(existing)

        source_reset = bool(selected and selected.get("kind") == "source")
        replace_identity = bool(selected and not selected.get("asis"))
        if replace_identity:
            # Clear stale art, lyrics, artists and IDs on the candidate, not the
            # still-playing active file. Reload the beets row to avoid old tags
            # leaking back into the replacement.
            clear_descriptive_tags(path, selected.get("source") if source_reset else None)
        item = existing or Item.from_path(str(path))
        item.path = os.fsencode(str(Path(path).resolve()))
        if replace_identity:
            item.album_id = None
            # Reset cached/legacy writable fields too; a read alone can leave
            # alias/flexible tag values on a reused beets item.
            item.clear()
            item.read()
        if source_reset or not replace_identity:
            self.seed(item, selected["source"]["source_title"] if source_reset else track["title"])
        album_info = None
        release_warning = None
        if replace_identity and not source_reset:
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
                    # The recording identity is independent of release metadata.
                    # A candidate's embedded album ID/date must not survive when
                    # no official release containing this recording was resolved.
                    for field in ("album", "mb_albumid", "mb_releasegroupid", "mb_releasetrackid"):
                        item[field] = ""
                    for field in ("year", "month", "day", "original_year", "original_month", "original_day"):
                        item[field] = 0
                    release_warning = (
                        "Recording identity was applied without verified release metadata: "
                        "no usable official MusicBrainz release was resolved for this recording. "
                        "Use Search MusicBrainz to select a compatible edition if needed."
                    )
                    print(f"WARNING {release_warning}", file=sys.stderr)

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
        # Analyze before any persistent beets mutation. Failure must not activate
        # audio with absent or stale gain values.
        loudness = self.measure_loudness(item)
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
        if release_warning:
            warnings.append(release_warning)

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

        if not source_reset:
            optional("Artwork", artwork)
            optional("Lyrics", lambda: self.plugins["lyrics"].add_item_lyrics(item, True))
        # Beets/MediaFile may emit empty identity fields while writing a reused
        # item. A source reset has an explicit allowlist, so enforce it after the
        # last beets write as well, then synchronize the DB from the final file.
        if source_reset:
            clear_descriptive_tags(final_path, selected["source"])
        elif release_warning:
            # A release lookup can fail after the recording was identified.
            # Do not let beets republish an incompatible edition's raw tags.
            clear_release_tags(final_path, overrides.get("album"))
        # Plugins may write the file again, so force the raw compatibility tags last.
        self.write_discovery_tags(final_path, track.get("discovered_at"))
        write_loudness_tags(final_path, loudness)
        if source_reset or release_warning:
            # Synchronize beets with the actual Opus fields after selective tag
            # cleanup so a later discovery-comment update cannot resurrect them.
            item.read()
        item.pipeline_complete = "yes"
        item.store()
        warnings.extend(self.http.failures)
        if not item.year:
            warnings.append("Release date is unknown; no discovery/upload date was substituted")
        if not item.lyrics and not source_reset:
            warnings.append("No lyrics were available from the configured lyrics source")
        if not item.get("r128_track_gain") and item.get("r128_track_gain") != 0:
            warnings.append("No R128 track gain was stored; inspect ReplayGain logs")
        return self.result(item) | {"warnings": warnings}

    @staticmethod
    def result(item):
        return {
            "file_path": os.fsdecode(item.path),
            "beets_id": item.id,
            "matched_title": f"{item.artist} - {item.title}" if item.artist else item.title,
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
    if mode in {"delete", "forget"}:
        return bridge.delete(track, delete_file=mode == "delete")
    if mode == "repair":
        return bridge.repair_file(track, path)
    if mode == "refresh":
        return bridge.refresh_item(track)
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
        if not selected or selected.get("asis") or selected.get("mbid") != recording_id:
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
