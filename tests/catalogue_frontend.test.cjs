"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {browser} = require("./frontend_harness.cjs");
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const b = browser();
  b.app.state.user = "admin";
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
  const built = [];
  const makeGraph = () => {
    const graph = {data:{nodes:[],links:[]},forces:{},
      zoomValue:1,paused:false,fitCount:0,settings:{},
      graphData(value){if (value===undefined) return this.data;this.data=value;return this;},
      d3Force(kind, value){
        if (arguments.length===1) return this.forces[kind];
        this.forces[kind] = value;return this;
      },
      d3ReheatSimulation(){this.reheated=true;return this;},
      zoom(value){if (value===undefined)return this.zoomValue;this.zoomValue=value;return this;},
      zoomToFit(){this.fitCount++;return this;},
      centerAt(x,y){this.center={x,y};return this;},
      pauseAnimation(){this.paused=true;return this;},
      resumeAnimation(){this.paused=false;return this;}
    };
    graph.forces.charge={strength(){return this;},distanceMax(){return this;}};
    graph.forces.link={distance(){return this;},strength(){return this;}};
    for (const name of ["width","height","backgroundColor","nodeRelSize","nodeVal","nodeLabel","linkLabel",
      "nodePointerAreaPaint","nodeCanvasObjectMode","minZoom","maxZoom","enableZoomInteraction",
      "enableNodeDrag","cooldownTicks","cooldownTime","autoPauseRedraw","onNodeClick","onNodeHover",
      "onNodeDragEnd","nodeCanvasObject","linkColor","linkWidth"]) {
      graph[name]=function(value){this.settings[name]=value;return this;};
    }
    built.push(graph);
    return graph;
  };
  b.context.window.ForceGraph = () => () => makeGraph();
  b.context.window.CatalogueUI = {openEntity:async()=>{}};
  b.context.ResizeObserver = class {observe(){} disconnect(){}};
  vm.runInContext(readFileSync(require.resolve("../src/static/graph-ui.js"),"utf8"),b.context);
  return {b,checkboxes,built,graph:b.context.window.GraphUI};
}

const sample = () => ({
  nodes:[
    {id:"artist:a",kind:"artist",label:"Artist A",owned:false},
    {id:"recording:r",kind:"recording",label:"Song",owned:true},
    {id:"artist:b",kind:"artist",label:"Artist B",owned:false}
  ],
  edges:[{source:"artist:a",target:"recording:r",relation:"performer"},
         {source:"recording:r",target:"artist:b",relation:"performer"}],
  owned_recording_count:1,truncated:false
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
  b.get("graphZoomIn").click();
  assert(renderer.zoomValue>1);
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
  assert.match(css, /\.force-graph-container canvas/);
  assert.match(css, /\.float-tooltip-kap/);
  assert.match(html, /\/static\/force-graph\.css\?v=__STATIC_VERSION__/);
});
