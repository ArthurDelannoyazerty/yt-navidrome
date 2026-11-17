# Explicit workflow state machines

Implementation and audit against PR #26, commit
`d6e9db541092d464024e9bc998538d8affeb08ea` (schema 9).
This change introduces schema 10. It changes application code, not an existing
production database or library. The source archive and patch are review artifacts;
they are not a deployed release.

## What removal now stops

Both **Archive source** and **Remove source and its music...** operate on the
selected library user and the selected source. Neither is a global stop button.
The source's durable origin associations are used, including entries that have
subsequently disappeared from a playlist. If another source (including an archived
source) or an independent discovery origin still claims the same logical track,
its work and music are retained.

| Work at the time of removal | Behavior |
| --- | --- |
| Queued exclusive import or metadata job | Cancelled transactionally, before the request returns. |
| Exclusive delayed retry | Also cancelled: delayed jobs are persisted as `PENDING` plus `not_before`, not a separate job-state string. |
| Active exclusive download / identification / source scan | A cancellation request is committed, then the worker task is signalled on its own event loop. Its subprocess supervisor performs process-group shutdown. |
| File application or publication already in progress | Not interrupted halfway through. A durable `stop_after_commit` request drains it to a safe boundary. |
| Committed operation whose cleanup is deferred | Its durable receipt and cleanup job are preserved. Cleanup gets priority when runnable; purge waits rather than deleting that job or its recovery paths. |
| Shared track, independently retained track, other source's scan, or another user's work | Not cancelled by this operation. |

Archiving retains already published audio, source history and the archived source
record. Purging subsequently removes exclusive tracks and their files, through
retryable cleanup. The source request returns `cancelled_jobs`, `cancelling_jobs`,
`finishing_commits` and `shared_tracks`. A safe commit is not falsely reported as
already cancelled.

Previously, purge only cancelled queued source scans when requested. Track jobs
were removed later when the purge worker deleted their tracks; an active import
could continue in the meantime. Archive did not cancel those track jobs.

The source lifecycle and cancellation requests now share one `BEGIN IMMEDIATE`
transaction. Worker interruption occurs only after it commits. A provider scan
that returns late is checked again inside `apply_snapshot`, and cannot silently
reactivate an archived/purging source. Playlist export rechecks the source inside
the same database lock used for archival, so an old exporter cannot recreate an
archived playlist.

## Design choice

`src/state_machines.py` defines small immutable transition tables and named
projections. `Machine.send(state, event)` accepts only declared events.
`Machine.require(before, after)` validates edges. These functions have no network,
filesystem or database effects.

Persistent state belongs to SQLite, not an additional in-memory FSM object. SQL
triggers generated from the same tables protect existing direct SQL writers as
well as service helpers. This avoids a second state store that could disagree
with SQLite after a restart. Transitions of the seven guarded fields are recorded
in `state_transition_log`; the scheduler bounds that diagnostic history to the
latest 20,000 entries.

In-memory browser requests use a small dependency-free `StateMachine` /
`AsyncState` implementation. It has named states and events, immutable transition
tables and request-generation tokens. It does not require adding XState, an ORM,
a task broker, or a new package to this single-process application.

State transitions do not replace business guards. Ownership, revision tokens,
file containment, matching identity, download permissions, quotas and active-job
checks still apply. A legal state edge does not itself authorize an operation.

## Backend inventory: 20 executable contracts

The full catalogue is available from `GET /api/state-machines` and from
`python tools/dump_state_machines.py`. The transition tables are the source of
truth, rather than a separately maintained diagram.

