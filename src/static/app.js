"use strict";

const $ = id => document.getElementById(id);
const FAST_POLL_MS = 2500;
const SLOW_POLL_MS = 10000;

const state = {
  user: "",
  tab: "music",
  musicView: "tracks",
  ignoredCount: 0,
  ignoredRulesCount: 0,
  disabledCount: 0,
  replacementTrack: null,
  replacementPending: false,
  ignoredPage: 1,
  ignoredMaxPage: 1,
  followLogs: true,
  unseenEvents: 0,
  removeTrack: null,
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
  integrityPage: 1,
  integrityMaxPage: 1,
  batchPending: false,
  repairable: 0,
};

const requestGate = new IngestorUI.RequestGate();
function beginRequest(scope) {
  const current = requestGate.begin(scope);
  const user = state.user;
  const generation = state.generation;
  return () => current() && user === state.user && generation === state.generation;
}

function setFilter(value) {
  state.musicView = value === "IGNORED" ? "ignored" : "tracks";
  $("musicResults").hidden = state.musicView === "ignored";
  $("ignoredMusicPanel").hidden = state.musicView !== "ignored";
  for (const tab of $("stats").children) tab.setAttribute("aria-pressed", String(tab.dataset.filter === value));
  if (value === "IGNORED") {
    state.ignoredPage = 1;
    guard(async () => reportErrors(await Promise.allSettled([loadIgnored(false), loadDisabledOrigins(false)])))();
    return;
  }
  $("filter").value = value;
  state.page = 1;
  state.generation += 1;
  guard(() => refreshTracks(false))();
}

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

async function api(path, body, method, current = () => true) {
  const requestMethod = method || (body ? "POST" : "GET");
  let response;
  try {
    response = await fetch(path, {
    method: requestMethod,
    cache: requestMethod === "GET" ? "no-store" : "default",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    if (!current()) return null;
    throw error;
  }
  if (!current()) return null;
  const data = await response.json().catch(() => ({
    error: `Invalid response (HTTP ${response.status})`,
  }));
  if (!current()) return null;
  if (!data || !response.ok || data.error) throw new Error(data?.error || `Invalid response (HTTP ${response.status})`);
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
    tab.setAttribute("aria-current", tab.dataset.tab === name ? "page" : "false");
  }
  for (const panel of document.querySelectorAll("[data-panel]")) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name === "maintenance") guard(() => refreshMaintenance(false))();
  if (name === "activity" && state.followLogs) requestAnimationFrame(followLatestEvents);
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
  window.SpotifyUI?.reset();
  window.MetadataUI?.reset();
  requestGate.reset();
  state.user = $("user").value;
  state.sources = [];
  state.musicView = "tracks";
  state.ignoredCount = 0;
  state.ignoredRulesCount = 0;
  state.disabledCount = 0;
  state.replacementTrack = null;
  state.replacementPending = false;
  $("replacementStatus").textContent = "";
  $("replacementSubmit").disabled = false;
  state.ignoredPage = 1;
  state.followLogs = true;
  state.unseenEvents = 0;
  state.removeTrack = null;
  $("musicResults").hidden = false;
  $("ignoredMusicPanel").hidden = true;
  updateLogFollowing();
  state.integrityPage = 1;
  state.integrityMaxPage = 1;
  state.repairable = 0;
  delete $("issueKind").dataset.signature;
  $("stats").replaceChildren();
  $("approveAll").disabled = true;
  $("retryAll").disabled = true;
  $("approveAll").textContent = "Approve all";
  $("retryAll").textContent = "Retry all failed";
  $("issueKind").replaceChildren(node("option", "All issue types"));
  $("issueKind").firstElementChild.value = "ALL";
  $("integrityIssues").replaceChildren();
  $("ignored").replaceChildren();
  $("disabledSources").replaceChildren();
  $("integritySummary").textContent = "Loading library audit...";
  $("healthSummary").textContent = "Library health: loading...";
  $("repairAll").disabled = true;
  $("maintenanceBadge").hidden = true;
  $("libraryScope").textContent = `Library totals for ${state.user}`;
  $("resultsSummary").textContent = "Loading matching tracks...";
  $("exportFailures").href = `/api/failures/export?user_id=${encodeURIComponent(state.user)}`;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
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
  const current = beginRequest("sources");
  const sources = await api(`/api/sources?user_id=${encodeURIComponent(state.user)}`, null, null, current);
  if (!current() || generation !== state.generation) return;
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
  if (!track || (track.user_id && track.user_id !== state.user)) throw new Error("Library changed; reopen this action.");
  const user = state.user;
  if (mode === "approve") extra.approval_token = track.approval_token;
  const result = await api(`/api/tracks/${encodeURIComponent(track.id)}/action`, {
    user_id: state.user,
    mode,
    ...extra,
  });
  if (user !== state.user) return;
  notice(result.message);
  state.approvals.delete(track.id);
  await refreshTracks(false);
}

