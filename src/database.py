import sqlite3
import uuid

DB_FILE = "library.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT)''')
    c.execute("INSERT OR IGNORE INTO users (id, username) VALUES ('1', 'admin')")
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS tracks (
            track_uuid TEXT PRIMARY KEY,
            source_url TEXT UNIQUE,
            title TEXT,
            playlist_name TEXT,
            discovery_date TEXT,
            status TEXT,          -- PENDING, DOWNLOADING, COMPLETED, FAILED, BOT_BLOCKED
            error_msg TEXT,
            file_path TEXT,
            user_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_track_to_queue(item: dict, user_id: str):
    """Adds a resolved track dictionary to SQLite."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    track_uuid = str(uuid.uuid4())
    try:
        c.execute('''
            INSERT INTO tracks (track_uuid, source_url, title, playlist_name, discovery_date, status, user_id) 
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
        ''', (track_uuid, item["url"], item["title"], item.get("playlist_name"), item.get("discovery_date"), user_id))
        conn.commit()
        return track_uuid
    except sqlite3.IntegrityError:
        return None  # URL already exists in database
    finally:
        conn.close()

def update_track_status(track_uuid, status, file_path=None, error_msg=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE tracks SET status=?, file_path=?, error_msg=? WHERE track_uuid=?", 
              (status, file_path, error_msg, track_uuid))
    conn.commit()
    conn.close()