import os
import sqlite3
from mutagen.oggopus import OggOpus

NAVIDROME_DIR = "./navidrome_library"
DB_FILE = "library.db"

def scan_and_heal():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    for root, _, files in os.walk(NAVIDROME_DIR):
        for file in files:
            if file.endswith(".opus"):
                full_path = os.path.join(root, file)
                try:
                    audio = OggOpus(full_path)
                    # Extract our hidden ID
                    track_uuid = audio.get("NAVIDROME_PIPELINE_ID", [None])[0]
                    if track_uuid:
                        c.execute("UPDATE tracks SET file_path=? WHERE track_uuid=?", (full_path, track_uuid))
                except Exception as e:
                    print(f"Failed to read {file}: {e}")
                    
    conn.commit()
    conn.close()
    print("Library scanned and database paths healed.")

if __name__ == "__main__":
    scan_and_heal()