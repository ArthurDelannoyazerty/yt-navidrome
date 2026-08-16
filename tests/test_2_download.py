import yt_dlp

# --- CONFIGURATION ---
# Use a SINGLE video URL for this test, not your whole playlist!
TEST_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ" 

def download_opus(url):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': 'test_audio.%(ext)s',
        'noplaylist': True, # CRITICAL: Prevents downloading the whole playlist
        'extractor_args': {
            'youtube': ['player_client=android'] # Bypasses the 403 Forbidden error
        },
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'opus',
            'preferredquality': '160',
        }],
        'quiet': False
    }

    print(f"Downloading {url}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    print("Download complete! File saved as test_audio.opus")

if __name__ == "__main__":
    download_opus(TEST_URL)