/* Existing-library English names: read-only review, scoped canary and verified bulk. */
"use strict";
window.LanguageUI = (() => {
  const $ = id => document.getElementById(id);
  const stateUI = {user: null, serial: 0, busy: false, review: null,
    canary: null, batch: null, statusTimer: null, requestTimer: null};
  const active = (user, serial) => user === state.user && user === stateUI.user &&
    serial === stateUI.serial;
  const params = user => "user_id=" + encodeURIComponent(user);
  const recheck = () => $("languageRecheck").checked;
  const scope = () => $("languageScope").value;
  const number = value => (Number(value) || 0).toLocaleString();
  const text = (id, value, error = false) => {
    const el = $(id);
    el.textContent = value;
    el.className = error ? "error-text" : "hint";
  };
  const el = (tag, value, cls) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    if (cls) node.className = cls;
    return node;
  };
  const kinds = {
    display_changes: "Visible name may change",
    original_updates: "Original-name tags need repair",
    unknown: "Cache incomplete",
    unchanged: "No detected change",
  };
  function selectedCount() {
    const c = stateUI.review?.changes || {};
    return (c.display_changes || 0) + (scope() === "names_and_originals" ? (c.original_updates || 0) : 0);
  }
  function resetMetrics() {
    for (const id of ["languageCountScanned", "languageCountNames", "languageCountOriginals",
      "languageCountUnchanged", "languageCountUnknown"]) $(id).textContent = "—";
    $("languageExcluded").textContent = "";
    $("languageExamples").replaceChildren();
    $("languageExamplesDetails").hidden = true;
  }
  function showExamples(rows) {
    const box = $("languageExamples");
    box.replaceChildren();
    for (const track of (rows || []).slice(0, 8)) {
      const card = el("div", undefined, "language-example");
      const proposed = track.proposed_name;
      card.append(el("span", kinds[track.kind] || "Review candidate", "language-example-kind"));
      card.append(el("strong", proposed || track.current_name || "Unknown recording"));
      card.append(el("small", "Current: " + (track.current_name || "Unknown")));
      if (track.canonical_name && track.canonical_name !== proposed)
        card.append(el("small", "Canonical: " + track.canonical_name));
      if (track.album) card.append(el("small", "Confirmed album: " + track.album));
      if (track.changes?.length) card.append(el("small", "Fields: " + track.changes.join(", ")));
      if (track.prediction) card.append(el("small", track.prediction));
      card.title = track.id || "";
      box.append(card);
    }
    $("languageExamplesDetails").hidden = !box.childElementCount;
  }
  function renderReview(data) {
    stateUI.review = data;
    const c = data.changes || {};
    $("languageCountScanned").textContent = number(data.scanned);
    $("languageCountNames").textContent = number(c.display_changes);
    $("languageCountOriginals").textContent = number(c.original_updates);
    $("languageCountUnchanged").textContent = number(c.unchanged);
    $("languageCountUnknown").textContent = number(c.unknown);
    const skipped = data.skipped || {};
    $("languageExcluded").textContent = "Safe for review: " + number(data.eligible) +
      ". Excluded: " + number((skipped.already_localized || 0) +
      (skipped.manual_or_busy || 0) + (skipped.invalid_or_missing || 0)) +
      " (already tagged " + number(skipped.already_localized) +
      "; manual/busy " + number(skipped.manual_or_busy) +
      "; missing/unsafe " + number(skipped.invalid_or_missing) + ").";
    showExamples(data.sample);
    renderActions();
  }
  function renderBatch() {
    const batch = stateUI.batch;
    if (!batch || batch.state === "idle") {
      $("languageJobSummary").textContent = "No recent English-tagging batch.";
      return;
    }
    $("languageJobSummary").textContent = "Last " +
      (batch.mode === "bulk" ? "remaining-library batch" : "3-track test") +
      ": " + number(batch.done) + "/" + number(batch.submitted) +
      " completed, " + number(batch.pending + batch.running) + " queued/running, " +
      number(batch.failed) + " failed." +
      (batch.state === "done" ? " All queued jobs finished." : "");
  }
  function renderActions() {
    const available = selectedCount();
    const c = stateUI.canary || {};
    const same = Boolean(c.include_localized) === recheck() &&
      (c.scope || "names_and_originals") === scope();
    const verified = c.verified && same;
    const remaining = Math.max(0, available - (same ? (c.track_count || 0) : 0));
    const completedBatch = stateUI.batch?.mode === "bulk" && stateUI.batch?.state === "done";
    $("languageScopeSummary").textContent = stateUI.review
      ? number(available) + " candidates selected from the cached review" +
        (same && c.track_count ? " (" + number(remaining) + " not in the test)" : "") + ". " +
        (scope() === "names" ? "Original-tag-only updates are excluded." :
          "Both visible-name changes and original-tag repairs are included.") +
        " Cache-incomplete and unchanged tracks are excluded."
      : "Review the library to select actual differences. Nothing is selected yet.";
    $("languageCanary").disabled = stateUI.busy || !stateUI.review?.review_token ||
      available < 1 || (same && c.track_count && ["running", "ready"].includes(c.state));
    $("languageVerify").disabled = stateUI.busy || !same || c.state !== "ready" || Boolean(c.verified);
    $("languageBulk").disabled = stateUI.busy || !stateUI.review?.review_token ||
      remaining < 1 || !verified || completedBatch;
    const label = !c.track_count ? "No test submitted yet." :
      !same ? "This selection differs from the last test. Run another 3-track test." :
      c.state === "running" ? number(c.pending) + " test job(s) still processing. Check Activity for details." :
      c.state === "failed" ? "A test job failed. Inspect Activity before retrying." :
      c.verified ? "Test complete and Navidrome check confirmed. Remaining updates are enabled." :
      "All " + number(c.track_count) + " test job(s) finished. After a Navidrome scan, check song IDs, favorites, ratings and playlists, then confirm.";
    text("languageCanarySummary", label, c.state === "failed" && same);
    renderBatch();
  }
  function reset() {
    stateUI.serial++;
    stateUI.user = null;
    stateUI.review = null; stateUI.canary = null; stateUI.batch = null;
    stateUI.busy = false;
    clearInterval(stateUI.statusTimer); clearInterval(stateUI.requestTimer);
    stateUI.statusTimer = null; stateUI.requestTimer = null;
    $("languageRecheck").checked = false;
    $("languageScope").value = "names";
    $("languageReviewBar").hidden = true;
    $("languageReview").disabled = false;
    resetMetrics();
    text("languageReviewStatus", "No review loaded. Run the review to see what would change.");
    text("languageApplyStatus", "Nothing queued from this page.");
    renderActions();
  }
  async function refreshStatus(user, serial, restore = false) {
    const endpoints = [
      api("/api/catalogue/language-canary?" + params(user)),
      api("/api/catalogue/language-batch-status?" + params(user)),
    ];
    if (restore) endpoints.push(api("/api/catalogue/language-review-state?" + params(user)));
    const data = await Promise.all(endpoints);
    if (!active(user, serial)) return;
    stateUI.canary = data[0];
    stateUI.batch = data[1];
    if (restore && !stateUI.review && data[2]?.state === "ready" && data[2].result) {
      stateUI.review = data[2].result;
      $("languageRecheck").checked = Boolean(stateUI.review.include_localized);
      renderReview(stateUI.review);
      text("languageReviewStatus", "Previous read-only review restored. " +
        "No files were changed by reviewing.");
    }
    renderActions();
  }
  async function enter() {
    if (!state.user) return;
    if (stateUI.user !== state.user) {
      reset();
      stateUI.user = state.user;
    }
    const user = stateUI.user, serial = stateUI.serial;
    try {
      await refreshStatus(user, serial, !stateUI.review);
    } catch (error) {
      if (active(user, serial)) text("languageReviewStatus", error.message, true);
    }
    if (!stateUI.statusTimer) {
      stateUI.statusTimer = setInterval(() => {
        if (state.tab === "language" && state.user === stateUI.user && !stateUI.busy) {
          refreshStatus(stateUI.user, stateUI.serial).catch(error =>
            text("languageApplyStatus", error.message, true));
        }
      }, 4000);
    }
  }
  async function pollProgress(user, serial, endpoint, target) {
    try {
      const data = await api(endpoint + "?" + params(user));
      if (!active(user, serial)) return;
      if (endpoint.endsWith("language-progress") && data.state === "running") {
        const fraction = data.total ? Math.round(100 * data.processed / data.total) : 0;
        $("languageReviewBar").value = fraction;
        text(target, "Scanning local tags: " + number(data.processed) + "/" +
          number(data.total || 0) + " checked; " + number(data.eligible) +
          " safe candidates. The scan is read-only.");
      } else if (endpoint.endsWith("language-apply-progress") &&
                 ["scanning", "queuing"].includes(data.state)) {
        text(target, "Selecting reviewed tracks: " +
          number(data.processed) + " examined, " + number(data.queued) +
          " submitted so far. Detailed events are in Activity.");
      }
    } catch (_) { /* The main request reports its own errors. */ }
  }
  async function review() {
    if (stateUI.busy || !state.user) return;
    const user = state.user, serial = stateUI.serial;
    stateUI.busy = true; stateUI.review = null;
    resetMetrics(); renderActions();
    $("languageReview").disabled = true;
    $("languageReviewBar").hidden = false;
    $("languageReviewBar").value = 0;
    text("languageReviewStatus", "Review started. Reading existing audio tags without changing files...");
    await pollProgress(user, serial, "/api/catalogue/language-progress", "languageReviewStatus");
    clearInterval(stateUI.requestTimer);
    stateUI.requestTimer = setInterval(() =>
      pollProgress(user, serial, "/api/catalogue/language-progress", "languageReviewStatus"), 1200);
    try {
      const result = await api("/api/catalogue/language-preview?" + params(user) +
        "&include_localized=" + String(recheck()));
      if (!active(user, serial)) return;
      renderReview(result);
      text("languageReviewStatus", "Read-only review complete. " + number(result.scanned) +
        " checked; " + number(result.changes?.display_changes) + " possible visible-name changes, " +
        number(result.changes?.original_updates) + " original-tag repairs. No audio or tags were modified.");
    } catch (error) {
      if (active(user, serial)) text("languageReviewStatus", error.message, true);
    } finally {
      clearInterval(stateUI.requestTimer); stateUI.requestTimer = null;
      if (active(user, serial)) {
        stateUI.busy = false; $("languageReview").disabled = false;
        $("languageReviewBar").hidden = true; renderActions();
        await refreshStatus(user, serial).catch(() => {});
      }
    }
  }
  async function apply(limit) {
    if (stateUI.busy || !stateUI.review?.review_token || !selectedCount()) return;
    const user = state.user, serial = stateUI.serial;
    const canary = limit === 3;
    if (!confirm(canary
      ? "Backed up your music and importer state? Queue up to THREE reviewed metadata changes, then verify Navidrome before continuing?"
      : "Have the test tracks finished, and have you checked song IDs, favorites, ratings and playlists in Navidrome? Queue all remaining reviewed changes?")) return;
    stateUI.busy = true; renderActions();
    text("languageApplyStatus", "Submitting reviewed " +
      (canary ? "test" : "remaining-library") + " updates...");
    clearInterval(stateUI.requestTimer);
    stateUI.requestTimer = setInterval(() =>
      pollProgress(user, serial, "/api/catalogue/language-apply-progress", "languageApplyStatus"), 1000);
    try {
      const result = await api("/api/catalogue/language-apply", {
        user_id: user, limit, confirmed: true, include_localized: recheck(),
        review_token: stateUI.review.review_token, scope: scope(),
      });
      if (!active(user, serial)) return;
      text("languageApplyStatus", number(result.queued) + " reviewed track job(s) submitted. " +
        "The worker now updates audio tags without changing MusicBrainz IDs or audio paths. " +
        "Follow completion above and see Activity for per-track details.");
    } catch (error) {
      if (active(user, serial)) text("languageApplyStatus", error.message, true);
    } finally {
      clearInterval(stateUI.requestTimer); stateUI.requestTimer = null;
      if (active(user, serial)) {
        stateUI.busy = false;
        await refreshStatus(user, serial).catch(() => {});
        renderActions();
      }
    }
  }
  async function verify() {
    if (stateUI.busy || $("languageVerify").disabled) return;
    const user = state.user, serial = stateUI.serial;
    if (!confirm("I have checked the test tracks in Navidrome after scanning: recording IDs, favorites, ratings and playlists remain correct.")) return;
    stateUI.busy = true; renderActions();
    try {
      await api("/api/catalogue/language-canary-verify", {user_id: user, confirmed: true});
      if (!active(user, serial)) return;
      text("languageApplyStatus", "Navidrome check confirmed. Remaining reviewed updates are now available.");
    } catch (error) {
      if (active(user, serial)) text("languageApplyStatus", error.message, true);
    } finally {
      if (active(user, serial)) {
        stateUI.busy = false;
        await refreshStatus(user, serial).catch(() => {});
        renderActions();
      }
    }
  }
  $("languageReview").addEventListener("click", review);
  $("languageCanary").addEventListener("click", () => apply(3));
  $("languageBulk").addEventListener("click", () => apply(5000));
  $("languageVerify").addEventListener("click", verify);
  $("languageScope").addEventListener("change", renderActions);
  $("languageRecheck").addEventListener("change", () => {
    stateUI.review = null; resetMetrics();
    text("languageReviewStatus", "Review again with the new recheck option before applying any updates.");
    renderActions();
  });
  $("languageOpenActivity").addEventListener("click", () =>
    document.querySelector('[data-tab="activity"]').click());
  return {enter, reset};
})();
