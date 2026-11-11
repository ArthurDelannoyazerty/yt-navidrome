"""Snapshot integrity and search-only contracts for the isolated spotDL bridge."""
from copy import deepcopy
import base64
import json
from types import SimpleNamespace

import pytest

import spotdl_bridge as bridge

PID = "0B0Cix0zWdxLKrBN6dsQcz"
TID = "6VRhkROS2SZHGlp0pxndbJ"
URL = f"https://open.spotify.com/playlist/{PID}"
TRACK = f"https://open.spotify.com/track/{TID}"


def track(identity=TID, **kwargs):
    return {"id": identity, "type": "track", "name": "Song",
            "artists": [{"name": "Artist"}], "duration_ms": 180500, **kwargs}


def occurrence(uid, identity=TID, **kwargs):
    return {"uid": uid, "addedAt": {"isoString": "2026-11-01T12:30:00Z"},
            "itemV2": {"data": {"__typename": "Track", "uri": f"spotify:track:{identity}",
                                  "name": "Song", "artists": {"items": [{"profile": {"name": "Artist"}}]},
                                  "duration": {"totalMilliseconds": 180500}}}, **kwargs}


def page(rows, total=None, *, revision="rev1", offset=0, name="Test playlist"):
    return {"data": {"playlistV2": {"uri": f"spotify:playlist:{PID}", "name": name,
            "revisionId": revision, "content": {"totalCount": len(rows) if total is None else total,
            "pagingInfo": {"offset": offset}, "items": rows}}}}


class Backend:
    def __init__(self, *pages, song=None, match="https://music.youtube.com/watch?v=abcdefghijk"):
        self.pages = list(pages)
        self.calls = []
        self.song = song or track()
        self.match = match
        self.searches = []

    def page(self, kind, identity, offset, limit):
        self.calls.append((kind, identity, offset, limit))
        return deepcopy(self.pages.pop(0))

    def track(self, url):
        self.calls.append(("track", url))
        return deepcopy(self.song)

    def search(self, entry):
        self.searches.append(entry)
        return self.match


def test_complete_paginated_snapshot_keeps_duplicate_occurrences_and_added_date():
    rows = [occurrence("one"), occurrence("two")]
    backend = Backend(page(rows[:1], total=2), page(rows[1:], total=2, offset=1), page(rows[:1], total=2))
    result = bridge.scan(URL, backend)
    assert result["complete"] and result["total_items"] == 2
    assert [row["key"] for row in result["entries"]] == [TID, TID]
    assert [row["entry_id"] for row in result["entries"]] == ["spotify:0", "spotify:1"]
    assert all(row["added_at"] == "2026-11-01T12:30:00Z" for row in result["entries"])
    assert all(row["basis"] == "playlist_added" for row in result["entries"])
    assert [call[2] for call in backend.calls] == [0, 1, 0]


def test_current_v3_identity_and_missing_added_date():
    row = {"uid": "one", "itemV2": {"data": {"uri": f"spotify:track:{TID}",
            "trackDuration": {"totalMilliseconds": 215253}, "mediaType": "AUDIO"}},
            "itemV3": {"data": {"uri": f"spotify:track:{TID}",
            "identityTrait": {"name": "Bangarang (feat. Sirah)",
            "contributors": {"items": [{"name": "Skrillex"}, {"name": "Sirah"}]}}}}}
    result = bridge.scan(URL, Backend(page([row]), page([row])))
    entry = result["entries"][0]
    assert entry["title"] == "Skrillex, Sirah - Bangarang (feat. Sirah)"
    assert entry["duration"] == 215.253
    assert entry["added_at"] is None and entry["basis"] == "first_seen"


def test_explicit_unavailable_local_and_podcast_items_are_counted():
    rows = [occurrence("track"), {"uid": "removed", "itemV2": {"data": None}},
            {"uid": "local", "is_local": True},
            {"uid": "episode", "itemV2": {"data": {"__typename": "Episode", "uri": "spotify:episode:test"}}}]
    result = bridge.scan(URL, Backend(page(rows), page(rows[:1], total=4)))
    assert result["total_items"] == 4 and result["skipped"] == 3
    assert len(result["entries"]) == 1


