"use strict";
// Run the real, dependency-free UI in the Chrome shipped on GitHub's runner.
// API fixtures keep layout and interaction regressions independent of providers.
const test = require("node:test");
const assert = require("node:assert/strict");
const {createServer} = require("node:http");
const {readFileSync, existsSync, mkdtempSync, rmSync, mkdirSync, writeFileSync} = require("node:fs");
const {join, resolve, basename} = require("node:path");
const {tmpdir} = require("node:os");
const {spawn, spawnSync} = require("node:child_process");
const {setTimeout: delay} = require("node:timers/promises");

const root = resolve(__dirname, "..");
const staticRoot = join(root, "src", "static");

function chromeBinary() {
  for (const candidate of [process.env.CHROME_PATH, "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"]) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  for (const name of ["google-chrome", "chromium"]) {
    const found = spawnSync("which", [name], {encoding: "utf8"});
    if (found.status === 0) return found.stdout.trim();
  }
  return null;
}

async function browser(t) {
  const binary = chromeBinary();
  if (!binary) {
    if (process.env.REQUIRE_BROWSER === "1") throw new Error("Chrome is required for browser regression tests");
    t.skip("No local Chrome installed; CI requires the real-browser checks");
    return null;
  }
  const profile = mkdtempSync(join(tmpdir(), "music-browser-"));
  const child = spawn(binary, ["--headless=new", "--no-sandbox", "--disable-gpu",
    "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
    "--remote-debugging-port=0", "--user-data-dir=" + profile, "about:blank"],
  {stdio: ["ignore", "ignore", "pipe"]});
  t.after(async () => {
    child.kill("SIGTERM");
    await Promise.race([new Promise(done => child.once("exit", done)), delay(1500)]);
    if (child.exitCode === null) child.kill("SIGKILL");
    rmSync(profile, {recursive: true, force: true, maxRetries: 3, retryDelay: 100});
  });
  const address = await new Promise((done, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("Chrome did not expose DevTools: " + output)), 15000);
    child.stderr.on("data", chunk => {
      output += chunk;
      const match = output.match(/DevTools listening on (ws:\/\/[^\s]+)/);
      if (match) { clearTimeout(timer); done(new URL(match[1])); }
    });
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.once("exit", code => { clearTimeout(timer); reject(new Error("Chrome exited " + code + ": " + output)); });
  });
  const target = await fetch("http://" + address.host + "/json/new?about:blank", {method: "PUT"}).then(r => r.json());
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, reject) => {
    socket.addEventListener("open", done, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  t.after(() => socket.close());
  let sequence = 0;
  const pending = new Map();
  const errors = [];
  const send = (method, params = {}) => new Promise((done, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error("CDP timeout: " + method)); }, 10000);
    pending.set(id, {done, reject, timeout});
    socket.send(JSON.stringify({id, method, params}));
  });
  socket.addEventListener("message", event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id);
      if (!request) return;
      pending.delete(message.id); clearTimeout(request.timeout);
      if (message.error) request.reject(new Error(message.error.message));
      else request.done(message.result);
    } else if (message.method === "Runtime.exceptionThrown") {
      errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
    } else if (message.method === "Page.javascriptDialogOpening") {
      send("Page.handleJavaScriptDialog", {accept: true}).catch(error => errors.push(error.message));
    }
  });
  await send("Page.enable");
  await send("Runtime.enable");
  const evaluate = async expression => {
    const result = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true, userGesture: true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const until = async (expression, description = expression) => {
    const deadline = Date.now() + 8000;
    do {
      if (await evaluate(expression)) return;
      await delay(50);
    } while (Date.now() < deadline);
    throw new Error("Timed out: " + description);
  };
  const screenshot = async name => {
    mkdirSync(join(root, "validation"), {recursive: true});
    const result = await send("Page.captureScreenshot", {format: "png", captureBeyondViewport: false});
    writeFileSync(join(root, "validation", name + ".png"), Buffer.from(result.data, "base64"));
  };
  return {send, evaluate, until, errors, screenshot};
}

