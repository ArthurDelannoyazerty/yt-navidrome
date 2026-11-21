/* Canonical browser URLs, independent of API routes and page rendering.
   Only committed navigation belongs in history; unsaved decisions stay in memory. */
(function (root) {
  "use strict";

  const pages = Object.freeze({
    music: "music", approval: "approval", catalogue: "catalogue",
    album: "albums", language: "language", graph: "graph", sources: "sources",
    activity: "activity", maintenance: "maintenance", help: "help",
  });
  const kinds = new Set(["artist", "recording", "release", "release-group", "work"]);
  const safe = value => encodeURIComponent(String(value));
  const validId = value => typeof value === "string" && value.length > 0 &&
    value.length <= 200 && value !== "." && value !== ".." && !/[\/\0]/.test(value);
  const decode = value => {
    try { return decodeURIComponent(value); } catch (_) { return null; }
  };
  const validSegment = value => validId(value) && value !== "." && value !== "..";
  const validUser = value => validSegment(value) && value.length <= 64;
  const integer = (value, defaultValue, max) => {
    const parsed = Number(value);
    return Number.isSafeInteger(parsed) && parsed >= 1 && parsed <= max ? parsed : defaultValue;
  };
  const stringParam = (params, name, max) => (params.get(name) || "").slice(0, max);
  function parse(pathname = "/", search = "") {
    if (pathname === "/" || pathname === "") return {user: null, tab: "music", legacy: true};
    const raw = pathname.split("/");
    if (raw[0] !== "" || raw[1] !== "libraries") return null;
    if (raw.at(-1) === "") raw.pop();
    const parts = raw.slice(2).map(decode);
    if (!parts.length || parts.some(part => !validSegment(part))) return null;
    const [user, page, ...rest] = parts;
    if (!validUser(user)) return null;
    const tab = Object.keys(pages).find(key => pages[key] === page);
    if (!tab) return null;
    const query = new URLSearchParams(search);
    const result = {user, tab};
    if (tab === "music") {
      if (rest.length === 1 && rest[0] === "ignored") result.ignored = true;
      else if (rest.length === 1) result.trackId = rest[0];
      else if (rest.length) return null;
      if (query.has("q")) result.q = stringParam(query, "q", 200);
      if (query.has("status")) result.status = stringParam(query, "status", 40);
      if (query.has("page")) result.page = integer(query.get("page"), 1, 100000);
    } else if (tab === "approval") {
      if (rest.length > 1) return null;
      if (rest.length) result.trackId = rest[0];
      if (query.has("status")) result.status = stringParam(query, "status", 40);
      if (query.has("q")) result.q = stringParam(query, "q", 200);
    } else if (tab === "catalogue") {
      if (rest.length && (rest.length !== 2 || !kinds.has(rest[0]))) return null;
      if (rest.length) [result.kind, result.entityId] = rest;
      if (query.has("q")) result.q = stringParam(query, "q", 200);
      if (query.has("kind")) result.searchKind = kinds.has(query.get("kind")) ? query.get("kind") : "recording";
      if (query.get("local") === "1") result.localOnly = true;
    } else if (tab === "album") {
      if (rest.length && rest.length !== 1 && !(rest.length === 3 && rest[1] === "editions"))
        return null;
      if (rest.length) result.familyId = rest[0];
      if (rest.length === 3) result.editionId = rest[2];
      if (query.has("q")) result.q = stringParam(query, "q", 100);
      if (query.get("include_assigned") === "1") result.includeAssigned = true;
    } else if (tab === "sources") {
      if (rest.length > 1) return null;
      if (rest.length) result.sourceId = rest[0];
    } else if (tab === "graph") {
      if (rest.length && (rest.length !== 2 || !kinds.has(rest[0]))) return null;
      if (rest.length) [result.kind, result.entityId] = rest;
      if (query.get("view") === "catalogue") result.view = "catalogue";
      if (query.has("focus")) result.focus = stringParam(query, "focus", 200);
      if (query.has("q")) result.q = stringParam(query, "q", 200);
      if (query.has("kinds")) result.kinds = stringParam(query, "kinds", 200);
      if (query.has("limit")) result.limit = integer(query.get("limit"), 180, 10000);
    } else if (tab === "help") {
      if (rest.length > 1) return null;
      if (rest.length) result.topic = rest[0];
    } else if (rest.length) return null;
    return result;
  }

  function build(route) {
    if (!route || !validUser(route.user) || !Object.hasOwn(pages, route.tab)) return null;
    const base = "/libraries/" + safe(route.user) + "/" + pages[route.tab];
    let suffix = "", query = new URLSearchParams();
    const id = value => validSegment(value) ? "/" + safe(value) : "";
    const add = (key, value, max) => {
      if (typeof value === "string" && value.trim()) query.set(key, value.slice(0, max));
    };
    if (route.tab === "music") {
      suffix = route.ignored ? "/ignored" : id(route.trackId);
      add("q", route.q, 200);
      if (!route.ignored) add("status", route.status && route.status !== "ALL" ? route.status : "", 40);
      if (route.page > 1) query.set("page", String(integer(route.page, 1, 100000)));
    } else if (route.tab === "approval") {
      suffix = id(route.trackId);
      add("q", route.q, 200);
      add("status", route.status && route.status !== "NEEDS_APPROVAL" ? route.status : "", 40);
    } else if (route.tab === "catalogue") {
      if (kinds.has(route.kind) && validSegment(route.entityId))
        suffix = "/" + safe(route.kind) + id(route.entityId);
      add("q", route.q, 200);
      if (kinds.has(route.searchKind) && route.searchKind !== "recording") query.set("kind", route.searchKind);
      if (route.localOnly) query.set("local", "1");
    } else if (route.tab === "album") {
      suffix = id(route.familyId);
      if (suffix && validSegment(route.editionId)) suffix += "/editions" + id(route.editionId);
      add("q", route.q, 100);
      if (route.includeAssigned) query.set("include_assigned", "1");
    } else if (route.tab === "sources") suffix = id(route.sourceId);
    else if (route.tab === "help") suffix = id(route.topic);
    else if (route.tab === "graph") {
      if (kinds.has(route.kind) && validSegment(route.entityId))
        suffix = "/" + safe(route.kind) + id(route.entityId);
      if (route.view === "catalogue") query.set("view", "catalogue");
      add("focus", route.focus, 200); add("q", route.q, 200);
      add("kinds", route.kinds, 200);
      if (route.limit && route.limit !== 180)
        query.set("limit", String(integer(route.limit, 180, 10000)));
    }
    const qs = query.toString();
    return base + suffix + (qs ? "?" + qs : "");
  }

  function read() {
    if (!root.location) return {user: null, tab: "music", legacy: true};
    return parse(root.location.pathname, root.location.search);
  }
  function write(route, {replace = false} = {}) {
    if (!root.history || !root.location) return false;
    const url = build(route);
    if (!url) return false;
    if (root.location.pathname + root.location.search === url) return false;
    root.history[replace ? "replaceState" : "pushState"]({musicRoute: true}, "", url);
    return true;
  }

  const api = Object.freeze({pages, parse, build, read, write});
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.IngestorRouter = api;
})(globalThis);
