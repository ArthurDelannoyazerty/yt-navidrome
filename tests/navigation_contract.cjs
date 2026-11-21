"use strict";

// Shared interaction contract: navigation and Help always describe the same
// user workflow. Routes retain their existing internal names (for example,
// the tab is "sources" and its URL is "/sources").
const TABS = Object.freeze([
  "music", "approval", "album", "sources", "language",
  "catalogue", "graph", "activity", "maintenance", "help",
]);
const helpPaths = user => TABS.map(topic => "/libraries/" + encodeURIComponent(user) + "/help/" + topic);
function tabNames(html) {
  return [...html.matchAll(/<a class="tab(?: active)?" data-tab="([^"]+)"/g)]
    .map(match => match[1]);
}
function helpSectionNames(html) {
  return [...html.matchAll(/<section id="help-([^"]+)" class="help-topic"/g)]
    .map(match => match[1]);
}
function helpLinkNames(html) {
  return [...html.matchAll(/<a href="#help-([^"]+)">/g)]
    .map(match => match[1]);
}
module.exports = {TABS, helpPaths, tabNames, helpSectionNames, helpLinkNames};
