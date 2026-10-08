"use strict";

const $ = id => document.getElementById(id);
const FAST_POLL_MS = 2500;
const SLOW_POLL_MS = 10000;

const state = {
  user: "",
  tab: "music",
  page: 1,
  maxPage: 1,
  after: 0,
  generation: 0,
  sources: [],
  events: [],
  tracks: new Map(),
  approvals: new Map(),
  fastPolling: false,
  slowPolling: false,
  systemSignature: "",
};

const node = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
};

function notice(message, error = false) {
  const element = $("notice");
  element.hidden = false;
  element.textContent = message;
  element.className = error ? "error" : "success";
}

async function api(path, body, method) {
  const requestMethod = method || (body ? "POST" : "GET");
  const response = await fetch(path, {
    method: requestMethod,
    cache: requestMethod === "GET" ? "no-store" : "default",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({
    error: `Invalid response (HTTP ${response.status})`,
  }));
  if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

const guard = fn => async (...args) => {
  try {
    await fn(...args);
  } catch (error) {
    notice(error?.message || String(error), true);
  }
};

function button(text, fn, cls = "secondary") {
  const element = node("button", text, cls);
  element.type = "button";
  element.addEventListener("click", guard(async () => {
    element.disabled = true;
    try { await fn(); } finally { element.disabled = false; }
  }));
  return element;
}

function badge(value) {
  return node("span", String(value).replaceAll("_", " "), `badge ${value}`);
}

function time(value) {
  return value ? value.replace("T", " ").replace("Z", " UTC") : "Unknown";
}

function stable(value) {
  return JSON.stringify(value);
}

function selectionTouches(element) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return false;
  for (let index = 0; index < selection.rangeCount; index += 1) {
    try {
      if (selection.getRangeAt(index).intersectsNode(element)) return true;
    } catch (_) {}
  }
  return false;
}

function elementIsBusy(element) {
  const active = document.activeElement;
  return Boolean(
    (active && active !== document.body && element.contains(active))
    || selectionTouches(element)
    || element.querySelector("details[open]")
    || element.matches("dialog[open]"),
  );
}

function updateKeyedChildren(container, items, {key, signature, render, background = false, empty}) {
  const existing = new Map(
    [...container.children]
      .filter(child => child.dataset?.key)
      .map(child => [child.dataset.key, child]),
  );
  let cursor = container.firstElementChild;
  const retained = new Set();

  for (const item of items) {
    const itemKey = String(key(item));
    const itemSignature = signature(item);
    const current = existing.get(itemKey);
    let desired = current;
    if (!current || current.dataset.signature !== itemSignature) {
      if (!(background && current && elementIsBusy(current))) {
        desired = render(item);
        desired.dataset.key = itemKey;
        desired.dataset.signature = itemSignature;
        if (current) {
          current.replaceWith(desired);
          // If the replaced node was our insertion cursor, the old node is now
          // detached. Point at the replacement before the next insertBefore().
          if (cursor === current) cursor = desired;
        }
      }
    }
    if (!desired) continue;
    retained.add(itemKey);
    if (!(background && elementIsBusy(desired)) && desired !== cursor) {
      container.insertBefore(desired, cursor);
    }
    cursor = desired.nextElementSibling;
  }

  for (const [itemKey, element] of existing) {
    if (!retained.has(itemKey) && !(background && elementIsBusy(element))) element.remove();
  }

  const oldEmpty = container.querySelector(":scope > [data-empty='true']");
  if (!items.length && empty) {
    if (!oldEmpty) {
      const emptyNode = empty();
      emptyNode.dataset.empty = "true";
      container.append(emptyNode);
    }
  } else if (oldEmpty && !(background && elementIsBusy(oldEmpty))) {
    oldEmpty.remove();
  }
}

function setTab(name) {
  state.tab = name;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name === "maintenance") guard(() => refreshMaintenance(false))();
}

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => setTab(tab.dataset.tab));
}

async function loadUsers(preferred) {
  const users = await api("/api/users");
  const select = $("user");
  select.replaceChildren(...users.map(user => {
    const option = node("option", user);
    option.value = user;
    return option;
  }));
  select.value = users.includes(preferred)
    ? preferred
    : (users.includes("admin") ? "admin" : users[0]);
  changeUser();
}

