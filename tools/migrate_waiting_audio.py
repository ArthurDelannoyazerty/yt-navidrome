#!/usr/bin/env python3
"""Move legacy unapproved music into audio/<track-id>.opus, preserving SQLite.

Offline, restart-safe and move-only: no metadata, audio packets or approval
decisions are changed. Requires the importer and Navidrome to be stopped.
Back up the music volume and Navidrome DB separately before applying.

    python tools/migrate_waiting_audio.py plan
    python tools/migrate_waiting_audio.py apply --music-backed-up --canary-count 3
    # Start Navidrome alone, scan and check songs/favorites/playlists.
    python tools/migrate_waiting_audio.py ack --navidrome-verified
    # Stop Navidrome again, then move the rest.
    python tools/migrate_waiting_audio.py apply --music-backed-up
    # Start Navidrome alone, scan and verify again.
    python tools/migrate_waiting_audio.py ack --navidrome-verified

Options --state-dir and --library-dir are identical to stable_path_migrate.py.
A separate waiting-audio-migration.json journal must reach COMPLETE before
starting the ingestor again. Plan/status are read-only.
"""
from __future__ import annotations

import sys

from stable_path_migrate import main


if __name__ == "__main__":
    main([*sys.argv[1:], "--waiting-only"])
