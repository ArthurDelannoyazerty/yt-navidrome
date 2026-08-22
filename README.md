



uv pip install --upgrade https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz


for the yt api keys, go to https://console.cloud.google.com , create a project, add the youtube api then create the api key with the yt api v3 selected.



nix-shell -p deno ffmpeg chromaprint uv --run 'uv run --with yt-dlp --with pyacoustid --with musicbrainzngs --with mutagen --with requests --with python-dotenv python main.py'

