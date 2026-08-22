import os
import re
import database

NAVIDROME_LIB_DIR = os.getenv("NAVIDROME_LIB_DIR", "./navidrome_library")

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", str(name)).strip()

def sync_playlist_file(playlist_name: str, user_id: str):
    """
    Generates or updates an #EXTM3U playlist inside the user's folder
    pointing relatively to all completed tracks.
    """
    if not playlist_name:
        return None

    tracks = database.get_completed_playlist_tracks(playlist_name, user_id)
    if not tracks:
        return None

    user_dir = os.path.join(NAVIDROME_LIB_DIR, sanitize_filename(user_id))
    os.makedirs(user_dir, exist_ok=True)

    safe_name = sanitize_filename(playlist_name)
    m3u_file_path = os.path.join(user_dir, f"{safe_name}.m3u")

    with open(m3u_file_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for track in tracks:
            full_path = track["file_path"]
            if os.path.exists(full_path):
                # Calculate the relative path from user_dir (e.g. Artist/Album/Song.opus)
                rel_path = os.path.relpath(full_path, start=user_dir)
                # Ensure forward slashes for cross-platform compatibility in Navidrome
                formatted_rel_path = rel_path.replace("\\", "/")
                
                title = track["title"] or "Unknown Title"
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(f"{formatted_rel_path}\n")

    return m3u_file_path