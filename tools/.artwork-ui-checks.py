from pathlib import Path

p = Path('src/static/app.js')
s = p.read_text()
old = '    metadata_tools: Boolean(window.MetadataUI),'
assert s.count(old) == 1
s = s.replace(old, old + '\n    artwork_tools: Boolean(window.ArtworkUI),')
p.write_text(s)
p = Path('.github/workflows/docker.yml')
s = p.read_text()
old = 'node --check src/static/metadata-ui.js'
assert s.count(old) == 1
p.write_text(s.replace(old, old + ' && node --check src/static/artwork-ui.js'))
p = Path('tests/browser_ui.test.cjs')
s = p.read_text()
old = '        if (url.pathname === "/api/review/approve") {'
assert s.count(old) == 1
s = s.replace(old, '''        if (url.pathname.endsWith("/artwork") && data.artwork) {
          data.artwork.preference = payload.selection === "auto"
            ? {mode: "auto", candidate_id: "cover-square", framing: "original"}
            : payload.selection ? {mode: "manual", candidate_id: payload.selection, framing: payload.framing}
            : data.artwork.preference;
          data.artwork.revision = String(data.writes.length % 10).repeat(64);
        } else if (url.pathname === "/api/review/approve") {''')
old = '      else if (url.pathname === "/api/sources") result = [];'
assert s.count(old) == 1
s = s.replace(old, '''      else if (url.pathname.endsWith("/artwork") && data.artwork) result = data.artwork;
      else if (/\\/artwork\\/[^/]+\\/image$/.test(url.pathname)) {
        response.setHeader("Content-Type", "image/png");
        response.end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"));
        return;
      }
      else if (url.pathname === "/api/sources") result = [];''')
s += '''

test("real browser: per-track cover comparison, pin, auto-selection and mobile layout", {timeout: 60000}, async t => {
  const chrome = await browser(t);
  if (!chrome) return;
  const data = fixtures();
  const image = id => `/api/tracks/track-1/artwork/${id}/image?user_id=admin`;
  data.artwork = {track_id: "track-1", title: "Artwork review fixture", revision: "d".repeat(64),
    preference: null, can_edit: true, working: false, current_image_url: image("current"), items: [
      {id: "cover-square", label: "Square source cover", source_type: "youtube", width: 600, height: 600,
        recommended_framing: "original", explanation: "Square artwork; 600 x 600", image_url: image("cover-square")},
      {id: "cover-wide", label: "Landscape source cover", source_type: "youtube", width: 1280, height: 720,
        recommended_framing: "original", explanation: "Original proportions; 1280 x 720", image_url: image("cover-wide")},
    ]};
  const origin = await fixtureServer(t, data);
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false});
  await chrome.send("Page.navigate", {url: origin});
  await chrome.until("document.querySelectorAll('#tracks tr[data-key]').length===1 && window.ArtworkUI");
  await chrome.evaluate("document.querySelector('#tracks .action-menu summary').click(); [...document.querySelectorAll('#tracks button')].find(b=>b.textContent==='Artwork...').click()");
  await chrome.until("document.querySelector('#artworkDialog').open && document.querySelectorAll('#artworkCandidates input').length===2");
  assert.equal(await chrome.evaluate("document.querySelector('#artworkApply').disabled"), true);
  await chrome.evaluate("document.querySelector('#artworkCandidates input[value=cover-wide]').click(); document.querySelector('#artworkFraming').value='fit'; document.querySelector('#artworkFraming').dispatchEvent(new Event('change'))");
  assert.equal(await chrome.evaluate("document.querySelector('#artworkPreview').src.includes('framing=fit')"), true);
  await chrome.screenshot("artwork-gallery-desktop");
  await chrome.evaluate("document.querySelector('#artworkApply').click(); document.querySelector('#artworkApply').click()");
  await chrome.until("document.querySelector('#artworkMode').textContent.includes('Pinned') && !document.querySelector('#artworkAuto').disabled");
  const saves = data.writes.filter(write => write.path.endsWith('/artwork'));
  assert.equal(saves.length, 1); assert.equal(saves[0].body.selection, "cover-wide"); assert.equal(saves[0].body.framing, "fit");
  assert.equal(data.tracks[0].operation_state, "NEEDS_APPROVAL");
  await chrome.evaluate("document.querySelector('#artworkAuto').click()");
  await chrome.until("document.querySelector('#artworkMode').textContent.includes('Automatic') && !document.querySelector('#artworkRefresh').disabled");
  assert.equal(data.writes.at(-1).body.selection, "auto");
  await chrome.evaluate("window.ArtworkUI.open({id:'track-1',title:'Fixture'})");
  await chrome.until("document.querySelector('#artworkDialog').open && document.querySelectorAll('#artworkCandidates input').length===2 && !document.querySelector('#artworkRefresh').disabled");
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await chrome.evaluate("(() => {const d=document.querySelector('#artworkDialog'),r=d.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && d.scrollWidth<=d.clientWidth+1;})()"), true);
  await chrome.screenshot("artwork-gallery-mobile");
  await chrome.evaluate("document.querySelector('#artworkClose').click()");
  await chrome.until("!document.querySelector('#artworkDialog').open");
  assert.deepEqual(chrome.errors, []);
});
'''
p.write_text(s)
p = Path('README.md')
p.write_text(p.read_text() + '''

## Portable source links and individual track covers

COMMENT and DESCRIPTION retain the discovery date and list the associated
canonical YouTube video and Spotify track URLs. **Maintenance > Update file source
links** backfills idle playable files without downloading or re-encoding audio.
**Source metadata applied** is a monitoring-database filter, not a new Navidrome tag.
Older imports without reliable provenance remain explicitly unknown.

**More > Artwork...** provides automatic YouTube fallback, cached alternatives,
framing previews, uploaded covers, and persistent manual pins. Each song can have
its own image, including songs in the same album. **Maintenance > Fill missing
covers** queues eligible files with no embedded picture and keeps existing images.
No Bandcamp metadata provider or new artist naming system is introduced.

To replace tier playlists A/B with All, monitor All in the same library and wait
for a successful complete sync first. The same video IDs are reused; only new
videos need ingestion. Then pause A/B monitoring to keep historical local playlist
exports, or remove those sources to remove the exports while retaining the music,
origins and discovery history. Online playlists are never edited. Rescan Navidrome
after file-tag or artwork changes. See [Source links and playlists](docs/source-links-and-playlists.md)
and [Track artwork](docs/track-artwork.md).
''')
