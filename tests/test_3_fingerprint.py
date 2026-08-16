import acoustid
import musicbrainzngs
import os
from dotenv import load_dotenv, find_dotenv

# --- CONFIGURATION ---
load_dotenv(find_dotenv())
ACOUSTID_API_KEY = os.getenv("ACOUSTID_API_KEY")  # Load your AcoustID API key from environment variable
FILE_PATH = "test_audio.opus"

# Setup MusicBrainz
musicbrainzngs.set_useragent("MyMusicPipeline", "1.0", "your_email@example.com")

def fingerprint_audio(file_path):
    print(f"Fingerprinting {file_path}...")
    try:
        # Generate fingerprint and match with AcoustID
        results = acoustid.match(ACOUSTID_API_KEY, file_path)
        
        for score, recording_id, title, artist in results:
            print(f"Found Match! Confidence: {score*100:.1f}%")
            print(f"AcoustID/MusicBrainz ID: {recording_id}")
            print(f"Artist: {artist} | Title: {title}")
            
            # Fetch rich metadata from MusicBrainz using the ID
            mb_data = musicbrainzngs.get_recording_by_id(recording_id, includes=["artists", "releases"])
            print(f"Official MusicBrainz Match: {mb_data['recording']['title']}")
            return mb_data['recording']
            
    except acoustid.NoBackendError:
        print("Error: fpcalc not found! Please install chromaprint/fpcalc on your OS.")
    except acoustid.WebServiceError as e:
        print(f"AcoustID API Error: {e}")
        
    print("No official match found.")
    return None

if __name__ == "__main__":
    fingerprint_audio(FILE_PATH)