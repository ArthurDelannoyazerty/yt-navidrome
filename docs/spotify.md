# Spotify sources

## Import a public playlist by URL

1. Select your library user and open **Sources**.
2. Paste a full Spotify playlist, track or album URL in **Add music**. Enable
   monitoring to keep its local playlist synchronized.
3. New tracks are queued automatically. spotDL reads Spotify metadata and finds
   a YouTube audio candidate; the existing yt-dlp downloader, beets identification,
   loudness processing, temporary publication and Approval workflow then run.
4. Review the tracks in **Approval**, using the same **Use** and original-source
   choices as YouTube imports. New audio still requires metadata approval.

Public URLs need no Spotify account connection, developer app, client ID or client
secret. Spotify is the metadata/discovery source; audio is acquired from a separate
YouTube origin. The application does not download Spotify audio streams or edit
online playlists. YouTube download options, cookies, retry budgets and its circuit
breaker continue to apply to the actual audio download.

Full `https://open.spotify.com/playlist/...`, `/track/...` and `/album/...` URLs,
their `spotify:...` URI forms and localized `intl-...` URLs are accepted. Sharing
parameters are removed. Open short links in Spotify first and paste the resulting
full URL. Artists, podcasts and local files are not import sources.

## Source filters and reuse

Both **Music** and **Approval** offer **Imported from Spotify** and **Imported from
YouTube** filters. They describe discovery provenance, not the service transporting
the audio. A Spotify import stays in the Spotify filter when spotDL finds its audio
on YouTube. If that video is also imported directly or discovered in a monitored
YouTube playlist, the logical track appears in both filters. No extra source badge
is added to music rows; **Sources & details** retains the origin URLs.

The same Spotify ID is reused across playlists, including repeated occurrences in
one playlist. Resolving a video already present in the selected library reuses its
track and audio. When approval identifies a recording already in the library,
Spotify playlist memberships follow the merged recording. A durable association
between the Spotify discovery origin and its selected audio origin prevents
playlist entries from being stranded on empty placeholders.

## Complete snapshots and dates

Only complete snapshots replace playlist memberships. The fast path reads the
ordinary public Spotify page only when its rows match an independent advertised
total and it explicitly has no next page. A second complete read must have the same
content fingerprint. This fingerprint is recorded as an observational digest,
not a Spotify-issued revision. Larger or incomplete documents fall back to the
public reader shipped with spotDL's free Spotify backend, which verifies independent
item totals, page positions and native playlist revisions before accepting the result.
Malformed, repeated, truncated or changed pages fail without replacing the last
valid snapshot or exported playlist. The scanner does not accept a Spotify embed
preview as a complete playlist. Collections are bounded to 10,000 occurrences.

Local, podcast and removed entries are skipped with a count while their original
positions remain available. Duplicate songs retain separate memberships. A real
playlist addition time is retained when provided; otherwise the date is explicitly
unknown and the stable first-observed date is used. A rescan does not manufacture
an earlier date or reset first observation. Album release dates are not discovery
dates. Beets/MusicBrainz remains responsible for approved release metadata.

The public backend depends on Spotify's web responses and can break when Spotify
changes them. A transient failure defers the job and applies a shared cooldown.
If scanning stops working, check Activity and use **Maintenance → Update spotDL**,
then retry. Existing library files and the last successful playlist snapshot stay
available. Private playlists need the optional account connection below; public
URL support does not confer private access.

## Updates and rollback

spotDL is installed in its own environment, separate from the application/beets
and yt-dlp environments. The container bundles spotDL 4.6.0. Maintenance displays
its active version, previous update outcome and next scheduled update, with
**Update spotDL** and **Roll back spotDL** controls next to the yt-dlp controls.

An update installs a fresh stable spotDL 4.x environment, checks the exact bridge
imports and an offline matching fixture, then atomically switches future work.
Active scans/searches keep their original interpreter; failed updates retain the
previous runtime. Rollback selects the previous validated runtime. The smoke
check validates compatibility, not live Spotify/YouTube service availability.

```dotenv
SPOTDL_UPDATE_ENABLED=true
SPOTDL_UPDATE_TIME=04:30
TZ=Europe/Paris
```

For development, install spotDL in a separate virtual environment and set
`SPOTDL_PYTHON` to its Python interpreter. Do not add spotDL to the application's
beets environment. Application changes still require rebuilding/restarting the
container; runtime updates only update the isolated provider packages.

## Recovery and existing imports

**Spotify: waiting for audio** finds entries that do not yet have a usable audio
origin. A failed search can be retried using the ordinary retry action. Monitoring
can queue a legacy idle Spotify entry for automatic audio lookup, but does not
override failed, stopped, deferred or approval decisions. A no-match result remains
visible as a failed operation so it can be reviewed rather than silently omitted.

**Link audio** remains available for an unlinked entry. Choose a confirmed recording
from this user's library or supply a specific YouTube video. **Review library
matches** still offers explicit, revision-checked name-match suggestions; names
alone never silently establish a recording identity. A manual link's cancellation
does not cancel a separately queued YouTube import.

Resolved audio enters the same durable ingest job. Its selected URL is saved before
download, so retries/restarts do not search for a different video after a file has
been staged. Existing approval tokens, staged-file hashes, ignore rules and source
download controls remain authoritative. Removing a monitored source removes its
local playlist export and memberships while retaining shared music.

Schema v8 adds discovery provenance and Spotify/audio associations, with a SQLite
backup before migration. Existing origins retain discovery provenance; existing
audio, identities and memberships are preserved.

## Optional account connection

An account connection enables **Browse my playlists** and an authenticated fallback
for sources the public reader cannot access. Public imports do not require it.
Create a Spotify Developer Dashboard app and register the exact callback URL:

```dotenv
SPOTIFY_CLIENT_ID=your_spotify_app_client_id
SPOTIFY_REDIRECT_URI=https://YOUR-INGESTOR-HOST/api/spotify/callback
```

For local development, use `http://127.0.0.1:8008/api/spotify/callback`; remote
callbacks require HTTPS. Open the app at the configured host, select the library
user, expand the optional connection section in **Sources**, and connect. PKCE
requires no client secret. The requested scopes are read-only playlist scopes.
Spotify developer-app eligibility, allowlisting and playlist access restrictions
still apply to this optional official API route.

Each library connects separately. Tokens remain in protected server-side files
under `state/spotify/`, never in the browser or application events. Disconnecting
removes that library's credentials and keeps imported files, origins and public
URL syncing. Protect the state volume/backups and keep the administration UI behind
authentication. Library user names organize data; they are not login credentials.

## Upstream references

- [spotDL](https://github.com/spotDL/spotify-downloader)
- [spotDL Spotify backend](https://github.com/spotDL/spotify-downloader/blob/v4.6.0/spotdl/utils/spotify.py)
- [Spotify Authorization Code with PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)
- [Spotify redirect requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [Spotify playlist items](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items)
