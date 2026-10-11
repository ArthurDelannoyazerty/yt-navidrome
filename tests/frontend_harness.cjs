const fs = require('node:fs');
const vm = require('node:vm');
const {RequestGate, resultsLabel} = require('../src/static/ui-state.js');
function element(tag = 'div') {
  return {
    tagName: tag, value: '', textContent: '', dataset: {}, disabled: false,
    children: [], listeners: {}, hidden: false, scrollTop: 0, clientHeight: 100,
    scrollHeight: 100, classList: {toggle() {}},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    append(...values) { this.children.push(...values); },
    replaceChildren(...values) { this.children = values; },
    querySelector() { return null; }, contains() { return false; },
    matches() { return false; }, setAttribute() {}, showModal() {}, close() {},
    get firstElementChild() { return this.children[0]; },
    get childElementCount() { return this.children.length; },
  };
}
function browser(spotify = false) {
  const nodes = new Map();
  const requests = [];
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    createElement: element, createTextNode: value => value,
    querySelectorAll: () => [], addEventListener() {}, body: element(),
  };
  const context = vm.createContext({
    document, console, Map, Set, Date, JSON, Number, String, Boolean,
    IngestorUI: {RequestGate, resultsLabel},
    localStorage: {setItem() {}, getItem() { return 'admin'; }},
    window: {getSelection: () => null, addEventListener() {}},
    confirm: () => true, setTimeout, clearTimeout, setInterval() {},
    fetch: (url, options) => new Promise(resolve => {
      requests.push({url, options, resolve: data => resolve({
        ok: true, status: 200, json: async () => data,
      })});
    }),
  });
  const path = require.resolve('../src/static/app.js');
  const source = fs.readFileSync(path, 'utf8');
  // Initialization alone is excluded; all definitions and event handlers run.
  vm.runInContext(source.slice(0, source.lastIndexOf('(async () => {')) +
    '\nglobalThis.app = {state, loadEvents, loadIntegrity, loadIgnored, loadSources, refreshTracks, showDetails};', context);
  if (spotify) vm.runInContext(fs.readFileSync(require.resolve('../src/static/spotify-ui.js'), 'utf8'), context);
  return {context, nodes, requests, app: context.app, spotify: context.window.SpotifyUI, get: id => document.getElementById(id)};
}


module.exports = {browser};
