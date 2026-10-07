"use strict";

const $ = id => document.getElementById(id);

const FAST_POLL_MS = 2000;
const SLOW_POLL_MS = 10000;

const state = {
  user: "",
  page: 1,
  maxPage: 1,
  after: 0,
  generation: 0,
  sources: [],
  events: [],
  approvals: new Map(),
  fastPolling: false,
  slowPolling: false,
  systemSignature: "",
};

const node = (tag, text, cls) => {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (cls) element.className = cls;
  return element;
};

function notice(message, error = false) {
  const element = $("notice");
  element.hidden = false;
  element.textContent = message;
  element.className = error ? "error" : "success";
}

async function api(path, body, method) {
  const requestMethod = method || (body ? "POST" : "GET");
  const response = await fetch(path, {
    method: requestMethod,
    cache: requestMethod === "GET" ? "no-store" : "default",
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
  });

  const data = await response.json().catch(() => ({
    error: `Invalid response (HTTP ${response.status})`,
  }));

  if (!response.ok || data.error) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }

  return data;
}

const guard = fn => async (...args) => {
  try {
    await fn(...args);
  } catch (error) {
    notice(error?.message || String(error), true);
  }
};

function button(text, fn, cls = "secondary") {
  const element = node("button", text, cls);
  element.type = "button";

  element.addEventListener("click", guard(async () => {
    element.disabled = true;

    try {
      await fn();
    } finally {
      element.disabled = false;
    }
  }));

  return element;
}

function badge(status) {
  return node(
    "span",
    status.replaceAll("_", " "),
    `badge ${status}`,
  );
}

function time(value) {
  return value
    ? value.replace("T", " ").replace("Z", " UTC")
    : "Unknown (not invented)";
}

function stable(value) {
  return JSON.stringify(value);
}

function setText(element, value) {
  const text = String(value);

  if (element.textContent !== text) {
    element.textContent = text;
  }
}

function selectionTouches(element) {
  const selection = window.getSelection();

  if (
    !selection
    || selection.isCollapsed
    || selection.rangeCount === 0
  ) {
    return false;
  }

  for (let index = 0; index < selection.rangeCount; index += 1) {
    try {
      if (selection.getRangeAt(index).intersectsNode(element)) {
        return true;
      }
    } catch (_) {
      // A detached node may throw. It cannot contain an active selection.
    }
  }

  return false;
}

function elementIsBusy(element) {
  const active = document.activeElement;

  if (
    active
    && active !== document.body
    && element.contains(active)
  ) {
    return true;
  }

  if (selectionTouches(element)) {
    return true;
  }

  if (element.querySelector("details[open]")) {
    return true;
  }

  return false;
}

function updateKeyedChildren(
  container,
  items,
  {
    key,
    signature,
    render,
    background = false,
    empty = null,
  },
) {
  const existing = new Map(
    [...container.children]
      .filter(child => child.dataset?.key)
      .map(child => [child.dataset.key, child]),
  );

  let cursor = container.firstElementChild;
  const retainedKeys = new Set();

  for (const item of items) {
    const itemKey = String(key(item));
    const itemSignature = signature(item);
    const current = existing.get(itemKey);

    let desired = current;

    if (
      !current
      || current.dataset.signature !== itemSignature
    ) {
      if (
        !(
          background
          && current
          && elementIsBusy(current)
        )
      ) {
        const currentWasCursor = current === cursor;

        desired = render(item);
        desired.dataset.key = itemKey;
        desired.dataset.signature = itemSignature;

        if (current) {
          current.replaceWith(desired);

          if (currentWasCursor) {
            cursor = desired;
          }
        }
      }
    }

    if (!desired) {
      continue;
    }

    retainedKeys.add(itemKey);

    // Preserve active selections/focus instead of moving a busy node just because
    // another item was inserted ahead of it during background polling.
    if (
      !(
        background
        && elementIsBusy(desired)
      )
      && desired !== cursor
    ) {
      container.insertBefore(desired, cursor);
    }

    cursor = desired.nextElementSibling;
  }

  for (const [itemKey, element] of existing) {
    if (retainedKeys.has(itemKey)) {
      continue;
    }

    if (
      background
      && elementIsBusy(element)
    ) {
      continue;
    }

    element.remove();
  }

  const oldEmpty = container.querySelector(
    ":scope > [data-empty='true']",
  );

  if (
    items.length === 0
    && empty
  ) {
    if (!oldEmpty) {
      const emptyNode = empty();
      emptyNode.dataset.empty = "true";
      container.append(emptyNode);
    }
  } else if (
    oldEmpty
    && !(
      background
      && elementIsBusy(oldEmpty)
    )
  ) {
    oldEmpty.remove();
  }
}

