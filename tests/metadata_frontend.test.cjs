/* Run the actual correction dialog callbacks, including debounce and stale replies. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {browser} = require('./frontend_harness.cjs');
const MBID = '11111111-1111-4111-8111-111111111111';
const result = {id: MBID, title: 'Right song', artist: 'Right artist', length_ms: 190000, score: 98,
  disambiguation: 'studio', releases: [{id: '22222222-2222-4222-8222-222222222222', title: 'Correct album', date: '2020'}]};
function setup() {
  const b = browser(false, true); const timers = new Map(); let id = 0;
  b.context.setTimeout = (fn, delay) => { timers.set(++id, {fn, delay}); return id; };
  b.context.clearTimeout = key => timers.delete(key);
  b.timers = timers; b.app.state.user = 'admin';
  vm.runInContext('refreshTracks = async () => {};', b.context);
  return b;
}
async function open(b, pane='search', extra={}) {
  const pending = b.metadata.open({id: 'track', title: 'Wrong artist - Wrong song'}, pane);
  b.requests.at(-1).resolve({track_id: 'track', user_id: 'admin', revision: 'r'.repeat(64),
    current_title: 'Wrong artist - Wrong song', current_mbid: MBID, can_correct: true, origin_count: 1,
    source: {source_title: 'Right artist - Right song', tags: {artist: ['Right artist'], title: ['Right song']},
      basis: 'provider_title_only', captured_at: null}, latest_identification: null, ...extra});
  await pending;
}
async function searchResults(b, data = [result], total=1) {
  const pending = b.metadata.search();
  b.requests.at(-1).resolve({items: data, total, page: 1, limit: 20}); await pending;
}

test('opening defaults to retained source rather than bad applied metadata', async () => {
  const b = setup(); await open(b);
  assert.equal(b.get('mbTitle').value, 'Right song'); assert.equal(b.get('mbArtist').value, 'Right artist');
  assert.match(b.get('sourceMetadataProvenance').textContent, /not archived/);
  assert.equal(b.get('mbApply').disabled, true); assert.equal(b.requests.length, 1);
});

test('typing bursts coalesce to a single 500ms timer and clear previous selection', async () => {
  const b = setup(); await open(b);
  for (const q of ['A', 'Ar', 'Artist']) { b.get('mbArtist').value = q; b.get('mbArtist').listeners.input(); }
  assert.equal(b.timers.size, 1); const timer = [...b.timers.values()][0]; assert.equal(timer.delay, 500);
  assert.equal(b.requests.length, 1); const pending = timer.fn();
  assert.match(b.requests.at(-1).url, /artist=Artist/);
  b.requests.at(-1).resolve({items: [], total: 0, page: 1, limit: 20}); await pending;
});

test('searching and selecting never applies before an explicit confirmation', async () => {
  const b = setup(); await open(b); await searchResults(b);
  await b.get('mbResults').children[0].children[1].listeners.click();
  assert.equal(b.get('mbApply').disabled, false); assert.equal(b.requests.length, 2);
  b.context.confirm = () => false; await b.get('mbApply').listeners.click(); assert.equal(b.requests.length, 2);
  b.context.confirm = () => true; b.get('mbRelease').value = result.releases[0].id;
  const applying = b.get('mbApply').listeners.click();
  const request = b.requests.at(-1); assert.match(request.url, /metadata-correction$/);
  const payload = JSON.parse(request.options.body);
  assert.equal(payload.mode, 'select_recording'); assert.equal(payload.mbid, MBID);
  assert.equal(payload.release_id, result.releases[0].id); assert.equal(payload.revision, 'r'.repeat(64));
  assert.equal(payload.user_id, 'admin'); assert.equal(payload.tags, undefined);
  request.resolve({message: 'Queued'}); await applying;
});

test('source restore is explicit and sends no untrusted replacement tags', async () => {
  const b = setup(); await open(b, 'source');
  assert.equal(b.timers.size, 0);
  const applying = b.get('sourceMetadataApply').listeners.click(); const request = b.requests.at(-1);
  assert.deepEqual(JSON.parse(request.options.body), {user_id: 'admin', mode: 'restore_source', revision: 'r'.repeat(64)});
  request.resolve({message: 'Queued'}); await applying;
});

test('editing during an in-flight search aborts it and discards delayed results', async () => {
  const b = setup(); await open(b);
  const first = b.metadata.search(); const old = b.requests.at(-1);
  b.get('mbTitle').value = 'Different song'; b.get('mbTitle').listeners.input();
  assert.equal(old.options.signal.aborted, true);
  old.resolve({items: [result], total: 1, page: 1, limit: 20}); await first;
  assert.equal(b.get('mbResults').children.length, 0); assert.equal(b.get('mbApply').disabled, true);
});

test('switching user cannot populate or apply another library context', async () => {
  const b = setup(); await open(b); const pending = b.metadata.search(); const request = b.requests.at(-1);
  b.app.state.user = 'guest'; b.metadata.reset();
  request.resolve({items: [result], total: 1, page: 1, limit: 20}); await pending;
  assert.equal(b.get('mbResults').children.length, 0);
  await b.get('mbApply').listeners.click(); assert.equal(b.requests.length, 2);
});

test('artist result opens recordings by artist ID without the old title filter', async () => {
  const b = setup(); await open(b); b.get('mbEntity').value = 'artist'; b.get('mbEntity').listeners.change();
  await searchResults(b, [{id: MBID, name: 'Right artist', type: 'Group'}]);
  await b.get('mbResults').children[0].children[1].listeners.click();
  assert.equal(b.get('mbTitle').value, ''); assert.equal(b.get('mbEntity').value, 'recording');
  const pending = b.metadata.search(); const request = b.requests.at(-1);
  assert.match(request.url, new RegExp('artist_id=' + MBID));
  request.resolve({items: [], total: 0, page: 1, limit: 20}); await pending;
});

test('search pagination invalidates the previously selected recording', async () => {
  const b = setup(); await open(b); await searchResults(b, [result], 42);
  await b.get('mbResults').children[0].children[1].listeners.click();
  const next = b.get('mbNext').listeners.click(); const request = b.requests.at(-1);
  assert.match(request.url, /page=2/); assert.equal(b.get('mbApply').disabled, true);
  request.resolve({items: [], total: 42, page: 2, limit: 20}); await next;
  assert.match(b.get('mbPage').textContent, /^2 \/ 3/);
});

test('Retry-After cooldown blocks immediate repeated searches', async () => {
  const b = setup(); await open(b); const pending = b.metadata.search();
  b.requests.at(-1).resolve({error: 'MusicBrainz cooldown', retry_after: 30}, 429); await pending;
  await b.metadata.search(); assert.equal(b.requests.length, 2);
  b.get('mbTitle').listeners.input();
  assert.ok([...b.timers.values()][0].delay >= 29000);
  b.metadata.reset(); assert.equal(b.timers.size, 0);
});


test('adding correction controls preserves per-action eligibility', () => {
  const b = setup();
  const make = (health, operation) => {
    b.context.fixture = {id: 'x', title: 'Song', health, operation_state: operation,
      discovered_at: null, discovery_basis: 'first_seen', origins: [], playlists: [], choices: []};
    const row = vm.runInContext('renderTrack(fixture)', b.context);
    const buttons = new Map();
    function walk(node) {
      if (!node || typeof node !== 'object') return;
      if (node.tagName === 'button') buttons.set(node.textContent, node);
      for (const child of node.children || []) walk(child);
    }
    walk(row); return buttons;
  };
  let buttons = make('UNAVAILABLE', 'NEEDS_APPROVAL');
  assert.equal(buttons.get('Restore source metadata').disabled, false);
  assert.equal(buttons.get('Edit metadata').disabled, true);
  assert.equal(buttons.get('Technical details').disabled, false);
  assert.equal(buttons.get('Repair tags / loudness').disabled, true);
  buttons = make('AVAILABLE', 'RUNNING');
  assert.equal(buttons.get('Restore source metadata').disabled, true);
  assert.equal(buttons.get('Edit metadata').disabled, true);
  assert.equal(buttons.get('Technical details').disabled, false);
  assert.equal(buttons.get('Delete local copy').disabled, true);
  buttons = make('AVAILABLE', 'IDLE');
  assert.equal(buttons.get('Repair tags / loudness').disabled, false);
});

test('row signature changes when correction script becomes available', () => {
  const b = setup(); b.context.fixture = {id: 'x', title: 'Song'};
  const ready = vm.runInContext('trackSignature(fixture)', b.context);
  b.context.window.MetadataUI = undefined;
  assert.notEqual(vm.runInContext('trackSignature(fixture)', b.context), ready);
});
