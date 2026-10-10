/* Shared request ordering and result labels. No DOM or framework dependency. */
(function (root) {
  "use strict";
  class RequestGate {
    constructor() { this.epoch = 0; this.sequence = new Map(); }
    reset() { this.epoch += 1; this.sequence.clear(); }
    begin(scope) {
      const epoch = this.epoch;
      const number = (this.sequence.get(scope) || 0) + 1;
      this.sequence.set(scope, number);
      return () => this.epoch === epoch && this.sequence.get(scope) === number;
    }
  }
  function resultsLabel(page, limit, total, libraryTotal) {
    const first = total ? (page - 1) * limit + 1 : 0;
    const last = Math.min(page * limit, total);
    return `Showing ${first.toLocaleString()}\u2013${last.toLocaleString()} of ${total.toLocaleString()} matching tracks. Library total: ${libraryTotal.toLocaleString()}.`;
  }
  const api = {RequestGate, resultsLabel};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.IngestorUI = api;
})(globalThis);
