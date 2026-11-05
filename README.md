# Music Ingestor

Self-hosted, provider-aware music ingestion for a user-separated Navidrome library.
FastAPI, SQLite, beets 2.14.1, and an isolated yt-dlp runtime are used throughout.
The administration interface must remain behind an authenticated reverse proxy.

## Listen before approving metadata

Every new audio download is published immediately to
`<library>/<user>/Waiting for approval/<track-id>.opus`, with **Waiting for approval**
as the album and **Various Artists** as the album artist. Source artist/title,
discovery tags and measured loudness are retained. Navidrome can play it and its
exported YouTube playlists after the next scan, even while MusicBrainz is offline.
Spotify-only entries still require an explicit audio link; Spotify supplies no audio.

New imports require metadata approval, including strong automatic matches. Use
**Approve**, **Review metadata**, or the original source tags. Approval replaces
the temporary copy, updates the playlist paths, and removes the temporary file.
The original staged download stays unchanged for identification and approval
revalidation. Existing approved audio remains active during reprocessing.

On startup, candidates awaiting approval from older versions are queued for
publication without redownloading or replacing their choices. No live library
files are reclassified during the schema upgrade. Temporary publication and
playlist export can be retried after an interruption.

## Core model

The application separates four concepts:

- **logical music** — one confirmed MusicBrainz recording per user;
- **origins** — one or more provider URLs/IDs that can refer to that recording;
- **playlist memberships** — source-specific positions and exact addition timestamps;
- **assets** — the active local file and provenance/history for replacements.

The same YouTube video in several playlists for one user creates one origin, one
logical track, one download, and several memberships. Different users remain
fully separate. Once MusicBrainz identity is confirmed, a different provider
origin resolving to the same recording is attached to the existing logical track.

## Discovery and release dates

Music release dates come only from beets/MusicBrainz. Playlist names and discovery
timestamps never become album or release dates.

Discovery uses these rules:

1. if any real playlist-add timestamp exists, the earliest one wins;
2. otherwise the earliest `first_seen` timestamp is used;
3. a later playlist timestamp can never replace an earlier playlist timestamp.

For example, direct observation in 2020 followed by playlist additions in 2019
and 2021 results in discovery `2019`, permanently.

The exact source-neutral value is written to both Opus tags:

```text
COMMENT=Discovery Date: 2019-04-17--21-36-42 UTC
DESCRIPTION=Discovery Date: 2019-04-17--21-36-42 UTC
```

Writing both keys keeps MediaFile/beets and common tag viewers compatible.

## Track actions

### Redownload

Downloads fresh audio from a selected downloadable origin. It must strongly match
the currently confirmed recording. The active file remains untouched until the
replacement is downloaded, identified, tagged, moved, committed to SQLite, and
all affected playlists are rewritten. If the source is gone or the identity does
not match, the previous file remains current and playable.

### Reprocess

Downloads from a selected origin and performs the complete pipeline again:
Chromaprint/AcoustID, MusicBrainz identification, approval when needed, release
metadata, artwork, embedded lyrics, ReplayGain, discovery tags, and beets file
organization. A changed identity always requires approval. When accepted, the
previous confirmed identity is kept in `identity_history`, linked to the origin.

### Remove music and ignore rules

Open **More → Remove…** to choose a removal scope. The dialog explains each
available choice and lists the affected URLs before the action is submitted:

- **Remove from this library** deletes the local track, audio and local playlist
  memberships. Monitored sources can import it again.
- **Remove and ignore this URL** also prevents that video from being reimported.
  Another upload of the same recording remains allowed. The plural URL choice is
  shown only where the existing operation can provide that exact scope.
- **Block this recording and its URLs** also blocks that MusicBrainz recording
  throughout the selected library user, including future uploads that match it.

For a track with several origins, **Sources & details → Origins → Remove & ignore
origin** can remove one eligible inactive origin while keeping the other origins
and audio. Use **Separate track** first when an incorrectly merged origin needs
its own review. The dialog never labels a recording-wide block as URL-only.

Ignored entries are in **Music → Ignored music**, with search and pagination.
**Allow recording (keep URLs ignored)** removes a recording-wide block without
restoring its URLs. **Restore just this URL** allows one video; **Restore all**
also allows every ignored URL, including an unwanted upload.

### Stop retrying or use another video

**Stop retrying this URL** disables the exact source used by the active download.
It cancels queued/deferred work and requests cancellation of a running download.
A publication transaction already in progress finishes safely. The track,
existing audio, source history and playlist memberships remain available.

Disabled URLs stay disabled across restarts and monitored-source sync.
**Enable URL** permits future downloads without starting one. **Use another URL…**
attaches one alternative YouTube video to the same track and checks its audio
before replacement. Existing audio stays playable during review. The replacement
cannot silently change a confirmed recording identity or merge another track.

