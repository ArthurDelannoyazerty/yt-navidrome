# Music Ingestor

Provider-neutral music ingestion for a user-separated Navidrome library. Python 3.13,
FastAPI, SQLite, beets 2.14.1, and a separately maintained yt-dlp environment.

**Validation status:** application tests were run; live service integration and a
Docker build were not possible in the authoring environment. See `TEST_REPORT.md`
for the exact results and the two real-beets smoke tests that still need execution.
Deploy against a copied library before replacing an existing installation.

## What changed

The downloader retains the supplied yt-dlp options and download call. Cobalt has
been removed. A small adapter delegates music identification, scoring, metadata,
file organization, artwork, embedded lyrics and ReplayGain to beets. A durable,
single-worker queue replaces overlapping background-task dispatch paths.

The web interface retains per-user libraries, ambiguous-match approval, batch
approval, retries, manual tag corrections, monitored sources and playlist exports.
It adds persistent source/job errors, downloader update status and rollback.

YouTube is the only implemented source provider in this version. The provider
interface and discovery metadata are generic; Spotify is intentionally not
advertised as working. A future provider should return `SourceRef`, `Snapshot`
and `Entry` values and register with `PROVIDERS` in `src/providers.py`.

## Dates and music metadata

These are separate concepts and remain separate:

| Meaning | Storage and behavior |
| --- | --- |
| Artist/release date | Beets' music date/original-date fields. `original_date: true` favors the known original release date. Unknown components remain unknown. |
| Discovery/addition time | UTC source timestamp in application state; `Discovery Date: 2021-04-17--21-36-42 UTC` in the standard file comment. |
| Membership-specific addition | Each playlist entry retains its own timestamp and position in the application database. |
| Filesystem modification time | Normal filesystem/tag-write behavior. Never forged to discovery time. |

The fixed-width comment sorts chronologically as text. It contains no provider
name. The `UTC` suffix avoids ambiguities around daylight-saving transitions.
When the same recording URL is in several playlists for the same user, its single
file comment records the earliest known discovery time; every membership still
has its own exact timestamp. The other user's copy and history are independent.

For YouTube playlists, addition time comes exclusively from
`playlistItems.snippet.publishedAt`. Neither the video's publication/upload time
nor the current time is a valid replacement. No partial source snapshot is
committed if one API page fails. Direct track imports, which have no playlist
addition event, use the first-seen time and record `discovery_basis=first_seen`.

Playlist names never become album tags. Real albums come from music metadata.
Unmatched tracks can be approved with their existing/seeded metadata; an unknown
album or release date is not fabricated from a playlist or discovery timestamp.

### Beets matching and release association

There are **no custom match-threshold settings**. Beets computes candidate
distances and recommendations. The UI displays `(1 - distance) * 100`; this is a
beets similarity score, not a calibrated probability. Strong recommendations are
accepted automatically. Other recommendations, including no candidates, remain
in `NEEDS_APPROVAL` until a user selects a candidate or keeps the original metadata.
Explicit batch approval of best matches overrides that wait by user choice.

An important distinction: singleton identification finds a recording, but does
not provide the complete album workflow or automatic album artwork. The adapter
therefore asks MusicBrainz for that recording's official releases, selects the
earliest dated one, and obtains its metadata through beets. This **release
selection rule is an application policy**, separate from beets' unchanged
recording-match recommendations. An explicit recording/release ID can correct
an unsuitable association in the UI. A release must contain the chosen recording.
No metadata service can guarantee the precise historical first publication of
every track; missing/ambiguous release information remains visible rather than
being replaced by a discovery date.

The adapter is pinned to **beets 2.14.1** because preserving a web approval workflow
requires calling its importer/matcher/library interfaces. Upgrade the pin and
adapter together, and run the integration tests before deployment.

### Artwork, lyrics and volume

`fetchart` and `embedart` handle actual release artwork. `lyrics` uses LRCLIB,
with `synced: true`, and writes lyrics inside the audio file. Timed lyrics are
preferred when available; otherwise plain lyrics may be stored. No new `.lrc`
sidecar is generated. Synchronized display depends on the playback client
supporting embedded timed lyrics; it cannot be promised for every Navidrome client.

