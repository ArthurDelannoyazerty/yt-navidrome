"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");

const renderer = vm.createContext({});
vm.runInContext(readFileSync(require.resolve("../src/static/pele-flowchart.js"), "utf8"), renderer);

function setup() {
  const panel = {hidden: true}, container = {}, status = {}, scripts = [], mounts = [], windowEvents = {};
  const controls = new Map(["workflowZoom", "workflowZoomIn", "workflowZoomOut", "workflowZoomReset"].map(id =>
    [id, {listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }}]));
  let changed;
  const window = {addEventListener: (name, fn) => { windowEvents[name] = fn; }};
  const document = {
    currentScript: {src: "https://ingestor.example/static/help-ui.js?v=content-hash"},
    importNode: node => node,
    querySelector: () => panel,
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
    {window, document, URL, MutationObserver, DOMParser});
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
  return {panel, status, scripts, mounts, loaded, windowEvents,
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