| Contract | Explicit states / representation | Integration and rationale |
| --- | --- | --- |
| `job` | `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED` | `store.py`: claim, finish, failure, recovery and cancellation; SQLite rejects reopening terminal jobs. Retrying creates a new job. |
| `job_control` | `QUEUED`, `DEFERRED`, `RUNNING`, `COMMITTING`, `DRAINING`, `CANCELLING`, terminal outcomes | Projection of queue state, deadline, stage, receipt evidence and cancellation request. No independently writable duplicate column. |
| `source` | `PENDING`, `SYNCING`, `OK`, `DEFERRED`, `FAILED`, `REMOVED`, `PURGING`, `PURGE_FAILED` | `main.py`, `store.py`, `source_purge.py`, `pipeline.py`: archive, restore, synchronization, purge and retry are named events. `REMOVED` means archived; final purge deletes the row and uses a receipt for cleanup completion. |
| `track_operation` | `IDLE`, `QUEUED`, `RUNNING`, `DEFERRED`, `NEEDS_APPROVAL`, `FAILED`, `STOPPED` | Guards every SQL writer, including metadata, artwork, review and Spotify operations. Direct helper/recovery edges are retained; not only normal worker edges are legal. |
| `track_health` | `UNAVAILABLE`, `AVAILABLE`, `MISSING` | Independent of operation state. A failed metadata operation must not imply that playable audio disappeared. |
| `origin_availability` | `UNKNOWN`, `AVAILABLE`, `UNAVAILABLE`, `DEFERRED` | Separate from user permission. Observed availability and whether a URL may be retried are different facts. |
| `download_permission` | `ENABLED`, `DISABLED` | Named enable/disable decisions, with Boolean-column guards and the existing downloadable-origin check. |
| `asset` | `CURRENT` -> `REPLACED` | SQLite prevents accidentally promoting an already replaced asset through a reverse state update. File health is tracked separately. |
| `receipt` | `COMMITTED` -> `FINALIZED` | Projection of the durable receipt's finalization timestamp. Finalization is idempotent and cannot be reopened. Track deletion and source purge use this contract for post-commit cleanup. |
| `publication` | `PREPARED`, `STAGED`, `PUBLISHED`, `COMMITTED`, `FINALIZED`, `ABORTED` | `pipeline.py`: a recoverable file/DB transaction. Staged results and receipts provide evidence; `SwapPhase` uses hashes to distinguish not-yet-swapped, already-swapped and conflicting bytes in repair/artwork paths. |
| `artwork_selection` | `auto`, `manual` | SQLite validates pin/automatic preference transitions. Discovery work still uses the ordinary track job and receipt mechanisms. |
| `download_circuit` | `CLOSED`, `OPEN`, `HALF_OPEN` | `runtime.py`: deadline expiration enters a probe state; a failed probe reopens, a successful probe closes. Failure count, window and deadline remain context. |
| `provider_cooldown` | `READY`, `WAITING` | Named deadline projection used by HTTP/spotDL policy. A deadline is data, not a separate enum value for every possible timestamp. |
| `runtime_update` | `IDLE`, `INSTALLING`, `VALIDATING`, `ACTIVATING`, `OK`, `FAILED`, `CANCELLED`, `INTERRUPTED`, `ROLLED_BACK` | Both yt-dlp and spotDL publish explicit settings phases. Old `RUNNING` settings are interpreted conservatively on recovery. The active pointer remains the filesystem authority; a post-activation error must not delete its runtime. |
| `oauth_attempt` | `PENDING`, `EXCHANGING`, `CONNECTED`, `FAILED`, `EXPIRED`, `DENIED`, `CANCELLED` | `spotify_auth.py`: one-use attempts are consumed before token exchange. Private token/PKCE files remain private; the diagnostic state catalogue contains no credentials. Not every failure is persisted, because a consumed attempt must not become reusable. |
| `spotify_credentials` | `DISCONNECTED`, `CONNECTED`, `EXPIRED`, `REFRESHING` | Token validity is derived from configuration and expiry, then refresh/rejection/revocation use named events under the existing credential lock. No separate writable Boolean attempts to represent all these cases. |
| `spotify_resolution` | `UNRESOLVED`, `RESOLVING`, `RESOLVED`, `WAITING_AUDIO`, `IMPORTING`, `LINKED`, `FAILED`, `CANCELLED` | `spotify_library.py`: resolver handoff and library-link stages are explicit. The durable job payload remains authoritative; resolved evidence is reused rather than repeating the provider lookup after a retry. |
| `service` | `NEW`, `STARTING`, `RECOVERING`, `RUNNING`, `FAILED`, `STOPPING`, `STOPPED` | `main.py`: instance lock, migrations, recovery, worker startup/failure and shutdown. Native asyncio tasks/events still perform concurrency and cancellation. |
| `metadata_cache` | `EMPTY`, `FETCHING`, `FRESH`, `STALE` | `musicbrainz_search.py`: cache/in-flight/deadline projection with named fetch/result/failure transitions. asyncio tasks still coalesce concurrent requests, and cancellation shielding preserves a shared lookup. |
| `schedule` | `UNSCHEDULED`, `WAITING`, `DUE`, `DISABLED` | `pipeline.py`: nightly update and integrity schedules use explicit deadline decisions. A newly configured nightly schedule is armed rather than accidentally running immediately. |

