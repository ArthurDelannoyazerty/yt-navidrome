# Review a large music library

Use **Review this view** after choosing a Music filter and search. The review queue contains the eligible tracks from that view in a stable order. **Recently updated** keeps the dashboard order; **A–Z (artist / title)** sorts the stored display title. The order is a snapshot, so saving one track does not move the next track.

## One decision, then the next track

The dialog shows original source metadata, actual current file tags and the selected change together. Listen to **Audio under review** while approving a candidate, or **Current local audio** for an existing track. The source video remains available as a separate link. Candidate playback is tied to the displayed approval token, so changed candidates cannot silently substitute another audio file.

- **Save & next** queues the displayed choice and opens the next item.
- **Skip** leaves a track unchanged for later.
- **Back** opens the previous item with fresh metadata.
- **Review skipped tracks** starts another pass over skipped items at the end.
- Alt + Enter saves and advances; Alt + Right skips; Alt + Left goes back.

Closing the dialog keeps its position in this browser for the selected library and filter/search/order. The app keeps only the latest queue per library user. Starting a queue for another view replaces that user's saved queue. A browser that cannot save the position can still review tracks normally.

Each newly opened track fetches a fresh revision and approval token. Server errors remain visible in the dialog. Reload the track if its metadata or candidate changed. Closing, reopening or changing library during a save prevents its delayed response from advancing a different review session.

## Choose metadata in one place

**Suggested matches** presents the automatic choices with match scores, duration differences and version clues where available. A beets match score is not the probability of correctness. **Wrong recording** rejects that recording for this source; rejected suggestions can be allowed again.

**Search MusicBrainz** starts with conservative source hints. Native Japanese names, aliases and sort names remain visible. A remembered artist mapping uses the MusicBrainz artist ID for later searches, and requires your explicit confirmation. You can forget that mapping. When a search broadens to the title alone, its explanation stays visible and later pages retain the same effective query. Search relevance describes text search, independently of the automatic match score.

**Original source** restores the retained source values and clears an applied MusicBrainz identity. **Edit manually** starts with actual file tags and submits only changed, nonempty fields with the revision you reviewed. Blank fields preserve their existing value. The existing More → Edit metadata action opens this pane.

## Approve an explicit selection

Check **Select** beside a waiting track after checking its displayed dropdown choice. **Select waiting on this page** selects only the rendered waiting rows. Selections remain while changing pages within the same view and clear when changing the filter, search or library. **Approve selected** sends exactly those track IDs, displayed candidate indexes and displayed approval tokens, up to 500 at a time.

Background refresh never replaces a saved approval token with a token for unseen suggestions. A track changed since selection is skipped by the server for another review. The response reports queued and skipped selections.

The original library-wide approval action remains under **Advanced library actions**, with an explicit warning that it applies the first suggestion across every page and filter. Source restoration, manual editing, refresh suggestions, per-origin management, repair, reprocessing, redownload and removal remain available.
# Metadata review evidence and matching

The matcher still uses pinned beets 2.14.1 and its native recommendation
thresholds. A match score is a weighted similarity score, not the probability
that a recording is correct. New downloads still require explicit approval.

## Better input without changing original tags

The application keeps retained source metadata separate from query hints.
Hints normalize Unicode width/spacing and remove complete video-only decorations
such as "[Official Video]" or "[HD]". Live, cover, remix, instrumental,
TV-size and other version labels remain available as evidence.

An explicit source artist or an "Artist - Title" title supplies an artist hint.
A channel ending in " - Topic" can supply a suggested artist when the source
has none. An arbitrary uploader/channel is never treated as the performer.
These hints are explanations and search inputs, not approval of an identity.

A confirmed artist alias is scoped to one library user. The user selects the
MusicBrainz artist explicitly; the server resolves the artist ID and name.
The alias maps retained source artist text (including a Topic hint) to that
identity. It is reversible and changes future search hints, not existing tags,
audio or approvals. It is useful for native-script and Latin-script names.
Artist search and recording results include canonical names, credited names,
artist IDs and available aliases.

Original script and source tags remain unchanged. There is no automatic
translation or global display-language change.

