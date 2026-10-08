"""Offline regression checks of the retained download function, without networking."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "downloader.py"


def load_function(error=None, cookies=""):
    calls = []

    class DownloadError(Exception):
        pass

    class Client:
        def __init__(self, options):
            calls.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def download(self, urls):
            calls.append(urls)
            if error:
                raise DownloadError(error)

    tree = ast.parse(SOURCE.read_text())
    tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    values = {
        "os": os,
        "yt_dlp": SimpleNamespace(
            YoutubeDL=Client,
            utils=SimpleNamespace(DownloadError=DownloadError),
        ),
        "DOWNLOAD_SLEEP_MIN": 2,
        "DOWNLOAD_SLEEP_MAX": 6,
        "PLAYER_CLIENTS": ["default"],
        "YT_COOKIES_FILE": cookies,
    }
    exec(compile(tree, str(SOURCE), "exec"), values)
    return values, calls


def test_download_options_and_call_are_unchanged():
    values, calls = load_function()
    assert values["download_audio_file"](
        "https://youtube.com/watch?v=abcdefghijk", "track-id"
    ) == "temp_track-id.opus"
    assert calls == [{
        "format": "bestaudio/best",
        "outtmpl": "temp_track-id.%(ext)s",
        "noplaylist": True,
        "playlistend": 1,
        "extractor_args": {"youtube": {"player_client": ["default"]}},
        "sleep_interval": 2,
        "max_sleep_interval": 6,
        "socket_timeout": 25,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "160",
        }],
        "quiet": True,
        "no_warnings": True,
    }, ["https://youtube.com/watch?v=abcdefghijk"]]


def test_existing_cookie_file_is_passed_through(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("fixture")
    values, calls = load_function(cookies=str(cookies))
    values["download_audio_file"]("url", "id")
    assert calls[0]["cookiefile"] == str(cookies)


@pytest.mark.parametrize(
    "message,kind",
    [
        ("sign in to confirm", "DownloadBotError"),
        ("HTTP 403", "DownloadBotError"),
        ("private video", "DownloadUnavailableError"),
        ("connection reset", "DownloadNetworkError"),
    ],
)
def test_failure_classification_without_another_downloader(message, kind):
    values, _ = load_function(error=message)
    with pytest.raises(values[kind]):
        values["download_audio_file"]("url", "id")


def test_retry_contract():
    function = next(
        node for node in ast.parse(SOURCE.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "download_audio_file"
    )
    assert ast.unparse(function.decorator_list[0]) == (
        "retry(stop=stop_after_attempt(3), "
        "wait=wait_exponential(multiplier=3, min=3, max=15), "
        "retry=retry_if_exception_type(DownloadNetworkError), reraise=True)"
    )
    assert "cobalt" not in SOURCE.read_text().lower()
