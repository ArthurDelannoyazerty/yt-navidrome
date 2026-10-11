const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {browser} = require('./frontend_harness.cjs');

for (const name of ['refresh', 'playlists', 'matches']) {
  test(`Spotify ${name} ignores delayed data from another library user`, async () => {
    const b = browser(true); b.app.state.user = 'admin';
    const pending = b.spotify[name](); b.app.state.user = 'guest'; b.spotify.reset();
    b.requests[0].resolve(null); await pending;
    assert.equal(b.get('spotifyPlaylists').children.length, 0);
    assert.equal(b.get('spotifyMatches').children.length, 0);
    assert.equal(b.get('spotifyStatus').textContent, 'Loading Spotify connection...');
  });
}
test('Spotify configuration errors are rendered without enabling authorization', async () => {
  const b = browser(true); b.app.state.user = 'admin';
  const pending = b.spotify.refresh();
  b.requests[0].resolve({configured: false, connected: false, configuration_error: 'Configure client ID'});
  await pending;
  assert.equal(b.get('spotifyConnect').disabled, true);
  assert.equal(b.get('spotifyStatus').textContent, 'Configure client ID');
});
test('Spotify link dialog discards old-user library candidates', async () => {
  const b = browser(true); b.app.state.user = 'admin';
  const pending = b.spotify.openLink({id: 'spotify-one', title: 'Artist - Song'});
  b.app.state.user = 'guest'; b.spotify.reset(); b.requests[0].resolve(null); await pending;
  assert.equal(b.get('spotifyCandidates').children.length, 0);
});
test('Spotify audio controls never pretend catalog entries are downloadable', () => {
  const b = browser(true);
  const track = {id:'one', title:'Artist - Song', current_asset_id:null,
    operation_state:'IDLE', origins:[{provider:'spotify', downloadable:0}]};
  assert.equal(b.spotify.controls(track)[0].textContent, 'Link audio');
  assert.equal(b.spotify.controls({...track, current_asset_id:1}).length, 0);
  assert.equal(b.spotify.controls({...track, operation_state:'DEFERRED', operation_kind:'SPOTIFY_AUDIO'})[0].textContent, 'Cancel waiting link');
});
test('Spotify matches are preview-only until the user confirms', async () => {
  const b = browser(true); b.app.state.user = 'admin';
  const pending = b.spotify.matches();
  b.requests[0].resolve({items:[], total:210, unique:200, ambiguous:5, unmatched:5, limit:50, page:1, revision:'revision'});
  await pending;
  assert.equal(b.requests.length, 1);
  assert.equal(b.get('spotifyLinkExact').textContent, 'Link unique matches (200)');
  vm.runInContext('confirm = () => false', b.context);
  await b.get('spotifyLinkExact').listeners.click();
  assert.equal(b.requests.length, 1);
});
test('Spotify UI script is versioned and its DOM IDs all exist', () => {
  const html = fs.readFileSync(require.resolve('../src/static/index.html'), 'utf8');
  const js = fs.readFileSync(require.resolve('../src/static/spotify-ui.js'), 'utf8');
  const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
  for (const match of js.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.has(match[1]), match[1]);
  assert.ok(html.includes('/static/spotify-ui.js?v=__STATIC_VERSION__'));
  assert.ok(html.includes('value="NEEDS_AUDIO"'));
  assert.ok(!js.includes('client_secret'));
});
