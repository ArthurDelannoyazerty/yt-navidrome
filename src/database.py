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

# Append these functions to src/database.py

def get_completed_playlist_tracks(playlist_name: str, user_id: str):
    """Fetches all completed tracks for a specific playlist in insertion/discovery order."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT title, file_path 
        FROM tracks 
        WHERE playlist_name = ? AND user_id = ? AND status = 'COMPLETED' AND file_path IS NOT NULL
        ORDER BY rowid ASC
    ''', (playlist_name, user_id))
    rows = c.fetchall()
    conn.close()
    return [{"title": r[0], "file_path": r[1]} for r in rows]

def get_user_playlists(user_id: str):
    """Returns a list of all distinct playlist names for a user."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        SELECT DISTINCT playlist_name 
        FROM tracks 
        WHERE user_id = ? AND playlist_name IS NOT NULL
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

# Append these functions to src/database.py

def get_dashboard_tracks(user_id: str = None, limit: int = 100):
    """Fetches the latest tracks from the database."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    if user_id:
        c.execute('''
            SELECT track_uuid, source_url, title, playlist_name, discovery_date, status, error_msg, file_path, user_id
            FROM tracks WHERE user_id = ?
            ORDER BY rowid DESC LIMIT ?
        ''', (user_id, limit))
    else:
        c.execute('''
            SELECT track_uuid, source_url, title, playlist_name, discovery_date, status, error_msg, file_path, user_id
            FROM tracks
            ORDER BY rowid DESC LIMIT ?
        ''', (limit,))
        
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

def get_track_by_uuid(track_uuid: str):
    """Fetches a single track's data."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM tracks WHERE track_uuid = ?', (track_uuid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_failed_tracks(user_id: str = None):
    """Fetches all tracks that failed or were blocked by bot checks."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    if user_id:
        c.execute("SELECT * FROM tracks WHERE user_id = ? AND status IN ('FAILED', 'BOT_BLOCKED')", (user_id,))
    else:
        c.execute("SELECT * FROM tracks WHERE status IN ('FAILED', 'BOT_BLOCKED')")
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return rows

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