function changeUser() {
  state.user = $("user").value;
  localStorage.setItem("music-user", state.user);
  state.page = 1;
  state.after = 0;
  state.generation += 1;
  state.events = [];
  state.tracks.clear();
  state.approvals.clear();
  state.systemSignature = "";
  $("logs").replaceChildren();
  $("tracks").replaceChildren();
  $("sources").replaceChildren();
  guard(() => refreshAll(false))();
}

$("user").addEventListener("change", changeUser);

$("userForm").addEventListener("submit", guard(async event => {
  event.preventDefault();
  const name = $("newUser").value.trim();
  await api("/api/users", {name});
  $("newUser").value = "";
  await loadUsers(name);
}));

$("ingestForm").addEventListener("submit", guard(async event => {
  event.preventDefault();
  const urls = $("urls").value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (!urls.length) return;
  await api("/api/sources", {
    user_id: state.user,
    urls,
    monitored: $("monitor").checked,
  });
  $("urls").value = "";
  notice("Sources queued. Existing origins will not be downloaded twice.");
  await refreshAll(false);
}));

function renderSource(source) {
  const row = node("div", null, "source");
  const content = node("div", null, "source-details");
  content.append(
    node("strong", source.title),
    node("p", source.url, "url"),
    badge(source.status),
    node("small", source.monitored ? " Monitored" : " One-off"),
  );
  if (source.synced_at) content.append(node("p", `Last sync: ${time(source.synced_at)}`, "hint"));
  if (source.error) content.append(node("div", source.error, "error-text"));
  const controls = node("div", null, "source-buttons");
  if (source.provider !== "legacy") {
    controls.append(button("Sync / retry", async () => {
      await api(`/api/sources/${encodeURIComponent(source.id)}/retry`, {user_id: state.user});
      notice("Source sync queued");
      await loadSources(state.generation, false);
    }));
  }
  controls.append(button("Remove", async () => {
    if (!confirm("Remove this source and exported playlist? Music files are retained.")) return;
    await api(`/api/sources/${encodeURIComponent(source.id)}?user_id=${encodeURIComponent(state.user)}`, null, "DELETE");
    await loadSources(state.generation, false);
  }, "danger"));
  row.append(content, controls);
  return row;
}

async function loadSources(generation, background = false) {
  const sources = await api(`/api/sources?user_id=${encodeURIComponent(state.user)}`);
  if (generation !== state.generation) return;
  state.sources = sources;
  updateKeyedChildren($("sources"), sources, {
    key: source => source.id,
    signature: stable,
    render: renderSource,
    background,
    empty: () => node("p", "No sources yet.", "empty"),
  });
}

async function action(track, mode, extra = {}) {
  const result = await api(`/api/tracks/${encodeURIComponent(track.id)}/action`, {
    user_id: state.user,
    mode,
    ...extra,
  });
  notice(result.message);
  state.approvals.delete(track.id);
  await refreshTracks(false);
}

function openOperation(track, mode) {
  const origins = track.origins.filter(origin => origin.downloadable);
  if (!origins.length) {
    notice("This music has no downloadable origin.", true);
    return;
  }
  $("operationTrackId").value = track.id;
  $("operationMode").value = mode;
  $("operationTrack").textContent = track.matched_title || track.title;
  $("operationTitle").textContent = mode === "redownload" ? "Redownload audio" : "Reprocess music";
  $("operationSubmit").textContent = mode === "redownload" ? "Redownload" : "Reprocess";
  $("operationHelp").textContent = mode === "redownload"
    ? "Downloads fresh audio and requires it to strongly match the current confirmed recording. The current file remains active until the replacement succeeds."
    : "Downloads fresh audio, fingerprints and identifies it again, then refreshes metadata, artwork, lyrics and ReplayGain. Identity changes require approval and the previous identity is kept in history.";
  const select = $("operationOrigin");
  select.replaceChildren(...origins.map(origin => {
    const option = node("option", `${origin.provider} — ${origin.title}`);
    option.value = origin.id;
    if (origin.id === track.current_origin_id) option.selected = true;
    return option;
  }));
  $("operationDialog").showModal();
}

$("cancelOperation").addEventListener("click", () => $("operationDialog").close());
$("operationForm").addEventListener("submit", guard(async event => {
  event.preventDefault();
  const track = state.tracks.get($("operationTrackId").value);
  const mode = $("operationMode").value;
  await action(track, mode, {origin_id: $("operationOrigin").value});
  $("operationDialog").close();
}));

function showEditor(track) {
  $("editForm").reset();
  $("editId").value = track.id;
  $("editTitle").textContent = track.matched_title || track.title;
  $("editDialog").showModal();
}

