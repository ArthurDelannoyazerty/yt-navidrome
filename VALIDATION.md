# MusicBrainz catalogue, album, graph and migration validation

Source baseline: PR #26 head `d6e9db541092d464024e9bc998538d8affeb08ea`
plus the local explicit-state-machine refactor. PRs #25 and #26 have already
been merged into GitHub `main`; these later changes are local and **have not
been pushed, merged, deployed or applied to production**.

## Local validation (2026-11-17)

| Check | Result |
| --- | --- |
| Python available-suite (pinned-beets tests excluded) | **567 passed, 6 skipped, 10 deselected** |
| Focused catalogue, graph, album and stable migration tests | **21 passed** |
| Node frontend tests including Graph toggles | **74 passed** |
| Compilation of Python modules and syntax of authored JS | Passed |
| `git diff --check` | Passed |
| Real-browser test | **Blocked by the managed Chromium browser on `127.0.0.1`** |
| Live Navidrome track IDs, playlist and rating retention | **Not tested** |

The ten deselections require the pinned beets runtime and failed when run
without it (`ModuleNotFoundError: No module named 'beets'`), rather than due
to an application assertion. Six tests explicitly skip for the same missing
dependency. These are **not passing beets integration tests**. GitHub CI must
install pinned dependencies and run the entire test suite before deployment.

The local browser test attempted navigation but Chromium displayed:
`Your organization doesn't allow you to view this site` for `127.0.0.1`.
This is an environment block, not a verified browser interaction.

## Commands for complete CI verification

```sh
uv sync --frozen --extra test
uv run python -m compileall -q src tools tests
uv run pytest
node --test tests/frontend_runtime.test.cjs tests/spotify_frontend.test.cjs \
  tests/metadata_frontend.test.cjs tests/catalogue_frontend.test.cjs \
  tests/help_frontend.test.cjs
REQUIRE_BROWSER=1 node --test tests/browser_ui.test.cjs
```

The migration's isolated tests exercise stable paths, unchanged file hashes,
beets and importer SQLite references, canary acknowledgement, collision refusal,
crash/restart recovery, and importer startup refusal during migration.

**Do not use these tests as proof of Navidrome data retention.** Back up importer
state, actual music, and the Navidrome database. First move only the canary
subset, scan Navidrome alone, verify the original song IDs and personal state,
and only then move the rest. Run a second verification before acknowledging the
migration and restarting the importer. See `docs/catalogue-graph-albums.md`.
