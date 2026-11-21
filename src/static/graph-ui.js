/* MusicBrainz force graph, Obsidian-style controls and per-library browser snapshots. */
"use strict";
window.GraphUI = (() => {
  const get = id => document.getElementById(id);
  const stage = get("graphCanvas");
  const MAX_NODES = 10000;
  const SNAPSHOT_VERSION = 2;
  const SETTINGS_KEY = "yt-navidrome-graph-settings-v2:";
  const SNAPSHOT_KEY = "yt-navidrome-graph-snapshot-v2:";
  const colors = {
    artist:"#8b5cf6", recording:"#10b981", release:"#f59e0b",
    "release-group":"#ef4444", work:"#38bdf8"
  };
  const sliders = [
    {id:"graphCenterForce", output:"graphCenterValue", key:"center", min:0, max:1},
    {id:"graphRepelForce", output:"graphRepelValue", key:"repel", min:0, max:20},
    {id:"graphLinkForce", output:"graphLinkValue", key:"link", min:0, max:1},
    {id:"graphLinkDistance", output:"graphDistanceValue", key:"distance", min:0, max:500}
  ];
  const ui = {
    user:null, serial:0, drawSerial:0, graph:null, renderer:null,
    selected:null, hovered:null, active:false, dragging:false,
    positions:new Map(), observer:null, saveTimer:null
  };
  let databasePromise = null;
  const kinds = () => [...document.querySelectorAll(".graph-type:checked")].map(input => input.value);
  const graphRoute = (kind,entityId) => ({
    user:state.user, tab:"graph",kind,entityId,
    view:get("graphView").value, focus:get("graphFocus").value.trim(),
    q:get("graphQuery").value.trim(), kinds:kinds().join(","),
    limit:Number(get("graphLimit").value)||180
  });
  const status = (message, error=false) => {
    get("graphStatus").textContent = message;
    get("graphStatus").className = error ? "error-text" : "hint";
  };
  const dimensions = () => ({
    width:Math.max(250, stage.clientWidth || 900),
    height:Math.max(320, stage.clientHeight || 500)
  });
  const end = value => typeof value === "object" ? value.id : value;
  const radius = node => node.kind === "artist" ? 10 : node.kind === "recording" ? 9 : 8;
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const storageKey = (prefix,user) => prefix + encodeURIComponent(user);
  function readLocal(key) {
    try { return JSON.parse(localStorage.getItem(key) || "null"); }
    catch (_) { return null; }
  }
  function writeLocal(key,value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; }
    catch (_) { return false; }
  }
  function openDatabase() {
    if (databasePromise) return databasePromise;
    if (typeof indexedDB === "undefined") return Promise.resolve(null);
    databasePromise = new Promise(resolve => {
      try {
        const request = indexedDB.open("yt-navidrome-music-graph", 1);
        request.onupgradeneeded = () => {
          if (!request.result.objectStoreNames.contains("snapshots"))
            request.result.createObjectStore("snapshots", {keyPath:"user"});
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => resolve(null);
        request.onblocked = () => resolve(null);
      } catch (_) { resolve(null); }
    });
    return databasePromise;
  }
  async function readSnapshot(user) {
    const local = readLocal(storageKey(SNAPSHOT_KEY,user));
    const db = await openDatabase();
    if (!db) return validSnapshot(local,user) ? local : null;
    let remote = null;
    try {
      remote = await new Promise(resolve => {
        const request = db.transaction("snapshots","readonly").objectStore("snapshots").get(user);
        request.onsuccess = () => resolve(request.result || null);
        request.onerror = () => resolve(null);
      });
    } catch (_) { /* Private browsing and revoked storage do not block the graph. */ }
    const values = [local, remote].filter(s => validSnapshot(s,user));
    return values.sort((a,b) => b.savedAt - a.savedAt)[0] || null;
  }
  function validSnapshot(snapshot,user) {
    return !!snapshot && snapshot.version === SNAPSHOT_VERSION && snapshot.user === user &&
      snapshot.graph && Array.isArray(snapshot.graph.nodes) && Array.isArray(snapshot.graph.edges) &&
      snapshot.graph.nodes.length <= MAX_NODES && snapshot.graph.edges.length <= 30000;
  }
  function writeSnapshot(snapshot) {
    const serialized = JSON.stringify(snapshot);
    // A small synchronous copy also survives an immediate page refresh. Use
    // IndexedDB for larger snapshots so localStorage quotas do not cap graphs.
    if (serialized.length <= 1500000)
      writeLocal(storageKey(SNAPSHOT_KEY,snapshot.user), snapshot);
    else {
      try { localStorage.removeItem(storageKey(SNAPSHOT_KEY,snapshot.user)); } catch (_) {}
    }
    openDatabase().then(db => {
      if (!db) return;
      try { db.transaction("snapshots","readwrite").objectStore("snapshots").put(snapshot); }
      catch (_) { /* Graph navigation remains usable with browser storage off. */ }
    });
  }
  function forceValues() {
    return Object.fromEntries(sliders.map(slider => [slider.key, Number(get(slider.id).value)]));
  }
  function refreshForceLabels() {
    for (const slider of sliders) {
      const value = Number(get(slider.id).value);
      get(slider.output).textContent = slider.key === "distance" ? String(value)
        : slider.key === "repel" ? value.toFixed(1) : value.toFixed(2);
    }
  }
  function controls() {
    return {
      view:get("graphView").value, query:get("graphQuery").value,
      focus:get("graphFocus").value, kinds:kinds(),
      limit:Number(get("graphLimit").value), layout:get("graphLayout").value,
      forces:forceValues()
    };
  }
  function restoreControls(value) {
    if (!value || typeof value !== "object") return;
    if (["downloaded","catalogue"].includes(value.view)) get("graphView").value = value.view;
    if (typeof value.query === "string") get("graphQuery").value = value.query.slice(0,100);
    if (typeof value.focus === "string") get("graphFocus").value = value.focus.slice(0,100);
    if (["free","layers"].includes(value.layout)) get("graphLayout").value = value.layout;
    if (Array.isArray(value.kinds)) {
      const enabled = new Set(value.kinds);
      for (const input of document.querySelectorAll(".graph-type")) input.checked = enabled.has(input.value);
    }
    if (Number.isInteger(value.limit) && value.limit >= 1 && value.limit <= MAX_NODES)
      get("graphLimit").value = String(value.limit);
    for (const slider of sliders) {
      const number = value.forces?.[slider.key];
      if (typeof number === "number" && Number.isFinite(number))
        get(slider.id).value = String(Math.max(slider.min,Math.min(slider.max,number)));
    }
    refreshForceLabels();
  }
  function saveControls(user=state.user) {
    if (user) writeLocal(storageKey(SETTINGS_KEY,user),controls());
  }
  function snapshot() {
    if (!ui.graph || !ui.user) return null;
    const nodes = ui.renderer?.graphData().nodes || [];
    const positions = nodes.filter(n => Number.isFinite(n.x) && Number.isFinite(n.y))
      .map(n => [n.id,n.x,n.y]);
    const center = ui.renderer?.centerAt();
    const zoom = ui.renderer?.zoom();
    return {
      version:SNAPSHOT_VERSION, user:ui.user, savedAt:Date.now(),
      controls:controls(), graph:ui.graph, positions,
      camera:center && Number.isFinite(center.x) && Number.isFinite(center.y) && Number.isFinite(zoom)
        ? {x:center.x,y:center.y,zoom} : null,
      selectedId:ui.selected?.id || null
    };
  }
  function saveNow() {
    if (ui.saveTimer) clearTimeout(ui.saveTimer);
    ui.saveTimer = null;
    const saved = snapshot();
    if (saved) writeSnapshot(saved);
  }
  function saveSoon() {
    if (ui.saveTimer) clearTimeout(ui.saveTimer);
    ui.saveTimer = setTimeout(saveNow, 350);
  }
  function totalCount(result) {
    const count = Number(result.total_nodes);
    return Number.isSafeInteger(count) && count >= 0 ? count : (result.nodes || []).length;
  }
  function updateTotal(result) {
    const total = totalCount(result);
    const available = Number(result.max_selectable_nodes);
    const maximum = Number.isSafeInteger(available) && available >= 0
      ? Math.min(total,available,MAX_NODES) : Math.min(total,MAX_NODES);
    const input = get("graphLimit");
    input.max = String(maximum);
    input.disabled = maximum === 0;
    if (maximum === 0) input.value = "0";
    else clampLimit(maximum);
    const suffix = total > maximum ? " (display safety limit: " + maximum.toLocaleString() + ")" : "";
    get("graphTotal").textContent = total.toLocaleString() + " available in this neighborhood" + suffix;
    return total;
  }
  function clampLimit(maximum) {
    const input = get("graphLimit");
    const bound = Number.isFinite(maximum) ? maximum : Number(input.max) || MAX_NODES;
    if (bound < 1) { input.value = "0"; return 0; }
    const requested = Number(input.value);
    const value = Number.isFinite(requested) && requested >= 1 ? Math.floor(requested) : 180;
    const clamped = Math.max(1,Math.min(value,bound,MAX_NODES));
    input.value = String(clamped);
    return clamped;
  }

  // Layer depth depends on topology, not entity type; one type can span layers.
  function assignLayers(nodes,links) {
    const lookup = new Map(nodes.map(n => [n.id,n]));
    const adjacent = new Map(nodes.map(n => [n.id,new Set()]));
    for (const edge of links) {
      if (!adjacent.has(edge.source) || !adjacent.has(edge.target)) continue;
      adjacent.get(edge.source).add(edge.target);
      adjacent.get(edge.target).add(edge.source);
    }
    const visited = new Set();
    let offset = 0;
    for (const seed of nodes) {
      if (visited.has(seed.id)) continue;
      const component = [], queue = [seed.id];
      visited.add(seed.id);
      for (let i=0;i<queue.length;i++) {
        const id = queue[i];component.push(id);
        for (const neighbor of adjacent.get(id)) {
          if (!visited.has(neighbor)) { visited.add(neighbor); queue.push(neighbor); }
        }
      }
      const root = component.sort((a,b) =>
        Number(lookup.get(b).kind === "artist") - Number(lookup.get(a).kind === "artist") ||
        adjacent.get(b).size-adjacent.get(a).size || a.localeCompare(b))[0];
      const depth = new Map([[root,0]]), bfs = [root];
      for (let i=0;i<bfs.length;i++) {
        for (const neighbor of adjacent.get(bfs[i])) {
          if (!depth.has(neighbor)) { depth.set(neighbor,depth.get(bfs[i])+1); bfs.push(neighbor); }
        }
      }
      const layers = new Map();
      for (const id of component) {
        const d=depth.get(id);
        if (!layers.has(d)) layers.set(d,[]);
        layers.get(d).push(lookup.get(id));
      }
      let maxRows = 1;
      for (const [d,items] of layers) {
        items.sort((a,b) => adjacent.get(b.id).size-adjacent.get(a.id).size || a.label.localeCompare(b.label));
        maxRows = Math.max(maxRows,items.length);
        items.forEach((n,i) => { n.targetX=d*175;n.targetY=offset+(i-(items.length-1)/2)*61; });
      }
      offset += Math.max(260,maxRows*61+125);
    }
    for (const node of nodes) node.targetY -= (offset-260)/2;
  }
  function layersForce() {
    let nodes = [];
    function force(alpha) {
      for (const node of nodes) {
        node.vx += (node.targetX-node.x)*0.095*alpha;
        node.vy += (node.targetY-node.y)*0.035*alpha;
      }
    }
    force.initialize = value => { nodes=value; };
    return force;
  }
  // D3 forceCenter changes the center of mass but does not pull individual
  // nodes inward. An actual x/y attraction matches Obsidian's Center force.
  function centerForce(setting) {
    let nodes = [];
    const strength = 0.18*setting*setting;
    function force(alpha) {
      for (const node of nodes) {
        node.vx -= node.x*strength*alpha;
        node.vy -= node.y*strength*alpha;
      }
    }
    force.initialize = value => { nodes=value; };
    return force;
  }
  function configure(reheat=true) {
    if (!ui.renderer) return;
    const values = forceValues();
    const layered = get("graphLayout").value === "layers";
    ui.renderer.d3Force("center",null);
    ui.renderer.d3Force("centerPull",centerForce(values.center));
    ui.renderer.d3Force("charge").strength(-3*values.repel*values.repel).distanceMax(850);
    ui.renderer.d3Force("link").distance(values.distance).strength(values.link);
    ui.renderer.d3Force("layer",layered ? layersForce() : null);
    if (reheat) {
      ui.renderer.cooldownTicks(230);
      ui.renderer.d3ReheatSimulation();
    }
  }
  function focused(edge) {
    return !!ui.selected && (end(edge.source) === ui.selected.id || end(edge.target) === ui.selected.id);
  }
  function paint(node,ctx,scale) {
    const active = ui.selected?.id === node.id || ui.hovered === node.id;
    const r = radius(node);
    ctx.beginPath(); ctx.arc(node.x,node.y,r,0,Math.PI*2);
    ctx.fillStyle = colors[node.kind] || "#a6b3c6"; ctx.fill();
    ctx.lineWidth = active ? 2.8 : node.owned ? 2.5 : 0.8;
    ctx.strokeStyle = active ? "#fff4a3" : node.owned ? "#f9d66e" : "#e2e8f0";
    ctx.stroke();
    if (!active && scale < 1.05) return;
    const label = node.label.length > 38 ? node.label.slice(0,35)+"…" : node.label;
    const font = 11 / Math.max(0.65,scale);
    ctx.font = font+"px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.lineWidth = 3/Math.max(0.65,scale);ctx.strokeStyle="#101824";
    ctx.strokeText(label,node.x,node.y+r+4);
    ctx.fillStyle="#f1f5f9";ctx.fillText(label,node.x,node.y+r+4);
  }
  function repaint() {
    ui.renderer?.nodeCanvasObject(paint)
      .linkColor(edge => ui.selected ?
        (focused(edge) ? "rgba(250,204,21,.82)" : "rgba(135,151,169,.14)") : "rgba(135,151,169,.50)")
      .linkWidth(edge => focused(edge) ? 2.2 : 1);
  }
  function resize() {
    if (!ui.renderer || stage.closest?.("[hidden]")) return;
    const d = dimensions();
    ui.renderer.width(d.width).height(d.height);
  }
  function ensureRenderer() {
    if (ui.renderer) return ui.renderer;
    if (typeof window.ForceGraph !== "function")
      throw new Error("Local force-graph renderer unavailable. Reload the page.");
    const d = dimensions();
    ui.renderer = window.ForceGraph()(stage)
      .width(d.width).height(d.height).backgroundColor("rgba(0,0,0,0)")
      .nodeRelSize(1).nodeVal(node => radius(node)**2)
      .nodeLabel(node => escape(node.label)+" ("+escape(node.kind)+")")
      .linkLabel(edge => escape(edge.relation))
      .nodePointerAreaPaint((node,color,ctx) => {
        ctx.fillStyle=color;ctx.beginPath();ctx.arc(node.x,node.y,radius(node)+5,0,Math.PI*2);ctx.fill();
      })
      .nodeCanvasObjectMode("replace")
      .minZoom(0.12).maxZoom(9).enableZoomInteraction(true).enableNodeDrag(true)
      .cooldownTicks(230).cooldownTime(9000).autoPauseRedraw(true)
      .onNodeClick(select).onNodeHover(node => {ui.hovered=node?.id||null;repaint();})
      .onNodeDrag(() => { if (!ui.dragging) {ui.dragging=true;ui.renderer.cooldownTicks(230);ui.renderer.d3ReheatSimulation();} })
      .onNodeDragEnd(node => {
        ui.dragging=false;node.fx=null;node.fy=null;ui.renderer.d3ReheatSimulation();saveSoon();
      })
      .onZoomEnd(saveSoon)
      .onEngineStop(saveSoon);
    repaint();
    if (typeof ResizeObserver !== "undefined") {
      ui.observer = new ResizeObserver(resize);ui.observer.observe(stage);
    }
    return ui.renderer;
  }
  function populatePicker(nodes) {
    const picker=get("graphNodeSelect");
    picker.replaceChildren();
    const first=document.createElement("option");
    first.value="";first.textContent="Jump to a node…";picker.append(first);
    for (const node of [...nodes].sort((a,b)=>a.label.localeCompare(b.label))) {
      const option=document.createElement("option");option.value=node.id;
      option.textContent=node.label+" ("+node.kind+")";picker.append(option);
    }
    picker.value="";
  }
  function draw(result, saved=null) {
    for (const node of ui.renderer?.graphData().nodes || []) {
      if (Number.isFinite(node.x) && Number.isFinite(node.y))
        ui.positions.set(node.id,{x:node.x,y:node.y});
    }
    if (saved?.positions) for (const [id,x,y] of saved.positions) {
      if (typeof id === "string" && Number.isFinite(x) && Number.isFinite(y))
        ui.positions.set(id,{x,y});
    }
    const nodes=(result.nodes||[]).map(n=>({...n}));
    const ids=new Set(nodes.map(n=>n.id));
    const links=(result.edges||[]).filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>({...e}));
    populatePicker(nodes);
    get("graphEmpty").hidden=nodes.length>0;
    if (!nodes.length) {ui.renderer?.graphData({nodes:[],links:[]});return;}
    assignLayers(nodes,links);
    nodes.forEach((node,i)=>{
      const pos=ui.positions.get(node.id);
      node.x=pos?.x??node.targetX+(i%5-2)*12;
      node.y=pos?.y??node.targetY+(i%7-3)*9;
    });
    const network=ensureRenderer(),drawId=++ui.drawSerial;
    network.cooldownTicks(saved ? 0 : 230);
    configure(false);
    network.graphData({nodes,links});
    if (saved?.selectedId) {
      const selected=nodes.find(n=>n.id===saved.selectedId);
      if (selected) select(selected);
    }
    requestAnimationFrame(()=>{
      if (ui.renderer !== network || ui.drawSerial !== drawId) return;
      if (saved?.camera && Number.isFinite(saved.camera.x) && Number.isFinite(saved.camera.y) &&
          Number.isFinite(saved.camera.zoom)) {
        network.centerAt(saved.camera.x,saved.camera.y,0);
        network.zoom(Math.max(0.12,Math.min(9,saved.camera.zoom)),0);
      } else network.zoomToFit(250,40);
    });
    if (!ui.active) network.pauseAnimation();
  }
  function select(node, updateUrl=true) {
    ui.selected=node;get("graphNodeSelect").value=node.id;
    if(updateUrl && state.tab==="graph")
      window.IngestorRouter?.write(graphRoute(node.kind,node.id.slice(node.kind.length+1)));
    const box=get("graphDetails");box.replaceChildren();
    const heading=document.createElement("h3");heading.textContent=node.label;
    const info=document.createElement("p");
    info.textContent=node.kind+" · "+node.id.slice(node.kind.length+1)+
      (node.owned?" · Downloaded in this library":" · Public catalogue only");
    box.append(heading,info);
    const inspect=document.createElement("button");
    inspect.type="button";inspect.className="secondary";inspect.textContent="Select in Catalogue";
    inspect.addEventListener("click",()=>{
      setTab("catalogue",true,{noHistory:true});
      window.CatalogueUI?.openEntity(node.kind,node.id.slice(node.kind.length+1))
        .catch(error=>status(error.message,true));
    });
    const focus=document.createElement("button");
    focus.type="button";focus.className="secondary";focus.textContent="Expand neighborhood";
    focus.addEventListener("click",()=>{
      get("graphFocus").value=node.id.slice(node.kind.length+1);
      saveControls();refresh();
    });
    box.append(inspect,focus);repaint();saveSoon();
  }
  function report(result,restored=false) {
    const count=totalCount(result);
    const missing=result.nodes.length && !result.edges.length ?
      " No cached connections yet: open Catalogue, choose Index downloaded recordings, then Refresh progress. Indexing can take time." : "";
    const sampled=get("graphView").value==="downloaded" &&
      (result.owned_recording_count||0)>result.nodes.filter(n=>n.owned).length ?
      " This is a bounded sample, not your complete music library; use search or a MusicBrainz ID to focus." : "";
    status(result.nodes.length+" nodes · "+result.edges.length+" relationships · "+
      (result.owned_recording_count||0)+" owned recordings. "+count+
      " available in this neighborhood."+missing+sampled+
      (result.truncated?" Display limited: change the node count or focus to explore more.":"")+
      (restored?" Restored from this browser; Explore refreshes the catalogue.":""));
  }
  async function refresh(suppressUrl=false) {
    if (!state.user) return;
    const user=state.user,serial=++ui.serial;
    if (!kinds().length) {
      ui.user=user;ui.graph={nodes:[],edges:[],total_nodes:0,max_selectable_nodes:0,
        owned_recording_count:0,truncated:false};
      ui.selected=null;ui.renderer?.graphData({nodes:[],links:[]});
      get("graphEmpty").hidden=false;get("graphDetails").replaceChildren();
      populatePicker([]);updateTotal(ui.graph);status("Enable at least one node type.");
      saveControls(user);saveSoon();
      if(!suppressUrl && state.tab==="graph")
        window.IngestorRouter?.write(graphRoute(),{replace:true});
      return;
    }
    const requestLimit=clampLimit();
    const params=new URLSearchParams({user_id:user,view:get("graphView").value,
      kinds:kinds().join(","),q:get("graphQuery").value.trim(),
      focus_id:get("graphFocus").value.trim(),limit:String(requestLimit)});
    saveControls(user);
    status("Loading graph neighborhood from the local catalogue...");
    try {
      const result=await api("/api/catalogue/graph?"+params);
      if (serial!==ui.serial || user!==state.user) return;
      ui.user=user;ui.graph=result;ui.selected=null;ui.hovered=null;
      if(!suppressUrl && state.tab==="graph")
        window.IngestorRouter?.write(graphRoute(),{replace:true});
      get("graphDetails").replaceChildren();
      updateTotal(result);
      draw(result);
      report(result);
      saveSoon();
    } catch (error) {
      if (serial===ui.serial && user===state.user) status(error.message,true);
    }
  }
  async function enter() {
    if (!state.user) return;
    ui.active=true;
    if (ui.graph && ui.user===state.user) {
      ui.renderer?.resumeAnimation();resize();return;
    }
    const user=state.user,serial=++ui.serial;
    restoreControls(readLocal(storageKey(SETTINGS_KEY,user)));
    const saved=await readSnapshot(user);
    if (serial!==ui.serial || state.user!==user || !ui.active) return;
    if (!saved) return refresh();
    restoreControls(saved.controls);
    ui.user=user;ui.graph=saved.graph;ui.selected=null;ui.hovered=null;
    get("graphDetails").replaceChildren();
    updateTotal(saved.graph);
    draw(saved.graph,saved);
    report(saved.graph,true);
  }
  async function openRoute(route) {
    if (!state.user || state.tab!=="graph") return;
    const hasLocation = route.entityId || route.focus || route.q || route.view || route.kinds || route.limit;
    if(!hasLocation) return enter();
    const user=state.user;
    ui.active=true;
    restoreControls(readLocal(storageKey(SETTINGS_KEY,user)));
    get("graphView").value=route.view||"downloaded";
    get("graphFocus").value=route.entityId||route.focus||"";
    get("graphQuery").value=route.q||"";
    if(route.kinds){
      const visible=new Set(route.kinds.split(","));
      for(const checkbox of document.querySelectorAll(".graph-type"))
        checkbox.checked=visible.has(checkbox.value);
    }
    if(route.limit)get("graphLimit").value=String(route.limit);
    await refresh(true);
    if(route.kind&&route.entityId && ui.graph?.nodes) {
      const key=route.kind+":"+route.entityId;
      const node=ui.renderer?.graphData().nodes.find(entry=>entry.id===key) ||
        ui.graph.nodes.find(entry=>entry.id===key);
      if(node)select(node,false);
      else status("The linked graph node has not been cached in this neighborhood.");
    }
  }
  function prepareSearch() {
    get("graphLimit").disabled=false;
    get("graphLimit").max=String(MAX_NODES);
    if (Number(get("graphLimit").value)<1) get("graphLimit").value="180";
    get("graphTotal").textContent="Available in this neighborhood: calculating…";
    saveControls();return refresh();
  }
  function zoom(factor) {
    if (ui.renderer)ui.renderer.zoom(Math.max(0.12,Math.min(9,ui.renderer.zoom()*factor)),180);
  }
  function fit() {
    if (ui.renderer && ui.graph?.nodes?.length)ui.renderer.zoomToFit(300,40);
  }
  function reset() {
    saveNow();ui.serial++;ui.drawSerial++;ui.user=null;ui.graph=null;
    ui.selected=null;ui.hovered=null;ui.active=false;ui.dragging=false;ui.positions.clear();
    ui.observer?.disconnect();ui.observer=null;ui.renderer?.pauseAnimation();ui.renderer=null;
    stage.replaceChildren();get("graphDetails").replaceChildren();
    get("graphEmpty").hidden=true;populatePicker([]);
    get("graphLimit").max=String(MAX_NODES);get("graphLimit").disabled=false;
    get("graphTotal").textContent="Available in this neighborhood: calculating…";
    status("");
  }
  get("graphSearchForm").addEventListener("submit",event=>{
    event.preventDefault();clampLimit();refresh();
  });
  get("graphView").addEventListener("change",prepareSearch);
  get("graphLimit").addEventListener("change",()=>{
    clampLimit();saveControls();refresh();
  });
  get("graphLayout").addEventListener("change",()=>{
    saveControls();configure();saveSoon();
  });
  for (const checkbox of document.querySelectorAll(".graph-type"))
    checkbox.addEventListener("change",prepareSearch);
  for (const slider of sliders)get(slider.id).addEventListener("input",()=>{
    refreshForceLabels();saveControls();configure();saveSoon();
  });
  refreshForceLabels();
  get("graphZoomReset").addEventListener("click",fit);
  get("graphNodeSelect").addEventListener("change",()=>{
    const id=get("graphNodeSelect").value;
    const node=ui.renderer?.graphData().nodes.find(n=>n.id===id);
    if (!node)return;
    select(node);ui.renderer.centerAt(node.x,node.y,300);
    ui.renderer.zoom(Math.max(1.5,ui.renderer.zoom()),300);
  });
  stage.addEventListener("keydown",event=>{
    if (event.key==="+" || event.key==="=") {event.preventDefault();zoom(1.3);}
    if (event.key==="-") {event.preventDefault();zoom(1/1.3);}
    if (event.key==="0") {event.preventDefault();fit();}
  });
  window.addEventListener("resize",resize);
  window.addEventListener("pagehide",saveNow);
  return {
    enter,openRoute,leave:()=>{saveNow();ui.active=false;ui.renderer?.pauseAnimation();},
    refresh,reset
  };
})();