$("cancelEdit").addEventListener("click", () => $("editDialog").close());
$("editForm").addEventListener("submit", guard(async event => {
  event.preventDefault();
  const overrides = {};
  for (const [field, id] of Object.entries({
    mbid: "editMbid", release_id: "editRelease", artist: "editArtist",
    title: "editSong", album: "editAlbum",
  })) {
    const value = $(id).value.trim();
    if (value) overrides[field] = value;
  }
  if (!Object.keys(overrides).length) throw new Error("Enter at least one metadata change.");
  await action({id: $("editId").value}, "retag", {overrides});
  $("editDialog").close();
}));

function musicbrainzLink(kind, id) {
  const link = node("a", `${kind} ${id.slice(0, 8)}`);
  link.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

async function showDetails(track) {
  const details = await api(`/api/tracks/${encodeURIComponent(track.id)}?user_id=${encodeURIComponent(state.user)}`);
  $("detailsTitle").textContent = details.matched_title || details.title;
  const body = $("detailsBody");
  const grid = node("div", null, "details-grid");

  const origins = node("div", null, "details-block");
  origins.append(node("h3", "Origins"));
  const originList = node("div", null, "details-list");
  for (const origin of details.origins) {
    const entry = node("div", null, "details-entry");
    entry.append(node("strong", origin.provider), node("div", origin.title), node("small", origin.url));
    if (origin.last_error) entry.append(node("div", origin.last_error, "error-text"));
    originList.append(entry);
  }
  origins.append(originList);

  const assets = node("div", null, "details-block");
  assets.append(node("h3", "Asset history"));
  const assetList = node("div", null, "details-list");
  for (const asset of details.assets) {
    const entry = node("div", null, "details-entry");
    entry.append(
      badge(asset.state),
      node("div", asset.path),
      node("small", `${asset.downloader_name || "unknown"} ${asset.downloader_version || ""} · ${asset.size_bytes || 0} bytes`),
    );
    assetList.append(entry);
  }
  assets.append(assetList);

  const history = node("div", null, "details-block");
  history.append(node("h3", "Identity history"));
  const historyList = node("div", null, "details-list");
  for (const item of details.identity_history) {
    historyList.append(node("div", `${item.previous_title || item.previous_mbid} → ${item.new_title || item.new_mbid} (${time(item.changed_at)})`, "details-entry"));
  }
  if (!details.identity_history.length) historyList.append(node("p", "No identity changes.", "empty"));
  history.append(historyList);

  const technical = node("div", null, "details-block");
  technical.append(
    node("h3", "Technical"),
    node("div", `Discovery: ${time(details.discovered_at)} (${details.discovery_basis})`),
    node("div", `Health: ${details.health}`),
    node("div", `Beets ID: ${details.beets_id ?? "none"}`),
    node("div", `MusicBrainz: ${details.mbid || "none"}`),
  );

  grid.append(origins, assets, history, technical);
  body.replaceChildren(grid);
  $("detailsDialog").showModal();
}

$("closeDetails").addEventListener("click", () => $("detailsDialog").close());

function renderTrack(track) {
  state.tracks.set(track.id, track);
  const row = node("tr");
  const music = node("td");
  const playlists = node("td");
  const discovery = node("td");
  const health = node("td");
  const actions = node("td");

  music.append(node("div", track.matched_title || track.title, "metadata"));
  if (track.matched_title && track.matched_title !== track.title) music.append(node("div", track.title, "source-title"));
  if (track.mbid) music.append(musicbrainzLink("recording", track.mbid));
  if (track.release_id) music.append(node("br"), musicbrainzLink("release", track.release_id));
  music.append(node("div", `${track.origins.length} origin${track.origins.length === 1 ? "" : "s"}`, "hint"));

  if (track.playlists.length) {
    for (const playlist of track.playlists) {
      const line = node("div", playlist.title);
      line.title = `Added: ${time(playlist.added_at)}`;
      playlists.append(line);
    }
  } else playlists.textContent = "No playlist";

  discovery.append(node("div", time(track.discovered_at)), node("small", track.discovery_basis));
  health.append(badge(track.health));
  if (track.operation_state !== "IDLE") health.append(badge(track.operation_state));
  if (track.operation_kind) health.append(node("div", track.operation_kind, "hint"));
  if (track.issue_count) health.append(node("div", `${track.issue_count} integrity issue${track.issue_count === 1 ? "" : "s"}`, "issue-link"));
  if (track.operation_error) {
    const details = node("details", null, "error-text");
    details.append(node("summary", "Last operation error"), node("div", track.operation_error));
    health.append(details);
  }

  const controls = node("div", null, "actions");
  const busy = ["QUEUED", "RUNNING"].includes(track.operation_state);
  if (track.operation_state === "NEEDS_APPROVAL") {
    const select = node("select", null, "approval");
    track.choices.forEach((choice, index) => {
      const option = node("option", `${choice.similarity}% | ${choice.artist ? `${choice.artist} - ` : ""}${choice.title}${choice.description ? ` | ${choice.description}` : ""}`);
      option.value = String(index);
      select.append(option);
    });
    select.value = state.approvals.get(track.id) ?? "0";
    select.addEventListener("change", () => state.approvals.set(track.id, select.value));
    controls.append(select, button("Approve", () => action(track, "approve", {index: Number(select.value)})));
  } else {
    const reprocess = button("Reprocess", () => openOperation(track, "reprocess"));
    const redownload = button("Redownload", () => openOperation(track, "redownload"));
    reprocess.disabled = busy || !track.origins.some(origin => origin.downloadable);
    redownload.disabled = busy || !track.origins.some(origin => origin.downloadable);
    controls.append(reprocess, redownload);
  }
  if (track.operation_state === "FAILED") controls.append(button("Retry", () => action(track, "retry")));

  const menu = node("details", null, "action-menu");
  menu.append(node("summary", "More"));
  const menuItems = node("div", null, "action-menu-items");
  menuItems.append(
    button("Edit metadata", () => showEditor(track)),
    button("Technical details", () => showDetails(track)),
    button("Delete local copy", async () => {
      if (!confirm(`Delete "${track.matched_title || track.title}" for ${state.user}? It can return on the next monitored sync.`)) return;
      await action(track, "delete");
    }, "danger"),
    button("Delete and ignore", async () => {
      if (!confirm(`Delete and ignore "${track.matched_title || track.title}" for ${state.user}? The confirmed recording and known origins will not be imported again until restored.`)) return;
      await action(track, "delete_ignore");
    }, "danger"),
  );
  for (const child of menuItems.children) child.disabled = busy;
  menu.append(menuItems);
  controls.append(menu);
  actions.append(controls);
  row.append(music, playlists, discovery, health, actions);
  return row;
}

function trackSignature(track) {
  return stable({
    title: track.title,
    matched_title: track.matched_title,
    mbid: track.mbid,
    release_id: track.release_id,
    origins: track.origins,
    playlists: track.playlists,
    discovered_at: track.discovered_at,
    discovery_basis: track.discovery_basis,
    health: track.health,
    operation_state: track.operation_state,
    operation_kind: track.operation_kind,
    operation_error: track.operation_error,
    choices: track.choices,
    issue_count: track.issue_count,
  });
}

function updateStats(stats) {
  const values = [
    ["Total", stats.total],
    ["Available", stats.available],
    ["Working", stats.working],
    ["Approval", stats.approval],
    ["Attention", stats.attention],
  ];
  updateKeyedChildren($("stats"), values, {
    key: value => value[0],
    signature: value => value.join(":"),
    render: ([label, count]) => {
      const element = node("span", null, "stat");
      element.append(node("strong", count), document.createTextNode(label));
      return element;
    },
    background: true,
  });
}

async function refreshTracks(background = true) {
  const generation = state.generation;
  const data = await api(
    `/api/tracks?user_id=${encodeURIComponent(state.user)}`
    + `&page=${state.page}&limit=50`
    + `&status=${encodeURIComponent($("filter").value)}`
    + `&q=${encodeURIComponent($("search").value.trim())}`,
  );
  if (generation !== state.generation) return;
  state.maxPage = Math.max(1, Math.ceil(data.total / data.limit));
  if (state.page > state.maxPage) {
    state.page = state.maxPage;
    return refreshTracks(background);
  }
  $("page").textContent = `${data.page} / ${state.maxPage} (${data.total})`;
  $("prev").disabled = state.page <= 1;
  $("next").disabled = state.page >= state.maxPage;
  updateStats(data.stats);
  const ids = new Set(data.tracks.map(track => track.id));
  for (const id of state.tracks.keys()) if (!ids.has(id)) state.tracks.delete(id);
  updateKeyedChildren($("tracks"), data.tracks, {
    key: track => track.id,
    signature: trackSignature,
    render: renderTrack,
    background,
    empty: () => {
      const row = node("tr");
      const cell = node("td", "No music in this view.", "empty");
      cell.colSpan = 5;
      row.append(cell);
      return row;
    },
  });
}

function eventNode(event) {
  return node("div", `${event.at} ${event.level}${event.target ? ` [${event.target.slice(0, 12)}]` : ""} ${event.message}`, `log-${event.level}`);
}

function eventVisible(event) {
  return !$("errorsOnly").checked || ["WARNING", "ERROR", "CRITICAL"].includes(event.level);
}

function renderEvents() {
  const box = $("logs");
  box.replaceChildren(...state.events.filter(eventVisible).map(eventNode));
  box.scrollTop = box.scrollHeight;
}

async function loadEvents() {
  const events = await api(`/api/events?user_id=${encodeURIComponent(state.user)}&after=${state.after}`);
  if (!events.length) return;
  state.after = events.at(-1).id;
  state.events.push(...events);
  state.events = state.events.slice(-2000);
  const box = $("logs");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  for (const event of events) if (eventVisible(event)) box.append(eventNode(event));
  if (!selectionTouches(box)) while (box.childElementCount > 2000) box.firstElementChild?.remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

$("clearLogs").addEventListener("click", () => {
  state.events = [];
  $("logs").replaceChildren();
  notice("Displayed logs cleared. Stored server events were not deleted.");
});
$("errorsOnly").addEventListener("change", renderEvents);

async function loadSystem(background = true) {
  const data = await api("/api/system");
  const signature = stable(data);
  if (signature === state.systemSignature) return;
  if (background && elementIsBusy($("runtime"))) return;
  const when = value => value ? new Date(value * 1000).toLocaleString() : "Not scheduled";
  const values = [
    ["Schema", data.schema],
    ["Beets", data.beets],
    ["yt-dlp", data.downloader.version || data.downloader.error],
    ["Next update", when(data.next_update)],
    ["Last update", data.last_update ? `${data.last_update.status}${data.last_update.error ? `: ${data.last_update.error}` : ""}` : "Bundled runtime"],
  ];
  $("runtime").replaceChildren(...values.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
  state.systemSignature = signature;
}

function renderIntegrityIssue(issue) {
  const item = node("div", null, "issue");
  const head = node("div", null, "issue-head");
  head.append(node("strong", issue.kind.replaceAll("_", " ")), badge(issue.severity));
  item.append(head, node("p", issue.message));
  if (issue.details?.path) item.append(node("small", issue.details.path));
  if (issue.track) item.append(button("Show music", () => {
    setTab("music");
    $("search").value = issue.track.matched_title || issue.track.title;
    state.page = 1;
    guard(() => refreshTracks(false))();
  }));
  return item;
}

async function loadIntegrity(background = true) {
  const data = await api(`/api/integrity?user_id=${encodeURIComponent(state.user)}`);
  const summary = data.running
    ? "Verification running…"
    : data.summary.total
      ? `${data.summary.errors} errors · ${data.summary.warnings} warnings · last run ${data.last_run ? time(data.last_run) : "never"}`
      : `No active issues · last run ${data.last_run ? time(data.last_run) : "never"}`;
  $("integritySummary").textContent = summary;
  $("healthSummary").textContent = data.running
    ? "Library verification is running…"
    : data.summary.total
      ? `Library health: ${data.summary.total} item${data.summary.total === 1 ? "" : "s"} need attention`
      : "Library health: no detected issues";
  const badgeElement = $("maintenanceBadge");
  badgeElement.hidden = data.summary.total === 0;
  badgeElement.textContent = data.summary.total;
  updateKeyedChildren($("integrityIssues"), data.issues, {
    key: issue => issue.id,
    signature: stable,
    render: renderIntegrityIssue,
    background,
    empty: () => node("p", "No active integrity issues.", "empty"),
  });
}

async function runIntegrity() {
  const result = await api("/api/integrity/run", {user_id: state.user});
  notice(result.message);
  await loadIntegrity(false);
}
$("verify").addEventListener("click", guard(runIntegrity));
$("verifyFromMusic").addEventListener("click", guard(runIntegrity));
$("healthSummary").addEventListener("click", () => setTab("maintenance"));

function renderIgnored(entry) {
  const item = node("div", null, "ignored-item");
  const head = node("div", null, "ignored-head");
  head.append(node("strong", entry.title), button("Restore", async () => {
    await api(`/api/ignored/${encodeURIComponent(entry.id)}?user_id=${encodeURIComponent(state.user)}`, null, "DELETE");
    notice("Ignored music restored.");
    await loadIgnored(false);
  }));
  item.append(head, node("p", entry.mbid ? `MusicBrainz ${entry.mbid}` : "No confirmed MusicBrainz identity", "hint"));
  for (const origin of entry.origins) item.append(node("small", `${origin.provider}: ${origin.title}`));
  return item;
}

async function loadIgnored(background = true) {
  const entries = await api(`/api/ignored?user_id=${encodeURIComponent(state.user)}`);
  updateKeyedChildren($("ignored"), entries, {
    key: entry => entry.id,
    signature: stable,
    render: renderIgnored,
    background,
    empty: () => node("p", "No ignored music.", "empty"),
  });
}

function reportErrors(results) {
  const errors = results.filter(result => result.status === "rejected");
  if (errors.length) notice(errors.map(result => result.reason?.message || String(result.reason)).join("\n"), true);
}

async function refreshFast(background = true) {
  if (!state.user) return;
  const results = await Promise.allSettled([refreshTracks(background), loadEvents()]);
  reportErrors(results);
}

async function refreshMaintenance(background = true) {
  if (!state.user) return;
  const results = await Promise.allSettled([loadIntegrity(background), loadIgnored(background), loadSystem(background)]);
  reportErrors(results);
}

async function refreshSlow(background = true) {
  if (!state.user) return;
  const results = await Promise.allSettled([loadSources(state.generation, background), refreshMaintenance(background)]);
  reportErrors(results);
}

async function refreshAll(background = false) {
  const results = await Promise.allSettled([refreshFast(background), refreshSlow(background)]);
  reportErrors(results);
}

let searchTimer = null;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.page = 1;
    state.generation += 1;
    guard(() => refreshTracks(false))();
  }, 300);
});
$("filter").addEventListener("change", () => {
  state.page = 1;
  state.generation += 1;
  guard(() => refreshTracks(false))();
});
$("prev").addEventListener("click", guard(async () => {
  if (state.page > 1) state.page -= 1;
  await refreshTracks(false);
}));
$("next").addEventListener("click", guard(async () => {
  if (state.page < state.maxPage) state.page += 1;
  await refreshTracks(false);
}));

