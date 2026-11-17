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
    ({value, checked:true, addEventListener(name, handler){ this[name] = handler; }}));
  b.context.document.querySelectorAll = selector =>
    selector === ".graph-type" ? checkboxes : selector === ".graph-type:checked" ? checkboxes.filter(c => c.checked) : [];
  b.context.document.createElementNS = (namespace, type) => {
    const e = b.context.document.createElement(type);
    e.setPointerCapture = () => {};
    e.getBoundingClientRect = () => ({width:1000,height:640});
    return e;
  };
  const svg = b.get("graphSvg");
  svg.setPointerCapture = () => {};
  svg.getBoundingClientRect = () => ({width:1000,height:640});
  vm.runInContext(readFileSync(require.resolve("../src/static/graph-ui.js"), "utf8"), b.context);
  return {b, checkboxes, graph:b.context.window.GraphUI};
}

test("Graph tab requests chosen node types without exposing another user's scope", async () => {
  const {b, checkboxes, graph} = harness();
  checkboxes.find(x => x.value === "recording").checked = false;
  checkboxes.find(x => x.value === "release").checked = false;
  const pending = graph.refresh();
  const request = b.requests.at(-1);
  assert.match(request.url, /\/api\/catalogue\/graph\?/);
  const url = new URL(request.url, "https://example.com");
  assert.equal(url.searchParams.get("user_id"), "admin");
  assert.equal(url.searchParams.get("kinds"), "artist,release-group,work");
  request.resolve({nodes:[],edges:[],owned_recording_count:0,truncated:false});
  await pending;
  assert.match(b.get("graphStatus").textContent, /0 nodes/);
});

test("Graph tab with all elements disabled performs no API search", async () => {
  const {b, checkboxes, graph} = harness();
  for (const checkbox of checkboxes) checkbox.checked = false;
  await graph.refresh();
  assert.equal(b.requests.length, 0);
  assert.match(b.get("graphStatus").textContent, /Enable at least one node type/);
});

test("Graph tab ignores responses from the previous library", async () => {
  const {b, graph} = harness();
  const first = graph.refresh();
  const old = b.requests.at(-1);
  b.app.state.user = "guest";
  const second = graph.refresh();
  const latest = b.requests.at(-1);
  latest.resolve({nodes:[],edges:[],owned_recording_count:0,truncated:false});
  await second;
  old.resolve({nodes:[{kind:"recording",id:"recording:old",label:"Private"}],edges:[],truncated:false});
  await first;
  assert.doesNotMatch(b.get("graphStatus").textContent, /Private/);
});


test("Graph explains why owned recordings have no cached relationship edges", async () => {
  const {b, graph} = harness();
  const pending = graph.refresh(), request = b.requests.at(-1);
  request.resolve({
    nodes:[{kind:"recording", id:"recording:test", label:"Creo - Sample", owned:true}],
    edges:[], owned_recording_count:350, truncated:false
  });
  await pending;
  const message = b.get("graphStatus").textContent;
  assert.match(message, /1 nodes · 0 relationships/);
  assert.match(message, /Index downloaded recordings/);
  assert.match(message, /bounded sample/);
});
