import requests
import os
from datetime import datetime
from dotenv import load_dotenv,find_dotenv

load_dotenv(find_dotenv()) 

# --- CONFIGURATION ---
YT_API_KEY = os.getenv("YT_API_KEY")  # Load your YouTube API key from environment variable
PLAYLIST_ID = os.getenv("PLAYLIST_ID")  # Replace with your playlist ID

def get_playlist_items(api_key, playlist_id):
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet",
        "playlistId": playlist_id,
        "maxResults": 5, # Just 5 for testing
        "key": api_key
    }
    
    response = requests.get(url, params=params).json()
    
    if "error" in response:
        print("API Error:", response["error"]["message"])
        return

    for item in response.get("items", []):
        title = item["snippet"]["title"]
        video_id = item["snippet"]["resourceId"]["videoId"]
        raw_date = item["snippet"]["publishedAt"] # ISO 8601 format
        
        # Convert to DD-MM-YYYY
        dt_obj = datetime.strptime(raw_date, "%Y-%m-%dT%H:%M:%SZ")
        formatted_date = dt_obj.strftime("%d-%m-%Y")
        
        print(f"[{formatted_date}] {title} (URL: https://youtu.be/{video_id})")

if __name__ == "__main__":
    get_playlist_items(YT_API_KEY, PLAYLIST_ID)

