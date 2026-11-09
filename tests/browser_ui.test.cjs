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
  {stdio: ["ignore", "ignore", "pipe"], detached: process.platform !== "win32"});
  t.after(() => {
    // Chrome forks subprocesses that can keep stderr open after its parent exits.
    // Close the entire process group so a failed assertion cannot strand CI.
    try {
      if (process.platform !== "win32") process.kill(-child.pid, "SIGKILL");
      else child.kill("SIGKILL");
    } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
    child.stderr.destroy();
    child.unref();
    rmSync(profile, {recursive: true, force: true, maxRetries: 5, retryDelay: 100});
  });
  const address = await new Promise((done, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("Chrome did not expose DevTools: " + output)), 30000);
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
    writes: [], disabled: [], ignored: [{id: "ignored-1", title: "Ignored song", mbid: null,
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
        const payload = body ? JSON.parse(body) : null;
        data.writes.push({path: url.pathname, method: request.method, body: payload});
        result = {message: "Change queued", queued: 1, skipped: 0, results: []};
        const parts = url.pathname.split("/");
        const current = data.tracks.find(track => track.id === parts[3]);
        if (url.pathname.endsWith("/artwork") && data.artwork) {
          data.artwork.preference = payload.selection === "auto"
            ? {mode: "auto", candidate_id: "cover-square", framing: "original"}
            : payload.selection ? {mode: "manual", candidate_id: payload.selection, framing: payload.framing}
            : data.artwork.preference;
          data.artwork.revision = String(data.writes.length % 10).repeat(64);
        } else if (url.pathname === "/api/review/approve") {
          result.results = (payload.items || []).map(item => ({track_id: item.track_id, status: "queued"}));
          result.queued = result.results.length;
          for (const item of result.results) data.tracks.find(track => track.id === item.track_id).operation_state = "QUEUED";
        } else if (current && url.pathname.endsWith("/action")) {
          current.operation_state = "QUEUED";
        } else if (current && url.pathname.endsWith("/disable")) {
          const origin = current.origins.find(origin => origin.id === parts[5]);
          origin.download_enabled = 0; origin.disabled_reason = "Disabled by user";
          current.operation_state = "STOPPED"; current.can_stop_download = false;
          data.disabled = [{...origin, track_id: current.id, track_title: current.title,
            user_id: current.user_id, operation_state: "STOPPED"}];
          result = {message: "URL disabled; audio and playlists retained", cancelled: true, cancellation_pending: false};
        } else if (current && url.pathname.endsWith("/enable")) {
          current.origins.find(origin => origin.id === parts[5]).download_enabled = 1;
          data.disabled = data.disabled.filter(origin => origin.id !== parts[5]);
        } else if (current && url.pathname.endsWith("/replacement")) {
          current.operation_state = "QUEUED";
        }
      } else if (url.pathname === "/api/users") result = ["admin", "guest"];
      else if (url.pathname === "/api/tracks") result = {tracks: data.tracks, total: data.tracks.length,
        page: 1, limit: 50, stats: {total: data.tracks.length, available: 1, approval: 1, working: 0, deferred: 0, failed: 0, attention: 0}};
      else if (url.pathname.endsWith("/artwork") && data.artwork) result = data.artwork;
      else if (/\/artwork\/[^/]+\/image$/.test(url.pathname)) {
        response.setHeader("Content-Type", "image/png");
        response.end(Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=", "base64"));
        return;
      }
      else if (url.pathname === "/api/sources") result = [];
      else if (url.pathname === "/api/disabled-origins") result = data.disabled;
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
        const track = data.tracks.find(track => track.id === url.pathname.split("/")[3]) || data.tracks[0];
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
      else if (url.pathname.endsWith("/audio")) {
        // A short silent PCM fixture exercises the real native player without a provider.
        const wav = Buffer.alloc(16044);
        wav.write("RIFF", 0); wav.writeUInt32LE(wav.length - 8, 4); wav.write("WAVEfmt ", 8);
        wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
        wav.writeUInt32LE(8000, 24); wav.writeUInt32LE(16000, 28);
        wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34);
        wav.write("data", 36); wav.writeUInt32LE(16000, 40);
        response.setHeader("Content-Type", "audio/wav");
        response.end(wav); return;
      }
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


test("real browser: stop the active source and queue another URL without removing music", {timeout: 60000}, async t => {
  const chrome = await browser(t);
  if (!chrome) return;
  const data = fixtures(), track = data.tracks[0];
  track.operation_state = "DEFERRED"; track.temporary_candidate_sha256 = null;
  track.origins.push({...track.origins[0], id: "origin-2", media_key: "lmnopqrstuv",
    title: "Restricted alternative", url: "https://www.youtube.com/watch?v=lmnopqrstuv"});
  track.active_origin_id = "origin-2"; track.can_stop_download = true; track.job_stage = "downloading";
  const origin = await fixtureServer(t, data);
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await chrome.send("Page.navigate", {url: origin});
  await chrome.until("document.querySelector('.stop-download') && !document.querySelector('.stop-download').disabled");
  await chrome.evaluate("document.querySelector('.stop-download').click()");
  await chrome.until("document.querySelector('#tracks').textContent.includes('Downloads stopped')");
  assert.equal(data.writes[0].path, "/api/tracks/track-1/origins/origin-2/disable");
  assert.equal(track.origins[0].download_enabled, 1);
  assert.equal(track.origins[1].download_enabled, 0);
  assert.equal(track.current_asset_id, 1);
  assert.equal(track.playlists.length, 1);

  await chrome.evaluate("document.querySelector('.replace-source[data-track-id=\"track-1\"]').click()");
  await chrome.until("document.querySelector('#replacementDialog').open");
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await chrome.evaluate("(() => {const r=document.querySelector('#replacementDialog').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth;})()"), true);
  await chrome.screenshot("replacement-source-mobile");
  await chrome.evaluate("document.querySelector('#replacementUrl').value='https://www.youtube.com/watch?v=ZYXWVUTSRQP'; document.querySelector('#replacementForm').requestSubmit(); document.querySelector('#replacementForm').requestSubmit()");
  await chrome.until("!document.querySelector('#replacementDialog').open");
  const replacements = data.writes.filter(write => write.path.endsWith("/replacement"));
  assert.equal(replacements.length, 1);
  assert.deepEqual(replacements[0].body, {user_id: "admin", url: "https://www.youtube.com/watch?v=ZYXWVUTSRQP"});
  assert.equal(track.origins[1].download_enabled, 0);
  await chrome.evaluate("document.querySelector('#stats [data-filter=IGNORED]').click()");
  await chrome.until("document.querySelector('#disabledSources').textContent.includes('Disabled by user')");
  assert.equal(await chrome.evaluate("document.querySelector('#disabledSources').closest('[data-panel]').dataset.panel"), "music");
  assert.deepEqual(chrome.errors, []);
});

