"""English preference and lossless preservation of canonical MusicBrainz names."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from mutagen.oggopus import OggOpus

from localized_names import (
    LANGUAGE_POLICY, apply_to_opus, artist_credit, is_localized,
    native_names, preferred_english_name, preferred_recording_title,
)


def test_musicbrainz_english_aliases_are_opt_in_and_not_guessed():
    raw = {"title": "踊子プリムロゼのテーマ", "aliases": [
        {"name": "Random search hint", "locale": "en", "type": "Search hint", "primary": True},
        {"name": "Primrose, the Dancer", "locale": "en", "type": None, "primary": True},
        {"name": "Odoriko", "locale": "en", "type": None, "primary": False},
    ]}
    assert preferred_english_name(raw) == "Primrose, the Dancer"
    assert preferred_english_name({"title": "踊子プリムロゼのテーマ"}) == "踊子プリムロゼのテーマ"
    assert preferred_english_name({"title": "元のタイトル", "aliases": [
        {"name": "English version", "locale": "en-US", "primary": True},
    ]}) == "English version"
    assert preferred_english_name({"title": "元", "aliases": [
        {"name": "Legal Alias", "locale": "en", "type": "Legal name", "primary": True},
    ]}) == "元"


def test_canonical_credit_keeps_actual_credited_script():
    raw = {"title": "踊子プリムロゼのテーマ", "artist-credit": [
        {"name": "西木康智", "joinphrase": " feat. ",
         "artist": {"name": "Yasunori Nishiki"}},
        {"name": "歌手", "artist": {"name": "Singer"}},
    ]}
    assert artist_credit(raw) == "西木康智 feat. 歌手"
    assert native_names(raw, {"title": "オクトパストラベラー",
                               "artist-credit": [{"name": "西木康智"}]}) == {
        "title": "踊子プリムロゼのテーマ",
        "artist": "西木康智 feat. 歌手",
        "album": "オクトパストラベラー",
        "albumartist": "西木康智",
    }


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "test.opus"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=48000", "-t", "0.2",
         "-c:a", "libopus", str(path)], check=True,
    )
    tags = OggOpus(str(path))
    tags["musicbrainz_trackid"] = ["11111111-1111-4111-8111-111111111111"]
    tags["musicbrainz_albumid"] = ["22222222-2222-4222-8222-222222222222"]
    tags["metadata_block_picture"] = ["test-artwork-not-decoded"]
    tags["comment"] = ["Discovery Date: keep"]
    tags["r128_track_gain"] = ["-200"]
    tags["title"] = ["踊子プリムロゼのテーマ"]
    tags["artist"] = ["西木康智"]
    tags["album"] = ["旧アルバム"]
    tags.save()
    return path


def test_localization_preserves_identity_loudness_art_and_original_names(audio):
    native = {"title": "踊子プリムロゼのテーマ", "artist": "西木康智",
              "album": "オクトパストラベラー", "albumartist": "西木康智"}
    result = apply_to_opus(audio,
                           {"title": "Primrose, the Dancer", "artist": "Yasunori Nishiki",
                            "album": "Octopath Traveler", "albumartist": "Yasunori Nishiki"},
                           native, include_album=True)
    tags = OggOpus(str(audio))
    assert result["title"] == "Primrose, the Dancer"
    assert tags["originaltitle"] == ["踊子プリムロゼのテーマ"]
    assert tags["originalartist"] == ["西木康智"]
    assert tags["originalalbum"] == ["オクトパストラベラー"]
    assert tags["albumartist"] == ["Yasunori Nishiki"]
    assert tags["originalalbumartist"] == ["西木康智"]
    assert tags["musicbrainz_trackid"] == ["11111111-1111-4111-8111-111111111111"]
    assert tags["musicbrainz_albumid"] == ["22222222-2222-4222-8222-222222222222"]
    assert tags["metadata_block_picture"] == ["test-artwork-not-decoded"]
    assert tags["comment"] == ["Discovery Date: keep"]
    assert tags["r128_track_gain"] == ["-200"]
    assert tags["ingestor_language_policy"] == [LANGUAGE_POLICY]
    assert is_localized(audio)


def test_recording_only_approval_does_not_assign_album(audio):
    apply_to_opus(audio, {"title": "Primrose, the Dancer",
                          "artist": "Yasunori Nishiki"},
                  {"title": "踊子プリムロゼのテーマ", "artist": "西木康智"},
                  include_album=False, clear_unassigned_album=True)
    tags = OggOpus(str(audio))
    assert tags["album"] == ["旧アルバム"]  # Existing album tag is not guessed.
    assert "originalalbum" not in tags
    assert tags["originaltitle"] == ["踊子プリムロゼのテーマ"]


def test_activity_report_distinguishes_visible_changes_from_canonical_tags(audio, tmp_path):
    from shutil import copyfile
    from localized_names import language_change_report

    visible = tmp_path / "english.opus"
    copyfile(audio, visible)
    apply_to_opus(visible,
                  {"title": "Primrose, the Dancer", "artist": "Yasunori Nishiki"},
                  {"title": "踊子プリムロゼのテーマ", "artist": "西木康智"})
    message = language_change_report(audio, visible)
    assert "Visible metadata changed" in message
    assert "TITLE:" in message and "Primrose, the Dancer" in message
    assert "ARTIST:" in message and "Yasunori Nishiki" in message
    assert "ORIGINALTITLE" in message and "ORIGINALARTIST" in message
    assert "MusicBrainz IDs and file paths unchanged" in message

    originals_only = tmp_path / "originals.opus"
    copyfile(audio, originals_only)
    apply_to_opus(originals_only,
                  {"title": "踊子プリムロゼのテーマ", "artist": "西木康智"},
                  {"title": "踊子プリムロゼのテーマ", "artist": "西木康智"})
    message = language_change_report(audio, originals_only)
    assert "Visible title, artist and album names unchanged" in message
    assert "ORIGINALTITLE" in message
    assert "English-first policy marker set" in message


def test_shared_resolver_uses_exact_official_english_track_and_keeps_recording_identity():
    """Genshin: the English *track* is not an alias of the CJK recording."""
    recording = "1f245767-ed69-4753-8992-c64eab34ce35"
    english = "342888a1-9244-4d5c-8316-1f7162dd27f4"
    japanese = "97a17f06-e88e-41c9-a522-3e6707dc8c7e"
    unrelated = "11111111-1111-4111-8111-111111111111"
    native = {"title": "未始的供献颂", "artist-credit": [{"name": "HOYO‐MiX"}]}
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE entities (kind TEXT,id TEXT,data TEXT);
        CREATE TABLE release_tracks (release_id TEXT,medium INTEGER,
                                   position INTEGER,recording_id TEXT,title TEXT);
        CREATE TABLE approval_prefetch_cache (kind TEXT,cache_key TEXT,payload TEXT);
    """)
    def release(rid, name, lang, status="Official"):
        con.execute("INSERT INTO entities VALUES (?,?,?)", (
            "release", rid, json.dumps({
                "title": name, "status": status,
                "text-representation": {"language": lang},
            }),
        ))
    release(english, "Genshin Impact - The Stellar Moments, Vol. 5", "eng")
    release(japanese, "原神 - 輝く星々", "jpn")
    con.executemany("INSERT INTO release_tracks VALUES (?,?,?,?,?)", [
        (english, 1, 11, recording, "Offertorium of Fortuitum"),
        (japanese, 1, 11, recording, "未だ響かぬ奉献唱"),
        (english, 1, 12, unrelated, "Wrong recording"),
    ])
    selected = preferred_recording_title(con, recording, native)
    assert selected == {"title": "Offertorium of Fortuitum",
                        "source": "official_release",
                        "release_id": english, "language": "eng"}
    assert preferred_recording_title(
        con, recording, native, assigned_release_id=japanese) == selected
    # Local album edition selection cannot change the English title source.
    assert preferred_recording_title(con, unrelated, {"title": "別の曲"})["source"] == "canonical"
    # A properly locale-marked recording alias always outranks official titles.
    aliased = {**native, "aliases": [{"name": "Official alias", "locale": "en"}]}
    assert preferred_recording_title(con, recording, aliased)["title"] == "Official alias"
    # A French or Japanese official release cannot supply the English fallback.
    con.execute("UPDATE entities SET data=? WHERE id=?", (
        json.dumps({"status": "Official",
                    "text-representation": {"language": "fra"}}), english,
    ))
    assert preferred_recording_title(con, recording, native)["source"] == "canonical"
    # English-language labels are required: Latin script alone is not proof.
    con.execute("UPDATE entities SET data=? WHERE id=?", (
        json.dumps({"status": "Official",
                    "text-representation": {"language": None}}), english,
    ))
    assert preferred_recording_title(con, recording, native)["source"] == "canonical"
    con.close()


