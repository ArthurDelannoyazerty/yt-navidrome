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
  assert.match(b.get('page').textContent, /^Page 2 of 3/);
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
  assert.equal(current[0].textContent,'Separate track');
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

test('Maintenance can unblock a MusicBrainz identity without restoring ignored source URLs', async () => {
  const b = browser(); b.app.state.user = 'admin';
  const entry = {id:'ignore-group',title:'LORN - Full album',mbid:'420c95c6-eb2c-4d09-b60c-ba12b1cb164c',
    origins:[{provider:'youtube',title:'Full album',media_key:'X3azfIj_JW8'}]};
  const item = vm.runInContext('renderIgnored', b.context)(entry);
  const buttons = item.children[0].children;
  assert.equal(buttons[1].textContent, 'Allow recording (keep URLs ignored)');
  assert.equal(buttons[2].textContent, 'Restore all (identity + URLs)');
  b.context.confirm = () => false;
  await buttons[1].listeners.click();
  assert.equal(b.requests.length, 0);
  b.context.confirm = () => true;
  const pending = buttons[1].listeners.click();
  assert.equal(b.requests[0].url, '/api/ignored/ignore-group/allow-recording');
  assert.deepEqual(JSON.parse(b.requests[0].options.body), {user_id:'admin'});
  b.requests[0].resolve({message:'Identity allowed; 1 source URL stays ignored'});
  for (let i = 0; i < 12 && !b.requests[1]; i++) await Promise.resolve();
  assert.equal(b.requests[1].url, '/api/ignored?user_id=admin');
  b.requests[1].resolve([]);
  await pending;
  assert.match(b.get('notice').textContent, /Identity allowed/);
});

test('Remove dialog offers unique honest scopes and a single menu entry', () => {
  const b = browser(); b.app.state.user = 'admin';
  const origin = {provider:'youtube',url:'https://youtu.be/abc',downloadable:true};
  const make = (mbid,origins) => ({id:'song',user_id:'admin',title:'Song',mbid,
    health:'AVAILABLE',operation_state:'IDLE',origins,playlists:[],choices:[]});
  const show = vm.runInContext('showRemoval', b.context);
  show(make(null,[origin]));
  assert.deepEqual(b.get('removeChoices').children.map(c=>c.children[0].value), ['delete','delete_ignore_urls']);
  show(make('recording-id',[origin]));
  assert.deepEqual(b.get('removeChoices').children.map(c=>c.children[0].value), ['delete','delete_ignore_urls','delete_ignore']);
  show(make('recording-id',[origin,{...origin,url:'https://youtu.be/def'}]));
  assert.deepEqual(b.get('removeChoices').children.map(c=>c.children[0].value), ['delete','delete_ignore']);
  const row = vm.runInContext('renderTrack', b.context)(make(null,[origin]));
  const menu = row.children[3].children[0].children.at(-1).children[1];
  assert.equal(menu.children.filter(c=>c.textContent==='Remove…').length,1);
  assert.equal(menu.children.some(c=>c.textContent==='Restore source metadata'),false);
});

test('Activity follows after a hidden load, pauses for reading and resumes on demand', async () => {
  const b = browser(); b.app.state.user='admin';
  const activity=b.get('activityPanel'); activity.dataset.panel='activity'; activity.hidden=true;
  b.context.document.querySelectorAll = selector => selector==='[data-panel]' ? [activity] : [];
  const logs=b.get('logs'); let position=0;
  Object.defineProperties(logs, {
    clientHeight:{get:()=>activity.hidden?0:480},
    scrollHeight:{get:()=>activity.hidden?0:Math.max(480,logs.children.length*20)},
    scrollTop:{get:()=>position,set:value=>{position=Math.min(Math.max(0,value),Math.max(0,logs.scrollHeight-logs.clientHeight));}},
  });
  const events=Array.from({length:100},(_,i)=>({id:i+1,at:'now',level:'INFO',message:'event'}));
  let pending=b.app.loadEvents(); b.requests.at(-1).resolve(events); await pending;
  vm.runInContext("setTab('activity')", b.context);
  assert.equal(logs.scrollTop,1520);
  logs.scrollTop=100; logs.listeners.scroll();
  pending=b.app.loadEvents(); b.requests.at(-1).resolve([{...events[0],id:101}]); await pending;
  assert.equal(logs.scrollTop,100); assert.equal(b.app.state.unseenEvents,1);
  assert.equal(b.get('jumpLatest').hidden,false);
  b.get('jumpLatest').listeners.click();
  assert.equal(logs.scrollTop,1540); assert.equal(b.app.state.followLogs,true);
});

test('Ignored music is a Music view and leaves the active track filter intact', async () => {
  const b=browser(); b.app.state.user='admin'; b.get('filter').value='NEEDS_APPROVAL';
  vm.runInContext('updateKeyedChildren=()=>{};',b.context);
  vm.runInContext("setFilter('IGNORED')",b.context);
  assert.equal(b.get('musicResults').hidden,true);
  assert.equal(b.get('ignoredMusicPanel').hidden,false);
  assert.equal(b.get('filter').value,'NEEDS_APPROVAL');
  b.requests.at(-1).resolve([]); await new Promise(resolve=>setImmediate(resolve));
});

