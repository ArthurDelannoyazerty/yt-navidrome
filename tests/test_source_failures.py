"""URL selection and permanent/transient download failure regressions."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from providers import parse_source


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=abcdefghijk&list=PLcontext",
    "https://youtu.be/abcdefghijk?list=PLcontext",
    "https://youtube.com/shorts/abcdefghijk?list=RDcontext",
])
def test_contextual_playlist_does_not_expand_video_import(url):
    result = parse_source(url)
    assert result.key == "video:abcdefghijk"
    assert not result.is_playlist


def test_explicit_playlist_still_imports_playlist():
    assert parse_source("https://youtube.com/playlist?list=PLcontext").is_playlist


@pytest.mark.parametrize("message,expected", [
    ("Requested format is not available", "DownloadPermanentError"),
    ("Unsupported URL", "DownloadPermanentError"),
    ("ffmpeg not found", "DownloadPermanentError"),
    ("connection reset by peer", "DownloadNetworkError"),
    ("Service temporarily unavailable", "DownloadNetworkError"),
    ("HTTP Error 503", "DownloadNetworkError"),
    ("Video unavailable: Sign in to confirm you are not a bot", "DownloadBotError"),
    ("Private video", "DownloadUnavailableError"),
])
def test_download_failure_classification(message, expected):
    source = Path(__file__).resolve().parents[1] / "src/downloader.py"
    tree = ast.parse(source.read_text())
    tree.body = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))]
    for item in tree.body:
        if isinstance(item, ast.FunctionDef):
            item.decorator_list = []

    class DownloadError(Exception):
        pass

    class Client:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def download(self, urls):
            raise DownloadError(message)

    namespace = {"os": os, "yt_dlp": SimpleNamespace(
        YoutubeDL=Client, utils=SimpleNamespace(DownloadError=DownloadError)),
        "DOWNLOAD_SLEEP_MIN": 2, "DOWNLOAD_SLEEP_MAX": 6,
        "PLAYER_CLIENTS": ["default"], "YT_COOKIES_FILE": ""}
    exec(compile(tree, str(source), "exec"), namespace)
    with pytest.raises(namespace[expected]):
        namespace["download_audio_file"]("https://youtube.com/watch?v=abcdefghijk", "test")