async function loadUsers(preferred) {
  const users = await api("/api/users");
  const userSelect = $("user");
  const current = preferred || userSelect.value;

  userSelect.replaceChildren(
    ...users.map(user => {
      const option = node("option", user);
      option.value = user;
      return option;
    }),
  );

  userSelect.value = users.includes(current)
    ? current
    : (
      users.includes("admin")
        ? "admin"
        : users[0]
    );

  changeUser();
}

function changeUser() {
  state.user = $("user").value;

  localStorage.setItem(
    "music-user",
    state.user,
  );

  state.page = 1;
  state.after = 0;
  state.generation += 1;
  state.events = [];
  state.approvals.clear();
  state.systemSignature = "";

  $("logs").replaceChildren();
  $("tracks").replaceChildren();
  $("sources").replaceChildren();

  guard(
    () => refreshAll({background: false}),
  )();
}

$("user").addEventListener(
  "change",
  changeUser,
);

$("userForm").addEventListener(
  "submit",
  guard(async event => {
    event.preventDefault();

    const name = $("newUser").value.trim();

    await api(
      "/api/users",
      {name},
    );

    $("newUser").value = "";

    await loadUsers(name);
  }),
);

$("ingestForm").addEventListener(
  "submit",
  guard(async event => {
    event.preventDefault();

    const urls = $("urls").value
      .split(/\r?\n/)
      .map(value => value.trim())
      .filter(Boolean);

    if (!urls.length) {
      return;
    }

    await api(
      "/api/sources",
      {
        user_id: state.user,
        urls,
        monitored: $("monitor").checked,
      },
    );

    $("urls").value = "";

    notice(
      "Sources queued. API and download errors will appear below.",
    );

    await refreshAll({
      background: false,
    });
  }),
);

function renderSource(source) {
  const row = node(
    "div",
    null,
    "source",
  );

  const content = node(
    "div",
    null,
    "source-details",
  );

  content.append(
    node(
      "strong",
      source.title,
    ),
    node(
      "p",
      source.url,
      "url",
    ),
    badge(source.status),
    node(
      "small",
      source.monitored
        ? "  Monitored"
        : "  One-off",
    ),
  );

  if (source.synced_at) {
    content.append(
      node(
        "p",
        `Last sync: ${time(source.synced_at)}`,
        "hint",
      ),
    );
  }

  if (source.error) {
    content.append(
      node(
        "div",
        source.error,
        "error-text",
      ),
    );
  }

  const controls = node(
    "div",
    null,
    "source-buttons",
  );

  if (source.provider !== "legacy") {
    controls.append(
      button(
        "Sync / retry",
        async () => {
          await api(
            `/api/sources/${encodeURIComponent(source.id)}/retry`,
            {
              user_id: state.user,
            },
          );

          notice("Source sync queued");

          await loadSources(
            state.generation,
            false,
          );
        },
      ),
    );
  }

  controls.append(
    button(
      "Remove",
      async () => {
        if (
          !confirm(
            "Remove this source and its exported playlist? Music files are retained.",
          )
        ) {
          return;
        }

        await api(
          `/api/sources/${encodeURIComponent(source.id)}?user_id=${encodeURIComponent(state.user)}`,
          null,
          "DELETE",
        );

        await loadSources(
          state.generation,
          false,
        );
      },
      "danger",
    ),
  );

  row.append(
    content,
    controls,
  );

  return row;
}

async function loadSources(
  generation,
  background = false,
) {
  const sources = await api(
    `/api/sources?user_id=${encodeURIComponent(state.user)}`,
  );

  if (generation !== state.generation) {
    return;
  }

  state.sources = sources;

  updateKeyedChildren(
    $("sources"),
    sources,
    {
      key: source => source.id,
      signature: source => stable(source),
      render: renderSource,
      background,
      empty: () => node(
        "p",
        "No sources yet.",
        "empty",
      ),
    },
  );
}

