/* Connected album clusters, user-scoped draft assignment and manual per-track override. */
"use strict";
window.AlbumUI = (() => {
  const $ = id => document.getElementById(id);
  const element = (tag,text=null,cls=null) => {
    const item=document.createElement(tag);
    if(text!=null)item.textContent=String(text);
    if(cls)item.className=cls;
    return item;
  };
  const button = (name,callback,cls="secondary") => {
    const el=element("button",name,cls);
    el.type="button";
    el.addEventListener("click",()=>Promise.resolve().then(callback).catch(error=>notice(error.message,true)));
    return el;
  };
  const ui={user:null,serial:0,clusters:[],detail:null,focus:null,work:new Map(),
    preview:null,searchTimer:null,preferences:[],sources:[],fetching:false};
  const active=(user,serial)=>state.user===user && ui.user===user &&
    state.tab==="album" && ui.serial===serial;
  const notice=(message,error=false,id="albumClusterStatus")=>{
    const target=$(id);target.textContent=message||"";
    target.className=error?"error-text":"hint";
  };
  const title=(item)=>item.english_title||item.name||item.display_title||item.title||"Unknown";
  const native=(item)=>item.original_title||item.name||item.title||"";
  const bilingual=(item,cls="album-bilingual")=>{
    const wrapper=element("div",null,cls);
    const original=native(item),english=title(item);
    wrapper.append(element("strong",original||english,"album-original"));
    if(english && original && english!==original)
      wrapper.append(element("span",english,"album-english"));
    return wrapper;
  };
  const trackLabel=t=>t.display_title||t.english_title||t.original_title||t.id;
  function clusterWork(id=ui.focus){
    if(!ui.work.has(id))ui.work.set(id,{resolved:new Set(),decisions:new Map(),selected:null,extra:new Map()});
    return ui.work.get(id);
  }
  function releases(){
    const detail=ui.detail;
    if(!detail)return [];
    const candidates=new Map((detail.releases||[]).map(a=>[a.id,a]));
    for(const [id,album] of clusterWork().extra)candidates.set(id,album);
    return [...candidates.values()].sort((a,b)=>b.matched_count-a.matched_count ||
      b.linked_track_count-a.linked_track_count ||
      Number(a.status!=="Official")-Number(b.status!=="Official") ||
      String(a.name).localeCompare(String(b.name)));
  }
  function remaining(){
    if(!ui.detail)return [];
    const work=clusterWork();
    return ui.detail.tracks.filter(t=>!t.locked && !t.not_found && !work.resolved.has(t.id));
  }
  function matched(album,track){
    if(!album?.complete)return null;
    return album.matched_track_ids.includes(track.id);
  }
  function invalidate(){
    ui.preview=null;
    $("albumPlanResults").replaceChildren();
    $("albumPlanConfirm").hidden=true;
    $("albumPlanStatus").textContent="";
  }
  function planSummary(){
    if(!ui.detail){
      $("albumPlanSummary").textContent="Select a cluster to begin.";
      $("albumPlanPreview").disabled=true;
      $("albumPlanClear").disabled=true;return;
    }
    const work=clusterWork(),left=remaining();
    const moves=[...work.decisions.entries()].filter(([id,d])=>d.action==="release" &&
      ui.detail.tracks.find(t=>t.id===id)?.current_release_id).length;
    const newItems=[...work.decisions.values()].filter(d=>d.action==="release").length-moves;
    const none=[...work.decisions.values()].filter(d=>d.action==="not_found").length;
    const reviewed=ui.detail.tracks.length-left.length;
    $("albumPlanSummary").textContent=
      reviewed+"/"+ui.detail.tracks.length+" reviewed · "+
      left.length+" still need a decision · "+newItems+" new assignments · "+
      moves+" album moves · "+none+" No Album Found. "+
      "Files change only after the final confirmation.";
    $("albumRemainingStatus").textContent=left.length
      ? left.length+" remaining in this cluster. Choose another edition for any excluded songs."
      : "Cluster reviewed. Confirm the pending changes, then continue to the next cluster.";
    $("albumPlanPreview").disabled=work.decisions.size===0;
    $("albumPlanClear").disabled=!work.decisions.size&&!work.resolved.size;
    $("albumMarkUnfound").disabled=!left.some(t=>!t.current_release_id);
    $("albumKeepAssigned").disabled=!left.some(t=>t.current_release_id);
  }
  function renderNavigation(){
    const list=$("albumClusterList");list.replaceChildren();
    const i=ui.clusters.findIndex(x=>x.id===ui.focus),count=ui.clusters.length;
    $("albumClusterPosition").textContent=count?(i+1)+" / "+count:"0 / 0";
    $("albumClusterCount").textContent=count+" groups";
    $("albumPrev").disabled=i<=0;
    $("albumNext").disabled=i<0||i>=count-1;
    for(let index=0;index<count;index++){
      const cluster=ui.clusters[index],row=button("",()=>selectCluster(cluster.id),
        "album-cluster-choice"+(cluster.id===ui.focus?" is-active":""));
      row.setAttribute("role","option");
      row.setAttribute("aria-selected",String(cluster.id===ui.focus));
      row.append(element("span",String(index+1).padStart(2,"0"),"album-cluster-number"));
      const info=element("span",null,"album-cluster-choice-body");
      info.append(element("span",cluster.title,"album-cluster-name"));
      info.append(element("small",cluster.track_count+" tracks · "+cluster.candidate_count+" editions"+
        (cluster.locked?" · "+cluster.locked+" locked":"")));
      row.append(info);list.append(row);
      if(cluster.id===ui.focus)row.scrollIntoView?.({block:"nearest"});
    }
  }
  async function loadClusters(focus=ui.focus){
    if(!ui.user)return;
    const user=ui.user,serial=++ui.serial;
    const query=new URLSearchParams({user_id:user,
      include_assigned:String($("albumIncludeAssigned").checked),
      q:$("albumClusterSearch").value.trim()});
    if(focus)query.set("focus_id",focus);
    ui.fetching=true;
    notice("Building clusters from exact cached MusicBrainz relationships…");
    try {
      const response=await api("/api/albums/clusters?"+query);
      if(!active(user,serial))return;
      ui.clusters=response.clusters||[];
      ui.focus=response.detail?.id||null;
      ui.detail=response.detail||null;
      ui.preview=null;
      $("albumPlanResults").replaceChildren();$("albumPlanConfirm").hidden=true;
      notice(response.total_tracks+" recordings in "+response.total_clusters+
        " connected cluster(s). Unindexed relationships may appear as single-song clusters.");
      const work=ui.detail&&clusterWork();
      if(work?.selected && !releases().some(a=>a.id===work.selected))work.selected=null;
      renderAll();
    } catch(error){if(active(user,serial))notice(error.message,true);}
    finally{if(active(user,serial))ui.fetching=false;}
  }
  async function selectCluster(id){
    if(!id||id===ui.focus)return;
    await loadClusters(id);
  }
  function renderAlbums(){
    const body=$("albumReleaseList");body.replaceChildren();
    const detail=ui.detail;
    if(!detail){body.append(element("p","No matching clusters. Change the music-title filter.","hint"));return;}
    const work=clusterWork(),albums=releases();
    $("albumClusterSummary").textContent=detail.tracks.length+" recordings · "+
      detail.total_releases+" linked editions";
    if(!albums.length)body.append(element("p",
      "No known release editions for these recordings. Index the recording in Catalogue, or find another release below.",
      "hint"));
    for(const release of albums){
      const card=button("",()=>selectEdition(release.id),
        "album-edition-choice"+(work.selected===release.id?" is-active":""));
      card.setAttribute("role","option");
      card.setAttribute("aria-selected",String(work.selected===release.id));
      card.append(bilingual(release));
      const meta=element("div",null,"album-edition-meta");
      const coverage=release.complete
        ? release.matched_count+"/"+detail.tracks.length+" exact matches"
        : release.linked_track_count+" linked · tracklist not cached";
      meta.append(element("span",coverage,"album-edition-count"),
        element("small",[release.date,release.country,release.status,
          release.track_count?release.track_count+" official tracks":null].filter(Boolean).join(" · ")));
      card.append(meta);body.append(card);
    }
    if(detail.releases_truncated)body.append(element("p",
      "Only the most relevant editions are displayed. Search for more below.","hint"));
    renderSelected();
  }
  function selectEdition(id){
    const item=releases().find(a=>a.id===id);
    if(!item)return;
    clusterWork().selected=id;
    renderAlbums();renderMusic();
  }
  function renderSelected(){
    const work=ui.detail&&clusterWork();
    const album=work&&releases().find(a=>a.id===work.selected);
    $("albumSelectedTitle").replaceChildren();
    const button=$("albumEditionUse");
    if(!album){button.disabled=true;$("albumSelectedTitle").textContent="Choose an album to see excluded recordings.";return;}
    $("albumSelectedTitle").append(element("span","Selected: ","hint"),bilingual(album));
    button.disabled=false;
    button.textContent=album.complete
      ? "Use this album for remaining matching songs"
      : "Sync this edition's complete tracklist";
  }
  async function useEdition(){
    const detail=ui.detail,work=detail&&clusterWork();
    const album=work&&releases().find(a=>a.id===work.selected);
    if(!album)return;
    if(!album.complete){
      try{
        await api("/api/catalogue/sync",{user_id:ui.user,kind:"release",id:album.id,sections:["lookup"]});
        notice("Release lookup queued. Refresh clusters after MusicBrainz synchronization.",false,"albumReleaseStatus");
      }catch(error){notice(error.message,true,"albumReleaseStatus");}
      return;
    }
    let count=0,same=0;
    for(const track of remaining()){
      if(!matched(album,track))continue;
      work.resolved.add(track.id);
      if(track.current_release_id===album.id){same++;continue;}
      work.decisions.set(track.id,{action:"release",release_id:album.id});count++;
    }
    invalidate();
    renderMusic();planSummary();
    notice(count+" assignments planned; "+same+" were already correct. "+
      remaining().length+" recordings still need another edition or review.");
  }
  function renderMusic(){
    const detail=ui.detail,work=detail&&clusterWork();
    const body=$("albumMusicList");body.replaceChildren();
    $("albumMusicHeading").textContent="Recordings in this cluster";
    $("albumMusicContext").textContent="";
    if(!detail){
      body.append(element("p","Select a cluster to inspect its music and editions.","hint"));return;
    }
    const album=releases().find(a=>a.id===work.selected);
    if(album){
      $("albumMusicHeading").textContent="Music not in this edition";
      $("albumMusicContext").textContent=album.complete
        ? "Red: absent from this edition. Green: exact recording included. "+
          "Only unresolved matching music is added when you use an edition."
        : "The tracklist has not been fetched; exclusion cannot be determined yet.";
    }else $("albumMusicContext").textContent=
      "Choose an edition in the middle column. All recordings are shown until then.";
    let shown=0,hidden=0;
    for(const track of detail.tracks){
      const match=album?matched(album,track):null;
      if(album&&match===true&&!$("albumShowIncluded").checked){hidden++;continue;}
      const color=album ? match===true?"album-music-included":
        match===false?"album-music-excluded":"album-music-unknown" : "album-music-neutral";
      const card=element("article",null,"album-track-comparison "+color);
      card.append(bilingual(track));
      const meta=element("div",null,"album-music-info");
      const proposal=work.decisions.get(track.id);
      const stateText=track.locked?"Album locked":
        proposal?.action==="not_found"?"No Album Found planned":
        proposal?.action==="release"?"Planned: "+(releases().find(a=>a.id===proposal.release_id)?.name||"selected edition"):
        track.not_found?"Previously reviewed: No Album Found":
        work.resolved.has(track.id)?"Reviewed: keep existing album":
        "Still needs an album decision";
      const current=track.current_release_id?"Current album: "+track.current_release_id.slice(0,8)+"…":"No current album";
      meta.append(element("small",current),element("small",stateText));
      card.append(meta);
      if(track.not_found){
        card.append(button("Search again",async()=>{
          const user=ui.user;
          await api("/api/albums/not-found/clear",{user_id:user,track_id:track.id});
          if(state.user===user){track.not_found=false;work.resolved.delete(track.id);renderAll();}
        }));
      }
      body.append(card);shown++;
    }
    if(!shown){
      body.append(element("p",album&&hidden
        ? hidden+" matching songs hidden. Enable ‘Show included songs’ to see them."
        : "There are no recordings to display.","hint"));
    }
    planSummary();
  }
  function resolveRemainder(action){
    if(!ui.detail)return;
    const work=clusterWork();
    for(const track of remaining()){
      if(action==="not_found" && !track.current_release_id){
        work.decisions.set(track.id,{action:"not_found"});work.resolved.add(track.id);
      }else if(action==="keep" && track.current_release_id){
        work.resolved.add(track.id);
      }
    }
    invalidate();renderMusic();
    notice("Remaining recordings reviewed. Review the plan before applying audio-tag changes.");
  }
  function resetCluster(){
    if(!ui.detail)return;
    if(!confirm("Clear all staged album decisions in this cluster? No audio has been changed by these drafts."))return;
    ui.work.delete(ui.focus);invalidate();renderAll();
  }
  function renderAll(){
    renderNavigation();renderAlbums();renderMusic();planSummary();renderPreference();
  }
  async function previewPlan(){
    if(!ui.detail)return;
    const work=clusterWork(),decisions=[...work.decisions].map(([track_id,data])=>({track_id,...data}));
    if(!decisions.length){notice("No audio-tag changes in the plan. Continue to another cluster.");return;}
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    notice("Checking exact release membership, album locks, audio versions and undo backup capacity…");
    try {
      const result=await api("/api/albums/plan-preview",{user_id:user,decisions});
      if(!active(user,serial)||ui.focus!==focus)return;
      ui.preview=result;
      const table=element("table",null,"album-preview-table");
      const header=element("tr");
      ["Recording","From","To","Result"].forEach(t=>header.append(element("th",t)));
      table.append(header);
      for(const change of result.items){
        const row=element("tr");
        [change.title||change.track_id,change.old_release_id||"Unassigned",
          change.target_album||"No Album Found",change.status.replaceAll("_"," ")].forEach(t=>row.append(element("td",t)));
        table.append(row);
      }
      const impact=element("div",null,"album-impact");
      for(const row of result.impact||[]){
        const name=releases().find(a=>a.id===row.release_id)?.name||row.release_id.slice(0,8);
        impact.append(element("span",name+": "+row.before+" → "+row.after+" songs","album-impact-item"));
      }
      $("albumPlanResults").replaceChildren(impact,table);
      $("albumPlanConfirm").hidden=!result.can_apply&&!result.can_mark;
      notice(result.can_apply+" album updates and "+result.can_mark+
        " No Album Found reviews are eligible. "+
        (result.items.length-result.can_apply-result.can_mark)+" unchanged or blocked.",false,"albumPlanStatus");
      $("albumPlanResults").scrollIntoView?.({block:"nearest"});
    }catch(error){if(active(user,serial))notice(error.message,true,"albumPlanStatus");}
  }
  async function applyPlan(){
    const data=ui.preview,work=ui.detail&&clusterWork();
    if(!data||!work)return;
    if(!confirm("Apply "+data.can_apply+" album changes and "+data.can_mark+
      " No Album Found decisions? Changed files keep reversible backups."))return;
    const user=ui.user,serial=ui.serial;
    $("albumPlanApply").disabled=true;
    try{
      const result=await api("/api/albums/plan-apply",{user_id:user,preview_token:data.preview_token});
      if(!active(user,serial))return;
      work.decisions.clear();invalidate();
      notice(result.queued+" reversible album updates queued; "+result.marked_not_found+
        " No Album Found decisions saved. Excluded music remains in this cluster.");
      renderMusic();loadHistory();
    }catch(error){if(active(user,serial))notice(error.message,true,"albumPlanStatus");}
    finally{$("albumPlanApply").disabled=false;}
  }
  async function findMore(online=false){
    const input=$("albumReleaseSearchInput").value.trim(),user=ui.user,serial=ui.serial;
    if(!ui.detail||!input)return;
    const resultBox=$("albumReleaseSearchResults");resultBox.replaceChildren();
    const add=async rid=>{
      const params=new URLSearchParams({user_id:user,anchor_id:ui.focus,release_id:rid,
        include_assigned:String($("albumIncludeAssigned").checked)});
      const edition=await api("/api/albums/cluster-edition?"+params);
      if(!active(user,serial))return;
      clusterWork().extra.set(rid,edition);clusterWork().selected=rid;
      renderAlbums();renderMusic();
    };
    const exact=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(input);
    if(exact){try{await add(input.toLowerCase());}catch(e){notice(e.message,true,"albumReleaseStatus");}return;}
    if(input.length<2){notice("Enter at least 2 characters.",true,"albumReleaseStatus");return;}
    notice(online?"Searching MusicBrainz…":"Searching cached release editions…",false,"albumReleaseStatus");
    try{
      const query=new URLSearchParams({user_id:user,kind:"release",q:input,
        local_only:"false",online:String(online)});
      const data=await api("/api/catalogue/search?"+query);
      if(!active(user,serial))return;
      for(const item of data.items||[]){
        const row=element("div",null,"album-more-result");
        row.append(element("span",item.name||"Unknown edition"));
        row.append(button("Compare",()=>add(item.id)));
        resultBox.append(row);
      }
      if(!data.items?.length)resultBox.append(element("p","No editions found. Try an exact release UUID.","hint"));
      notice((data.items||[]).length+" results. Add an edition without resetting the cluster plan.",
        false,"albumReleaseStatus");
    }catch(error){if(active(user,serial))notice(error.message,true,"albumReleaseStatus");}
  }
  async function loadHistory(){
    const user=ui.user,serial=ui.serial;
    if(!user)return;
    try{
      const rows=await api("/api/catalogue/album-history?user_id="+encodeURIComponent(user));
      if(!active(user,serial))return;
      const body=$("albumHistory");body.replaceChildren();
      for(const change of (rows||[]).slice(0,100)){
        const row=element("article",null,"album-history-row");
        row.append(element("span",
          change.track_id.slice(0,8)+" · "+change.release_id.slice(0,8)+" · "+change.status));
        if(change.status==="DONE")row.append(button("Undo",async()=>{
          if(!confirm("Undo this individual album change using its saved backup?"))return;
          await api("/api/catalogue/album-undo",{user_id:user,change_id:change.id});
          notice("Undo queued. Refresh history after the worker finishes.");await loadHistory();
        }));
        if(change.error && ["QUEUED","PREPARED","UNDO_PREPARED"].includes(change.status))
          row.append(button("Retry",async()=>{
            await api("/api/catalogue/album-retry",{user_id:user,change_id:change.id});
            notice("Interrupted album operation requeued.");await loadHistory();
          }));
        if(change.error)row.append(element("small",change.error,"error-text"));
        body.append(row);
      }
      if(!rows?.length)body.append(element("p","No album changes yet.","hint"));
    }catch(error){if(active(user,serial))notice(error.message,true,"albumPlanStatus");}
  }
  function renderPreference(){
    const src=$("albumPreferenceSource").value;
    const pref=ui.preferences.find(p=>p.source_id===src);
    const selector=$("albumPreferredRelease"),selected=selector.value;
    selector.replaceChildren();
    const first=element("option","Select an edition");first.value="";selector.append(first);
    const ids=new Set(releases().map(r=>r.id));
    if(pref?.release_id)ids.add(pref.release_id);
    for(const id of ids){
      const opt=element("option",releases().find(x=>x.id===id)?.name||id);
      opt.value=id;selector.append(opt);
    }
    selector.value=pref?.release_id||([...ids].includes(selected)?selected:"");
    $("albumPreferAuto").checked=Boolean(pref?.auto_apply);
    $("albumPreferenceSave").disabled=!src;
    $("albumPreferenceClear").disabled=!src||!pref;
    $("albumPreferenceStatus").textContent=src
      ? (pref?"Current preference: "+(pref.release_id||""):"No preference for this source")
      : "Choose a source to manage future-recording preferences.";
  }
  async function loadPreferences(){
    const user=ui.user,serial=ui.serial;
    try{
      const values=await api("/api/catalogue/preferences?user_id="+encodeURIComponent(user));
      if(!active(user,serial))return;
      ui.preferences=values||[];renderPreference();
    }catch(error){if(active(user,serial))notice(error.message,true,"albumPreferenceStatus");}
  }
  async function savePreference(clear=false){
    const src=$("albumPreferenceSource").value,user=ui.user,serial=ui.serial;
    if(!src){notice("Select a source first.",true,"albumPreferenceStatus");return;}
    const id=$("albumPreferredRelease").value;
    if(!clear&&!id){notice("Choose an exact edition first.",true,"albumPreferenceStatus");return;}
    if(!clear&&$("albumPreferAuto").checked&&!confirm("Apply this edition automatically to future matching songs from the selected source? Existing tags are unchanged."))return;
    try {
      await api("/api/catalogue/preference",{user_id:user,source_id:src,release_id:clear?null:id,
        enabled:!clear&&$("albumPreferAuto").checked,clear});
      if(!active(user,serial))return;
      await loadPreferences();
      notice(clear?"Source preference cleared.":"Source preference saved.",false,"albumPreferenceStatus");
    }catch(error){if(active(user,serial))notice(error.message,true,"albumPreferenceStatus");}
  }
  async function enter(){
    if(!state.user)return;
    if(ui.user!==state.user){
      reset();ui.user=state.user;
      const user=ui.user,serial=ui.serial;
      try{
        const sources=await api("/api/catalogue/sources?user_id="+encodeURIComponent(user));
        if(!active(user,serial))return;
        ui.sources=sources||[];
        const selector=$("albumPreferenceSource"),initial=element("option","Choose a source");
        initial.value="";selector.replaceChildren(initial);
        for(const source of ui.sources){
          const opt=element("option",source.title);opt.value=source.id;selector.append(opt);
        }
      }catch(error){if(active(user,serial))notice(error.message,true);}
    }
    await loadClusters(ui.focus);
    void loadHistory();
    void loadPreferences();
  }
  function leave(){ui.serial++;clearTimeout(ui.searchTimer);}
  function reset(){
    ui.serial++;clearTimeout(ui.searchTimer);ui.searchTimer=null;
    ui.user=null;ui.clusters=[];ui.detail=null;ui.focus=null;ui.preview=null;
    ui.work.clear();ui.sources=[];ui.preferences=[];ui.fetching=false;
    $("albumIncludeAssigned").checked=false;$("albumShowIncluded").checked=false;
    $("albumClusterSearch").value="";$("albumReleaseSearchInput").value="";
    $("albumReleaseSearchResults").replaceChildren();$("albumPlanResults").replaceChildren();
    $("albumPlanConfirm").hidden=true;renderAll();
  }

  // The Music tab's details pane keeps a one-recording override and lock.
  // It reuses the same reviewed album-only jobs as cluster planning.
  function mountTrack(track,container,apiRequest,onChange){
    const user=track.user_id,id=track.id;
    const section=element("section",null,"details-block album-track-editor");
    section.append(element("h3","Album assignment & lock"));
    section.append(element("p","Current release: "+(track.release_id||"No album assigned"),"hint"));
    const lockState=element("p","Checking album lock…","hint");
    const toggle=button("Lock album",async()=>{
      if(user!==state.user)return;
      if(!isLocked&&!confirm("Lock this album? Automatic and manual album changes will be blocked until you unlock it."))return;
      try{
        const data=await apiRequest("/api/albums/lock",
          {user_id:user,track_id:id,locked:!isLocked});
        isLocked=data.locked;drawLock();
        await onChange?.();
      }catch(error){message.textContent=error.message;}
    });
    let isLocked=false;
    const message=element("p",null,"hint");
    const drawLock=()=>{
      lockState.textContent=isLocked?"Album locked: assignments are frozen.":"Album unlocked: an edition can be reviewed.";
      toggle.textContent=isLocked?"Unlock album":"Lock album";
    };
    section.append(lockState,toggle);
    const form=element("form",null,"album-track-search");
    const label=element("label","Search release name or enter an exact MusicBrainz release UUID","control-field");
    const input=element("input");input.type="search";input.maxLength=180;
    input.placeholder="Release title or UUID";label.append(input);
    const actions=element("div",null,"row");
    const cached=element("button","Search cache","secondary");cached.type="submit";
    const online=button("Search MusicBrainz",()=>search(true));
    actions.append(cached,online);form.append(label,actions);
    const results=element("div",null,"album-track-search-results");
    const preview=element("div",null,"album-track-change-preview");
    async function inspect(release_id){
      if(isLocked){message.textContent="Unlock the album before changing it.";return;}
      message.textContent="Checking exact recording membership and audio safety…";
      try{
        const result=await apiRequest("/api/albums/plan-preview",{user_id:user,
          decisions:[{track_id:id,action:"release",release_id}]});
        if(user!==state.user)return;
        preview.replaceChildren();
        const item=result.items?.[0];
        preview.append(element("p",(item?.target_album||release_id)+" · "+(item?.status||"Unknown"),"hint"));
        if(item?.status==="COMPATIBLE"){
          preview.append(button("Confirm this album change",async()=>{
            if(!confirm("Queue a reversible album-only tag change for this recording?"))return;
            try {
              const applied=await apiRequest("/api/albums/plan-apply",
                {user_id:user,preview_token:result.preview_token});
              message.textContent=applied.queued+" album change queued. The file remains at its stable path.";
              await onChange?.();
            }catch(error){message.textContent=error.message;}
          },""));
        }else if(item?.status==="UNCHANGED"){
          preview.append(element("p","This recording already has that edition.","hint"));
        }else preview.append(element("p",
          "This edition cannot be used. The recording must appear in its complete cached tracklist, and the current audio must be idle.","hint"));
      }catch(error){message.textContent=error.message;}
    }
    async function search(remote=false){
      const q=input.value.trim();
      if(!q)return;
      results.replaceChildren();preview.replaceChildren();
      const exact=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(q);
      if(exact){await inspect(q.toLowerCase());return;}
      if(q.length<2){message.textContent="Enter at least two characters.";return;}
      try{
        const params=new URLSearchParams({user_id:user,kind:"release",q,online:String(remote),local_only:"false"});
        const data=await apiRequest("/api/catalogue/search?"+params);
        if(user!==state.user)return;
        for(const candidate of data.items||[]){
          const row=element("div",null,"album-track-search-row");
          row.append(element("span",candidate.name));
          row.append(button("Review",()=>inspect(candidate.id)));
          results.append(row);
        }
        if(!data.items?.length)results.append(element("p","No matching releases in this search.","hint"));
      }catch(error){message.textContent=error.message;}
    }
    form.addEventListener("submit",e=>{e.preventDefault();void search(false);});
    section.append(form,results,preview,message);
    container.prepend(section);
    apiRequest("/api/albums/lock?"+new URLSearchParams({user_id:user,track_id:id})).then(data=>{
      if(user!==state.user)return;isLocked=Boolean(data.locked);drawLock();
    }).catch(error=>{lockState.textContent=error.message;});
  }

  $("albumRefresh").addEventListener("click",()=>loadClusters(ui.focus));
  $("albumIncludeAssigned").addEventListener("change",()=>{
    ui.work.clear();ui.focus=null;ui.detail=null;void loadClusters(null);
  });
  $("albumClusterSearch").addEventListener("input",()=>{
    clearTimeout(ui.searchTimer);
    ui.searchTimer=setTimeout(()=>loadClusters(null),280);
  });
  $("albumPrev").addEventListener("click",()=>{
    const index=ui.clusters.findIndex(c=>c.id===ui.focus);if(index>0)void selectCluster(ui.clusters[index-1].id);
  });
  $("albumNext").addEventListener("click",()=>{
    const index=ui.clusters.findIndex(c=>c.id===ui.focus);
    if(index>=0&&index<ui.clusters.length-1)void selectCluster(ui.clusters[index+1].id);
  });
  $("albumShowIncluded").addEventListener("change",renderMusic);
  $("albumEditionUse").addEventListener("click",()=>useEdition());
  $("albumMarkUnfound").addEventListener("click",()=>resolveRemainder("not_found"));
  $("albumKeepAssigned").addEventListener("click",()=>resolveRemainder("keep"));
  $("albumPlanClear").addEventListener("click",resetCluster);
  $("albumPlanPreview").addEventListener("click",()=>previewPlan());
  $("albumPlanApply").addEventListener("click",()=>applyPlan());
  $("albumPlanCancel").addEventListener("click",invalidate);
  $("albumReleaseSearchForm").addEventListener("submit",event=>{event.preventDefault();void findMore(false);});
  $("albumReleaseSearchOnline").addEventListener("click",()=>findMore(true));
  $("albumHistoryRefresh").addEventListener("click",()=>loadHistory());
  $("albumPreferenceSource").addEventListener("change",renderPreference);
  $("albumPreferenceSave").addEventListener("click",()=>savePreference(false));
  $("albumPreferenceClear").addEventListener("click",()=>savePreference(true));
  return {enter,leave,reset,mountTrack};
})();
