/* Shared request ordering and result labels. No DOM or framework dependency. */
(function (root) {
  "use strict";
  class StateMachine {
    #state;
    constructor(name, initial, graph) {
      this.name = name;
      this.graph = Object.freeze(Object.fromEntries(Object.entries(graph).map(
        ([state, events]) => [state, Object.freeze({...events})]
      )));
      if (!Object.hasOwn(this.graph, initial) || Object.values(this.graph).some(
        events => Object.values(events).some(target => !Object.hasOwn(this.graph, target))
      )) throw new Error(`Invalid state machine: ${name}`);
      this.#state = initial;
    }
    get state() { return this.#state; }
    can(event) { return Object.hasOwn(this.graph[this.#state], event); }
    send(event) {
      if (!this.can(event)) throw new Error(`${this.name}: cannot ${event} from ${this.#state}`);
      this.#state = this.graph[this.#state][event];
      return this.#state;
    }
  }

  const ASYNC_GRAPH = Object.freeze({
    idle: {start: "pending", reset: "idle"},
    pending: {start: "pending", succeed: "ready", fail: "failed", reset: "idle"},
    ready: {start: "pending", reset: "idle"},
    failed: {start: "pending", reset: "idle"},
  });
  class AsyncState {
    constructor(name) {
      this.machine = new StateMachine(name, "idle", ASYNC_GRAPH);
      this.epoch = 0;
    }
    get state() { return this.machine.state; }
    get pending() { return this.state === "pending"; }
    start() { this.epoch += 1; this.machine.send("start"); return this.epoch; }
    reset() { this.epoch += 1; this.machine.send("reset"); }
    current(epoch) { return epoch === this.epoch; }
    finish(epoch = this.epoch) {
      if (!this.current(epoch) || !this.pending) return false;
      this.machine.send("succeed"); return true;
    }
    fail(epoch = this.epoch) {
      if (!this.current(epoch) || !this.pending) return false;
      this.machine.send("fail"); return true;
    }
  }
  function selectionMachine(name, values, initial) {
    const events = Object.fromEntries(values.map(value => [value, value]));
    return new StateMachine(name, initial, Object.fromEntries(values.map(value => [value, events])));
  }
  class RequestGate {
    constructor() { this.epoch = 0; this.tasks = new Map(); }
    reset() {
      this.epoch += 1;
      for (const task of this.tasks.values()) task.reset();
      this.tasks.clear();
    }
    begin(scope) {
      const epoch = this.epoch;
      const task = this.tasks.get(scope) || new AsyncState(`request:${scope}`);
      this.tasks.set(scope, task);
      const token = task.start();
      const current = () => this.epoch === epoch && this.tasks.get(scope) === task && task.current(token);
      current.succeed = () => current() && task.finish(token);
      current.fail = () => current() && task.fail(token);
      return current;
    }
    state(scope) { return this.tasks.get(scope)?.state || "idle"; }
  }
  function resultsLabel(page, limit, total, libraryTotal) {
    const first = total ? (page - 1) * limit + 1 : 0;
    const last = Math.min(page * limit, total);
    return `Showing ${first.toLocaleString()}\u2013${last.toLocaleString()} of ${total.toLocaleString()} matching tracks. Library total: ${libraryTotal.toLocaleString()}.`;
  }
  const api = {StateMachine, AsyncState, selectionMachine, RequestGate, resultsLabel};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.IngestorUI = api;
})(globalThis);