### Persistent enforcement

The guarded database fields are `jobs.state`, `sources.status`,
`tracks.operation_state`, `tracks.health`, `track_origins.availability`,
`assets.state`, and `artwork_preferences.mode`. Job-stage vocabulary, Boolean
monitoring/download permissions and monotone receipt finalization also have
SQLite guards. A source cannot move from `REMOVED` directly to `OK`; it must be
explicitly restored. A completed job cannot be set back to `PENDING`.

Schema 10 installs the state constraints, attribution backfill where necessary,
and schema-version update transactionally. Existing unknown lifecycle values are
rejected instead of silently being assigned a guessed meaning. Versioned
migration backups are retained. State constraints mean ad-hoc repair SQL must
also respect the contracts; this is intentional.

The transition log is diagnostic history, **not** an event-sourced replacement
for the database, a complete user audit trail, or an authentication mechanism.
External SQLite clients can read the schema without loading Python functions.

## Browser inventory

| Area | Replacement for implicit state |
| --- | --- |
| Account selection | `loading`, `choosing`, `selected`, `error`; remembered identity remains an explicit user preference, validated against the user list. |
| Tab and music-view selection | Finite selection machines reject unknown panels/views instead of rendering an invalid combination. |
| Every request scope in `RequestGate` | `idle` -> `pending` -> `ready` / `failed`, with retry/reset and epoch fencing. Old-user and out-of-order responses cannot settle the current request. |
| Fast/slow polling, replacement and bulk actions | Independent async actors, not mutable `...Pending` flags. User switching resets the actors; an old poll's finalizer cannot unlock or block a new account's poll. |
| Per-button writes | An async actor prevents duplicate synthetic/concurrent submissions and exposes failure/retry without a stuck disabled button. |
| Metadata approval | Independent detail-load, queue-load, mutation and search actors. Existing ownership/revision/serial guards still protect the displayed approval decision. |
| Release lookup cache | A per-recording async actor replaces `pending` flags; retry failure remains distinct from a successful empty result. |
| Spotify browser requests | Named request scopes share the common ready/failure/stale-result contracts. Server credential status remains authoritative. |
| Artwork changes | A mutation actor replaces the `busy` flag; native AbortController/serial guards retain ownership of read requests and poll callbacks. |
| Help renderer and diagram | An async loader plus `unmounted` / `mounting` / `mounted` / `failed` diagram lifecycle. Script retry and diagram disposal are separate from whether the Help tab is visible. |

Native audio playback, dialog visibility and AbortController cancellation already
have browser-owned lifecycle APIs. They are not copied into a second application
state column. Pagination, selected IDs, query text, scroll-follow preference,
revision strings and numeric retry deadlines are context, not large artificial
state machines. API HTTP status codes and provider JSON called `state` are data,
not local workflow states.

## Complete first-party file review

The following coverage includes every application Python module, authored browser
script and application migration tool in this snapshot. Vendored Pele code,
external beets/downloader internals and generated dependency locks are not rewritten.

- **Workflow owners:** `store.py`, `pipeline.py`, `main.py`, `source_purge.py`,
  `runtime.py`, `spotdl_runtime.py`, `spotify_auth.py`, `spotify_library.py`,
  `musicbrainz_search.py`, `artwork_jobs.py`, and new `state_machines.py`.
