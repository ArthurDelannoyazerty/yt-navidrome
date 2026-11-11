/* Load only the flowchart renderer, and only when the Help tab is opened. */
"use strict";
(() => {
  const panel = document.querySelector('[data-panel="help"]');
  const container = document.getElementById("workflowDiagram");
  if (!panel || !container) return;
  const status = document.getElementById("workflowDiagramStatus");
  const zoomControls = document.getElementById("workflowZoom");
  const assetURL = new URL("pele-flowchart.js", document.currentScript.src);
  assetURL.search = new URL(document.currentScript.src).search;
  const source = `flowchart LR
    accTitle: Music operation states
    accDescr: Imports enter Queued, then Processing. A review decision sends Waiting for approval back to Queued. Successful operations become Idle. Deferred work retries automatically. Failed or Stopped work needs a user action before returning to Queued. Audio availability is independent of these states.
    queued["Queued"] -->|Worker starts|processing["Processing"]
    processing -->|Review needed|review["Waiting for approval"]
    review -->|Use or approve|queued
    processing -->|Success|idle["Idle"]
    idle -.->|New operation|queued
    processing -->|Temporary problem|deferred["Deferred"]
    deferred -->|Retry time|processing
    processing -->|Error|failed["Failed"]
    failed -->|Retry|queued
    processing -->|URL disabled or restricted|stopped["Stopped"]
    stopped -->|Fix source, then retry|queued
  `;
  let loading = null, diagram = null;

  function diagramNode(svg) {
    // Pele puts its default variables in an inline style. Keep the application's
    // strict CSP: the equivalent defaults live in style.css instead.
    // This is fixed application-authored SVG from the pinned renderer, whose
    // attributes are double-quoted. Remove styles before any DOM parsing.
    const root = new DOMParser().parseFromString(svg.replace(/\sstyle="[^"]*"/g, ""), "image/svg+xml").documentElement;
    return document.importNode(root, true);
  }

  function loadRenderer() {
    if (window.IngestorPele) return Promise.resolve(window.IngestorPele);
    if (!loading) loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = assetURL.href;
      script.addEventListener("load", () => {
        if (window.IngestorPele) resolve(window.IngestorPele);
        else { loading = null; script.remove(); reject(new Error("Diagram renderer unavailable")); }
      }, {once: true});
      script.addEventListener("error", () => {
        loading = null; script.remove(); reject(new Error("Diagram renderer unavailable"));
      }, {once: true});
      document.head.append(script);
    });
    return loading;
  }

  async function enter() {
    if (panel.hidden) return;
    if (diagram) { diagram.refresh(); return; }
    status.hidden = false;
    status.textContent = "Loading workflow diagram…";
    try {
      const pele = await loadRenderer();
      if (panel.hidden || diagram) return;
      diagram = pele.mount(container, source, {
        fontSize: 14, padding: 24, links: false, images: false,
        sanitize: diagramNode,
        // Pele's own toolbar uses inline style attributes. These app buttons
        // use external CSS while keeping Pele's pan, pinch and keyboard zoom.
        zoom: {always: true, controls: false},
        onRender: () => { status.hidden = true; zoomControls.hidden = false; },
        onError: () => {
          status.hidden = false;
          zoomControls.hidden = true;
          status.textContent = "The diagram could not be drawn. All steps are described in the transition table below.";
        },
      });
    } catch (_) {
      status.textContent = "The diagram could not be loaded. All steps are described in the transition table below.";
    }
  }

  document.getElementById("workflowZoomIn").addEventListener("click", () => diagram?.zoom?.zoomBy(1.4));
  document.getElementById("workflowZoomOut").addEventListener("click", () => diagram?.zoom?.zoomBy(1 / 1.4));
  document.getElementById("workflowZoomReset").addEventListener("click", () => diagram?.zoom?.reset());
  new MutationObserver(enter).observe(panel, {attributes: true, attributeFilter: ["hidden"]});
  window.addEventListener("pagehide", () => { diagram?.destroy(); diagram = null; });
  window.addEventListener("pageshow", enter);
  enter();
})();
