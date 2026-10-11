# Spotify sources

Spotify is a **metadata source**, not an audio downloader. Import Spotify tracks,
playlists and albums, preserve their playlist addition history, and link them to
confirmed audio in the same Music Ingestor library. Audio is never extracted from
Spotify, and the application does not modify your Spotify account or playlists.

## One-time setup

Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
For a Development Mode app, check the current Premium/allowlist requirements for
the app owner and each connecting user. Add the exact callback URL to the app:

```text
https://YOUR-INGESTOR-HOST/api/spotify/callback
```

Set these values in your server's `.env`, then recreate the container:

```dotenv
SPOTIFY_CLIENT_ID=your_spotify_app_client_id
SPOTIFY_REDIRECT_URI=https://YOUR-INGESTOR-HOST/api/spotify/callback
```

For local development use `http://127.0.0.1:8008/api/spotify/callback` (or an
explicit IPv6 loopback). Spotify does not allow `localhost` callbacks. Remote
callbacks require HTTPS. Open Ingestor at the callback's host before connecting;
the callback and connection request must use the same browser and origin.

In **Sources**, select the intended library user, then **Connect Spotify**. The
PKCE flow needs no client secret. It requests only `playlist-read-private` and
`playlist-read-collaborative`, not playback, library modification, or email access.
Every Ingestor library connects separately. A different Spotify account requires
an explicit disconnect first, preventing accidental account replacement.

Credentials and one-use login attempts are kept under `state/spotify/`, with
0700 directories and 0600 files. Access/refresh tokens are never returned to the
browser or stored in application events. They are not encrypted at rest: protect
the state volume and backups. Disconnect deletes local credentials and pending
logins but retains imported metadata and audio. Revoke the app in Spotify's
account settings as well when revocation is desired.

The admin UI still requires your authenticated reverse proxy; library user
names are organization, **not authentication**. Keep the OAuth callback protected
by that proxy, and avoid logging its query parameters (short-lived OAuth codes).

## Supported sources and access restrictions

Paste one or more full `https://open.spotify.com/track/...`, `/playlist/...`, or
`/album/...` URLs in **Add music**. Spotify `spotify:track:...`,
`spotify:playlist:...`, and `spotify:album:...` URIs and localized `intl-...` URLs
are accepted. Sharing parameters are removed during canonicalization.

**Browse my playlists** supports selection across pages, up to 100 playlists per
import, with optional monitoring. Spotify may list a followed playlist but deny
its contents: the current playlist-items endpoint only permits playlists the
connected account owns or collaborates on. A denial is an actionable source
error, never interpreted as an empty playlist. Open Spotify short links first
and paste the resulting full URL. Artist pages, podcasts, local files, mixes,
and Spotify audio downloading are not supported.

The provider uses `/playlists/{id}/items` and the newer `item` field, while
accepting the legacy `track` field in responses. It does not depend on removed
popularity, market, or audio-features fields, nor on removed batch-fetch APIs.
The stable `account_id` field is used when supplied, with legacy `id` fallback.

## Link audio without duplicating your library

After synchronization, unlinked entries appear under **Spotify: needs audio
link**. This is an expected metadata-only state, not a corrupt/missing file.
Spotify entries do not launch downloads on their own.

**Review library matches** suggests existing, available MusicBrainz-confirmed
recordings whose artist/title text matches. Only Unicode normalization, case and
whitespace are normalized; live/remaster/edition labels and punctuation are not
removed. **Link unique matches** requires confirmation and acts across all
preview pages. Ambiguous names are excluded. Even exact names are not proof of
identity: review editions before accepting a batch. Preview changes invalidate
the batch confirmation rather than silently selecting different recordings.

For each unmatched entry, **Link audio** offers two routes:

* Search the same user's existing library and explicitly confirm a recording.
  This reuses its file, MusicBrainz ID and asset provenance. Spotify becomes an
  additional metadata origin; its memberships and earlier discovery date are
  attached to that recording. No audio download or re-encoding occurs.
* Supply one YouTube video URL you are authorized to download. A separate
  YouTube source goes through the normal download, identification, approval,
  tagging and loudness pipeline. The Spotify link waits while this happens.
  Approve the YouTube track when requested. If its confirmed artist/title differs
  from Spotify, the link fails for review rather than accepting it silently; use
  the existing-library search to confirm the correct version explicitly.

`YT_API_KEY` is required for the YouTube route, not for Spotify metadata syncing.
This release deliberately does not search YouTube and automatically select an
unverified recording for each Spotify entry. **Cancel waiting link** cancels only
the waiting metadata operation, not an independently queued audio import.
**Retry all failed** also retries failed Spotify links with their original job
payload; deferred links already retry automatically.

## Synchronization and recovery guarantees

Each complete snapshot is committed atomically. Failed pagination, changed
playlist revisions, unsafe/repeated `next` URLs, or permission errors preserve the
previous memberships and local playlist. The provider constructs its own API
URLs: access tokens are not forwarded to pagination URLs supplied by responses.
Requests share the existing host pacing and Retry-After/backoff behavior.

Playlists and albums are paginated in batches of 50, with a 10,000-item safety
bound. Duplicate track occurrences preserve their separate positions. Null,
local and podcast items are skipped with a summary warning. Null playlist
`added_at` remains unknown; first observation is used only as a fallback.
Direct tracks and albums do not manufacture playlist-add or release dates.
Only MusicBrainz/beets supplies the audio release tags.

Metadata links use durable receipts and reserve the target while discovery tags
and playlists are updated. A restart resumes finalization without another audio
download. The same Spotify origin in several sources or subsequent syncs stays
attached to the same per-user logical recording. Deleting a Spotify source
removes its exported playlist, not the shared audio. Delete and ignore continues
to exclude known Spotify origins and, for linked tracks, confirmed recordings.

No database schema change is required (schema v2 is retained). Existing YouTube
sources, download options and matching thresholds are unchanged. Restart/rebuild
is required for this application code; the nightly yt-dlp updater does not update
Music Ingestor itself.

## Primary API references

Verified against Spotify's documentation, including the 2026 changes:

- [Authorization Code with PKCE](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)
- [Redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [Token refresh](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens)
- [Playlist items and access requirements](https://developer.spotify.com/documentation/web-api/reference/get-playlists-items)
- [February 2026 migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)
- [May 2026 stable account IDs](https://developer.spotify.com/documentation/web-api/references/changes/may-2026)
- [July 2026 developer-account quota changes](https://developer.spotify.com/documentation/web-api/references/changes/july-2026)
