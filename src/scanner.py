import os
import sqlite3
from mutagen.oggopus import OggOpus

NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")
DB_FILE = os.getenv("LIBRARY_DB", "library.db")


def scan_and_heal():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.execute("PRAGMA busy_timeout=5000")
    c = conn.cursor()
    healed = 0
    for root, _, files in os.walk(NAVIDROME_LIB_DIR):
        for file in files:
            if not file.endswith(".opus"):
                continue
            full_path = os.path.join(root, file)
            try:
                track_uuid = OggOpus(full_path).get("NAVIDROME_PIPELINE_ID", [None])[0]
                if track_uuid:
                    c.execute("UPDATE tracks SET file_path=? WHERE track_uuid=?",
                              (full_path, track_uuid))
                    healed += 1
            except Exception as e:
                print(f"Failed to read {file}: {e}")
    conn.commit()
    conn.close()
    print(f"Library scanned: {healed} path(s) verified/healed.")


if __name__ == "__main__":
    scan_and_heal()