def test_shared_resolver_prefers_official_english_to_pseudo_and_preserves_native():
    native = {"title": "未始的供献颂", "aliases": []}
    rid = "1f245767-ed69-4753-8992-c64eab34ce35"
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE entities (kind TEXT,id TEXT,data TEXT);
        CREATE TABLE release_tracks (release_id TEXT,medium INTEGER,
                                   position INTEGER,recording_id TEXT,title TEXT);
        CREATE TABLE approval_prefetch_cache (kind TEXT,cache_key TEXT,payload TEXT);
    """)
    con.execute("INSERT INTO approval_prefetch_cache VALUES (?,?,?)", (
        "pseudo_title", rid, json.dumps({
            "title": "Unofficial translation", "release_id": "22222222-2222-4222-8222-222222222222",
            "language": "eng",
        }),
    ))
    pseudo = preferred_recording_title(con, rid, native)
    assert pseudo["source"] == "pseudo_release"
    assert pseudo["title"] == "Unofficial translation"
    official = "342888a1-9244-4d5c-8316-1f7162dd27f4"
    con.execute("INSERT INTO entities VALUES (?,?,?)", (
        "release", official, json.dumps({
            "status": "Official", "text-representation": {"language": "eng"},
        }),
    ))
    con.execute("INSERT INTO release_tracks VALUES (?,?,?,?,?)", (
        official, 1, 11, rid, "Offertorium of Fortuitum",
    ))
    chosen = preferred_recording_title(con, rid, native)
    assert chosen["source"] == "official_release"
    assert chosen["title"] == "Offertorium of Fortuitum"
    assert native["title"] == "未始的供献颂"
    con.close()
