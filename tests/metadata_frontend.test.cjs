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
  assert.equal(buttons.has('Restore source metadata'), false);
  assert.equal(buttons.get('Edit metadata').disabled, true);
  assert.equal(buttons.get('Sources & details').disabled, false);
  assert.equal(buttons.get('Repair tags / loudness').disabled, true);
  buttons = make('AVAILABLE', 'RUNNING');
  assert.equal(buttons.get('Review metadata').disabled, true);
  assert.equal(buttons.get('Edit metadata').disabled, true);
  assert.equal(buttons.get('Sources & details').disabled, false);
  assert.equal(buttons.get('Remove…').disabled, true);
  buttons = make('AVAILABLE', 'IDLE');
  assert.equal(buttons.get('Repair tags / loudness').disabled, false);
});

test('row signature changes when correction script becomes available', () => {
  const b = setup(); b.context.fixture = {id: 'x', title: 'Song'};
  const ready = vm.runInContext('trackSignature(fixture)', b.context);
  b.context.window.MetadataUI = undefined;
  assert.notEqual(vm.runInContext('trackSignature(fixture)', b.context), ready);
});


function reviewContext(id='first', extra={}) {
  return {track_id:id,user_id:'admin',revision:'r'.repeat(64),current_title:'Current '+id,
    current_mbid:MBID,state:'NEEDS_APPROVAL',health:'AVAILABLE',can_correct:true,origin_count:1,
    choices:[{index:3,kind:'recording',mbid:MBID,artist:'末廣健一郎',title:'誓いの旋律',match_score:85.7,
      evidence:{local_duration_seconds:190,candidate_duration_seconds:192,duration_delta_seconds:2,artist_ids:[]}}],
    approval_token:'token-'+id,current_tags:{artist:['Wrong artist'],title:['Wrong song'],album:['Current album']},
    current_duration_seconds:190,candidate_duration_seconds:192,
    source:{source_title:'Kenichiro Suehiro - 誓いの旋律',tags:{artist:['Kenichiro Suehiro'],title:['誓いの旋律']},basis:'provider_title_only'},
    source_url:'https://youtu.be/abcdefghijk',latest_identification:null,...extra};
}
function storageFor(b) {
  const storage=new Map();
  b.context.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
  return storage;
}
async function requested(b,index) {
  for(let i=0;i<40&&!b.requests[index];i++) await Promise.resolve();
  assert.ok(b.requests[index], 'Expected request '+index);
  return b.requests[index];
}
async function queue(b,ids=['first','second']) {
  b.get('filter').value='NEEDS_APPROVAL';b.get('search').value='日本語';b.get('reviewSort').value='artist';
  const pending=b.metadata.startQueue();
  const request=b.requests.at(-1);
  assert.match(request.url,/api\/review\/queue/);
  assert.match(request.url,/status=NEEDS_APPROVAL/);
  assert.match(request.url,/sort=artist/);
  request.resolve({ids,total:ids.length,matching_total:ids.length,excluded:0});
  const next=await requested(b,b.requests.indexOf(request)+1);
  next.resolve(reviewContext(ids[0]));
  await pending;
}

test('Save & next sends the displayed index/token once and advances the stable queue', async()=>{
  const b=setup(), storage=storageFor(b); await queue(b);
  assert.equal(b.get('metadataSaveNext').disabled,false);
  assert.match(b.get('metadataAudio').src,/candidate=true/);
  const pending=b.get('metadataSaveNext').listeners.click();
  await b.get('metadataSaveNext').listeners.click();
  assert.equal(b.requests.length,3);
  assert.deepEqual(JSON.parse(b.requests[2].options.body),{user_id:'admin',mode:'approve',index:3,approval_token:'token-first'});
  b.requests[2].resolve({message:'Queued'});
  const second=await requested(b,3); assert.match(second.url,/tracks\/second\/metadata-context/);
  second.resolve(reviewContext('second')); await pending;
  assert.equal(b.get('metadataQueueProgress').textContent,'Track 2 of 2');
  const saved=JSON.parse(storage.get('music-review-queue-v1:admin'));
  assert.equal(saved.index,1); assert.deepEqual(saved.completed,['first']);
});

test('Skip, close, resume and Back preserve one queue per library and apply nothing', async()=>{
  const b=setup(),storage=storageFor(b);await queue(b);
  const skip=b.get('metadataQueueSkip').listeners.click();
  (await requested(b,2)).resolve(reviewContext('second'));await skip;
  assert.deepEqual(JSON.parse(storage.get('music-review-queue-v1:admin')).skipped,['first']);
  b.metadata.reset();
  const resume=b.metadata.startQueue();
  assert.match(b.requests[3].url,/tracks\/second\/metadata-context/);
  b.requests[3].resolve(reviewContext('second'));await resume;
  const back=b.get('metadataQueueBack').listeners.click();
  b.requests[4].resolve(reviewContext('first'));await back;
  assert.equal(b.get('metadataQueueProgress').textContent,'Track 1 of 2');
  assert.equal(b.requests.some(request=>request.options.method==='POST'),false);
  assert.equal(storage.size,1);
});

