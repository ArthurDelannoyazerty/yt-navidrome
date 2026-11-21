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
    preview:null,searchTimer:null,pollTimer:null,familyTimer:null,preferences:[],sources:[],fetching:false};
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
    if(!ui.work.has(id))ui.work.set(id,{
      resolved:new Set(),selected:null,extra:new Map(),unchecked:new Set(),
      manual:new Map(),edition:null,loadingId:null,loadingSerial:null
    });
    return ui.work.get(id);
  }
  function releases(){
    if(!ui.detail)return [];
    const candidates=new Map((ui.detail.releases||[]).map(a=>[a.id,a]));
    for(const [id,album] of clusterWork().extra)candidates.set(id,album);
    return [...candidates.values()].sort((a,b)=>b.matched_count-a.matched_count ||
      b.linked_track_count-a.linked_track_count ||
      Number(a.status!=="Official")-Number(b.status!=="Official") ||
      String(a.name).localeCompare(String(b.name)));
  }
  function activeEdition(){
    if(!ui.detail)return null;
    const work=clusterWork();
    return work.edition?.id===work.selected?work.edition:null;
  }
  function remaining(){
    if(!ui.detail)return [];
    const work=clusterWork();
    return ui.detail.tracks.filter(t=>!t.locked&&!t.not_found&&!work.resolved.has(t.id));
  }
  // The checkbox only controls initial selection. A deliberate click (or
  // Select all) may include an already-assigned local track without it.
  const canChoose=track=>Boolean(track&&!track.locked&&!track.out_of_scope);
  const choiceKey=(edition,track)=>edition.id+":"+track.id;
  function selectedRecordingFor(track,edition){
    if(!canChoose(track)||clusterWork().resolved.has(track.id))return null;
    const work=clusterWork(),key=choiceKey(edition,track);
    if(work.manual.has(key))return work.manual.get(key);
    if(track.in_scope!==false && edition.matched_track_ids.includes(track.id) &&
       !work.unchecked.has(key))return track.mbid;
    return null;
  }
  function setSelection(track,edition,target,checked){
    if(!canChoose(track))return;
    const work=clusterWork(),key=choiceKey(edition,track);
    if(!checked){
      work.manual.delete(key);work.unchecked.add(key);
    }else{
      if(!edition.tracks.some(song=>song.recording_id===target))return;
      // One local copy per official position: never silently submit two
      // different local tracks with the same destination recording MBID.
      for(const other of ui.detail.tracks){
        if(other.id!==track.id && selectedRecordingFor(other,edition)===target){
          work.unchecked.add(choiceKey(edition,other));
          work.manual.delete(choiceKey(edition,other));
        }
      }
      work.unchecked.delete(key);
      if(track.in_scope===false || track.mbid!==target)
        work.manual.set(key,target);
      else work.manual.delete(key);
    }
    renderMusic();
  }
  function selectedSummary(){
    const edition=activeEdition(),rows=ui.detail?.tracks||[];
    if(!edition?.complete)return {selected:[],moves:[],newTracks:[],unchanged:[],
      identityChanges:[],hiddenOrange:0};
    const selected=rows.flatMap(track=>{
      const target=selectedRecordingFor(track,edition);
      return target&&edition.tracks.some(song=>song.recording_id===target)
        ? [{...track,target_recording_id:target}] : [];
    });
    const moves=selected.filter(t=>t.current_release_id&&t.current_release_id!==edition.id);
    const newTracks=selected.filter(t=>!t.current_release_id);
    const identityChanges=selected.filter(t=>t.mbid!==t.target_recording_id);
    const unchanged=selected.filter(t=>t.current_release_id===edition.id&&
      t.mbid===t.target_recording_id);
    return {selected,moves,newTracks,unchanged,identityChanges,
      hiddenOrange:$("albumOnlyGreen").checked?moves.length:0};
  }
  function selectAll(){
    const edition=activeEdition(),detail=ui.detail;
    if(!edition?.complete||!detail)return;
    const work=clusterWork();
    const chosen=new Map(),used=new Set();
    for(const song of edition.tracks){
      const exact=detail.tracks.filter(t=>t.mbid===song.recording_id);
      const alternatives=(song.possible_local_matches||[])
        .map(candidate=>detail.tracks.find(t=>t.id===candidate.id)).filter(Boolean);
      const best=[...exact,...alternatives].find(t=>
        canChoose(t)&&!work.resolved.has(t.id)&&!used.has(t.id));
      if(best){
        chosen.set(best.id,song.recording_id);
        used.add(best.id);
      }
    }
    for(const track of detail.tracks){
      const key=choiceKey(edition,track);
      work.manual.delete(key);
      work.unchecked.add(key);
    }
    for(const track of detail.tracks){
      if(!chosen.has(track.id))continue;
      const target=chosen.get(track.id),key=choiceKey(edition,track);
      if(track.mbid===target&&track.in_scope!==false)work.unchecked.delete(key);
      else work.manual.set(key,target);
    }
    renderMusic();
    notice(chosen.size+" available local tracks selected for this album edition. "+
      "Locked and missing recordings were skipped.");
  }
  function renderRemaining(){
    if(!ui.detail){$("albumRemainingStatus").textContent="Select a cluster.";return;}
    const left=remaining(),counts=selectedSummary();
    $("albumRemainingStatus").textContent=left.length+" local songs still open in this cluster · "+
      counts.selected.length+" checked for this edition"+
      (counts.hiddenOrange?" (including "+counts.hiddenOrange+" hidden orange moves)":"")+".";
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
      if(cluster.id===ui.focus)list.scrollTop=Math.max(0,index*65-100);
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
      clearTimeout(ui.pollTimer);ui.pollTimer=null;
      clearTimeout(ui.familyTimer);ui.familyTimer=null;
      if(!$("albumValidateDialog").open)$("albumValidateDialog").close?.();
      notice(response.total_tracks+" recordings in "+response.total_clusters+
        " release-group workspace(s). Assigned songs remain visible; manually selecting them is always possible.");
      const work=ui.detail&&clusterWork();
      if(work?.selected && !releases().some(a=>a.id===work.selected))work.selected=null;
      renderAll();
      if(work?.selected)void loadEdition(work.selected);
      if(ui.detail?.cache_progress?.discovering_editions) scheduleFamilyRefresh();
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
        ? release.matched_count+"/"+detail.tracks.length+" exact MBID matches"
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
    // Do not allow a click on the previous scope while its replacement is loading.
    if(ui.fetching||!releases().some(a=>a.id===id))return;
    const work=clusterWork();
    work.selected=id;work.edition=null;
    clearTimeout(ui.pollTimer);ui.pollTimer=null;
    renderAlbums();renderMusic();
    void loadEdition(id);
  }
  async function loadEdition(id){
    if(!ui.detail||!id)return;
    const user=ui.user,serial=ui.serial,focus=ui.focus,work=clusterWork(focus);
    if(work.loadingId===id&&work.loadingSerial===serial)return;
    work.loadingId=id;work.loadingSerial=serial;
    try{
      const query=new URLSearchParams({user_id:user,anchor_id:focus,release_id:id,
        include_assigned:String($("albumIncludeAssigned").checked)});
      const edition=await api("/api/albums/cluster-edition?"+query);
      if(!active(user,serial)||ui.focus!==focus||work.selected!==id)return;
      work.edition=edition;
      work.extra.set(id,{...(releases().find(a=>a.id===id)||{}),...edition});
      renderAlbums();renderMusic();
      if(!edition.complete)scheduleEditionRefresh(id);
    }catch(error){
      if(active(user,serial)&&ui.focus===focus&&work.selected===id){
        notice("Tracklist is being indexed in the MusicBrainz cache: "+error.message,false,"albumReleaseStatus");
        scheduleEditionRefresh(id);
      }
    }finally{
      if(work.loadingSerial===serial){work.loadingId=null;work.loadingSerial=null;}
    }
  }
  function scheduleEditionRefresh(id){
    clearTimeout(ui.pollTimer);
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    ui.pollTimer=setTimeout(async()=>{
      if(!active(user,serial)||ui.focus!==focus||clusterWork().selected!==id)return;
      // The server enqueues missing tracklists automatically; this is a
      // lightweight cache-only refresh, not another MusicBrainz HTTP call.
      await loadEdition(id);
      if(active(user,serial)&&ui.focus===focus&&activeEdition()?.complete){
        void loadClusters(focus);
      }
    },5000);
  }
  function scheduleFamilyRefresh(){
    clearTimeout(ui.familyTimer);
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    ui.familyTimer=setTimeout(()=>{
      if(!active(user,serial)||ui.focus!==focus)return;
      if($("albumValidateDialog").open){scheduleFamilyRefresh();return;}
      void loadClusters(focus);
    },12000);
  }
  function renderSelected(){
    const work=ui.detail&&clusterWork();
    const album=work&&releases().find(a=>a.id===work.selected);
    $("albumSelectedTitle").replaceChildren();
    const control=$("albumEditionUse");
    control.textContent="Validate Album";
    if(!album){
      control.disabled=true;
      $("albumSelectAll").disabled=true;
      $("albumSelectedTitle").textContent="Choose an album edition to inspect its complete tracklist.";
      return;
    }
    $("albumSelectedTitle").append(element("span","Selected: ","hint"),bilingual(album));
    const edition=activeEdition(),counts=selectedSummary();
    control.disabled=!edition?.complete||!counts.selected.length;
    $("albumSelectAll").disabled=!edition?.complete;
    if(!edition?.complete){
      $("albumSelectedTitle").append(element("small",
        "Complete tracklist is being fetched automatically…","hint"));
    }else{
      $("albumSelectedTitle").append(element("small",
        counts.selected.length+" selected · "+counts.newTracks.length+" new · "+
        counts.moves.length+" from other albums · "+
        counts.identityChanges.length+" recording IDs to update"+
        (counts.hiddenOrange?" ("+counts.hiddenOrange+" hidden by filter)":""),
        "album-validation-count"));
    }
  }
  function renderMusic(){
    const body=$("albumMusicList");body.replaceChildren();
    const detail=ui.detail;
    $("albumMusicHeading").textContent="Tracks on this edition";
    if(!detail){
      $("albumSelectAll").disabled=true;
      $("albumMusicContext").textContent="Choose an album family to review.";
      renderRemaining();return;
    }
    const work=clusterWork(),edition=activeEdition();
    if(!work.selected){
      $("albumMusicContext").textContent="Select an edition to inspect official track membership. Unassigned songs can still be marked No Album Found after review.";
      for(const track of detail.tracks){
        const kind=track.locked?"gray":track.current_release_id?"purple":"blue";
        if($("albumOnlyGreen").checked)continue;
        const card=element("article",null,"album-track-comparison album-music-"+kind);
        const label=element("div",null,"album-track-decision");
        const check=element("input");check.type="checkbox";check.disabled=true;check.checked=false;
        check.setAttribute("aria-label","Not eligible until an edition is chosen");
        const bodyText=element("div",null,"album-track-decision-label");
        bodyText.append(bilingual(track));
        bodyText.append(element("small",track.locked?"Album locked":
          track.current_release_id?"Assigned to another release":"Local song without an album","hint"));
        label.append(check,bodyText);card.append(label);
        if(kind==="blue"&&track.mbid&&!track.not_found)
          card.append(button("No Album Found",()=>markNotFound(track),"secondary album-not-found-action"));
        body.append(card);
      }
      if(!body.childNodes.length)body.append(element("p","Select an edition to display its green songs.","hint"));
      renderRemaining();return;
    }
    if(!edition?.complete){
      $("albumMusicContext").textContent="The complete release tracklist is being prefetched. Membership is unknown until cached; no assignments are available yet.";
      body.append(element("p","Indexing this edition in the background. This panel updates automatically when its MusicBrainz tracklist arrives.","hint"));
      renderRemaining();return;
    }
    const localById=new Map(detail.tracks.map(t=>[t.id,t]));
    const seen=new Set(edition.tracks.map(t=>t.recording_id));
    let officialVisible=0,clusterVisible=0,hidden=0;
    body.append(element("div","OFFICIAL EDITION TRACKLIST ("+edition.tracks.length+")","album-track-section-label"));
    const addCard=(nameData,track,kind,summary,available,selected,recordingKey,targetRecordingId=null)=>{
      if($("albumOnlyGreen").checked&&kind!=="green"){hidden++;return;}
      const card=element("article",null,"album-track-comparison album-music-"+kind);
      const line=element("div",null,"album-track-decision");
      const check=element("input");
      check.type="checkbox";check.checked=Boolean(selected);check.disabled=!available;
      check.setAttribute("aria-label","Include "+(track?.display_title||nameData.title||nameData.original_title||"recording")+
        " for this album");
      if(track)check.dataset.trackId=track.id;
      if(available&&track&&targetRecordingId){
        check.addEventListener("change",()=>{
          setSelection(track,edition,targetRecordingId,check.checked);
        });
      }
      const content=element("div",null,"album-track-decision-label");
      content.append(bilingual(nameData));
      if(nameData.medium&&nameData.position)
        content.append(element("small","Disc "+nameData.medium+" · Track "+nameData.position,"hint"));
      const info=element("div",null,"album-music-info");
      info.append(element("small",summary));
      if(kind==="candidate"&&track)
        info.append(element("small","Local: "+track.display_title+
          " · checking this changes its recording ID to the official edition position."));
      if(track?.current_release_id)
        info.append(element("small","Current release: "+track.current_release_id.slice(0,8)+"…"));
      content.append(info);
      if(recordingKey==="cluster"&&track&&canChoose(track)){
        const row=element("div",null,"album-track-manual-target");
        const picker=element("select");
        const none=element("option","Choose official track position…");
        none.value="";picker.append(none);
        for(const song of edition.tracks){
          const option=element("option","Disc "+song.medium+" · Track "+song.position+" · "+(song.english_title||song.title));
          option.value=song.recording_id;
          picker.append(option);
        }
        picker.value=targetRecordingId||"";
        picker.setAttribute("aria-label","Official album position for "+track.display_title);
        picker.addEventListener("change",()=>{
          if(picker.value)setSelection(track,edition,picker.value,true);
          else setSelection(track,edition,targetRecordingId,false);
        });
        row.append(picker);
        content.append(row);
      }
      line.append(check,content);card.append(line);
      if(kind==="blue"&&track?.mbid&&track.in_scope!==false&&!track.not_found)
        card.append(button("No Album Found",()=>markNotFound(track),"secondary album-not-found-action"));
      if(track?.not_found)card.append(button("Search again",async()=>{
        const user=ui.user;
        await api("/api/albums/not-found/clear",{user_id:user,track_id:track.id});
        if(ui.user===user){track.not_found=false;work.resolved.delete(track.id);renderMusic();}
      }));
      body.append(card);
      if(recordingKey==="official")officialVisible++;else clusterVisible++;
    };
    const suggestions=new Set();
    for(const song of edition.tracks){
      const scoped=detail.tracks.filter(t=>t.mbid===song.recording_id);
      const scopedIds=new Set(scoped.map(t=>t.id));
      const extra=(song.local_copies||[]).filter(t=>!scopedIds.has(t.id))
        .map(t=>({...t,mbid:song.recording_id,out_of_scope:true}));
      const local=[...scoped,...extra];
      if(!local.length){
        const candidates=(song.possible_local_matches||[])
          .map(t=>localById.get(t.id)).filter(Boolean);
        if(!candidates.length){
          addCard(song,null,"red","No local recording for this official track",
            false,false,"official");
        }else{
          for(const track of candidates){
            suggestions.add(track.id);
            const selectable=canChoose(track)&&!work.resolved.has(track.id);
            addCard(song,track,track.locked?"gray":"candidate",
              track.locked?"Album locked":"Possible local match · different recording ID",
              selectable,selectedRecordingFor(track,edition)===song.recording_id,
              "official",song.recording_id);
          }
        }
        continue;
      }
      for(const track of local){
        const kind=track.locked?"gray":
          track.current_release_id&&track.current_release_id!==edition.id?"orange":"green";
        const message=track.locked?"Album locked · cannot change":
          track.out_of_scope?"Downloaded outside this review family · read-only":
          kind==="orange"?"Local · currently assigned to another album":
          track.current_release_id===edition.id?"Local · already correctly assigned":
          "Local · no album assigned";
        const editable=canChoose(track)&&!work.resolved.has(track.id);
        addCard(song,track,kind,message,editable,
          selectedRecordingFor(track,edition)===song.recording_id,
          "official",song.recording_id);
      }
    }
    const other=detail.tracks.filter(t=>!seen.has(t.mbid) &&
      !suggestions.has(t.id) && t.current_release_id!==edition.id);
    if(other.length&&!$("albumOnlyGreen").checked)
      body.append(element("div","OTHER LOCAL MUSIC IN THIS FAMILY ("+other.length+")",
        "album-track-section-label album-cluster-only-label"));
    for(const track of other){
      const kind=track.locked?"gray":track.current_release_id?"purple":"blue";
      const target=selectedRecordingFor(track,edition);
      const msg=track.locked?"Album locked · not on this edition":
        kind==="purple"?"Local · assigned elsewhere · choose an official position":
        track.not_found?"Previously marked No Album Found · choose an official position":
        "Local · choose an official position to include it";
      addCard(track,track,kind,msg,canChoose(track)&&!!target,!!target,
        "cluster",target);
    }
    if(!officialVisible&&!clusterVisible)
      body.append(element("p",hidden?"No green tracks to display. Turn off Green tracks only to see the other states.":"No tracks to display.","hint"));
    $("albumMusicContext").textContent=edition.tracks.length+" official tracks · "+
      detail.tracks.length+" local family recordings · "+other.length+
      " local recordings without an exact match on this edition · "+
      (edition.possible_match_count||0)+" local candidates with different recording IDs.";
    renderSelected();renderRemaining();
  }
  async function markNotFound(track){
    if(!track?.mbid||track.current_release_id||track.locked)return;
    if(!confirm("Mark this one local recording No Album Found? This does not write a fake album tag."))return;
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    try{
      const preview=await api("/api/albums/plan-preview",
        {user_id:user,decisions:[{track_id:track.id,action:"not_found"}]});
      if(!active(user,serial)||ui.focus!==focus)return;
      if(preview.can_mark){
        await api("/api/albums/plan-apply",{user_id:user,preview_token:preview.preview_token});
        if(!active(user,serial)||ui.focus!==focus)return;
        track.not_found=true;clusterWork().resolved.add(track.id);
        renderMusic();notice("Marked one recording No Album Found. It stays available for later review.");
      }else notice("This recording cannot currently be marked No Album Found.",true);
    }catch(error){if(active(user,serial))notice(error.message,true);}
  }
  function renderAll(){
    renderNavigation();renderAlbums();renderMusic();renderPreference();
  }
  async function validateEdition(){
    const detail=ui.detail,edition=activeEdition();
    if(!detail||!edition?.complete)return;
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    const counts=selectedSummary();
    if(!counts.selected.length){notice("No eligible checked local recordings in this edition.");return;}
    const decisions=counts.selected.map(track=>({
      track_id:track.id,action:"release",release_id:edition.id,
      target_recording_id:track.target_recording_id
    }));
    try{
      $("albumEditionUse").disabled=true;
      const preview=await api("/api/albums/plan-preview",{user_id:user,decisions});
      if(!active(user,serial)||ui.focus!==focus||clusterWork().selected!==edition.id)return;
      ui.preview={...preview,selectedIds:counts.selected.map(t=>t.id),edition_id:edition.id};
      if(!preview.can_apply){
        for(const row of preview.items)
          if(row.status==="UNCHANGED")clusterWork().resolved.add(row.track_id);
        ui.preview=null;
        notice(counts.unchanged.length+" recordings already use this edition. "+
          "No audio changes were needed; incompatible or locked records remain unchanged.");
        renderMusic();return;
      }
      const incompatible=preview.items.filter(t=>!["COMPATIBLE","UNCHANGED"].includes(t.status));
      $("albumValidateTitle").textContent="Validate Album — "+edition.name;
      $("albumValidateSummary").textContent=
        counts.newTracks.length+" unassigned · "+counts.moves.length+" moved from other albums · "+
        counts.unchanged.length+" already correct · "+
        counts.identityChanges.length+" recording ID changes · "+incompatible.length+" blocked"+
        (counts.hiddenOrange?" · "+counts.hiddenOrange+" orange moves currently hidden by the green-only filter":"")+".";
      const impact=$("albumValidateImpact");impact.replaceChildren();
      for(const row of preview.impact||[]){
        impact.append(element("span",
          (releases().find(a=>a.id===row.release_id)?.name||row.release_id.slice(0,8))+
          ": "+row.before+" → "+row.after+" local tracks","album-impact-item"));
      }
      const details=$("albumValidateDetails");details.replaceChildren();
      const list=element("div",null,"album-validate-list");
      for(const row of preview.items){
        const entry=element("div",null,"album-validate-row");
        entry.append(element("strong",row.title||row.track_id));
        entry.append(element("small",
          (row.old_release_id?"From "+row.old_release_id.slice(0,8)+"…":"Previously unassigned")+
          " → "+(row.target_album||edition.name)+" · "+row.status,"hint"));
        const destination=edition.tracks.find(t=>t.recording_id===row.target_recording_id);
        if(destination)entry.append(element("small",
          "Target: Disc "+destination.medium+" · Track "+destination.position+
          " · "+(destination.english_title||destination.title),"hint"));
        if(row.recording_id!==row.target_recording_id){
          entry.append(element("small","Recording MBID: "+
            (row.recording_id?row.recording_id.slice(0,8)+"…":"unidentified")+
            " → "+row.target_recording_id.slice(0,8)+"… · reversible","hint"));
        }
        list.append(entry);
      }
      details.append(list);
      $("albumValidateStatus").textContent=preview.can_apply+
        " eligible updates. Song title and artist are preserved; selected different-MBID copies "+
        "will adopt the chosen official recording ID. Undo restores prior audio tags and ID.";
      $("albumValidateConfirm").disabled=!preview.can_apply;
      $("albumValidateDialog").showModal();
    }catch(error){if(active(user,serial))notice(error.message,true);}
    finally{renderSelected();}
  }
  async function confirmEdition(){
    const preview=ui.preview;
    if(!preview)return;
    const user=ui.user,serial=ui.serial,focus=ui.focus;
    $("albumValidateConfirm").disabled=true;
    try{
      const result=await api("/api/albums/plan-apply",{
        user_id:user,preview_token:preview.preview_token});
      if(!active(user,serial)||ui.focus!==focus)return;
      for(const row of preview.items){
        if(row.status==="COMPATIBLE"||row.status==="UNCHANGED")
          clusterWork().resolved.add(row.track_id);
      }
      ui.preview=null;
      $("albumValidateDialog").close();
      notice(result.queued+" reversible album changes queued. Unmatched cluster music remains for another edition.");
      renderMusic();renderSelected();
      void loadHistory();
    }catch(error){
      if(active(user,serial))notice(error.message,true,"albumValidateStatus");
      $("albumValidateConfirm").disabled=false;
    }
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
      clusterWork().extra.set(rid,edition);
      selectEdition(rid);
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
        if(change.new_recording_id&&change.new_recording_id!==change.old_recording_id)
          row.append(element("small","Recording ID: "+
            (change.old_recording_id?change.old_recording_id.slice(0,8)+"…":"unidentified")+
            " → "+change.new_recording_id.slice(0,8)+"…","hint"));
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
    }catch(error){if(active(user,serial))notice(error.message,true,"albumClusterStatus");}
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
  function leave(){
    ui.serial++;clearTimeout(ui.searchTimer);clearTimeout(ui.pollTimer);
    clearTimeout(ui.familyTimer);ui.familyTimer=null;
    ui.pollTimer=null;$("albumValidateDialog").close?.();
  }
  function reset(){
    ui.serial++;clearTimeout(ui.searchTimer);clearTimeout(ui.pollTimer);
    clearTimeout(ui.familyTimer);ui.familyTimer=null;
    ui.searchTimer=null;ui.pollTimer=null;
    ui.user=null;ui.clusters=[];ui.detail=null;ui.focus=null;ui.preview=null;
    ui.work.clear();ui.sources=[];ui.preferences=[];ui.fetching=false;
    $("albumIncludeAssigned").checked=false;$("albumOnlyGreen").checked=false;
    $("albumClusterSearch").value="";$("albumReleaseSearchInput").value="";
    $("albumReleaseSearchResults").replaceChildren();
    $("albumValidateDetails").replaceChildren();$("albumValidateDialog").close?.();
    renderAll();
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
      }catch(error){
        message.textContent=error.message;
        if(/tracklist|release.*cached/i.test(error.message||"")){
          preview.append(button("Sync full MusicBrainz tracklist",async()=>{
            await apiRequest("/api/catalogue/sync",{
              user_id:user,kind:"release",id:release_id,sections:["lookup"]});
            message.textContent="Tracklist lookup queued. Check this edition again after synchronization.";
          }));
        }
      }
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
    const hasChoices=[...ui.work.values()].some(w=>w.unchecked.size||w.manual.size);
    if(hasChoices&&!confirm("Changing preselection will reset your manual song choices. Continue?")){
      $("albumIncludeAssigned").checked=!$("albumIncludeAssigned").checked;
      return;
    }
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
  $("albumOnlyGreen").addEventListener("change",()=>{
    renderMusic();renderSelected();
  });
  $("albumSelectAll").addEventListener("click",selectAll);
  $("albumEditionUse").addEventListener("click",()=>validateEdition());
  $("albumValidateConfirm").addEventListener("click",()=>confirmEdition());
  $("albumValidateCancel").addEventListener("click",()=>{$("albumValidateDialog").close();ui.preview=null;});
  $("albumReleaseSearchForm").addEventListener("submit",event=>{event.preventDefault();void findMore(false);});
  $("albumReleaseSearchOnline").addEventListener("click",()=>findMore(true));
  $("albumHistoryRefresh").addEventListener("click",()=>loadHistory());
  $("albumPreferenceSource").addEventListener("change",renderPreference);
  $("albumPreferenceSave").addEventListener("click",()=>savePreference(false));
  $("albumPreferenceClear").addEventListener("click",()=>savePreference(true));
  return {enter,leave,reset,mountTrack};
})();
