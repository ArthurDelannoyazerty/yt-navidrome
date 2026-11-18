/* Shared public MusicBrainz catalogue; local album edits require a fresh preview. */
"use strict";
window.CatalogueUI = (() => {
  const byId = id => document.getElementById(id);
  const ui = { user: null, detail: null, results: [], recommendations: [], preview: null, busy: false, serial: 0, languageToken: 0, languageBusy: false, languageCanary: null, languageResult: null };
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
  function reset() {
    ui.user = null; ui.detail = null; ui.results = []; ui.recommendations = [];
    ui.preview = null; ui.serial++;
    for (const id of ["catalogueResults", "catalogueRecommendations", "cataloguePreview", "catalogueHistory"]) byId(id).replaceChildren();
    byId("catalogueResults").append(element("p", "Search to find cached MusicBrainz entries.", "hint"));
    byId("catalogueDetail").replaceChildren(element("p", "Select an entry on the left to browse its discography or tracklist.", "hint"));
    byId("catalogueLanguagePlan").hidden = true;
    byId("catalogueLanguageSample").replaceChildren();
    byId("catalogueLanguageStatus").textContent = "";
    ui.languageBusy = false; ui.languageToken++; ui.languageCanary = null; ui.languageResult = null;
    byId("catalogueLanguageActivity").textContent = "";
    byId("catalogueLanguageRecheck").checked = false;
    byId("catalogueLanguageVerify").hidden = true;
    for (const id of ["catalogueStatus", "catalogueAlbumStatus", "catalogueProgress"]) byId(id).textContent = "";
    byId("catalogueSources").replaceChildren();
  }
  function selectedSources() {
    return [...byId("catalogueSources").selectedOptions].map(option => option.value);
  }
  function selectedTracks() {
    return [...new Set(byId("catalogueTrackIDs").value.split(/[\s,;]+/).map(s => s.trim()).filter(Boolean))];
  }
  async function enter() {
    if (!state.user) return;
    if (ui.user === state.user) return;
    reset();
    ui.user = state.user;
    const user = ui.user, serial = ui.serial;
    try {
      const [sources, progress, history] = await Promise.all([
        api(`/api/catalogue/sources?user_id=${encodeURIComponent(user)}`),
        api(`/api/catalogue/progress?user_id=${encodeURIComponent(user)}`),
        api(`/api/catalogue/album-history?user_id=${encodeURIComponent(user)}`),
      ]);
      if (!current(user, serial)) return;
      byId("catalogueSources").replaceChildren(...sources.map(source => {
        const option = element("option", `${source.title} (${source.status})`);
        option.value = source.id;
        return option;
      }));
      displayProgress(progress); displayHistory(history);
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
  async function recommend() {
    const user = state.user, serial = ui.serial;
    const source_ids = selectedSources(), track_ids = selectedTracks();
    if (!source_ids.length && !track_ids.length) { message("Select a source or track first.", true, "catalogueAlbumStatus"); return; }
    message("Comparing cached recording-to-release relationships...", false, "catalogueAlbumStatus");
    const result = await api("/api/catalogue/recommendations", {user_id: user, release_id: "00000000-0000-4000-8000-000000000000", source_ids, track_ids});
    if (!current(user, serial)) return;
    ui.recommendations = result.candidates || [];
    ui.preview = null; byId("cataloguePreview").replaceChildren();
    const body = byId("catalogueRecommendations"); body.replaceChildren();
    if (result.missing_cache?.length) {
      const warning = element("p", `${result.missing_cache.length} recordings need catalogue relationships; use Index downloaded recordings.`, "hint");
      body.append(warning);
    }
    for (const entry of ui.recommendations) {
      const card = element("article", null, "catalogue-entry");
      card.append(element("h4", entry.album));
      card.append(element("p", `${entry.matched} of ${result.recording_count} known recordings occur on this release · ${entry.unmatched} unmatched · ${entry.release_tracks_cached} tracklist entries cached`, "hint"));
      card.append(element("p", `${entry.date || "Unknown date"} · ${entry.country || "Unknown country"} · ${entry.status || "Unknown status"}`, "hint"));
      const actions = element("div", null, "row");
      actions.append(button("Select release", () => openEntity("release", entry.release_id)));
      actions.append(button("Sync tracklist", async () => {
        await api("/api/catalogue/sync", {user_id: state.user, kind: "release", id: entry.release_id, sections: ["lookup"], force: true});
        message("Full release lookup queued. Refresh progress and compare again when complete.", false, "catalogueAlbumStatus");
      }));
      actions.append(button("Preview assignment", () => previewAlbum(entry)));
      card.append(actions); body.append(card);
    }
    message(`${result.track_count} selected local tracks, ${ui.recommendations.length} release candidates. Coverage is not proof of the correct album.`, false, "catalogueAlbumStatus");
  }
  async function previewAlbum(release) {
    const user = state.user, serial = ui.serial;
    const params = {user_id: user, release_id: release.release_id, source_ids: selectedSources(), track_ids: selectedTracks()};
    const result = await api("/api/catalogue/album-preview", params);
    if (!current(user, serial)) return;
    ui.preview = result;
    const box = byId("cataloguePreview"); box.replaceChildren();
    box.append(element("h4", `Review: ${result.album}`));
    const counts = {};
    for (const item of result.items) counts[item.status] = (counts[item.status] || 0) + 1;
    box.append(element("p", Object.entries(counts).map(([k, v]) => `${k}: ${v}`).join(" · "), "hint"));
    const outer = element("div", null, "table-scroll"), table = element("table", null, "catalogue-preview-table");
    const thead = element("thead"), header = element("tr");
    for (const title of ["Music", "Old album", "Proposed album", "Result"]) header.append(element("th", title));
    thead.append(header); table.append(thead);
    const tbody = element("tbody");
    for (const item of result.items) {
      const row = element("tr");
      for (const text of [item.track_id, item.old_release_id || "None", item.release_tags?.album || "No match", item.status]) row.append(element("td", text));
      tbody.append(row);
    }
    table.append(tbody); outer.append(table); box.append(outer);
    if (result.can_apply > 0) box.append(button(`Apply ${result.can_apply} compatible changes`, async () => {
      if (!confirm(`Update album tags on ${result.can_apply} local audio files in ${user}? Paths and recording IDs stay unchanged. Undo copies will be retained. Navidrome should be backed up.`)) return;
      const applied = await api("/api/catalogue/album-apply", {user_id: user, preview_token: result.preview_token});
      if (!current(user, serial)) return;
      message(`${applied.queued} album-only changes queued. Existing files are never moved.`, false, "catalogueAlbumStatus");
      ui.preview = null; box.replaceChildren(); await loadHistory();
    }, false));
    box.append(element("p", "Only tracks marked COMPATIBLE are queued. Busy or previously unchanged tracks are not modified.", "hint"));
    if (selectedSources().length === 1) {
      const auto = element("label");
      const checkbox = element("input"); checkbox.type = "checkbox";
      auto.append(checkbox, document.createTextNode(" Automatically use this exact release for future compatible recordings from this source (opt-in)"));
      box.append(auto);
      box.append(button("Save source album preference", async () => {
        const chosen = await api("/api/catalogue/preference", {
          user_id: state.user, source_id: selectedSources()[0], release_id: release.release_id,
          enabled: checkbox.checked,
        });
        message(chosen.auto_apply ? "Automatic album preference enabled for compatible future recordings." : "Preference saved for manual reuse; automatic application disabled.", false, "catalogueAlbumStatus");
      }));
    }
  }
  function displayHistory(rows) {
    const container = byId("catalogueHistory"); container.replaceChildren();
    for (const item of rows || []) {
      const card = element("div", null, "catalogue-entry");
      card.append(element("p", `${item.track_id} · ${item.old_release_id || "No album"} → ${item.release_id} · ${item.status}`));
      if (item.error) card.append(element("p", item.error, "error-text"));
      if (item.error && ["QUEUED", "PREPARED", "UNDO_PREPARED"].includes(item.status))
        card.append(button("Retry interrupted change", async () => {
          await api("/api/catalogue/album-retry", {user_id: state.user, change_id: item.id});
          message("Album operation requeued; previous undo snapshot is retained.", false, "catalogueAlbumStatus");
          await loadHistory();
        }));
      if (item.status === "DONE") card.append(button("Undo album change", async () => {
        if (!confirm("Restore the exact previous audio tags from the saved backup? This does not move or delete audio.")) return;
        await api("/api/catalogue/album-undo", {user_id: state.user, change_id: item.id});
        message("Undo queued; refresh the history after the worker finishes.", false, "catalogueAlbumStatus");
        await loadHistory();
      }));
      container.append(card);
    }
    if (!rows?.length) container.append(element("p", "No album changes have been applied.", "hint"));
  }
  async function loadHistory() {
    const user = state.user, data = await api(`/api/catalogue/album-history?user_id=${encodeURIComponent(user)}`);
    if (user === state.user) displayHistory(data);
  }
  const languageCurrent = (user, token) => user === state.user && token === ui.languageToken;
  const recheck = () => byId("catalogueLanguageRecheck").checked;
  const languageActivity = value => { byId("catalogueLanguageActivity").textContent = value; };
  async function refreshCanary(user, token) {
    const canary = await api("/api/catalogue/language-canary?user_id=" + encodeURIComponent(user));
    if (!languageCurrent(user, token)) return;
    ui.languageCanary = canary;
    const sameMode = Boolean(canary.include_localized) === recheck();
    byId("catalogueLanguageVerify").hidden = canary.state !== "ready" || canary.verified || !sameMode;
    byId("catalogueLanguageBulk").disabled = !ui.languageResult?.eligible || !canary.verified ||
      !sameMode || ui.languageBusy;
    if (canary.state === "running") {
      languageActivity("Canary processing: " + canary.pending + " remaining. Check Activity and review again after completion.");
    } else if (canary.state === "failed") {
      languageActivity("A canary job failed or its tags are missing. Check Activity before retrying.");
    } else if (canary.verified && sameMode) {
      languageActivity("Navidrome check acknowledged; remaining tracks can be queued.");
    } else if (canary.state === "ready" && sameMode) {
      languageActivity("Canary finished. After Navidrome scans, verify IDs, favorites, ratings and playlists, then acknowledge.");
    }
  }
  async function previewEnglishNames() {
    if (ui.languageBusy || !state.user) return;
    const user = state.user, token = ++ui.languageToken, includeLocalized = recheck();
    ui.languageBusy = true;
    byId("catalogueLanguagePreview").disabled = true;
    byId("catalogueLanguagePlan").hidden = true;
    languageActivity("Review started. Reading local Opus tags; detailed events appear in Activity.");
    message("Checking approved local audio without changing files...", false, "catalogueLanguageStatus");
    let polling = true;
    const progressTask = (async () => {
      while (polling && languageCurrent(user, token)) {
        try {
          const progress = await api("/api/catalogue/language-progress?user_id=" + encodeURIComponent(user));
          if (polling && languageCurrent(user, token) && progress.state === "running") {
            languageActivity("Reviewing local tags: " + progress.processed + "/" + (progress.total || "…") +
              " checked; " + progress.eligible + " eligible so far. See Activity for details.");
          }
        } catch (_) { /* Primary review request reports errors. */ }
        if (polling) await new Promise(resolve => setTimeout(resolve, 1100));
      }
    })();
    try {
      const params = new URLSearchParams({user_id: user, include_localized: String(includeLocalized)});
      const result = await api("/api/catalogue/language-preview?" + params);
      if (!languageCurrent(user, token)) return;
      ui.languageResult = result;
      const omitted = result.skipped || {};
      byId("catalogueLanguageCounts").textContent =
        result.eligible + " eligible of " + (result.scanned ?? "scanned") + " tracks. Already tagged: " +
        (omitted.already_localized || 0) + "; busy/manually edited: " +
        (omitted.manual_or_busy || 0) + "; missing/unsafe: " + (omitted.invalid_or_missing || 0) + ".";
      const examples = byId("catalogueLanguageSample");
      examples.replaceChildren(...(result.sample || []).map(track => {
        const item = element("div", null, "catalogue-language-example");
        const proposed = element("strong", track.proposed_name || track.current_name);
        const original = element("small", track.canonical_name && track.canonical_name !== track.proposed_name
          ? "Original: " + track.canonical_name : "Current: " + track.current_name);
        const note = element("small", track.prediction || "");
        item.append(proposed, original, note);
        if (track.album) item.append(element("small", "Album: " + track.album));
        item.title = track.id;
        return item;
      }));
      byId("catalogueLanguagePlan").hidden = false;
      byId("catalogueLanguageCanary").disabled = !result.eligible;
      message("Read-only review finished. No audio or tags changed. Suggested names use cached MusicBrainz data; final names are checked when applied.",
        false, "catalogueLanguageStatus");
      languageActivity("Review complete: " + (result.scanned ?? result.eligible) + " checked; " + result.eligible +
        " eligible. See Activity for details.");
      await refreshCanary(user, token);
    } catch (error) {
      if (languageCurrent(user, token)) {
        message(error.message, true, "catalogueLanguageStatus");
        languageActivity("Review failed. No tags were modified; inspect Activity for the error.");
      }
    } finally {
      polling = false;
      if (languageCurrent(user, token)) {
        ui.languageBusy = false;
        byId("catalogueLanguagePreview").disabled = false;
      }
      void progressTask;
    }
  }
  async function applyEnglishNames(limit) {
    if (ui.languageBusy || !ui.languageResult) return;
    const user = state.user, token = ui.languageToken, canary = limit === 3;
    if (!confirm(canary
      ? "Backed up music, importer and Navidrome? Queue up to THREE English-name updates for checking before bulk?"
      : "Navidrome song IDs, ratings, favorites and playlists checked after the canary? Backups available? Queue remaining eligible English-name updates?")) return;
    ui.languageBusy = true;
    byId("catalogueLanguageCanary").disabled = true;
    byId("catalogueLanguageBulk").disabled = true;
    message("Queuing English-name updates...", false, "catalogueLanguageStatus");
    try {
      const result = await api("/api/catalogue/language-apply",
        {user_id: user, limit, confirmed: true, include_localized: recheck()});
      if (!languageCurrent(user, token)) return;
      message(result.queued + " track(s) queued. The worker updates display/original tags on stable files; no download or MusicBrainz ID changes. Watch Activity for completion/errors.",
        false, "catalogueLanguageStatus");
      languageActivity(result.queued + " English-name jobs queued. Review Activity; check Navidrome after scanning.");
      await refreshCanary(user, token);
    } catch (error) {
      if (languageCurrent(user, token)) message(error.message, true, "catalogueLanguageStatus");
    } finally {
      ui.languageBusy = false;
      if (languageCurrent(user, token)) {
        byId("catalogueLanguagePreview").disabled = false;
        await refreshCanary(user, token).catch(error =>
          message(error.message, true, "catalogueLanguageStatus"));
      }
    }
  }
  async function verifyEnglishCanary() {
    if (ui.languageBusy || !state.user) return;
    const user = state.user, token = ui.languageToken;
    if (!confirm("I checked the canary tracks in Navidrome after scanning: song IDs, favorites, ratings and playlists remain intact.")) return;
    ui.languageBusy = true;
    try {
      await api("/api/catalogue/language-canary-verify", {user_id: user, confirmed: true});
      if (!languageCurrent(user, token)) return;
      message("Canary verified. Remaining eligible tracks can now be updated without changing MusicBrainz IDs or file paths.",
        false, "catalogueLanguageStatus");
      await refreshCanary(user, token);
    } catch (error) {
      if (languageCurrent(user, token)) message(error.message, true, "catalogueLanguageStatus");
    } finally {
      ui.languageBusy = false;
      if (languageCurrent(user, token)) await refreshCanary(user, token).catch(error =>
        message(error.message, true, "catalogueLanguageStatus"));
    }
  }

  function setResultsHeight() {
    const slider = byId("catalogueResultsHeight");
    const value = Math.min(720, Math.max(200, Number(slider.value) || 360));
    byId("catalogueResults").style.maxHeight = `${value}px`;
    byId("catalogueResultsHeightValue").textContent = `${value} px`;
  }
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
  byId("catalogueRecommend").addEventListener("click", () => recommend().catch(error => message(error.message, true, "catalogueAlbumStatus")));
  byId("catalogueHistoryRefresh").addEventListener("click", () => loadHistory().catch(error => message(error.message, true, "catalogueAlbumStatus")));
  byId("catalogueRefreshProgress").addEventListener("click", () => refreshProgress().catch(error => message(error.message, true)));
  byId("catalogueLanguagePreview").addEventListener("click", () =>
    previewEnglishNames().catch(error => message(error.message, true, "catalogueLanguageStatus")));
  byId("catalogueLanguageCanary").addEventListener("click", () => applyEnglishNames(3));
  byId("catalogueLanguageBulk").addEventListener("click", () => applyEnglishNames(5000));
  byId("catalogueLanguageVerify").addEventListener("click", verifyEnglishCanary);
  byId("catalogueLanguageRecheck").addEventListener("change", () => {
    ui.languageResult = null; byId("catalogueLanguagePlan").hidden = true;
    message("Review again to include or exclude already tagged tracks.", false, "catalogueLanguageStatus");
  });
  byId("catalogueIndexLibrary").addEventListener("click", async () => {
    if (!confirm("Queue local recordings for rate-limited background MusicBrainz indexing? A large library may take hours; audio is not modified.")) return;
    try {
      const result = await api("/api/catalogue/index-library", {user_id: state.user, limit: 5000});
      message(`${result.queued_or_cached} recording identities queued or already cached.`, false);
      await refreshProgress();
    } catch (error) { message(error.message, true); }
  });
  return {enter, reset, openEntity, refreshProgress};
})();
