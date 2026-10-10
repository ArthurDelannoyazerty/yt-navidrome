# Music Ingestor

Self-hosted, provider-aware music ingestion for a user-separated Navidrome library.
FastAPI, SQLite, beets 2.14.1, and an isolated yt-dlp runtime are used throughout.
The administration interface must remain behind an authenticated reverse proxy.

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

### Delete / Delete and ignore

`Delete local copy` removes the selected user's file, beets item, memberships,
and logical track. A monitored source can add it again.

`Delete and ignore` additionally creates a tombstone for the confirmed recording
and every known origin. Known origins are skipped during later synchronization;
a new origin that resolves to the same ignored MusicBrainz recording is also
rejected. Ignored music can be restored from Maintenance.

## Asset provenance and health

Each current/replaced asset records:

- provider origin and source URL;
- download timestamp;
- downloader implementation/version;
- file size, mtime, and SHA-256;
- current/replaced state.

Track health (`AVAILABLE`, `MISSING`, `UNAVAILABLE`) is separate from the latest
operation state (`QUEUED`, `RUNNING`, `DEFERRED`, `NEEDS_APPROVAL`, `FAILED`, `IDLE`). A failed
redownload therefore reports an operation error without making an intact current
asset unavailable.

The daily/manual integrity check verifies current files, hashes changed files,
checks beets membership/path agreement, and reports untracked Opus files. It does
not silently delete or repair music.

## Interface layout

The single-page interface is split into five tabs:

- **Music** — search, status filter, pagination, health, Reprocess/Redownload, and a
  compact More menu for metadata, details, deletion, and ignore;
- **Sources** — URL ingestion and monitored playlists;
- **Activity** — persistent server events plus a `Clear view` button that only
  clears the browser display;
- **Maintenance** — paginated integrity issues, optional batch tag/loudness repair,
  failed-operation export, ignored music, and downloader updates;
- **Help** — illustrated workflow, separate health/operation state diagrams,
  button reference, playback settings, and large-library guidance.

Search is server-side and matches source titles and identified artist/title.
Pagination is applied after filtering/search and uses 50 rows per page.

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
| `YT_CIRCUIT_FAILURES` | `3` | blocked YouTube downloads before the shared circuit opens |
| `YT_CIRCUIT_WINDOW_SECONDS` | `300` | rolling window for blocked-download detection |
| `YT_CIRCUIT_COOLDOWN_SECONDS` | `900` | initial YouTube circuit cooldown |
| `YT_CIRCUIT_MAX_COOLDOWN_SECONDS` | `3600` | maximum repeated circuit cooldown |
| `YTDLP_UPDATE_ENABLED` | `true` | isolated nightly downloader update |
| `YTDLP_UPDATE_TIME` | `04:00` | local scheduled time |
| `TZ` | `Europe/Paris` | updater timezone |

The API resolver uses `playlistItems.snippet.publishedAt`, never video upload time,
for playlist addition history. Spotify and other providers are not enabled yet;
the origin model allows future metadata-only providers to coexist with separate
downloadable origins.

## SQLite schema versioning

The database uses `PRAGMA user_version`. Fresh installations create schema v2.
An unversioned database containing data is refused rather than silently changed.

Schema v1 upgrades automatically to v2 in place. Before the migration changes the
durable job queue, it creates a consistent backup under `state/backups/`. The v2
migration adds deferred-job scheduling and also repairs approval candidates that
the old monitored-source resync bug may have changed back to `QUEUED`, provided
their staged audio still exists.

Future schema upgrades follow the same pattern:

1. make a consistent SQLite backup under `state/backups/`;
2. run an explicit transactional migration;
3. increment `user_version` only after success.

This intentionally avoids a heavy migration framework while retaining deterministic,
testable upgrades for the small SQLite schema.

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
node --test tests/frontend_runtime.test.cjs
```

CI runs these checks for every pull request. The container intentionally uses one
Uvicorn worker because the durable SQLite job worker is single-instance.

## Managing a large library

Counters always describe the **whole selected library user**. Search and status
filters affect the matching-results count and table, not those counters. Health
and operation states overlap: an available track can have a failed redownload.
The Attention filter has the same definition as its counter (missing audio or a
failed operation). Click a counter to select its corresponding filter.

**Retry all failed** queues every FAILED track for the selected user, across all
pages and regardless of search. It does not retry deferred, queued, processing,
or approval tracks. It preserves the original failed operation and recovery
receipt. The response reports queued and skipped tracks. A shared circuit and
provider pacing still apply; this does not launch hundreds of parallel downloads.

Maintenance includes a JSON export of failed operations for diagnosis and
paginated/filterable audit results. Repair eligible tags operates on safe, audited,
idle files only. It never approves identities or downloads audio. Hash mismatches,
unreadable files and outside-library paths are excluded rather than silently
accepting changed files. Individual repair is also in the track's More menu.

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