test('ignored-source restore targets only one URL without changing identity block', async () => {
  const b = browser(); b.app.state.user = 'admin';
  const entry = {id:'group', title:'wrong album',mbid:'420c95c6',
    origins:[{provider:'youtube',media_key:'goodid',title:'correct song',url:'https://youtu.be/goodid'},
      {provider:'youtube',media_key:'badid',title:'full album',url:'https://youtu.be/badid'}]};
  const item = vm.runInContext('renderIgnored',b.context)(entry);
  const restoreGood = item.children[2].children[1];
  assert.equal(restoreGood.textContent, 'Restore just this URL');
  assert.match(item.children[2].children[0].textContent, /goodid/);
  b.context.confirm = () => true;
  const pending = restoreGood.listeners.click();
  assert.equal(b.requests[0].url,'/api/ignored/group/origins/youtube/goodid?user_id=admin');
  assert.equal(b.requests[0].options.method, 'DELETE');
  b.requests[0].resolve({message:'Selected URL restored, other source remains ignored'});
  for (let i = 0; i < 12 && !b.requests[1]; i++) await Promise.resolve();
  b.requests[1].resolve([]);
  await pending;
  assert.match(b.get('notice').textContent, /other source remains ignored/);
});

test('bottom pagination shares filters, page state and disabled boundaries', async () => {
  const b = browser(); b.app.state.user = 'admin'; b.app.state.maxPage = 3;
  b.get('filter').value = 'NEEDS_APPROVAL'; b.get('search').value = 'Lorn';
  vm.runInContext('updateStats = () => {}; updateKeyedChildren = () => {};', b.context);
  const next = b.get('nextBottom').listeners.click();
  assert.match(b.requests[0].url, /page=2&limit=50&status=NEEDS_APPROVAL&q=Lorn/);
  b.requests[0].resolve({page: 2, limit: 50, total: 120, stats: {total: 120}, tracks: []});
  await next;
  assert.equal(b.get('pageBottom').textContent, b.get('page').textContent);
  assert.equal(b.get('prevBottom').disabled, false);
  const last = b.get('nextBottom').listeners.click();
  b.requests[1].resolve({page: 3, limit: 50, total: 120, stats: {total: 120}, tracks: []});
  await last;
  assert.equal(b.get('nextBottom').disabled, true);
  assert.equal(b.get('next').disabled, true);
  b.app.state.page = 2;
  const previous = b.get('prevBottom').listeners.click();
  b.requests[2].resolve({page: 1, limit: 50, total: 120, stats: {total: 120}, tracks: []});
  await previous;
  assert.equal(b.get('prevBottom').disabled, true);
  assert.equal(b.get('prev').disabled, true);
});

test('YouTube channel prefixes the title without adding rows or changing sources', () => {
  const b = browser();
  const render = vm.runInContext('renderTrack', b.context);
  const track = {
    id:'song', title:'The Toxic Avenger - Résidence Beauséjour',
    matched_title:'The Toxic Avenger - Résidence beauséjour', current_origin_id:'current',
    origins:[
      {id:'other',provider:'youtube',title:'Another upload',channel:'Other channel'},
      {id:'current',provider:'youtube',title:'Video title',channel:'Enchanté Records',url:'https://youtu.be/abcdefghijk'},
    ],
    playlists:[],health:'AVAILABLE',operation_state:'IDLE',choices:[],
  };
  const music = render(track).children[0];
  assert.equal(music.children[0].textContent, 'Enchanté Records | The Toxic Avenger - Résidence beauséjour');
  assert.equal(music.children[1].textContent, track.title);
  assert.equal(music.children.length, 3);
  const sources = music.children[2];
  assert.equal(sources.tagName, 'details');
  assert.equal(sources.children[0].textContent, '2 sources');
  assert.equal(sources.children[2].children[1].href, 'https://youtu.be/abcdefghijk');
  const missingChannel = render({...track, origins:[{provider:'youtube'}]}).children[0];
  assert.equal(missingChannel.children[0].textContent, track.matched_title);
  const unmatched = render({...track, matched_title:null, current_origin_id:null}).children[0];
  assert.equal(unmatched.children[0].textContent, `Other channel | ${track.title}`);
  assert.equal(unmatched.children.length, 2);
});