- **Shared policy / request effects:** `http_policy.py` uses the explicit cooldown
  projection. `beets_bridge.py` remains an isolated command adapter: it owns no
  additional durable application lifecycle. Its file changes are orchestrated by
  pipeline jobs and recoverable receipts. `downloader.py` and `spotdl_bridge.py`
  are bounded subprocess adapters, not independent application-state owners.
- **API façades:** `artwork_routes.py`, `metadata_routes.py`, `review_routes.py`,
  `spotify_routes.py` delegate mutations to the guarded store/workflows. A route's
  command name or response status is not a new lifecycle.
- **Data/metadata helpers:** `artwork.py`, `audio_tags.py`, `common.py`,
  `library_query.py`, `metadata_hints.py`, `providers.py`, `source_metadata.py`,
  `spotify_provider.py`. Provenance, metadata source, preferences, policy sets and
  content fingerprints are not converted into fake lifecycle states. Their
  existing business validation remains necessary alongside the FSMs.
- **Authored browser code:** `static/app.js`, `static/ui-state.js`,
  `static/metadata-ui.js`, `static/spotify-ui.js`, `static/artwork-ui.js`,
  `static/help-ui.js` use the contracts described above. HTML/CSS render those
  states; they are not separate state stores.
- **Maintenance/build tools:** `tools/import_legacy.py` is a dry-run or single
  transactional import command, not a resumable worker. Imported values are
  subject to schema-10 guards. `tools/vendor-pele/build.mjs` and `entry.js` are
  build-time bundling tools. `tools/dump_state_machines.py` exports the executable
  contracts for review. Tests and CI are validation of these workflows, not a
  second implementation of their state.

An audit inventory is not a proof that no future code can introduce another
implicit workflow. New temporal state should extend these contracts or document
why a native task, transaction, policy set or immutable record is the appropriate
owner. Do not introduce a new writable status flag alongside an existing
machine without specifying which representation is authoritative.

## Regressions addressed beyond queue cancellation

1. A late source scan could recreate source state after archive/purge. Snapshot
   application now rejects inactive source state inside the write transaction.
2. Playlist export could race archival and recreate the playlist file. Both now
   serialize against the same SQLite lock and reread current source state.
3. A shared idle track could lose an unrelated, independent origin during purge.
   Origin cleanup is now restricted to origins actually attributed to this source.
4. A pending committed cleanup job could be deleted by a higher-priority purge.
   Draining cleanup now runs first when ready; purge waits when it is deferred.
5. Cancellation was signalled from FastAPI worker threads directly onto an asyncio
   task. Signals now use the task's event loop and survive shutdown via durable
   flags, rather than relying solely on an in-memory task reference.
6. A downloader update error after pointer activation could delete the newly
   active runtime. Cleanup now checks the active pointer before removing its
   directory, and update phases distinguish install/validation/activation.
7. Browser request Booleans could be settled by stale completions or continue
   blocking polling after account changes. Async actors use generation tokens
   and reset their ownership on account changes.

## Verification and remaining release gates

`tests/test_state_machines.py` tests the actual declared transitions, SQLite
constraints and rollback behavior, migration, source scope, delayed cancellation,
thread-safe running cancellation, safe commit boundaries, deferred cleanup,
stale scans/exports, independent origins, runtime update failure and service
restart. Existing backend and JavaScript regression suites remain in place.

Run in the project's dependency-complete environment:

```sh
uv sync --frozen --extra test
uv run python -m compileall -q src tools tests
uv run pytest
node --test tests/frontend_runtime.test.cjs tests/spotify_frontend.test.cjs \
  tests/metadata_frontend.test.cjs tests/help_frontend.test.cjs
REQUIRE_BROWSER=1 node --test tests/browser_ui.test.cjs
```

The accompanying validation report identifies local checks and exclusions.
The local environment lacks pinned beets, and its managed browser blocks the
loopback test site. Therefore this refactor still requires the full pinned-beets
and real-browser CI run before merge/deployment. Older green checks on PR #26 do
not validate this new patch. No production audio or uploaded SQLite file was
modified. Removed source rows from before the archival feature are still not
reconstructed automatically.