function showRemoval(track) {
  if (["QUEUED", "RUNNING", "DEFERRED"].includes(track.operation_state)) return;
  state.removeTrack = {...track, user_id: state.user};
  $("removeTitle").textContent = track.matched_title || track.title;
  $("removeLibrary").textContent = `Library: ${state.user}`;
  const options = [
    ["delete", "Remove from this library", "Delete this entry and its local audio. A monitored source can import it again."],
    [track.origins.length === 1 ? "delete_ignore_urls" : "delete_ignore", `Remove and ignore ${track.origins.length === 1 ? "this URL" : "these URLs"}`,
      "These source URLs will stay ignored. Another upload of the same recording remains allowed."],
  ];
  // The legacy delete_ignore operation blocks an MBID too. For multiple origins,
  // retain that honest scope until the dedicated source-only API is available.
  if (track.mbid && track.origins.length > 1) options.pop();
  if (track.mbid) options.push(["delete_ignore", "Block this recording and its URLs",
    "Block this MusicBrainz recording throughout this library, including future uploads that match it."]);
  $("removeChoices").replaceChildren(...options.map(([value, title, explanation], index) => {
    const label = node("label", null, "remove-choice");
    const input = node("input"); input.type = "radio"; input.name = "removeScope"; input.value = value; input.checked = index === 0;
    const text = node("span"); text.append(node("strong", title), node("small", explanation));
    label.append(input, text); return label;
  }));
  $("removeUrls").replaceChildren(...track.origins.map(origin => node("li", origin.url || origin.title)));
  $("removeDialog").showModal();
}
function closeRemoval() { state.removeTrack = null; $("removeDialog").close(); }
$("cancelRemoval").addEventListener("click", closeRemoval);
$("removeDialog").addEventListener("cancel", () => { state.removeTrack = null; });
$("removeDialog").addEventListener("close", () => { state.removeTrack = null; });
$("removeForm").addEventListener("submit", guard(async event => {
  event.preventDefault();
  const track = state.removeTrack;
  if (!track || track.user_id !== state.user) throw new Error("Library changed; reopen this action.");
  const selected = $("removeChoices").querySelector("input:checked");
  if (!selected) throw new Error("Choose what to remove.");
  $("removeSubmit").disabled = true;
  try {
    await action(track, selected.value);
    state.removeTrack = null; $("removeDialog").close();
    await loadIgnored(false);
  } finally { $("removeSubmit").disabled = false; }
}));

function openOperation(track, mode) {
  const origins = track.origins.filter(origin => origin.downloadable && originDownloadsEnabled(origin));
  if (!origins.length) {
    notice("No enabled audio URL is available. Enable a source or choose another URL.", true);
    return;
  }
  $("operationForm").dataset.user = state.user;
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
  const track = {id: $("operationTrackId").value, user_id: $("operationForm").dataset.user};
  const mode = $("operationMode").value;
  await action(track, mode, {origin_id: $("operationOrigin").value});
  $("operationDialog").close();
}));

function showEditor(track) {
  $("editForm").reset();
  $("editForm").dataset.user = state.user;
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
  await action({id: $("editId").value, user_id: $("editForm").dataset.user}, "retag", {overrides});
  $("editDialog").close();
}));

