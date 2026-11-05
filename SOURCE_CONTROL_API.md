# Source control API contract (implementation in progress)

- Origin records gain `download_enabled` (0/1), `disabled_at`, `disabled_reason`.
- POST `/api/tracks/{track_id}/origins/{origin_id}/disable` JSON `{user_id, reason?}` disables future downloads and cancels pre-publication work for this origin. It keeps audio, music identity, discovery history, and playlist membership. Response includes `message`, `cancelled`, `cancellation_pending`, `publication_finishing`.
- POST same `/enable` JSON `{user_id}` re-enables the URL without automatically retrying. Response `message`.
- POST `/api/tracks/{track_id}/replacement` JSON `{user_id, url}` validates a single YouTube video and queues replacement audio on the existing logical track. The old URL remains disabled; current audio stays active until success. Explicit review handles mismatch. Existing URL owned by another track is rejected with actionable text (use the existing track rather than silently merge).
- GET `/api/disabled-origins?user_id=...` lists retained disabled origins (each includes track_id, url, title, disabled_reason, disabled_at). Can show alongside Ignored music with a disabled-source badge.
- Track `/action` supports `refresh_suggestions`: identify existing pristine staged or current audio using retained source hints, then request review, without a new download.
- Track payload gains `job_stage`, `download_failures`, `can_stop_download`, `needs_source`. Stopped operation state is `STOPPED`; existing assets remain AVAILABLE. Source-specific restricted errors stop automatic retry and disable that URL; generic transient errors exhaust a configurable limit and become FAILED.
- API disable works even for busy tracks. It is separate from existing generic origin ignore/detach and delete actions.