async function action(
  track,
  mode,
  extra = {},
) {
  const result = await api(
    `/api/tracks/${encodeURIComponent(track.id)}/action`,
    {
      user_id: state.user,
      mode,
      ...extra,
    },
  );

  notice(result.message);

  state.approvals.delete(track.id);

  await refreshAll({
    background: false,
  });
}

function showEditor(track) {
  $("editForm").reset();
  $("editId").value = track.id;

  $("editTitle").textContent =
    track.matched_title
    || track.title;

  $("editDialog").showModal();
}

$("cancelEdit").addEventListener(
  "click",
  () => $("editDialog").close(),
);

$("editForm").addEventListener(
  "submit",
  guard(async event => {
    event.preventDefault();

    const overrides = {};

    for (
      const [field, id]
      of Object.entries({
        mbid: "editMbid",
        release_id: "editRelease",
        artist: "editArtist",
        title: "editSong",
        album: "editAlbum",
      })
    ) {
      const value = $(id).value.trim();

      if (value) {
        overrides[field] = value;
      }
    }

    if (!Object.keys(overrides).length) {
      throw new Error(
        "Enter a metadata change, or use Re-identify to find new matches.",
      );
    }

    await action(
      {
        id: $("editId").value,
      },
      "retag",
      {
        overrides,
      },
    );

    $("editDialog").close();
  }),
);

function musicbrainzLink(
  kind,
  id,
) {
  const link = node(
    "a",
    `${kind} ${id.slice(0, 8)}`,
  );

  link.href =
    `https://musicbrainz.org/${kind}/${encodeURIComponent(id)}`;

  link.target = "_blank";
  link.rel = "noopener noreferrer";

  return link;
}

function renderTrack(track) {
  const row = node("tr");

  const title = node("td");
  const playlists = node("td");
  const discovered = node("td");
  const status = node("td");
  const actions = node("td");

  title.append(
    node(
      "strong",
      track.title,
    ),
    node(
      "div",
      track.url,
      "url",
    ),
  );

  if (track.matched_title) {
    title.append(
      node(
        "div",
        track.matched_title,
        "metadata",
      ),
    );
  }

  if (track.mbid) {
    title.append(
      musicbrainzLink(
        "recording",
        track.mbid,
      ),
    );
  }

  if (track.release_id) {
    title.append(
      node("br"),
      musicbrainzLink(
        "release",
        track.release_id,
      ),
    );
  }

  for (const playlist of track.playlists) {
    const line = node(
      "div",
      playlist.title,
    );

    line.title =
      `Added: ${time(playlist.added_at)}`;

    playlists.append(line);
  }

  if (!track.playlists.length) {
    playlists.textContent = "No playlist";
  }

  discovered.append(
    node(
      "div",
      time(track.discovered_at),
    ),
    node(
      "small",
      track.discovery_basis,
    ),
  );

  status.append(
    badge(track.status),
  );

  if (track.error) {
    const details = node(
      "details",
      null,
      track.status === "COMPLETED"
        ? "warning-text"
        : "error-text",
    );

    details.append(
      node(
        "summary",
        track.status === "COMPLETED"
          ? "Enrichment warnings"
          : "Error details",
      ),
      node(
        "div",
        track.error,
        "error-text",
      ),
    );

    status.append(details);
  }

  const controls = node(
    "div",
    null,
    "actions",
  );

  if (track.status === "FAILED") {
    controls.append(
      button(
        "Retry",
        () => action(
          track,
          "retry",
        ),
      ),
      button(
        "Redownload",
        async () => {
          if (
            confirm(
              "Discard the staged audio and download again? Any previous library file is retained until success.",
            )
          ) {
            await action(
              track,
              "redownload",
            );
          }
        },
      ),
    );
  }

  if (track.status === "COMPLETED") {
    controls.append(
      button(
        "Edit",
        () => showEditor(track),
      ),
      button(
        "Re-identify",
        async () => {
          if (
            confirm(
              "Look up metadata again without downloading the audio?",
            )
          ) {
            await action(
              track,
              "reidentify",
            );
          }
        },
      ),
      button(
        "Redownload",
        async () => {
          if (
            confirm(
              "Download a replacement? The existing file is kept until the replacement succeeds.",
            )
          ) {
            await action(
              track,
              "redownload",
            );
          }
        },
      ),
    );
  }

  if (track.status === "NEEDS_APPROVAL") {
    const select = node(
      "select",
      null,
      "approval",
    );

    select.setAttribute(
      "aria-label",
      "Metadata candidate",
    );

    track.choices.forEach(
      (choice, index) => {
        const description =
          choice.description
            ? ` | ${choice.description}`
            : "";

        const option = node(
          "option",
          `${choice.similarity}% | ${choice.artist} - ${choice.title}${description}`,
        );

        option.value = String(index);

        select.append(option);
      },
    );

    const original = node(
      "option",
      "Keep existing / source metadata (no match)",
    );

    original.value = "original";

    select.append(original);

    select.value =
      state.approvals.get(track.id)
      ?? (
        track.choices.length
          ? "0"
          : "original"
      );

    select.addEventListener(
      "change",
      () => {
        state.approvals.set(
          track.id,
          select.value,
        );
      },
    );

    controls.append(
      select,
      button(
        "Approve",
        () => action(
          track,
          "approve",
          {
            index:
              select.value === "original"
                ? null
                : Number(select.value),
          },
        ),
      ),
    );
  }

  actions.append(controls);

  row.append(
    title,
    playlists,
    discovered,
    status,
    actions,
  );

  return row;
}

