# Local graph renderer

`src/static/force-graph.min.js` is the standalone UMD build of [force-graph](https://github.com/vasturiano/force-graph), version **1.51.4**, by Vasco Asturiano, under the MIT license in `src/static/force-graph-LICENSE.txt`.

The pinned upstream distribution is available from `https://cdn.jsdelivr.net/npm/force-graph@1.51.4/dist/force-graph.min.js`. This copy was retrieved from the [Netdisco vendored build](https://github.com/netdisco/netdisco/blob/master/share/public/javascripts/force-graph.min.js), whose version header identifies 1.51.4. It is loaded from the app's local `/static/` directory so graph browsing works with no external CDN at runtime. Verify the upstream version and license before refreshing this file.

The upstream UMD bundle embeds two CSS modules which create inline `<style>` tags.
These were removed from `force-graph.min.js` and their exact styles were
externalized into `src/static/force-graph.css`. This is deliberate: the
application enforces `style-src 'self'` and must not use `unsafe-inline`.
When updating the vendored bundle, repeat the extraction and run both the
strict-CSP real-browser test and frontend contract tests.
