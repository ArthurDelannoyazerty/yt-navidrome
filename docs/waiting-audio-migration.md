# Move existing unapproved music to stable `audio/<track-id>.opus` paths

New imports publish at their permanent `audio/<track-id>.opus` path. The
`Waiting for approval` grouping is an **album tag**, not a folder. The
original candidate used to identify/approve each track stays in
`/data/state/staging` and is not changed by this migration.

**This migration is for already published unapproved music created by older
versions. Do not manually `mv` these files**: the importer tracks paths in
`ingestor.sqlite` (tracks + assets), Beets' per-user `library.db`, and
generated `.m3u` playlists.

## Before starting

- Deploy an image containing `tools/migrate_waiting_audio.py`; don't run it
  from an older container.
- Make **verified, restorable backups** of the entire music volume, importer
  state, per-user Beets databases and **Navidrome's database**. The script
  creates its own SQLite snapshots, but **does not back up Navidrome or audio**.
- Stop **both** `yt-navidrome` and **Navidrome**, and stop any other writers.
  A running importer will refuse the migration. The move runs on the same
  filesystem; cross-device moves are not supported.
- Run the maintenance container using the **same mounted paths and UID/GID**
  as the normal importer, but with the command overridden to invoke Python.
  Inside that maintenance container, the default paths are
  `/data/state` and `/data/library`.

## Commands inside the maintenance container

First inspect the move plan (no files or databases are modified):

```sh
python /app/tools/migrate_waiting_audio.py plan
```

Move **up to three** unapproved audio files, still with unchanged tags and SHA-256:

```sh
python /app/tools/migrate_waiting_audio.py apply --music-backed-up --canary-count 3
```

Start **Navidrome only** and rescan. Check that those three songs still have
the same Navidrome IDs, play counts, favorites, ratings and playlist memberships.
If a song reappears as a different song or is missing, **do not acknowledge**;
investigate or restore your backups. When verified, stop Navidrome and run:

```sh
python /app/tools/migrate_waiting_audio.py ack --navidrome-verified
```

Move all remaining planned unapproved tracks:

```sh
python /app/tools/migrate_waiting_audio.py apply --music-backed-up
```

Start Navidrome alone, rescan again and check your library before the final
acknowledgement:

```sh
python /app/tools/migrate_waiting_audio.py ack --navidrome-verified
```

You can check the state at any time while the importer is stopped:

```sh
python /app/tools/migrate_waiting_audio.py status
```

Only when the state is `COMPLETE` should the ingestor be started again.
The migration has a separate
`/data/state/waiting-audio-migration.json` resume journal. The application
refuses to start while this migration is awaiting verification.

## What changes, and what does not

For every eligible **currently referenced** unapproved asset in the legacy
folder, the tool validates its existing SHA-256, the destination, and the Beets
item path. It renames the Opus file into the existing stable audio directory,
updates **both** importer tables (`tracks.file_path`, `assets.path`), changes
the matching Beets `items.path`, and rewrites exported playlists. It creates
SQLite backups and logs each completed move in the journal. Interrupted moves
can be resumed without repeating a successful filesystem rename.

It does **not** change the music's tags, artist, album, recording ID, source,
approval selection, pending candidate, or audio bytes. It never overwrites an
occupied destination, blindly accepts a changed hash, deletes unrelated
files, or moves an unreferenced `*.opus` file.

`Waiting for approval` directories containing only old covers or orphaned
files are not automatically removed; verify them separately. The need for
Navidrome verification is real: even a move with unchanged tags may cause a
different Navidrome song ID on some deployments.

The original approved-music migration
(`tools/stable_path_migrate.py`) remains separate and unchanged by this
journal; its completed state is not overwritten.
