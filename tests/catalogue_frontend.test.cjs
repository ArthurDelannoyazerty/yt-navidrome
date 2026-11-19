"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {browser} = require("./frontend_harness.cjs");
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(storage = new Map(), user = "admin") {
  const b = browser();
  b.context.localStorage = {
    getItem: key => storage.get(key) ?? null,
    setItem: (key,value) => storage.set(key,String(value)),
    removeItem: key => storage.delete(key),
  };
  b.app.state.user = user;
  const checkboxes = ["artist", "recording", "release", "release-group", "work"].map(value =>
    ({value, checked:true, addEventListener(name, handler){this[name]=handler;}}));
  b.context.document.querySelectorAll = selector =>
    selector === ".graph-type" ? checkboxes : selector === ".graph-type:checked" ? checkboxes.filter(c => c.checked) : [];
  const canvas = b.get("graphCanvas");
  canvas.clientWidth = 1000;
  canvas.clientHeight = 500;
  canvas.closest = () => null;
  b.get("graphView").value = "downloaded";
  b.get("graphLayout").value = "free";
  b.get("graphLimit").value = "180";
  b.get("graphLimit").max = "10000";
  b.get("graphCenterForce").value = "0.35";
  b.get("graphRepelForce").value = "7.5";
  b.get("graphLinkForce").value = "0.30";
  b.get("graphLinkDistance").value = "90";
  const built = [];
  const makeGraph = () => {
    const graph = {data:{nodes:[],links:[]},forces:{},
      zoomValue:1,center:{x:0,y:0},paused:false,fitCount:0,reheats:0,settings:{},
      graphData(value){if (value===undefined) return this.data;this.data=value;return this;},
      d3Force(kind, value){
        if (arguments.length===1) return this.forces[kind];
        this.forces[kind] = value;return this;
      },
      d3ReheatSimulation(){this.reheated=true;this.reheats++;return this;},
      zoom(value){if (value===undefined)return this.zoomValue;this.zoomValue=value;return this;},
      zoomToFit(){this.fitCount++;return this;},
      centerAt(x,y){if (arguments.length===0)return this.center;this.center={x,y};return this;},
      pauseAnimation(){this.paused=true;return this;},
      resumeAnimation(){this.paused=false;return this;}
    };
    graph.forces.charge={strength(v){this.strengthValue=v;return this;},distanceMax(){return this;}};
    graph.forces.link={distance(v){this.distanceValue=v;return this;},strength(v){this.strengthValue=v;return this;}};
    for (const name of ["width","height","backgroundColor","nodeRelSize","nodeVal","nodeLabel","linkLabel",
      "nodePointerAreaPaint","nodeCanvasObjectMode","minZoom","maxZoom","enableZoomInteraction",
      "enableNodeDrag","cooldownTicks","cooldownTime","autoPauseRedraw","onNodeClick","onNodeHover",
      "onNodeDrag","onNodeDragEnd","onZoomEnd","onEngineStop","nodeCanvasObject","linkColor","linkWidth"]) {
      graph[name]=function(value){this.settings[name]=value;return this;};
    }
    built.push(graph);
    return graph;
  };
  b.context.window.ForceGraph = () => () => makeGraph();
  b.context.window.CatalogueUI = {openEntity:async()=>{}};
  b.context.ResizeObserver = class {observe(){} disconnect(){}};
  vm.runInContext(readFileSync(require.resolve("../src/static/graph-ui.js"),"utf8"),b.context);
  return {b,checkboxes,built,graph:b.context.window.GraphUI,storage};
}

const sample = () => ({
  nodes:[
    {id:"artist:a",kind:"artist",label:"Artist A",owned:false},
    {id:"recording:r",kind:"recording",label:"Song",owned:true},
    {id:"artist:b",kind:"artist",label:"Artist B",owned:false}
  ],
  edges:[{source:"artist:a",target:"recording:r",relation:"performer"},
         {source:"recording:r",target:"artist:b",relation:"performer"}],
  owned_recording_count:1,total_nodes:3,max_selectable_nodes:3,truncated:false
});

test("Graph tab requests chosen node types without exposing another user's scope", async () => {
  const {b, checkboxes, graph} = harness();
  checkboxes.find(x=>x.value==="recording").checked=false;
  checkboxes.find(x=>x.value==="release").checked=false;
  const pending=graph.refresh(),request=b.requests.at(-1);
  const url=new URL(request.url,"https://example.com");
  assert.equal(url.pathname,"/api/catalogue/graph");
  assert.equal(url.searchParams.get("user_id"),"admin");
  assert.equal(url.searchParams.get("kinds"),"artist,release-group,work");
  assert.equal(url.searchParams.get("limit"),"180");
  request.resolve({nodes:[],edges:[],owned_recording_count:0,truncated:false});
  await pending;
  assert.match(b.get("graphStatus").textContent,/0 nodes/);
});