Permanent access restrictions stop that URL automatically. Transient download
failures stop after `DOWNLOAD_MAX_ATTEMPTS` attempts (default `5`). Metadata
deferrals and time spent waiting for a shared cooldown do not use that budget.
See [Source controls and retry behavior](docs/source-controls.md).

## Asset provenance and health

Each current/replaced asset records:

- provider origin and source URL;
- download timestamp;
- downloader implementation/version;
- file size, mtime, and SHA-256;
- current/replaced state.

Track health (`AVAILABLE`, `MISSING`, `UNAVAILABLE`) is separate from the latest
operation state (`QUEUED`, `RUNNING`, `DEFERRED`, `NEEDS_APPROVAL`, `FAILED`, `STOPPED`, `IDLE`). A failed
redownload therefore reports an operation error without making an intact current
asset unavailable.

The daily/manual integrity check verifies current files, hashes changed files,
checks beets membership/path agreement, and reports untracked Opus files. It does
not silently delete or repair music.

## Interface layout

The single-page interface is split into five tabs:

- **Music** — search, status filters, continuous metadata review, explicit selected
  approvals, pagination and an **Ignored music** view. Less frequent actions stay
  in **More**, including sources, repair, reprocessing, redownload and removal.
- **Sources** — URL ingestion and monitored playlists.
- **Activity** — persistent events, following the latest message by default.
  Scrolling up pauses following; **Jump to latest** resumes it. **Clear view**
  clears only the browser display.
- **Maintenance** — paginated integrity issues, optional batch tag/loudness repair,
  failed-operation export and downloader updates.
- **Help** — numbered workflow steps, separate recovery guidance and an expandable
  table of operation transitions.

Search is server-side and matches source titles/channels and identified
artist/title. Pagination follows filtering/search, with 50 rows per page and
Previous/Next controls above and below the results. Library totals stay in the
filter counters; the redundant visible results-summary line is removed.
Narrow screens use stacked track rows.

## Start with Docker Compose

```sh
cp .env.template .env
# Set YT_API_KEY and review the schedule values.
docker compose up -d --build
```

Open `http://127.0.0.1:8008`. The default Compose binding is loopback-only.
The container runs as UID/GID 1000.

Persistent layout:

```text
/data/state/
  ingestor.sqlite
  backups/
  beets/<user>/library.db
  staging/
  runtimes/
/data/library/<user>/
  <artist>/<album>/...
  000000-playlists/*.m3u
```

The committed `uv.lock` is used by both CI and Docker with `uv sync --frozen`.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `YT_API_KEY` | none | YouTube Data API v3; required |
| `SYNC_INTERVAL_HOURS` | `6` | monitored source sync; `0` disables |
| `YT_DAILY_BUDGET` | `9000` | local API budget guard |
| `PLAYLIST_ORDER` | `source` | `source` or `discovery` |
| `INTEGRITY_INTERVAL_HOURS` | `24` | automatic verification; `0` disables |
| `YT_PLAYER_CLIENTS` | empty | yt-dlp maintained defaults |
| `YT_DLP_COOKIES_FILE` | empty | optional in-container cookie path |
| `DOWNLOAD_SLEEP_MIN` | `2` | preserved downloader pacing |
| `DOWNLOAD_SLEEP_MAX` | `6` | preserved downloader pacing |
| `DOWNLOAD_MAX_ATTEMPTS` | `5` | failed download attempts before an automatic stop; excludes metadata/cooldown waits |
| `YT_CIRCUIT_FAILURES` | `3` | blocked YouTube downloads before the shared circuit opens |
| `YT_CIRCUIT_WINDOW_SECONDS` | `300` | rolling window for blocked-download detection |
| `YT_CIRCUIT_COOLDOWN_SECONDS` | `900` | initial YouTube circuit cooldown |
| `YT_CIRCUIT_MAX_COOLDOWN_SECONDS` | `3600` | maximum repeated circuit cooldown |
| `YTDLP_UPDATE_ENABLED` | `true` | isolated nightly downloader update |
| `YTDLP_UPDATE_TIME` | `04:00` | local scheduled time |
| `TZ` | `Europe/Paris` | updater timezone |

The API resolver uses `playlistItems.snippet.publishedAt`, never video upload time,
for playlist addition history. Spotify tracks, albums and owned/collaborative playlists are supported as
metadata-only sources, with per-library OAuth connection and explicit links to
separate audio origins. See [Spotify setup and workflow](docs/spotify.md).

## SQLite schema versioning

The database uses `PRAGMA user_version`. Fresh installations create schema v5.
An unversioned database containing data is refused rather than silently changed.

