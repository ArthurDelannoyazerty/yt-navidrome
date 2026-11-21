# MusicBrainz catalogue, graph and safe album changes

This version is based on PR #26 and the explicit-state-machine refactor.
The original **Music** and **Approval** tabs remain available.

## Catalogue

- Use **Catalogue → Search MusicBrainz** to find an artist, recording, work,
  release group, or release. Local results do not require a network request.
- Select an entity to see its cached details and links. **Refresh** queues
  paginated MusicBrainz requests through the common rate limiter.
- An explicitly requested artist sync includes album families (release groups),
  credited recordings, works, and direct artist metadata. After complete browse
  pages, it incrementally queues the album editions and their full tracklists,
  including music not owned by any user. This can take hours for large discographies
  and always follows the shared API limiter; no synchronous crawl is required.
- The separate `musicbrainz-catalogue.sqlite` contains public metadata shared
  across users, **not** an assertion that any user owns the music. The
  importer database retains user-scoped identity approvals, source attribution,
  and album preferences.
- Each entity and relationship has an updated/check timestamp. A partial or
  interrupted paginated browse does not replace the previous completed scope.
  A staleness threshold makes data *eligible* for a user-triggered refresh;
  it never triggers an unbounded background crawl of the entire discography.

## Graph

Open **Graph** and select **Downloaded library** or **Cached catalogue**.
Independently toggle **Recordings**, **Artists**, **Releases**, **Album families**,
**Works**; all unchecked means an empty visualization. Artist-only and
release-only views can still reach their nodes through hidden recording nodes.
Filter by a name, or focus on one MusicBrainz MBID to expand nearby relationships.
The graph reports the total selectable nodes in the *current bounded-depth
neighborhood*, independently of the display limit. Enter an integer from 1 to
that total (up to a 10,000-node server safety ceiling); the input is clamped
when the current view or filters change. The number is not the total count of
all MusicBrainz entities in the database. Public catalogue nodes do not imply
ownership, and downloaded views cannot reveal another library user's music.

The Graph tab uses a locally vendored 2D Canvas renderer (force-graph 1.51.4,
MIT). **Free physics** is an unconstrained spring layout; **Soft layers** pull
nodes toward breadth-first graph distances within each connected component,
not fixed entity-type columns. Five high-contrast node colors, with an explicit
legend, distinguish entity types; a gold ring marks downloaded recordings.
Both modes support wheel/pinch zoom, dragging, panning, fit-to-view, focus
selection, and progressive labels. The Obsidian-style force controls are Center
force, Repel force, Link force and Link distance; they update D3-based forces
live. The simulation cools automatically, and pauses while the tab is hidden.
A browser-local snapshot saves the most recent graph, positions, viewport and
controls per library user (IndexedDB with localStorage fallback). Reloading the
page reuses it without re-fetching; **Explore** explicitly refreshes catalogue
data. Browser storage is local to the current origin and may be cleared by the
browser; it never changes the server catalogue. The node selector provides
keyboard-accessible navigation and retains the Catalogue and neighborhood
expansion actions.

## Album organization

The identity of a MusicBrainz **recording** is approved manually, independently
of its album edition. A recording may appear on multiple editions/releases;
no first-match heuristic is accepted as proof of the correct album.

Open the dedicated **Album** tab, select local recordings (optionally including
recordings assigned to other albums and sources), and compare releases. **Compare album candidates** uses cached
recording-to-release links. Coverage counts show compatibility, not confidence;
a tiny two-song set may be ambiguous and a large source may mix several albums.

A release must have its complete cached tracklist before a preview can apply it.
The preview explicitly distinguishes `COMPATIBLE`, `UNCHANGED`, `INCOMPATIBLE`,
`BUSY`, `UNAVAILABLE`, and `NEEDS_STABLE_PATH_MIGRATION`. A fresh token is
required to queue edits. Album edits are durable, copy-on-write, metadata-only,
retain the original audio and tags for undo, and keep the path, recording MBID,
and source memberships unchanged. The old and new SHA-256 digests are recorded.

An album preference can be stored for a **source**, but automatic use for future
compatible recordings is **off by default**. The user must opt in explicitly;
conflicting source preferences are not resolved by guessing. Existing approved
songs are never retagged by changing a preference or obtaining a new match.

Physical duplicate merging by AcoustID is deliberately **not included**.

## NixOS stable-path migration

**Do not execute before you have successfully built and deployed an image that
contains `tools/stable_path_migrate.py`.** This migration is an explicit
maintenance operation; it is not part of the database schema upgrade.

The paths supplied in the NixOS configuration are:

- Importer state on the host: `/var/lib/services/navidrome-importer`, container
  `/data` (actual SQLite state in `/data/state`).
- Music on the host: `/mnt/storage-4tb/media/music`, container `/data/library`.
- Container name: `yt-navidrome`, API port `8009` on the host.

**Before deployment:** back up the importer state, all music, and the Navidrome
server database. Use a separate disk and verify backups can be read. Never
copy a live SQLite database without stopping the application or using its
SQLite backup API. Do not infer Navidrome's database host mount from the
importer configuration: inspect the Navidrome container mounts first.

1. Stop the importer and Navidrome; prevent any other file writers and scanners
   from touching the mounted music directory. Ensure there are no `RUNNING`
   importer jobs to recover first.
2. Run the new image **one time** as a maintenance container with the **same
   UID/GID and bind mounts** as the regular importer, but override the command
   to use `python /app/tools/stable_path_migrate.py plan`. Example of the command
   **inside** the maintenance container:

   ```sh
   python /app/tools/stable_path_migrate.py plan
   ```