@pytest.mark.parametrize("bad_page", [
    page([], total=1),
    page([occurrence("a")], total=2, offset=2),
    page([occurrence("a")], total=True),
    page([occurrence("a")], total=bridge.MAX_ITEMS + 1),
    page([occurrence("a")], revision=None),
    {"data": {"playlistV2": None}},
    {"errors": [{"message": "partial"}], "data": {"playlistV2": {}}},
])
def test_missing_or_inconsistent_metadata_never_becomes_complete(bad_page):
    with pytest.raises(bridge.BridgeError):
        bridge.scan(URL, Backend(bad_page))


@pytest.mark.parametrize("change", ["revision", "total", "name"])
def test_revision_and_metadata_rechecked_before_publishing(change):
    initial = page([occurrence("a")])
    after = deepcopy(initial)
    obj = after["data"]["playlistV2"]
    if change == "revision":
        obj["revisionId"] = "rev2"
    elif change == "total":
        obj["content"]["totalCount"] = 2
    else:
        obj["name"] = "renamed"
    with pytest.raises(bridge.BridgeError, match="changed"):
        bridge.scan(URL, Backend(initial, after))


def test_changed_revision_on_middle_page_is_rejected():
    backend = Backend(page([occurrence("one")], total=2),
                      page([occurrence("two")], total=2, offset=1, revision="rev2"))
    with pytest.raises(bridge.BridgeError, match="changed"):
        bridge.scan(URL, backend)


def test_repeated_page_cannot_hide_missing_occurrences():
    backend = Backend(page([occurrence("one")], total=2),
                      page([occurrence("one")], total=2, offset=1))
    with pytest.raises(bridge.BridgeError, match="repeated"):
        bridge.scan(URL, backend)


def test_unknown_track_shape_does_not_silently_remove_a_membership():
    backend = Backend(page([{"uid": "one", "itemV2": {"data": {"__typename": "Surprise"}}}]))
    with pytest.raises(bridge.BridgeError):
        bridge.scan(URL, backend)


@pytest.mark.parametrize("row", [{"uid": "one", "itemV2": {}},
                                  {"uid": "one", "itemV3": {}}, {"uid": "one"}])
def test_missing_envelope_is_not_confused_with_explicit_unavailable(row):
    with pytest.raises(bridge.BridgeError, match="envelope"):
        bridge.scan(URL, Backend(page([row])))


@pytest.mark.parametrize("date", ["not-a-date", "2026-11-01T12:00:00", {"isoString": 123}])
def test_invalid_added_dates_are_rejected_instead_of_invented(date):
    with pytest.raises(bridge.BridgeError, match="addition date"):
        bridge.scan(URL, Backend(page([occurrence("one", addedAt=date)])))


def test_empty_playlist_requires_a_verified_empty_snapshot():
    result = bridge.scan(URL, Backend(page([]), page([])))
    assert result["complete"] and result["total_items"] == 0 and result["entries"] == []


def test_direct_track_is_first_seen_without_album_release_date():
    result = bridge.scan(TRACK, Backend(song=track(album={"release_date": "2001-01-01"})))
    assert result["entries"][0]["added_at"] is None
    assert result["entries"][0]["basis"] == "first_seen"


def test_album_tracks_are_complete_and_keep_first_seen_dates():
    def album(rows):
        return {"data": {"albumUnion": {"uri": f"spotify:album:{PID}", "name": "An album",
                "tracksV2": {"totalCount": 1, "items": rows}}}}
    row = {"track": {**occurrence("one")["itemV2"]["data"], "release_date": "2001-01-01"}}
    result = bridge.scan(f"https://open.spotify.com/album/{PID}", Backend(album([row]), album([row])))
    assert result["total_items"] == 1 and result["entries"][0]["added_at"] is None