Existing versioned installations upgrade in place, with a consistent SQLite
backup under `state/backups/` before each versioned migration:

- v1 → v2 adds deferred-job scheduling and repairs approval candidates incorrectly
  requeued by old monitored-source synchronization when staged audio still exists.
- v2 → v3 adds the temporary-candidate digest used to validate approval audio.
- v3 → v4 retains source channel names for conservative identification hints.
- v4 → v5 adds persistent source download permissions, failure budgets and durable
  cancellation/stage fields for jobs. Existing origins start enabled.

Origin identity rejections, source snapshots, identification reports and explicit
artist aliases are retained in additive tables. Existing tracks, assets,
memberships and confirmed identities are preserved.

Future schema upgrades follow the same pattern: back up SQLite, apply an explicit
transactional migration, and increment `user_version` only after success.

## Provider deferral and YouTube circuit breaker

Transient metadata-provider failures (for example MusicBrainz 503/429 responses)
do not discard a downloaded candidate. The same durable job is marked
`DEFERRED` with exponential retry timing; when it becomes due, the staging receipt
is reused and the metadata stage resumes without another YouTube download.

YouTube playback blocks are handled separately. Several bot-verification,
HTTP 403/429, or equivalent playback failures inside the configured rolling
window open a shared circuit breaker. Pending YouTube jobs receive the same
cooldown and are skipped by the worker until it expires, while source sync,
maintenance, and other due jobs remain claimable. A successful YouTube download
resets the breaker only after the cooldown and rolling failure window have elapsed;
an unrelated success cannot erase recent blocks.

## Nightly yt-dlp updates

Updates install a new yt-dlp virtual environment under `/data/state/runtimes`,
validate imports and the actual wrapper, then atomically switch future downloads.
Running downloads keep their original runtime; FastAPI does not restart. Rollback
selects the previous runtime. Rebuild the container for application, Python, Deno,
ffmpeg, or operating-system updates.

## Development and checks

Requirements: Python 3.13, uv, Node.js, ffmpeg, and fpcalc.

```sh
uv sync --frozen --extra test
uv run python -m compileall -q src tools tests
uv run pytest
node --check src/static/app.js
node --check src/static/ui-state.js
node --check src/static/spotify-ui.js
node --check src/static/metadata-ui.js
node --test tests/frontend_runtime.test.cjs tests/spotify_frontend.test.cjs tests/metadata_frontend.test.cjs
# Requires Chrome/Chromium; set CHROME_PATH if it is not found automatically.
REQUIRE_BROWSER=1 node --test tests/browser_ui.test.cjs
```

CI runs these checks and builds the Docker image for every pull request. Browser
checks exercise desktop and narrow-screen interactions and retain screenshots in
the validation artifact. The container intentionally uses one
Uvicorn worker because the durable SQLite job worker is single-instance.

## Correct an accidentally merged YouTube origin

A confirmed recording can have several YouTube origins, but a full-album upload
might have been incorrectly approved as the same song. In **Music → More →
Sources & details → Origins**, choose the *specific incorrect URL*:

- **Separate track** creates a separate logical track without copying the existing
  recording's audio or MusicBrainz identity. Its playlist memberships follow the
  new row, and its own audio is queued for download into **Waiting for approval**.
  The rejected identity is excluded from subsequent automatic suggestions, so a
  sync or reprocess cannot silently undo the separation. An explicit MusicBrainz
  search can still select that identity if the separation was a mistake.
- **Remove & ignore origin** removes only that URL from locally generated
  playlists, retains the good track/audio/remaining origins, and records an
  origin-only ignore rule so monitored sync does not re-add it. It is reversible
  in **Music → Ignored music → Restore**.

Both operations require at least two origins, an idle track, and selecting an
origin that **does not supply the currently active audio**. If the wrong video is
the active audio source, first **Reprocess** using the correct video's URL and
approve it, then detach/ignore the wrong one. Existing audio is never blindly
assigned to another URL. If discovery dates change, a normal durable comment
job updates the existing file and fingerprint. Local playlist exports refresh.
**These actions never edit your YouTube playlist online**; remove the video on
YouTube separately if you also want the remote playlist cleaned.

## Managing a large library

Choose a Music filter and search, then **Review this view**. The dialog keeps a
stable queue and puts original source values, actual current tags, the selected
change and audio playback together. **Save & next**, **Back** and **Skip** avoid
reopening each row. **Review skipped tracks** makes another pass over undecided
items. The latest queue for each library user resumes in the same browser.

Use Alt + Enter to save and advance, Alt + Right to skip and Alt + Left to go back.
**A–Z (artist / title)** sorts the stored display title; it can help group related
entries. Queue membership is a snapshot of the current filter/search/order, with
fresh metadata and approval tokens loaded as each track opens.