function musicbrainzLink(kind, id) {
  const link = node("a", `${kind} ${id.slice(0, 8)}`);
  link.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

async function manageTrackOrigin(track, origin, mode) {
  const user = state.user;
  if (track.user_id !== user) throw new Error("Library changed; reopen the details dialog.");
  const description = mode === "detach"
    ? `Separate "${origin.title}" into its own track? Its playlists follow it, and its audio will be downloaded for review. The existing audio is kept.`
    : `Remove and ignore only the ${origin.provider} origin "${origin.title}"? It will disappear from local exported playlists and will NOT be imported again on monitored sync until restored in Music → Ignored music. The online YouTube playlist is NOT changed.`;
  if (!confirm(description)) return;
  const response = await api(
    `/api/tracks/${encodeURIComponent(track.id)}/origins/${encodeURIComponent(origin.id)}/${mode}`,
    {user_id: user},
  );
  if (user !== state.user) return;
  $("detailsDialog").close();
  notice(response.message, Boolean(response.playlist_refresh_failed?.length));
  await refreshTracks(false);
  if (response.playlist_refresh_failed?.length) await loadSources(state.generation, false);
}

function originDownloadsEnabled(origin) {
  return origin.download_enabled !== 0 && origin.download_enabled !== false;
}

async function setOriginDownload(track, origin, enabled) {
  const user = state.user;
  if (track.user_id !== user) throw new Error("Library changed; reopen this action.");
  if (!enabled && !confirm(`Stop downloads from this URL and disable future retries?\n${origin.url || origin.title}\n\nYour music, existing audio and playlists are kept. You can enable the URL again in Ignored music.`)) return;
  const result = await api(
    `/api/tracks/${encodeURIComponent(track.id)}/origins/${encodeURIComponent(origin.id)}/${enabled ? "enable" : "disable"}`,
    {user_id: user},
  );
  if (state.user !== user) return;
  const detail = result.publication_finishing
    ? " The current file is finishing safely; future downloads from this URL are disabled."
    : result.cancellation_pending ? " Stopping the current download. Wait for it to finish stopping before choosing another URL." : "";
  notice((result.message || (enabled ? "URL enabled. Retry when ready." : "Future downloads from this URL disabled.")) + detail);
  await Promise.all([refreshTracks(false), loadDisabledOrigins(false)]);
  if (state.user === user && $("detailsDialog").open) await showDetails(track);
}

function openReplacement(track) {
  if (track.user_id !== state.user) throw new Error("Library changed; reopen this action.");
  state.replacementTrack = {...track};
  state.replacementPending = false;
  $("replacementTitle").textContent = track.matched_title || track.title || track.track_title;
  $("replacementUrl").value = "";
  $("replacementStatus").textContent = "";
  $("replacementSubmit").disabled = false;
  $("replacementDialog").showModal();
  $("replacementUrl").focus?.();
}

function closeReplacement() {
  if (state.replacementPending) return;
  state.replacementTrack = null;
  $("replacementDialog").close();
}
$("cancelReplacement").addEventListener("click", closeReplacement);
$("replacementDialog").addEventListener("cancel", event => {
  if (state.replacementPending) event.preventDefault();
  else state.replacementTrack = null;
});
$("replacementForm").addEventListener("submit", async event => {
  event.preventDefault();
  const track = state.replacementTrack;
  if (state.replacementPending || !track) return;
  if (track.user_id !== state.user) { $("replacementStatus").textContent = "Library changed; reopen this action."; return; }
  const url = $("replacementUrl").value.trim();
  if (!url) { $("replacementStatus").textContent = "Enter a single YouTube video URL."; return; }
  state.replacementPending = true;
  $("replacementSubmit").disabled = true;
  $("replacementStatus").textContent = "Queuing replacement audio…";
  try {
    const result = await api(`/api/tracks/${encodeURIComponent(track.id)}/replacement`, {user_id: track.user_id, url});
    if (track.user_id !== state.user || state.replacementTrack !== track) return;
    state.replacementPending = false;
    closeReplacement();
    $("detailsDialog").close();
    notice(result.message || "Replacement audio queued. Existing audio stays playable until it succeeds.");
    await Promise.all([refreshTracks(false), loadDisabledOrigins(false)]);
  } catch (error) {
    if (track.user_id === state.user && state.replacementTrack === track) $("replacementStatus").textContent = error.message || String(error);
  } finally {
    if (track.user_id === state.user && (!state.replacementTrack || state.replacementTrack === track)) {
      state.replacementPending = false;
      $("replacementSubmit").disabled = false;
    }
  }
});

async function showDetails(track) {
  const current = beginRequest("details");
  const details = await api(`/api/tracks/${encodeURIComponent(track.id)}?user_id=${encodeURIComponent(state.user)}`, null, null, current);
  if (!current()) return;
  $("detailsTitle").textContent = details.matched_title || details.title;
  const body = $("detailsBody");
  const grid = node("div", null, "details-grid");

  const origins = node("div", null, "details-block");
  origins.append(node("h3", "Origins"));
  const originList = node("div", null, "details-list");
  for (const origin of details.origins) {
    const entry = node("div", null, "details-entry");
    entry.append(node("strong", origin.provider), node("div", origin.title), node("small", origin.url));
    const currentAudio = origin.id === details.current_origin_id || details.assets.some(asset => asset.state === "CURRENT" && asset.origin_id === origin.id);
    if (currentAudio) entry.append(node("small", "Current audio source — reprocess from another origin before detaching", "hint"));
    if (!originDownloadsEnabled(origin)) {
      entry.append(badge("DISABLED"), node("small", origin.disabled_reason || "Future downloads from this URL are disabled.", "hint"));
    }
    if (origin.last_error) entry.append(node("div", origin.last_error, "error-text"));
    const actions = node("div", null, "source-buttons origin-buttons");
    const detached = button("Separate track", () => manageTrackOrigin(details, origin, "detach"));
    const ignored = button("Remove & ignore origin", () => manageTrackOrigin(details, origin, "ignore"), "danger");
    const unavailable = details.origins.length < 2 || details.operation_state !== "IDLE" || currentAudio;
    detached.disabled = ignored.disabled = unavailable;
    actions.append(detached, ignored);
    if (origin.downloadable) {
      const enabled = originDownloadsEnabled(origin);
      const toggle = button(enabled ? "Disable downloads" : "Enable URL", () => setOriginDownload(details, origin, !enabled), enabled ? "secondary origin-disable" : "secondary origin-enable");
      toggle.dataset.originId = origin.id;
      actions.append(toggle);
    }
    entry.append(actions);
    originList.append(entry);
  }
  const replacement = button("Use another URL…", () => openReplacement(details), "secondary replace-source");
  replacement.disabled = ["QUEUED", "RUNNING", "DEFERRED"].includes(details.operation_state);
  origins.append(originList, replacement, node("p", "Disabling downloads keeps the track and its audio. A replacement video is checked before it replaces existing audio.", "hint"));

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

  const tags = node("div", null, "details-block file-tags");
  tags.append(node("h3", "Actual file tags"));
  const values = details.file_tags || {};
  for (const [label, value] of [
    ["Expected discovery", values.expected_discovery],
    ["COMMENT", values.comment], ["DESCRIPTION", values.description],
    ["R128 gain (Q7.8)", values.r128_track_gain],
    ["Navidrome track gain (dB)", values.navidrome_gain_db],
    ["Playback true peak", values.replaygain_track_peak],
    ["Loudness policy", values.policy],
  ]) {
    const field = node("div", null, "tag-field");
    field.append(node("strong", label), node("code", value ?? "Not present"));
    tags.append(field);
  }
  if (values.error) tags.append(node("p", values.error, "error-text"));
  if (values.issues?.length) tags.append(node("p", `Audit: ${values.issues.join(", ")}`, "warning-text"));
  tags.append(node("p", "These values are read from the current audio file. Playback clients may not expose every field.", "hint"));
  grid.append(tags, technical, origins, assets, history);
  body.replaceChildren(grid);
  $("detailsDialog").showModal();
}

$("closeDetails").addEventListener("click", () => $("detailsDialog").close());

function renderTrack(track) {
  state.tracks.set(track.id, track);
  const row = node("tr");
  const music = node("td");
  const playlists = node("td");
  const health = node("td");
  const actions = node("td");

  const title = track.matched_title || track.title;
  const youtubeOrigin = track.origins.find(origin => origin.provider === "youtube" && origin.id === track.current_origin_id)
    || track.origins.find(origin => origin.provider === "youtube");
  const channel = youtubeOrigin?.channel?.trim();
  music.append(node("div", channel ? `${channel} | ${title}` : title, "metadata"));
  if (track.matched_title && track.matched_title !== track.title) music.append(node("div", track.title, "source-title"));
  const originList = node("details", null, "origin-links");
  originList.append(node("summary", `${track.origins.length} source${track.origins.length === 1 ? "" : "s"}`));
  for (const origin of track.origins) {
    const line = node("div", null, "origin-line");
    line.append(node("span", `${origin.provider}: `, "origin-provider"));
    if (origin.url && /^https?:\/\//i.test(origin.url)) {
      const link = node("a", origin.url, "origin-url");
      link.href = origin.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = `Open ${origin.provider} origin`;
      line.append(link);
    } else {
      line.append(node("span", origin.url || origin.media_key || "Unknown origin", "origin-url"));
    }
    if (!originDownloadsEnabled(origin)) line.append(badge("DISABLED"));
    originList.append(line);
  }
  music.append(originList);

  if (track.playlists.length) {
    for (const playlist of track.playlists) {
      const line = node("div", playlist.title);
      line.title = `Added: ${time(playlist.added_at)}`;
      playlists.append(line);
    }
  } else playlists.textContent = "No playlist";

  const labels = {AVAILABLE: "Playable", MISSING: "Missing audio", UNAVAILABLE: "No audio yet",
    QUEUED: "Queued", RUNNING: "Processing", NEEDS_APPROVAL: "Waiting for approval",
    DEFERRED: "Retry scheduled", FAILED: "Failed", STOPPED: "Downloads stopped", IDLE: "Ready"};
  const status = track.operation_state === "IDLE" ? track.health : track.operation_state;
  const stateBadge = badge(status);
  stateBadge.textContent = labels[status] || status;
  health.append(stateBadge);
  if (track.temporary_candidate_sha256) health.append(node("small", "Playable · temporary album", "availability"));
  else if (track.health === "AVAILABLE" && status !== "AVAILABLE") health.append(node("small", "Current audio is playable", "availability"));
  if (!track.current_asset_id && track.origins.some(o => o.provider === "spotify") && !track.origins.some(o => o.downloadable)) stateBadge.textContent = "Needs audio link";
  if (track.operation_state === "DEFERRED" && track.retry_at) {
    health.append(node("div", `Retry after ${new Date(track.retry_at * 1000).toLocaleString()}`, "hint"));
  }
  if (track.cancellation_pending) health.append(node("div", "Stopping download…", "hint"));
  else if (track.needs_source) health.append(node("div", "Choose another audio URL or enable a source.", "hint"));
  if (track.download_failures) health.append(node("div", `${track.download_failures} failed download attempt${track.download_failures === 1 ? "" : "s"}`, "hint"));
  if (track.issue_count) health.append(node("div", `${track.issue_count} integrity issue${track.issue_count === 1 ? "" : "s"}`, "issue-link"));
  if (track.operation_error) {
    const details = node("details", null, "error-text");
    details.append(
      node("summary", track.operation_state === "DEFERRED" ? "Deferred reason" : "Last operation error"),
      node("div", track.operation_error),
    );
    health.append(details);
  }

  const controls = node("div", null, "actions");
  const busy = ["QUEUED", "RUNNING", "DEFERRED"].includes(track.operation_state);
  if (track.operation_state === "NEEDS_APPROVAL") {
    const select = node("select", null, "approval");
    select.setAttribute("aria-label", `Metadata suggestion for ${track.matched_title || track.title}`);
    track.choices.forEach((choice, index) => {
      const option = node("option", `${choice.kind === "source" ? "Source" : choice.kind === "asis" ? "Current tags" : `${choice.similarity}%`} | ${choice.artist ? `${choice.artist} - ` : ""}${choice.title}${choice.description ? ` | ${choice.description}` : ""}`);
      option.value = String(index);
      select.append(option);
    });
    select.value = state.approvals.get(track.id) ?? "0";
    select.addEventListener("change", () => state.approvals.set(track.id, select.value));
    controls.append(select, button("Approve", () => action(track, "approve", {index: Number(select.value)}), "primary"));
  }
  if (window.SpotifyUI) controls.append(...window.SpotifyUI.controls(track));
  if (window.MetadataUI) {
    const review = button("Review metadata", () => window.MetadataUI.open(track));
    review.disabled = busy;
    controls.append(review);
  }
  const activeOrigin = track.origins.find(origin => origin.id === track.active_origin_id);
  if (track.can_stop_download && activeOrigin) {
    const stop = button(track.cancellation_pending ? "Stopping…" : "Stop retrying this URL", () => setOriginDownload(track, activeOrigin, false), "secondary stop-download");
    stop.disabled = Boolean(track.cancellation_pending) || !originDownloadsEnabled(activeOrigin);
    stop.dataset.trackId = track.id;
    controls.append(stop);
  }
  if (track.operation_state === "FAILED" || (track.operation_state === "STOPPED" && track.origins.some(origin => origin.downloadable && originDownloadsEnabled(origin)))) {
    controls.append(button("Retry", () => action(track, "retry")));
  }
  if (track.operation_state === "STOPPED" || track.needs_source) {
    const replacement = button("Use another URL…", () => openReplacement(track), "primary replace-source");
    replacement.disabled = busy || Boolean(track.cancellation_pending);
    replacement.dataset.trackId = track.id;
    controls.append(replacement);
  }

  const menu = node("details", null, "action-menu");
  menu.append(node("summary", "More"));
  const menuItems = node("div", null, "action-menu-items");
  const editMetadata = button("Edit metadata", () => showEditor(track));
  editMetadata.disabled = busy || track.health !== "AVAILABLE";
  const technicalDetails = button("Sources & details", () => showDetails(track));
  const repairTags = button("Repair tags / loudness", async () => {
    if (!confirm("Recalculate loudness and discovery tags on this existing file? Audio and identity are preserved. No download is needed.")) return;
    await action(track, "repair");
  });
  repairTags.disabled = track.health !== "AVAILABLE" || track.operation_state !== "IDLE";
  const remove = button("Remove…", () => showRemoval(track), "danger");
  remove.disabled = busy;
  const refreshSuggestions = button("Refresh suggestions", async () => {
    await action(track, "refresh_suggestions");
  });
  refreshSuggestions.disabled = busy || track.health !== "AVAILABLE";
  refreshSuggestions.title = "Identify the existing audio again using its source hints. No new download.";
  menuItems.append(editMetadata, technicalDetails, refreshSuggestions, repairTags, remove);
  const reprocess = button("Reprocess", () => openOperation(track, "reprocess"));
  const redownload = button("Redownload", () => openOperation(track, "redownload"));
  for (const operation of [reprocess, redownload]) {
    operation.disabled = busy || !["AVAILABLE", "MISSING"].includes(track.health) || !track.origins.some(origin => origin.downloadable && originDownloadsEnabled(origin));
  }
  menuItems.append(reprocess, redownload);
  menu.append(menuItems);
  controls.append(menu);
  actions.append(controls);
  for (const [cell, label] of [[music, "Track"], [playlists, "Playlists"], [health, "Status"], [actions, "Actions"]]) cell.dataset.label = label;
  row.append(music, playlists, health, actions);
  return row;
}

function trackSignature(track) {
  return stable({
    metadata_tools: Boolean(window.MetadataUI),
    spotify_tools: Boolean(window.SpotifyUI),
    title: track.title,
    matched_title: track.matched_title,
    mbid: track.mbid,
    release_id: track.release_id,
    origins: track.origins,
    playlists: track.playlists,
    discovered_at: track.discovered_at,
    discovery_basis: track.discovery_basis,
    health: track.health,
    temporary_candidate_sha256: track.temporary_candidate_sha256,
    operation_state: track.operation_state,
    operation_kind: track.operation_kind,
    operation_error: track.operation_error,
    retry_at: track.retry_at,
    defer_count: track.defer_count,
    job_stage: track.job_stage,
    active_origin_id: track.active_origin_id,
    can_stop_download: track.can_stop_download,
    cancellation_pending: track.cancellation_pending,
    download_failures: track.download_failures,
    needs_source: track.needs_source,
    choices: track.choices,
    issue_count: track.issue_count,
    approval_token: track.approval_token,
  });
}

function updateStats(stats) {
  const approveAll = $("approveAll");
  approveAll.disabled = !stats.approval;
  approveAll.disabled ||= state.batchPending;
  approveAll.textContent = stats.approval ? `Approve all (${stats.approval})` : "Approve all";
  const retryAll = $("retryAll");
  retryAll.disabled = !stats.failed || state.batchPending;
  retryAll.textContent = stats.failed ? `Retry all failed (${stats.failed})` : "Retry all failed";
  const values = [
    ["All music", stats.total, "ALL"], ["Waiting for approval", stats.approval, "NEEDS_APPROVAL"],
    ["In progress", stats.working + stats.deferred, "WORKING"], ["Needs attention", stats.attention, "ATTENTION"],
    ["Ignored music", state.ignoredCount, "IGNORED"],
  ];
  for (const [label, count, filter] of values) {
    let element = $("stats").querySelector(`[data-filter="${filter}"]`);
    if (!element) {
      element = button(null, () => setFilter(filter), "stat");
      element.dataset.filter = filter;
      element.append(node("strong", "0"), node("span", label));
      $("stats").append(element);
    }
    element.firstElementChild.textContent = count.toLocaleString();
    element.setAttribute("aria-pressed", String(state.musicView === "ignored" ? filter === "IGNORED" : $("filter").value === filter));
    element.title = `Filter by ${label.toLowerCase()}; the count is for the whole selected library`;
  }
}

async function refreshTracks(background = true) {
  const current = beginRequest("tracks");
  const generation = state.generation;
  const data = await api(
    `/api/tracks?user_id=${encodeURIComponent(state.user)}`
    + `&page=${state.page}&limit=50`
    + `&status=${encodeURIComponent($("filter").value)}`
    + `&q=${encodeURIComponent($("search").value.trim())}`,
    null, null, current,
  );
  if (!current() || generation !== state.generation) return;
  state.maxPage = Math.max(1, Math.ceil(data.total / data.limit));
  if (state.page > state.maxPage) {
    state.page = state.maxPage;
    return refreshTracks(background);
  }
  for (const suffix of ["", "Bottom"]) {
    $("page" + suffix).textContent = `Page ${data.page} of ${state.maxPage}`;
    $("prev" + suffix).disabled = state.page <= 1;
    $("next" + suffix).disabled = state.page >= state.maxPage;
  }
  updateStats(data.stats);
  $("resultsSummary").textContent = IngestorUI.resultsLabel(data.page, data.limit, data.total, data.stats.total);
  const ids = new Set(data.tracks.map(track => track.id));
  for (const id of state.tracks.keys()) if (!ids.has(id)) state.tracks.delete(id);
  for (const track of data.tracks) state.tracks.set(track.id, track);
  updateKeyedChildren($("tracks"), data.tracks, {
    key: track => track.id,
    signature: trackSignature,
    render: renderTrack,
    background,
    empty: () => {
      const row = node("tr");
      const cell = node("td", "No music in this view.", "empty");
      cell.colSpan = 4;
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
  if (state.followLogs) followLatestEvents();
}

async function loadEvents() {
  const current = beginRequest("events");
  const response = await api(`/api/events?user_id=${encodeURIComponent(state.user)}&after=${state.after}`, null, null, current);
  if (!current()) return;
  const events = response.filter(event => event.id > state.after);
  if (!events.length) return;
  state.after = events.at(-1).id;
  state.events.push(...events);
  state.events = state.events.slice(-2000);
  const box = $("logs");
  for (const event of events) if (eventVisible(event)) box.append(eventNode(event));
  if (!selectionTouches(box)) while (box.childElementCount > 2000) box.firstElementChild?.remove();
  if (state.followLogs) followLatestEvents();
  else { state.unseenEvents += events.filter(eventVisible).length; updateLogFollowing(); }
}

function updateLogFollowing() {
  $("followLogs").checked = state.followLogs;
  $("jumpLatest").hidden = state.followLogs;
  $("jumpLatest").textContent = state.unseenEvents ? `Jump to latest · ${state.unseenEvents} new` : "Jump to latest";
}
function followLatestEvents() {
  if (state.tab !== "activity" || !state.followLogs) return;
  const box = $("logs");
  box.scrollTop = box.scrollHeight;
  state.unseenEvents = 0; updateLogFollowing();
}
$("logs").addEventListener("scroll", () => {
  if (state.tab !== "activity") return;
  const box = $("logs");
  state.followLogs = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  if (state.followLogs) state.unseenEvents = 0;
  updateLogFollowing();
});
$("followLogs").addEventListener("change", () => {
  state.followLogs = $("followLogs").checked;
  updateLogFollowing(); followLatestEvents();
});
$("jumpLatest").addEventListener("click", () => { state.followLogs = true; followLatestEvents(); });

$("clearLogs").addEventListener("click", () => {
  state.events = [];
  state.unseenEvents = 0; state.followLogs = true; updateLogFollowing();
  $("logs").replaceChildren();
  notice("Displayed logs cleared. Stored server events were not deleted.");
});
$("errorsOnly").addEventListener("change", renderEvents);

async function loadSystem(background = true) {
  const current = beginRequest("system");
  const data = await api("/api/system", null, null, current);
  if (!current()) return;
  const signature = stable(data);
  if (signature === state.systemSignature) return;
  if (background && elementIsBusy($("runtime"))) return;
  const when = value => value ? new Date(value * 1000).toLocaleString() : "Not scheduled";
  const circuit = data.youtube_circuit || {};
  const youtubeStatus = circuit.open
    ? `Paused until ${when(circuit.until)} · ${circuit.reason || "temporary block"}`
    : circuit.failures
      ? `Ready · ${circuit.failures} recent block${circuit.failures === 1 ? "" : "s"}`
      : "Ready";
  const values = [
    ["Schema", data.schema],
    ["Beets", data.beets],
    ["yt-dlp", data.downloader.version || data.downloader.error],
    ["YouTube downloads", youtubeStatus],
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
    setFilter("ALL");
  }));
  return item;
}

async function loadIntegrity(background = true) {
  const current = beginRequest("integrity");
  const data = await api(`/api/integrity?user_id=${encodeURIComponent(state.user)}&page=${state.integrityPage}&limit=50&kind=${encodeURIComponent($("issueKind").value)}`, null, null, current);
  if (!current()) return;
  state.integrityMaxPage = Math.max(1, Math.ceil(data.total / data.limit));
  if (state.integrityPage > state.integrityMaxPage) {
    state.integrityPage = state.integrityMaxPage;
    return loadIntegrity(background);
  }
  state.repairable = data.repairable_tracks || 0;
  $("repairAll").textContent = `Repair eligible tags (${state.repairable})`;
  $("repairAll").disabled = !state.repairable || data.running || state.batchPending;
  $("issuePage").textContent = `${data.page} / ${state.integrityMaxPage} (${data.total} matching issues)`;
  $("issuePrev").disabled = data.page <= 1;
  $("issueNext").disabled = data.page >= state.integrityMaxPage;
  const kind = $("issueKind").value;
  const options = [{kind: "ALL", count: data.summary.total}, ...data.kinds];
  if (!options.some(value => value.kind === kind)) options.push({kind, count: 0});
  const signature = stable(options);
  if ($("issueKind").dataset.signature !== signature) {
    $("issueKind").replaceChildren(...options.map(value => {
      const option = node("option", `${value.kind === "ALL" ? "All issue types" : value.kind.replaceAll("_", " ")} (${value.count})`);
      option.value = value.kind;
      return option;
    }));
    $("issueKind").value = kind;
    $("issueKind").dataset.signature = signature;
  }
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
  const user = state.user;
  const result = await api("/api/integrity/run", {user_id: state.user});
  if (user !== state.user) return;
  notice(result.message);
  await loadIntegrity(false);
}
$("verify").addEventListener("click", guard(runIntegrity));
$("verifyFromMusic").addEventListener("click", guard(runIntegrity));
$("healthSummary").addEventListener("click", () => setTab("maintenance"));

function renderIgnored(entry) {
  const item = node("div", null, "ignored-item");
  const head = node("div", null, "ignored-head");
  head.append(node("strong", entry.title));
  if (entry.mbid) head.append(button("Allow recording (keep URLs ignored)", async () => {
    const user = state.user;
    if (!confirm(`Allow MusicBrainz recording ${entry.mbid} again for ${user}? This does NOT restore its ${entry.origins.length} ignored source URL(s). Use Retry on any failed approval afterward.`)) return;
    const result = await api(`/api/ignored/${encodeURIComponent(entry.id)}/allow-recording`, {user_id: user});
    if (user !== state.user) return;
    notice(result.message);
    await loadIgnored(false);
  }));
  head.append(button("Restore all (identity + URLs)", async () => {
    const user = state.user;
    if (!confirm(`Restore all ignore rules for "${entry.title}"? The blocked MusicBrainz identity (if any) and ALL ${entry.origins.length} source URL(s) can be re-imported on the next monitored sync.`)) return;
    await api(`/api/ignored/${encodeURIComponent(entry.id)}?user_id=${encodeURIComponent(user)}`, null, "DELETE");
    if (user !== state.user) return;
    notice("All ignored recording and source URL rules restored.");
    await loadIgnored(false);
  }));
  item.append(head, node("p", entry.mbid ? `MusicBrainz ${entry.mbid}` : "No confirmed MusicBrainz identity", "hint"));
  for (const origin of entry.origins) {
    const source = node("div", null, "ignored-origin");
    source.append(node("small", `${origin.provider}: ${origin.title} — ${origin.url}`));
    source.append(button("Restore just this URL", async () => {
      const user = state.user;
      if (!confirm(`Allow this ${origin.provider} source URL again? ${origin.url} Other ignored URLs and any recording-wide block will remain unchanged.`)) return;
      const result = await api(`/api/ignored/${encodeURIComponent(entry.id)}/origins/${encodeURIComponent(origin.provider)}/${encodeURIComponent(origin.media_key)}?user_id=${encodeURIComponent(user)}`, null, "DELETE");
      if (user !== state.user) return;
      notice(result.message);
      await loadIgnored(false);
    }));
    item.append(source);
  }
  return item;
}

async function loadIgnored(background = true) {
  const current = beginRequest("ignored");
  const entries = await api(`/api/ignored?user_id=${encodeURIComponent(state.user)}`, null, null, current);
  if (!current()) return;
  state.ignoredRulesCount = entries.length;
  updateIgnoredCount();
  const query = $("ignoredSearch").value.trim().toLocaleLowerCase();
  const matching = entries.filter(entry => !query || `${entry.title} ${entry.mbid || ""} ${(entry.origins || []).map(origin => `${origin.title} ${origin.url}`).join(" ")}`.toLocaleLowerCase().includes(query));
  state.ignoredMaxPage = Math.max(1, Math.ceil(matching.length / 50));
  state.ignoredPage = Math.min(state.ignoredPage, state.ignoredMaxPage);
  $("ignoredPage").textContent = `Page ${state.ignoredPage} of ${state.ignoredMaxPage}`;
  $("ignoredPrev").disabled = state.ignoredPage <= 1;
  $("ignoredNext").disabled = state.ignoredPage >= state.ignoredMaxPage;
  updateKeyedChildren($("ignored"), matching.slice((state.ignoredPage - 1) * 50, state.ignoredPage * 50), {
    key: entry => entry.id,
    signature: stable,
    render: renderIgnored,
    background,
    empty: () => node("p", "No ignored music.", "empty"),
  });
}

$("ignoredSearch").addEventListener("input", () => { state.ignoredPage = 1; guard(async () => reportErrors(await Promise.allSettled([loadIgnored(false), loadDisabledOrigins(false)])))(); });
for (const [id, delta] of [["ignoredPrev", -1], ["ignoredNext", 1]]) {
  $(id).addEventListener("click", guard(async () => {
    state.ignoredPage = Math.max(1, Math.min(state.ignoredMaxPage, state.ignoredPage + delta));
    await loadIgnored(false);
  }));
}

function updateIgnoredCount() {
  state.ignoredCount = state.ignoredRulesCount + state.disabledCount;
  const tab = $("stats").querySelector('[data-filter="IGNORED"]');
  if (tab) tab.firstElementChild.textContent = state.ignoredCount.toLocaleString();
}

function renderDisabledOrigin(origin) {
  const track = {id: origin.track_id, user_id: origin.user_id || state.user, title: origin.track_title || origin.title};
  const item = node("div", null, "ignored-item disabled-origin");
  const head = node("div", null, "ignored-head");
  head.append(node("strong", track.title), badge("DISABLED"));
  item.append(head, node("p", origin.url, "origin-url"), node("p", origin.disabled_reason || "Downloads from this URL are disabled.", "hint"));
  const actions = node("div", null, "row");
  const enable = button("Enable URL", () => setOriginDownload(track, origin, true), "secondary origin-enable");
  const replacement = button("Use another URL…", () => openReplacement(track), "secondary replace-source");
  replacement.disabled = ["QUEUED", "RUNNING", "DEFERRED"].includes(origin.operation_state);
  actions.append(enable, replacement, button("Sources & details", () => showDetails(track)));
  item.append(actions);
  return item;
}

async function loadDisabledOrigins(background = true) {
  const current = beginRequest("disabledOrigins");
  const entries = await api(`/api/disabled-origins?user_id=${encodeURIComponent(state.user)}`, null, null, current);
  if (!current()) return;
  state.disabledCount = new Set(entries.map(entry => entry.track_id)).size;
  updateIgnoredCount();
  const query = $("ignoredSearch").value.trim().toLocaleLowerCase();
  const matching = entries.filter(entry => !query || `${entry.track_title || ""} ${entry.title} ${entry.url}`.toLocaleLowerCase().includes(query));
  updateKeyedChildren($("disabledSources"), matching, {
    key: entry => entry.id, signature: stable, render: renderDisabledOrigin, background,
    empty: () => node("p", "No disabled download URLs.", "empty"),
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
  const results = await Promise.allSettled([loadIntegrity(background), loadIgnored(background), loadDisabledOrigins(background), loadSystem(background)]);
  reportErrors(results);
}

async function refreshSlow(background = true) {
  if (!state.user) return;
  const results = await Promise.allSettled([loadSources(state.generation, background), refreshMaintenance(background), window.SpotifyUI?.refresh()]);
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
for (const suffix of ["", "Bottom"]) {
  for (const [direction, delta] of [["prev", -1], ["next", 1]]) {
    $(direction + suffix).addEventListener("click", guard(async () => {
      state.page = Math.max(1, Math.min(state.maxPage, state.page + delta));
      await refreshTracks(false);
      if (suffix) $("musicResults").scrollIntoView?.({block: "start"});
    }));
  }
}
$("addMusic").addEventListener("click", () => {
  setTab("sources");
  $("urls").focus();
});

$("syncAll").addEventListener("click", guard(async () => {
  const user = state.user;
  for (const source of state.sources.filter(source => source.monitored && !["PENDING", "SYNCING", "DEFERRED"].includes(source.status))) {
    if (user !== state.user) return;
    await api(`/api/sources/${encodeURIComponent(source.id)}/retry`, {user_id: user});
  }
  if (user !== state.user) return;
  notice("Monitored sources queued");
  await loadSources(state.generation, false);
}));

for (const [id, mode, message] of [
  ["approveAll", "best", () => `Approve the top candidate for every track awaiting approval for "${state.user}"? This only affects the current library user.`],
  ["approveOriginal", "original", () => `Keep current/source metadata for all waiting tracks for "${state.user}"?`],
  ["retryAll", "retry", () => `Retry ALL failed tracks for "${state.user}", across every page? Search and filters do not limit this action. Deferred, queued and approval tracks are not changed.`],
  ["repairAll", "repair", () => `Repair discovery and loudness tags on ${state.repairable} eligible existing files for "${state.user}"? This uses the latest audit across all pages. No downloads, identity changes or audio re-encoding. Files with unexplained hash changes are excluded.`],
]) {
  const element = $(id);
  if (!element) continue;
  element.addEventListener("click", guard(async () => {
    if (state.batchPending || !confirm(message())) return;
    const user = state.user;
    state.batchPending = true;
    element.disabled = true;
    try {
      const result = await api("/api/batch", {user_id: user, mode});
      if (user !== state.user) return;
      notice(`${result.message}. ${result.skipped || 0} skipped because their state changed or they were ineligible.`);
    } finally {
      state.batchPending = false;
      await refreshAll(false);
    }
  }));
}

$("issueKind").addEventListener("change", () => {
  state.integrityPage = 1;
  guard(() => loadIntegrity(false))();
});
$("issuePrev").addEventListener("click", guard(async () => {
  state.integrityPage = Math.max(1, state.integrityPage - 1);
  await loadIntegrity(false);
}));
$("issueNext").addEventListener("click", guard(async () => {
  state.integrityPage = Math.min(state.integrityMaxPage, state.integrityPage + 1);
  await loadIntegrity(false);
}));

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
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("spotify") === "connected";
    await loadUsers(connected ? params.get("library") : localStorage.getItem("music-user"));
    if (connected) {
      setTab("sources"); notice("Spotify connected. Import a source or browse your playlists.");
      window.history.replaceState(null, "", window.location.pathname);
    }
  } catch (error) {
    notice(error?.message || String(error), true);
  } finally {
    setInterval(() => guard(pollFast)(), FAST_POLL_MS);
    setInterval(() => guard(pollSlow)(), SLOW_POLL_MS);
  }
})();
