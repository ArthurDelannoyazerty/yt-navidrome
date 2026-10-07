"use strict";
const $ = id => document.getElementById(id);
const state = {user: "", page: 1, maxPage: 1, after: 0, generation: 0, sources: [], events: [], approvals: new Map()};
const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = String(text); if (cls) e.className = cls; return e; };
function notice(message, error = false) { const e = $("notice"); e.hidden = false; e.textContent = message; e.className = error ? "error" : "success"; }
async function api(path, body, method) {
  const response = await fetch(path, {method: method || (body ? "POST" : "GET"), headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  const data = await response.json().catch(() => ({error: `Invalid response (HTTP ${response.status})`}));
  if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
const guard = fn => async (...args) => { try { await fn(...args); } catch (error) { notice(error.message, true); } };
function button(text, fn, cls = "secondary") { const b = node("button", text, cls); b.type = "button"; b.addEventListener("click", guard(async () => { b.disabled = true; try { await fn(); } finally { b.disabled = false; } })); return b; }
function badge(status) { return node("span", status.replaceAll("_", " "), `badge ${status}`); }
function time(value) { return value ? value.replace("T", " ").replace("Z", " UTC") : "Unknown (not invented)"; }
async function loadUsers(preferred) {
  const users = await api("/api/users"); $("user").replaceChildren(...users.map(u => { const o = node("option", u); o.value = u; return o; }));
  $("user").value = users.includes(preferred) ? preferred : (users.includes("admin") ? "admin" : users[0]);
  changeUser();
}
function changeUser() {
  state.user = $("user").value; localStorage.setItem("music-user", state.user); state.page = 1; state.after = 0; state.generation++; state.events = []; state.approvals.clear();
  $("logs").replaceChildren(); $("tracks").replaceChildren(); $("sources").replaceChildren(); guard(refresh)();
}
$("user").addEventListener("change", changeUser);
$("userForm").addEventListener("submit", guard(async event => { event.preventDefault(); const name = $("newUser").value.trim(); await api("/api/users", {name}); $("newUser").value = ""; await loadUsers(name); }));
$("ingestForm").addEventListener("submit", guard(async event => {
  event.preventDefault(); const urls = $("urls").value.split(/\r?\n/).map(s => s.trim()).filter(Boolean); if (!urls.length) return;
  await api("/api/sources", {user_id: state.user, urls, monitored: $("monitor").checked}); $("urls").value = ""; notice("Sources queued. API and download errors will appear below."); await refresh();
}));
async function loadSources(generation) {
  const sources = await api(`/api/sources?user_id=${encodeURIComponent(state.user)}`); if (generation !== state.generation) return;
  state.sources = sources; const fragment = document.createDocumentFragment();
  for (const s of sources) {
    const row = node("div", null, "source"), content = node("div", null, "source-details");
    content.append(node("strong", s.title), node("p", s.url, "url"), badge(s.status), node("small", s.monitored ? "  Monitored" : "  One-off"));
    if (s.synced_at) content.append(node("p", `Last sync: ${time(s.synced_at)}`, "hint"));
    if (s.error) content.append(node("div", s.error, "error-text"));
    const controls = node("div", null, "source-buttons");
    if (s.provider !== "legacy") controls.append(button("Sync / retry", async () => { await api(`/api/sources/${encodeURIComponent(s.id)}/retry`, {user_id: state.user}); notice("Source sync queued"); await refresh(); }));
    controls.append(button("Remove", async () => { if (!confirm("Remove this source and its exported playlist? Music files are retained.")) return; await api(`/api/sources/${encodeURIComponent(s.id)}?user_id=${encodeURIComponent(state.user)}`, null, "DELETE"); await refresh(); }, "danger"));
    row.append(content, controls); fragment.append(row);
  }
  if (!sources.length) fragment.append(node("p", "No sources yet.", "empty")); $("sources").replaceChildren(fragment);
}
async function action(track, mode, extra = {}) { const result = await api(`/api/tracks/${encodeURIComponent(track.id)}/action`, {user_id: state.user, mode, ...extra}); notice(result.message); state.approvals.delete(track.id); await refresh(); }
function showEditor(track) { $("editForm").reset(); $("editId").value = track.id; $("editTitle").textContent = track.matched_title || track.title; $("editDialog").showModal(); }
$("cancelEdit").addEventListener("click", () => $("editDialog").close());
$("editForm").addEventListener("submit", guard(async event => {
  event.preventDefault(); const overrides = {};
  for (const [field, id] of Object.entries({mbid: "editMbid", release_id: "editRelease", artist: "editArtist", title: "editSong", album: "editAlbum"})) { const value = $(id).value.trim(); if (value) overrides[field] = value; }
  if (!Object.keys(overrides).length) throw new Error("Enter a metadata change, or use Re-identify to find new matches.");
  await action({id: $("editId").value}, "retag", {overrides}); $("editDialog").close();
}));
function musicbrainzLink(kind, id) { const a = node("a", `${kind} ${id.slice(0, 8)}`); a.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; }
async function loadTracks(generation) {
  const data = await api(`/api/tracks?user_id=${encodeURIComponent(state.user)}&page=${state.page}&limit=50&status=${encodeURIComponent($("filter").value)}`);
  if (generation !== state.generation) return;
  state.maxPage = Math.max(1, Math.ceil(data.total / data.limit)); if (state.page > state.maxPage) { state.page = state.maxPage; return loadTracks(generation); }
  $("page").textContent = `${data.page} / ${state.maxPage} (${data.total} tracks)`; $("prev").disabled = state.page <= 1; $("next").disabled = state.page >= state.maxPage;
  const stats = [["Total", Object.values(data.stats).reduce((a, b) => a + b, 0)], ["Completed", data.stats.COMPLETED || 0], ["Queued / processing", (data.stats.PENDING || 0) + (data.stats.PROCESSING || 0)], ["Approval", data.stats.NEEDS_APPROVAL || 0], ["Failed", data.stats.FAILED || 0]];
  $("stats").replaceChildren(...stats.map(([label, n]) => { const e = node("span", null, "stat"); e.append(node("strong", n), document.createTextNode(label)); return e; }));
  if (document.activeElement?.classList.contains("approval")) return; // Do not interrupt an open selection.
  const fragment = document.createDocumentFragment();
  for (const track of data.tracks) {
    const row = node("tr"), title = node("td"), playlists = node("td"), discovered = node("td"), status = node("td"), actions = node("td");
    title.append(node("strong", track.title), node("div", track.url, "url"));
    if (track.matched_title) title.append(node("div", track.matched_title, "metadata"));
    if (track.mbid) title.append(musicbrainzLink("recording", track.mbid));
    if (track.release_id) title.append(node("br"), musicbrainzLink("release", track.release_id));
    for (const p of track.playlists) { const line = node("div", p.title); line.title = `Added: ${time(p.added_at)}`; playlists.append(line); }
    if (!track.playlists.length) playlists.textContent = "No playlist";
    discovered.append(node("div", time(track.discovered_at)), node("small", track.discovery_basis)); status.append(badge(track.status));
    if (track.error) { const details = node("details", null, track.status === "COMPLETED" ? "warning-text" : "error-text"); details.append(node("summary", track.status === "COMPLETED" ? "Enrichment warnings" : "Error details"), node("div", track.error, "error-text")); status.append(details); }
    const controls = node("div", null, "actions");
    if (track.status === "FAILED") controls.append(button("Retry", () => action(track, "retry")), button("Redownload", async () => { if (confirm("Discard the staged audio and download again? Any previous library file is retained until success.")) await action(track, "redownload"); }));
    if (track.status === "COMPLETED") {
      controls.append(button("Edit", () => showEditor(track)), button("Re-identify", async () => { if (confirm("Look up metadata again without downloading the audio?")) await action(track, "reidentify"); }), button("Redownload", async () => { if (confirm("Download a replacement? The existing file is kept until the replacement succeeds.")) await action(track, "redownload"); }));
    }
    if (track.status === "NEEDS_APPROVAL") {
      const select = node("select", null, "approval"); select.setAttribute("aria-label", "Metadata candidate");
      track.choices.forEach((choice, i) => { const option = node("option", `${choice.similarity}% | ${choice.artist} - ${choice.title}${choice.description ? " | " + choice.description : ""}`); option.value = String(i); select.append(option); });
      const original = node("option", "Keep existing / source metadata (no match)"); original.value = "original"; select.append(original); select.value = state.approvals.get(track.id) ?? (track.choices.length ? "0" : "original");
      select.addEventListener("change", () => state.approvals.set(track.id, select.value));
      controls.append(select, button("Approve", () => action(track, "approve", {index: select.value === "original" ? null : Number(select.value)})));
    }
    actions.append(controls); row.append(title, playlists, discovered, status, actions); fragment.append(row);
  }
  if (!data.tracks.length) { const row = node("tr"), cell = node("td", "No tracks in this view.", "empty"); cell.colSpan = 5; row.append(cell); fragment.append(row); }
  $("tracks").replaceChildren(fragment);
}
function renderEvents() {
  const box = $("logs"), atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const items = state.events.filter(e => !$("errorsOnly").checked || ["WARNING", "ERROR", "CRITICAL"].includes(e.level));
  box.replaceChildren(...items.map(e => node("div", `${e.at} ${e.level}${e.target ? " [" + e.target.slice(0, 12) + "]" : ""} ${e.message}`, `log-${e.level}`)));
  if (atBottom) box.scrollTop = box.scrollHeight;
}
async function loadEvents(generation) {
  const events = await api(`/api/events?user_id=${encodeURIComponent(state.user)}&after=${state.after}`); if (generation !== state.generation) return;
  if (events.length) { state.after = events.at(-1).id; state.events.push(...events); state.events = state.events.slice(-2000); renderEvents(); }
}
async function loadSystem() {
  const data = await api("/api/system");
  const when = value => value ? new Date(value * 1000).toLocaleString() : "Not scheduled";
  const values = [["Beets", data.beets], ["yt-dlp", data.downloader.version || data.downloader.error], ["Next update", when(data.next_update)], ["Last update", data.last_update ? (data.last_update.status + (data.last_update.error ? ": " + data.last_update.error : "")) : "Bundled runtime"]];
  $("runtime").replaceChildren(...values.flatMap(([label, value]) => [node("dt", label), node("dd", value)]));
}
async function refresh() { if (!state.user) return; const gen = state.generation; const results = await Promise.allSettled([loadSources(gen), loadTracks(gen), loadEvents(gen), loadSystem()]); const errors = results.filter(r => r.status === "rejected"); if (errors.length) notice(errors.map(r => r.reason.message).join("\n"), true); }
$("filter").addEventListener("change", () => { state.page = 1; state.generation++; guard(refresh)(); });
$("prev").addEventListener("click", guard(async () => { if (state.page > 1) state.page--; await refresh(); }));
$("next").addEventListener("click", guard(async () => { if (state.page < state.maxPage) state.page++; await refresh(); }));
$("errorsOnly").addEventListener("change", renderEvents);
$("syncAll").addEventListener("click", guard(async () => { for (const source of state.sources.filter(s => s.monitored && !["PENDING", "SYNCING"].includes(s.status))) await api(`/api/sources/${encodeURIComponent(source.id)}/retry`, {user_id: state.user}); notice("Monitored sources queued"); await refresh(); }));
for (const [id, mode, message] of [["approveBest", "best", "Approve the top candidate for all waiting tracks? This overrides beets' uncertainty."], ["approveOriginal", "original", "Keep existing/source metadata for all waiting tracks without matching? Unknown album/release dates will not be invented."], ["retryAll", "retry", "Retry all failed tracks for this user?"]]) $(id).addEventListener("click", guard(async () => { if (!confirm(message)) return; const result = await api("/api/batch", {user_id: state.user, mode}); notice(result.message); await refresh(); }));
$("update").addEventListener("click", guard(async () => { const result = await api("/api/downloader/update", {}); notice(result.message); await loadSystem(); }));
$("rollback").addEventListener("click", guard(async () => { if (!confirm("Switch future downloads to the previous yt-dlp runtime? The nightly schedule remains enabled.")) return; const result = await api("/api/downloader/rollback", {}); notice(result.message); await loadSystem(); }));
window.addEventListener("unhandledrejection", event => { notice(event.reason?.message || String(event.reason), true); });
window.addEventListener("error", event => notice(event.message, true));
(async () => { try { await loadUsers(localStorage.getItem("music-user")); } catch (error) { notice(error.message, true); } finally { setInterval(() => guard(refresh)(), 4000); } })();
