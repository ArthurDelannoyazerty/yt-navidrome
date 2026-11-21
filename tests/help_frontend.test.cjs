"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {TABS, tabNames, helpSectionNames, helpLinkNames} = require("./navigation_contract.cjs");

const renderer = vm.createContext({});
vm.runInContext(readFileSync(require.resolve("../src/static/pele-flowchart.js"), "utf8"), renderer);

function setup() {
  const panel = {hidden: true}, container = {}, status = {}, scripts = [], mounts = [], windowEvents = {};
  const topics=TABS.map(id=>{
    const attributes={href:"#help-"+id};
    return {dataset:{topic:id},listeners:{},getAttribute:key=>attributes[key]??null,
      setAttribute(key,value){attributes[key]=String(value);},
      removeAttribute(key){delete attributes[key];},
      addEventListener(name,listener){this.listeners[name]=listener;}};
  });
  const controls = new Map(["workflowZoom", "workflowZoomIn", "workflowZoomOut", "workflowZoomReset"].map(id =>
    [id, {listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }}]));
  let changed;
  const window = {addEventListener: (name, fn) => { windowEvents[name] = fn; }};
  const document = {
    currentScript: {src: "https://ingestor.example/static/help-ui.js?v=content-hash"},
    importNode: node => node,
    querySelector: () => panel,
    querySelectorAll: selector => selector===".help-sidebar a" ? topics : [],
    getElementById: id => controls.get(id) || (id === "workflowDiagram" ? container : status),
    createElement: () => ({
      listeners: {},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      remove() { scripts.splice(scripts.indexOf(this), 1); },
    }),
    head: {append: script => scripts.push(script)},
  };
  class MutationObserver {
    constructor(callback) { changed = callback; }
    observe() {}
  }
  class DOMParser {
    parseFromString(markup, type) { assert.equal(type, "image/svg+xml"); return {documentElement: {markup}}; }
  }
  vm.runInNewContext(readFileSync(require.resolve("../src/static/help-ui.js"), "utf8"),
    {window, document, URL, MutationObserver, DOMParser, IngestorUI: require("../src/static/ui-state.js")});
  function loaded() {
    window.IngestorPele = {mount(target, source, options) {
      const result = renderer.IngestorPele.render(source, {fontSize: options.fontSize, idPrefix: "help-test"});
      const mounted = {result, refreshes: 0, destroyed: false,
        refresh() { this.refreshes++; }, destroy() { this.destroyed = true; }};
      mounted.options = options;
      mounted.node = options.sanitize(result.svg);
      mounts.push(mounted);
      options.onRender(result);
      return mounted;
    }};
    scripts[0].listeners.load();
  }
  return {panel, status, scripts, mounts, loaded, windowEvents, topics,
    help:window.HelpUI,
    show() { panel.hidden = false; return changed(); },
    hide() { panel.hidden = true; return changed(); }};
}

