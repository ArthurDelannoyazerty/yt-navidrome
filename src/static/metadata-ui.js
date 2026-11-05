/* Continuous, revision-checked review with retained source metadata and independent search. */
"use strict";
window.MetadataUI = (() => {
  const ui = {context: null, user: null, track: null, serial: 0, loadSerial: 0, timer: null,
    controller: null, page: 1, maxPage: 1, artistId: null, selected: null, choiceIndex: null,
    pane: "suggestions", sending: false, loading: false, retryUntil: 0, effectiveQuery: null,
    searchLoaded: false, queue: null, manualOriginal: {}, storageWarned: false};
  const manualFields = {artist: "metadataManualArtist", title: "metadataManualTitle", album: "metadataManualAlbum",
    mbid: "metadataManualMbid", release_id: "metadataManualRelease"};
  const paneNames = ["suggestions", "search", "source", "manual"];
  const paneId = name => "metadata" + name[0].toUpperCase() + name.slice(1);
  const list = value => Array.isArray(value) ? value.join("; ") : value || "";
  const score = choice => choice.match_score ?? choice.similarity;
  const choices = () => ui.context?.choices || ui.context?.candidates || [];
  const selectedChoice = () => choices().find((choice, index) => (choice.index ?? index) === ui.choiceIndex);

  function invalidate() {
    clearTimeout(ui.timer); ui.timer = null;
    ui.serial += 1; ui.controller?.abort(); ui.controller = null;
  }
  function stopAudio() {
    const audio = $("metadataAudio");
    audio.pause?.(); audio.removeAttribute?.("src"); audio.load?.();
  }
  function reset() {
    invalidate(); ui.loadSerial += 1; stopAudio();
    ui.context = null; ui.track = null; ui.user = null; ui.selected = null; ui.choiceIndex = null;
    ui.artistId = null; ui.queue = null; ui.sending = false; ui.loading = false;
    ui.retryUntil = 0; ui.effectiveQuery = null; ui.searchLoaded = false;
    $("metadataDialog").close();
    $("mbResults").replaceChildren(); $("sourceMetadataValues").replaceChildren();
    $("metadataDiagnostic").replaceChildren(); $("metadataSuggestions").replaceChildren();
    $("metadataNotice").textContent = ""; $("metadataReload").hidden = true;
    updateLaunch();
  }
  function valid(serial, user) {
    return serial === ui.serial && user === state.user && user === ui.user && Boolean(ui.context);
  }
  function feedback(message, error = false) {
    $("metadataNotice").textContent = message;
    $("metadataNotice").className = error ? "error-text" : "hint";
    $("metadataReload").hidden = !error || !ui.track;
  }
  function status(message, error = false) {
    $("mbStatus").textContent = message;
    $("mbStatus").className = error ? "error-text" : "hint";
  }
  function duration(seconds) {
    if (!Number.isFinite(seconds)) return "Unknown";
    const rounded = Math.round(seconds);
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
  }
  function values(id, rows) {
    $(id).replaceChildren(...rows.flatMap(([key, value]) => [node("dt", key), node("dd", list(value) || "Not available")]));
  }
  function manualChanges() {
    const changed = {};
    for (const [field, id] of Object.entries(manualFields)) {
      const value = $(id).value.trim();
      if (value && value !== (ui.manualOriginal[field] || "")) changed[field] = value;
    }
    return changed;
  }
  function canSave(name = ui.pane) {
    if (!ui.context || ui.sending || ui.loading) return false;
    if (name === "suggestions") return ui.context.state === "NEEDS_APPROVAL" && Boolean(ui.context.approval_token && selectedChoice());
    if (!ui.context.can_correct) return false;
    if (name === "search") return Boolean(ui.selected);
    if (name === "source") return Boolean(ui.context.source);
    return ui.context.health === "AVAILABLE" && Object.keys(manualChanges()).length > 0;
  }
  function updateButtons() {
    const busy = ui.sending || ui.loading;
    for (const [name, id] of [["suggestions", "metadataSuggestionApply"], ["search", "mbApply"], ["source", "sourceMetadataApply"], ["manual", "metadataManualApply"]]) {
      $(id).disabled = !canSave(name);
      $(id).hidden = Boolean(ui.queue);
    }
    for (const name of paneNames) $(paneId(name) + "Tab").disabled = busy;
    for (const id of [...Object.values(manualFields), "mbArtist", "mbEntity", "mbUseSource"]) $(id).disabled = busy;
    $("mbTitle").disabled = busy || $("mbEntity").value === "artist";
    $("metadataSaveNext").disabled = !ui.queue || !canSave() || ui.queue.index >= ui.queue.ids.length;
    $("metadataQueueBack").disabled = busy || !ui.queue || ui.queue.index <= 0;
    $("metadataQueueSkip").disabled = busy || !ui.queue || ui.queue.index >= ui.queue.ids.length;
    $("metadataQueueRestart").disabled = busy;
    $("metadataReviewSkipped").disabled = busy;
  }
  function proposal() {
    if (!ui.context) return;
    let item = null, label = "", selectedDuration = null;
    if (ui.pane === "suggestions") {
      item = selectedChoice(); label = item?.kind === "source" ? "Original source tags" : item?.kind === "asis" ? "Current tags" : "Automatic suggestion";
      selectedDuration = item?.evidence?.candidate_duration_seconds;
    } else if (ui.pane === "search") {
      item = ui.selected && {...ui.selected, mbid: ui.selected.id, album: ui.selected.releases?.find(release => release.id === $("mbRelease").value)?.title || "Official release chosen by worker"};
      label = "Selected MusicBrainz recording"; selectedDuration = Number.isFinite(item?.length_ms) ? item.length_ms / 1000 : null;
    } else if (ui.pane === "source") {
      const tags = ui.context.source?.tags;
      item = tags && {artist: list(tags.artist), title: list(tags.title), album: list(tags.album), mbid: null};
      label = "Restore original source tags";
    } else {
      item = {...ui.manualOriginal, ...manualChanges()}; label = "Edited current tags";
    }
    values("metadataProposedValues", item
      ? [["Choice", label], ["Artist", item.artist], ["Title", item.title], ["Album", item.album],
        ["Recording", item.mbid || "No MusicBrainz identity"], ["Duration", duration(selectedDuration)]]
      : [["Choice", "Choose a suggestion or select a search result."]]);
    updateButtons();
  }
  function link(kind, id, text) {
    const a = node("a", text); a.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;
    a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  }
  function aliases(item) {
    const names = new Set();
    for (const value of [item, ...(item.artists || [])]) {
      if (value.sort_name && value.sort_name !== value.name) names.add(value.sort_name);
      for (const alias of value.aliases || []) if (alias.name && alias.name !== value.name) names.add(alias.name);
    }
    return [...names].slice(0, 8).join(" · ");
  }
  function rememberButton(artist) {
    if (!artist?.id || !ui.context?.search_hints?.artist) return null;
    const current = ui.context.approved_artist_alias;
    if (current?.artist_id === artist.id) return null;
    const buttonNode = button(`Remember artist: ${artist.name}`, () => decide("artist-alias", {artist_id: artist.id, forget: false}, artist));
    buttonNode.disabled = !ui.context.can_correct || ui.sending;
    return buttonNode;
  }
  function renderAlias() {
    const area = $("metadataAlias"); area.replaceChildren();
    const alias = ui.context?.approved_artist_alias;
    if (!alias) return;
    const forget = button("Forget artist mapping", () => decide("artist-alias", {forget: true}));
    forget.disabled = !ui.context.can_correct || ui.sending;
    area.append(node("span", `Remembered for this library: ${alias.alias} → ${alias.artist_name}`), forget);
  }
  function renderRejected() {
    const rejected = ui.context?.rejected_candidates || [];
    $("metadataRejectedDetails").hidden = !rejected.length;
    $("metadataRejected").replaceChildren(...rejected.map(item => {
      const row = node("div", null, "row");
      const restore = button("Allow suggestion again", () => decide("reject-candidate", {mbid: item.mbid, reject: false}));
      restore.disabled = !ui.context.can_correct || ui.sending;
      row.append(link("recording", item.mbid, item.mbid), restore);
      return row;
    }));
  }
  function evidence(item) {
    const box = node("div", null, "match-evidence"), data = item.evidence || {};
    const parts = [];
    if (Number.isFinite(data.candidate_duration_seconds)) parts.push(`Recording ${duration(data.candidate_duration_seconds)}`);
    if (Number.isFinite(data.local_duration_seconds)) parts.push(`Audio ${duration(data.local_duration_seconds)}`);
    if (Number.isFinite(data.duration_delta_seconds)) parts.push(`Duration difference ${Math.abs(data.duration_delta_seconds).toFixed(1)} s`);
    if (data.fingerprint_supported != null) parts.push(data.fingerprint_supported ? "Fingerprint support recorded" : "No fingerprint support recorded");
    if (Number.isFinite(data.score_gap)) parts.push(`Score gap ${data.score_gap}`);
    if (parts.length) box.append(node("small", parts.join(" · ")));
    if (data.source_version_clues?.length || data.version_clues?.length) box.append(node("small", `Source version: ${(data.source_version_clues || []).join(", ") || "unspecified"} · Recording version: ${(data.version_clues || []).join(", ") || "unspecified"}`));
    if (data.version_conflict) box.append(node("strong", "Version clues differ. Listen before choosing.", "warning-text"));
    if (data.penalties?.length) box.append(node("small", `Matching differences: ${data.penalties.map(value => typeof value === "string" ? value : value.field || value.name || value.reason || "").filter(Boolean).join(", ")}`));
    return box;
  }
  function renderSuggestions() {
    const items = choices();
    $("metadataSuggestions").replaceChildren(...items.map((item, index) => {
      const entry = node("div", null, "metadata-suggestion");
      const label = node("label", null, "suggestion-choice");
      const input = node("input"); input.type = "radio"; input.name = "metadataChoice";
      input.value = String(item.index ?? index); input.checked = ui.choiceIndex === (item.index ?? index);
      input.disabled = ui.context.state !== "NEEDS_APPROVAL" || ui.sending;
      input.addEventListener("change", () => { if (input.checked) { ui.choiceIndex = Number(input.value); proposal(); } });
      const description = node("span");
      description.append(node("strong", `${item.artist ? item.artist + " — " : ""}${item.title || "Untitled"}`));
      const kind = item.kind === "source" ? "Original source tags" : item.kind === "asis" || item.asis ? "Keep current tags" : `Match ${score(item) ?? "?"}/100`;
      description.append(node("small", [kind, item.album, item.description].filter(Boolean).join(" · ")));
      label.append(input, description); entry.append(label, evidence(item));
      if (item.mbid && item.kind !== "asis" && !item.asis) {
        const reject = button("Wrong recording", () => decide("reject-candidate", {mbid: item.mbid, reject: true}), "secondary reject-candidate");
        reject.disabled = !ui.context.can_correct || ui.sending;
        entry.append(reject);
      }
      const artistIds = item.evidence?.artist_ids || [];
      if (artistIds.length === 1) {
        const remember = rememberButton({id: artistIds[0], name: item.artist || "this artist"});
        if (remember) entry.append(remember);
      }
      return entry;
    }));
    if (!items.length) $("metadataSuggestions").append(node("p", "No pending suggestions. Search MusicBrainz, use source metadata, or edit the current tags.", "empty"));
    else if (ui.context.state !== "NEEDS_APPROVAL") $("metadataSuggestions").append(node("p", "These are archived choices. Search or edit to change the current metadata.", "hint"));
  }
  function canSearch() {
    const title = $("mbTitle").value.trim(), artist = $("mbArtist").value.trim();
    return $("mbEntity").value === "artist" ? artist.length >= 2 : Boolean(ui.artistId) || title.length + artist.length >= 2;
  }
  function sourceQuery(run = true) {
    const hints = ui.context?.search_hints || {}, tags = ui.context?.source?.tags || {};
    $("mbTitle").value = (hints.title || list(tags.title)).slice(0, 200);
    $("mbArtist").value = (hints.artist || list(tags.artist)).slice(0, 200);
    ui.artistId = hints.artist_id || null;
    $("mbEntity").value = "recording"; $("mbTitle").disabled = false;
    if (run) schedule();
  }
  function clearSelection() {
    ui.selected = null;
    $("mbSelected").textContent = "Select a recording to preview the change.";
    $("mbApply").disabled = true;
    $("mbRelease").replaceChildren(); $("mbReleaseField").hidden = true;
    $("mbSelectedArtist").replaceChildren();
    proposal();
  }
  function schedule() {
    if (ui.sending || ui.loading) return;
    invalidate(); ui.page = 1; ui.effectiveQuery = null; ui.searchLoaded = false; clearSelection();
    $("mbResults").replaceChildren(); $("mbPrev").disabled = true; $("mbNext").disabled = true; $("mbPage").textContent = "";
    if (!ui.context || ui.pane !== "search") return;
    if (!canSearch()) { status("Enter a title or artist. Artist search requires an artist name."); return; }
    status("Waiting for typing to stop…");
    ui.timer = setTimeout(() => guard(search)(), Math.max(500, ui.retryUntil - Date.now()));
  }
  function selectRecording(item) {
    if (ui.sending) return;
    ui.selected = item;
    $("mbSelected").textContent = `${ui.context.current_title} → ${item.artist ? item.artist + " — " : ""}${item.title}`;
    const automatic = node("option", "Automatically choose an official release"); automatic.value = "";
    $("mbRelease").replaceChildren(automatic, ...(item.releases || []).map(release => {
      const option = node("option", [release.title, release.date, release.country, release.status].filter(Boolean).join(" | "));
      option.value = release.id; return option;
    }));
    $("mbReleaseField").hidden = false;
    $("mbSelectedArtist").replaceChildren();
    if (item.artists?.length === 1) {
      const remember = rememberButton(item.artists[0]); if (remember) $("mbSelectedArtist").append(remember);
    }
    proposal();
  }
  function resultNode(item, entity) {
    const row = node("div", null, "mb-result"), content = node("div", null, "mb-result-content");
    if (entity === "artist") {
      content.append(link("artist", item.id, item.name), node("small", [item.type, item.country, item.disambiguation].filter(Boolean).join(" | ")));
      if (aliases(item)) content.append(node("small", aliases(item), "artist-aliases"));
      row.append(content, button("Show recordings", () => {
        $("mbArtist").value = item.name; $("mbTitle").value = "";
        $("mbEntity").value = "recording"; $("mbTitle").disabled = false;
        ui.artistId = item.id; schedule();
      }));
      const remember = rememberButton(item); if (remember) row.append(remember);
    } else {
      content.append(link("recording", item.id, item.title), node("div", item.artist || "Artist unknown"),
        node("small", [duration(Number.isFinite(item.length_ms) ? item.length_ms / 1000 : null), item.first_release_date, item.disambiguation].filter(Boolean).join(" | ")),
        node("small", (item.releases || []).slice(0, 3).map(release => release.title).join(" / ")));
      if (aliases(item)) content.append(node("small", aliases(item), "artist-aliases"));
      if (item.score != null) content.append(node("small", `Search relevance: ${item.score} (text search, not a match probability)`));
      row.append(content, button("Select recording", () => selectRecording(item)));
    }
    return row;
  }
  async function search() {
    if (!ui.context || ui.pane !== "search" || !canSearch() || ui.sending) return;
    if (Date.now() < ui.retryUntil) { status(`MusicBrainz cooldown: retry in ${Math.ceil((ui.retryUntil - Date.now()) / 1000)} s.`, true); return; }
    invalidate(); clearSelection(); $("mbResults").replaceChildren();
    const serial = ui.serial, user = ui.user, entity = $("mbEntity").value;
    const controller = new AbortController(); ui.controller = controller;
    const effective = ui.page > 1 ? ui.effectiveQuery : null;
    const params = new URLSearchParams({user_id: user, entity, title: effective ? effective.title : $("mbTitle").value.trim(),
      artist: effective ? effective.artist : $("mbArtist").value.trim(), page: String(ui.page)});
    const artistId = effective ? effective.artist_id : ui.artistId;
    if (artistId && entity === "recording") params.set("artist_id", artistId);
    status("Searching MusicBrainz; shared API pacing may add a short wait…");
    $("mbPrev").disabled = true; $("mbNext").disabled = true;
    try {
      const response = await fetch(`/api/musicbrainz/search?${params}`, {cache: "no-store", signal: controller.signal});
      const data = await response.json();
      if (!valid(serial, user)) return;
      if (!response.ok) {
        if (response.status === 429) ui.retryUntil = Date.now() + 1000 * Math.max(1, Number(data.retry_after) || 5);
        status(data.error || `MusicBrainz search failed (HTTP ${response.status}).`, true); return;
      }
      ui.effectiveQuery = data.query_used || null; ui.searchLoaded = true;
      ui.maxPage = Math.max(1, Math.min(100, Math.ceil(data.total / data.limit)));
      $("mbPage").textContent = `${data.page} / ${ui.maxPage} (${data.total} results)`;
      $("mbPrev").disabled = data.page <= 1; $("mbNext").disabled = data.page >= ui.maxPage;
      $("mbResults").replaceChildren(...data.items.map(item => resultNode(item, entity)));
      const message = data.items.length ? "Check artist, duration and edition before selecting. Nothing is applied automatically."
        : "No result. Try a shorter title, or search the artist and browse recordings.";
      status([data.query_note, message].filter(Boolean).join(" "), Boolean(data.query_note && data.query_strategy === "title_only"));
    } catch (error) {
      if (valid(serial, user) && error.name !== "AbortError") status(error.message || String(error), true);
    }
  }
  function pane(name) {
    if (!paneNames.includes(name) || ui.sending) return;
    invalidate(); ui.pane = name;
    for (const option of paneNames) {
      $(paneId(option) + "Pane").hidden = name !== option;
      $(paneId(option) + "Tab").setAttribute("aria-pressed", String(name === option));
    }
    if (name === "search" && !ui.searchLoaded) schedule();
    proposal();
  }
  function diagnostic(report) {
    const box = $("metadataDiagnostic"); box.replaceChildren();
    if (!report) {
      box.append(node("p", "No archived identification report for this import. Refresh suggestions records new evidence using existing audio."));
      return;
    }
    const seed = report.diagnostics?.search_seed || {};
    box.append(node("p", `Last identification: ${time(report.at)}`), node("p", `Source input: ${report.source_title}`),
      node("p", `Search seed: ${seed.artist || "unknown artist"} — ${seed.title || "unknown title"}`),
      node("p", `Recommendation: ${report.recommendation}. Automatic candidates: ${report.candidate_count}.`),
      node("p", `Fingerprint generated: ${report.diagnostics?.fingerprint_generated ? "yes" : "not recorded"}; AcoustID identifier: ${report.diagnostics?.acoustid_id_present ? "present" : "not recorded"}.`));
    if (report.previous_mbid) box.append(node("p", `Previously applied identity: ${report.previous_title || report.previous_mbid}`));
  }
  function renderContext(data, fresh = false) {
    ui.context = data;
    $("metadataCurrent").textContent = data.current_title;
    $("metadataOrigin").textContent = `Source: ${data.source?.source_title || "No audio source yet"}`;
    $("metadataShared").hidden = data.origin_count <= 1;
    $("metadataUnavailable").hidden = data.can_correct;
    $("sourceMetadataProvenance").textContent = data.source?.basis === "original_download_tags"
      ? `Archived before tagging (${time(data.source.captured_at)}).`
      : "Original embedded tags were not archived for this import. Retained provider fields are shown; unknown fields stay empty.";
    const source = data.source?.tags || {}, current = data.current_tags || {};
    values("sourceMetadataValues", Object.entries(source));
    values("metadataOriginalValues", [["Artist", source.artist], ["Title", source.title], ["Album", source.album], ["Duration", duration(data.candidate_duration_seconds)]]);
    values("metadataCurrentValues", [["Artist", current.artist], ["Title", current.title || data.current_title], ["Album", current.album],
      ["Recording", data.current_mbid || "No MusicBrainz identity"], ["Duration", duration(data.current_duration_seconds)]]);
    const hints = data.search_hints || {};
    $("metadataSearchHint").textContent = [...(hints.notes || []), hints.artist_id ? "Search uses the confirmed artist ID." : ""].filter(Boolean).join(" ");
    if (fresh) {
      ui.choiceIndex = data.state === "NEEDS_APPROVAL" && choices().length ? (choices()[0].index ?? 0) : null;
      ui.manualOriginal = {artist: list(current.artist), title: list(current.title), album: list(current.album),
        mbid: data.current_mbid || "", release_id: data.current_release_id || ""};
      for (const [field, id] of Object.entries(manualFields)) $(id).value = ui.manualOriginal[field];
      sourceQuery(false);
      const audio = $("metadataAudio"), reviewing = data.state === "NEEDS_APPROVAL";
      audio.hidden = data.health !== "AVAILABLE" && !reviewing;
      audio.setAttribute("aria-label", reviewing ? "Listen to the audio under review" : "Listen to the current local audio");
      if (!audio.hidden) audio.src = `/api/tracks/${encodeURIComponent(data.track_id)}/audio?user_id=${encodeURIComponent(ui.user)}${reviewing ? "&candidate=true" + (data.approval_token ? "&approval_token=" + encodeURIComponent(data.approval_token) : "") : ""}`;
      $("metadataAudioNote").textContent = audio.hidden ? "No local audio is available." : reviewing ? "Audio under review. Existing library audio is retained until the change succeeds." : "Current local audio.";
      if (data.current_tags_error) $("metadataAudioNote").textContent += ` ${data.current_tags_error}`;
      const sourceLink = $("metadataSourceLink"), url = data.source_url;
      sourceLink.hidden = !url || !/^https?:\/\//i.test(url);
      if (!sourceLink.hidden) sourceLink.href = url;
      else sourceLink.removeAttribute?.("href");
    }
    renderSuggestions(); renderAlias(); renderRejected(); diagnostic(data.latest_identification); proposal();
  }
  async function loadTrack(track, initialPane = "suggestions") {
    invalidate(); stopAudio(); ui.loadSerial += 1;
    const loadSerial = ui.loadSerial, user = ui.user;
    ui.track = {...track}; ui.context = null; ui.selected = null; ui.choiceIndex = null;
    ui.loading = true; ui.sending = false; ui.searchLoaded = false; ui.effectiveQuery = null;
    ui.page = 1; ui.pane = initialPane;
    $("metadataHeading").textContent = ui.queue ? "Review your music" : "Review metadata";
    $("metadataCurrent").textContent = track.matched_title || track.title || "Loading track…";
    $("metadataWorkspace").hidden = true; $("metadataQueueComplete").hidden = true;
    $("mbResults").replaceChildren(); clearSelection();
    feedback("Loading source, current tags and suggestions…");
    updateQueue(); updateButtons();
    if (!$("metadataDialog").open) $("metadataDialog").showModal();
    try {
      const data = await api(`/api/tracks/${encodeURIComponent(track.id)}/metadata-context?user_id=${encodeURIComponent(user)}`,
        null, null, () => loadSerial === ui.loadSerial && user === state.user && user === ui.user);
      if (loadSerial !== ui.loadSerial || user !== state.user || user !== ui.user) return;
      ui.loading = false; renderContext(data, true);
      $("metadataWorkspace").hidden = false; feedback("");
      if (initialPane === "suggestions" && !choices().length) initialPane = "search";
      pane(initialPane);
    } catch (error) {
      if (loadSerial === ui.loadSerial && user === state.user && user === ui.user) {
        ui.loading = false; feedback(`${error.message || error} You can reload this track or skip it.`, true); updateButtons();
      }
    }
  }
  async function open(track, initialPane = "suggestions") {
    reset(); ui.user = state.user; await loadTrack(track, initialPane);
  }
  async function save(advance = false, name = ui.pane) {
    if (!canSave(name)) return;
    const context = ui.context, user = ui.user, loadSerial = ui.loadSerial, queue = ui.queue;
    const mode = {suggestions: "approve", search: "select_recording", source: "restore_source", manual: "retag"}[name];
    const body = {user_id: user, mode};
    let path = `/api/tracks/${encodeURIComponent(context.track_id)}`;
    if (mode === "approve") {
      body.index = ui.choiceIndex; body.approval_token = context.approval_token; path += "/action";
    } else if (mode === "retag") {
      body.revision = context.revision; body.overrides = manualChanges(); path += "/action";
    } else {
      body.revision = context.revision; path += "/metadata-correction";
      if (mode === "select_recording") {
        body.mbid = ui.selected.id; if ($("mbRelease").value) body.release_id = $("mbRelease").value;
      }
    }
    if (!advance && mode !== "approve") {
      const chosen = mode === "restore_source" ? context.source?.source_title : mode === "retag" ? "your edited tags" : `${ui.selected.artist || ""} — ${ui.selected.title}`;
      if (!confirm(`Apply ${chosen} to "${context.current_title}" in ${user}? ${mode === "restore_source" ? "Applied MusicBrainz IDs, artwork and lyrics will be removed." : "The displayed metadata change will be queued."}`)) return;
    }
    const current = () => context === ui.context && loadSerial === ui.loadSerial && user === ui.user && user === state.user && queue === ui.queue;
    ui.sending = true; invalidate(); updateButtons(); feedback("Saving the reviewed choice…");
    try {
      const data = await api(path, body, null, current);
      if (!current()) return;
      ui.sending = false;
      if (advance && queue) {
        if (!queue.completed.includes(context.track_id)) queue.completed.push(context.track_id);
        queue.skipped = queue.skipped.filter(id => id !== context.track_id);
        queue.index += 1; persistQueue(); await showQueued();
      } else {
        reset(); notice(data.message || "Metadata change queued.");
      }
      if (user === state.user) await refreshTracks(false);
    } catch (error) {
      if (current()) feedback(error.message || String(error), true);
    } finally {
      if (current()) { ui.sending = false; updateButtons(); }
    }
  }
  async function decide(endpoint, payload, artist = null) {
    if (!ui.context?.can_correct || ui.sending || ui.loading) return;
    const context = ui.context, user = ui.user, loadSerial = ui.loadSerial;
    if (artist && !confirm(`Remember "${context.search_hints.artist}" as "${artist.name}" for future searches in ${user}? Existing tags and approvals are unchanged.`)) return;
    const current = () => context === ui.context && loadSerial === ui.loadSerial && user === ui.user && user === state.user;
    ui.sending = true; invalidate(); updateButtons();
    try {
      const data = await api(`/api/tracks/${encodeURIComponent(context.track_id)}/${endpoint}`, {user_id: user, revision: context.revision, ...payload}, null, current);
      if (!current()) return;
      ui.sending = false;
      if (endpoint === "reject-candidate") ui.choiceIndex = null;
      renderContext(data);
      if (endpoint === "artist-alias") ui.artistId = data.search_hints?.artist_id || null;
      feedback(data.message || "Review decision saved.");
      await refreshTracks(false);
    } catch (error) {
      if (current()) feedback(error.message || String(error), true);
    } finally {
      if (ui.context === context && loadSerial === ui.loadSerial && user === state.user) { ui.sending = false; updateButtons(); }
    }
  }

  function scope() {
    return {user: state.user, status: $("filter").value || "ALL", q: $("search").value.trim(), sort: $("reviewSort").value || "updated"};
  }
  function storageKey(user) { return "music-review-queue-v1:" + user; }
  function sameScope(a, b) { return a.user === b.user && a.status === b.status && a.q === b.q && a.sort === b.sort; }
  function savedQueue(selectedScope) {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey(selectedScope.user)) || "null");
      if (!stored || !sameScope(stored.scope || {}, selectedScope) || !Array.isArray(stored.ids) || stored.ids.length > 10000
        || !stored.ids.length || !stored.ids.every(id => typeof id === "string" && id.length <= 200)
        || !Number.isInteger(stored.index) || stored.index < 0 || stored.index > stored.ids.length) return null;
      const ids = new Set(stored.ids);
      stored.skipped = Array.isArray(stored.skipped) ? stored.skipped.filter(id => ids.has(id)) : [];
      stored.completed = Array.isArray(stored.completed) ? stored.completed.filter(id => ids.has(id)) : [];
      return stored.index < stored.ids.length || stored.skipped.length ? stored : null;
    } catch (_) { return null; }
  }
  function persistQueue() {
    if (!ui.queue) return;
    try { localStorage.setItem(storageKey(ui.queue.scope.user), JSON.stringify(ui.queue)); ui.storageWarned = false; }
    catch (_) {
      if (!ui.storageWarned) { feedback("Review continues, but this browser could not save the resume position.", true); ui.storageWarned = true; }
    }
    updateLaunch();
  }
  function updateLaunch() {
    const selectedScope = scope(), saved = state.user && savedQueue(selectedScope);
    $("startReview").textContent = saved ? `Resume review (${Math.max(saved.ids.length - saved.index, saved.skipped.length)} left)` : "Review this view";
  }
  function updateQueue() {
    const queue = ui.queue;
    $("metadataQueueBar").hidden = !queue; $("metadataQueueActions").hidden = !queue;
    if (!queue) return;
    $("metadataQueueProgress").textContent = queue.index < queue.ids.length ? `Track ${queue.index + 1} of ${queue.ids.length}` : `${queue.ids.length} tracks visited`;
    $("metadataQueueScope").textContent = `${queue.scope.user} · ${queue.scope.status.replaceAll("_", " ")}${queue.scope.q ? " · “" + queue.scope.q + "”" : ""} · ${queue.scope.sort === "artist" ? "A–Z (artist / title)" : "Recently updated"} · ${queue.completed.length} saved · ${queue.skipped.length} skipped${ui.storageWarned ? " · Resume unavailable in this browser" : ""}`;
    updateButtons();
  }
  async function showQueued() {
    const queue = ui.queue;
    if (!queue || queue.scope.user !== state.user) return;
    if (!$("metadataDialog").open) $("metadataDialog").showModal();
    updateQueue();
    if (queue.index >= queue.ids.length) {
      invalidate(); stopAudio(); ui.loadSerial += 1; ui.context = null; ui.track = null; ui.loading = false;
      $("metadataWorkspace").hidden = true; $("metadataQueueComplete").hidden = false;
      $("metadataQueueSummary").textContent = `${queue.completed.length} reviewed choice${queue.completed.length === 1 ? "" : "s"} saved; ${queue.skipped.length} track${queue.skipped.length === 1 ? "" : "s"} left unchanged for later.`;
      $("metadataReviewSkipped").hidden = !queue.skipped.length;
      feedback(""); persistQueue(); updateButtons(); return;
    }
    await loadTrack({id: queue.ids[queue.index]}, "suggestions");
  }
  async function startQueue(force = false) {
    if (ui.loading || ui.sending) return;
    const selectedScope = scope(), saved = !force && savedQueue(selectedScope);
    reset(); ui.user = selectedScope.user;
    if (saved) { ui.queue = saved; await showQueued(); return; }
    const loadSerial = ++ui.loadSerial, user = ui.user;
    ui.loading = true; $("metadataDialog").showModal(); $("metadataWorkspace").hidden = true;
    $("metadataQueueComplete").hidden = true; feedback("Preparing a review queue from this filter and search…"); updateQueue(); updateButtons();
    const params = new URLSearchParams({user_id: user, status: selectedScope.status, q: selectedScope.q, sort: selectedScope.sort});
    try {
      const data = await api(`/api/review/queue?${params}`, null, null, () => loadSerial === ui.loadSerial && user === state.user && user === ui.user);
      if (loadSerial !== ui.loadSerial || user !== state.user || user !== ui.user) return;
      ui.loading = false;
      ui.queue = {scope: selectedScope, ids: [...new Set(data.ids)], index: 0, completed: [], skipped: []};
      persistQueue();
      if (!ui.queue.ids.length) {
        $("metadataQueueComplete").hidden = false; $("metadataQueueSummary").textContent = `No reviewable audio in this view.${data.excluded ? " Busy tracks and tracks without audio are excluded." : ""}`;
        $("metadataReviewSkipped").hidden = true; feedback(""); updateQueue(); return;
      }
      await showQueued();
    } catch (error) {
      if (loadSerial === ui.loadSerial && user === state.user && user === ui.user) { ui.loading = false; feedback(error.message || String(error), true); updateButtons(); }
    }
  }
  async function moveQueue(delta, skip = false) {
    if (!ui.queue || ui.sending || ui.loading) return;
    const queue = ui.queue, id = queue.ids[queue.index];
    if (skip && id && !queue.completed.includes(id) && !queue.skipped.includes(id)) queue.skipped.push(id);
    queue.index = Math.max(0, Math.min(queue.ids.length, queue.index + delta));
    persistQueue(); await showQueued();
  }

  $("mbTitle").addEventListener("input", schedule);
  $("mbArtist").addEventListener("input", () => { ui.artistId = null; schedule(); });
  $("mbEntity").addEventListener("change", () => { $("mbTitle").disabled = $("mbEntity").value === "artist"; schedule(); });
  $("mbSearchForm").addEventListener("submit", guard(async event => { event.preventDefault(); await search(); }));
  $("mbUseSource").addEventListener("click", () => sourceQuery());
  $("mbPrev").addEventListener("click", guard(async () => { ui.page = Math.max(1, ui.page - 1); await search(); }));
  $("mbNext").addEventListener("click", guard(async () => { ui.page = Math.min(ui.maxPage, ui.page + 1); await search(); }));
  $("mbRelease").addEventListener("change", proposal);
  $("metadataAudio").addEventListener("error", () => {
    if (ui.context) $("metadataAudioNote").textContent = "Audio is unavailable or the reviewed candidate changed. Reload this track to listen again.";
  });
  $("mbApply").addEventListener("click", () => save(false, "search"));
  $("sourceMetadataApply").addEventListener("click", () => save(false, "source"));
  $("metadataSuggestionApply").addEventListener("click", () => save(false, "suggestions"));
  $("metadataManualApply").addEventListener("click", () => save(false, "manual"));
  for (const id of Object.values(manualFields)) $(id).addEventListener("input", proposal);
  for (const name of paneNames) $(paneId(name) + "Tab").addEventListener("click", () => pane(name));
  $("metadataClose").addEventListener("click", reset);
  $("metadataDialog").addEventListener("cancel", reset);
  $("metadataReload").addEventListener("click", guard(() => !ui.sending && !ui.loading && ui.track && loadTrack(ui.track, ui.pane)));
  $("metadataQueueBack").addEventListener("click", guard(() => moveQueue(-1)));
  $("metadataQueueSkip").addEventListener("click", guard(() => moveQueue(1, true)));
  $("metadataSaveNext").addEventListener("click", () => save(true));
  $("metadataQueueRestart").addEventListener("click", guard(() => startQueue(true)));
  $("metadataReviewSkipped").addEventListener("click", guard(async () => {
    if (!ui.queue || ui.sending || ui.loading) return;
    ui.queue = {...ui.queue, ids: [...ui.queue.skipped], index: 0, completed: [], skipped: []};
    persistQueue(); await showQueued();
  }));
  $("metadataDialog").addEventListener("keydown", event => {
    if (!ui.queue || !event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "Enter") { event.preventDefault(); guard(() => save(true))(); }
    else if (event.key === "ArrowRight") { event.preventDefault(); guard(() => moveQueue(1, true))(); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); guard(() => moveQueue(-1))(); }
  });
  updateLaunch();
  return {open, reset, search, startQueue, updateLaunch};
})();

if (state.user) guard(() => refreshTracks(false))();
