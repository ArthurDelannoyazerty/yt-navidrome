# Source links, source metadata and switching monitored playlists

COMMENT and DESCRIPTION carry the same discovery line followed by a Sources block
containing all associated canonical YouTube video and Spotify track URLs. Tracking
parameters, credentials, signed playback links and playlist URLs are excluded.
Disabled videos remain part of the provenance until explicitly detached/removed.

For existing music, use **Maintenance > Update file source links**. This queues
idle playable tracks in the selected library across all pages. It edits comments
without downloading audio, changing metadata or re-encoding audio. Unexpected
file hash changes are refused. Busy tracks are skipped and can be updated later.
Normal imports, corrections, repairs, origin merges and Spotify links update URLs
through the ordinary pipeline. Rescan Navidrome after file changes.

**Music > More filters > Source metadata applied** shows tracks where source tags
were actually accepted. Edited source-based metadata stays in this group and also
appears under Manually edited metadata. Review this view supports these filters.
The classification is application database state only, never a special audio tag.
The v6 migration backs up SQLite. Existing confirmed identities and trustworthy
finalized source-restore receipts are recognized; uncertain older imports remain
Metadata origin unknown, not inferred merely from an absent recording ID.

## Replacing A and B with All

Add All with monitoring in the same library user and wait for a complete sync.
Identical provider video IDs from A/B reuse the same track/audio and gain All's
playlist membership. Only new videos need ingestion. A different upload of the
same music is a different origin until its identity is confirmed.

Then **Pause monitoring** on A/B to retain their local historical playlists, or
**Remove** those sources to remove their source entries and exported playlists.
Neither action deletes the music, approvals, origins or retained discovery dates.
The online YouTube/Spotify playlists are never edited. Music absent from All is
not automatically deleted. An already queued/running sync may finish after pause;
resuming monitoring queues a fresh sync without redownloading unchanged audio.