function trackSignature(track) {
  // Include only values that affect the rendered row.
  // Unchanged tracks keep the exact same DOM node across polls.
  return stable({
    title: track.title,
    url: track.url,
    matched_title: track.matched_title,
    mbid: track.mbid,
    release_id: track.release_id,
    playlists: track.playlists,
    discovered_at: track.discovered_at,
    discovery_basis: track.discovery_basis,
    status: track.status,
    error: track.error,
    choices: track.choices,
  });
}

function updateStats(stats) {
  const values = [
    [
      "Total",
      Object.values(stats).reduce(
        (sum, count) => sum + count,
        0,
      ),
    ],
    [
      "Completed",
      stats.COMPLETED || 0,
    ],
    [
      "Queued / processing",
      (stats.PENDING || 0)
      + (stats.PROCESSING || 0),
    ],
    [
      "Approval",
      stats.NEEDS_APPROVAL || 0,
    ],
    [
      "Failed",
      stats.FAILED || 0,
    ],
  ];

  updateKeyedChildren(
    $("stats"),
    values,
    {
      key: ([label]) => label,
      signature:
        ([label, count]) =>
          `${label}:${count}`,
      render: ([label, count]) => {
        const element = node(
          "span",
          null,
          "stat",
        );

        element.append(
          node(
            "strong",
            count,
          ),
          document.createTextNode(label),
        );

        return element;
      },
      background: true,
    },
  );
}

async function loadTracks(
  generation,
  background = false,
) {
  const data = await api(
    `/api/tracks?user_id=${encodeURIComponent(state.user)}`
      + `&page=${state.page}`
      + "&limit=50"
      + `&status=${encodeURIComponent($("filter").value)}`,
  );

  if (generation !== state.generation) {
    return;
  }

  state.maxPage = Math.max(
    1,
    Math.ceil(
      data.total / data.limit,
    ),
  );

  if (state.page > state.maxPage) {
    state.page = state.maxPage;

    return loadTracks(
      generation,
      background,
    );
  }

  setText(
    $("page"),
    `${data.page} / ${state.maxPage} (${data.total} tracks)`,
  );

  $("prev").disabled =
    state.page <= 1;

  $("next").disabled =
    state.page >= state.maxPage;

  updateStats(data.stats);

  updateKeyedChildren(
    $("tracks"),
    data.tracks,
    {
      key: track => track.id,
      signature: trackSignature,
      render: renderTrack,
      background,
      empty: () => {
        const row = node("tr");

        const cell = node(
          "td",
          "No tracks in this view.",
          "empty",
        );

        cell.colSpan = 5;

        row.append(cell);

        return row;
      },
    },
  );
}