`replaygain` uses ffmpeg and native Opus R128 tags, with `r128_targetlevel: 89`
(the -18 LUFS target from the previous implementation). This writes gain metadata,
not a new audio normalization/transcoding pass. Missing artwork/lyrics or a gain
calculation failure does not discard successful audio; warnings are logged and,
where detected by the adapter, attached to the completed track.

## Start with Docker Compose

Create a new installation directory, then:

```sh
cp .env.example .env
# Edit .env: set YT_API_KEY and review the schedule settings.
docker compose up -d --build
```

Open `http://127.0.0.1:8008`. The default Compose binding is loopback-only.
Enable YouTube Data API v3 for the API key and restrict the key to that API.
An API key is not OAuth authorization: private playlists requiring account
consent are not supported by this provider. Downloader cookies do not authorize
YouTube Data API calls.

The container runs as UID/GID 1000. Default named volumes contain:

```text
/data/state/
  ingestor.sqlite          application jobs, sources, memberships, events
  beets/<user>/library.db  independent beets databases
  staging/<track-id>/      recoverable download/approval work
  runtimes/               versioned downloader venvs and active/previous pointers
/data/library/
  <user>/
    <album artist>/<real album>/<artist> - <title>.opus
    <artist>/Non-Album/<artist> - <title>.opus
    000000-playlists/<source title>--<source-id-prefix>.m3u
```

Library paths are controlled by `beets.yaml`; destination collision handling is
beets' responsibility. Playlist suffixes prevent two similarly named sources
from overwriting one another. Playlist writes are atomic and readable by the
separate media-server process. Internal JSON staging/runtime files use private
file permissions.

For bind mounts, replace the two volume entries with directories owned/writable
by UID 1000. Mount the library read-only into Navidrome. Configure separate
Navidrome library access for each user's subdirectory when user-level viewing
restrictions are required.

**The user selector is not authentication.** This is an administration interface
whose operator can manage every user. Per-user folders/databases prevent accidental
cross-user imports; they do not implement accounts, login or authorization for
untrusted visitors. Use an authenticated reverse proxy for remote access, and do
not publish port 8008 directly to the Internet. No Docker socket mount is needed.

### Cookies and preserved downloader settings

The existing settings remain `YT_PLAYER_CLIENTS`, `YT_DLP_COOKIES_FILE`,
`DOWNLOAD_SLEEP_MIN`, and `DOWNLOAD_SLEEP_MAX`. Output remains Opus using the
supplied `FFmpegExtractAudio` settings. Deno, ffmpeg and fpcalc are included in
the runtime image. Actual YouTube availability/blocking is outside this app's
control; no alternative download service is used on failure.

A cookie file must be mounted at the container path named by
`YT_DLP_COOKIES_FILE`. Allow the pipeline user to read and, when yt-dlp saves its
cookie jar, write it. Keep cookie files private and out of source control.

### Dependency resolution

No `uv.lock` contents were supplied with the old project, and a replacement could
not be resolved in the network-restricted authoring environment. A fake lockfile
is not included. The Docker build performs a real resolution and retains its
result as `/app/build-uv.lock`. CI also resolves dependencies and publishes its
`uv.lock` as an artifact. Those resolutions may differ for unpinned transitive
packages; beets itself is pinned. For fully reproducible deployment, generate,
review and commit a real lockfile on your build machine and make both CI and the
Docker build consume that same lock with `uv sync --frozen`.

## Nightly yt-dlp updates: no API restart

By default the scheduler checks at **04:00 Europe/Paris** each day. Configure
`YTDLP_UPDATE_TIME`, `TZ`, or `YTDLP_UPDATE_ENABLED=false` in `.env`. A first start
schedules the next occurrence; a previously scheduled update missed while the
app was stopped is attempted after restart. Manual update and rollback buttons
are in the UI. The check loop runs every 30 seconds, so execution is approximate,
not a real-time clock guarantee.

Each update creates a **new** virtual environment under `/data/state/runtimes`,
installs the latest pre-release/nightly `yt-dlp[default]` plus the downloader's
retry dependency, verifies imports and the actual wrapper, and atomically
switches `active.json`. It does not update the API's Python environment.