def test_resolve_only_searches_and_returns_canonical_transport_separately():
    backend = Backend()
    result = bridge.resolve(TRACK, backend)
    assert result["spotify_url"] == TRACK
    assert result["youtube_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert result["title"] == "Artist - Song"
    assert result["duration"] == 180.5
    assert backend.searches[0]["url"] == TRACK


def test_no_match_is_actionable_and_does_not_fabricate_transport():
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.resolve(TRACK, Backend(match=None))
    assert exc.value.code == "no_match" and not exc.value.retryable


@pytest.mark.parametrize("url", ["http://open.spotify.com/track/" + TID,
    "https://open.spotify.com.evil.test/track/" + TID,
    "https://user:secret@open.spotify.com/track/" + TID,
    "https://open.spotify.com:443/track/" + TID,
    TRACK + "?si=test", TRACK + "#fragment", "spotify:track:" + TID, URL])
def test_resolve_rejects_noncanonical_or_nontrack_input(url):
    backend = Backend()
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.resolve(url, backend)
    assert exc.value.code == "invalid_url" and not backend.calls


@pytest.mark.parametrize("url", ["https://evil.test/watch?v=abcdefghijk",
    "https://www.youtube.com/playlist?list=PLtest",
    "https://www.youtube.com/watch?v=abcdefghijk&list=PLtest",
    "https://www.youtube.com/watch?v=abcdefghijk&v=12345678901",
    "file:///tmp/file", "http://www.youtube.com/watch?v=abcdefghijk",
    "https://user:pass@www.youtube.com/watch?v=abcdefghijk"])
def test_resolve_rejects_unsafe_audio_provider_result(url):
    with pytest.raises(bridge.BridgeError) as exc:
        bridge.resolve(TRACK, Backend(match=url))
    assert exc.value.code == "invalid_match"


def test_json_failure_is_structured_and_never_includes_upstream_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_version", lambda: "4.6.0")
    def fail(url):
        raise RuntimeError("Authorization: Bearer PRIVATE_TOKEN")
    monkeypatch.setattr(bridge, "scan", fail)
    output = tmp_path / "result.json"
    assert bridge.main(["scan", "--url", URL, "--output", str(output)]) == 1
    data = json.loads(output.read_text())
    assert data["version"] == "4.6.0" and data["code"] == "provider_error"
    assert not data["ok"] and data["retryable"]
    assert "PRIVATE_TOKEN" not in output.read_text()


def test_smoke_payload_uses_real_runtime_version(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_version", lambda: "4.6.0")
    monkeypatch.setattr(bridge, "smoke", lambda: {"matching_checked": True})
    output = tmp_path / "result.json"
    assert bridge.main(["smoke", "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == {
        "schema_version": 1, "mode": "smoke", "version": "4.6.0", "ok": True, "matching_checked": True}


@pytest.mark.parametrize("value, expected", [("600", 600), ("90000", 86400),
                                            ("NaN", None), ("bad", None)])
def test_retry_after_is_bounded_without_leaking_response(value, expected):
    exc = RuntimeError("not used")
    exc.response = SimpleNamespace(headers={"Retry-After": value})
    assert bridge._retry_after(exc) == expected


def document_entity(rows, total=None):
    value = page(rows, total)["data"]["playlistV2"]
    value["__typename"] = "Playlist"
    value.pop("revisionId")
    value["content"]["pagingInfo"] = {"nextOffset": None}
    return value


def public_documents(monkeypatch, *entities):
    values = iter(entities)
    monkeypatch.setattr(bridge, "_public_entity", lambda url: deepcopy(next(values)))


def test_full_public_document_needs_two_equal_complete_observations(monkeypatch):
    rows = [occurrence("one", addedAt=None), occurrence("two", addedAt=None)]
    entity = document_entity(rows)
    public_documents(monkeypatch, entity, entity)
    def no_api():
        raise AssertionError("A complete public document must not initialize the public API")
    monkeypatch.setattr(bridge, "SpotDLBackend", no_api)
    result = bridge.scan(URL)
    assert result["scan_method"] == "public_document" and result["complete"]
    assert result["revision"].startswith("html-sha256:")
    assert [entry["key"] for entry in result["entries"]] == [TID, TID]
    assert [entry["position"] for entry in result["entries"]] == [0, 1]
    assert all(entry["added_at"] is None and entry["basis"] == "first_seen" for entry in result["entries"])


@pytest.mark.parametrize("change", ["identity", "order", "date", "title", "skipped"])
def test_public_same_size_mutation_is_rejected_without_falling_back(monkeypatch, change):
    rows = [occurrence("one"), occurrence("two", identity="5i7fZq3chLyCHo3VeB6goD"),
            {"uid": "skip", "itemV2": {"data": {"__typename": "Episode", "uri": "spotify:episode:one"}}}]
    first = document_entity(rows)
    second = deepcopy(first)
    items = second["content"]["items"]
    if change == "identity":
        items[0]["itemV2"]["data"]["uri"] = "spotify:track:" + "1" * 22
    elif change == "order":
        items[0], items[1] = items[1], items[0]
    elif change == "date":
        items[0]["addedAt"]["isoString"] = "2026-11-02T12:30:00Z"
    elif change == "title":
        second["name"] = "Renamed"
    else:
        items[2]["itemV2"]["data"]["uri"] = "spotify:episode:two"
    public_documents(monkeypatch, first, second)
    with pytest.raises(bridge.BridgeError, match="changed between"):
        bridge.scan(URL)


def test_incomplete_public_document_uses_paginated_backend(monkeypatch):
    first = document_entity([occurrence("one")], total=2)
    public_documents(monkeypatch, first)
    api = Backend(page([occurrence("one"), occurrence("two")]),
                  page([occurrence("one")], total=2))
    monkeypatch.setattr(bridge, "SpotDLBackend", lambda: api)
    result = bridge.scan(URL)
    assert result["scan_method"] == "spotdl_public_api" and result["total_items"] == 2


@pytest.mark.parametrize("missing", ["totalCount", "items", "pagingInfo"])
def test_missing_public_fields_cannot_publish_false_empty(missing):
    entity = document_entity([])
    entity["content"].pop(missing)
    with pytest.raises(bridge._PublicDocumentUnsupported):
        bridge._document_snapshot(entity, "playlist", PID)


def test_public_page_with_continuation_is_not_a_full_snapshot():
    entity = document_entity([occurrence("one")])
    entity["content"]["pagingInfo"]["nextOffset"] = 1
    with pytest.raises(bridge._PublicDocumentUnsupported):
        bridge._document_snapshot(entity, "playlist", PID)


def test_public_empty_playlist_requires_full_public_metadata_and_second_read(monkeypatch):
    entity = document_entity([])
    public_documents(monkeypatch, entity, entity)
    result = bridge.scan(URL)
    assert result["complete"] and result["total_items"] == 0 and not result["entries"]
    entity["__typename"] = "NotFound"
    with pytest.raises(bridge._PublicDocumentUnsupported):
        bridge._document_snapshot(entity, "playlist", PID)


def test_second_public_document_becoming_incomplete_defers(monkeypatch):
    public_documents(monkeypatch, document_entity([occurrence("one")]),
                     document_entity([], total=1))
    with pytest.raises(bridge.BridgeError, match="incomplete second"):
        bridge.scan(URL)


def test_public_parser_extracts_only_exact_entity_and_never_uses_embed_data():
    entity = document_entity([occurrence("one")])
    state = {"session": {"accessToken": "UNUSED_FIXTURE_TOKEN"},
             "entities": {"items": {f"spotify:playlist:{PID}": entity}}}
    encoded = base64.b64encode(json.dumps(state).encode()).decode()
    document = '<html><script id="initialState" type="text/plain">' + encoded + '</script></html>'
    assert bridge._entity_from_document(document, "playlist", PID) == entity
    with pytest.raises(bridge._PublicDocumentUnsupported):
        bridge._entity_from_document(document, "playlist", "0" * 22)
    with pytest.raises(bridge._PublicDocumentUnsupported):
        bridge._entity_from_document('<script id="__NEXT_DATA__">{"trackList": []}</script>', "playlist", PID)


def test_public_track_metadata_avoids_spotify_api_initialization(monkeypatch):
    entity = {**track(), "uri": "spotify:track:" + TID, "__typename": "Track"}
    public_documents(monkeypatch, entity)
    def no_api():
        raise AssertionError("Public track metadata must not initialize the public API")
    monkeypatch.setattr(bridge, "SpotDLBackend", no_api)
    result = bridge.scan(TRACK)
    assert result["entries"][0]["url"] == TRACK
    assert result["entries"][0]["added_at"] is None
