# Approval

Open **Approval**, between Music and Sources. It defaults to **Waiting for approval**.
Its Show menu has the same filters as Music, including Source metadata applied.
Approval keeps its filter and search separate from Music. The list contains
reviewable tracks; currently active jobs and tracks without reviewable audio are
excluded. Refresh reloads the list when new imports arrive.

Current music, source title/channel, tags, artwork and audio are on the left.
Beets matches are table rows on the right. **Use** applies the displayed match
in one click and advances after the server accepts the change. The separate
**Use original source metadata** button does the same for retained source tags.
MusicBrainz search starts automatically below, using the current source hints.
Search alone never changes metadata. **Use** on a result selects that recording;
its Details control also offers release selection when editions are available.

**Previous** and **Next** navigate without applying anything. **Choose music** is
collapsed by default; it searches video title/channel within this queue and renders
30 entries per page, even for a large library. The last position is remembered by
track ID for the current library/filter/search. There are no custom shortcuts.

Music's **Review metadata** action opens the same Approval page, not a dialog.
**Edit metadata** opens that page's manual editor. Less frequent operations remain
under Edit metadata, More metadata options, or a match's Details. They include
manual tags, keeping current tags, remembered artist mappings, rejected matches,
matching evidence and release selection. Artwork remains a separate editor.
The former Review this view / review order / selected-approval toolbar and row
selection checkboxes have been removed. Advanced library-wide approval and the
revision-checked selected-approval API remain available.

Each choice uses the displayed approval token or revision. Double clicks are
blocked. A rejected/stale change stays on the track with Reload available. Queued
means the worker accepted the request, not that file processing already finished;
worker failures remain visible in Music and Activity. Changing tabs, filters or
library invalidates old responses and stops automatic searches from the abandoned
review. Query fallback, provider pacing and Retry-After remain in effect.

Desktop and laptop layouts use two columns. Only narrow phone screens stack the
music above the matching controls. Existing green/neutral styling is reused.