Every download launches its own subprocess and retains the interpreter selected
at launch. Existing downloads therefore continue using their old environment;
new downloads use the new one. The API does not need to restart, Python modules
are not hot-reloaded, and the container does not need Docker privileges.

An installation/import failure leaves the old runtime active. The active,
previous and currently used runtimes are retained; obsolete runtimes are pruned.
Rollback selects the previous version for future downloads. Disable scheduled
updates while investigating a bad nightly, otherwise the next scheduled run may
install it again. Import checks are not a live-video compatibility test: a
nightly can import successfully and still fail for a particular video.

Image rebuilds remain necessary for OS security fixes, Python, Deno, ffmpeg or
application changes. The weekly GitHub image build publishes a new image; it does
**not** automatically redeploy an already running server.

## Retries and API limits

One durable worker serializes ingestion/metadata jobs. All requests-based
metadata plugins use a shared, SQLite-backed host clock, including across child
processes and application restarts. Requests are spaced at least 1.1 seconds
apart per host, except AcoustID at 0.4 seconds. Existing beets/provider limiting
can make requests slower; it is not weakened.

Transient connection errors, HTTP 429 and selected 5xx errors get at most three
attempts. `Retry-After` seconds and HTTP dates are honored. Long cooldowns defer
rather than keeping a worker asleep for hours. Authentication/key errors do not
receive pointless immediate retries. YouTube quota exhaustion pauses API calls
until the next Pacific-midnight quota period; the local default budget is 9,000
list requests/day, leaving headroom below the usual 10,000-unit project quota.
All YouTube endpoints used here are list endpoints costing one unit per request.
The budget only counts this application's calls, not other apps sharing the key.

After bounded retries, a failed source remains visible with its error. Its
previous successful snapshot and playlist stay intact. Monitored sources retry
on their next scheduled sync (default six hours); one-off sources require a
manual retry. `SYNC_INTERVAL_HOURS=0` disables periodic source syncing. Failed
track jobs require an explicit retry, which resumes staged work where available
instead of always downloading again. The preserved yt-dlp download function
retains its own existing network retry behavior, separately from this API policy.

Service-wide limits can also depend on source IP, account or unrelated clients.
The app cannot coordinate calls from other software on the same IP/API project;
reduce their traffic or add a shared external limit when necessary. The LRCLIB
and artwork pacing is a conservative local policy, not a claim of a published
universal provider quota.

## Playlist and recovery behavior

Complete source snapshots reconcile membership, retain repeated entries, and
write relative-path `.m3u` files. `PLAYLIST_ORDER=source` preserves the source's
order; `PLAYLIST_ORDER=discovery` sorts by each entry's addition timestamp. Removing
a membership/source does not delete the music file. Empty playlists are rewritten
as empty playlists instead of silently leaving a stale list.

Jobs, candidate choices, downloaded-file receipts and apply-operation checkpoints
are persisted. Interrupted work resumes at startup. Explicit redownload/retag
work preserves the previous good file until the replacement is committed and
all affected playlist exports have succeeded. Beets' library and the application
ledger are separate databases: operation checkpoints reconcile interrupted moves;
this is not a distributed transaction or a substitute for backups.

Only one API process/replica may use a state directory. A filesystem lock rejects
a second instance. Do not run external `beet move/import` operations against these
same managed libraries while the service is running; the app's source/playlist
ledger would not automatically track arbitrary external edits.

The UI polls for new events and errors; it no longer maintains a separate
WebSocket broadcasting subsystem. The most recent 20,000 server/worker events
are retained in SQLite, with a smaller in-browser view. Completed job history is
bounded separately. Request errors, child-process output, source failures,
maintenance failures and metadata warnings are surfaced; browser connection
failures appear in a visible banner. A stopped server or unwritable/full state
volume cannot persist its own errors, so container logs and disk monitoring are
still necessary. Common API secrets are redacted; logs are still administrative
information and should not be publicly exposed.

## Migrate the old installation safely

**Do not mount the old database as the new application's state database.** The
schema is intentionally different. Keep the old deployment and backups available
until you have validated the new copy.

