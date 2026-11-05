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
