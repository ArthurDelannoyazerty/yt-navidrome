/* Spotify metadata and reviewed audio links; credentials stay on the server. */
"use strict";
window.SpotifyUI = (() => {
  const ui = {playlistPage: 1, playlistMax: 1, selected: new Map(), matchPage: 1,
    matchMax: 1, revision: null, unique: 0, link: null};
  const scope = name => {
    const user = state.user;
    const latest = requestGate.begin(`spotify-${name}`);
    return {user, current: () => latest() && state.user === user};
  };
  const spotifyLink = (url, text) => {
    const a = node("a", text); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer";
    return a;
  };
  function reset() {
    ui.selected.clear(); ui.playlistPage = 1; ui.matchPage = 1; ui.revision = null; ui.link = null;
    for (const id of ["spotifyPlaylistsDialog", "spotifyMatchesDialog", "spotifyLinkDialog"]) $(id).close();
    for (const id of ["spotifyPlaylists", "spotifyMatches", "spotifyCandidates"]) $(id).replaceChildren();
    $("spotifyStatus").textContent = "Loading Spotify connection...";
    $("spotifyConnect").disabled = true; $("spotifyDisconnect").disabled = true;
    $("spotifyBrowse").disabled = true; $("spotifyLinkExact").disabled = true;
  }
  async function refresh() {
    if (!state.user) return;
    const ticket = scope("status");
    const data = await api(`/api/spotify/status?user_id=${encodeURIComponent(ticket.user)}`, null, null, ticket.current);
    if (!ticket.current()) return;
    $("spotifyStatus").textContent = data.configuration_error || (data.connected
      ? `Connected for ${ticket.user}: ${data.display_name || data.account_id}`
      : `Not connected for ${ticket.user}. Connect to read Spotify sources.`);
    $("spotifyRedirect").textContent = data.redirect_uri || "https://YOUR-INGESTOR-HOST/api/spotify/callback";
    $("spotifyConnect").disabled = !data.configured;
    $("spotifyConnect").textContent = data.connected ? "Reconnect Spotify" : "Connect Spotify";
    $("spotifyDisconnect").disabled = !data.connected;
    $("spotifyBrowse").disabled = !data.connected;
  }
  $("spotifyConnect").addEventListener("click", guard(async () => {
    const ticket = scope("connect");
    const data = await api("/api/spotify/authorize", {user_id: ticket.user}, null, ticket.current);
    if (ticket.current()) window.location.assign(data.url);
  }));
  $("spotifyDisconnect").addEventListener("click", guard(async () => {
    const user = state.user;
    if (!confirm(`Disconnect Spotify for ${user}? Imported files and links remain. Future Spotify syncs need reconnection.`)) return;
    const data = await api(`/api/spotify/connection?user_id=${encodeURIComponent(user)}`, null, "DELETE");
    if (user !== state.user) return;
    notice(data.message); await refresh();
  }));
  function selectedCount() {
    $("spotifyImportSelected").textContent = `Import selected (${ui.selected.size})`;
    $("spotifyImportSelected").disabled = !ui.selected.size;
  }
  async function playlists() {
    const ticket = scope("playlists");
    const data = await api(`/api/spotify/playlists?user_id=${encodeURIComponent(ticket.user)}&page=${ui.playlistPage}`, null, null, ticket.current);
    if (!ticket.current()) return;
    ui.playlistMax = Math.max(1, Math.ceil(data.total / data.limit));
    $("spotifyPlaylistPage").textContent = `${data.page} / ${ui.playlistMax}`;
    $("spotifyPlaylistPrev").disabled = ui.playlistPage <= 1;
    $("spotifyPlaylistNext").disabled = ui.playlistPage >= ui.playlistMax;
    $("spotifyPlaylists").replaceChildren(...data.items.map(item => {
      const row = node("div", null, "spotify-choice");
      const check = node("input"); check.type = "checkbox"; check.checked = ui.selected.has(item.url);
      check.setAttribute("aria-label", `Select ${item.title}`);
      check.addEventListener("change", () => {
        if (check.checked && ui.selected.size >= 100) { check.checked = false; notice("Select up to 100 playlists per import.", true); return; }
        if (check.checked) ui.selected.set(item.url, item.title); else ui.selected.delete(item.url);
        selectedCount();
      });
      const text = node("div"); text.append(spotifyLink(item.url, item.title), node("small", item.owned ? "Your playlist" : "Requires collaborator access to import"));
      row.append(check, text); return row;
    }));
    if (!data.items.length) $("spotifyPlaylists").append(node("p", "No playlists returned for this account.", "empty"));
    selectedCount();
  }
  $("spotifyBrowse").addEventListener("click", guard(async () => {
    $("spotifyPlaylistsDialog").showModal(); await playlists();
  }));
  $("spotifyPlaylistPrev").addEventListener("click", guard(async () => { ui.playlistPage = Math.max(1, ui.playlistPage - 1); await playlists(); }));
  $("spotifyPlaylistNext").addEventListener("click", guard(async () => { ui.playlistPage = Math.min(ui.playlistMax, ui.playlistPage + 1); await playlists(); }));
  $("spotifyImportSelected").addEventListener("click", guard(async () => {
    const ticket = scope("import"); const urls = [...ui.selected.keys()];
    if (!urls.length || !confirm(`Import ${urls.length} Spotify playlists for ${ticket.user}? Audio linking is a separate step.`)) return;
    await api("/api/sources", {user_id: ticket.user, urls, monitored: $("spotifyMonitor").checked});
    if (!ticket.current()) return;
    ui.selected.clear(); $("spotifyPlaylistsDialog").close();
    notice("Spotify sources queued. Review library matches after synchronization."); await refreshAll(false);
  }));
  async function matches() {
    const ticket = scope("matches");
    const data = await api(`/api/spotify/matches?user_id=${encodeURIComponent(ticket.user)}&page=${ui.matchPage}`, null, null, ticket.current);
    if (!ticket.current()) return;
    ui.revision = data.revision; ui.unique = data.unique;
    ui.matchMax = Math.max(1, Math.ceil(data.total / data.limit));
    $("spotifyMatchesSummary").textContent = `${data.total} unlinked entries: ${data.unique} unique exact-name matches, ${data.ambiguous} ambiguous, ${data.unmatched} without an exact match.`;
    $("spotifyMatchPage").textContent = `${data.page} / ${ui.matchMax}`;
    $("spotifyMatchPrev").disabled = ui.matchPage <= 1;
    $("spotifyMatchNext").disabled = ui.matchPage >= ui.matchMax;
    $("spotifyLinkExact").disabled = !data.unique;
    $("spotifyLinkExact").textContent = `Link unique matches (${data.unique})`;
    $("spotifyMatches").replaceChildren(...data.items.map(item => {
      const row = node("div", null, "spotify-match");
      const details = node("div"); details.append(node("strong", item.title));
      if (!item.candidates.length) details.append(node("small", "No exact name match. Search your library or supply a YouTube video."));
      for (const candidate of item.candidates) {
        const line = node("div"); line.append(node("span", `${candidate.matched_title} `), musicbrainzLink("recording", candidate.mbid)); details.append(line);
      }
      row.append(details, button("Review link", () => {
        $("spotifyMatchesDialog").close(); return openLink({id: item.id, title: item.title});
      }));
      return row;
    }));
    if (!data.items.length) $("spotifyMatches").append(node("p", "No unlinked Spotify entries. Import or sync a Spotify source first.", "empty"));
  }
  $("spotifyReviewMatches").addEventListener("click", guard(async () => {
    ui.matchPage = 1; $("spotifyMatchesDialog").showModal(); await matches();
  }));
  $("spotifyMatchPrev").addEventListener("click", guard(async () => { ui.matchPage = Math.max(1, ui.matchPage - 1); await matches(); }));
  $("spotifyMatchNext").addEventListener("click", guard(async () => { ui.matchPage = Math.min(ui.matchMax, ui.matchPage + 1); await matches(); }));
  $("spotifyLinkExact").addEventListener("click", guard(async () => {
    const ticket = scope("bulk-link");
    if (!ui.revision || !ui.unique || !confirm(`Link ${ui.unique} unique exact-name matches across ALL preview pages for ${ticket.user}? Names are suggestions, not proof of identical recordings. Review editions and versions first. No audio will be downloaded.`)) return;
    const data = await api("/api/spotify/link-exact", {user_id: ticket.user, revision: ui.revision}, null, ticket.current);
    if (!ticket.current()) return;
    notice(data.message); await matches(); await refreshTracks(false);
  }));
  async function candidates() {
    if (!ui.link) return;
    const link = ui.link; const ticket = scope("candidates");
    const data = await api(`/api/spotify/candidates?user_id=${encodeURIComponent(link.user)}&q=${encodeURIComponent($("spotifySearch").value.trim())}&page=${link.page}`, null, null, ticket.current);
    if (!ticket.current() || ui.link !== link) return;
    link.maxPage = Math.max(1, Math.ceil(data.total / data.limit));
    $("spotifyCandidatePage").textContent = `${data.page} / ${link.maxPage}`;
    $("spotifyCandidatePrev").disabled = link.page <= 1;
    $("spotifyCandidateNext").disabled = link.page >= link.maxPage;
    $("spotifyCandidates").replaceChildren(...data.items.map(item => {
      const row = node("div", null, "spotify-match"); const text = node("div");
      text.append(node("strong", item.title), musicbrainzLink("recording", item.mbid));
      row.append(text, button("Use this recording", async () => {
        if (ui.link !== link || state.user !== link.user) return;
        if (!confirm(`Link Spotify "${link.title}" to "${item.title}" (${item.mbid})? This reuses existing audio and preserves Spotify playlist dates.`)) return;
        const result = await api(`/api/spotify/entries/${encodeURIComponent(link.id)}/link`, {user_id: link.user, target_id: item.id, expected_mbid: item.mbid});
        if (ui.link !== link || state.user !== link.user) return;
        notice(result.message); $("spotifyLinkDialog").close(); await refreshTracks(false);
      })); return row;
    }));
    if (!data.items.length) $("spotifyCandidates").append(node("p", "No available, idle, MusicBrainz-confirmed tracks on this page. Refine the search or import audio first.", "empty"));
  }
  async function openLink(track) {
    ui.link = {id: track.id, title: track.title, user: state.user, page: 1, maxPage: 1};
    $("spotifyLinkTitle").textContent = track.title; $("spotifyAudioUrl").value = "";
    $("spotifySearch").value = track.title.slice(0, 200);
    $("spotifyCandidates").replaceChildren(); $("spotifyLinkDialog").showModal();
    await candidates();
  }
  $("spotifySearchForm").addEventListener("submit", guard(async event => {
    event.preventDefault(); if (ui.link) ui.link.page = 1; await candidates();
  }));
  $("spotifyCandidatePrev").addEventListener("click", guard(async () => { if (ui.link) ui.link.page = Math.max(1, ui.link.page - 1); await candidates(); }));
  $("spotifyCandidateNext").addEventListener("click", guard(async () => { if (ui.link) ui.link.page = Math.min(ui.link.maxPage, ui.link.page + 1); await candidates(); }));
  $("spotifyAudioForm").addEventListener("submit", guard(async event => {
    event.preventDefault(); const link = ui.link;
    if (!link || link.user !== state.user) return;
    const audio_url = $("spotifyAudioUrl").value.trim();
    if (!audio_url || !confirm(`Import this YouTube video for "${link.title}"? The usual audio identification/approval pipeline runs first. A name mismatch will require explicit review.`)) return;
    const result = await api(`/api/spotify/entries/${encodeURIComponent(link.id)}/link`, {user_id: link.user, audio_url});
    if (ui.link !== link || link.user !== state.user) return;
    notice(result.message); $("spotifyLinkDialog").close(); await refreshAll(false);
  }));
  function controls(track) {
    if (track.current_asset_id || !track.origins.some(o => o.provider === "spotify") || track.origins.some(o => o.downloadable)) return [];
    if (["QUEUED", "DEFERRED"].includes(track.operation_state) && ["SPOTIFY_LINK", "SPOTIFY_AUDIO"].includes(track.operation_kind)) {
      return [button("Cancel waiting link", async () => {
        const user = state.user;
        if (!confirm("Cancel this waiting link? Independently queued YouTube imports are retained.")) return;
        const result = await api(`/api/spotify/entries/${encodeURIComponent(track.id)}/cancel`, {user_id: user});
        if (state.user !== user) return;
        notice(result.message); await refreshTracks(false);
      })];
    }
    const link = button("Link audio", () => openLink(track));
    link.disabled = ["QUEUED", "RUNNING", "DEFERRED"].includes(track.operation_state);
    return [link];
  }
  for (const [buttonId, dialogId] of [["spotifyClosePlaylists", "spotifyPlaylistsDialog"], ["spotifyCloseMatches", "spotifyMatchesDialog"], ["spotifyCloseLink", "spotifyLinkDialog"]]) {
    $(buttonId).addEventListener("click", () => { $(dialogId).close(); if (dialogId === "spotifyLinkDialog") ui.link = null; });
  }
  return {reset, refresh, controls, matches, playlists, openLink};
})();