test("Graph tab with all elements disabled performs no API search", async () => {
  const {b,checkboxes,graph}=harness();
  for(const checkbox of checkboxes)checkbox.checked=false;
  await graph.refresh();
  assert.equal(b.requests.length,0);
  assert.match(b.get("graphStatus").textContent,/Enable at least one node type/);
});

test("Graph tab ignores responses from the previous library", async () => {
  const {b,graph}=harness();
  const first=graph.refresh(),old=b.requests.at(-1);
  b.app.state.user="guest";
  const second=graph.refresh(),latest=b.requests.at(-1);
  latest.resolve({nodes:[],edges:[],owned_recording_count:0,truncated:false});await second;
  old.resolve({nodes:[{kind:"recording",id:"recording:old",label:"Private"}],edges:[],truncated:false});
  await first;
  assert.doesNotMatch(b.get("graphStatus").textContent,/Private/);
});

test("Graph explains why owned recordings have no cached relationship edges", async () => {
  const {b,graph}=harness();
  const pending=graph.refresh(),request=b.requests.at(-1);
  request.resolve({nodes:[{kind:"recording",id:"recording:test",label:"Sample",owned:true}],
    edges:[],owned_recording_count:350,truncated:false});
  await pending;
  assert.match(b.get("graphStatus").textContent,/1 nodes · 0 relationships/);
  assert.match(b.get("graphStatus").textContent,/Index downloaded recordings/);
  assert.match(b.get("graphStatus").textContent,/bounded sample/);
});

test("Canvas renderer has physics, zoom, node selection and topology-based layers", async () => {
  const {b,built,graph}=harness();
  const pending=graph.enter();
  await tick();
  b.requests.at(-1).resolve(sample());
  await pending;
  const renderer=built[0];
  assert.equal(renderer.graphData().nodes.length,3);
  assert.equal(renderer.graphData().links.length,2);
  assert.equal(renderer.settings.enableZoomInteraction,true);
  assert.equal(renderer.settings.enableNodeDrag,true);
  assert(renderer.fitCount>0);
  b.get("graphLayout").value="layers";
  b.get("graphLayout").listeners.change();
  assert.equal(typeof renderer.forces.layer,"function");
  const artists=renderer.graphData().nodes.filter(n=>n.kind==="artist");
  assert.notEqual(artists[0].targetX,artists[1].targetX);
  renderer.forces.layer.initialize(renderer.graphData().nodes.map(n=>Object.assign(n,{vx:0,vy:0})));
  renderer.forces.layer(0.5);
  renderer.settings.onNodeClick(artists[0]);
  assert.equal(b.get("graphDetails").children[0].textContent,"Artist A");
  b.get("graphZoomReset").click();
  assert(renderer.fitCount>1);
  graph.leave();
  assert(renderer.paused);
});

test("Graph uses requested bounded limit and clears old canvas", async () => {
  const {b,built,graph}=harness();
  b.get("graphLimit").value="800";
  const pending=graph.refresh();
  assert.match(b.requests.at(-1).url,/limit=800/);
  b.requests.at(-1).resolve(sample());
  await pending;
  const renderer=built[0];
  graph.reset();
  assert(renderer.paused);
  assert.equal(b.get("graphDetails").children.length,0);
});

test("Force graph styles are external to preserve the strict CSP", () => {
  const source = readFileSync(require.resolve("../src/static/force-graph.min.js"), "utf8");
  const css = readFileSync(require.resolve("../src/static/force-graph.css"), "utf8");
  const html = readFileSync(require.resolve("../src/static/index.html"), "utf8");
  assert.doesNotMatch(source, /document\.createElement\("style"\)/);
  assert.doesNotThrow(() => new vm.Script(source, {filename:"force-graph.min.js"}));
  assert.match(css, /\.force-graph-container canvas/);
  assert.match(css, /\.float-tooltip-kap/);
  assert.match(html, /\/static\/force-graph\.css\?v=__STATIC_VERSION__/);
});

test("Obsidian force controls update center attraction, repulsion, link strength and distance", async () => {
  const {b,built,graph}=harness();
  const pending=graph.refresh();b.requests.at(-1).resolve(sample());await pending;
  const renderer=built[0];
  assert.equal(renderer.forces.charge.strengthValue,-3*7.5*7.5);
  assert.equal(renderer.forces.link.strengthValue,0.3);
  assert.equal(renderer.forces.link.distanceValue,90);
  assert.equal(typeof renderer.forces.centerPull,"function");
  b.get("graphCenterForce").value="0.8";b.get("graphCenterForce").listeners.input();
  b.get("graphRepelForce").value="12";b.get("graphRepelForce").listeners.input();
  b.get("graphLinkForce").value="0.7";b.get("graphLinkForce").listeners.input();
  b.get("graphLinkDistance").value="220";b.get("graphLinkDistance").listeners.input();
  assert.equal(renderer.forces.charge.strengthValue,-432);
  assert.equal(renderer.forces.link.strengthValue,0.7);
  assert.equal(renderer.forces.link.distanceValue,220);
  assert.equal(b.get("graphCenterValue").textContent,"0.80");
  assert.equal(b.get("graphRepelValue").textContent,"12.0");
  assert(renderer.reheats>=4);
  const node={x:100,y:-100,vx:0,vy:0};
  renderer.forces.centerPull.initialize([node]);
  renderer.forces.centerPull(1);
  assert(node.vx<0 && node.vy>0,"center force must attract each node toward the origin");
});

