/* Read-only search, explicit one-click decisions and scoped page navigation. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const {browser}=require('./frontend_harness.cjs');
const MBID='11111111-1111-4111-8111-111111111111';
const RID='22222222-2222-4222-8222-222222222222';
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
  const b=browser(false,true),timers=new Map(),storage=new Map();let id=0;
  b.context.setTimeout=(fn,delay)=>{timers.set(++id,{fn,delay});return id;};
  b.context.clearTimeout=key=>timers.delete(key);
  b.context.localStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)};
  b.timers=timers;b.storage=storage;b.app.state.user='admin';
  vm.runInContext('refreshTracks=async()=>{};',b.context);
  b.fire=async delay=>{const [key,timer]=[...timers].find(([,t])=>t.delay===delay)||[];assert.ok(timer,`timer ${delay}`);timers.delete(key);return timer.fn();};
  return b;
}
function context(id='one',extra={}){
  return {track_id:id,user_id:'admin',revision:'r'.repeat(64),current_title:'Applied title',current_mbid:MBID,
    state:'NEEDS_APPROVAL',health:'AVAILABLE',can_correct:true,origin_id:'origin-'+id,origin_count:1,
    source_title:'Video '+id,source_channel:'Channel '+id,source_url:'https://www.youtube.com/watch?v=abcdefghijk',
    source:{source_title:'Original artist - Original title',tags:{artist:['Original artist'],title:['Original title']},basis:'provider_title_only'},
    current_tags:{artist:['Current artist'],title:['Current title'],album:['Album']},current_duration_seconds:198,
    search_hints:{artist:'Original artist',title:'Original title'},approval_token:'a'.repeat(64),rejected_candidates:[],
    choices:[{index:3,kind:'candidate',mbid:MBID,artist:'Detected artist',title:'Detected title',match_score:88,
      evidence:{candidate_duration_seconds:199,duration_delta_seconds:1,fingerprint_support:true}},
      {index:5,kind:'source',title:'Original title'}, {index:7,kind:'asis',title:'Current title',asis:true}],...extra};
}
const summaries=(count=2)=>Array.from({length:count},(_,i)=>({id:i===0?'one':i===1?'two':String(i+1),title:'Canonical '+i,source_title:'Video '+i,channel:'Channel '+i}));
async function open(b,{tracks=summaries(),data=context(),pane=''}={}){
  const pending=b.metadata.open({id:data.track_id,title:'Song',operation_state:data.state},pane);
  assert.match(b.requests.at(-1).url,/\/api\/review\/queue/);
  b.requests.at(-1).resolve({ids:tracks.map(t=>t.id),tracks,total:tracks.length,excluded:0});await tick();
  b.requests.at(-1).resolve(data);await pending;
}
function walk(node,fn){if(!node||typeof node!=='object')return;fn(node);for(const child of node.children||[])walk(child,fn);}
function buttons(node,text){const out=[];walk(node,n=>{if(n.tagName==='button'&&(!text||n.textContent===text))out.push(n);});return out;}
async function search(b,items=[{id:MBID,title:'Found',artist:'Artist',length_ms:198000,releases:[{id:RID,title:'Release'}]}],extra={}){
  const pending=b.metadata.search();b.requests.at(-1).resolve({items,total:items.length,page:1,limit:20,...extra});await pending;
}

test('opening uses a real tab and independent Waiting for approval filter',async()=>{
  const b=setup();b.get('filter').value='ALL';await open(b);
  assert.equal(b.app.state.tab,'approval');assert.equal(b.get('approvalFilter').value,'NEEDS_APPROVAL');
  assert.equal(b.get('filter').value,'ALL');assert.equal(b.get('approvalWorkspace').hidden,false);
  assert.equal(b.get('metadataCurrent').textContent,'Video one');assert.equal(b.get('metadataOrigin').textContent,'Channel one');
  assert.equal(b.get('approvalPosition').textContent,'1 / 2');
  assert.equal(b.get('mbTitle').value,'Original title');assert.equal(b.get('mbArtist').value,'Original artist');
  assert.match(b.requests[0].url,/status=NEEDS_APPROVAL/);
  assert.equal(b.timers.size,1);assert.equal([...b.timers.values()][0].delay,500);
});
test('MusicBrainz starts automatically after loading a track without a submenu or write',async()=>{
  const b=setup();await open(b);const pending=b.fire(500);
  assert.match(b.requests.at(-1).url,/musicbrainz\/search/);
  b.requests.at(-1).resolve({items:[],total:0,page:1,limit:20});await pending;
  assert.equal(b.requests.some(r=>r.options?.method==='POST'),false);
});
test('Beets shows candidate rows, source and keep-current choices remain separate',async()=>{
  const b=setup();await open(b);
  assert.equal(b.get('metadataSuggestions').children.length,1);
  assert.equal(b.get('metadataSuggestions').children[0].tagName,'tr');
  assert.equal(buttons(b.get('metadataSuggestions'),'Use').length,1);
  assert.equal(b.get('sourceMetadataApply').disabled,false);
  assert.equal(b.get('metadataKeepCurrent').hidden,false);
});
test('one-click use sends the displayed candidate index/token once then advances',async()=>{
  const b=setup();await open(b);const use=buttons(b.get('metadataSuggestions'),'Use')[0];
  const pending=use.listeners.click();await use.listeners.click();
  const request=b.requests.at(-1);
  assert.deepEqual(JSON.parse(request.options.body),{user_id:'admin',mode:'approve',index:3,approval_token:'a'.repeat(64)});
  assert.equal(b.requests.filter(r=>r.options?.method==='POST').length,1);
  request.resolve({message:'Queued'});await tick();
  assert.match(b.requests.at(-1).url,/tracks\/two\/metadata-context/);
  b.requests.at(-1).resolve(context('two'));await pending;
  assert.equal(b.get('approvalPosition').textContent,'2 / 2');assert.equal(b.get('approvalNext').disabled,true);
});
test('original source is a single explicit revision-checked choice with no browser-supplied tags',async()=>{
  const b=setup();await open(b,{tracks:summaries(1)});
  const pending=b.get('sourceMetadataApply').listeners.click(),request=b.requests.at(-1);
  assert.deepEqual(JSON.parse(request.options.body),{user_id:'admin',mode:'restore_source',revision:'r'.repeat(64)});
  request.resolve({message:'Queued'});await pending;
  assert.equal(b.get('approvalEmpty').hidden,false);assert.equal(b.get('approvalWorkspace').hidden,true);
  assert.equal(b.get('approvalPrevious').disabled,false);
});
test('Next and Previous never submit and use fresh context',async()=>{
  const b=setup();await open(b);let pending=b.get('approvalNext').listeners.click();
  b.requests.at(-1).resolve(context('two'));await pending;
  assert.equal(b.get('approvalPosition').textContent,'2 / 2');
  pending=b.get('approvalPrevious').listeners.click();b.requests.at(-1).resolve(context('one',{revision:'b'.repeat(64)}));await pending;
  assert.equal(b.get('approvalPosition').textContent,'1 / 2');assert.equal(b.requests.some(r=>r.options?.method==='POST'),false);
});
test('failed or stale approval stays on this track with reload available',async()=>{
  const b=setup();await open(b);const pending=buttons(b.get('metadataSuggestions'),'Use')[0].listeners.click();
  b.requests.at(-1).resolve({error:'Approval candidates changed; reload'},400);await pending;
  assert.equal(b.get('approvalPosition').textContent,'1 / 2');assert.match(b.get('metadataNotice').textContent,/changed/);
  assert.equal(b.get('metadataReload').hidden,false);assert.equal(b.get('sourceMetadataApply').disabled,false);
});
test('a save response from an abandoned page cannot advance a new page',async()=>{
  const b=setup();await open(b);const save=buttons(b.get('metadataSuggestions'),'Use')[0].listeners.click(),old=b.requests.at(-1);
  b.metadata.leave();b.app.state.tab='music';await open(b,{data:context('two')});
  old.resolve({message:'Queued old'});await save;
  assert.equal(b.get('metadataCurrent').textContent,'Video two');assert.notEqual(b.get('metadataNotice').textContent,'Queued old');
});
test('user changes invalidate old metadata, search and save responses',async()=>{
  const b=setup();await open(b);const searching=b.metadata.search(),oldSearch=b.requests.at(-1);
  b.metadata.reset();b.app.state.user='guest';b.app.state.tab='approval';const entered=b.metadata.enter(),queue=b.requests.at(-1);
  assert.match(queue.url,/user_id=guest/);queue.resolve({tracks:[],ids:[],total:0});await entered;
  oldSearch.resolve({items:[{id:MBID,title:'Private old result'}],total:1,page:1,limit:20});await searching;
  assert.equal(b.get('mbResults').children.length,0);assert.equal(b.get('sourceMetadataApply').disabled,true);
});
test('typing debounces search and aborts late results',async()=>{
  const b=setup();await open(b);const pending=b.metadata.search(),old=b.requests.at(-1);
  for(const title of ['N','Ne','New title']){b.get('mbTitle').value=title;b.get('mbTitle').listeners.input();}
  assert.equal(old.options.signal.aborted,true);assert.equal(b.timers.size,1);
  old.resolve({items:[{id:MBID,title:'Obsolete'}],total:1,page:1,limit:20});await pending;
  assert.equal(b.get('mbResults').children.length,0);
});
test('leaving the page cancels automatic search and prevents hidden provider requests',async()=>{
  const b=setup();await open(b);b.metadata.leave();b.app.state.tab='music';
  assert.equal(b.timers.size,0);const count=b.requests.length;await b.metadata.search();assert.equal(b.requests.length,count);
});
test('MusicBrainz use can select a compatible edition without trusting result tags',async()=>{
  const b=setup();await open(b,{tracks:summaries(1)});await search(b);
  let edition;walk(b.get('mbResults'),n=>{if(n.tagName==='select')edition=n;});edition.value=RID;
  const pending=buttons(b.get('mbResults'),'Use')[0].listeners.click();
  const request=b.requests.at(-1);assert.match(request.url,/metadata-correction$/);
  assert.deepEqual(JSON.parse(request.options.body),{user_id:'admin',mode:'select_recording',mbid:MBID,release_id:RID,revision:'r'.repeat(64)});
  request.resolve({message:'Queued'});await pending;
});
test('broadened search explains its effective query and pagination retains it',async()=>{
  const b=setup();await open(b);
  await search(b,[],{total:40,query_used:{title:'Short',artist:'',artist_id:null},query_note:'Title-only search',query_strategy:'title_only'});
  assert.match(b.get('mbStatus').textContent,/Title-only/);
  const pending=b.get('mbNext').listeners.click();const request=b.requests.at(-1);
  assert.match(request.url,/page=2/);assert.match(request.url,/title=Short&artist=&/);
  request.resolve({items:[],total:40,page:2,limit:20});await pending;
});
test('artist browsing uses its identity and clears the previous song restriction',async()=>{
  const b=setup();await open(b);b.get('mbEntity').value='artist';
  await search(b,[{id:MBID,name:'Chosen artist',aliases:[]}]);
  await buttons(b.get('mbResults'),'Tracks')[0].listeners.click();
  assert.equal(b.get('mbTitle').value,'');assert.equal(b.get('mbArtist').value,'Chosen artist');
  const pending=b.metadata.search();assert.match(b.requests.at(-1).url,new RegExp('artist_id='+MBID));
  b.requests.at(-1).resolve({items:[],total:0,page:1,limit:20});await pending;
});
test('429 observes the shared cooldown and does not spam requests',async()=>{
  const b=setup();await open(b);const pending=b.metadata.search(),request=b.requests.at(-1);
  request.resolve({error:'Cooldown',retry_after:30},429);await pending;
  const before=b.requests.length;await b.metadata.search();assert.equal(b.requests.length,before);
  assert.ok([...b.timers.values()][0].delay>=29000);
  b.metadata.leave();assert.equal(b.timers.size,0);
});
test('manual editing starts with actual tags and sends only changed nonempty values',async()=>{
  const b=setup();await open(b,{tracks:summaries(1),pane:'manual'});
  assert.equal(b.get('metadataManualPane').open,true);assert.equal(b.get('metadataManualArtist').value,'Current artist');
  b.get('metadataManualTitle').value='Correct title';b.get('metadataManualTitle').listeners.input();
  b.get('metadataManualAlbum').value='';const pending=b.get('metadataManualApply').listeners.click();
  const request=b.requests.at(-1);assert.deepEqual(JSON.parse(request.options.body),{user_id:'admin',mode:'retag',overrides:{title:'Correct title'},revision:'r'.repeat(64)});
  request.resolve({message:'Queued'});await pending;
});
test('rejecting a recording keeps the page and updates displayed candidate tokens',async()=>{
  const b=setup();await open(b);const pending=buttons(b.get('metadataSuggestions'),'Wrong recording')[0].listeners.click(),request=b.requests.at(-1);
  assert.equal(JSON.parse(request.options.body).mbid,MBID);
  request.resolve({...context(),revision:'z'.repeat(64),approval_token:'b'.repeat(64),choices:[{index:4,kind:'candidate',title:'Other',mbid:RID}],message:'Rejected'});
  await pending;assert.equal(b.get('approvalPosition').textContent,'1 / 2');
  const apply=buttons(b.get('metadataSuggestions'),'Use')[0].listeners.click(),next=b.requests.at(-1);
  assert.equal(JSON.parse(next.options.body).approval_token,'b'.repeat(64));next.resolve({error:'test failure'},400);await apply;
});
test('remembering an alias still requires explicit confirmation and uses current revision',async()=>{
  const b=setup();await open(b);b.get('mbEntity').value='artist';await search(b,[{id:MBID,name:'Native artist'}]);
  b.context.confirm=()=>false;const before=b.requests.length;await buttons(b.get('mbResults'),'Remember artist')[0].listeners.click();assert.equal(b.requests.length,before);
  b.context.confirm=()=>true;const pending=buttons(b.get('mbResults'),'Remember artist')[0].listeners.click(),request=b.requests.at(-1);
  assert.equal(JSON.parse(request.options.body).revision,'r'.repeat(64));
  request.resolve({...context(),search_hints:{title:'Original title',artist:'Native artist',artist_id:MBID},revision:'z'.repeat(64)});await pending;
  assert.equal(b.get('mbArtist').value,'Native artist');
});
test('a 4000-track chooser stays collapsed and renders at most one page of title/channel summaries',async()=>{
  const b=setup();await open(b,{tracks:summaries(4000)});
  assert.equal(b.get('approvalPickerList').children.length,0);
  b.get('approvalPicker').open=true;b.get('approvalPicker').listeners.toggle();
  assert.equal(b.get('approvalPickerList').children.length,30);
  assert.equal(b.get('approvalPickerPage').textContent,'1 / 134');
  assert.equal(b.get('approvalPickerList').children[0].children[0].textContent,'Video 0');
  assert.equal(b.get('approvalPickerList').children[0].children[1].textContent,'Channel 0');
  b.get('approvalPickerSearch').value='Channel 3999';b.get('approvalPickerSearch').listeners.input();await b.fire(150);
  assert.equal(b.get('approvalPickerList').children.length,1);
  const pending=b.get('approvalPickerList').children[0].listeners.click();
  b.requests.at(-1).resolve(context('4000'));await pending;
  assert.equal(b.get('approvalPosition').textContent,'4000 / 4000');assert.equal(b.get('approvalPicker').open,false);
});
test('storage failure never prevents next/previous or approval',async()=>{
  const b=setup();b.context.localStorage={getItem(){throw new Error('Unavailable');},setItem(){throw new Error('Quota');}};
  await open(b);const pending=b.get('approvalNext').listeners.click();b.requests.at(-1).resolve(context('two'));await pending;
  assert.equal(b.get('metadataCurrent').textContent,'Video two');assert.equal(b.get('sourceMetadataApply').disabled,false);
});
test('re-entering restores the last track by ID, not a stale numeric position',async()=>{
  const b=setup();await open(b);const next=b.get('approvalNext').listeners.click();b.requests.at(-1).resolve(context('two'));await next;
  b.metadata.leave();const entering=b.metadata.enter();b.requests.at(-1).resolve({tracks:summaries().reverse(),ids:['two','one'],total:2});await tick();
  assert.match(b.requests.at(-1).url,/tracks\/two\/metadata-context/);b.requests.at(-1).resolve(context('two'));await entering;
  assert.equal(b.get('approvalPosition').textContent,'1 / 2');
});
test('another filter is independent of Music and failed list loads can be retried',async()=>{
  const b=setup();await open(b);b.get('filter').value='AVAILABLE';b.get('approvalFilter').value='SOURCE_METADATA';
  b.get('approvalFilter').listeners.change();const request=b.requests.at(-1);
  assert.match(request.url,/status=SOURCE_METADATA/);request.resolve({error:'Network failed'},503);await tick();
  assert.equal(b.get('filter').value,'AVAILABLE');assert.equal(b.get('approvalRefresh').disabled,false);
  assert.equal(b.get('approvalNext').disabled,true);assert.match(b.get('metadataNotice').textContent,/Network failed/);
});
test('no shortcut listeners or modal calls remain in the approval implementation',()=>{
  const js=fs.readFileSync(require.resolve('../src/static/metadata-ui.js'),'utf8');
  assert.equal(js.includes('showModal'),false);assert.equal(js.includes('keydown'),false);
});
