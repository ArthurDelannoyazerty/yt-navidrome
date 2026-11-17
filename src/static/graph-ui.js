/* Bounded public-catalogue graph with individually toggleable entity types. */
"use strict";
window.GraphUI = (() => {
  const $g = id => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const ui = {user: null, serial: 0, graph: null, zoom: 1, x: 0, y: 0, group: null, dragged: null, selected: null};
  const TYPE_X = {artist: 95, work: 285, recording: 485, release: 690, "release-group": 900};
  const svg = $g("graphSvg");
  function svgNode(type, attrs = {}, text) {
    const el = document.createElementNS(NS, type);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
    if (text != null) el.textContent = String(text);
    return el;
  }
  function status(value, error = false) {
    $g("graphStatus").textContent = value;
    $g("graphStatus").className = error ? "error-text" : "hint";
  }
  function reset() {
    ui.user = null; ui.serial++; ui.graph = null; ui.selected = null;
    ui.zoom = 1; ui.x = 0; ui.y = 0; ui.group = null;
    svg.replaceChildren(); $g("graphDetails").replaceChildren(); status("");
  }
  function kinds() { return [...document.querySelectorAll(".graph-type:checked")].map(checkbox => checkbox.value); }
  async function refresh() {
    if (!state.user) return;
    const user = state.user, serial = ++ui.serial;
    if (!kinds().length) { svg.replaceChildren(); status("Enable at least one node type."); return; }
    const params = new URLSearchParams({user_id: user, view: $g("graphView").value,
      kinds: kinds().join(","), q: $g("graphQuery").value.trim(), focus_id: $g("graphFocus").value.trim(), limit: "180"});
    status("Loading graph neighborhood from the local catalogue...");
    try {
      const result = await api(`/api/catalogue/graph?${params}`);
      if (serial !== ui.serial || user !== state.user) return;
      ui.user = user; ui.graph = result; ui.selected = null;
      ui.zoom = 1; ui.x = 0; ui.y = 0;
      draw(result);
      status(`${result.nodes.length} nodes · ${result.edges.length} relationships · ${result.owned_recording_count} owned recordings indexed.${result.truncated ? " Graph truncated: enter an artist/music filter or focus on an ID to explore more." : ""}`);
    } catch (error) { if (serial === ui.serial && user === state.user) status(error.message, true); }
  }
  function positions(nodes, edges) {
    const byType = new Map(), degree = new Map();
    for (const node of nodes) { const arr = byType.get(node.kind) || []; arr.push(node); byType.set(node.kind, arr); degree.set(node.id, 0); }
    for (const edge of edges) { degree.set(edge.source, (degree.get(edge.source) || 0) + 1); degree.set(edge.target, (degree.get(edge.target) || 0) + 1); }
    const largest = Math.max(1, ...[...byType.values()].map(x => x.length));
    const height = Math.max(630, largest * 42 + 85);
    const positions = new Map();
    for (const [kind, items] of byType.entries()) {
      items.sort((a,b) => (degree.get(b.id) - degree.get(a.id)) || a.label.localeCompare(b.label));
      for (const [i,node] of items.entries()) {
        const y = height * (i + 1) / (items.length + 1);
        positions.set(node.id, {x: TYPE_X[kind] || 480, y});
      }
    }
    // A few vertical-only iterations bring connected nodes into aligned clusters
    // without running an expensive 2D all-pairs force simulation.
    for (let iteration = 0; iteration < 32; iteration++) {
      const desired = new Map(), counts = new Map();
      for (const edge of edges) {
        const a = positions.get(edge.source), b = positions.get(edge.target);
        if (!a || !b) continue;
        desired.set(edge.source, (desired.get(edge.source) || 0) + b.y);
        desired.set(edge.target, (desired.get(edge.target) || 0) + a.y);
        counts.set(edge.source, (counts.get(edge.source) || 0) + 1);
        counts.set(edge.target, (counts.get(edge.target) || 0) + 1);
      }
      for (const [kind, items] of byType.entries()) {
        items.sort((a,b) => positions.get(a.id).y - positions.get(b.id).y);
        for (const [i,node] of items.entries()) {
          const point = positions.get(node.id);
          if (counts.get(node.id)) point.y = point.y * .84 + (desired.get(node.id) / counts.get(node.id)) * .16;
          const low = i * 31 + 30, high = height - ((items.length - i - 1) * 31 + 30);
          point.y = Math.min(height - 25, Math.max(25, Math.min(high, Math.max(low, point.y))));
          if (i && point.y < positions.get(items[i-1].id).y + 31) point.y = positions.get(items[i-1].id).y + 31;
        }
      }
    }
    return {positions, height};
  }
  function draw(result) {
    svg.replaceChildren();
    const nodes = result.nodes || [], edges = result.edges || [];
    if (!nodes.length) { svg.append(svgNode("text", {x:50,y:70,fill:"#e2e8f0"}, "No nodes match this view. Search online in Catalogue or change graph filters.")); return; }
    const {positions: p, height} = positions(nodes, edges);
    svg.setAttribute("viewBox", `0 0 1000 ${height}`);
    const world = svgNode("g"); ui.group = world;
    for (const edge of edges) {
      const a = p.get(edge.source), b = p.get(edge.target);
      if (!a || !b) continue;
      const line = svgNode("line", {class:"graph-edge",x1:a.x,y1:a.y,x2:b.x,y2:b.y});
      line.append(svgNode("title", {}, edge.relation)); world.append(line);
    }
    for (const item of nodes) {
      const pos = p.get(item.id); if (!pos) continue;
      const group = svgNode("g", {class:"graph-node",role:"button",tabindex:"0",
        "data-kind":item.kind,"data-owned":String(item.owned),transform:`translate(${pos.x},${pos.y})`});
      const circle = svgNode("circle", {r:item.kind === "recording" ? 13 : 11});
      const label = item.label.length > 30 ? item.label.slice(0,27) + "..." : item.label;
      group.append(circle, svgNode("text", {y:28}, label), svgNode("title", {}, `${item.kind}: ${item.label}${item.owned ? " (owned)" : ""}`));
      const activate = () => select(item);
      group.addEventListener("click", activate);
      group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); } });
      world.append(group);
    }
    svg.append(world); updateView();
  }
  function updateView() {
    if (!ui.group) return;
    ui.group.setAttribute("transform", `translate(${ui.x},${ui.y}) scale(${ui.zoom})`);
  }
  function zoom(factor) {
    ui.zoom = Math.max(.25, Math.min(4, ui.zoom * factor)); updateView();
  }
  function select(item) {
    ui.selected = item;
    const box = $g("graphDetails"); box.replaceChildren();
    const h = document.createElement("h3"); h.textContent = item.label;
    const meta = document.createElement("p"); meta.textContent = `${item.kind} · ${item.id.split(":").slice(1).join(":")}${item.owned ? " · Downloaded in this library" : " · Public catalogue only"}`;
    box.append(h,meta);
    const inspect = document.createElement("button"); inspect.type="button"; inspect.className="secondary";
    inspect.textContent = "Inspect in Catalogue";
    inspect.addEventListener("click", () => {
      setTab("catalogue"); window.CatalogueUI?.openEntity(item.kind, item.id.slice(item.kind.length + 1)).catch(error => status(error.message, true));
    });
    const focus = document.createElement("button"); focus.type="button"; focus.className="secondary";
    focus.textContent = "Expand neighborhood";
    focus.addEventListener("click", () => { $g("graphFocus").value = item.id.slice(item.kind.length + 1); refresh(); });
    box.append(inspect,focus);
  }
  $g("graphSearchForm").addEventListener("submit", event => { event.preventDefault(); refresh(); });
  $g("graphView").addEventListener("change", refresh);
  for (const checkbox of document.querySelectorAll(".graph-type")) checkbox.addEventListener("change", refresh);
  $g("graphZoomIn").addEventListener("click", () => zoom(1.25));
  $g("graphZoomOut").addEventListener("click", () => zoom(.8));
  $g("graphZoomReset").addEventListener("click", () => {ui.zoom=1;ui.x=0;ui.y=0;updateView();});
  svg.addEventListener("pointerdown", event => {
    if (event.target.closest?.(".graph-node")) return;
    ui.dragged = {x:event.clientX,y:event.clientY,startX:ui.x,startY:ui.y};
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", event => {
    if (!ui.dragged) return;
    const rect = svg.getBoundingClientRect();
    ui.x = ui.dragged.startX + (event.clientX - ui.dragged.x) * 1000 / rect.width;
    ui.y = ui.dragged.startY + (event.clientY - ui.dragged.y) * 1000 / rect.width;
    updateView();
  });
  for (const event of ["pointerup","pointercancel","lostpointercapture"]) svg.addEventListener(event, () => {ui.dragged=null;});
  svg.addEventListener("wheel", event => { if (!event.ctrlKey) return; event.preventDefault(); zoom(event.deltaY > 0 ? .9 : 1.1); }, {passive:false});
  return {enter: () => {if (state.user && (!ui.graph || ui.user !== state.user)) return refresh();}, refresh, reset};
})();
