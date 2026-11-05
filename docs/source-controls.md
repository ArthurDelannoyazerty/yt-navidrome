# Stop retries, disable a video, and review existing audio

A download source is a video URL associated with a logical library track. Disabling a video prevents new downloads from that URL without deleting the track, its current audio, its original source metadata, or its playlist memberships.

## Source controls

**Stop retrying / disable video** cancels a queued or deferred operation immediately. For an active download, it requests a durable stop and terminates that job's subprocess group. The worker remains available for other tracks. The stop survives service restart and monitored-source synchronization.

If an audio update has already entered its publication or metadata-application transaction, it finishes that recoverable transaction. The response says that publication is finishing. The URL remains disabled for future downloads.

**Enable video** only changes the source permission. It does not start a download. Use Retry when ready.

**Use another URL** accepts one YouTube video, including a watch link copied from a playlist. An explicit playlist URL is rejected. The alternative is attached to the same logical track. Existing audio remains playable while the alternative is downloaded and reviewed, including audio in the temporary approval album. The original URL stays disabled if it was disabled, and its playlist memberships remain attached.

A strong match to the current confirmed MusicBrainz recording can replace the audio. Other matches require review. A replacement cannot change a confirmed recording's identity: correct that identity separately first. An alternative matching another existing canonical track is rejected with a review message; this operation does not silently merge two logical tracks or move their original playlist memberships.

Source disable is distinct from deleting local audio or adding an ignored-music rule. It does not create a MusicBrainz or URL tombstone.

## Retry behavior

Age/login, members-only, regional restrictions, unavailable, private, and removed video errors disable that individual URL and require intervention. They do not open the library-wide YouTube circuit breaker. Bot verification and HTTP blocking continue to use the shared circuit.

The application stops automatic download retries after `DOWNLOAD_MAX_ATTEMPTS` failed download attempts (default `5`). This is the application attempt budget; the downloader may perform its own limited retries within an attempt. Explicit Retry starts a fresh job budget.

Waiting for a shared YouTube cooldown does not consume a video's download budget. Metadata-provider deferrals do not consume it either. A completed download receipt and staged audio survive metadata deferrals, so retrying identification does not download again.

Stopped tracks use operation state `STOPPED` and appear in Attention. Their health can remain `AVAILABLE` when existing audio is present.

## Refresh metadata suggestions

The `refresh_suggestions` action identifies existing audio without downloading. It reuses the original staged candidate when available, otherwise copies the verified current audio. Retained source tags, the original title, and channel are supplied as identification hints; processed library tags are not archived as original source metadata.

Refreshing always returns suggestions for human review. It retains the current playable file until the user approves an application. An unfinished application must be recovered with Retry before starting a fresh review or alternative download. While an alternative video's approval is pending, finish that candidate review before manually retagging the currently playing audio or refreshing suggestions; those actions must not silently switch from the candidate to the old file.

Clients can include the current metadata `revision` with `retag` and `refresh_suggestions`. A stale provided revision is rejected inside the queue transaction. Older clients may omit it.

## API

| Endpoint | JSON body | Result |
| --- | --- | --- |
| `POST /api/tracks/{track_id}/origins/{origin_id}/disable` | `{user_id, reason?}` | Disable the selected origin and stop matching pre-publication work |
| `POST /api/tracks/{track_id}/origins/{origin_id}/enable` | `{user_id}` | Enable without starting work |
| `POST /api/tracks/{track_id}/replacement` | `{user_id, url}` | Associate and queue one alternative video |
| `GET /api/disabled-origins?user_id=…` | — | Disabled origins with track, reason, and timestamp |
| Existing track `/action` endpoint | `{user_id, mode:"refresh_suggestions", revision?}` | Queue review using existing audio |

Disable responses include `cancelled`, `cancellation_pending`, `publication_finishing`, and a human-readable `message`.

Track and detail payloads include `active_origin_id` (also `job_origin_id`), `job_stage`, `can_stop_download`, `cancellation_pending`, `download_failures`, and `needs_source`. The active origin may differ from the currently playing origin during a replacement. Disabling the old playing URL does not cancel work downloading a different URL.

Origin payloads include `download_enabled`, `disabled_at`, and `disabled_reason`.

## Persistence and migration

Schema version 5 adds source-enable and job-cancellation fields. Upgrading a version 4 database first creates a consistent SQLite backup under the existing backups directory, then adds fields in one transaction. Existing origins remain enabled by default. Disabled-source state is independent of provider availability and is not overwritten by source synchronization.