test("real browser: continuous review saves, resumes and approves only the explicit selection", {timeout: 60000}, async t => {
  const chrome = await browser(t);
  if (!chrome) return;
  const data = fixtures(), next = structuredClone(data.tracks[0]);
  next.id = "track-2"; next.title = "第二の曲"; next.approval_token = "c".repeat(64);
  next.origins[0].id = "origin-2"; next.current_origin_id = "origin-2";
  next.choices.forEach(choice => { choice.title = next.title; });
  data.tracks.push(next);
  const origin = await fixtureServer(t, data);
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false});
  await chrome.send("Page.navigate", {url: origin});
  await chrome.until("document.querySelectorAll('#tracks tr[data-key]').length===2");
  await chrome.evaluate("document.querySelector('#startReview').click()");
  await chrome.until("document.querySelector('#metadataDialog').open && document.querySelector('#metadataQueueProgress').textContent==='Track 1 of 2' && !document.querySelector('#metadataSaveNext').disabled");
  assert.equal(await chrome.evaluate("document.querySelector('#metadataAudio').src.includes('candidate=true') && document.querySelector('#metadataAudio').src.includes('approval_token=')"), true);
  assert.equal(await chrome.evaluate("document.querySelector('#metadataProposedValues').textContent.includes('末廣健一郎')"), true);
  await chrome.screenshot("metadata-review-desktop");

  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await chrome.evaluate("(() => {const d=document.querySelector('#metadataDialog'),r=d.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && d.scrollWidth<=d.clientWidth+1;})()"), true);
  await chrome.evaluate("document.querySelector('#metadataSaveNext').scrollIntoView({block:'nearest'})");
  assert.equal(await chrome.evaluate("(() => {const r=document.querySelector('#metadataSaveNext').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;})()"), true);
  await chrome.screenshot("metadata-review-mobile");
  await chrome.evaluate("document.querySelector('#metadataSaveNext').click(); document.querySelector('#metadataSaveNext').click()");
  await chrome.until("document.querySelector('#metadataQueueProgress').textContent==='Track 2 of 2' && document.querySelector('#metadataCurrent').textContent==='第二の曲'");
  const saves = data.writes.filter(write => write.path.endsWith("/action"));
  assert.equal(saves.length, 1);
  assert.deepEqual(saves[0].body, {user_id: "admin", mode: "approve", index: 0, approval_token: "a".repeat(64)});

  await chrome.evaluate("document.querySelector('#metadataClose').click()");
  await chrome.until("!document.querySelector('#metadataDialog').open && document.querySelector('#startReview').textContent.includes('Resume')");
  await chrome.evaluate("document.querySelector('#startReview').click()");
  await chrome.until("document.querySelector('#metadataCurrent').textContent==='第二の曲' && !document.querySelector('#metadataSaveNext').disabled");
  await chrome.evaluate("document.querySelector('#metadataQueueSkip').click()");
  await chrome.until("!document.querySelector('#metadataQueueComplete').hidden");
  assert.equal(await chrome.evaluate("document.querySelector('#metadataQueueSummary').textContent.includes('1 track left unchanged')"), true);
  await chrome.evaluate("document.querySelector('#metadataReviewSkipped').click()");
  await chrome.until("document.querySelector('#metadataQueueProgress').textContent==='Track 1 of 1' && document.querySelector('#metadataCurrent').textContent==='第二の曲'");
  await chrome.evaluate("document.querySelector('#metadataClose').click()");
  await chrome.evaluate("document.querySelector('#selectReviewPage').click()");
  await chrome.until("document.querySelector('#approveSelected').textContent==='Approve selected (1)'");
  await chrome.evaluate("document.querySelector('#approveSelected').click()");
  await chrome.until("document.querySelector('#approveSelected').textContent==='Approve selected (0)'");
  const batches = data.writes.filter(write => write.path === "/api/review/approve");
  assert.equal(batches.length, 1);
  assert.deepEqual(batches[0].body, {user_id: "admin", items: [{track_id: "track-2", index: 0, approval_token: "c".repeat(64)}]});
  assert.deepEqual(chrome.errors, []);
});


