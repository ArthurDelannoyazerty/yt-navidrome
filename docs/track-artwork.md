# Per-track artwork

Artwork is independent of artist spelling, recording identity and album. Songs in
the same album may have different embedded pictures. No Bandcamp provider or new
artist-name management is introduced.

## Use

New approvals, including Original source, prefer suitable album/existing artwork
and fall back to the best cached or available YouTube thumbnail across associated
videos. Original source clears stale identity-dependent pictures, but a manually
pinned image is an explicit user choice and is reapplied. A newly merged video
schedules one artwork reconsideration without downloading its audio again.

Open **More > Artwork...** to compare candidates. **Find covers** refreshes known
source-video thumbnails without removing a pin. Select an image and preview its
framing, then **Use this image**. Uploading an image creates a preview only.
**Auto-select** releases a manual override. Pins are per track, never per album.

Good square images are preferred within each source class; resolution and simple
sharpness evidence break ties. Small square images do not automatically outrank
large usable images. Ranking is a heuristic, not recognition of a semantically
correct cover. A uniform dark border revealing a large nearly square image may be
trimmed automatically. Landscape art otherwise retains its proportions. Fit on a
square pads without cropping; center cropping is an explicit previewed choice.
The uncropped, normalized image remains cached. Images are never stretched and
no shared cover.jpg is written into folders holding unrelated songs.

## Existing library

**Maintenance > Fill missing covers** queues idle playable tracks without an
embedded picture, retaining existing covers and manual choices. Busy/pending
tracks can be handled after their operation finishes. No audio is downloaded or
re-encoded. Navidrome needs to rescan changed files; a client's choice to display
album artwork instead of per-track artwork is outside this application's control.

An individual cover can be changed while metadata approval is pending: the
current playable file changes, but the pristine candidate, approval token and
choices remain intact. A pinned image is reused when metadata is later approved.
Failed cover jobs can be retried through the existing Retry control.

## Safety and limits

Candidates, source, dimensions and selections are user/track-scoped. Normalized
JPEG images are cached by content hash; a cached cover remains usable after the
video disappears. Disabled audio download URLs can still contribute public
thumbnails. An origin that was removed/ignored is not an automatic thumbnail
source, while a manually chosen cached image stays pinned.

Updates use a verified staged copy, atomic file replacement and a durable receipt.
External hash changes are refused. Crashes after replacement can finish syncing
without fetching or rendering the image again. Audio packets, descriptive tags,
source comments and pending metadata approvals are preserved. Missing/corrupt
pinned artwork causes an explicit error instead of silently losing the choice.

Gallery/image GETs read local data only. Remote image fetching is restricted to
fixed YouTube thumbnail hosts and validated video paths, with redirects disabled,
timeouts, byte limits and the existing shared provider limiter. Arbitrary remote
image URLs are not accepted. Uploads are limited to still JPEG/PNG/WebP, 8 MiB and
20 megapixels. SVG/animation is rejected. Discovery considers at most 50 videos,
two usable variants per video, and a maximum of 120 candidates per track.

Provider failures are warnings, not reasons to reject otherwise valid new audio.
If no usable image exists, a placeholder/current image remains and manual upload
is available. Schema v7 adds image tables after backing up older databases; schema
migration itself does not rewrite audio files.