$("syncAll").addEventListener("click", guard(async () => {
  for (const source of state.sources.filter(source => source.monitored && !["PENDING", "SYNCING"].includes(source.status))) {
    await api(`/api/sources/${encodeURIComponent(source.id)}/retry`, {user_id: state.user});
  }
  notice("Monitored sources queued");
  await loadSources(state.generation, false);
}));

for (const [id, mode, message] of [
  ["approveBest", "best", "Approve the top candidate for all waiting tracks?"],
  ["approveOriginal", "original", "Keep current/source metadata for all waiting tracks?"],
  ["retryAll", "retry", "Retry all failed operations for this user?"],
]) {
  const element = $(id);
  if (!element) continue;
  element.addEventListener("click", guard(async () => {
    if (!confirm(message)) return;
    const result = await api("/api/batch", {user_id: state.user, mode});
    notice(result.message);
    await refreshTracks(false);
  }));
}

$("update").addEventListener("click", guard(async () => {
  const result = await api("/api/downloader/update", {});
  notice(result.message);
  await loadSystem(false);
}));
$("rollback").addEventListener("click", guard(async () => {
  if (!confirm("Switch future downloads to the previous yt-dlp runtime?")) return;
  const result = await api("/api/downloader/rollback", {});
  notice(result.message);
  await loadSystem(false);
}));

window.addEventListener("unhandledrejection", event => notice(event.reason?.message || String(event.reason), true));
window.addEventListener("error", event => notice(event.message, true));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) guard(() => refreshAll(true))();
});

async function pollFast() {
  if (document.hidden || state.fastPolling || !state.user) return;
  state.fastPolling = true;
  try { await refreshFast(true); } finally { state.fastPolling = false; }
}
async function pollSlow() {
  if (document.hidden || state.slowPolling || !state.user) return;
  state.slowPolling = true;
  try { await refreshSlow(true); } finally { state.slowPolling = false; }
}

(async () => {
  try {
    await loadUsers(localStorage.getItem("music-user"));
  } catch (error) {
    notice(error?.message || String(error), true);
  } finally {
    setInterval(() => guard(pollFast)(), FAST_POLL_MS);
    setInterval(() => guard(pollSlow)(), SLOW_POLL_MS);
  }
})();
