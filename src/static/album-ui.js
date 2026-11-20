/* Album workspace: explicit per-recording plans, not playlist-wide album writes. */
"use strict";
window.AlbumUI = (() => {
  const el = id => document.getElementById(id);
  const create = (tag, text, cls) => {
    const item = document.createElement(tag);
    if (text != null) item.textContent = String(text);
    if (cls) item.className = cls;
    return item;
  };
  const action = (label, callback, cls = "secondary") => {
    const button = create("button", label, cls);
    button.type = "button";
    button.addEventListener("click", () => Promise.resolve().then(callback).catch(error => status(error.message, true)));
    return button;
  };
  const ui = {
    user:null, generation:0, loading:false,
    tracks:new Map(), selected:new Set(), plan:new Map(), candidates:new Map(),
    compared:[], releases:new Map(), recommendations:[], preview:null,
    offset:0, hasMore:false, total:0, searching:false
  };
  const active = (user, generation) =>
    ui.user === user && state.user === user && ui.generation === generation && state.tab === "album";
  const selectedIds = () => [...ui.selected];
  const sorted = (a,b) => String(a).localeCompare(String(b));
  const status = (text, error=false, field="albumPlanStatus") => {
    el(field).textContent=text;
    el(field).className=error ? "error-text" : "hint";
  };
  const label = track => track.title || track.id;
  const editionLabel = id => ui.releases.get(id)?.name || ui.candidates.get(id)?.album || ("Release " + id.slice(0,8));
  const hasExactMatch = (release,track) =>
    release.complete && release.tracks.some(item => item.recording_id === track.recording_id);
  const allChosen = () => [...ui.tracks.values()].filter(item => ui.selected.has(item.id));
  function invalidatePreview() {
    ui.preview=null;
    el("albumPlanResults").replaceChildren();
    el("albumPlanConfirm").hidden=true;
    el("albumPlanStatus").textContent="";
  }
  function planStats() {
    let move=0,newly=0,none=0;
    for (const [id,decision] of ui.plan) {
      const item=ui.tracks.get(id);
      if (decision.action === "not_found") none++;
      else if (item?.current_release_id) move++;
      else newly++;
    }
    return {move,newly,none};
  }
  function refreshSummary() {
    const counts=planStats();
    el("albumPlanSummary").textContent =
      ui.plan.size + " proposed decisions · " + counts.newly + " new album assignments · " +
      counts.move + " explicit moves · " + counts.none + " No Album Found. " +
      ui.selected.size + " recordings selected. Other recordings remain unchanged.";
    el("albumPlanPreview").disabled=ui.plan.size===0;
    el("albumCompareStatus").textContent=ui.compared.length + " of 4 albums compared";
    el("albumTrackCount").textContent=ui.selected.size + " selected · " +
      ui.tracks.size + " loaded / " + ui.total + " in current scope";
  }
  function selectTrack(id,checked) {
    if (checked) {
      if (ui.selected.size >= 500 && !ui.selected.has(id)) {
        status("Select at most 500 recordings per comparison. Split larger sets into batches.",true);
        renderTracks();return;
      }
      ui.selected.add(id);
    } else ui.selected.delete(id);
    invalidatePreview();refreshSummary();renderMatrix();
  }
  function renderTracks() {
    const body=el("albumTracks");
    const query=el("albumTrackSearch").value.trim().toLocaleLowerCase();
    const rows=[...ui.tracks.values()].filter(t =>
      !query || [label(t),t.recording_id,t.current_release_id].join(" ").toLocaleLowerCase().includes(query));
    body.replaceChildren();
    for (const item of rows) {
      const row=create("label",null,"album-track-row");
      const check=create("input");check.type="checkbox";check.checked=ui.selected.has(item.id);
      check.addEventListener("change",() => selectTrack(item.id,check.checked));
      const bodyText=create("span",null,"album-track-text");
      bodyText.append(create("strong",label(item)));
      let album=item.current_release_id
        ? "Assigned · " + editionLabel(item.current_release_id)
        : item.not_found ? "Reviewed: No Album Found" : "No album assigned";
      const meta=create("small",album);
      bodyText.append(meta);
      if (item.current_release_id) row.classList.add?.("album-track-assigned");
      row.append(check,bodyText);
      if (item.not_found) {
        const clear=action("Search again", async () => {
          await api("/api/albums/not-found/clear",{user_id:ui.user,track_id:item.id});
          if (state.user !== ui.user) return;
          item.not_found=false;
          ui.plan.delete(item.id);renderTracks();renderMatrix();refreshSummary();
        });
        clear.setAttribute("aria-label","Clear No Album Found for "+label(item));
        row.append(clear);
      }
      body.append(row);
    }
    if (!rows.length) body.append(create("p","No recordings match this view. Try another source or enable already-assigned music.","hint"));
    el("albumLoadMore").hidden=!ui.hasMore;
    refreshSummary();
  }
  async function loadTracks(reset=true) {
    if (!ui.user)return;
    const user=ui.user, generation=ui.generation;
    if (ui.loading)return;
    ui.loading=true;
    if (reset){ui.offset=0;ui.tracks.clear();}
    const params=new URLSearchParams({user_id:user,
      include_assigned:String(el("albumIncludeAssigned").checked),
      offset:String(ui.offset),limit:"100"});
    if(el("albumSource").value)params.set("source_id",el("albumSource").value);
    try {
      const result=await api("/api/albums/tracks?"+params);
      if (!active(user,generation))return;
      for(const item of result.items||[])ui.tracks.set(item.id,item);
      ui.total=result.total;ui.offset=result.offset+(result.items||[]).length;
      ui.hasMore=Boolean(result.has_more);
      renderTracks();renderMatrix();
    } catch(error) {if(active(user,generation))status(error.message,true);}
    finally {if(ui.user===user)ui.loading=false;}
  }
  function releaseCard(entry) {
    const rid=entry.release_id||entry.id;
    const box=create("article",null,"album-release-card");
    box.append(create("strong",entry.album||entry.name||"Unknown release"));
    const info=create("p",null,"hint");
    const details=[entry.date||"Date unknown",entry.country||"Country unknown",
      entry.status||"Status unknown"];
    if (entry.matched!=null)details.unshift(entry.matched+" matched of "+ui.selected.size+" selected");
    info.textContent=details.join(" · ");
    box.append(info);
    const actions=create("div",null,"row");
    const already=ui.compared.includes(rid);
    actions.append(action(already?"In comparison":"Compare edition",() => addRelease(rid)));
    if (already) actions.firstElementChild.disabled=true;
    const link=create("a","MusicBrainz");
    link.href="https://musicbrainz.org/release/"+encodeURIComponent(rid);
    link.target="_blank";link.rel="noopener noreferrer";
    actions.append(link);
    box.append(actions);
    return box;
  }
  function renderRecommendations() {
    const body=el("albumCandidates");body.replaceChildren();
    const grouped=new Map();
    for(const item of ui.recommendations) {
      const key=item.release_group_id||item.release_id;
      if(!grouped.has(key))grouped.set(key,[]);
      grouped.get(key).push(item);
    }
    for(const [group,entries] of grouped) {
      const section=create("section",null,"album-family");
      section.append(create("h4",entries[0].album+" · "+entries.length+" edition"+(entries.length!==1?"s":"")));
      const list=create("div",null,"album-candidate-list");
      for(const entry of entries)list.append(releaseCard(entry));
      section.append(list);body.append(section);
    }
    if(!grouped.size)body.append(create("p","Select recordings, then ask for candidate albums. Search for more releases below.","hint"));
  }
  async function suggest() {
    const user=ui.user,generation=ui.generation,ids=selectedIds();
    if(!ids.length){status("Select at least one recording first.",true,"albumRecommendStatus");return;}
    if(ids.length>500){status("Limit this comparison to 500 recordings.",true,"albumRecommendStatus");return;}
    status("Finding album editions from the local MusicBrainz cache…",false,"albumRecommendStatus");
    try {
      const result=await api("/api/catalogue/recommendations",{
        user_id:user,release_id:"00000000-0000-4000-8000-000000000000",track_ids:ids});
      if(!active(user,generation))return;
      ui.recommendations=result.candidates||[];
      for(const entry of ui.recommendations)ui.candidates.set(entry.release_id,entry);
      renderRecommendations();
      status(ui.recommendations.length+" candidate editions. "+
        (result.missing_cache?.length||0)+" recordings lack cached release links. "+
        "Coverage is compatibility, not proof of a preferred edition.",false,"albumRecommendStatus");
    } catch(error) {if(active(user,generation))status(error.message,true,"albumRecommendStatus");}
  }
  async function searchRelease(online=false) {
    const q=el("albumSearchQuery").value.trim();
    if(!q){status("Enter an album title or MusicBrainz release ID.",true,"albumSearchStatus");return;}
    const user=ui.user,generation=ui.generation;
    const exact=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(q);
    if(exact){await addRelease(q.toLowerCase());return;}
    if(q.length<2){status("Enter at least two characters.",true,"albumSearchStatus");return;}
    status(online?"Searching MusicBrainz with its shared rate limit…":"Searching cached releases…",false,"albumSearchStatus");
    try{
      const query=new URLSearchParams({user_id:user,kind:"release",q,online:String(online),local_only:"false"});
      const result=await api("/api/catalogue/search?"+query);
      if(!active(user,generation))return;
      const body=el("albumSearchResults");body.replaceChildren();
      for(const item of result.items||[]){
        ui.candidates.set(item.id,{release_id:item.id,album:item.name});
        body.append(releaseCard({id:item.id,name:item.name,
          date:item.data?.date,country:item.data?.country,status:item.data?.status}));
      }
      if(!result.items?.length)body.append(create("p","No releases found. Try another title or MusicBrainz ID.","hint"));
      status((result.items||[]).length+" matching editions; select Compare edition to add one.",false,"albumSearchStatus");
    }catch(error){if(active(user,generation))status(error.message,true,"albumSearchStatus");}
  }
  async function addRelease(id){
    if(ui.compared.includes(id))return;
    if(ui.compared.length>=4){
      status("Remove a compared edition before adding another. Planned track assignments are preserved.",true,"albumCompareStatus");
      el("albumCompared").scrollIntoView?.({block:"start",behavior:"smooth"});return;
    }
    ui.compared.push(id);invalidatePreview();await compare();
  }
  async function compare() {
    if(!ui.user)return;
    if(!ui.compared.length){renderCompared();renderMatrix();return;}
    const user=ui.user,generation=ui.generation;
    status("Loading exact release tracklists…",false,"albumCompareStatus");
    try{
      const result=await api("/api/albums/compare",{
        user_id:user,release_ids:ui.compared,selected_track_ids:selectedIds(),
        include_assigned:el("albumIncludeAssigned").checked});
      if(!active(user,generation))return;
      for(const release of result.releases||[])ui.releases.set(release.id,release);
      if(el("albumIncludeAssigned").checked) {
        let appended=0;
        for(const track of result.additional_assigned_matches||[]) {
          if(!ui.tracks.has(track.id)){ui.tracks.set(track.id,track);appended++;}
        }
        if(appended)renderTracks();
      }
      renderCompared();renderMatrix();
    }catch(error){if(active(user,generation))status(error.message,true,"albumCompareStatus");}
  }
  function renderCompared(){
    const body=el("albumCompared");body.replaceChildren();
    for(const rid of ui.compared){
      const release=ui.releases.get(rid);
      const box=create("article",null,"album-compared-card");
      box.append(create("strong",editionLabel(rid)));
      box.append(create("p",
        release?.complete ? release.track_count+" cached official tracklist entries" :
          "Tracklist incomplete — sync before assigning","hint"));
      const buttons=create("div",null,"row");
      buttons.append(action("Remove comparison",() => {
        ui.compared=ui.compared.filter(id=>id!==rid);
        invalidatePreview();renderCompared();renderMatrix();
      }));
      if(!release?.complete)buttons.append(action("Sync full tracklist",async()=>{
        await api("/api/catalogue/sync",{user_id:ui.user,kind:"release",id:rid,sections:["lookup"]});
        status("Release lookup queued. Use Refresh comparison after the catalogue worker finishes.",false,"albumCompareStatus");
      }));
      else buttons.append(action("Assign albumless matches",() => assignAlbumless(rid)));
      box.append(buttons);
      if(release?.complete){
        const details=create("details"),head=create("summary","Official tracklist ("+release.track_count+")");
        const list=create("ol",null,"album-official-tracks");
        for(const song of release.tracks){
          const li=create("li",(song.medium>1 ? "Disc "+song.medium+" · " : "")+
            song.position+". "+song.title);
          li.className="album-official-track";
          list.append(li);
        }
        if(release.track_count>release.tracks.length)
          list.append(create("li","Additional tracks not shown; use a smaller edition.","hint"));
        details.append(head,list);box.append(details);
      }
      body.append(box);
    }
    refreshSummary();
  }
  function assignAlbumless(rid){
    const release=ui.releases.get(rid);
    if(!release?.complete)return;
    let count=0;
    for(const item of allChosen()){
      if(item.current_release_id || ui.plan.has(item.id) || !hasExactMatch(release,item))continue;
      ui.plan.set(item.id,{action:"release",release_id:rid});count++;
    }
    invalidatePreview();renderMatrix();refreshSummary();
    status(count+" previously unassigned recordings added to the plan. Existing album assignments were not changed.",false,"albumPlanStatus");
  }
  function setDestination(id,value) {
    const track=ui.tracks.get(id);
    if(!track)return;
    if(!value || value==="keep" || value===track.current_release_id){
      ui.plan.delete(id);
    }else if(value==="not_found"){
      if(track.current_release_id)return;
      ui.plan.set(id,{action:"not_found"});
    }else {
      const release=ui.releases.get(value);
      if(!release || !hasExactMatch(release,track))return;
      ui.plan.set(id,{action:"release",release_id:value});
    }
    invalidatePreview();renderMatrix();
  }
  function findForTrack(track){
    el("albumSearchQuery").value=label(track);
    el("albumSearchForm").scrollIntoView?.({block:"start",behavior:"smooth"});
    void searchRelease(false);
  }
  function renderMatrix(){
    const body=el("albumMatrix");body.replaceChildren();
    const chosen=allChosen();
    if(!chosen.length){
      body.append(create("p","Select recordings from the list to compare release membership. You can then assign each to one edition or mark it No Album Found.","hint"));
      refreshSummary();return;
    }
    const table=create("table",null,"album-compare-table");
    const head=create("thead"),header=create("tr");
    for(const name of ["Recording","Current album",...ui.compared.map(editionLabel),"Proposed assignment","Other options"])
      header.append(create("th",name));
    head.append(header);table.append(head);
    const tbody=create("tbody");
    for(const track of chosen){
      const row=create("tr");
      row.append(create("td",label(track)));
      row.append(create("td",track.current_release_id ? editionLabel(track.current_release_id) :
        track.not_found ? "No Album Found" : "Unassigned"));
      for(const id of ui.compared){
        const release=ui.releases.get(id);
        const found=release?.complete ? hasExactMatch(release,track) : null;
        const cell=create("td", found===null?"?":found?"✓":"—",
          found===null?"album-match-unknown":found?"album-match-yes":"album-match-no");
        cell.title=found===null?"Tracklist not fully cached":found?"Exact recording ID present":"Recording ID absent";
        row.append(cell);
      }
      const cell=create("td"),select=create("select");
      select.setAttribute("aria-label","Assign album for "+label(track));
      const initial=create("option",track.current_release_id ? "Keep current album" : "Leave unassigned");
      initial.value="";select.append(initial);
      for(const [rid,release] of ui.releases){
        if(!hasExactMatch(release,track))continue;
        const option=create("option",editionLabel(rid));option.value=rid;select.append(option);
      }
      const decision=ui.plan.get(track.id);
      if(decision?.action==="not_found"){
        const option=create("option","No Album Found");option.value="not_found";select.append(option);
      }
      select.value=decision?.action==="release" ? decision.release_id :
        decision?.action==="not_found" ? "not_found" : "";
      select.addEventListener("change",() => setDestination(track.id,select.value));
      cell.append(select);row.append(cell);
      const more=create("td",null,"album-row-actions");
      more.append(action("Find more albums",() => findForTrack(track)));
      if(!track.current_release_id){
        more.append(action(decision?.action==="not_found"?"Undo No Album Found":"No Album Found",()=>{
          setDestination(track.id,decision?.action==="not_found"?"":"not_found");
        }));
      }
      row.append(more);tbody.append(row);
    }
    table.append(tbody);body.append(table);
    refreshSummary();
  }
  async function previewPlan() {
    if(!ui.plan.size){status("Select at least one album assignment or No Album Found decision.",true);return;}
    const user=ui.user,generation=ui.generation;
    status("Validating recording IDs, release tracklists and audio versions…");
    const decisions=[...ui.plan].map(([track_id,decision])=>({track_id,...decision}));
    try {
      const result=await api("/api/albums/plan-preview",{user_id:user,decisions});
      if(!active(user,generation))return;
      ui.preview=result;
      const table=create("table",null,"album-preview-table");
      const header=create("tr");
      for(const title of ["Recording","Existing release","New decision","Eligibility"])header.append(create("th",title));
      table.append(header);
      for(const item of result.items){
        const row=create("tr");
        for(const value of [
          item.title||item.track_id,
          item.old_release_id ? editionLabel(item.old_release_id) : "Unassigned",
          item.action==="not_found" ? "No Album Found" : item.target_album,
          item.status.replaceAll("_"," ")
        ])row.append(create("td",value));
        table.append(row);
      }
      el("albumPlanResults").replaceChildren(table);
      const eligible=result.can_apply+result.can_mark;
      el("albumPlanConfirm").hidden=eligible===0;
      status(result.can_apply+" eligible album changes; "+result.can_mark+
        " No Album Found decisions; "+(result.items.length-eligible)+
        " unchanged or blocked. Only eligible decisions are applied.");
      el("albumPlanResults").scrollIntoView?.({block:"nearest",behavior:"smooth"});
    }catch(error){if(active(user,generation))status(error.message,true);}
  }
  async function applyPlan(){
    const preview=ui.preview;
    if(!preview)return;
    const user=ui.user,generation=ui.generation;
    if(!confirm("Confirm "+preview.can_apply+" album tag changes and "+preview.can_mark+
      " No Album Found decisions? Album changes retain individual undo backups. No recording IDs or file paths change."))return;
    el("albumPlanApply").disabled=true;
    try{
      const result=await api("/api/albums/plan-apply",{user_id:user,preview_token:preview.preview_token});
      if(!active(user,generation))return;
      ui.preview=null;ui.plan.clear();
      el("albumPlanResults").replaceChildren();el("albumPlanConfirm").hidden=true;
      status(result.queued+" album-only changes queued · "+
        result.marked_not_found+" recordings marked No Album Found. "+
        "Refresh history when the background worker finishes.");
      refreshSummary();await Promise.all([loadTracks(true),loadHistory()]);
    }catch(error){if(active(user,generation))status(error.message,true);}
    finally{el("albumPlanApply").disabled=false;}
  }
  async function loadHistory(){
    const user=ui.user,generation=ui.generation;
    if(!user)return;
    try{
      const result=await api("/api/catalogue/album-history?user_id="+encodeURIComponent(user));
      if(!active(user,generation))return;
      const body=el("albumHistory");body.replaceChildren();
      for(const change of (result||[]).slice(0,100)){
        const row=create("article",null,"album-history-row");
        row.append(create("span",change.track_id.slice(0,8)+" · "+
          editionLabel(change.release_id)+" · "+change.status));
        if(change.status==="DONE")row.append(action("Undo",async()=>{
          if(!confirm("Undo this individual album change from its backup?"))return;
          await api("/api/catalogue/album-undo",{user_id:user,change_id:change.id});
          status("Album undo queued. Refresh history after it completes.");
          await loadHistory();
        }));
        if(change.error)row.append(create("p",change.error,"error-text"));
        body.append(row);
      }
      if(!result?.length)body.append(create("p","No album changes yet.","hint"));
    }catch(error){if(active(user,generation))status(error.message,true);}
  }
  async function enter(){
    if(!state.user)return;
    if(ui.user===state.user){
      if(!ui.tracks.size)await loadTracks(true);
      return;
    }
    reset();
    ui.user=state.user;
    const user=ui.user,generation=ui.generation;
    try {
      const sources=await api("/api/catalogue/sources?user_id="+encodeURIComponent(user));
      if(!active(user,generation))return;
      const chooser=el("albumSource");
      const first=create("option","All library sources");first.value="";
      chooser.replaceChildren(first);
      for(const source of sources){
        const option=create("option",source.title);option.value=source.id;chooser.append(option);
      }
      await Promise.all([loadTracks(true),loadHistory()]);
    }catch(error){if(active(user,generation))status(error.message,true);}
  }
  function leave(){ui.generation++;}
  function reset(){
    ui.generation++;ui.user=null;ui.loading=false;ui.tracks.clear();ui.selected.clear();
    ui.plan.clear();ui.candidates.clear();ui.compared=[];ui.releases.clear();
    ui.recommendations=[];ui.preview=null;ui.offset=0;ui.hasMore=false;ui.total=0;
    for(const id of ["albumTracks","albumCandidates","albumSearchResults","albumCompared",
      "albumMatrix","albumPlanResults","albumHistory"])el(id).replaceChildren();
    for(const id of ["albumTrackCount","albumRecommendStatus","albumSearchStatus",
      "albumCompareStatus","albumPlanStatus"])el(id).textContent="";
    el("albumIncludeAssigned").checked=false;el("albumTrackSearch").value="";
    el("albumSearchQuery").value="";el("albumSource").value="";
    el("albumPlanConfirm").hidden=true;el("albumLoadMore").hidden=true;
    renderRecommendations();renderMatrix();refreshSummary();
  }
  el("albumRefresh").addEventListener("click",()=>loadTracks(true));
  el("albumSource").addEventListener("change",()=>loadTracks(true));
  el("albumIncludeAssigned").addEventListener("change",()=>{
    void loadTracks(true).then(()=>compare());
  });
  el("albumTrackSearch").addEventListener("input",renderTracks);
  el("albumSelectVisible").addEventListener("click",()=>{
    const query=el("albumTrackSearch").value.trim().toLocaleLowerCase();
    for(const track of ui.tracks.values()){
      if(ui.selected.size>=500)break;
      if(!query||label(track).toLocaleLowerCase().includes(query))ui.selected.add(track.id);
    }
    renderTracks();invalidatePreview();renderMatrix();
  });
  el("albumClearSelection").addEventListener("click",()=>{
    ui.selected.clear();renderTracks();invalidatePreview();renderMatrix();
  });
  el("albumLoadMore").addEventListener("click",()=>loadTracks(false));
  el("albumRecommend").addEventListener("click",()=>suggest());
  el("albumSearchForm").addEventListener("submit",event=>{event.preventDefault();void searchRelease(false);});
  el("albumOnlineSearch").addEventListener("click",()=>searchRelease(true));
  el("albumPlanPreview").addEventListener("click",()=>previewPlan());
  el("albumPlanApply").addEventListener("click",()=>applyPlan());
  el("albumPlanCancel").addEventListener("click",invalidatePreview);
  el("albumHistoryRefresh").addEventListener("click",()=>loadHistory());
  return {enter,leave,reset};
})();
