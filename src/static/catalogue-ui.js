/* Shared public MusicBrainz catalogue; local album edits require a fresh preview. */
"use strict";
window.CatalogueUI = (() => {
  const byId = id => document.getElementById(id);
  const ui = { user: null, detail: null, results: [],
    busy: false, serial: 0, prefetchTimer: null, cacheActionPending: false,
    failedLoading: false, failedSerial: 0, failedTotal: null, failedVisible: 0 };
  const current = (user, serial) => user === state.user && serial === ui.serial;
  const element = (tag, text, cls) => {
    const e = document.createElement(tag);
    if (text != null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  };
  const button = (label, handler, secondary = true) => {
    const e = element("button", label, secondary ? "secondary" : "");
    e.type = "button";
    e.addEventListener("click", () => Promise.resolve().then(handler).catch(error => message(error.message, true)));
    return e;
  };
  function message(value, error = false, id = "catalogueStatus") {
    const target = byId(id);
    target.textContent = value;
    target.className = error ? "error-text" : "hint";
  }
  function leave() {
    clearTimeout(ui.prefetchTimer); ui.prefetchTimer = null;
    ui.failedSerial++; ui.failedLoading = false;
    byId("approvalCacheConfirm").hidden = true;
  }
  function reset() {
    leave();
    ui.user = null; ui.detail = null; ui.results = [];
    ui.serial++;
    ui.failedTotal = null; ui.failedVisible = 0;
    byId("approvalFailedSection").open = false;
    byId("approvalFailedList").replaceChildren();
    byId("approvalFailedMore").hidden = true;
    byId("approvalFailedCount").textContent = "0";
    byId("approvalFailedNotice").textContent = "";
    for (const id of ["catalogueResults"]) byId(id).replaceChildren();
    byId("catalogueResults").append(element("p", "Search to find cached MusicBrainz entries.", "hint"));
    byId("catalogueDetail").replaceChildren(element("p", "Select an entry on the left to browse its discography or tracklist.", "hint"));
    for (const id of ["catalogueStatus", "catalogueProgress"]) byId(id).textContent = "";
  }
  const num = value => Number(value || 0).toLocaleString();
  function bytes(value) {
    const amount = Math.max(0, Number(value) || 0);
    if (amount < 1024) return amount + " B";
    if (amount < 1048576) return (amount / 1024).toFixed(1) + " KiB";
    return (amount / 1048576).toFixed(1) + " MiB";
  }
  function failedEntry(item) {
    const row = element("article", null, "approval-failed-row");
    const info = element("div", null, "approval-failed-info");
    info.append(
      element("strong", item.label || "MusicBrainz request"),
      element("small", item.kind + " · " + num(item.attempts) + " failed executions"),
      element("p", item.last_error || "No error detail available.", "hint")
    );
    const retry = element("button", "Retry 3 times", "secondary");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      const user = ui.user;
      if (!user || state.tab !== "catalogue" || retry.disabled) return;
      retry.disabled = true;
      try {
        await api("/api/musicbrainz/approval-prefetch-retry", {
          user_id: user, job_id: item.job_id
        });
        if (ui.user !== user || state.tab !== "catalogue") return;
        message("Request requeued at low priority for 3 more attempts.",
          false, "approvalFailedNotice");
        await refreshFailedJobs(true);
        void refreshPrefetchStatus();
      } catch (error) {
        if (ui.user === user && state.tab === "catalogue") {
          retry.disabled = false;
          message(error.message || String(error), true, "approvalFailedNotice");
        }
      }
    });
    row.append(info, retry);
    return row;
  }
  async function refreshFailedJobs(reset = true) {
    if (!ui.user || state.tab !== "catalogue" ||
        !byId("approvalFailedSection").open || ui.failedLoading) return;
    const user = ui.user, serial = ++ui.failedSerial, offset = reset ? 0 : ui.failedVisible;
    ui.failedLoading = true;
    try {
      const result = await api("/api/musicbrainz/approval-prefetch-failures?" +
        new URLSearchParams({user_id: user, limit: "50", offset: String(offset)}));
      if (serial !== ui.failedSerial || user !== ui.user ||
          state.tab !== "catalogue" || !byId("approvalFailedSection").open) return;
      const list = byId("approvalFailedList");
      if (reset) list.replaceChildren();
      for (const item of result.items || []) list.append(failedEntry(item));
      ui.failedVisible = offset + (result.items || []).length;
      ui.failedTotal = Number(result.total || 0);
      byId("approvalFailedCount").textContent = num(ui.failedTotal);
      byId("approvalFailedMore").hidden = ui.failedVisible >= ui.failedTotal;
      if (!ui.failedTotal) list.append(element("p", "No disabled requests.", "hint"));
    } catch (error) {
      if (serial === ui.failedSerial && user === ui.user && state.tab === "catalogue")
        message(error.message || String(error), true, "approvalFailedNotice");
    } finally {
      if (serial === ui.failedSerial) ui.failedLoading = false;
    }
  }
  async function refreshPrefetchStatus() {
    if (!(ui.user && state.tab === "catalogue")) return;
    const user = ui.user;
    try {
      const response = await fetch("/api/musicbrainz/approval-prefetch-status?user_id=" +
        encodeURIComponent(user), {cache:"no-store"});
      const data = await response.json();
      if (!(ui.user && state.tab === "catalogue") || user !== ui.user || !response.ok) return;
      const done = Number(data.completed || 0), total = Number(data.total || 0);
      const queued = Number(data.queued || 0), retrying = Number(data.deferred || 0);
      const failed = Number(data.failed || 0), atTail = Number(data.tail_queued || 0);
      byId("approvalFailedCount").textContent = num(failed);
      if (byId("approvalFailedSection").open && ui.failedTotal !== failed)
        void refreshFailedJobs(true);
      byId("approvalCacheCompleted").textContent = num(done) + " / " + num(total);
      byId("approvalCacheQueue").textContent = num(queued) + " / " + num(total);
      byId("approvalCacheDeferred").textContent = num(retrying);
      byId("approvalCacheSize").textContent = bytes(data.cache_bytes);
      byId("approvalCacheProgress").max = Math.max(1, total);
      byId("approvalCacheProgress").value = Math.min(done, Math.max(1, total));
      const cooldown = Number(data.cooldown_seconds || 0);
      byId("approvalPrefetchStatus").textContent = !data.background
        ? "Background prefetch is stopped. Saved names remain available."
        : cooldown > 0
          ? "MusicBrainz cooldown: prefetch paused for " + cooldown + "s. Approval remains usable."
          : queued > 0
            ? "Preparing metadata for " + num(data.waiting_tracks) + " waiting tracks in the background."
            : failed > 0
              ? num(failed) + " requests disabled after repeated failures. Expand Failed searches below to retry."
              : "Background prefetch is caught up with currently discovered work.";
      byId("approvalCacheDetail").textContent =
        num(data.cache_entries) + " saved entries (" + num(data.cached) + " fresh); " +
        num(data.due) + " ready to fetch, " + num(data.running) + " running; " +
        num(atTail) + " at the queue tail, " + num(failed) + " disabled. " +
        "Shared Catalogue database: " + bytes(data.catalogue_db_bytes) + " on disk. " +
        "Total grows as new candidates are discovered.";
    } catch (_) {
      if ((ui.user && state.tab === "catalogue")) byId("approvalPrefetchStatus").textContent =
        "Could not read cache progress. Approval is still available.";
    }
    if ((ui.user && state.tab === "catalogue") && user === ui.user) {
      clearTimeout(ui.prefetchTimer);
      ui.prefetchTimer = setTimeout(refreshPrefetchStatus, 10000);
    }
  }
  function cacheCleanupNotice(message, error = false) {
    byId("approvalCacheNotice").textContent = message;
    byId("approvalCacheNotice").className = error ? "error-text" : "hint";
  }
  function cancelCacheCleanup() {
    byId("approvalCacheConfirm").hidden = true;
    if (!ui.cacheActionPending) cacheCleanupNotice();
  }
  async function prepareCacheCleanup() {
    if (!(ui.user && state.tab === "catalogue") || ui.cacheActionPending) return;
    const user = ui.user; ui.cacheActionPending = true;
    byId("approvalCacheClean").disabled = true;
    byId("approvalCacheConfirm").hidden = true;
    cacheCleanupNotice("Calculating which cache items are safe to remove…");
    try {
      const response = await fetch("/api/musicbrainz/approval-prefetch-clean-preview?user_id=" +
        encodeURIComponent(user), {cache:"no-store"});
      const plan = await response.json();
      if (!response.ok || plan.error) throw Error(plan.error || "Cleanup preview unavailable");
      if (!(ui.user && state.tab === "catalogue") || user !== ui.user) return;
      byId("approvalCacheImpact").textContent =
        "Remove " + num(plan.remove_entries) + " unused cached items (" +
        bytes(plan.reclaim_bytes) + ") and " + num(plan.remove_queued) + " obsolete jobs. " +
        "Keep " + num(plan.keep_entries) + " cached items and " +
        num(plan.keep_queued) + " jobs, protecting " +
        num(plan.protected_recordings) + " recordings, " +
        num(plan.protected_artists) + " artists and " +
        num(plan.protected_searches) +
        " searches for downloaded or waiting music. Shared Catalogue data and audio are untouched.";
      byId("approvalCacheConfirm").hidden = false;
      cacheCleanupNotice("Review the numbers and confirm, or cancel.");
    } catch (error) {
      if ((ui.user && state.tab === "catalogue") && user === ui.user)
        cacheCleanupNotice(error.message || String(error), true);
    } finally {
      ui.cacheActionPending = false;
      byId("approvalCacheClean").disabled = false;
    }
  }
  async function performCacheCleanup() {
    if (!(ui.user && state.tab === "catalogue") || ui.cacheActionPending || byId("approvalCacheConfirm").hidden) return;
    const user = ui.user; ui.cacheActionPending = true;
    byId("approvalCacheConfirmButton").disabled = true;
    byId("approvalCacheClean").disabled = true;
    cacheCleanupNotice("Removing unused cache data and obsolete jobs…");
    try {
      const response = await fetch("/api/musicbrainz/approval-prefetch-clean", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({user_id:user,confirmed:true}), cache:"no-store"
      });
      const result = await response.json();
      if (!response.ok || result.error) throw Error(result.error || "Cleanup failed");
      if (!(ui.user && state.tab === "catalogue") || user !== ui.user) return;
      byId("approvalCacheConfirm").hidden = true;
      cacheCleanupNotice("Removed " + num(result.remove_entries) + " cached items (" +
        bytes(result.reclaim_bytes) + ") and " + num(result.remove_queued) +
        " queued jobs. Local and pending music metadata was retained.");
      clearTimeout(ui.prefetchTimer); ui.prefetchTimer = null;
      void refreshPrefetchStatus();
    } catch (error) {
      if ((ui.user && state.tab === "catalogue") && user === ui.user)
        cacheCleanupNotice(error.message || String(error), true);
    } finally {
      ui.cacheActionPending = false;
      byId("approvalCacheConfirmButton").disabled = false;
      byId("approvalCacheClean").disabled = false;
    }
  }
  async function enter() {
    if (!state.user) return;
    if (ui.user === state.user) {
      void refreshPrefetchStatus();
      return;
    }
    reset();
    ui.user = state.user;
    void refreshPrefetchStatus();
    const user = ui.user, serial = ui.serial;
    try {
      const progress = await api(`/api/catalogue/progress?user_id=${encodeURIComponent(user)}`);
      if (!current(user, serial)) return;
      displayProgress(progress);
    } catch (error) { if (current(user, serial)) message(error.message, true); }
  }
  async function search(online = false) {
    const user = state.user, serial = ++ui.serial;
    const q = byId("catalogueQuery").value.trim(), kind = byId("catalogueKind").value;
    const local_only = byId("catalogueLocalOnly").checked;
    if (q.length < 2) { message("Enter at least two characters.", true); return; }
    message(online ? "Searching MusicBrainz with shared API rate limiting..." : "Searching cached catalogue...");
    const params = new URLSearchParams({user_id: user, kind, q, online: String(online), local_only: String(local_only)});
    try {
      const result = await api(`/api/catalogue/search?${params}`);
      if (!current(user, serial)) return;
      ui.results = result.items || [];
      const body = byId("catalogueResults");
      body.replaceChildren(...ui.results.map(renderResult));
      body.scrollTop = 0;
      if (!ui.results.length) body.append(element("p",
        local_only
          ? "No linked local entries found. Index downloaded recordings to build missing relationships, or uncheck the filter to browse the full cache."
          : "No matching catalogue entries found.", "hint"));
      const scope = local_only ? "linked to your library" : "in the public catalogue";
      message(`${ui.results.length} ${online ? "MusicBrainz" : "cached"} results ${scope}.` +
        (local_only ? " Links can be incomplete until indexing finishes." :
          online ? " Results are also saved in the cache." : " Use Search MusicBrainz to find more."));
    } catch (error) { if (current(user, serial)) message(error.message, true); }
  }
  function renderResult(item) {
    const row = element("article", null, "catalogue-entry");
    row.append(element("h4", item.name));
    row.append(element("p", `${item.kind} · ${item.id}`, "hint"));
    const actions = element("div", null, "row");
    actions.append(button("Select", () => openEntity(item.kind, item.id), false));
    const link = element("a", "MusicBrainz");
    link.href = `https://musicbrainz.org/${item.kind}/${item.id}`;
    link.rel = "noopener noreferrer"; link.target = "_blank";
    actions.append(link);
    row.append(actions);
    return row;
  }
  async function openEntity(kind, id) {
    const user = state.user, serial = ui.serial;
    const box = byId("catalogueDetail");
    box.replaceChildren(element("p", `Loading ${kind} details...`, "hint"));
    message(`Selecting ${kind} ${id}...`);
    const data = await api(`/api/catalogue/entity?${new URLSearchParams({user_id: user, kind, id})}`);
    if (!current(user, serial)) return;
    ui.detail = data;
    box.replaceChildren();
    box.append(element("h4", data.entity?.name || `${kind} ${id}`));
    box.append(element("p", `${kind} · ${id}`, "hint"));
    const actionRow = element("div", null, "row");
    actionRow.append(button("Sync / refresh", async () => {
      const result = await api("/api/catalogue/sync", {user_id: state.user, kind, id, force: true});
      message(`Queued ${kind} refresh; use Refresh progress.`, false);
      if (result.entity) await openEntity(kind, id);
    }));
    actionRow.append(button("Show in graph", () => {
      byId("graphFocus").value = id;
      setTab("graph"); window.GraphUI?.refresh();
    }));
    box.append(actionRow);
    const scopes = data.scopes || [];
    if (scopes.length) {
      const list = element("ul", null, "catalogue-detail-list");
      for (const scope of scopes) list.append(element("li", `${scope.section}: ${scope.state} ${scope.total == null ? "" : `(${scope.total} entries)`} · ${scope.last_complete_at || "not complete"}${scope.last_error ? ` · ${scope.last_error}` : ""}`));
      box.append(list);
    }
    const records = data.tracks || [];
    if (kind === "release") {
      box.append(element("h4", `Tracklist (${records.length} cached tracks)`));
      if (!records.length) box.append(element("p", "Sync this release to fetch its complete tracklist.", "hint"));
      const list = element("ol", null, "catalogue-detail-list");
      for (const track of records) {
        const row = element("li", `${track.medium}.${track.position} ${track.title} · ${track.recording_id}`);
        row.append(button("Recording", () => openEntity("recording", track.recording_id)));
        list.append(row);
      }
      box.append(list);
    }
    if (data.relations?.length) {
      box.append(element("h4", `Relationships (${data.relations.length} displayed)`));
      const list = element("ul", null, "catalogue-detail-list");
      for (const link of data.relations) {
        const row = element("li", `${link.relation}: ${link.name || link.id} (${link.kind}) `);
        row.append(button("Open", () => openEntity(link.kind, link.id)));
        list.append(row);
      }
      box.append(list);
    }
    if (data.entity?.data) {
      const raw = element("details");
      raw.append(element("summary", "MusicBrainz metadata"));
      raw.append(element("pre", JSON.stringify(data.entity.data, null, 2)));
      box.append(raw);
    }
    message(`Selected ${data.entity?.name || kind}. Details are shown on the right.`);
    // The selected entry is beside search on desktop and below it on mobile.
    if (window.innerWidth <= 850) {
      box.closest(".catalogue-selection-card")?.scrollIntoView?.({behavior: "auto", block: "start"});
    }
  }
  function displayProgress(data) {
    const counts = Object.entries(data.counts || {}).map(([k, count]) => `${k}: ${count}`).join(" · ");
    const active = (data.active || []).slice(0, 4).map(row => `${row.kind} ${row.section}: page ${row.cursor + 1}${row.total == null ? "" : ` / ${Math.ceil(row.total / 100)}`}`).join("; ");
    message(`${counts || "Nothing queued"}${active ? ` · ${active}` : ""}`, false, "catalogueProgress");
  }
  async function refreshProgress() {
    const user = state.user, data = await api(`/api/catalogue/progress?user_id=${encodeURIComponent(user)}`);
    if (user === state.user) displayProgress(data);
  }
  function setResultsHeight() {
    const slider = byId("catalogueResultsHeight");
    const value = Math.min(720, Math.max(200, Number(slider.value) || 360));
    byId("catalogueResults").style.maxHeight = `${value}px`;
    byId("catalogueResultsHeightValue").textContent = `${value} px`;
  }
  byId("approvalFailedSection").addEventListener("toggle", () => {
    if (byId("approvalFailedSection").open) void refreshFailedJobs(true);
  });
  byId("approvalFailedMore").addEventListener("click", () => {
    void refreshFailedJobs(false);
  });
  byId("approvalCacheClean").addEventListener("click", prepareCacheCleanup);
  byId("approvalCacheConfirmButton").addEventListener("click", performCacheCleanup);
  byId("approvalCacheCancel").addEventListener("click", cancelCacheCleanup);
  byId("catalogueResultsHeight").addEventListener("input", setResultsHeight);
  setResultsHeight();
  byId("catalogueLocalOnly").addEventListener("change", () => {
    if (byId("catalogueQuery").value.trim().length >= 2) {
      // The online search is already cached; toggling does not re-query MusicBrainz.
      search(false).catch(error => message(error.message, true));
    }
  });
  byId("catalogueSearchForm").addEventListener("submit", event => { event.preventDefault(); search(false).catch(error => message(error.message, true)); });
  byId("catalogueOnlineSearch").addEventListener("click", () => search(true).catch(error => message(error.message, true)));
  byId("catalogueRefreshProgress").addEventListener("click", () => refreshProgress().catch(error => message(error.message, true)));
  byId("catalogueIndexLibrary").addEventListener("click", async () => {
    if (!confirm("Queue local recordings for rate-limited background MusicBrainz indexing? A large library may take hours; audio is not modified.")) return;
    try {
      const result = await api("/api/catalogue/index-library", {user_id: state.user, limit: 5000});
      message(`${result.queued_or_cached} recording identities queued or already cached.`, false);
      await refreshProgress();
    } catch (error) { message(error.message, true); }
  });
  return {enter, leave, reset, openEntity, refreshProgress};
})();
