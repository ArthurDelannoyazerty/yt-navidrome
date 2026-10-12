# Correct a misidentified recording

## Two different operations, now explicit

**Keep current applied metadata** keeps the existing identity. It is not an undo.
**Restore source metadata** removes the applied MusicBrainz identity and restores
only the descriptive values retained from the audio source. The approval list
contains separate choices for both; the source choice has no MusicBrainz ID.

For an available track, open **More > Restore source metadata**. The dialog shows
exactly which values can be restored before confirmation. This operates on a
staged copy of the current audio: it does not redownload or re-encode it. Stale
MusicBrainz IDs, artist credits, album/date, artwork and lyrics from the bad match
are cleared, then retained source values are applied. Discovery comments and
measured loudness are written again. The previous confirmed identity remains in
identity history and the old asset remains active until successful activation.

For a track awaiting approval, the same action applies to its **staged candidate**,
not an old file that may still be playing. The candidate's hash is checked. Missing
or changed candidates must be reviewed again; the new selection is not silently
applied to a different download.

### What “original” can and cannot mean

From this version, the first pristine downloaded candidate's allowlisted tags are
archived before beets changes them, independently of the applied metadata. This
archive is not overwritten by reprocessing. It contains descriptive title, artist,
album, track/disc, date and genre tags, not cookies, credentials or full provider
responses. Unknown fields are not filled from the wrong confirmed recording.

Old imports did not have this archive. For those files, the dialog explicitly
shows **retained provider title only**. It derives artist/title only from an
explicit `Artist - Title` string. It does not pretend to recover historic embedded
album/date/artist values that were never saved. Refreshing a source can change its
retained provider title; that is not proof of the title at the original download.
No audio is downloaded solely to reconstruct metadata.

When one logical recording has multiple origins, restoring one source can split
it away rather than clear other origins' confirmed identity. A remaining origin
may need ingestion to supply its own audio. Choosing an already-present confirmed
MusicBrainz recording can merge the selected origin instead. The confirmation
mentions this and the existing receipt/recovery logic remains in use.

## Search MusicBrainz without copying IDs

1. Click **Search MusicBrainz** on the music row. The initial fields come from the
   retained source, not the potentially wrong current identification.
2. Search **Recordings** with a song title, artist, or both. Remove video-only
   decorations such as `Official Video` if there are no results.
3. Alternatively choose **Artists**, find the artist, then **Show recordings**.
   This searches by the selected artist's MusicBrainz ID and clears the previous
   title restriction. Results are paginated (20/page, up to 100 pages); narrow the
   title for a large catalogue.
4. Check title, credited artists, duration, date and disambiguation (live/remix/etc.),
   click **Select recording**, optionally choose a returned release, then
   **Apply selected recording**. A final confirmation previews the old/new title.
   No search result is applied automatically, and no manual ID copying is needed.

The browser sends IDs and a revision token, not trusted metadata tags. The worker
resolves the recording again using the pinned beets adapter, validates an explicit
release against the recording, and uses the existing safe activation pipeline.
An available track is corrected without a new audio download. Stale dialogs and
cross-library actions are rejected. The server remains a local administration
interface, not an authentication boundary; retain the authenticated reverse proxy.

### API pacing

Typing waits **500 ms** before searching. This is a debounce, not permission to
send two MusicBrainz requests per second. Search, metadata lookup and ingestion
share the persistent `musicbrainz.org` clock: actual attempts are spaced by at
least **1.1 seconds** in this application. Requests recheck cooldown after waiting,
and long waits return a `Retry-After` response rather than block indefinitely.
Identical in-flight searches coalesce; public results are cached for two minutes
with a bounded cache and at most four queued distinct searches. Closing/changing
inputs invalidates late browser replies. A cancelled browser request can already
have reached the server, so server-side pacing still applies.

No MusicBrainz account/API key is needed for these public read-only searches.
Other applications sharing the same external IP must also obey the service limit.
A descriptive User-Agent is sent. Existing provider retries and cooldowns remain.

## Why a recording can exist on the website but not be suggested

Automatic identification is not a complete browser-style search of every
recording. beets' `chroma` plugin contributes fingerprint candidates by resolving
AcoustID results to MusicBrainz recordings. Existence in MusicBrainz does not prove
that the downloaded audio has a usable/correct AcoustID association. Text-based
candidate generation also depends on the title/artist actually supplied; a video
upload title is not always a clean song title or recording edition. A high
text-search relevance score is **not** an audio-match confidence percentage.

In the previous application, the last “original” approval choice reused the
current MBID and the bridge reused the existing beets item. It could therefore
keep an incorrect identity instead of restoring the source. The automatic search
also seeded from the canonical track title, not necessarily the selected origin's
title. This version uses the selected origin and discards embedded MBID/AcoustID
hints in memory when starting a fresh identification. Choosing a new identity
reloads a cleaned candidate so old descriptive tags do not leak back from beets.
Native matching thresholds are not weakened and bulk approval is not treated as
additional evidence of correctness.

The dialog's **Why was this recording not recognized?** section shows the last
saved identification inputs, prior identity, recommendation, candidate count,
fingerprint availability and top suggestions. Reports are retained per origin
and bounded, independently of staging cleanup. They are evidence, not an invented
root-cause diagnosis. Old imports have no reconstructed report: reprocessing
records evidence from the next attempt. Without the exact recording/source and
that evidence, a specific failed recognition cannot be attributed conclusively
to title mismatch, missing fingerprint linkage, a different edition or another
cause. Use manual search to correct the known identity rather than repeatedly
approving the same incorrect automatic candidate.

## Compatibility and verification

Two additive tables retain source snapshots and the latest identification report.
Schema stays v2 and no tracks, sources or audio are rebuilt. Back up the state
volume and music before any deployment. Apply only after reviewing the PR and
rebuild/recreate the application container; updating yt-dlp alone is insufficient.
Rescan Navidrome after corrections that rename files or change tags.

Tests cover raw audio-packet preservation, restoration and history, source-only
fallback, shared-origin splitting, pending approval bindings, stale revisions,
server-side MB lookups, query escaping, cache/coalescing, shared rate limits,
Retry-After, cross-user isolation, debounce, late replies and explicit selection.
Live external availability and a particular user's failed recognition are not
proved by mocked API tests.

## Primary references

- [MusicBrainz API: rate limits, identification, public reads](https://musicbrainz.org/doc/MusicBrainz_API)
- [MusicBrainz search: recording/artist fields, paging and literal queries](https://musicbrainz.org/doc/MusicBrainz_API/Search)
- [beets Chromaprint/AcoustID: how fingerprint candidates are resolved](https://docs.beets.io/en/stable/plugins/chroma.html)
- [MusicBrainz AcoustID: recording associations and missing matches](https://musicbrainz.org/doc/AcoustID)
- [Pinned beets Item read/clear behavior](https://github.com/beetbox/beets/blob/v2.14.1/beets/library/models.py)
