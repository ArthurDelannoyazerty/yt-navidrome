/* Existing-library English names: read-only review, scoped canary and verified bulk. */
"use strict";
window.LanguageUI = (() => {
  const $ = id => document.getElementById(id);
  const emptyChanges = () => ({token: null, items: [], nextOffset: 0, total: 0, loading: false});
  const stateUI = {user: null, serial: 0, busy: false, review: null,
    canary: null, batch: null, statusTimer: null, requestTimer: null,
    changes: emptyChanges()};
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
    resetNameList();
  }
  function nameState(row) {
    const status = row.job_state;
    if (status === "PENDING") return {label: "Queued", locked: true, note: "Waiting"};
    if (status === "RUNNING") return {label: "Applying", locked: true, note: "In progress"};
    if (status === "DONE") return {label: "Done", locked: true, note: "Applied"};
    if (status === "FAILED" || status === "CANCELLED")
      return {label: "Retry", locked: false, note: "Check Activity"};
    return {label: "Change", locked: false, note: ""};
  }
  function updateNameRow(row) {
    if (!row.element) return;
    const status = nameState(row);
    const button = row.element.querySelector("button");
    button.textContent = status.label;
    button.disabled = status.locked || stateUI.busy;
    row.element.querySelector(".language-change-row-status").textContent = status.note;
  }
  function createNameRow(row) {
    const item = el("div", undefined, "language-change-row");
    item.dataset.trackId = row.id;
    item.setAttribute("role", "listitem");
    const before = el("div", undefined, "language-change-column");
    before.append(el("small", (row.changed_field || "Title / artist") + " · now"),
                  el("strong", row.before_label ?? row.current_name ?? "Unknown"));
    const arrow = el("span", "→", "language-change-arrow");
    arrow.setAttribute("aria-label", "changes to");
    const after = el("div", undefined, "language-change-column");
    after.append(el("small", (row.changed_field || "Title / artist") + " · proposed"),
                 el("strong", row.after_label ?? row.proposed_name ?? "Unknown"));
    const actions = el("div", undefined, "language-change-actions");
    const button = el("button", "Change");
    button.type = "button";
    button.className = "secondary";
    button.setAttribute("aria-label", "Change " + (row.current_name || row.before_label || row.id));
    const status = el("small", "", "language-change-row-status");
    actions.append(button, status);
    item.append(before, arrow, after, actions);
    row.element = item;
    button.addEventListener("click", () => changeOneName(row));
    updateNameRow(row);
    return item;
  }
  function resetNameList() {
    stateUI.changes = emptyChanges();
    $("languageNameChangesCount").textContent = "(—)";
    $("languageNameChangesList").replaceChildren(el("p", "Run a review to load name changes.", "hint"));
    text("languageNameChangesStatus", "Run a review to load proposed name changes.");
    $("languageChangeAll").disabled = true;
  }
  async function loadNextNamePage() {
    const changes = stateUI.changes;
    const user = state.user, serial = stateUI.serial;
    const token = stateUI.review?.review_token;
    if (!user || !token || changes.token !== token || changes.loading ||
        changes.nextOffset === null) return;
    changes.loading = true;
    const offset = changes.nextOffset;
    text("languageNameChangesStatus", "Loading name changes…");
    try {
      const query = new URLSearchParams({user_id: user, review_token: token,
        offset: String(offset), limit: "80"});
      const data = await api("/api/catalogue/language-name-changes?" + query);
      if (!active(user, serial) || stateUI.changes !== changes ||
          stateUI.review?.review_token !== token) return;
      changes.total = data.total;
      changes.nextOffset = data.next_offset;
      if (!offset) $("languageNameChangesList").replaceChildren();
      for (const incoming of data.items || []) {
        const row = {...incoming};
        changes.items.push(row);
        $("languageNameChangesList").append(createNameRow(row));
      }
      $("languageNameChangesCount").textContent = "(" + number(data.total) + ")";
      if (!changes.items.length) {
        $("languageNameChangesList").append(el("p", "No visible-name changes in this review.", "hint"));
      }
      text("languageNameChangesStatus", "Showing " + number(changes.items.length) +
        " of " + number(changes.total) + " proposed visible-name changes." +
        (changes.nextOffset !== null ? " Scroll down for more." : ""));
      renderActions();
      // A large viewport can fit all of a short page without producing a
      // scroll event; load the next page in that case.
    } catch (error) {
      if (active(user, serial) && stateUI.changes === changes)
        text("languageNameChangesStatus", error.message + " Run the review again if the list is outdated.", true);
    } finally {
      if (stateUI.changes === changes) {
        changes.loading = false;
        const list = $("languageNameChangesList");
        if (changes.nextOffset !== null && list.scrollHeight <= list.clientHeight + 25)
          void loadNextNamePage();
      }
    }
  }
  async function refreshNameStates(user, serial) {
    const changes = stateUI.changes, token = stateUI.review?.review_token;
    if (!token || !changes.items.length || changes.token !== token) return;
    const data = await api("/api/catalogue/language-name-change-status?" +
      new URLSearchParams({user_id: user, review_token: token}));
    if (!active(user, serial) || changes !== stateUI.changes) return;
    for (const row of changes.items) {
      const current = data.states?.[row.id] || null;
      if (row.job_state !== current) {
        row.job_state = current;
        updateNameRow(row);
      }
    }
  }
  async function changeOneName(row) {
    if (stateUI.busy || !stateUI.review?.review_token || nameState(row).locked) return;
    const user = state.user, serial = stateUI.serial;
    const token = stateUI.review.review_token;
    if (!confirm("Queue the selected name update?\n\nNow: " +
      (row.before_label || row.current_name) + "\nProposed: " +
      (row.after_label || row.proposed_name) +
      "\n\nThe worker rechecks MusicBrainz before writing. MusicBrainz IDs and audio paths are preserved.")) return;
    stateUI.busy = true;
    renderActions(); updateNameRow(row);
    text("languageNameChangesStatus", "Queuing the selected name change…");
    try {
      await api("/api/catalogue/language-name-change", {
        user_id: user, track_id: row.id, review_token: token, confirmed: true,
      });
      if (!active(user, serial) || stateUI.review?.review_token !== token) return;
      row.job_state = "PENDING";
      text("languageNameChangesStatus", "Selected track queued. Follow its status here or open Activity for details.");
    } catch (error) {
      if (active(user, serial)) text("languageNameChangesStatus", error.message, true);
    } finally {
      if (active(user, serial)) {
        stateUI.busy = false;
        renderActions(); updateNameRow(row);
      }
    }
  }
  async function changeAllNames() {
    const c = stateUI.canary || {};
    if (stateUI.busy || !stateUI.review?.review_token || !c.verified ||
        c.scope !== "names" || Boolean(c.include_localized) !== recheck() ||
        $("languageChangeAll").disabled) return;
    // Reuse the already reviewed, canary-protected bulk operation. Never
    // include original-tag-only repairs in the list's Change all action.
    $("languageScope").value = "names";
    renderActions();
    const queued = await apply(5000);
    if (queued && stateUI.review?.review_token)
      text("languageNameChangesStatus", number(queued.queued) +
        " reviewed visible-name change(s) queued. Watch the rows or Activity for results.");
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
    stateUI.changes.token = data.review_token || null;
    if (data.review_token) void loadNextNamePage();
    else text("languageNameChangesStatus", "Run the review again to build a full list of proposed names.");
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
    const remaining = Math.max(0, available - (same ? (c.review_tested || 0) : 0));
    const submittedBatch = stateUI.batch?.mode === "bulk" &&
      stateUI.batch?.review_token === stateUI.review?.review_token &&
      stateUI.batch?.scope === scope();
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
      remaining < 1 || !verified || submittedBatch;
    const nameCanaryValid = Boolean(c.verified) && c.scope === "names" &&
      Boolean(c.include_localized) === recheck();
    const namesDone = stateUI.batch?.mode === "bulk" &&
      stateUI.batch?.scope === "names" &&
      stateUI.batch?.review_token === stateUI.review?.review_token;
    $("languageChangeAll").disabled = stateUI.busy || !stateUI.review?.review_token ||
      stateUI.changes.token !== stateUI.review.review_token ||
      !stateUI.changes.total || !nameCanaryValid || namesDone;
    $("languageChangeAll").title = nameCanaryValid
      ? "Queue the remaining reviewed visible-name changes"
      : "First test up to three visible-name changes and confirm them in Navidrome below";
    for (const row of stateUI.changes.items) updateNameRow(row);
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
    stateUI.changes = emptyChanges();
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
    if (!restore && state.tab === "language" && !stateUI.busy)
      await refreshNameStates(user, serial).catch(() => {});
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
      return result;
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
  $("languageChangeAll").addEventListener("click", changeAllNames);
  $("languageNameChangesList").addEventListener("scroll", () => {
    const list = $("languageNameChangesList");
    if (list.scrollTop + list.clientHeight >= list.scrollHeight - 140)
      void loadNextNamePage();
  });
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
