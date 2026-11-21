"use strict";

/* Application navigation and direct-link restoration. Feature modules own their
   internal data and unsaved drafts; this controller owns browser history. */
// Browser Back/Forward and direct link entry share exactly one restoration path.
// The URL selects data; album checks, preview tokens and unsaved edits do not.
async function restoreBrowserRoute(route) {
  const revision = ++routeRevision;
  if (!route) { notice("Unknown application URL.", true); return; }
  if (route.user && route.user !== state.user) {
    if (!Array.from($("user").options).some(option => option.value === route.user)) {
      notice("The linked library is unavailable. Choose an accessible library.", true);
      return;
    }
    restoringRoute = true;
    try {
      $("user").value = route.user;
      changeUser();
    } finally { restoringRoute = false; }
  }
  if (!state.user || revision !== routeRevision) return;
  const featureRoute = ["approval", "catalogue", "album", "graph"].includes(route.tab);
  restoringRoute = true;
  try { setTab(route.tab, false, {skipEnter: featureRoute, noHistory: true}); }
  finally { restoringRoute = false; }
  if (route.tab === "music") {
    const ignored = Boolean(route.ignored);
    state.musicView = ignored ? "ignored" : "tracks";
    $("musicResults").hidden = ignored;
    $("ignoredMusicPanel").hidden = !ignored;
    $("search").value = route.q || "";
    $("filter").value = route.status || "ALL";
    if (!$("filter").value) $("filter").value = "ALL";
    state.page = route.page || 1;
    state.generation++;
    if (ignored) await Promise.allSettled([loadIgnored(false), loadDisabledOrigins(false)]);
    else await refreshTracks(false);
    if (revision !== routeRevision || state.tab !== "music") return;
    if (route.trackId) await showDetails({id:route.trackId});
    else if ($("detailsDialog").open) $("detailsDialog").close();
    return;
  }
  if (route.tab === "album") {
    await window.AlbumUI?.openRoute(route);
  } else if (route.tab === "approval") {
    await window.MetadataUI?.openRoute(route);
  } else if (route.tab === "catalogue") {
    await window.CatalogueUI?.openRoute(route);
  } else if (route.tab === "graph") {
    await window.GraphUI?.openRoute(route);
  } else if (route.tab === "sources") {
    await loadSources(state.generation, false);
    if (route.sourceId && !focusSource(route.sourceId, true))
      notice("This source is unavailable in the selected library.", true);
  } else if (route.tab === "help" && route.topic) {
    const topic = document.getElementById("help-" + route.topic);
    if (topic) topic.scrollIntoView?.({block:"start"});
    else notice("Unknown Help topic.", true);
  }
}

(async () => {
  try {
    const initial = Router?.read();
    const connected = new URLSearchParams(window.location.search).get("spotify") === "connected";
    await loadUsers(initial?.user || rememberedLibrary());
    bootstrappingRoute = false;
    if (state.user) {
      if (connected) {
        Router?.write(pageRoute("sources"), {replace:true});
        await restoreBrowserRoute(Router?.read() || pageRoute("sources"));
        notice("Spotify connected. Import a source or browse your playlists.");
      } else if (initial?.user) {
        await restoreBrowserRoute(initial);
      } else {
        Router?.write(pageRoute("music"), {replace:true});
        await restoreBrowserRoute(Router?.read() || pageRoute("music"));
      }
    } else if (initial?.user) {
      notice("The linked library is unavailable. Select a library to continue.", true);
    }
    window.addEventListener("popstate", () => {
      void restoreBrowserRoute(Router?.read()).catch(error => notice(error.message, true));
    });
  } catch (error) {
    bootstrappingRoute = false;
    notice(error?.message || String(error), true);
  } finally {
    setInterval(() => guard(pollFast)(), FAST_POLL_MS);
    setInterval(() => guard(pollSlow)(), SLOW_POLL_MS);
  }
})();