3. Review every source and destination, ensure there are no conflicts, and
   confirm free space, backup integrity, and a restorable Navidrome database.
   The migration refuses mismatched hashes, missing beets records and symlinks.
4. Begin with a **small canary migration**, while both services are stopped:

   ```sh
   python /app/tools/stable_path_migrate.py apply --music-backed-up --canary-count 3
   ```

   If more than three files need moving, this moves **only three** into the
   `audio/<track-id>.opus` layout, without changing audio bytes or tags, then
   stops at `AWAITING_CANARY_SCAN`. The journal and importer/beets SQLite
   snapshots make interrupted execution resumable.
5. Start Navidrome **alone** and force a scan. Before moving the rest, verify
   the canary songs have the **same Navidrome song IDs**, playlist memberships,
   ratings/favorites and play counts. If anything changed unexpectedly, restore
   backups and investigate before continuing. Then acknowledge the canary:

   ```sh
   python /app/tools/stable_path_migrate.py ack --navidrome-verified
   ```

6. Stop Navidrome again and migrate the remaining files:

   ```sh
   python /app/tools/stable_path_migrate.py apply --music-backed-up
   ```

   Run a second Navidrome-only scan and verify the entire library. Only after
   this second checkpoint acknowledge completion:

   ```sh
   python /app/tools/stable_path_migrate.py ack --navidrome-verified
   ```

   If fewer than four files need migrating, the canary step moves them all and
   the first acknowledgement completes the migration. The importer refuses to
   start while either scan checkpoint is pending.

7. Restart the importer and allow normal sync. New and migrated approved files
   now share the same stable `audio/<id>.opus` path scheme. The dedicated
   `Waiting for approval` directory is temporary and not a legacy approved
   album path; it is cleaned up when that music is approved.

**Important:** The `ack` flag records your verification; the tool does not
query the Navidrome server database to prove that song IDs and personal
playback state remained stable. Navidrome/Feishin continuity must be checked
against your actual server version and settings. The migration is not proven
safe for production solely by importer unit tests.

Never reformat or modify album tags at the same time as the move-only scan.
Do not enable bulk album edits until the Navidrome checkpoint is complete.

### Connected album-cluster workspace

The Album tab uses one workspace per MusicBrainz **release group** (album
family), not transitive connected components. A downloaded recording may
appear in multiple families (for example, both an album and a compilation)
without merging those families; track artists and featured artists never
create family edges. Cached release-group edition browsing discovers sibling
releases and prioritizes the currently focused family. Unknown recording
relationships remain standalone until indexed.

All downloaded songs belonging to a family stay visible, even if already
assigned. The checkbox controls whether previously assigned recordings
are eligible for **changes**, not whether they appear in album context.
Searching a song title finds its family workspaces. These are navigation and
comparison views, **not** instructions to tag every song with one album.

The three desktop columns scroll independently: cluster navigator, candidate
edition list, and the selected edition's **complete official MusicBrainz
tracklist** plus other local cluster recordings. Original and available English
titles are displayed together. Tracklists are fetched automatically through
the persistent, rate-limited Catalogue queue. Missing metadata remains
neutral/unknown until the full release has been cached; no manual Sync button
is needed. The focused cluster's releases have higher queue priority.

Six subdued exact-membership colors plus a separate unverified-candidate
state describe local assignment. A sibling edition may contain a different
recording MBID for an identically titled song. Such a local copy is displayed
as a read-only **possible recording match** when its normalized title and
available duration agree; track-position evidence is shown where known.
It is not green/orange, is never selected for tagging automatically, and
requires an explicit correction of recording identity or independently
verified album membership before assignment.

Six exact-membership colors describe *exact release membership* and local assignment:
- **Green:** downloaded, on this release, and unassigned or already assigned
  to it. Checked by default.
- **Orange:** downloaded, on this release, but currently assigned to another
  album. Checked by default; uncheck to avoid that particular move.
- **Red:** no exact-MBID local recording for this official track. It may still
  have a differently identified local copy. Unchecked/disabled.
- **Gray:** album-locked recording. Unchecked/disabled.
- **Purple:** local cluster song missing from the selected release and
  assigned to another album. Unchecked/disabled.
- **Blue:** local cluster song missing from the release and unassigned.
  Unchecked/disabled; may be individually reviewed as No Album Found.

**Green tracks only** is a display-only filter. Orange selections stay selected
when hidden; the confirmation dialog explicitly counts any hidden orange moves.
The compact legend stays below the internally scrolling music cards.

**Validate Album** replaces the previous persistent assignment-plan card.
It previews the currently checked *eligible* songs, including any moves from
other releases and per-album before/after counts, in a small modal. Files are
not touched until the user confirms. The existing backend rechecks exact
release membership, album locks, file checksums, current job state and undo
backup space before queuing reversible album-only changes. Cluster-only songs
remain for another edition, and unchecked songs are untouched. An albumless
song can still be marked No Album Found individually without a fake audio tag.

If a local recording is already assigned to a release whose **fully fetched**
official tracklist does not include its MusicBrainz recording ID, this rare
contradiction is deliberately **not** mixed into Album. Catalogue instead has
a collapsed "Review album metadata mismatches" section listing only verified
cases from the current user's library, with a direct link to that song's
Music details. The user can correct the album manually and use the per-track
album lock to prevent unintended future reassignment.

History and per-song undo remain available. Advanced source-specific album
preferences still apply only to future matching recordings. Album artwork
continues to be managed by the separate Artwork tool.
