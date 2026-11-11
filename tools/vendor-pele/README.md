# Pele flowchart bundle

The Help tab loads `src/static/pele-flowchart.js` from this application only when
opened. The asset contains [Pele](https://github.com/obsidianmd/pele) **1.13.0**,
its core browser API and the flowchart renderer, with no other diagram types or
runtime dependencies. `pele-LICENSE.txt` preserves the upstream MIT license.

Rebuild from the repository root with Node 20 or later:

```sh
cd tools/vendor-pele
npm ci
npm run build
```

Both Pele and esbuild are pinned; the lockfile records package integrity hashes.
The entry point exports `mount` and `render` as `window.IngestorPele`.
Production and Docker do not need npm, an external CDN, or a JavaScript build step.

The application keeps `style-src 'self'`. `help-ui.js` strips the SVG's inline
styles before mounting; `style.css` supplies the matching Pele root variables,
font and responsive sizing. App-styled zoom buttons call Pele's zoom API with its
inline-styled toolbar disabled. Keep these defaults in sync when upgrading Pele.

After changing versions, rebuild and run the frontend tests. Inspect the Help
diagram on both desktop and mobile because layout is part of the renderer API.

Upstream documentation:

- [API and individual diagram imports](https://pele.run/api)
- [CSS variable theming](https://pele.run/theming)
- [Mermaid syntax compatibility](https://pele.run/compatibility)
