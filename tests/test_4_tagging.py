from mutagen.oggopus import OggOpus

FILE_PATH = "test_audio.opus"

def tag_opus_file(file_path):
    print(f"Tagging {file_path}...")
    
    # Load the opus file
    audio = OggOpus(file_path)
    
    # Clear existing messy YouTube tags if needed
    audio.delete()
    
    # Standard Tags
    audio["title"] = "Never Gonna Give You Up"
    audio["artist"] = "Rick Astley"
    audio["album"] = "Whenever You Need Somebody"
    
    # Order in your 10-year Playlist (Navidrome reads this to sort)
    audio["tracknumber"] = "0001"
    audio["grouping"] = "My10YearPlaylist" # Custom way to group playlists
    
    # Your Custom Pipeline Tags
    audio["yt_date_added"] = "16-08-2026"
    audio["mbid_status"] = "Yes" # Or "No" if fingerprinting failed
    audio["musicbrainz_trackid"] = "8b528c12-32a2-4a0f-90e6-5b4d7945d8b2" # Example MBID
    
    # Save tags to file
    audio.save()
    print("Tags successfully injected!")
    
    # Verify by reading them back
    verify = OggOpus(file_path)
    print("\nVerified Tags in File:")
    for key, value in verify.items():
        print(f" - {key}: {value[0]}")

if __name__ == "__main__":
    tag_opus_file(FILE_PATH)