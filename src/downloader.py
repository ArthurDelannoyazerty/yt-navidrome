"""The original working yt-dlp download path, isolated in a fresh process.
Only the removed secondary-download branch is changed. Do not tune its options here.
"""
import os
import sys
import traceback
import yt_dlp
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


def _env_float(name, default):
    raw = (os.getenv(name) or "").split("#", 1)[0].strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        print(f"WARNING Invalid {name}={raw!r}; using {default}", flush=True)
        return default


DOWNLOAD_SLEEP_MIN = _env_float("DOWNLOAD_SLEEP_MIN", 2)
DOWNLOAD_SLEEP_MAX = _env_float("DOWNLOAD_SLEEP_MAX", 6)
PLAYER_CLIENTS = ([c.strip() for c in os.getenv("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
                  or ["default"])
YT_COOKIES_FILE = os.getenv("YT_DLP_COOKIES_FILE", "")

class DownloadBotError(Exception): pass
class DownloadUnavailableError(Exception): pass
class DownloadNetworkError(Exception): pass

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=3, min=3, max=15),
    retry=retry_if_exception_type(DownloadNetworkError),
    reraise=True,
)
def download_audio_file(url: str, track_uuid: str):
    temp_filename = f"temp_{track_uuid}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{temp_filename}.%(ext)s',
        'noplaylist': True,
        'playlistend': 1,
        'extractor_args': {'youtube': {'player_client': PLAYER_CLIENTS}},
        'sleep_interval': DOWNLOAD_SLEEP_MIN,
        'max_sleep_interval': DOWNLOAD_SLEEP_MAX,
        'socket_timeout': 25,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'opus', 'preferredquality': '160'}],
        'quiet': True,
        'no_warnings': True,
    }
    if YT_COOKIES_FILE and os.path.exists(YT_COOKIES_FILE):
        ydl_opts['cookiefile'] = YT_COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return f"{temp_filename}.opus"
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if any(k in err for k in ("sign in", "403", "429", "bot", "not available", "try again later", "precondition check failed")):
            raise DownloadBotError(f"yt-dlp blocked the download: {e}") from e
        if any(k in err for k in ("unavailable", "private", "removed")):
            raise DownloadUnavailableError("Video unavailable or removed.")
        raise DownloadNetworkError(f"Network error during download: {e}")


if __name__ == "__main__":
    try:
        print(download_audio_file(sys.argv[1], sys.argv[2]), flush=True)
    except DownloadBotError:
        traceback.print_exc()
        sys.exit(20)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