For explicit batch approval, select waiting rows after checking their displayed
choices, then **Approve selected**. Selection can span pages in the same view.
The server receives those exact track IDs, candidate indexes and approval tokens;
changed candidates are skipped for another review. Library-wide approval remains
under **Advanced library actions** with its scope explained.

Counters describe the **whole selected library user**. Search and status filters
affect the table. Health and operation states overlap: playable audio can coexist
with a failed or stopped download. **Attention** includes missing audio and failed
or stopped operations.

**Retry all failed** queues FAILED tracks for the selected user across all pages
and searches. Stopped/disabled sources need an explicit source decision; they are
not silently restarted by that bulk action. Original recovery receipts and
provider pacing still apply.

Maintenance includes failed-operation export and paginated/filterable audit
results. Repair works only on eligible, audited idle files and never approves
identities or downloads audio. Hash mismatches, unreadable files and paths outside
the library are excluded. Individual repair remains in the track's More menu.

See [Continuous review and selected approvals](docs/continuous-review.md) and
[Metadata matching evidence and artist aliases](docs/metadata-review.md).

## Playback loudness and discovery tags

See [Playback and maintenance](docs/playback-maintenance.md) for rollout and
client configuration. Opus audio is not re-encoded by tag repair. The pipeline
measures fresh loudness and true peak using beets' ffmpeg backend and writes
standard -23 LUFS R128 gain plus a peak-only compatibility tag for Navidrome.
`auto: false` avoids duplicate beets import hooks: the bridge invokes processing
explicitly. Do not enable extra plugins merely to get these steps to run.

Track details reads COMMENT, DESCRIPTION and loudness fields from the **actual
current file**, alongside the expected discovery value. Navidrome and Feishin
column support is client/version dependent; the ingestor does not change their UI.

## Reliability boundaries

Approval is bound to the staged candidate hash. Missing or changed candidates,
including legacy approvals without a hash, require another review. Deduplication
only discards a new candidate when the canonical asset exists and its hash agrees.
Successful discovery-tag updates refresh the asset fingerprint.

Deletion commits logical removal, optional ignore rules and a cleanup receipt in
one transaction before changing files. A cleanup failure is deferred and resumes
after restart even when the track row is already gone. Source resync can recreate
a normally deleted track; Delete and ignore prevents that. Migrations acquire the
instance lock first, back up SQLite, and apply v1 changes and version atomically.

CI retains the exact source snapshot, Python JUnit results and browser-state test
output as validation artifacts. The Python integration suite exercises real pinned
beets and ffmpeg offline; it does not prove live external providers are available.

## Spotify sources

See [Spotify setup and workflow](docs/spotify.md). Configure `SPOTIFY_CLIENT_ID`
and `SPOTIFY_REDIRECT_URI`, then connect the selected library from Sources.
Paste Spotify track/playlist/album URLs or browse and select your playlists.
Spotify imports metadata and exact playlist addition times, not audio streams.

Review matching library recordings in bulk, or use **Link audio** to choose an
existing confirmed track or a separate YouTube video. Unlinked entries have a
**Spotify: needs audio link** filter; they are not marked as integrity failures.
Automatic unreviewed Spotify-to-YouTube searching is intentionally not performed.
Tokens remain in protected server-side files; no client secret is needed.

## Correcting misidentified music

Open **Review metadata** and choose **Suggested matches**, **Search MusicBrainz**,
**Original source** or **Edit manually**. Source restoration has one entry point
inside this dialog; **More → Edit metadata** opens its manual pane.

Search starts from conservative source hints. Native Japanese names remain intact;
query normalization removes video decorations without discarding version clues.
An explicitly confirmed artist alias is remembered for the selected library and
uses the MusicBrainz artist ID in later searches. **Wrong recording** remembers a
rejection for that source, with a way to allow the suggestion again.

Automatic match scores, search relevance, duration differences and fingerprint
evidence are shown as separate signals. They are not calibrated probabilities,
and the automatic matching thresholds have not been lowered.

**Original source** clears an incorrect MusicBrainz identity and restores only
retained source values. It remains distinct from keeping current applied tags.
Older imports without archived original tags explicitly fall back to retained
provider metadata. Available audio can be corrected or used to refresh suggestions
without downloading it again; pending replacement audio must finish its own review.

MusicBrainz search waits 500 ms after typing and shares the server's 1.1-second
provider limiter. The existing audio stays active until a successful replacement.

See [Metadata correction and recognition diagnostics](docs/metadata-corrections.md)
for provenance, identity changes, API limits and why a recording on MusicBrainz
may not appear as an automatic fingerprint candidate.

