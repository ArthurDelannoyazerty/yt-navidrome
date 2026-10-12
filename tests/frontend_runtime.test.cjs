/* Exercise the real browser callbacks with delayed responses, no npm packages. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {RequestGate, resultsLabel} = require('../src/static/ui-state.js');

const {browser} = require('./frontend_harness.cjs');

test('latest response wins within a request scope', () => {
  const gate = new RequestGate();
  const first = gate.begin('tracks'); const last = gate.begin('tracks');
  assert.equal(first(), false); assert.equal(last(), true);
  assert.equal(gate.begin('events')(), true); assert.equal(last(), true);
});
test('reset invalidates all old-user tickets, including matching sequence numbers', () => {
  const gate = new RequestGate(); const old = gate.begin('events');
  gate.reset(); const fresh = gate.begin('events');
  assert.equal(old(), false); assert.equal(fresh(), true);
});
test('result counts describe filtering separately from global totals', () => {
  assert.equal(resultsLabel(2, 50, 163, 2557), 'Showing 51\u2013100 of 163 matching tracks. Library total: 2,557.');
  assert.equal(resultsLabel(1, 50, 0, 2557), 'Showing 0\u20130 of 0 matching tracks. Library total: 2,557.');
});
for (const name of ['loadEvents', 'loadIntegrity', 'loadIgnored', 'showDetails']) {
  test(`${name} discards an old user's delayed response`, async () => {
    const b = browser(); b.app.state.user = 'admin'; b.get('issueKind').value = 'ALL';
    const pending = b.app[name]({id: 'old-track'});
    b.app.state.user = 'guest'; b.app.state.generation += 1;
    // An invalid body is intentional: no old response may be interpreted at all.
    b.requests[0].resolve(null); await pending;
    assert.equal(b.app.state.after, 0);
    assert.equal(b.app.state.events.length, 0);
    assert.equal(b.get('detailsTitle').textContent, '');
    assert.equal(b.get('integritySummary').textContent, '');
  });
}
test('overlapping Activity requests neither duplicate events nor regress the cursor', async () => {
  const b = browser(); b.app.state.user = 'admin';
  const a = b.app.loadEvents(), z = b.app.loadEvents();
  const events = [1, 2].map(id => ({id, at: 'now', level: 'INFO', message: 'test'}));
  b.requests[1].resolve(events); await z;
  b.requests[0].resolve(events); await a;
  assert.equal(b.app.state.after, 2);
  assert.equal(b.app.state.events.length, 2);
  assert.equal(b.get('logs').children.length, 2);
});
test('older page response cannot replace the newer page', async () => {
  const b = browser(); b.app.state.user = 'admin';
  vm.runInContext('updateStats = () => {}; updateKeyedChildren = () => {};', b.context);
  const a = b.app.refreshTracks(); b.app.state.page = 2; const z = b.app.refreshTracks();
  b.requests[1].resolve({page: 2, limit: 50, total: 120, stats: {total: 2557}, tracks: []}); await z;
  b.requests[0].resolve({page: 1, limit: 50, total: 120, stats: {total: 2557}, tracks: []}); await a;
  assert.match(b.get('page').textContent, /^2 \/ 3/);
  assert.match(b.get('resultsSummary').textContent, /51\u2013100/);
});
test('Retry all uses FAILED bulk endpoint and is not limited by UI page or search', async () => {
  const b = browser(); b.app.state.user = 'admin'; b.app.state.page = 20; b.get('search').value = 'one song';
  vm.runInContext('refreshAll = async () => {};', b.context);
  const pending = b.get('retryAll').listeners.click();
  assert.equal(b.requests[0].url, '/api/batch');
  assert.deepEqual(JSON.parse(b.requests[0].options.body), {user_id: 'admin', mode: 'retry'});
  assert.equal(b.app.state.batchPending, true);
  b.requests[0].resolve({message: 'Queued 200 tracks', skipped: 0}); await pending;
  assert.equal(b.app.state.batchPending, false);
});
test('operation dialog remains usable if polling removed its row from the page map', async () => {
  const b = browser(); b.app.state.user = 'admin';
  b.get('operationTrackId').value = 'off-page'; b.get('operationForm').dataset.user = 'admin';
  b.get('operationMode').value = 'redownload'; b.get('operationOrigin').value = 'origin';
  vm.runInContext('globalThis.sent = null; action = async (...args) => { globalThis.sent = args; };', b.context);
  await b.get('operationForm').listeners.submit({preventDefault() {}});
  assert.equal(b.context.sent[0].id, 'off-page');
  assert.equal(b.context.sent[0].user_id, 'admin');
  assert.equal(b.context.sent[1], 'redownload');
});

test('origins show isolated detach/ignore controls with a current-audio guard', async () => {
  const b = browser(); b.app.state.user = 'admin';
  const pending = b.app.showDetails({id: 'merged'});
  b.requests[0].resolve({id:'merged',user_id:'admin',title:'LORN - Perfekt Dark',
    operation_state:'IDLE',current_origin_id:'good',origins:[
      {id:'good',provider:'youtube',title:'Song',url:'https://youtu.be/MSb_31SUEXE'},
      {id:'bad',provider:'youtube',title:'Full album',url:'https://youtu.be/X3azfIj_JW8'}],
    assets:[{origin_id:'good',state:'CURRENT',path:'/library/song.opus'}],identity_history:[],file_tags:{}});
  await pending;
  const originEntries = b.get('detailsBody').children[0].children[2].children[1].children;
  const current = originEntries[0].children.at(-1).children;
  const incorrect = originEntries[1].children.at(-1).children;
  assert.equal(current[0].textContent,'Detach origin');
  assert.equal(current[0].disabled,true);
  assert.equal(current[1].disabled,true);
  assert.equal(incorrect[0].disabled,false);
  assert.equal(incorrect[1].disabled,false);
  assert.equal(incorrect[1].textContent,'Remove & ignore origin');
});

test('origin removal requires confirmation and is scoped to selected user/URL', async () => {
  const b = browser(); b.app.state.user = 'admin';
  const track = {id:'merged',user_id:'admin'};
  const origin = {id:'bad',provider:'youtube',title:'Full album'};
  vm.runInContext('refreshTracks = async () => {};', b.context);
  b.context.confirm = () => false;
  await vm.runInContext('manageTrackOrigin', b.context)(track, origin, 'ignore');
  assert.equal(b.requests.length,0);
  b.context.confirm = () => true;
  const pending = vm.runInContext('manageTrackOrigin', b.context)(track, origin, 'ignore');
  assert.equal(b.requests.length,1);
  assert.match(b.requests[0].url,/\/api\/tracks\/merged\/origins\/bad\/ignore$/);
  assert.deepEqual(JSON.parse(b.requests[0].options.body),{user_id:'admin'});
  b.requests[0].resolve({message:'Origin removed and ignored',playlist_refresh_failed:[]});
  await pending;
  assert.equal(b.get('notice').textContent,'Origin removed and ignored');
  b.app.state.user = 'guest';
  await assert.rejects(() => vm.runInContext('manageTrackOrigin', b.context)(track, origin, 'ignore'), /Library changed/);
  assert.equal(b.requests.length,1);
});
