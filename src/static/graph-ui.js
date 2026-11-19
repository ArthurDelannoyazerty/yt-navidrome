/* Locally bundled force-graph renderer for the cached MusicBrainz graph. */
"use strict";
window.GraphUI = (() => {
  const get = id => document.getElementById(id);
  const stage = get("graphCanvas");
  const colors = {artist:"#8086e7",recording:"#24a582",release:"#e19b58","release-group":"#b993dc",work:"#75b0d3"};
  const ui = {user:null,serial:0,graph:null,renderer:null,selected:null,hovered:null,active:false,positions:new Map(),observer:null};
  const kinds = () => [...document.querySelectorAll(".graph-type:checked")].map(x=>x.value);
  const status = (s,error=false) => {get("graphStatus").textContent=s;get("graphStatus").className=error?"error-text":"hint";};
  const dimensions = () => ({width:Math.max(250,stage.clientWidth||900),height:Math.max(320,stage.clientHeight||500)});
  const end = item => typeof item === "object" ? item.id : item;
  const radius = n => n.kind==="artist" ? 10 : n.kind==="recording" ? 9 : 8;
  const escape = s => String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function assignLayers(nodes,links) {
    const lookup=new Map(nodes.map(n=>[n.id,n]));
    const adjacent=new Map(nodes.map(n=>[n.id,new Set()]));
    for(const e of links) {
      if(!adjacent.has(e.source)||!adjacent.has(e.target))continue;
      adjacent.get(e.source).add(e.target);adjacent.get(e.target).add(e.source);
    }
    const visited=new Set();let offset=0;
    for(const seed of nodes) {
      if(visited.has(seed.id))continue;
      const component=[],queue=[seed.id];visited.add(seed.id);
      for(let i=0;i<queue.length;i++) {
        const id=queue[i];component.push(id);
        for(const neighbor of adjacent.get(id))if(!visited.has(neighbor)){visited.add(neighbor);queue.push(neighbor);}
      }
      const root=component.sort((a,b)=>Number(lookup.get(b).kind==="artist")-Number(lookup.get(a).kind==="artist")||adjacent.get(b).size-adjacent.get(a).size||a.localeCompare(b))[0];
      const depth=new Map([[root,0]]),bfs=[root];
      for(let i=0;i<bfs.length;i++)for(const neighbor of adjacent.get(bfs[i])) {
        if(!depth.has(neighbor)){depth.set(neighbor,depth.get(bfs[i])+1);bfs.push(neighbor);}
      }
      const layers=new Map();
      for(const id of component){const d=depth.get(id);if(!layers.has(d))layers.set(d,[]);layers.get(d).push(lookup.get(id));}
      let maxRows=1;
      for(const [d,items] of layers) {
        items.sort((a,b)=>adjacent.get(b.id).size-adjacent.get(a.id).size||a.label.localeCompare(b.label));
        maxRows=Math.max(maxRows,items.length);
        items.forEach((n,i)=>{n.targetX=d*175;n.targetY=offset+(i-(items.length-1)/2)*61;});
      }
      offset+=Math.max(260,maxRows*61+125);
    }
    for(const node of nodes)node.targetY-=(offset-260)/2;
  }
  function layersForce() {
    let nodes=[];
    function force(alpha){for(const n of nodes){n.vx+=(n.targetX-n.x)*.095*alpha;n.vy+=(n.targetY-n.y)*.035*alpha;}}
    force.initialize=value=>{nodes=value;};
    return force;
  }
  function configure() {
    if(!ui.renderer)return;
    const layered=get("graphLayout").value==="layers";
    ui.renderer.d3Force("charge").strength(layered?-135:-180).distanceMax(850);
    ui.renderer.d3Force("link").distance(layered?96:85).strength(layered?.18:.33);
    ui.renderer.d3Force("layer",layered?layersForce():null);
    ui.renderer.d3ReheatSimulation();
  }
  function focused(link){return !!ui.selected&&(end(link.source)===ui.selected.id||end(link.target)===ui.selected.id);}
  function paint(n,ctx,scale) {
    const active=ui.selected?.id===n.id||ui.hovered===n.id,r=radius(n);
    ctx.beginPath();ctx.arc(n.x,n.y,r,0,Math.PI*2);ctx.fillStyle=colors[n.kind]||"#a6b3c6";ctx.fill();
    ctx.lineWidth=active?2.8:n.owned?2.5:.8;
    ctx.strokeStyle=active?"#fff4a3":n.owned?"#f9d66e":"#e2e8f0";ctx.stroke();
    if(!active&&scale<1.05)return;
    const label=n.label.length>38?n.label.slice(0,35)+"…":n.label;
    const font=11/Math.max(.65,scale);
    ctx.font=font+"px system-ui, sans-serif";ctx.textAlign="center";ctx.textBaseline="top";
    ctx.lineWidth=3/Math.max(.65,scale);ctx.strokeStyle="#101824";ctx.strokeText(label,n.x,n.y+r+4);
    ctx.fillStyle="#f1f5f9";ctx.fillText(label,n.x,n.y+r+4);
  }
  function repaint() {
    ui.renderer?.nodeCanvasObject(paint)
      .linkColor(e=>ui.selected?(focused(e)?"rgba(250,204,21,.82)":"rgba(135,151,169,.14)"):"rgba(135,151,169,.50)")
      .linkWidth(e=>focused(e)?2.2:1);
  }
  function resize(){
    if(!ui.renderer||stage.closest?.("[hidden]"))return;
    const d=dimensions();ui.renderer.width(d.width).height(d.height);
  }
  function ensureRenderer(){
    if(ui.renderer)return ui.renderer;
    if(typeof window.ForceGraph!=="function")throw new Error("Local force-graph renderer unavailable. Reload the page.");
    const d=dimensions();
    ui.renderer=window.ForceGraph()(stage)
      .width(d.width).height(d.height).backgroundColor("rgba(0,0,0,0)")
      .nodeRelSize(1).nodeVal(n=>radius(n)**2)
      .nodeLabel(n=>escape(n.label)+" ("+escape(n.kind)+")")
      .linkLabel(e=>escape(e.relation))
      .nodePointerAreaPaint((n,c,ctx)=>{ctx.fillStyle=c;ctx.beginPath();ctx.arc(n.x,n.y,radius(n)+5,0,Math.PI*2);ctx.fill();})
      .nodeCanvasObjectMode("replace")
      .minZoom(.12).maxZoom(9).enableZoomInteraction(true).enableNodeDrag(true)
      .cooldownTicks(230).cooldownTime(9000).autoPauseRedraw(true)
      .onNodeClick(select).onNodeHover(n=>{ui.hovered=n?.id||null;repaint();})
      .onNodeDragEnd(n=>{n.fx=null;n.fy=null;ui.renderer.d3ReheatSimulation();});
    repaint();
    if(typeof ResizeObserver!=="undefined"){ui.observer=new ResizeObserver(resize);ui.observer.observe(stage);}
    return ui.renderer;
  }
  function populatePicker(nodes){
    const picker=get("graphNodeSelect");picker.replaceChildren();
    const first=document.createElement("option");first.value="";first.textContent="Jump to a node…";picker.append(first);
    for(const n of [...nodes].sort((a,b)=>a.label.localeCompare(b.label))){
      const option=document.createElement("option");option.value=n.id;option.textContent=n.label+" ("+n.kind+")";picker.append(option);
    }
    picker.value="";
  }
  function draw(result){
    for(const n of ui.renderer?.graphData().nodes||[])if(Number.isFinite(n.x)&&Number.isFinite(n.y))ui.positions.set(n.id,{x:n.x,y:n.y});
    const nodes=(result.nodes||[]).map(n=>({...n})),ids=new Set(nodes.map(n=>n.id));
    const links=(result.edges||[]).filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>({...e}));
    populatePicker(nodes);get("graphEmpty").hidden=nodes.length>0;
    if(!nodes.length){ui.renderer?.graphData({nodes:[],links:[]});return;}
    assignLayers(nodes,links);
    nodes.forEach((n,i)=>{const p=ui.positions.get(n.id);n.x=p?.x??n.targetX+(i%5-2)*12;n.y=p?.y??n.targetY+(i%7-3)*9;});
    const network=ensureRenderer();configure();network.graphData({nodes,links});
    requestAnimationFrame(()=>{if(ui.renderer===network)network.zoomToFit(250,40);});
    if(!ui.active)network.pauseAnimation();
  }
  function select(node){
    ui.selected=node;get("graphNodeSelect").value=node.id;
    const box=get("graphDetails");box.replaceChildren();
    const heading=document.createElement("h3");heading.textContent=node.label;
    const info=document.createElement("p");info.textContent=node.kind+" · "+node.id.slice(node.kind.length+1)+(node.owned?" · Downloaded in this library":" · Public catalogue only");
    box.append(heading,info);
    const inspect=document.createElement("button");inspect.type="button";inspect.className="secondary";inspect.textContent="Select in Catalogue";
    inspect.addEventListener("click",()=>{setTab("catalogue");window.CatalogueUI?.openEntity(node.kind,node.id.slice(node.kind.length+1)).catch(error=>status(error.message,true));});
    const focus=document.createElement("button");focus.type="button";focus.className="secondary";focus.textContent="Expand neighborhood";
    focus.addEventListener("click",()=>{get("graphFocus").value=node.id.slice(node.kind.length+1);refresh();});
    box.append(inspect,focus);repaint();
  }
  async function refresh(){
    if(!state.user)return;
    const user=state.user,serial=++ui.serial;
    if(!kinds().length){
      ui.graph=null;ui.selected=null;ui.renderer?.graphData({nodes:[],links:[]});get("graphEmpty").hidden=false;
      get("graphDetails").replaceChildren();populatePicker([]);status("Enable at least one node type.");return;
    }
    const params=new URLSearchParams({user_id:user,view:get("graphView").value,
      kinds:kinds().join(","),q:get("graphQuery").value.trim(),focus_id:get("graphFocus").value.trim(),
      limit:get("graphLimit").value||"180"});
    status("Loading graph neighborhood from the local catalogue...");
    try{
      const result=await api("/api/catalogue/graph?"+params);
      if(serial!==ui.serial||user!==state.user)return;
      ui.user=user;ui.graph=result;ui.selected=null;ui.hovered=null;get("graphDetails").replaceChildren();
      draw(result);
      const missing=result.nodes.length&&!result.edges.length?" No cached connections yet: open Catalogue, choose Index downloaded recordings, then Refresh progress. Indexing can take time.":"";
      const sampled=get("graphView").value==="downloaded"&&(result.owned_recording_count||0)>result.nodes.filter(n=>n.owned).length
        ?" This is a bounded sample, not your complete music library; use search or a MusicBrainz ID to focus.":"";
      status(result.nodes.length+" nodes · "+result.edges.length+" relationships · "+(result.owned_recording_count||0)+" owned recordings."+missing+sampled+(result.truncated?" Graph truncated: enter a filter or focus on an ID to explore more.":""));
    }catch(error){if(serial===ui.serial&&user===state.user)status(error.message,true);}
  }
  function zoom(factor){if(ui.renderer)ui.renderer.zoom(Math.max(.12,Math.min(9,ui.renderer.zoom()*factor)),180);}
  function fit(){if(ui.renderer&&ui.graph?.nodes?.length)ui.renderer.zoomToFit(300,40);}
  function reset(){
    ui.user=null;ui.serial++;ui.graph=null;ui.selected=null;ui.hovered=null;ui.active=false;ui.positions.clear();
    ui.observer?.disconnect();ui.observer=null;ui.renderer?.pauseAnimation();ui.renderer=null;
    stage.replaceChildren();get("graphDetails").replaceChildren();get("graphEmpty").hidden=true;populatePicker([]);status("");
  }
  get("graphSearchForm").addEventListener("submit",event=>{event.preventDefault();refresh();});
  get("graphView").addEventListener("change",refresh);
  get("graphLimit").addEventListener("change",refresh);
  get("graphLayout").addEventListener("change",()=>{if(ui.renderer&&ui.graph?.nodes?.length)configure();});
  for(const checkbox of document.querySelectorAll(".graph-type"))checkbox.addEventListener("change",refresh);
  get("graphZoomIn").addEventListener("click",()=>zoom(1.3));
  get("graphZoomOut").addEventListener("click",()=>zoom(1/1.3));
  get("graphZoomReset").addEventListener("click",fit);
  get("graphNodeSelect").addEventListener("change",()=>{
    const id=get("graphNodeSelect").value,node=ui.renderer?.graphData().nodes.find(n=>n.id===id);
    if(!node)return;select(node);ui.renderer.centerAt(node.x,node.y,300);ui.renderer.zoom(Math.max(1.5,ui.renderer.zoom()),300);
  });
  stage.addEventListener("keydown",event=>{
    if(event.key==="+"||event.key==="="){event.preventDefault();zoom(1.3);}
    if(event.key==="-"){event.preventDefault();zoom(1/1.3);}
    if(event.key==="0"){event.preventDefault();fit();}
  });
  window.addEventListener("resize",resize);
  return {enter:()=>{
    if(!state.user)return;ui.active=true;
    if(!ui.graph||ui.user!==state.user)return refresh();
    ui.renderer?.resumeAnimation();resize();
  },leave:()=>{ui.active=false;ui.renderer?.pauseAnimation();},refresh,reset};
})();