function fixtures() {
  const origin = {id: "origin-1", provider: "youtube", media_key: "abcdefghijk",
    url: "https://www.youtube.com/watch?v=abcdefghijk", title: "誓いの旋律",
    channel: "Kenichiro Suehiro - Topic", downloadable: 1, download_enabled: 1};
  const track = {id: "track-1", user_id: "admin", title: "誓いの旋律", matched_title: null,
    operation_state: "NEEDS_APPROVAL", operation_kind: "INGEST", health: "AVAILABLE",
    current_origin_id: origin.id, current_asset_id: 1, mbid: null, file_path: "/library/admin/song.opus",
    temporary_candidate_sha256: "temporary", issue_count: 0, origins: [origin],
    approval_token: "a".repeat(64), playlists: [{title: "Music Anime", added_at: "2021-04-17T21:36:42Z"}],
    choices: [{kind: "candidate", title: "誓いの旋律", artist: "末廣健一郎",
      mbid: "11111111-1111-4111-8111-111111111111", similarity: 85.7, match_score: 85.7,
      evidence: {duration_delta_seconds: 0.3, source_version_clues: [], version_clues: []}},
    {kind: "source", title: "誓いの旋律", artist: "Kenichiro Suehiro", similarity: 0}]};
  const event = id => ({id, at: "2026-11-05T14:00:00Z", level: "INFO", target: "track-1",
    message: "Fixture event " + id + " — downloaded music remains available while metadata is reviewed."});
  return {tracks: [track], events: Array.from({length: 240}, (_, n) => event(n + 1)), event,
    writes: [], ignored: [{id: "ignored-1", title: "Ignored song", mbid: null,
      origins: [{provider: "youtube", media_key: "lmnopqrstuv", url: "https://youtu.be/lmnopqrstuv", title: "Ignored song"}]}]};
}

async function fixtureServer(t, data) {
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, "http://localhost");
      response.setHeader("Cache-Control", "no-store");
      if (url.pathname === "/" || url.pathname.startsWith("/static/")) {
        const file = url.pathname === "/" ? "index.html" : basename(url.pathname);
        const content = readFileSync(join(staticRoot, file));
        response.setHeader("Content-Type", file.endsWith(".js") ? "text/javascript" : file.endsWith(".css") ? "text/css" : "text/html");
        response.end(file === "index.html" ? content.toString().replaceAll("__STATIC_VERSION__", "browser-test") : content);
        return;
      }
      response.setHeader("Content-Type", "application/json");
      let result;
      if (request.method !== "GET") {
        let body = "";
        for await (const chunk of request) body += chunk;
        data.writes.push({path: url.pathname, method: request.method, body: body ? JSON.parse(body) : null});
        result = {message: "Change queued", queued: 1, skipped: 0, results: []};
      } else if (url.pathname === "/api/users") result = ["admin", "guest"];
      else if (url.pathname === "/api/tracks") result = {tracks: data.tracks, total: data.tracks.length,
        page: 1, limit: 50, stats: {total: data.tracks.length, available: 1, approval: 1, working: 0, deferred: 0, failed: 0, attention: 0}};
      else if (url.pathname === "/api/sources" || url.pathname === "/api/disabled-origins") result = [];
      else if (url.pathname === "/api/ignored") result = data.ignored;
      else if (url.pathname === "/api/events") result = data.events.filter(event => event.id > Number(url.searchParams.get("after") || 0));
      else if (url.pathname === "/api/integrity") result = {page: 1, limit: 50, total: 0, issues: [], kinds: [],
        summary: {total: 0, errors: 0, warnings: 0}, repairable_tracks: 0, running: false, last_run: null};
      else if (url.pathname === "/api/system") result = {schema: 5, beets: "2.14.1",
        downloader: {version: "fixture"}, youtube_circuit: {open: false, failures: 0}};
      else if (url.pathname === "/api/spotify/status") result = {configured: false, connected: false};
      else if (url.pathname === "/api/review/queue") result = {ids: data.tracks.map(track => track.id),
        tracks: data.tracks, total: data.tracks.length, matching_total: data.tracks.length, excluded: 0, truncated: false};
      else if (url.pathname === "/api/musicbrainz/search") result = {entity: "recording", items: [], total: 0, page: 1, limit: 20};
      else if (url.pathname.endsWith("/metadata-context")) {
        const track = data.tracks[0];
        result = {track_id: track.id, user_id: "admin", revision: "b".repeat(64),
          current_title: track.title, current_mbid: null, state: track.operation_state, health: track.health,
          can_correct: true, origin_id: track.current_origin_id, source_url: track.origins[0].url, origin_count: 1,
          source: {tags: {title: [track.title], artist: ["Kenichiro Suehiro"]}, basis: "original_download"},
          current_tags: {title: [track.title], artist: ["Kenichiro Suehiro"]},
          search_hints: {title: track.title, artist: "Kenichiro Suehiro", artist_basis: "topic_channel", notes: [], version_clues: []},
          choices: track.choices.map((choice, index) => ({...choice, index})),
          candidates: track.choices.map((choice, index) => ({...choice, index})),
          approval_token: track.approval_token, latest_identification: null, artist_aliases: [], rejected_candidates: []};
      } else if (url.pathname === "/api/tracks/track-1") result = {...data.tracks[0], assets: [], identity_history: [], integrity_issues: [], file_tags: {tags: {}, issues: []}};
      else if (url.pathname.endsWith("/audio")) { response.statusCode = 404; result = {error: "Fixture has no media"}; }
      else { response.statusCode = 404; result = {error: "Unexpected fixture request: " + url.pathname}; }
      response.end(JSON.stringify(result));
    } catch (error) {
      response.statusCode = 500;
      response.end(JSON.stringify({error: error.message}));
    }
  });
  await new Promise(done => server.listen(0, "127.0.0.1", done));
  t.after(() => { server.closeAllConnections(); return new Promise(done => server.close(done)); });
  return "http://127.0.0.1:" + server.address().port;
}

