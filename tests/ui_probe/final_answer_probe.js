/* Drives app.js's final-answer reconciliation against a stub DOM.
 *
 * app.js is an IIFE that reads the DOM, fetches, and listens on an
 * EventSource, so the probe supplies all three and then performs a scripted
 * sequence of server answers and stream events, printing the transcript
 * afterwards as JSON. That is what lets a test assert on what an operator
 * would actually read -- one entry or two, clipped or complete -- rather than
 * on the presence of a function call.
 *
 * The probe prints:
 *   { entries: [{role, text}], statusFetches: n }
 *
 * Usage: node final_answer_probe.js '<json steps>'
 *
 * Each step is either
 *   {"status": {...}}      the next /api/status answer
 *   {"event": name, data: {...}}   delivered to the registered listener
 * and each step waits for pending promise jobs to drain before the next.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { StubDocument } = require(path.join(__dirname, "..", "ui_stub", "dom.js"));

const APP_JS = process.env.APP_JS;

// Every id app.js resolves at load. Absent ones would be null and app.js guards
// most of them; the transcript and the composer are the ones it does not.
const IDS = [
  "workspace", "agent", "model", "state-chip", "state-label", "models",
  "models-recheck", "models-note", "stream-hint", "thread", "feed", "composer",
  "task", "send", "approval-overlay", "approval-fields", "approval-id",
  "approval-note", "approve", "reject",
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

  // app.js seeds the feed with innerHTML and looks for the resulting marker.
  // The stub DOM refuses innerHTML on purpose -- for the board and health
  // renderers, where operator- or provider-supplied text must never be parsed
  // as markup. The feed's only innerHTML is a hard-coded literal this file
  // also contains, so the seed is modelled here rather than by relaxing the
  // stub for the renderers that must not use it.
  Object.defineProperty(nodes.feed, "innerHTML", {
    value: "",
    writable: true,
    configurable: true,
  });
  nodes.feed.querySelector = function () {
    return this.innerHTML ? null : null;
  };

  doc.getElementById = function (id) {
    return nodes[id] || null;
  };
  doc.querySelector = function () {
    return { content: "probe-token" };
  };
  // app.js registers one document-level listener for `visibilitychange`, and a
  // tab that was backgrounded when the run finished takes exactly that path.
  // Recording it here is what lets a test drive a refresh without a real tab.
  const documentListeners = {};
  doc.addEventListener = function (name, handler) {
    (documentListeners[name] = documentListeners[name] || []).push(handler);
  };
  doc.hidden = false;
  doc._nodes = nodes;
  doc._documentListeners = documentListeners;
  return doc;
}

/* The fake stream: app.js registers one listener per event name, and the
 * probe delivers frames by calling the ones it registered. A name app.js never
 * registered is an error here rather than a silent no-op, because a missing
 * listener is exactly the defect this file exists to catch. */
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
  let statusFetches = 0;

  function response(body, ok, status) {
    return Promise.resolve({
      ok: ok === undefined ? true : ok,
      status: status === undefined ? 200 : status,
      json: function () {
        return Promise.resolve(body);
      },
    });
  }

  // The fake stream's listener table lives here rather than in the sandbox, so
  // the probe can deliver frames after app.js has finished registering.
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
    fetch: function (url) {
      if (url === "/api/status") {
        statusFetches += 1;
        return response(statusPayload);
      }
      if (url === "/api/events-ticket") return response({ ticket: "t" });
      if (url === "/api/models") return response({ tiers: [] });
      if (String(url).indexOf("/api/orders") === 0) return response({ columns: [], statuses: [], priorities: [] });
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
    if (step.status !== undefined) statusPayload = step.status;
    if (step.visible !== undefined) doc.hidden = !step.visible;
    if (step.focus) {
      (doc._documentListeners["visibilitychange"] || []).forEach(function (handler) {
        handler({ type: "visibilitychange" });
      });
    }
    if (step.event) {
      const handlers = listeners[step.event];
      if (!handlers || !handlers.length) {
        throw new Error("app.js registered no listener for " + step.event);
      }
      handlers.forEach(function (handler) {
        handler({
          data: JSON.stringify(Object.assign({ seq: step.seq || 1, event: step.event }, step.data || {})),
        });
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

  process.stdout.write(JSON.stringify({ entries: entries, statusFetches: statusFetches }));
}

main().catch(function (error) {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});