test('Stop retrying disables the active job URL, not the source of playing audio', async () => {
  const b=browser(); b.app.state.user='admin';
  vm.runInContext('refreshTracks=async()=>{}; loadDisabledOrigins=async()=>{};',b.context);
  const track={id:'song',user_id:'admin',title:'Song',health:'AVAILABLE',operation_state:'DEFERRED',
    current_origin_id:'playing',active_origin_id:'replacement',can_stop_download:true,playlists:[],choices:[],
    origins:[{id:'playing',provider:'youtube',title:'Old audio',url:'https://youtu.be/playing',downloadable:true},
      {id:'replacement',provider:'youtube',title:'New audio',url:'https://youtu.be/replacement',downloadable:true}]};
  const row=vm.runInContext('renderTrack',b.context)(track);
  const stop=row.children[3].children[0].children.find(c=>c.textContent==='Stop retrying this URL');
  assert.ok(stop); assert.equal(stop.disabled,false);
  const pending=stop.listeners.click();
  assert.equal(b.requests[0].url,'/api/tracks/song/origins/replacement/disable');
  assert.deepEqual(JSON.parse(b.requests[0].options.body),{user_id:'admin'});
  b.requests[0].resolve({message:'URL disabled',cancellation_pending:true});
  await pending;
  assert.match(b.get('notice').textContent,/Stopping the current download/);
  const unknown=vm.runInContext('renderTrack',b.context)({...track,active_origin_id:null});
  assert.equal(unknown.children[3].children[0].children.some(c=>c.textContent==='Stop retrying this URL'),false);
});

test('disabled origins cannot be selected for a new download and can be enabled without retrying', async () => {
  const b=browser(); b.app.state.user='admin';
  vm.runInContext('refreshTracks=async()=>{}; loadDisabledOrigins=async()=>{};',b.context);
  const track={id:'song',user_id:'admin',title:'Song',origins:[
    {id:'bad',provider:'youtube',title:'Restricted',downloadable:true,download_enabled:0},
    {id:'good',provider:'youtube',title:'Available',downloadable:true,download_enabled:1}]};
  vm.runInContext('openOperation',b.context)(track,'redownload');
  assert.deepEqual(b.get('operationOrigin').children.map(c=>c.value),['good']);
  const pending=vm.runInContext('setOriginDownload',b.context)(track,track.origins[0],true);
  assert.equal(b.requests[0].url,'/api/tracks/song/origins/bad/enable');
  b.requests[0].resolve({message:'URL enabled. Retry explicitly when ready.'});
  await pending;
  assert.equal(b.requests.length,1);
  b.app.state.user='guest';
  await assert.rejects(()=>vm.runInContext('setOriginDownload',b.context)(track,track.origins[0],false),/Library changed/);
  assert.equal(b.requests.length,1);
});

test('stopped tracks keep audio availability and offer another URL', () => {
  const b=browser(); b.app.state.user='admin';
  const track={id:'song',user_id:'admin',title:'Song',health:'AVAILABLE',operation_state:'STOPPED',
    playlists:[],choices:[],origins:[{id:'bad',downloadable:true,download_enabled:0}]};
  const row=vm.runInContext('renderTrack',b.context)(track);
  assert.equal(row.children[2].children[0].textContent,'Downloads stopped');
  assert.equal(row.children[2].children[1].textContent,'Current audio is playable');
  const controls=row.children[3].children[0].children;
  assert.ok(controls.some(c=>c.textContent==='Use another URL…' && !c.disabled));
  assert.equal(controls.some(c=>c.textContent==='Retry'),false);
});

test('replacement URL submission is scoped, resists double clicks and keeps errors visible', async () => {
  const b=browser(); b.app.state.user='admin';
  vm.runInContext('refreshTracks=async()=>{}; loadDisabledOrigins=async()=>{};',b.context);
  vm.runInContext('openReplacement',b.context)({id:'song',user_id:'admin',title:'Song'});
  b.get('replacementUrl').value='https://youtu.be/newvideo';
  const event={preventDefault(){}};
  const pending=b.get('replacementForm').listeners.submit(event);
  await b.get('replacementForm').listeners.submit(event);
  assert.equal(b.requests.length,1);
  assert.equal(b.requests[0].url,'/api/tracks/song/replacement');
  assert.deepEqual(JSON.parse(b.requests[0].options.body),{user_id:'admin',url:'https://youtu.be/newvideo'});
  b.requests[0].resolve({error:'Stop the current download first'},409);
  await pending;
  assert.match(b.get('replacementStatus').textContent,/Stop the current download first/);
  assert.equal(b.get('replacementSubmit').disabled,false);
  assert.ok(b.app.state.replacementTrack);
  const second=b.get('replacementForm').listeners.submit(event);
  b.app.state.user='guest';
  b.get('replacementStatus').textContent='New library';
  b.requests[1].resolve({message:'Queued'});
  await second;
  assert.equal(b.get('replacementStatus').textContent,'New library');
});

test('disabled-source responses and removal cancellation do not retain stale library state', async () => {
  const b=browser(); b.app.state.user='admin';
  const pending=vm.runInContext('loadDisabledOrigins',b.context)();
  b.app.state.user='guest'; b.app.state.generation++;
  b.requests[0].resolve(null); await pending;
  assert.equal(b.get('disabledSources').children.length,0);
  b.app.state.removeTrack={id:'old'};
  b.get('removeDialog').listeners.cancel();
  assert.equal(b.app.state.removeTrack,null);
});
