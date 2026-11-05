# Source download controls

The Music view treats download availability separately from playable audio. A stopped or disabled URL does not delete the track, audio, discovery history or playlist memberships.

- **Stop retrying this URL** acts on the origin in the active job payload, even if another origin supplies the current playable audio. The button is unavailable while cancellation is already pending. If the job origin is unknown, use the exact URL in Sources & details.
- **Sources & details → Disable downloads / Enable URL** works per URL. Enabling a URL does not automatically retry it.
- **Use another URL…** queues a single YouTube video on the existing track. The server checks identity and retains current audio until replacement succeeds. The dialog preserves server validation errors and prevents duplicate submissions.
- **Music → Ignored music** has separate sections for ignore rules and retained disabled download URLs. Restoring an ignore rule and enabling a disabled URL remain distinct actions.
- **Refresh suggestions** identifies available audio again without another download. Reprocess and Redownload remain available for enabled audio origins.

Cancellation that arrives after publication has started allows that file operation to finish safely; the URL still becomes disabled for future downloads. The UI displays this result explicitly.

Frontend regression tests cover exact active-origin targeting, disabled-origin eligibility, explicit re-enabling without retry, kept audio on stopped tracks, duplicate replacement submissions, server errors, and stale library responses. Run `node --test tests/frontend_runtime.test.cjs` and the complete frontend suite before release.
