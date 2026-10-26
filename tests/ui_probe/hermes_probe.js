/* Drives app.js's Byte/Hermes run routing and Hermes-answer rendering against a
 * stub DOM.
 *
 * FORGE-20 added a run-mode selector and the text-only Hermes path. This probe
 * exercises three things the shipped app.js must do, all of which are otherwise
 * unobservable from the HTTP layer:
 *
 *   - a send with the selector on "hermes" POSTs to /api/run-hermes, and on
 *     "byte" to /api/run;
 *   - a completed Hermes run renders its answer from /api/status's
 *     `hermes_result.text` into the conversation, exactly once;
 *   - a failed Hermes run states the failure instead of fabricating an answer.
 *
 * It prints:
 *   { entries: [{role, text}], runs: [url, ...] }
 *
 *   runs -- every POST body-URL app.js issued, newest last, so a test can assert
 *   which endpoint a send actually hit without trusting the UI's own words.
 *
 * Usage: node hermes_probe.js '<json steps>'
 *
 * Each step is one of
 *   {"mode": "hermes"|"byte"}   set the run-mode selector's value
 *   {"task": "..."}             set the composer's task text
 *   {"submit": true}            fire the composer's submit handler
 *   {"status": {...}}           the next /api/status answer (and a refresh)
 *   {"event": name, data: {...}}  delivered to the registered listener
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { StubDocument } = require(path.join(__dirname, "..", "ui_stub", "dom.js"));

const APP_JS = process.env.APP_JS;

// Every id app.js resolves at load. `run-mode` is the FORGE-20 addition; the
// rest are the set the existing probes established, kept identical so this
// probe cannot drive a page the others cannot.
const IDS = [
  "workspace", "agent", "model", "state-chip", "state-label", "models",
  "models-recheck", "models-note", "stream-hint", "thread", "feed", "composer",
  "task", "run-mode", "send", "approval-overlay", "approval-fields",
  "approval-id", "approval-note", "approve", "reject",
  "board-view", "workbench-view", "view-switch", "board", "board-note",
  "order-form", "order-title", "order-priority", "order-smith",
  "order-overlay", "order-detail", "order-close",
];

function makeDocument() {
  const doc = new StubDocument();
  const nodes = {};
  IDS.forEach(function (id) {
    const node = doc.createElement(id === "thread" ? "ol" : "div");
    node.setAttribute("data-id", id);
    nodes[id] = node;
  });

  // The feed's only innerHTML is a hard-coded literal. Same note as the other
  // probes: modelled here rather than by relaxing the stub.
  Object.defineProperty(nodes.feed, "innerHTML", {
    value: "",
    writable: true,
    configurable: true,
  });
  nodes.feed.querySelector = function () {
    return null;
  };

  // The two controls this probe drives at send time. The stub has no `.value`
  // of its own, so it is attached here -- a real <select> and <textarea> carry
  // one, and app.js reads exactly this field.
  nodes["run-mode"].value = "byte";
  nodes.task.value = "";

  doc.getElementById = function (id) {
    return nodes[id] || null;
  };
  doc.querySelector = function () {
    return { content: "probe-token" };
  };

  const documentListeners = {};
  doc.addEventListener = function (name, handler) {
    (documentListeners[name] = documentListeners[name] || []).push(handler);
  };
  doc.hidden = false;
  doc._nodes = nodes;
  doc._documentListeners = documentListeners;
  return doc;
}

const drain = () => new Promise(function (resolve) {
  setImmediate(function () {
    setImmediate(function () {
      setImmediate(resolve);
    });
  });
});

async function main() {
  const steps = JSON.parse(process.argv[2] || "[]");
  const doc = makeDocument();

  let statusPayload = { state: "idle", pending_approval: null, result: null };
  const runs = [];

  function response(body, ok, status) {
    return Promise.resolve({
      ok: ok === undefined ? true : ok,
      status: status === undefined ? 200 : status,
      json: function () {
        return Promise.resolve(body);
      },
    });
  }

  const listeners = {};

  const sandbox = {
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    JSON: JSON,
    encodeURIComponent: encodeURIComponent,
    Promise: Promise,
    document: doc,
    window: { setTimeout: setTimeout, console: console },
    fetch: function (url, options) {
      if (url === "/api/status") return response(statusPayload);
      if (url === "/api/events-ticket") return response({ ticket: "t" });
      if (url === "/api/models") return response({ tiers: [] });
      if (url === "/api/run-hermes" || url === "/api/run") {
        runs.push(url);
        return response({ run_id: "run-hermes-0001", state: "running" }, true, 202);
      }
      if (String(url).indexOf("/api/orders") === 0) {
        return response({ columns: [], statuses: [], priorities: [] });
      }
      throw new Error("unexpected fetch: " + url);
    },
  };
  sandbox.window.document = doc;

  sandbox.EventSource = function FakeEventSource() {
    this.close = function () {};
    this.addEventListener = function (name, handler) {
      (listeners[name] = listeners[name] || []).push(handler);
    };
  };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), sandbox, { filename: "app.js" });
  await drain();

  for (const step of steps) {
    if (step.mode !== undefined) doc._nodes["run-mode"].value = step.mode;
    if (step.task !== undefined) doc._nodes.task.value = step.task;
    if (step.status !== undefined) statusPayload = step.status;
    if (step.event) {
      const handlers = listeners[step.event];
      if (!handlers || !handlers.length) {
        throw new Error("app.js registered no listener for " + step.event);
      }
      handlers.forEach(function (handler) {
        handler({
          data: JSON.stringify(
            Object.assign({ seq: step.seq || 1, event: step.event }, step.data || {})
          ),
        });
      });
    }
    if (step.submit) {
      const handlers = doc._nodes.composer.listeners.submit || [];
      handlers.forEach(function (handler) {
        handler({ preventDefault: function () {} });
      });
    }
    await drain();
  }

  const entries = doc._nodes.thread.children.map(function (node) {
    const role = node.children[0];
    const body = node.children[1];
    return {
      role: role ? role.textContent : "",
      text: body ? body.textContent : "",
    };
  });

  process.stdout.write(JSON.stringify({ entries: entries, runs: runs }));
}

main().catch(function (error) {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});