test("Number input never requests more than the available node total", async () => {
  const {b,graph}=harness();
  const pending=graph.refresh(),request=b.requests.at(-1);
  const value=sample();value.total_nodes=14;value.max_selectable_nodes=14;value.truncated=true;
  request.resolve(value);await pending;
  assert.equal(b.get("graphLimit").value,"14");
  assert.equal(b.get("graphLimit").max,"14");
  assert.match(b.get("graphTotal").textContent,/14 available/);
  b.get("graphLimit").value="100000";
  b.get("graphLimit").listeners.change();
  const second=b.requests.at(-1);
  assert.equal(new URL(second.url,"https://localhost").searchParams.get("limit"),"14");
  second.resolve(value);await tick();
  assert.equal(b.get("graphLimit").value,"14");
});

test("Browser snapshots restore graph, positions, camera, force controls and selection without refetching", async () => {
  const stored=new Map();
  const first=harness(stored);
  const pending=first.graph.enter();await tick();
  first.b.requests.at(-1).resolve(sample());await pending;
  const old=first.built[0];old.data.nodes[0].x=345;old.data.nodes[0].y=-78;
  old.centerAt(20,40);old.zoom(2);
  first.b.get("graphLinkDistance").value="175";first.b.get("graphLinkDistance").listeners.input();
  first.b.get("graphLayout").value="layers";first.b.get("graphLayout").listeners.change();
  old.settings.onNodeClick(old.data.nodes[0]);
  first.graph.leave();
  const next=harness(stored);
  await next.graph.enter();
  assert.equal(next.b.requests.length,0,"restoration should use saved data without API requests");
  const renderer=next.built[0];
  assert.equal(renderer.settings.cooldownTicks,0,"cached positions must not restart physics");
  assert.equal(renderer.reheats,0);
  assert.equal(renderer.graphData().nodes[0].x,345);
  assert.equal(renderer.graphData().nodes[0].y,-78);
  assert.equal(renderer.zoomValue,2);
  assert.equal(renderer.center.x,20);
  assert.equal(next.b.get("graphLinkDistance").value,"175");
  assert.equal(next.b.get("graphLayout").value,"layers");
  assert.equal(next.b.get("graphNodeSelect").value,"artist:a");
  assert.match(next.b.get("graphStatus").textContent,/Restored from this browser/);
  const explicit=next.graph.refresh();
  assert.equal(next.b.requests.length,1,"Explore must still refresh the live graph");
  next.b.requests[0].resolve(sample());await explicit;
});

test("Saved graph data from one library user is not visible to another", async () => {
  const stored=new Map(),admin=harness(stored);
  const first=admin.graph.refresh();admin.b.requests[0].resolve(sample());await first;
  admin.graph.leave();
  const guest=harness(stored,"guest"),loading=guest.graph.enter();
  await tick();
  assert.equal(guest.b.requests.length,1);
  assert.equal(new URL(guest.b.requests[0].url,"https://localhost").searchParams.get("user_id"),"guest");
  guest.b.requests[0].resolve({nodes:[],edges:[],total_nodes:0,max_selectable_nodes:0,owned_recording_count:0,truncated:false});
  await loading;
  assert.equal(guest.b.get("graphDetails").children.length,0);
});

test("Graph legend and four force sliders match node colors without zoom buttons", () => {
  const html=readFileSync(require.resolve("../src/static/index.html"),"utf8");
  const css=readFileSync(require.resolve("../src/static/style.css"),"utf8");
  const js=readFileSync(require.resolve("../src/static/graph-ui.js"),"utf8");
  assert.match(html,/id="graphLimit" type="number"/);
  assert.match(html,/id="graphTotal"/);
  for(const name of ["graphCenterForce","graphRepelForce","graphLinkForce","graphLinkDistance"])
    assert(html.includes('id="'+name+'" type="range"'));
  for(const kind of ["artist","recording","release","release-group","work"]) {
    assert(html.includes("graph-dot-"+kind));
    assert(css.includes(".graph-dot-"+kind+" {"));
  }
  assert.doesNotMatch(html,/id="graphZoomIn"|id="graphZoomOut"/);
  assert(!js.includes('get("graphZoomIn")') && !js.includes('get("graphZoomOut")'));
});