test("real browser: per-track cover comparison, pin, auto-selection and mobile layout", {timeout: 60000}, async t => {
  const chrome = await browser(t);
  if (!chrome) return;
  const data = fixtures();
  const image = id => `/api/tracks/track-1/artwork/${id}/image?user_id=admin`;
  data.artwork = {track_id: "track-1", title: "Artwork review fixture", revision: "d".repeat(64),
    preference: null, can_edit: true, working: false, current_image_url: image("current"), items: [
      {id: "cover-square", label: "Square source cover", source_type: "youtube", width: 600, height: 600,
        recommended_framing: "original", explanation: "Square artwork; 600 x 600", image_url: image("cover-square")},
      {id: "cover-wide", label: "Landscape source cover", source_type: "youtube", width: 1280, height: 720,
        recommended_framing: "original", explanation: "Original proportions; 1280 x 720", image_url: image("cover-wide")},
    ]};
  const origin = await fixtureServer(t, data);
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 1000, deviceScaleFactor: 1, mobile: false});
  await chrome.send("Page.navigate", {url: origin});
  await chrome.until("document.querySelectorAll('#tracks tr[data-key]').length===1 && window.ArtworkUI");
  await chrome.evaluate("document.querySelector('#tracks .action-menu summary').click(); [...document.querySelectorAll('#tracks button')].find(b=>b.textContent==='Artwork...').click()");
  await chrome.until("document.querySelector('#artworkDialog').open && document.querySelectorAll('#artworkCandidates input').length===2");
  assert.equal(await chrome.evaluate("document.querySelector('#artworkApply').disabled"), true);
  await chrome.evaluate("document.querySelector('#artworkCandidates input[value=cover-wide]').click(); document.querySelector('#artworkFraming').value='fit'; document.querySelector('#artworkFraming').dispatchEvent(new Event('change'))");
  assert.equal(await chrome.evaluate("document.querySelector('#artworkPreview').src.includes('framing=fit')"), true);
  await chrome.screenshot("artwork-gallery-desktop");
  await chrome.evaluate("document.querySelector('#artworkApply').click(); document.querySelector('#artworkApply').click()");
  await chrome.until("document.querySelector('#artworkMode').textContent.includes('Pinned') && !document.querySelector('#artworkAuto').disabled");
  const saves = data.writes.filter(write => write.path.endsWith('/artwork'));
  assert.equal(saves.length, 1); assert.equal(saves[0].body.selection, "cover-wide"); assert.equal(saves[0].body.framing, "fit");
  assert.equal(data.tracks[0].operation_state, "NEEDS_APPROVAL");
  await chrome.evaluate("document.querySelector('#artworkAuto').click()");
  await chrome.until("document.querySelector('#artworkMode').textContent.includes('Automatic') && !document.querySelector('#artworkRefresh').disabled");
  assert.equal(data.writes.at(-1).body.selection, "auto");
  await chrome.evaluate("window.ArtworkUI.open({id:'track-1',title:'Fixture'})");
  await chrome.until("document.querySelector('#artworkDialog').open && document.querySelectorAll('#artworkCandidates input').length===2 && !document.querySelector('#artworkRefresh').disabled");
  await chrome.send("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await chrome.evaluate("(() => {const d=document.querySelector('#artworkDialog'),r=d.getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && d.scrollWidth<=d.clientWidth+1;})()"), true);
  await chrome.screenshot("artwork-gallery-mobile");
  await chrome.evaluate("document.querySelector('#artworkClose').click()");
  await chrome.until("!document.querySelector('#artworkDialog').open");
  assert.deepEqual(chrome.errors, []);
});
