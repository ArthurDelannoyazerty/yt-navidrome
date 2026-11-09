/* Per-track covers: automatic selection, previewed manual pins, and no album-wide edits. */
(() => {
  const ui = {track: null, user: null, context: null, selected: null, framing: "original", serial: 0, busy: false, timer: null, controller: null, polls: 0};
  const message = (text, error = false) => {
    $("artworkStatus").textContent = text;
    $("artworkStatus").classList.toggle("error-text", error);
  };
  const active = serial => serial === ui.serial && ui.user === state.user && $("artworkDialog").open;
  function reset() {
    ui.serial += 1; clearTimeout(ui.timer); ui.controller?.abort();
    ui.track = null; ui.context = null; ui.selected = null; ui.busy = false;
    $("artworkUpload").value = ""; $("artworkDialog").close();
  }
  function buttons() {
    const disabled = ui.busy || !ui.context?.can_edit || ui.context?.working;
    $("artworkApply").disabled = disabled || !ui.selected;
    for (const id of ["artworkAuto", "artworkRefresh", "artworkUpload"]) $(id).disabled = disabled;
    $("artworkFraming").disabled = disabled || !ui.selected;
    for (const input of $("artworkCandidates").querySelectorAll("input")) input.disabled = disabled;
  }
  function preview() {
    const candidate = ui.context?.items.find(item => item.id === ui.selected);
    $("artworkPreviewPanel").hidden = !candidate;
    if (candidate) {
      $("artworkPreview").src = candidate.image_url + "&framing=" + encodeURIComponent(ui.framing);
      $("artworkPreview").alt = `Preview: ${candidate.label}`;
      $("artworkPreviewCaption").textContent = `${candidate.label} · ${candidate.width} × ${candidate.height}`;
    }
    buttons();
  }
  function render(data) {
    ui.context = data;
    $("artworkTitle").textContent = data.title;
    $("artworkMode").textContent = data.preference?.mode === "manual"
      ? "Pinned image. It stays selected during metadata changes and redownloads."
      : "Automatic selection. Suitable album or embedded art first, then the best available YouTube thumbnail.";
    $("artworkCurrent").hidden = false; $("artworkCurrent").src = data.current_image_url;
    $("artworkCurrentEmpty").hidden = true;
    $("artworkCandidates").replaceChildren(...data.items.map(item => {
      const card = node("article", null, "artwork-card"), label = node("label");
      const image = node("img"); image.src = item.image_url + "&framing=" + item.recommended_framing;
      image.alt = item.label; image.loading = "lazy";
      const input = node("input"); input.type = "radio"; input.name = "artworkCandidate";
      input.value = item.id; input.checked = item.id === ui.selected;
      input.addEventListener("change", () => {
        if (!input.checked) return;
        ui.selected = item.id; ui.framing = item.recommended_framing;
        $("artworkFraming").value = ui.framing; preview();
      });
      label.append(image, input, node("strong", item.label));
      card.append(label, node("small", `${item.source_type} · ${item.width} × ${item.height}${item.selected ? " · Selected" : ""}`), node("small", item.explanation));
      if (item.source_url) {
        const link = node("a", "Source video"); link.href = item.source_url; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link);
      }
      return card;
    }));
    if (!data.items.length) $("artworkCandidates").append(node("p", "No cached alternatives yet. Find covers checks associated YouTube videos. You can also upload an image.", "empty"));
    if (ui.selected && !data.items.some(item => item.id === ui.selected)) ui.selected = null;
    if (data.error) message(data.error + " Use Retry on the music row before another change.", true);
    else if (data.working) message("Artwork queued or processing. Your music remains playable.");
    else if (!data.can_edit) message("Finish the current operation before changing artwork.");
    preview();
  }
  async function load(serial = ui.serial, keepMessage = false) {
    ui.controller?.abort(); const controller = new AbortController(); ui.controller = controller;
    const response = await fetch(`/api/tracks/${encodeURIComponent(ui.track.id)}/artwork?user_id=${encodeURIComponent(ui.user)}`, {cache: "no-store", signal: controller.signal});
    const data = await response.json();
    if (!active(serial)) return;
    if (!response.ok) throw new Error(data.error || "Artwork could not be loaded");
    if (!keepMessage && !data.working) message("");
    render(data); clearTimeout(ui.timer);
    if (data.working && ui.polls++ < 120) ui.timer = setTimeout(() => load(serial).catch(error => {
      if (active(serial) && error.name !== "AbortError") message(error.message, true);
    }), 1500);
    else if (data.working) message("Artwork is still queued. Close and reopen this dialog to check progress, or inspect Activity.");
  }
  async function open(track) {
    reset(); ui.track = track; ui.user = state.user; ui.polls = 0;
    const serial = ui.serial;
    $("artworkTitle").textContent = track.matched_title || track.title;
    $("artworkCandidates").replaceChildren(); $("artworkPreviewPanel").hidden = true;
    $("artworkCurrent").removeAttribute("src"); $("artworkCurrent").hidden = true;
    $("artworkCurrentEmpty").hidden = false; $("artworkMode").textContent = "";
    $("artworkDialog").showModal(); message("Loading artwork..."); buttons();
    try { await load(serial); }
    catch (error) { if (active(serial) && error.name !== "AbortError") message(error.message, true); }
  }
  async function apply(selection, refresh = false) {
    if (ui.busy || !ui.context?.can_edit) return;
    const serial = ui.serial, context = ui.context, user = ui.user;
    ui.busy = true; buttons(); message(refresh ? "Finding covers from associated videos..." : "Queuing selected artwork...");
    try {
      const response = await fetch(`/api/tracks/${encodeURIComponent(ui.track.id)}/artwork`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({user_id: user, revision: context.revision, selection, framing: ui.framing, refresh}),
      });
      const data = await response.json();
      if (!active(serial)) return;
      if (!response.ok) throw new Error(data.error || "Artwork was not queued");
      ui.busy = false; ui.polls = 0; message(data.message); await load(serial, true);
      if (active(serial)) await refreshTracks(false);
    } catch (error) { if (active(serial)) message(error.message, true); }
    finally { if (active(serial)) { ui.busy = false; buttons(); } }
  }
  $("artworkCurrent").addEventListener("error", () => { $("artworkCurrent").hidden = true; $("artworkCurrentEmpty").hidden = false; });
  $("artworkClose").addEventListener("click", reset);
  $("artworkDialog").addEventListener("cancel", reset);
  $("artworkDialog").addEventListener("close", () => {
    // A delayed close event from the previous opening must not cancel a new one.
    if (!$("artworkDialog").open) { clearTimeout(ui.timer); ui.controller?.abort(); ui.serial += 1; }
  });
  $("artworkFraming").addEventListener("change", () => { ui.framing = $("artworkFraming").value; preview(); });
  $("artworkApply").addEventListener("click", () => apply(ui.selected));
  $("artworkRefresh").addEventListener("click", () => apply(null, true));
  $("artworkAuto").addEventListener("click", () => apply("auto"));
  $("artworkUpload").addEventListener("change", async () => {
    const file = $("artworkUpload").files[0];
    if (!file || ui.busy || !ui.context?.can_edit) return;
    if (file.size > 8 * 1024 * 1024) { message("Use an image of at most 8 MiB.", true); return; }
    const serial = ui.serial; ui.busy = true; buttons(); message("Uploading an image for preview...");
    try {
      const params = new URLSearchParams({user_id: ui.user, revision_token: ui.context.revision, filename: file.name.slice(0, 200)});
      const response = await fetch(`/api/tracks/${encodeURIComponent(ui.track.id)}/artwork/upload?${params}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file,
      });
      const data = await response.json();
      if (!active(serial)) return;
      if (!response.ok) throw new Error(data.error || "Upload failed");
      ui.selected = data.candidate_id; ui.framing = "original"; $("artworkFraming").value = "original";
      message(data.message); await load(serial, true);
    } catch (error) { if (active(serial)) message(error.message, true); }
    finally { if (active(serial)) { ui.busy = false; $("artworkUpload").value = ""; buttons(); } }
  });
  $("fillMissingCovers").addEventListener("click", guard(async () => {
    if (!confirm(`Find artwork for idle playable tracks without embedded images in "${state.user}"? Existing covers and pins are kept. Audio is not downloaded again.`)) return;
    const user = state.user, control = $("fillMissingCovers"); control.disabled = true;
    try { const data = await api("/api/artwork/fill-missing", {user_id: user}); if (user === state.user) { notice(data.message); await refreshTracks(false); } }
    finally { control.disabled = false; }
  }));
  window.ArtworkUI = {open, reset};
})();
