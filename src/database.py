import os
import re
import sqlite3
import uuid

DB_FILE = os.getenv("LIBRARY_DB", "library.db")


def _conn():
    """WAL mode + busy timeout: safe under concurrent workers."""
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = _conn()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT UNIQUE)''')
    c.execute("INSERT OR IGNORE INTO users (id, username) VALUES ('admin', 'admin')")
    c.execute("INSERT OR IGNORE INTO users (id, username) VALUES ('guest', 'guest')")

    # Per-user isolation: same song can exist for two different users.
    c.execute('''
        CREATE TABLE IF NOT EXISTS tracks (
            track_uuid TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            title TEXT,
            playlist_name TEXT,
            discovery_date TEXT,
            status TEXT DEFAULT 'PENDING',
            error_msg TEXT,
            file_path TEXT,
            metadata_choices TEXT,
            user_id TEXT NOT NULL DEFAULT 'admin',
            matched_title TEXT,
            mbid TEXT,
            UNIQUE(user_id, source_url)
        )
    ''')

    # A song living in two playlists of the same user => both .m3u files include it.
    c.execute('''
        CREATE TABLE IF NOT EXISTS playlist_memberships (
            track_uuid TEXT NOT NULL REFERENCES tracks(track_uuid) ON DELETE CASCADE,
            playlist_name TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (track_uuid, playlist_name, user_id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS monitored_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            url TEXT NOT NULL,
            label TEXT,
            UNIQUE(user_id, url)
        )
    ''')

    c.execute("CREATE INDEX IF NOT EXISTS idx_tracks_user_status ON tracks(user_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pm_playlist ON playlist_memberships(user_id, playlist_name)")

    conn.commit()
    conn.close()

# ---------- USERS ----------

def get_users():
    conn = _conn()
    rows = conn.execute("SELECT username FROM users ORDER BY username ASC").fetchall()
    conn.close()
    return [r[0] for r in rows]

def add_user(username: str):
    conn = _conn()
    clean = re.sub(r'[\\/*?:"<>|]', "", str(username)).strip()
    if clean:
        conn.execute("INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", (clean, clean))
        conn.commit()
    conn.close()
    return clean

# ---------- QUEUE ----------

def add_track_to_queue(item: dict, user_id: str):
    """Inserts track + playlist membership. Returns uuid, or None if (user, url) already known."""
    conn = _conn()
    track_uuid = str(uuid.uuid4())
    try:
        conn.execute('''
            INSERT INTO tracks (track_uuid, source_url, title, playlist_name, discovery_date, status, user_id)
            VALUES (?, ?, ?, ?, ?, 'PENDING', ?)
        ''', (track_uuid, item["url"], item.get("title"),
              item.get("playlist_name"), item.get("discovery_date"), user_id))
        if item.get("playlist_name"):
            conn.execute('''
                INSERT OR IGNORE INTO playlist_memberships (track_uuid, playlist_name, user_id)
                VALUES (?, ?, ?)
            ''', (track_uuid, item["playlist_name"], user_id))
        conn.commit()
        return track_uuid
    except sqlite3.IntegrityError:
        # Known URL for this user: still record the playlist membership if new.
        if item.get("playlist_name"):
            try:
                conn.execute('''
                    INSERT OR IGNORE INTO playlist_memberships (track_uuid, playlist_name, user_id)
                    VALUES ((SELECT track_uuid FROM tracks WHERE user_id=? AND source_url=?), ?, ?)
                ''', (user_id, item["url"], item["playlist_name"], user_id))
                conn.commit()
            except sqlite3.IntegrityError:
                pass
        return None
    finally:
        conn.close()


# ---------- STATUS ENGINE (atomic claims prevent double-dispatch races) ----------

def claim_track(track_uuid: str, from_statuses: list, to_status: str, error_msg: str = None) -> bool:
    """Atomically transitions a track ONLY if it's currently in one of from_statuses."""
    conn = _conn()
    ph = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f'''UPDATE tracks SET status=?, error_msg=COALESCE(?, error_msg)
            WHERE track_uuid=? AND status IN ({ph})''',
        (to_status, error_msg, track_uuid, *from_statuses))
    conn.commit()
    ok = cur.rowcount == 1
    conn.close()
    return ok


def update_track_status(track_uuid, status, file_path=None, error_msg=None,
                        matched_title=None, mbid=None):
    conn = _conn()
    conn.execute('''
        UPDATE tracks
        SET status=?,
            file_path=COALESCE(?, file_path),
            error_msg=?,
            matched_title=COALESCE(?, matched_title),
            mbid=COALESCE(?, mbid)
        WHERE track_uuid=?
    ''', (status, file_path, error_msg, matched_title, mbid, track_uuid))
    conn.commit()
    conn.close()


def update_track_metadata_choices(track_uuid, choices_json, temp_file_path):
    conn = _conn()
    conn.execute('''
        UPDATE tracks SET status='NEEDS_APPROVAL', metadata_choices=?, file_path=?
        WHERE track_uuid=?
    ''', (choices_json, temp_file_path, track_uuid))
    conn.commit()
    conn.close()


def reset_track_for_redownload(track_uuid: str):
    conn = _conn()
    conn.execute('''
        UPDATE tracks SET status='PENDING', error_msg=NULL, file_path=NULL,
            matched_title=NULL, mbid=NULL, metadata_choices=NULL
        WHERE track_uuid=?
    ''', (track_uuid,))
    conn.commit()
    conn.close()


def fail_interrupted() -> int:
    """Startup recovery: anything mid-flight when the server died becomes retryable."""
    conn = _conn()
    cur = conn.execute('''
        UPDATE tracks SET status='FAILED', error_msg='Interrupted by server restart'
        WHERE status IN ('PENDING', 'DOWNLOADING')
    ''')
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def update_retag_result(track_uuid, matched_title, mbid, file_path):
    conn = _conn()
    conn.execute('''
        UPDATE tracks SET matched_title=?, mbid=?, file_path=?,
            status='COMPLETED', error_msg=NULL
        WHERE track_uuid=?
    ''', (matched_title, mbid, file_path, track_uuid))
    conn.commit()
    conn.close()


# ---------- READERS ----------

def get_tracks_by_status(status: str, user_id: str = None):
    conn = _conn()
    if user_id:
        rows = conn.execute("SELECT * FROM tracks WHERE status=? AND user_id=?",
                            (status, user_id)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tracks WHERE status=?", (status,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_failed_tracks(user_id: str = None):
    conn = _conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE user_id=? AND status IN ('FAILED','BOT_BLOCKED')",
            (user_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tracks WHERE status IN ('FAILED','BOT_BLOCKED')").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_track_by_uuid(track_uuid: str):
    conn = _conn()
    row = conn.execute("SELECT * FROM tracks WHERE track_uuid=?", (track_uuid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_dashboard_tracks(user_id: str = None, limit: int = 200):
    conn = _conn()
    cols = ("track_uuid, source_url, title, playlist_name, discovery_date, status, "
            "error_msg, file_path, user_id, metadata_choices, matched_title, mbid")
    order = ("ORDER BY CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END, rowid DESC LIMIT ?")
    if user_id:
        rows = conn.execute(f"SELECT {cols} FROM tracks WHERE user_id=? {order}",
                            (user_id, limit)).fetchall()
    else:
        rows = conn.execute(f"SELECT {cols} FROM tracks {order}", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_tracks(query: str, user_id: str = None):
    conn = _conn()
    term = f"%{query.strip()}%"
    sql = '''SELECT track_uuid, source_url, title, playlist_name, discovery_date, status, file_path, user_id
             FROM tracks
             WHERE (title LIKE ? OR source_url LIKE ? OR track_uuid LIKE ?)'''
    params = [term, term, term]
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " LIMIT 10"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_completed_playlist_tracks(playlist_name: str, user_id: str):
    """All completed tracks belonging to this playlist, in true chronological order."""
    conn = _conn()
    rows = conn.execute('''
        SELECT t.title, t.file_path
        FROM tracks t
        JOIN playlist_memberships pm
            ON pm.track_uuid = t.track_uuid AND pm.user_id = t.user_id
        WHERE pm.playlist_name = ? AND pm.user_id = ?
          AND t.status = 'COMPLETED' AND t.file_path IS NOT NULL
        ORDER BY t.discovery_date ASC, t.rowid ASC
    ''', (playlist_name, user_id)).fetchall()
    conn.close()
    return [{"title": r[0], "file_path": r[1]} for r in rows]

def get_status_stats(user_id: str | None = None) -> dict:
    """True global counts via GROUP BY — correct regardless of any dashboard LIMIT."""
    conn = _conn()
    sql = "SELECT status, COUNT(*) FROM tracks"
    params: list = []
    if user_id:
        sql += " WHERE user_id=?"
        params.append(user_id)
    counts = {r[0]: r[1] for r in conn.execute(sql + " GROUP BY status", params)}
    conn.close()
    return {
        "total": sum(counts.values()),
        "completed": counts.get("COMPLETED", 0),
        "downloading": counts.get("DOWNLOADING", 0) + counts.get("PENDING", 0),
        "pending_approval": counts.get("NEEDS_APPROVAL", 0),
        "failed": counts.get("FAILED", 0) + counts.get("BOT_BLOCKED", 0),
    }

def get_user_playlists(user_id: str):
    conn = _conn()
    rows = conn.execute('''SELECT DISTINCT playlist_name FROM playlist_memberships
                           WHERE user_id = ?''', (user_id,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------- MONITORED URLS ----------

def get_monitored_urls(user_id: str):
    conn = _conn()
    rows = conn.execute("SELECT id, url, label FROM monitored_urls WHERE user_id=?",
                        (user_id,)).fetchall()
    conn.close()
    return [{"id": r[0], "url": r[1], "label": r[2]} for r in rows]


def get_all_monitored_urls():
    conn = _conn()
    rows = conn.execute("SELECT user_id, url, label FROM monitored_urls ORDER BY user_id").fetchall()
    conn.close()
    return [{"user_id": r[0], "url": r[1], "label": r[2]} for r in rows]


def add_monitored_url(user_id: str, url: str, label: str):
    conn = _conn()
    try:
        conn.execute("INSERT INTO monitored_urls (user_id, url, label) VALUES (?, ?, ?)",
                     (user_id, url, label))
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # already monitored by THIS user (other users may monitor the same URL)
    finally:
        conn.close()


def delete_monitored_url(url_id: int):
    conn = _conn()
    conn.execute("DELETE FROM monitored_urls WHERE id=?", (url_id,))
    conn.commit()
    conn.close()