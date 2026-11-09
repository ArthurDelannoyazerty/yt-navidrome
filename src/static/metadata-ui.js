/* Full-page approval. Provider lookup is read-only; only explicit choices enqueue changes. */
"use strict";
window.MetadataUI = (() => {
  const PICKER_PAGE_SIZE = 30;
  const ui = {user: null, context: null, track: null, tracks: [], index: -1, scope: "",
    queueSerial: 0, loadSerial: 0, searchSerial: 0, controller: null, contextController: null,
    timer: null, filterTimer: null, pickerTimer: null, loading: false, sending: false,
    queueLoading: false, page: 1, maxPage: 1, artistId: null, effectiveQuery: null,
    retryUntil: 0, pickerPage: 1, submitted: new Set(), manualOriginal: {},
    actions: {base: [], search: []}, audioKey: null, audioSerial: 0};
  const manualFields = {artist: "metadataManualArtist", title: "metadataManualTitle", album: "metadataManualAlbum",
    mbid: "metadataManualMbid", release_id: "metadataManualRelease"};
  const list = value => Array.isArray(value) ? value.join("; ") : value || "";
  const choices = () => ui.context?.choices || ui.context?.candidates || [];
  const active = () => state.tab === "approval" && ui.user === state.user && Boolean(ui.user);
  const scope = () => JSON.stringify([$("approvalFilter").value || "NEEDS_APPROVAL", $("approvalSearch").value.trim()]);
  const storageKey = user => "approval-position-v1:" + user;
  const duration = seconds => Number.isFinite(seconds)
    ? `${Math.floor(Math.round(seconds) / 60)}:${String(Math.round(seconds) % 60).padStart(2, "0")}` : "\u2014";

  function feedback(message = "", error = false) {
    $("metadataNotice").textContent = message;
    $("metadataNotice").className = error ? "error-text" : "hint";
    $("metadataReload").hidden = !error || !ui.track;
  }
  function status(message = "", error = false) {
    $("mbStatus").textContent = message;
    $("mbStatus").className = error ? "error-text" : "hint";
  }
  function invalidateSearch() {
    clearTimeout(ui.timer); ui.timer = null;
    ui.searchSerial += 1; ui.controller?.abort(); ui.controller = null;
  }
  function stopAudio() {
    ui.audioSerial += 1; ui.audioKey = null;
    const audio = $("metadataAudio");
    audio.pause?.(); audio.removeAttribute("src"); audio.load?.();
  }
  function leave() {
    invalidateSearch(); ui.contextController?.abort(); ui.contextController = null;
    clearTimeout(ui.filterTimer); clearTimeout(ui.pickerTimer);
    ui.loadSerial += 1; ui.queueSerial += 1;
    ui.loading = false; ui.queueLoading = false; ui.sending = false;
    stopAudio(); $("approvalPicker").open = false; updateButtons();
  }
  function reset() {
    leave(); ui.user = null; ui.context = null; ui.track = null; ui.tracks = []; ui.index = -1;
    ui.scope = ""; ui.submitted.clear(); ui.actions = {base: [], search: []};
    ui.retryUntil = 0; ui.pickerPage = 1;
    $("approvalFilter").value = "NEEDS_APPROVAL"; $("approvalSearch").value = "";
    $("approvalPickerSearch").value = ""; $("approvalPickerList").replaceChildren();
    $("approvalWorkspace").hidden = true; $("approvalEmpty").hidden = true;
    $("metadataManualPane").open = false;
    feedback(); status(); updateButtons();
  }
  function rememberPosition() {
    const id = ui.tracks[ui.index]?.id;
    if (!id) return;
    try { localStorage.setItem(storageKey(ui.user), JSON.stringify({scope: ui.scope, id})); } catch (_) {}
  }
  function rememberedPosition() {
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey(ui.user)) || "null");
      return saved?.scope === ui.scope && typeof saved.id === "string" ? saved.id : null;
    } catch (_) { return null; }
  }
  function manualChanges() {
    const result = {};
    for (const [field, id] of Object.entries(manualFields)) {
      const value = $(id).value.trim();
      if (value && value !== (ui.manualOriginal[field] || "")) result[field] = value;
    }
    return result;
  }
  function updateButtons() {
    const busy = ui.loading || ui.sending || ui.queueLoading || !active();
    $("approvalPrevious").disabled = busy || ui.index <= 0;
    $("approvalNext").disabled = busy || ui.index < 0 || ui.index >= ui.tracks.length - 1;
    $("approvalRefresh").disabled = ui.sending || ui.queueLoading || !active();
    $("approvalFilter").disabled = ui.sending;
    $("approvalSearch").disabled = ui.sending;
    $("approvalPosition").textContent = ui.index >= ui.tracks.length && ui.tracks.length
      ? `${ui.tracks.length} / ${ui.tracks.length}` : `${ui.index >= 0 ? ui.index + 1 : 0} / ${ui.tracks.length}`;
    const mutable = !busy && ui.context?.can_correct;
    $("sourceMetadataApply").disabled = !mutable || !ui.context?.source;
    $("metadataManualApply").disabled = !mutable || ui.context?.health !== "AVAILABLE" || !Object.keys(manualChanges()).length;
    $("metadataReload").disabled = ui.sending || ui.queueLoading;
    $("approvalArtwork").disabled = busy || !ui.track || !window.ArtworkUI;
    for (const id of [...Object.values(manualFields), "mbArtist", "mbEntity", "mbUseSource", "mbSearchNow"]) $(id).disabled = busy;
    $("mbTitle").disabled = busy || $("mbEntity").value === "artist";
    const keep = choices().find(choice => choice.asis || choice.kind === "asis");
    $("metadataKeepCurrent").hidden = !keep || ui.context?.state !== "NEEDS_APPROVAL";
    $("metadataKeepCurrent").disabled = !mutable || !keep;
    for (const action of [...ui.actions.base, ...ui.actions.search]) action.element.disabled = busy || !action.permitted;
  }
  // Capture the displayed context: stale detached rows must never submit a different track.
  function control(text, callback, permitted = true, group = "base") {
    const element = node("button", text, "secondary"); element.type = "button";
    const context = ui.context, searchSerial = ui.searchSerial;
    element.addEventListener("click", async () => {
      if (element.disabled || !active() || ui.loading || ui.sending || context !== ui.context
        || (group === "search" && searchSerial !== ui.searchSerial)) return;
      try { await callback(); } catch (error) { if (active() && context === ui.context) feedback(error.message || String(error), true); }
    });
    ui.actions[group].push({element, permitted});
    return element;
  }
  function values(id, rows) {
    $(id).replaceChildren(...rows.flatMap(([key, value]) => [node("dt", key), node("dd", list(value) || "\u2014")]));
  }
  function mbLink(kind, id, text) {
    const link = node("a", text); link.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;
    link.target = "_blank"; link.rel = "noopener noreferrer"; return link;
  }
  function aliasNames(item) {
    const names = new Set();
    for (const value of [item, ...(item.artists || [])]) {
      if (value.sort_name && value.sort_name !== value.name) names.add(value.sort_name);
      for (const alias of value.aliases || []) if (alias.name && alias.name !== value.name) names.add(alias.name);
    }
    return [...names].slice(0, 8).join(" \u00b7 ");
  }
  function rememberButton(artist, group = "base") {
    if (!artist?.id || !ui.context?.search_hints?.artist || ui.context.approved_artist_alias?.artist_id === artist.id) return null;
    return control("Remember artist", async () => {
      const sourceArtist = ui.context.approved_artist_alias?.alias || ui.context.search_hints.artist;
      if (confirm(`Remember "${sourceArtist}" as "${artist.name}" for future searches in ${ui.user}?`))
        await decide("artist-alias", {artist_id: artist.id, forget: false});
    }, ui.context.can_correct, group);
  }
  function evidence(item) {
    const details = node("details"), data = item.evidence || {};
    details.append(node("summary", "Details"));
    if (item.description) details.append(node("p", item.description));
    if (Number.isFinite(data.duration_delta_seconds)) details.append(node("p", `Duration difference: ${data.duration_delta_seconds.toFixed(1)} s`));
    const fingerprint = data.fingerprint_support;
    details.append(node("p", fingerprint === true ? "Fingerprint supports this recording" : fingerprint === false ? "Fingerprint returned other recordings" : "Fingerprint support unknown"));
    if (data.version_conflict) details.append(node("p", "Version clues differ", "warning-text"));
    if (data.source_version_clues?.length) details.append(node("p", "Source: " + data.source_version_clues.join(", ")));
    if (data.version_clues?.length) details.append(node("p", "Recording: " + data.version_clues.join(", ")));
    if (data.penalties?.length) details.append(node("p", "Differences: " + data.penalties.map(value => typeof value === "string" ? value : value.field || value.name || "").filter(Boolean).join(", ")));
    if (item.mbid) {
      const actions = node("div", null, "row");
      actions.append(control("Wrong recording", () => decide("reject-candidate", {mbid: item.mbid, reject: true}), ui.context.can_correct));
      if (data.artist_ids?.length === 1) {
        const remember = rememberButton({id: data.artist_ids[0], name: item.artist}); if (remember) actions.append(remember);
      }
      details.append(actions);
    }
    return details;
  }
  function useChoice(item) {
    return ui.context?.state === "NEEDS_APPROVAL"
      ? submit("approve", {index: item.index ?? choices().indexOf(item)})
      : item.mbid ? submit("select_recording", {mbid: item.mbid}) : null;
  }
  function renderSuggestions() {
    const matches = choices().filter(item => item.kind !== "source" && item.kind !== "asis" && !item.asis);
    $("metadataSuggestions").replaceChildren(...matches.map(item => {
      const row = node("tr"); row.dataset.choice = String(item.index ?? choices().indexOf(item));
      const title = node("td"); title.append(item.mbid ? mbLink("recording", item.mbid, item.title) : node("strong", item.title));
      if (item.album) title.append(node("small", item.album));
      title.append(evidence(item));
      const action = node("td"), use = control("Use", () => useChoice(item), ui.context.can_correct && Boolean(ui.context.state === "NEEDS_APPROVAL" || item.mbid));
      use.setAttribute("aria-label", `Use ${item.artist || ""} ${item.title}`); action.append(use);
      row.append(title, node("td", item.artist || "\u2014"), node("td", duration(item.evidence?.candidate_duration_seconds)),
        node("td", item.match_score ?? item.similarity ?? "\u2014"), action);
      row.addEventListener("click", event => {
        if (!event.target.closest("a,button,details,input,select") && !use.disabled) use.click();
      });
      return row;
    }));
    $("beetsEmpty").hidden = matches.length > 0;
  }
  function renderDecisions() {
    const alias = ui.context.approved_artist_alias;
    $("metadataAlias").replaceChildren();
    if (alias) {
      const row = node("div", null, "row"); row.append(node("span", `${alias.alias} \u2192 ${alias.artist_name}`),
        control("Forget artist mapping", () => decide("artist-alias", {forget: true}), ui.context.can_correct));
      $("metadataAlias").append(row);
    }
    const rejected = ui.context.rejected_candidates || [];
    $("metadataRejectedDetails").hidden = !rejected.length;
    $("metadataRejected").replaceChildren(...rejected.map(item => {
      const row = node("div", null, "row"); row.append(mbLink("recording", item.mbid, item.mbid),
        control("Allow suggestion again", () => decide("reject-candidate", {mbid: item.mbid, reject: false}), ui.context.can_correct));
      return row;
    }));
    const report = ui.context.latest_identification, box = $("metadataDiagnostic"); box.replaceChildren();
    if (report) {
      const seed = report.diagnostics?.search_seed || {};
      box.append(node("p", `${seed.artist || ""} \u2014 ${seed.title || report.source_title || ""}`),
        node("p", `Recommendation: ${report.recommendation || "unknown"}`));
      const gap = report.diagnostics?.top_score_gap;
      if (Number.isFinite(gap)) box.append(node("p", `Top score gap: ${gap.toFixed(1)}/100`));
    } else box.append(node("p", "No saved identification report."));
  }
  function sourceQuery() {
    const hints = ui.context?.search_hints || {}, tags = ui.context?.source?.tags || {};
    $("mbTitle").value = (hints.title || list(tags.title)).slice(0, 200);
    $("mbArtist").value = (hints.artist || list(tags.artist)).slice(0, 200);
    ui.artistId = hints.artist_id || null; $("mbEntity").value = "recording";
  }
  function syncAudio(data) {
    const audio = $("metadataAudio"), candidate = data.state === "NEEDS_APPROVAL";
    audio.hidden = data.health !== "AVAILABLE" && !candidate;
    const url = `/api/tracks/${encodeURIComponent(data.track_id)}/audio?user_id=${encodeURIComponent(ui.user)}${candidate ? "&candidate=true&approval_token=" + encodeURIComponent(data.approval_token || "") : ""}`;
    const key = [ui.user, data.track_id, candidate ? data.origin_id : "current"].join(":");
    if (!audio.hidden && audio.getAttribute("src") !== url) {
      const position = ui.audioKey === key && Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
      const resume = ui.audioKey === key && audio.paused === false;
      const serial = ++ui.audioSerial; audio.pause?.(); audio.src = url; ui.audioKey = key;
      if (position > 0 || resume) {
        audio.addEventListener("loadedmetadata", () => {
          if (serial !== ui.audioSerial || !active()) return;
          try { audio.currentTime = Number.isFinite(audio.duration) ? Math.min(position, audio.duration) : position; } catch (_) {}
          if (resume) audio.play?.()?.catch?.(() => {});
        }, {once: true}); audio.load?.();
      }
    }
    $("metadataAudioNote").textContent = data.current_tags_error || (audio.hidden ? "No local audio" : candidate ? "Audio under review" : "Current audio");
    audio.setAttribute("aria-label", candidate ? "Listen to the audio under review" : "Listen to current audio");
  }
  function renderContext(data) {
    ui.context = data; ui.actions.base = [];
    $("metadataCurrent").textContent = data.source_title || data.source?.source_title || ui.track?.source_title || data.current_title;
    $("metadataOrigin").textContent = data.source_channel || data.search_hints?.channel || ui.track?.channel || "";
    const names = {NEEDS_APPROVAL: "Waiting for approval", IDLE: "Ready", QUEUED: "Queued", RUNNING: "Processing", DEFERRED: "Deferred", FAILED: "Failed", STOPPED: "Stopped"};
    const stateBadge = badge(data.state || "UNKNOWN"); stateBadge.textContent = names[data.state] || data.state;
    $("approvalState").replaceChildren(stateBadge);
    const current = data.current_tags || {}, source = data.source?.tags || {};
    values("metadataCurrentValues", [["Artist", current.artist], ["Title", current.title], ["Album", current.album],
      ["Duration", duration(data.candidate_duration_seconds ?? data.current_duration_seconds)]]);
    values("sourceMetadataValues", Object.entries(source));
    $("sourceMetadataProvenance").textContent = data.source?.basis === "original_download_tags" ? "Retained download tags" : "Retained source fields";
    const link = $("metadataSourceLink"); link.hidden = !/^https?:\/\//i.test(data.source_url || "");
    if (!link.hidden) link.href = data.source_url; else link.removeAttribute("href");
    ui.manualOriginal = {artist: list(current.artist), title: list(current.title), album: list(current.album), mbid: data.current_mbid || "", release_id: data.current_release_id || ""};
    for (const [field, id] of Object.entries(manualFields)) $(id).value = ui.manualOriginal[field];
    syncAudio(data); renderSuggestions(); renderDecisions(); updateButtons();
  }
  async function loadTrack(track, initialPane = "") {
    invalidateSearch(); ui.contextController?.abort(); stopAudio();
    const serial = ++ui.loadSerial, user = ui.user;
    ui.track = track; ui.context = null; ui.loading = true; ui.actions = {base: [], search: []};
    $("approvalWorkspace").hidden = true; $("approvalEmpty").hidden = true;
    $("mbResults").replaceChildren(); status(); feedback("Loading track...");
    $("metadataManualPane").open = initialPane === "manual";
    updateButtons();
    const controller = new AbortController(); ui.contextController = controller;
    try {
      const response = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/metadata-context?user_id=${encodeURIComponent(user)}`, {cache: "no-store", signal: controller.signal});
      const data = await response.json();
      if (!active() || serial !== ui.loadSerial || user !== ui.user) return;
      if (!response.ok || data.error) throw new Error(data.error || "Track could not be loaded");
      ui.loading = false; renderContext(data); sourceQuery(); feedback();
      $("approvalWorkspace").hidden = false;
      const cover = $("approvalCover"); cover.hidden = false;
      cover.src = `/api/tracks/${encodeURIComponent(track.id)}/artwork/current/image?user_id=${encodeURIComponent(user)}&v=${encodeURIComponent(data.revision)}`;
      rememberPosition(); updateButtons(); scheduleSearch();
    } catch (error) {
      if (active() && serial === ui.loadSerial && user === ui.user && error.name !== "AbortError") {
        ui.loading = false; feedback(error.message || String(error), true); updateButtons();
      }
    }
  }
  async function refreshQueue(preferredId = null, explicitTrack = null, initialPane = "") {
    if (!state.user || ui.sending) return;
    invalidateSearch(); ui.contextController?.abort(); stopAudio(); ui.loadSerial += 1;
    ui.user = state.user; const user = ui.user, serial = ++ui.queueSerial;
    ui.scope = scope(); const requestedScope = ui.scope;
    ui.queueLoading = true; ui.loading = false; ui.context = null; ui.track = null;
    ui.tracks = []; ui.index = -1; $("approvalPickerList").replaceChildren();
    $("approvalWorkspace").hidden = true; $("approvalEmpty").hidden = true;
    ui.actions = {base: [], search: []}; feedback("Loading music..."); updateButtons();
    const params = new URLSearchParams({user_id: user, status: $("approvalFilter").value || "NEEDS_APPROVAL", q: $("approvalSearch").value.trim()});
    const valid = () => active() && user === ui.user && serial === ui.queueSerial && requestedScope === scope();
    try {
      const data = await api(`/api/review/queue?${params}`, null, null, valid);
      if (!valid()) return;
      ui.tracks = data.tracks || (data.ids || []).map(id => ({id, title: id}));
      if (explicitTrack && !ui.tracks.some(track => track.id === explicitTrack.id)) ui.tracks.unshift(explicitTrack);
      const selected = preferredId || rememberedPosition();
      const index = ui.tracks.findIndex(track => track.id === selected);
      ui.index = ui.tracks.length ? Math.max(0, index) : -1;
      ui.queueLoading = false; ui.submitted.clear(); ui.pickerPage = 1;
      $("approvalPickerSearch").value = ""; renderPicker(); updateButtons();
      if (ui.index >= 0) {
        await loadTrack(ui.tracks[ui.index], initialPane);
        if (valid() && data.truncated) feedback(`Showing the first ${data.limit} tracks. Narrow the filter or search.`, true);
      } else {
        feedback(); $("approvalEmpty").hidden = false;
        $("approvalEmpty").textContent = data.excluded ? "No reviewable music in this view." : "No music in this view.";
      }
    } catch (error) { if (valid()) { ui.queueLoading = false; feedback(error.message || String(error), true); updateButtons(); } }
  }
  async function enter() {
    if (!state.user || state.tab !== "approval") return;
    if (ui.user && ui.user !== state.user) reset();
    const id = ui.user === state.user ? ui.tracks[ui.index]?.id : null;
    return refreshQueue(id);
  }
  async function open(track, initialPane = "") {
    if (track.user_id && track.user_id !== state.user) return;
    leave(); setTab("approval", false);
    $("approvalFilter").value = track.operation_state && track.operation_state !== "NEEDS_APPROVAL" ? "ALL" : "NEEDS_APPROVAL";
    $("approvalSearch").value = "";
    return refreshQueue(track.id, track, initialPane);
  }
  async function navigate(index) {
    if (!active() || ui.sending || ui.queueLoading || index < 0 || index >= ui.tracks.length) return;
    ui.index = index; $("approvalPicker").open = false;
    await loadTrack(ui.tracks[index]); renderPicker();
  }
  function renderPicker() {
    if (!$("approvalPicker").open) return;
    const query = $("approvalPickerSearch").value.trim().toLocaleLowerCase();
    const items = ui.tracks.map((track, index) => ({track, index})).filter(({track}) =>
      `${track.source_title || track.title || ""}\n${track.channel || ""}`.toLocaleLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(items.length / PICKER_PAGE_SIZE));
    ui.pickerPage = Math.min(Math.max(1, ui.pickerPage), pages);
    $("approvalPickerList").replaceChildren(...items.slice((ui.pickerPage - 1) * PICKER_PAGE_SIZE, ui.pickerPage * PICKER_PAGE_SIZE).map(({track, index}) => {
      const item = node("button", null, "secondary"); item.type = "button";
      item.append(node("strong", track.source_title || track.title || "Untitled"), node("small", track.channel || ""));
      item.setAttribute("aria-current", String(index === ui.index));
      item.addEventListener("click", () => navigate(index)); return item;
    }));
    if (!items.length) $("approvalPickerList").append(node("p", "No matching music.", "hint"));
    $("approvalPickerPage").textContent = `${ui.pickerPage} / ${pages}`;
    $("approvalPickerPrevious").disabled = ui.pickerPage <= 1;
    $("approvalPickerNext").disabled = ui.pickerPage >= pages;
  }
  function canSearch() {
    const title = $("mbTitle").value.trim(), artist = $("mbArtist").value.trim();
    return $("mbEntity").value === "artist" ? artist.length >= 2 : Boolean(ui.artistId) || title.length + artist.length >= 2;
  }
  function scheduleSearch() {
    if (!active() || ui.sending || ui.loading) return;
    invalidateSearch(); ui.page = 1; ui.effectiveQuery = null; ui.actions.search = [];
    $("mbResults").replaceChildren(); $("mbPrev").disabled = true; $("mbNext").disabled = true; $("mbPage").textContent = "";
    if (!ui.context || !canSearch()) { status("Enter a title or artist."); return; }
    status("Searching..."); ui.timer = setTimeout(search, Math.max(500, ui.retryUntil - Date.now()));
  }
  function resultTable(items, entity) {
    const container = node("div", null, "table-scroll"), table = node("table", null, "approval-table");
    const head = node("thead"), headings = node("tr"), body = node("tbody");
    const labels = entity === "artist" ? ["Artist", "Details", ""] : ["Track", "Artist", "Duration", ""];
    for (const label of labels) { const th = node("th", label); th.setAttribute("scope", "col"); headings.append(th); }
    head.append(headings);
    for (const item of items) {
      const row = node("tr"), info = node("td"), action = node("td");
      if (entity === "artist") {
        info.append(mbLink("artist", item.id, item.name));
        if (aliasNames(item)) info.append(node("small", aliasNames(item)));
        action.append(control("Tracks", () => {
          $("mbArtist").value = item.name; $("mbTitle").value = ""; $("mbEntity").value = "recording";
          ui.artistId = item.id; updateButtons(); scheduleSearch();
        }, true, "search"));
        const remember = rememberButton(item, "search"); if (remember) info.append(remember);
        row.append(info, node("td", [item.type, item.country, item.disambiguation].filter(Boolean).join(" \u00b7 ")), action);
      } else {
        info.append(mbLink("recording", item.id, item.title));
        const edition = node("select"); edition.setAttribute("aria-label", `Release for ${item.title}`);
        const automatic = node("option", "Automatic release"); automatic.value = ""; edition.append(automatic);
        for (const release of item.releases || []) {
          const option = node("option", [release.title, release.date, release.country].filter(Boolean).join(" \u00b7 "));
          option.value = release.id; edition.append(option);
        }
        if (item.disambiguation) info.append(node("small", item.disambiguation));
        const details = node("details"); details.append(node("summary", "Details"));
        if (item.score != null) details.append(node("p", `Search relevance: ${item.score}`));
        if (aliasNames(item)) details.append(node("p", aliasNames(item)));
        if (item.releases?.length) details.append(edition);
        if (item.artists?.length === 1) { const remember = rememberButton(item.artists[0], "search"); if (remember) details.append(remember); }
        info.append(details);
        action.append(control("Use", () => submit("select_recording", {mbid: item.id, ...(edition.value ? {release_id: edition.value} : {})}), ui.context.can_correct, "search"));
        row.append(info, node("td", item.artist || "\u2014"), node("td", duration(Number.isFinite(item.length_ms) ? item.length_ms / 1000 : null)), action);
      }
      body.append(row);
    }
    table.append(head, body); container.append(table); return container;
  }
  async function search() {
    if (!active() || !ui.context || !canSearch() || ui.sending || ui.loading) return;
    if (Date.now() < ui.retryUntil) {
      status(`MusicBrainz retry in ${Math.ceil((ui.retryUntil - Date.now()) / 1000)} s.`);
      clearTimeout(ui.timer); ui.timer = setTimeout(search, Math.max(500, ui.retryUntil - Date.now())); return;
    }
    invalidateSearch(); ui.actions.search = []; $("mbResults").replaceChildren();
    const serial = ui.searchSerial, user = ui.user, context = ui.context, entity = $("mbEntity").value;
    const controller = new AbortController(); ui.controller = controller;
    const effective = ui.page > 1 ? ui.effectiveQuery : null;
    const params = new URLSearchParams({user_id: user, entity, title: effective ? effective.title : $("mbTitle").value.trim(),
      artist: effective ? effective.artist : $("mbArtist").value.trim(), page: String(ui.page)});
    const artistId = effective ? effective.artist_id : ui.artistId;
    if (artistId && entity === "recording") params.set("artist_id", artistId);
    const valid = () => active() && serial === ui.searchSerial && user === ui.user && context === ui.context;
    status("Searching..."); $("mbPrev").disabled = true; $("mbNext").disabled = true;
    try {
      const response = await fetch(`/api/musicbrainz/search?${params}`, {cache: "no-store", signal: controller.signal});
      const data = await response.json(); if (!valid()) return;
      if (!response.ok) {
        if (response.status === 429) {
          ui.retryUntil = Date.now() + 1000 * Math.max(1, Number(data.retry_after) || 5);
          ui.timer = setTimeout(search, Math.max(500, ui.retryUntil - Date.now()));
        }
        status(data.error || `Search failed (HTTP ${response.status}).`, true); return;
      }
      ui.effectiveQuery = data.query_used || null;
      ui.maxPage = Math.max(1, Math.min(100, Math.ceil(data.total / data.limit)));
      $("mbPage").textContent = `${data.page} / ${ui.maxPage}`;
      $("mbPrev").disabled = data.page <= 1; $("mbNext").disabled = data.page >= ui.maxPage;
      if (data.items.length) $("mbResults").append(resultTable(data.items, entity));
      status(data.query_note || (data.items.length ? "" : "No results."), data.query_strategy === "title_only");
      updateButtons();
    } catch (error) { if (valid() && error.name !== "AbortError") status(error.message || String(error), true); }
  }
  async function submit(mode, extra = {}) {
    const context = ui.context;
    if (!active() || !context?.can_correct || ui.sending || ui.loading || ui.queueLoading) return;
    if (mode === "approve" && (context.state !== "NEEDS_APPROVAL" || !context.approval_token)) return;
    const user = ui.user, serial = ui.loadSerial;
    const path = `/api/tracks/${encodeURIComponent(context.track_id)}/${mode === "approve" || mode === "retag" ? "action" : "metadata-correction"}`;
    const body = {user_id: user, mode, ...extra,
      ...(mode === "approve" ? {approval_token: context.approval_token} : {revision: context.revision})};
    const valid = () => active() && context === ui.context && serial === ui.loadSerial && user === ui.user;
    ui.sending = true; invalidateSearch(); updateButtons(); feedback("Queuing change...");
    try {
      const data = await api(path, body, null, valid); if (!valid()) return;
      ui.sending = false; ui.submitted.add(context.track_id);
      let next = ui.index + 1;
      while (next < ui.tracks.length && ui.submitted.has(ui.tracks[next].id)) next += 1;
      if (next < ui.tracks.length) await navigate(next);
      else {
        ui.index = ui.tracks.length; ui.context = null; stopAudio();
        $("approvalWorkspace").hidden = true; $("approvalEmpty").hidden = false;
        $("approvalEmpty").textContent = "No more tracks in this view.";
      }
      if (active() && user === ui.user) { feedback(data.message || "Change queued."); updateButtons(); }
      if (user === state.user) await refreshTracks(false);
    } catch (error) { if (valid()) feedback(error.message || String(error), true); }
    finally { if (valid()) { ui.sending = false; updateButtons(); scheduleSearch(); } }
  }
  async function decide(endpoint, payload) {
    const context = ui.context, serial = ui.loadSerial, user = ui.user;
    if (!active() || !context?.can_correct || ui.sending || ui.loading) return;
    const valid = () => active() && context === ui.context && serial === ui.loadSerial && user === ui.user;
    ui.sending = true; invalidateSearch(); updateButtons();
    try {
      const data = await api(`/api/tracks/${encodeURIComponent(context.track_id)}/${endpoint}`, {user_id: user, revision: context.revision, ...payload}, null, valid);
      if (!valid()) return;
      ui.sending = false; renderContext(data); feedback(data.message || "Saved.");
      if (endpoint === "artist-alias") sourceQuery();
      scheduleSearch();
      if (user === state.user) await refreshTracks(false);
    } catch (error) { if (valid()) feedback(error.message || String(error), true); }
    finally { if (valid()) { ui.sending = false; updateButtons(); } }
  }

  $("approvalPrevious").addEventListener("click", () => navigate(ui.index - 1));
  $("approvalNext").addEventListener("click", () => navigate(ui.index + 1));
  $("approvalRefresh").addEventListener("click", () => refreshQueue(ui.tracks[ui.index]?.id));
  $("approvalFilter").addEventListener("change", () => { clearTimeout(ui.filterTimer); refreshQueue(); });
  $("approvalSearch").addEventListener("input", () => {
    invalidateSearch(); ui.contextController?.abort(); ui.loadSerial += 1; ui.queueSerial += 1;
    ui.context = null; ui.loading = true; $("approvalWorkspace").hidden = true; updateButtons();
    clearTimeout(ui.filterTimer); ui.filterTimer = setTimeout(refreshQueue, 300);
  });
  $("approvalPicker").addEventListener("toggle", renderPicker);
  $("approvalPickerSearch").addEventListener("input", () => {
    clearTimeout(ui.pickerTimer); ui.pickerPage = 1; ui.pickerTimer = setTimeout(renderPicker, 150);
  });
  $("approvalPickerPrevious").addEventListener("click", () => { ui.pickerPage -= 1; renderPicker(); });
  $("approvalPickerNext").addEventListener("click", () => { ui.pickerPage += 1; renderPicker(); });
  $("metadataReload").addEventListener("click", () => ui.track ? loadTrack(ui.track) : refreshQueue());
  $("approvalCover").addEventListener("error", () => { $("approvalCover").hidden = true; });
  $("approvalArtwork").addEventListener("click", () => ui.track && window.ArtworkUI?.open(ui.track));
  $("sourceMetadataApply").addEventListener("click", () => submit("restore_source"));
  $("metadataManualApply").addEventListener("click", () => Object.keys(manualChanges()).length && submit("retag", {overrides: manualChanges()}));
  $("metadataKeepCurrent").addEventListener("click", () => {
    const choice = choices().find(item => item.asis || item.kind === "asis"); if (choice) useChoice(choice);
  });
  for (const id of Object.values(manualFields)) $(id).addEventListener("input", updateButtons);
  $("mbSearchForm").addEventListener("submit", event => { event.preventDefault(); ui.page = 1; ui.effectiveQuery = null; search(); });
  $("mbTitle").addEventListener("input", scheduleSearch);
  $("mbArtist").addEventListener("input", () => { ui.artistId = null; scheduleSearch(); });
  $("mbEntity").addEventListener("change", () => { updateButtons(); scheduleSearch(); });
  $("mbUseSource").addEventListener("click", () => { sourceQuery(); updateButtons(); scheduleSearch(); });
  $("mbPrev").addEventListener("click", () => { if (ui.page > 1) { ui.page -= 1; return search(); } });
  $("mbNext").addEventListener("click", () => { if (ui.page < ui.maxPage) { ui.page += 1; return search(); } });
  $("approvalFilter").value = "NEEDS_APPROVAL";
  return {open, enter, leave, reset, search};
})();

if (state.user) {
  guard(() => refreshTracks(false))();
  if (state.tab === "approval") guard(() => window.MetadataUI.enter())();
}