function eventNode(event) {
  return node(
    "div",
    `${event.at} ${event.level}`
      + `${
        event.target
          ? ` [${event.target.slice(0, 12)}]`
          : ""
      }`
      + ` ${event.message}`,
    `log-${event.level}`,
  );
}

function eventIsVisible(event) {
  return (
    !$("errorsOnly").checked
    || [
      "WARNING",
      "ERROR",
      "CRITICAL",
    ].includes(event.level)
  );
}

function renderEvents() {
  const box = $("logs");

  const atBottom =
    box.scrollTop
      + box.clientHeight
    >= box.scrollHeight - 40;

  const visible =
    state.events.filter(
      eventIsVisible,
    );

  box.replaceChildren(
    ...visible.map(eventNode),
  );

  if (atBottom) {
    box.scrollTop =
      box.scrollHeight;
  }
}

function appendEvents(events) {
  if (!events.length) {
    return;
  }

  const box = $("logs");

  const atBottom =
    box.scrollTop
      + box.clientHeight
    >= box.scrollHeight - 40;

  for (const event of events) {
    if (eventIsVisible(event)) {
      box.append(
        eventNode(event),
      );
    }
  }

  // Keep the browser DOM bounded too.
  // Never remove nodes underneath an active text selection.
  if (!selectionTouches(box)) {
    while (
      box.childElementCount > 2000
    ) {
      box.firstElementChild?.remove();
    }
  }

  if (atBottom) {
    box.scrollTop =
      box.scrollHeight;
  }
}

async function loadEvents(generation) {
  const events = await api(
    `/api/events?user_id=${encodeURIComponent(state.user)}&after=${state.after}`,
  );

  if (generation !== state.generation) {
    return;
  }

  if (!events.length) {
    return;
  }

  state.after =
    events.at(-1).id;

  state.events.push(
    ...events,
  );

  state.events =
    state.events.slice(-2000);

  appendEvents(events);
}

async function loadSystem(
  background = false,
) {
  const data = await api(
    "/api/system",
  );

  const signature =
    stable(data);

  const runtime =
    $("runtime");

  if (
    signature
    === state.systemSignature
  ) {
    return;
  }

  if (
    background
    && elementIsBusy(runtime)
  ) {
    return;
  }

  const when = value =>
    value
      ? new Date(
          value * 1000,
        ).toLocaleString()
      : "Not scheduled";

  const values = [
    [
      "Beets",
      data.beets,
    ],
    [
      "yt-dlp",
      data.downloader.version
      || data.downloader.error,
    ],
    [
      "Next update",
      when(data.next_update),
    ],
    [
      "Last update",
      data.last_update
        ? data.last_update.status
          + (
            data.last_update.error
              ? `: ${data.last_update.error}`
              : ""
          )
        : "Bundled runtime",
    ],
  ];

  runtime.replaceChildren(
    ...values.flatMap(
      ([label, value]) => [
        node(
          "dt",
          label,
        ),
        node(
          "dd",
          value,
        ),
      ],
    ),
  );

  state.systemSignature =
    signature;
}

function reportSettledErrors(results) {
  const errors =
    results.filter(
      result =>
        result.status === "rejected",
    );

  if (errors.length) {
    notice(
      errors
        .map(
          result =>
            result.reason?.message
            || String(result.reason),
        )
        .join("\n"),
      true,
    );
  }
}

async function refreshFast({
  background = true,
} = {}) {
  if (!state.user) {
    return;
  }

  const generation =
    state.generation;

  const results =
    await Promise.allSettled([
      loadTracks(
        generation,
        background,
      ),
      loadEvents(
        generation,
      ),
    ]);

  reportSettledErrors(
    results,
  );
}

async function refreshSlow({
  background = true,
} = {}) {
  if (!state.user) {
    return;
  }

  const generation =
    state.generation;

  const results =
    await Promise.allSettled([
      loadSources(
        generation,
        background,
      ),
      loadSystem(
        background,
      ),
    ]);

  reportSettledErrors(
    results,
  );
}

