import pytest
from common import timestamp
from http_policy import ApiDeferred
from providers import YouTube, parse_source


@pytest.mark.parametrize("url", ["https://youtube.com/watch?v=abcdefghijk", "https://youtu.be/abcdefghijk?t=12", "https://music.youtube.com/watch?v=abcdefghijk", "https://youtube.com/shorts/abcdefghijk"])
def test_video_canonicalization(url):
    assert parse_source(url).key == "video:abcdefghijk"


@pytest.mark.parametrize("url", ["https://youtube.com.evil.test/watch?v=abcdefghijk", "file:///etc/passwd", "https://youtube.com/playlist?list=RDtest", "https://youtube.com/channel/example", "https://open.spotify.com/playlist/example", "https://user@youtube.com/watch?v=abcdefghijk"])
def test_reject_unsupported(url):
    with pytest.raises(ValueError):
        parse_source(url)


class API:
    def __init__(self, pages):
        self.pages, self.calls, self.messages = iter(pages), [], []
    def get_json(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return result
    def report(self, level, message):
        self.messages.append((level, message))


def item(vid="abcdefghijk", entry="entry", position=0, title="Music"):
    return {"id": entry, "snippet": {"resourceId": {"videoId": vid}, "title": title, "position": position, "publishedAt": "2021-04-17T23:36:42+02:00"}, "contentDetails": {"videoPublishedAt": "1999-01-01T00:00:00Z"}}


def source():
    return {"source_key": "playlist:PLx", "is_playlist": True, "url": "https://youtube.com/playlist?list=PLx"}


def test_true_timestamp_and_complete_pagination():
    api = API([{"items": [{"snippet": {"title": "Playlist"}}]}, {"items": [item()], "nextPageToken": "next"}, {"items": [item(entry="again", position=1)]}])
    snapshot = YouTube().resolve(source(), api)
    assert len(snapshot.entries) == 2
    assert snapshot.entries[0].added_at == "2021-04-17T21:36:42Z"
    assert api.calls[2][1]["params"]["pageToken"] == "next"
    assert api.calls[1][1]["params"]["maxResults"] == 50
    assert "key" not in api.calls[1][1]["params"]
    assert api.calls[1][1]["headers"]["X-Goog-Api-Key"] == "test-api-key"


def test_second_page_failure_returns_no_partial_snapshot():
    api = API([{"items": [{"snippet": {"title": "Playlist"}}]}, {"items": [item()], "nextPageToken": "next"}, ApiDeferred("Temporary failure")])
    with pytest.raises(ApiDeferred):
        YouTube().resolve(source(), api)


def test_private_entries_warn_and_empty_playlist_is_valid():
    api = API([{"items": [{"snippet": {"title": "Playlist"}}]}, {"items": [item(title="[Private video]")]}])
    assert YouTube().resolve(source(), api).entries == []
    assert api.messages[0][0] == "WARNING"


def test_missing_api_key_fails_without_downloader(monkeypatch):
    monkeypatch.delenv("YT_API_KEY")
    api = API([])
    with pytest.raises(ApiDeferred):
        YouTube().resolve(source(), api)
    assert not api.calls


def test_repeated_page_token_rejected():
    api = API([{"items": [{"snippet": {"title": "Playlist"}}]}, {"items": [], "nextPageToken": "same"}, {"items": [], "nextPageToken": "same"}])
    with pytest.raises(ApiDeferred, match="repeated"):
        YouTube().resolve(source(), api)