test('an old save response cannot advance a closed and reopened review session', async()=>{
  const b=setup(),storage=storageFor(b);await queue(b);
  const save=b.get('metadataSaveNext').listeners.click(), old=b.requests[2];
  b.metadata.reset();
  const openNew=b.metadata.open({id:'other',title:'Other'},'search');
  b.requests[3].resolve(reviewContext('other'));await openNew;
  old.resolve({message:'Queued'});await save;
  assert.equal(b.requests.length,4);
  assert.equal(b.get('metadataCurrent').textContent,'Current other');
  assert.equal(b.get('metadataQueueBar').hidden,true);
  assert.equal(JSON.parse(storage.get('music-review-queue-v1:admin')).index,0);
});

test('switching library during a save leaves the new library review untouched', async()=>{
  const b=setup();storageFor(b);await queue(b);
  const save=b.get('metadataSaveNext').listeners.click(),old=b.requests[2];
  b.app.state.user='guest';b.metadata.reset();
  const next=b.metadata.open({id:'guest-track',title:'Guest'},'search');
  b.requests[3].resolve(reviewContext('guest-track',{user_id:'guest'}));await next;
  old.resolve({message:'Queued'});await save;
  assert.equal(b.requests.length,4);assert.equal(b.get('metadataCurrent').textContent,'Current guest-track');
  assert.match(b.get('metadataAudio').src,/user_id=guest/);
});

test('rejecting a candidate replaces the revision/token and requires another choice', async()=>{
  const b=setup();
  const pending=b.metadata.open({id:'first'},'suggestions');b.requests[0].resolve(reviewContext());await pending;
  const audio=b.get('metadataAudio');audio.currentTime=42;audio.paused=true;audio.duration=190;
  const entry=b.get('metadataSuggestions').children[0];
  const reject=entry.children.find(child=>child.textContent==='Wrong recording');
  const rejected=reject.listeners.click();
  assert.deepEqual(JSON.parse(b.requests[1].options.body),{user_id:'admin',revision:'r'.repeat(64),mbid:MBID,reject:true});
  b.requests[1].resolve(reviewContext('first',{revision:'s'.repeat(64),approval_token:'fresh-token',
    choices:[{index:0,kind:'source',title:'誓いの旋律',artist:'Kenichiro Suehiro'}]}));
  await rejected;
  assert.match(audio.src,/approval_token=fresh-token/);
  audio.currentTime=0;audio.listeners.loadedmetadata();assert.equal(audio.currentTime,42);
  assert.equal(b.get('metadataSuggestionApply').disabled,true);
  const radio=b.get('metadataSuggestions').children[0].children[0].children[0];
  radio.checked=true;radio.listeners.change();
  const applying=b.get('metadataSuggestionApply').listeners.click();
  assert.deepEqual(JSON.parse(b.requests[2].options.body),{user_id:'admin',mode:'approve',index:0,approval_token:'fresh-token'});
  b.requests[2].resolve({message:'Queued'});await applying;
});

test('manual editing starts with actual tags and sends only changed fields with its revision', async()=>{
  const b=setup();
  const pending=b.metadata.open({id:'first'},'manual');
  b.requests[0].resolve(reviewContext('first',{state:'IDLE',current_release_id:result.releases[0].id}));await pending;
  assert.equal(b.get('metadataManualArtist').value,'Wrong artist');
  assert.equal(b.get('metadataManualRelease').value,result.releases[0].id);
  b.get('metadataManualTitle').value='Corrected title';b.get('metadataManualTitle').listeners.input();
  const applying=b.get('metadataManualApply').listeners.click();
  assert.deepEqual(JSON.parse(b.requests[1].options.body),{user_id:'admin',mode:'retag',revision:'r'.repeat(64),overrides:{title:'Corrected title'}});
  b.requests[1].resolve({error:'This track changed; review it again'},409);await applying;
  assert.match(b.get('metadataNotice').textContent,/track changed/);
  assert.equal(b.get('metadataReload').hidden,false);
});

test('broadened search explains its query and keeps the same query on later pages', async()=>{
  const b=setup();await open(b);
  const pending=b.metadata.search();
  b.requests[1].resolve({items:[result],total:42,page:1,limit:20,query_used:{title:'Right song',artist:'',artist_id:null},
    query_strategy:'title_only',query_note:'Artist constraint removed. Verify the artist.'});await pending;
  assert.match(b.get('mbStatus').textContent,/Artist constraint removed/);
  const next=b.get('mbNext').listeners.click();
  assert.match(b.requests[2].url,/artist=&page=2/);
  b.requests[2].resolve({items:[],total:42,page:2,limit:20});await next;
});