async function refreshAll({
  background = false,
} = {}) {
  const results =
    await Promise.allSettled([
      refreshFast({
        background,
      }),
      refreshSlow({
        background,
      }),
    ]);

  reportSettledErrors(
    results,
  );
}

async function pollFast() {
  if (
    document.hidden
    || state.fastPolling
    || !state.user
  ) {
    return;
  }

  state.fastPolling = true;

  try {
    await refreshFast({
      background: true,
    });
  } finally {
    state.fastPolling = false;
  }
}

async function pollSlow() {
  if (
    document.hidden
    || state.slowPolling
    || !state.user
  ) {
    return;
  }

  state.slowPolling = true;

  try {
    await refreshSlow({
      background: true,
    });
  } finally {
    state.slowPolling = false;
  }
}

$("filter").addEventListener(
  "change",
  () => {
    state.page = 1;
    state.generation += 1;

    guard(
      () =>
        refreshFast({
          background: false,
        }),
    )();
  },
);

$("prev").addEventListener(
  "click",
  guard(async () => {
    if (state.page > 1) {
      state.page -= 1;
    }

    await refreshFast({
      background: false,
    });
  }),
);

$("next").addEventListener(
  "click",
  guard(async () => {
    if (
      state.page
      < state.maxPage
    ) {
      state.page += 1;
    }

    await refreshFast({
      background: false,
    });
  }),
);

$("errorsOnly").addEventListener(
  "change",
  renderEvents,
);

$("syncAll").addEventListener(
  "click",
  guard(async () => {
    for (
      const source
      of state.sources.filter(
        source =>
          source.monitored
          && ![
            "PENDING",
            "SYNCING",
          ].includes(
            source.status,
          ),
      )
    ) {
      await api(
        `/api/sources/${encodeURIComponent(source.id)}/retry`,
        {
          user_id:
            state.user,
        },
      );
    }

    notice(
      "Monitored sources queued",
    );

    await refreshSlow({
      background: false,
    });
  }),
);

for (
  const [id, mode, message]
  of [
    [
      "approveBest",
      "best",
      "Approve the top candidate for all waiting tracks? This overrides beets' uncertainty.",
    ],
    [
      "approveOriginal",
      "original",
      "Keep existing/source metadata for all waiting tracks without matching? Unknown album/release dates will not be invented.",
    ],
    [
      "retryAll",
      "retry",
      "Retry all failed tracks for this user?",
    ],
  ]
) {
  $(id).addEventListener(
    "click",
    guard(async () => {
      if (!confirm(message)) {
        return;
      }

      const result =
        await api(
          "/api/batch",
          {
            user_id:
              state.user,
            mode,
          },
        );

      notice(
        result.message,
      );

      await refreshFast({
        background: false,
      });
    }),
  );
}

$("update").addEventListener(
  "click",
  guard(async () => {
    const result =
      await api(
        "/api/downloader/update",
        {},
      );

    notice(
      result.message,
    );

    await loadSystem(
      false,
    );
  }),
);

$("rollback").addEventListener(
  "click",
  guard(async () => {
    if (
      !confirm(
        "Switch future downloads to the previous yt-dlp runtime? The nightly schedule remains enabled.",
      )
    ) {
      return;
    }

    const result =
      await api(
        "/api/downloader/rollback",
        {},
      );

    notice(
      result.message,
    );

    await loadSystem(
      false,
    );
  }),
);

window.addEventListener(
  "unhandledrejection",
  event => {
    notice(
      event.reason?.message
      || String(event.reason),
      true,
    );
  },
);

window.addEventListener(
  "error",
  event => {
    notice(
      event.message,
      true,
    );
  },
);

document.addEventListener(
  "visibilitychange",
  () => {
    if (!document.hidden) {
      guard(
        () =>
          refreshAll({
            background: true,
          }),
      )();
    }
  },
);

(async () => {
  try {
    await loadUsers(
      localStorage.getItem(
        "music-user",
      ),
    );
  } catch (error) {
    notice(
      error?.message
      || String(error),
      true,
    );
  } finally {
    setInterval(
      () => guard(pollFast)(),
      FAST_POLL_MS,
    );

    setInterval(
      () => guard(pollSlow)(),
      SLOW_POLL_MS,
    );
  }
})();