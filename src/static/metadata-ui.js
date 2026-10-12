/* Reviewed corrections: source provenance and independent MusicBrainz search. */
"use strict";
window.MetadataUI = (() => {
  const ui = {context: null, user: null, serial: 0, timer: null, controller: null,
    page: 1, maxPage: 1, artistId: null, selected: null, pane: "search", sending: false, retryUntil: 0};

  function invalidate() {
    clearTimeout(ui.timer); ui.timer = null;
    ui.serial += 1;
    ui.controller?.abort(); ui.controller = null;
  }
  function reset() {
    invalidate(); ui.context = null; ui.selected = null; ui.artistId = null;
    ui.sending = false; ui.retryUntil = 0;
    $("metadataDialog").close();
    $("mbResults").replaceChildren(); $("sourceMetadataValues").replaceChildren();
    $("metadataDiagnostic").replaceChildren();
  }
  function valid(serial, user) {
    return serial === ui.serial && user === state.user && user === ui.user && Boolean(ui.context);
  }
  function status(text, error = false) {
    $("mbStatus").textContent = text;
    $("mbStatus").className = error ? "error-text" : "hint";
  }
  function canSearch() {
    const title = $("mbTitle").value.trim(), artist = $("mbArtist").value.trim();
    return $("mbEntity").value === "artist" ? artist.length >= 2 : Boolean(ui.artistId) || title.length + artist.length >= 2;
  }
  function sourceQuery() {
    const tags = ui.context?.source?.tags || {};
    $("mbTitle").value = (tags.title || [""])[0].slice(0, 200);
    $("mbArtist").value = (tags.artist || []).join(" ").slice(0, 200);
    ui.artistId = null;
    $("mbEntity").value = "recording"; $("mbTitle").disabled = false;
    schedule();
  }
  function clearSelection() {
    ui.selected = null;
    $("mbSelected").textContent = "Select a recording to preview the change.";
    $("mbApply").disabled = true;
    $("mbRelease").replaceChildren(); $("mbReleaseField").hidden = true;
  }
  function schedule() {
    invalidate(); ui.page = 1; clearSelection();
    $("mbResults").replaceChildren(); $("mbPrev").disabled = true; $("mbNext").disabled = true;
    $("mbPage").textContent = "";
    if (!ui.context || ui.pane !== "search") return;
    if (!canSearch()) { status("Enter a title or artist. Artist search requires an artist name."); return; }
    status("Waiting for typing to stop...");
    ui.timer = setTimeout(() => guard(search)(), Math.max(500, ui.retryUntil - Date.now()));
  }
  function link(kind, id, text) {
    const a = node("a", text); a.href = `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;
    a.target = "_blank"; a.rel = "noopener noreferrer"; return a;
  }
  function selectRecording(item) {
    ui.selected = item;
    $("mbSelected").textContent = `${ui.context.current_title} \u2192 ${item.artist ? item.artist + " - " : ""}${item.title}`;
    const automatic = node("option", "Automatically choose an official release"); automatic.value = "";
    $("mbRelease").replaceChildren(automatic, ...(item.releases || []).map(release => {
      const option = node("option", [release.title, release.date, release.country, release.status].filter(Boolean).join(" | "));
      option.value = release.id; return option;
    }));
    $("mbReleaseField").hidden = false;
    $("mbApply").disabled = !ui.context.can_correct || ui.sending;
  }
  function resultNode(item, entity) {
    const row = node("div", null, "mb-result");
    const content = node("div", null, "mb-result-content");
    if (entity === "artist") {
      content.append(link("artist", item.id, item.name),
        node("small", [item.type, item.country, item.disambiguation].filter(Boolean).join(" | ")));
      row.append(content, button("Show recordings", () => {
        $("mbArtist").value = item.name; $("mbTitle").value = "";
        $("mbEntity").value = "recording"; $("mbTitle").disabled = false;
        ui.artistId = item.id; schedule();
      }));
    } else {
      const duration = Number.isFinite(item.length_ms) ? `${Math.floor(item.length_ms / 60000)}:${String(Math.floor(item.length_ms / 1000) % 60).padStart(2, "0")}` : "Duration unknown";
      content.append(link("recording", item.id, item.title), node("div", item.artist || "Artist unknown"),
        node("small", [duration, item.first_release_date, item.disambiguation].filter(Boolean).join(" | ")),
        node("small", (item.releases || []).slice(0, 3).map(r => r.title).join(" / ")));
      if (item.score != null) content.append(node("small", `Search relevance: ${item.score} (not audio confidence)`));
      row.append(content, button("Select recording", () => selectRecording(item)));
    }
    return row;
  }
  async function search() {
    if (!ui.context || ui.pane !== "search" || !canSearch()) return;
    if (Date.now() < ui.retryUntil) {
      status(`MusicBrainz cooldown: retry in ${Math.ceil((ui.retryUntil - Date.now()) / 1000)} s.`, true);
      return;
    }
    invalidate(); clearSelection(); $("mbResults").replaceChildren();
    const serial = ui.serial, user = ui.user, entity = $("mbEntity").value;
    const controller = new AbortController(); ui.controller = controller;
    const params = new URLSearchParams({user_id: user, entity, title: $("mbTitle").value.trim(),
      artist: $("mbArtist").value.trim(), page: String(ui.page)});
    if (ui.artistId && entity === "recording") params.set("artist_id", ui.artistId);
    status("Searching MusicBrainz; shared API pacing may add a short wait...");
    $("mbPrev").disabled = true; $("mbNext").disabled = true;
    try {
      const response = await fetch(`/api/musicbrainz/search?${params}`, {cache: "no-store", signal: controller.signal});
      const data = await response.json();
      if (!valid(serial, user)) return;
      if (!response.ok) {
        if (response.status === 429) ui.retryUntil = Date.now() + 1000 * Math.max(1, Number(data.retry_after) || 5);
        status(data.error || `MusicBrainz search failed (HTTP ${response.status}).`, true);
        return;
      }
      ui.maxPage = Math.max(1, Math.min(100, Math.ceil(data.total / data.limit)));
      $("mbPage").textContent = `${data.page} / ${ui.maxPage} (${data.total} results)`;
      $("mbPrev").disabled = data.page <= 1; $("mbNext").disabled = data.page >= ui.maxPage;
      $("mbResults").replaceChildren(...data.items.map(item => resultNode(item, entity)));
      status(data.items.length ? "Check artist, duration and edition before selecting. Nothing is applied automatically."
        : "No result. Remove video decorations from the title, search only the title, or search the artist and browse recordings.");
    } catch (error) {
      if (valid(serial, user) && error.name !== "AbortError") status(error.message || String(error), true);
    }
  }
  function pane(name) {
    invalidate(); ui.pane = name;
    $("metadataSearchPane").hidden = name !== "search";
    $("metadataSourcePane").hidden = name !== "source";
    $("metadataSearchTab").setAttribute("aria-pressed", String(name === "search"));
    $("metadataSourceTab").setAttribute("aria-pressed", String(name === "source"));
    if (name === "search") schedule();
  }
  function diagnostic(report) {
    const box = $("metadataDiagnostic"); box.replaceChildren();
    if (!report) {
      box.append(node("p", "No archived identification report for this import. Reprocess records evidence for future diagnosis; old approvals cannot be reconstructed."));
      return;
    }
    const seed = report.diagnostics?.search_seed || {};
    box.append(node("p", `Last identification: ${time(report.at)}`),
      node("p", `Source input: ${report.source_title}`),
      node("p", `Search seed: ${seed.artist || "unknown artist"} - ${seed.title || "unknown title"}`),
      node("p", `Recommendation: ${report.recommendation}. Automatic candidates: ${report.candidate_count}.`),
      node("p", `Fingerprint generated: ${report.diagnostics?.fingerprint_generated ? "yes" : "not recorded"}; AcoustID identifier: ${report.diagnostics?.acoustid_id_present ? "present" : "not recorded"}.`));
    if (report.previous_mbid) box.append(node("p", `Previously applied identity: ${report.previous_title || report.previous_mbid}`));
    for (const item of (report.candidates || []).slice(0, 5)) box.append(node("small", `${item.similarity ?? "?"}% | ${item.artist || ""} - ${item.title || ""}`));
  }
  async function open(track, initialPane = "search") {
    reset(); ui.user = state.user;
    const serial = ui.serial, user = ui.user;
    $("metadataHeading").textContent = "Correct music metadata";
    $("metadataCurrent").textContent = track.matched_title || track.title || "Loading track...";
    $("metadataDialog").showModal();
    $("mbApply").disabled = true; $("sourceMetadataApply").disabled = true;
    status("Loading source and current identity...");
    const data = await api(`/api/tracks/${encodeURIComponent(track.id)}/metadata-context?user_id=${encodeURIComponent(user)}`,
      null, null, () => serial === ui.serial && user === state.user);
    if (serial !== ui.serial || user !== state.user) return;
    ui.context = data; ui.page = 1; ui.selected = null;
    $("metadataCurrent").textContent = `Current: ${data.current_title}`;
    $("metadataOrigin").textContent = `Source: ${data.source?.source_title || "No audio source yet"}`;
    $("metadataShared").hidden = data.origin_count <= 1;
    $("metadataUnavailable").hidden = data.can_correct;
    $("sourceMetadataApply").disabled = !data.can_correct || !data.source;
    $("sourceMetadataProvenance").textContent = data.source?.basis === "original_download_tags"
      ? `Archived before tagging (${time(data.source.captured_at)}).`
      : "Original embedded tags were not archived for this import. The retained provider title is available; artist is derived only from an explicit 'Artist - Title'. Unknown album/date fields will be cleared, not invented.";
    const values = data.source?.tags || {};
    $("sourceMetadataValues").replaceChildren(...Object.entries(values).flatMap(([key, value]) => [node("dt", key), node("dd", value.join("; "))]));
    diagnostic(data.latest_identification); sourceQuery(); pane(initialPane);
  }
  async function apply(mode) {
    if (!ui.context?.can_correct || ui.sending) return;
    if (mode === "select_recording" && !ui.selected) return;
    const context = ui.context, user = ui.user;
    const selection = ui.selected;
    const chosen = mode === "restore_source" ? context.source?.source_title : `${selection.artist} - ${selection.title}`;
    const warning = mode === "restore_source"
      ? "Applied MusicBrainz IDs, artwork and lyrics will be removed. Only retained source values are restored."
      : "The selected recording will replace the current identity; check the edition and duration.";
    if (!confirm(`Correct "${context.current_title}" to "${chosen}" for ${user}? ${warning} Shared recordings may be merged or split; other origins may then need reingestion.`)) return;
    ui.sending = true; $("mbApply").disabled = true; $("sourceMetadataApply").disabled = true;
    const body = {user_id: user, mode, revision: context.revision};
    if (mode === "select_recording") { body.mbid = selection.id; if ($("mbRelease").value) body.release_id = $("mbRelease").value; }
    try {
      const data = await api(`/api/tracks/${encodeURIComponent(context.track_id)}/metadata-correction`, body);
      if (ui.context !== context || user !== state.user) return;
      reset(); notice(data.message); await refreshTracks(false);
    } finally {
      if (ui.context === context) {
        ui.sending = false; $("mbApply").disabled = !ui.selected;
        $("sourceMetadataApply").disabled = !context.source;
      }
    }
  }
  $("mbTitle").addEventListener("input", schedule);
  $("mbArtist").addEventListener("input", () => { ui.artistId = null; schedule(); });
  $("mbEntity").addEventListener("change", () => { $("mbTitle").disabled = $("mbEntity").value === "artist"; schedule(); });
  $("mbSearchForm").addEventListener("submit", guard(async event => { event.preventDefault(); await search(); }));
  $("mbUseSource").addEventListener("click", sourceQuery);
  $("mbPrev").addEventListener("click", guard(async () => { ui.page = Math.max(1, ui.page - 1); await search(); }));
  $("mbNext").addEventListener("click", guard(async () => { ui.page = Math.min(ui.maxPage, ui.page + 1); await search(); }));
  $("mbApply").addEventListener("click", guard(() => apply("select_recording")));
  $("sourceMetadataApply").addEventListener("click", guard(() => apply("restore_source")));
  $("metadataSearchTab").addEventListener("click", () => pane("search"));
  $("metadataSourceTab").addEventListener("click", () => pane("source"));
  $("metadataClose").addEventListener("click", reset);
  $("metadataDialog").addEventListener("cancel", reset);
  return {open, reset, search};
})();

// A very fast first API response may precede this deferred script. Repaint once
// after tools become available; the keyed row signature includes this readiness.
if (state.user) guard(() => refreshTracks(false))();