test("real browser: log following, focused removal, ignored navigation and mobile layout", {timeout: 60000}, async t => {
  const chrome = await browser(t);
  if (!chrome) return;
  const data = fixtures();
  const origin = await fixtureServer(t, data);
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await chrome.send("Page.navigate", {url: origin});
  await chrome.until("document.querySelectorAll('#tracks tr[data-key]').length === 1 && document.querySelector('#logs').children.length === 240");
  assert.equal(await chrome.evaluate("document.querySelector('#notice').hidden || !document.querySelector('#notice').classList.contains('error')"), true);

  // Logs were loaded while display:none: opening Activity must follow the bottom.
  await chrome.evaluate("document.querySelector('[data-tab=activity]').click()");
  await chrome.until("(() => {const b=document.querySelector('#logs');return b.clientHeight > 0 && b.scrollTop > 0 && b.scrollHeight-b.clientHeight-b.scrollTop < 3;})()");
  await chrome.evaluate("document.querySelector('#logs').scrollTop=0");
  await chrome.until("!document.querySelector('#followLogs').checked");
  data.events.push(data.event(241));
  await chrome.evaluate("loadEvents()");
  assert.equal(await chrome.evaluate("document.querySelector('#logs').scrollTop"), 0);
  assert.equal(await chrome.evaluate("document.querySelector('#jumpLatest').hidden"), false);
  await chrome.evaluate("document.querySelector('#jumpLatest').click()");
  await chrome.until("document.querySelector('#followLogs').checked && document.querySelector('#jumpLatest').hidden");
  await chrome.screenshot("activity-following");

  await chrome.evaluate("document.querySelector('[data-tab=music]').click()");
  assert.equal(await chrome.evaluate("document.querySelector('#resultsSummary').classList.contains('sr-only')"), true);
  await chrome.evaluate("[...document.querySelectorAll('#tracks button')].find(b=>b.textContent==='Remove…').click()");
  await chrome.until("document.querySelector('#removeDialog').open");
  assert.deepEqual(await chrome.evaluate("[...document.querySelectorAll('#removeChoices input')].map(i=>i.value)"), ["delete", "delete_ignore_urls"]);
  assert.equal(await chrome.evaluate("[...document.querySelectorAll('#tracks button')].some(b=>b.textContent==='Restore source metadata')"), false);
  await chrome.screenshot("music-removal-desktop");

  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await chrome.evaluate("(() => {const r=document.querySelector('#removeDialog').getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.height<=innerHeight;})()"), true);
  await chrome.screenshot("music-removal-mobile");
  await chrome.evaluate("document.querySelector('#cancelRemoval').click()");
  await chrome.evaluate("document.querySelector('#stats [data-filter=IGNORED]').click()");
  await chrome.until("!document.querySelector('#ignoredMusicPanel').hidden && document.querySelector('#ignored').textContent.includes('Ignored song')");
  assert.equal(await chrome.evaluate("document.querySelector('#ignoredMusicPanel').closest('[data-panel]').dataset.panel"), "music");
  await chrome.screenshot("ignored-music-mobile");
  assert.deepEqual(chrome.errors, []);
});