test('artist aliases require explicit confirmation and replace the correction revision', async()=>{
  const b=setup();await open(b,'search',{search_hints:{title:'誓いの旋律',artist:'Kenichiro Suehiro',notes:[]}});
  b.get('mbEntity').value='artist';b.get('mbEntity').listeners.change();
  await searchResults(b,[{id:MBID,name:'末廣健一郎',aliases:[{name:'Kenichiro Suehiro',locale:'en'}]}]);
  const remember=b.get('mbResults').children[0].children.find(child=>child.textContent==='Remember artist: 末廣健一郎');
  assert.ok(remember);
  b.context.confirm=()=>false;await remember.listeners.click();assert.equal(b.requests.length,2);
  b.context.confirm=()=>true;
  const saving=remember.listeners.click();
  assert.deepEqual(JSON.parse(b.requests[2].options.body),{user_id:'admin',revision:'r'.repeat(64),artist_id:MBID,forget:false});
  b.requests[2].resolve(reviewContext('track',{revision:'s'.repeat(64),approved_artist_alias:{alias:'Kenichiro Suehiro',artist_id:MBID,artist_name:'末廣健一郎'},
    search_hints:{title:'誓いの旋律',artist:'末廣健一郎',artist_id:MBID,notes:[]}}));await saving;
  b.get('metadataSourceTab').listeners.click();
  const restore=b.get('sourceMetadataApply').listeners.click();
  assert.equal(JSON.parse(b.requests[3].options.body).revision,'s'.repeat(64));
  b.requests[3].resolve({message:'Queued'});await restore;
});

test('storage quota errors do not prevent reviewing or saving the next track', async()=>{
  const b=setup();
  b.context.localStorage={getItem:()=>null,setItem:()=>{throw new Error('Quota exceeded');}};
  await queue(b,['first']);
  assert.equal(b.get('metadataSaveNext').disabled,false);
  const pending=b.get('metadataSaveNext').listeners.click();
  b.requests[2].resolve({message:'Queued'});await pending;
  assert.equal(b.get('metadataQueueComplete').hidden,false);
  assert.match(b.get('metadataQueueScope').textContent,/Resume unavailable/);
});


function treeText(element) {
  return [element.textContent,...(element.children||[]).map(treeText)].filter(Boolean).join(' ');
}
test('review evidence uses backend fingerprint tri-state and diagnostic score gap', async()=>{
  const b=setup();
  const pending=b.metadata.open({id:'first'},'suggestions');
  b.requests[0].resolve(reviewContext('first',{choices:[true,false,null].map((support,index)=>({
    index,kind:'candidate',title:'Choice '+index,artist:'Artist',match_score:85-index,
    evidence:{fingerprint_support:support,duration_delta_seconds:2.1,penalties:[{field:'artist',penalty:0.1}]}
  })),latest_identification:{at:'now',source_title:'Song',recommendation:'medium',candidate_count:3,
    diagnostics:{fingerprint_generated:true,acoustid_id_present:true,top_score_gap:3.4}}}));
  await pending;
  const suggestions=b.get('metadataSuggestions').children;
  assert.match(treeText(suggestions[0]),/Fingerprint lookup supports this recording/);
  assert.match(treeText(suggestions[1]),/Fingerprint lookup returned other recordings/);
  assert.match(treeText(suggestions[2]),/Fingerprint support is unknown/);
  assert.match(treeText(suggestions[0]),/Matching differences: artist/);
  assert.match(treeText(b.get('metadataDiagnostic')),/top two match scores differed by 3.4 points out of 100/);
});

test('missing actual tags remain unknown and alias confirmation names the original source text', async()=>{
  const b=setup();
  await open(b,'search',{current_tags:{},current_tags_error:'Current audio metadata could not be read.',
    search_hints:{artist:'Already mapped canonical name',title:'Song',artist_id:MBID,notes:[]},
    approved_artist_alias:{alias:'Kenichiro Suehiro',artist_name:'Already mapped canonical name',artist_id:MBID}});
  assert.equal(b.get('metadataCurrentValues').children[3].textContent,'Not available');
  b.get('mbEntity').value='artist';b.get('mbEntity').listeners.change();
  const different='33333333-3333-4333-8333-333333333333';
  await searchResults(b,[{id:different,name:'Correct artist'}]);
  let confirmation='';
  b.context.confirm=message=>{confirmation=message;return false;};
  const remember=b.get('mbResults').children[0].children.find(child=>child.textContent==='Remember artist: Correct artist');
  await remember.listeners.click();
  assert.match(confirmation,/Remember "Kenichiro Suehiro" as "Correct artist"/);
});
