# Frontend navigation and module boundaries

## Goal and non-goals

The FastAPI media API, SQLite queues, MusicBrainz cache, beets and audio
recovery are **unchanged**. Navigation is a browser concern. Do not put preview
tokens, album selections, audio mutation requests or other unsaved data into
the address bar.

The frontend remains dependency-free, framework-free and served from
\`src/static/\`. The existing feature screens are retained.

## URL contract

All normal navigation is scoped to a selected library user:

| Content | Address |
| --- | --- |
| Music list | \`/libraries/<user>/music\` |
| Music filters | \`/libraries/<user>/music?status=NEEDS_APPROVAL&q=title&page=2\` |
| Ignored music | \`/libraries/<user>/music/ignored\` |
| Local track details | \`/libraries/<user>/music/<track-id>\` |
| Approve / review a track | \`/libraries/<user>/approval/<track-id>\` |
| MusicBrainz entity | \`/libraries/<user>/catalogue/<kind>/<mbid>\` |
| Album family | \`/libraries/<user>/albums/<release-group-id>\` |
| Specific album edition | \`/libraries/<user>/albums/<release-group-id>/editions/<release-id>\` |
| Source card | \`/libraries/<user>/sources/<source-id>\` |
| Graph selected node | \`/libraries/<user>/graph/<kind>/<mbid>\` |
| Help topic | \`/libraries/<user>/help/<topic>\` |
| Other pages | \`/libraries/<user>/{language,activity,maintenance}\` |

Catalogue supports \`q\`, \`kind\` and \`local=1\` query parameters. The Album
workspace supports \`q\` and \`include_assigned=1\`. Graph supports \`view\`,
\`focus\`, \`q\`, \`kinds\` and \`limit\` as well as selected-node paths.

The root \`/\` stays a valid entry point, including Spotify's
\`?spotify=connected\` callback. After an explicit library selection, navigation
uses the corresponding canonical library URL. URL library names **select a
context**, not an authentication credential. Data access is still verified by
each backend endpoint and the reverse proxy must remain authenticated.

## Code ownership

- \`router.js\` — pure URL parser/builder and history writer. No DOM, API calls
  or domain-specific selection state; Node tests exercise the contract.
- \`navigation.js\` — startup, library-from-URL resolution, Back/Forward and
  direct-route restoration. Runs last, after feature scripts are initialized.
- \`app.js\` — shared application shell, user management, Music list and source,
  activity and maintenance controls. Calls the router only for committed
  navigation actions.
- \`ui-state.js\` — shared async-state and stale-request guards.
- \`metadata-ui.js\`, \`album-ui.js\`, \`catalogue-ui.js\`, \`graph-ui.js\` and
  other \`*-ui.js\` files — their own feature fetches, rendering and transient
  state. Each deep-linkable feature exposes \`openRoute(route)\` while retaining
  its existing public functions.
- \`index.html\` — semantic section links, stable DOM panels and dialogs.
  \`style.css\` retains the existing visual system.
- FastAPI \`main.py\` — serves the **same** versioned shell on validated browser
  routes; \`/api/\` and \`/static/\` remain independent. Browser-shell responses
  are non-cacheable; static assets retain immutable, content-addressed URLs.

Cross-feature navigation should route to the destination rather than rely on a
feature's internal selected item. In particular, opening a Catalogue entity
from Graph should produce the Catalogue entity URL.

## History and restoration

- Top-level tab clicks and item selections create addressable history entries.
- Debounced text search and correction of an invalid page number replace the
  current entry, preventing dozens of history entries per keystroke.
- Music detail is still the same dialog; a direct link opens it. Closing it
  returns to the Music list URL. An item may be opened without being present on
  the current paginated list.
- Album deep links load the requested family first, then the release edition,
  even while the MusicBrainz cache is fetching a tracklist. The URL remains
  stable during refresh.
- Graph node links reload the requested neighborhood (not a stale snapshot);
  browser-only snapshots remain useful when opening the general Graph page.
- Previously remembered library/approval position may only be used when there
  is no explicit library/entry in the URL.
- Switching library discards prior user-scoped feature state and changes the
  canonical URL. Back/Forward may switch the selected library if that user is
  accessible, but never uses another user's data as a silent fallback.
- Album checkbox drafts, manual recording-ID mappings, pending mutations,
  modal confirmations and approval tokens remain temporary state.

## Validation

- \`node --test tests/router.test.cjs\` — route round-trips, encoding, invalid
  paths and history operations.
- \`node --test tests/browser_ui.test.cjs\` — real-browser deep links, refresh,
  Back/Forward, source selection, and existing media workflows.
- \`pytest tests/test_frontend_routes.py\` — FastAPI fallback, CSP, HTML
  no-store, immutable static files and API separation.
- The existing full Python and JavaScript suites must pass before merge.

A future migration may decompose large \`app.js\` and \`style.css\` sections into
domain-specific files, but it should be separate from navigation changes to
avoid mixing media behavior with URL migration.