Stop writes in the old deployment, back up its database and music, and copy its
library into a new location preserving the per-user directory tree. Point the
new Compose library volume/bind mount at that copy and use an empty state volume.
Build the new image but do not start its normal API before migration; the tool
requires a fresh destination database.

Run a dry run with the old database mounted read-only. Substitute the actual old
library root **as recorded in the old database**, which may be `/data/library`
or `/app/src/navidrome_library`:

```sh
docker compose build
docker compose run --rm --no-deps \
  -v /absolute/path/to/old/library.db:/legacy/library.db:ro \
  ingestor python /app/tools/import_legacy.py \
  --database /legacy/library.db \
  --old-library-root /data/library
```

The new library mount must already contain the copied audio. Relative legacy
paths are resolved against `/app/src` unless `--old-working-directory` is supplied.
Review missing audio, unsupported URLs and canonical duplicates in the report.
After review, repeat with `--apply`. Add `--queue-reidentify` to identify/tag the
copied audio with beets without redownloading it. Then start the API:

```sh
docker compose up -d
```

The tool preserves track UUIDs, source URLs and usable file paths. Old playlist
memberships become explicitly labelled, non-monitored `(legacy)` playlists;
the original monitored URLs are queued separately. This avoids guessing source
identity from playlist names. After comparing exports, remove obsolete legacy
playlist entries through the UI.

**Legacy precision cannot be recovered from a date-only column.** The old date
is retained as provenance; no midnight timestamp is invented. Syncing still
available original playlists can recover exact API timestamps and update file
comments. Old MusicBrainz approval JSON is incompatible with beets candidates
and is not reused; old in-flight audio outside the copied library is reported
as unavailable. Files whose old album/date tags were wrong remain unchanged
until re-identification/correction. The migration tool itself changes neither
the supplied old database nor the copied audio tags.

## Development and tests

Install Python 3.13, uv, ffmpeg and fpcalc, then:

```sh
uv sync --extra test
uv run pytest
uv run python -m compileall -q src tools tests
node --check src/static/app.js
```

Real-beets smoke tests use generated Opus audio, fixture MusicBrainz metadata and
fixture lyrics/artwork: they exercise real beets library/tag/file/gain operations
without making remote API calls. CI sets `REQUIRE_BEETS=1` so a missing beets
installation is a failure, not a silent skip. They do not replace a live provider
smoke test with your key, client and library.

For a local non-Docker API, export writable `INGESTOR_STATE_DIR` and
`NAVIDROME_LIB_DIR`, set `YTDLP_PYTHON` to the interpreter containing yt-dlp and
tenacity, then run one worker:

```sh
export INGESTOR_STATE_DIR="$PWD/.local/state"
export NAVIDROME_LIB_DIR="$PWD/.local/library"
export YTDLP_PYTHON="$PWD/.venv/bin/python"
uv run uvicorn main:app --app-dir src --host 127.0.0.1 --port 8008 --workers 1
```

## Primary documentation consulted

- Beets 2.14.1 configuration: https://beets.readthedocs.io/en/v2.14.1/reference/config.html
- Beets autotagging: https://beets.readthedocs.io/en/v2.14.1/guides/tagger.html
- Chroma: https://beets.readthedocs.io/en/v2.14.1/plugins/chroma.html
- Fetchart: https://beets.readthedocs.io/en/v2.14.1/plugins/fetchart.html
- Embedart: https://beets.readthedocs.io/en/v2.14.1/plugins/embedart.html
- Lyrics: https://beets.readthedocs.io/en/v2.14.1/plugins/lyrics.html
- ReplayGain: https://beets.readthedocs.io/en/v2.14.1/plugins/replaygain.html
- Pinned source/API contracts: https://github.com/beetbox/beets/tree/v2.14.1
- YouTube playlist item timestamp: https://developers.google.com/youtube/v3/docs/playlistItems
- YouTube quotas: https://developers.google.com/youtube/v3/determine_quota_cost
- MusicBrainz API: https://musicbrainz.org/doc/MusicBrainz_API
- AcoustID API: https://acoustid.org/webservice
- yt-dlp installation/nightlies: https://github.com/yt-dlp/yt-dlp/wiki/Installation