## Manual search

Search starts with the requested title and artist. If the first page is empty,
it can try a cleaned title and then title-only search. There are at most two
fallback attempts. A selected artist ID is never dropped by fallback.

The response reports the effective query and explains when the artist
restriction was removed. Search relevance is not audio-match confidence.
Pagination follows the query selected from page one rather than broadening the
query merely because a later page is empty.

All requests, including artist confirmation, use the existing shared
MusicBrainz HTTP policy and limiter. Identical requests coalesce and results are
cached briefly. Rate limits or provider errors do not cause a broader search.

## Evidence available during review

Automatic choices include:

- the beets match score and individual non-zero distance penalties;
- local and candidate durations, with the absolute difference;
- source/candidate version clues and a warning when those clues differ;
- candidate artist IDs;
- whether the current AcoustID result supports this recording, when known.

A version-clue warning is a prompt to inspect the recording. Missing labels do
not establish that two recordings differ. A duration difference can be caused
by an intro or outro and does not prove a match is wrong.

The identification report records source and normalized input, hint provenance,
fingerprint state, linked recording IDs, candidate count and the gap between the
top two scores. It does not claim a measured success probability. The adapter
uses pinned beets APIs; offline integration tests cover those API assumptions.

Diagnostics refer to the audio origin currently under review. A newer report
from a different upload of the same logical track cannot replace that evidence.

Current descriptive tags are read from the actual file inside the selected
user's library. Source tags come from the retained archive or explicit legacy
provider-title fallback. Internal tags, fingerprints and file paths are not
included in the public candidate choices.

## Remembering a wrong suggestion

Rejecting a displayed recording remembers the decision for that source origin.
It removes that suggestion from the current approval choices and future
automatic candidates. It does not block the recording globally or delete audio.
The original-source choice and the explicit keep-current-tags choice remain.
The decision can be reversed. Refresh suggestions after reversing a decision to
retrieve the candidate again.

Alias and suggestion decisions use the current review revision and reject
stale dialogs or active work. A stopped track with available audio can still
be reviewed. Every decision advances its revision even within the same clock
tick. Approval choices additionally have a token bound to their durable content.

## API contract for the review interface

GET /api/tracks/{track_id}/metadata-context returns existing context plus:

- choices and candidates: sanitized choices with stable original index values;
- approval_token: token to send when approving a choice;
- search_hints and approved_artist_alias;
- rejected_candidates;
- current_tags: descriptive values as arrays;
- current_duration_seconds and candidate_duration_seconds when available;
- current_tags_error when current audio cannot be inspected;
- latest_identification for the selected current/pending origin.

POST /api/tracks/{track_id}/artist-alias takes user_id, revision and artist_id.
Set forget=true to remove the alias; no artist ID is needed for that operation.
The server determines the source alias and resolves artist metadata itself.

POST /api/tracks/{track_id}/reject-candidate takes user_id, revision and mbid.
Set reject=false to allow that suggestion again. Both endpoints return refreshed
context and a message.

Automatic identification accepts a track search_hints object. The bridge also
accepts origin_id with source_title, source_channel, source_tags and source_basis;
it can look up confirmed aliases for that owned origin itself. When retained
source hints have no artist, an old applied artist is cleared in memory before
fresh identification. Retained files and tags are not changed during lookup.

## Evaluating recognition changes

Use a labelled sample before changing approval thresholds. Include native
Japanese script, mixed-script names, title-only Topic uploads, live/remix/TV edits,
and unusual long uploads. Record these separately:

1. Is the recorded performance/recording correct?
2. Is the chosen album/release the desired edition?
3. Is the preferred spelling/script shown?
4. Was the correct recording present among the first few suggestions?
5. How long did review take and was manual search needed?

Keep a held-out sample for evaluating new normalization or matching rules.
Track wrong-version acceptance and false merges as well as top-choice accuracy.
No percentage accuracy improvement is claimed by these code changes.

## Review interface

See [Review a large music library](continuous-review.md) for Save & next, resume, selected approvals, playback and the source/current/proposed comparison.