test("Help lazily loads the versioned local bundle and renders all seven states", async () => {
  const page = setup();
  assert.equal(page.scripts.length, 0);
  const entering = page.show();
  assert.equal(page.scripts.length, 1);
  assert.equal(page.scripts[0].src, "https://ingestor.example/static/pele-flowchart.js?v=content-hash");
  page.loaded(); await entering;
  assert.equal(page.mounts.length, 1);
  assert.equal(page.mounts[0].options.zoom.controls, false);
  assert.equal(typeof page.mounts[0].options.sanitize, "function");
  assert.doesNotMatch(page.mounts[0].node.markup, /\sstyle=/);
  const svg = page.mounts[0].result.svg;
  assert.match(svg, /<title[^>]*>Music operation states<\/title>/);
  assert.match(svg, /aria-describedby=/);
  const states = [...svg.matchAll(/class="pele-node [^"]*" data-id="([^"]+)"/g)].map(match => match[1]).sort();
  assert.deepEqual(states, ["deferred", "failed", "idle", "processing", "queued", "review", "stopped"]);
  assert.equal(page.status.hidden, true);
  await page.hide(); await page.show();
  assert.equal(page.scripts.length, 1);
  assert.equal(page.mounts.length, 1);
  assert.equal(page.mounts[0].refreshes, 1);
  page.windowEvents.pagehide();
  assert.equal(page.mounts[0].destroyed, true);
});

test("a renderer load failure preserves the readable fallback and can be retried", async () => {
  const page = setup(), entering = page.show();
  page.scripts[0].listeners.error(); await entering;
  assert.equal(page.mounts.length, 0);
  assert.equal(page.scripts.length, 0);
  assert.equal(page.status.hidden, false);
  assert.match(page.status.textContent, /transition table/);
  await page.hide();
  const retry = page.show(); page.loaded(); await retry;
  assert.equal(page.mounts.length, 1);
  assert.equal(page.status.hidden, true);
});

test("leaving Help while its bundle loads does not mount a hidden diagram", async () => {
  const page = setup(), entering = page.show();
  await page.hide(); page.loaded(); await entering;
  assert.equal(page.mounts.length, 0);
  await page.show();
  assert.equal(page.mounts.length, 1);
  assert.equal(page.scripts.length, 1);
});


test("Help menu has one reachable section for each application tab", () => {
  const html = readFileSync(require.resolve("../src/static/index.html"), "utf8");
  const tabs=tabNames(html),topics=helpSectionNames(html),links=helpLinkNames(html);
  assert.deepEqual(tabs, TABS);
  assert.deepEqual(topics, TABS);
  assert.deepEqual(links, TABS);
  assert.equal(new Set(tabs).size, TABS.length);
  assert.equal(new Set(topics).size, TABS.length);
  assert.match(html, /id="workflowDiagram"/);
  assert.match(html, /id="spotifyRedirect"/);
  assert.match(html, /id="help-language"/);
  assert.match(html, /ORIGINALTITLE/);
  assert.doesNotMatch(html, /id="catalogueLanguagePreview"/,
    "Language controls must not remain embedded in Catalogue");
});


test("Help topic indicator follows direct-link restoration and ignores unknown topics",()=>{
  const page=setup();
  assert.equal(page.topics.filter(t=>t.getAttribute("aria-current")==="location").length,1);
  assert.equal(page.topics[0].getAttribute("aria-current"),"location");
  assert.equal(page.help.selectTopic("album"),true);
  assert.equal(page.topics.find(t=>t.dataset.topic==="album").getAttribute("aria-current"),"location");
  assert.equal(page.topics.filter(t=>t.getAttribute("aria-current")==="location").length,1);
  assert.equal(page.help.selectTopic("unknown-topic"),false);
  assert.equal(page.topics.find(t=>t.dataset.topic==="album").getAttribute("aria-current"),"location");
  const target=page.topics.find(t=>t.dataset.topic==="sources");
  target.listeners.click();
  assert.equal(target.getAttribute("aria-current"),"location");
  assert.equal(page.help.selectTopic("approval"),true);
  assert.equal(page.topics.find(t=>t.dataset.topic==="approval").getAttribute("aria-current"),"location");
  assert.equal(page.topics.filter(t=>t.getAttribute("aria-current")==="location").length,1);
});

test("Help covers the recording-first workflow, safe album overrides and recovery",()=>{
  const html=readFileSync(require.resolve("../src/static/index.html"),"utf8");
  const sections=new Map([...html.matchAll(/^        <section id="help-([a-z]+)" class="help-topic"[\s\S]*?^        <\/section>/gm)]
    .map(match=>[match[1],match[0]]));
  assert.equal(sections.size,TABS.length);
  const essential={
    music:["Recommended recording and album workflow","Approve the recording","Choose the album","browser Back and Forward"],
    approval:["First metadata step","Approve without an album","does not invent an album","Original source metadata"],
    album:["Second metadata step","Select all","different","Validate Album","Undo","locked","duplicate"],
    sources:["Spotify","Archive versus permanent removal","cannot be undone","monitoring"],
    language:["Optional finishing step","three tracks","Navidrome","Reverse"],
    catalogue:["Cache synchronization","Album family cache","Index downloaded recordings"],
    graph:["shareable URL","never changes local files"],
    activity:["Follow latest","failed","retry"],
    maintenance:["Verify library","current items needing attention","Navidrome"],
    help:["Approval → Album","browser Back / Forward","authentication"]
  };
  for(const [topic,terms] of Object.entries(essential)){
    const text=sections.get(topic);
    assert.ok(text,topic+" missing");
    for(const term of terms)assert.ok(text.includes(term),topic+" missing "+term);
  }
  assert.match(sections.get("album"),/possible same-title\/duration local song/);
  assert.match(sections.get("album"),/not automatically considered the same recording/);
  assert.match(sections.get("album"),/one official position cannot receive two local recordings/i);
  assert.match(sections.get("approval"),/A recording-only approval does not invent an album/);
  assert.doesNotMatch(sections.get("help"),/Maintenance 760/);
});
