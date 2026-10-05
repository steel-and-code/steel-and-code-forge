/* Drives app.js's derived activity line against a stub DOM.
 *
 * The activity line is the one piece of UI that must never be wrong in a
 * particular direction: it is what an operator reads to decide whether Forge is
 * working, stuck, or done. So this probe exists to make its output *checkable*
 * rather than merely plausible.
 *
 * It prints:
 *   { steps: [{label, kind}], storage: [...], sheets: [...], note }
 *
 *   label  -- the text in #activity-text after that step
 *   kind   -- the data-kind on #activity-line after that step
 *   storage-- every storage API app.js called, which must be empty
 *   sheets -- the activity sheet's open/closed state after that step
 *
 * Usage: node activity_probe.js '<json steps>'
 *
 * Each step is one of
 *   {"status": {...}}    the next /api/status answer
 *   {"event": name, data: {...}}   delivered to the registered listener
 *   {"toggle": true}     click the Activity toggle button
 *   {"close": true}      click the Activity close button
 *   {"escape": true}     press Escape
 *   {"age": ms}          advance the probe's clock, firing the liveness timer
 *
 * The clock is the probe's, not the host's. app.js measures silence with
 * `Date.now()`, so driving the liveness counter requires a clock the probe
 * controls -- otherwise a test of "no activity for 47s" would have to actually
 * wait 47 seconds, and a test that waits is a test that gets deleted.
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { StubDocument, StubElement } = require(
  path.join(__dirname, "..", "ui_stub", "dom.js")
);

const APP_JS = process.env.APP_JS;

// Every id app.js resolves at load. The four activity ids are new in this
// strike; the rest are the set the existing probes established, kept identical
// so a change to one probe cannot silently break the others.
const IDS = [
  "workspace", "agent", "model", "state-chip", "state-label", "models",
  "models-recheck", "models-note", "stream-hint", "thread", "feed",
  "activity-line", "activity-text", "activity-toggle", "activity-close",
  "composer", "task", "send", "approval-overlay", "approval-fields",
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

  // The feed's only innerHTML is a hard-coded literal, modelled here rather than
  // by relaxing the stub -- see the same note in final_answer_probe.js.
  Object.defineProperty(nodes.feed, "innerHTML", {
    value: "",
    writable: true,
    configurable: true,
  });
  nodes.feed.querySelector = function () {
    return null;
  };

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

  // `document.body` is what the sheet state is written to. The stub has no
  // body of its own, so it is modelled here as an attribute bag -- which is all
  // app.js uses it for.
  const body = new StubElement("body", doc);
  body.attributes = {};
  doc.body = body;

  // The click path for the two sheet controls. app.js registers a listener with
  // addEventListener; firing it directly is what lets a test drive the sheet
  // without a real browser.
  doc._nodes = nodes;
  doc._body = body;
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

  /* The probe's clock.
   *
   * `now` is the only time app.js's liveness logic can see, and `intervals` are
   * the timers it registered. A step with `age` advances the clock and fires due
   * intervals, so "no activity for 47s" is asserted by moving a number rather
   * than by sleeping.
   */
  let now = 1000000;
  const intervals = new Map();
  let nextTimer = 1;

  /* Storage calls app.js made, recorded so a test can assert there were none.
   *
   * The file's own rule is NO STORAGE, and an assertion about it is only
   * meaningful if the probe would notice a violation. This is that notice.
   */
  const storage = [];

  const sandbox = {
    console: console,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    JSON: JSON,
    Date: {
      now: function () {
        return now;
      },
    },
    encodeURIComponent: encodeURIComponent,
    Promise: Promise,
    document: doc,
    window: {
      setTimeout: setTimeout,
      console: console,
      // The liveness timer is a window interval. Modelled explicitly, because
      // a real one would keep this probe alive forever.
      setInterval: function (fn, ms) {
        const id = nextTimer++;
        intervals.set(id, { fn: fn, ms: ms, due: now + ms });
        return id;
      },
      clearInterval: function (id) {
        intervals.delete(id);
      },
    },
    fetch: function (url) {
      if (url === "/api/status") return response(statusPayload);
      if (url === "/api/events-ticket") return response({ ticket: "t" });
      if (url === "/api/models") return response({ tiers: [] });
      if (String(url).indexOf("/api/orders") === 0) {
        return response({ columns: [], statuses: [], priorities: [] });
      }
      throw new Error("unexpected fetch: " + url);
    },
    localStorage: {
      setItem: function (k, v) { storage.push("localStorage.setItem:" + k); },
      getItem: function (k) { storage.push("localStorage.getItem:" + k); return null; },
      removeItem: function (k) { storage.push("localStorage.removeItem:" + k); },
    },
    sessionStorage: {
      setItem: function (k, v) { storage.push("sessionStorage.setItem:" + k); },
      getItem: function (k) { storage.push("sessionStorage.getItem:" + k); return null; },
      removeItem: function (k) { storage.push("sessionStorage.removeItem:" + k); },
    },
    indexedDB: {
      open: function (k) { storage.push("indexedDB.open:" + k); },
    },
  };
  sandbox.window.document = doc;
  sandbox.globalThis = sandbox;

  sandbox.EventSource = function FakeEventSource() {
    this.close = function () {};
    this.addEventListener = function (name, handler) {
      (listeners[name] = listeners[name] || []).push(handler);
    };
  };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), sandbox, { filename: "app.js" });
  await drain();

  function readLine() {
    const text = doc._nodes["activity-text"].textContent;
    const kind = doc._nodes["activity-line"].getAttribute("data-kind");
    return { label: text, kind: kind };
  }

  function click(id) {
    const node = doc._nodes[id];
    // The stub's own `click()` fires the registered handlers. Used rather than
    // a hand-rolled dispatch so the probe goes through the same path a browser
    // would, and a listener registered by a different mechanism still fires.
    node.click();
  }

  function advance(ms) {
    now += ms;
    // Fire every interval whose deadline has passed, in deadline order, so a
    // multi-second advance produces the same text a real one would.
    const due = [];
    intervals.forEach(function (entry, id) {
      if (entry.due <= now) due.push({ id: id, entry: entry });
    });
    due.sort(function (a, b) { return a.entry.due - b.entry.due; });
    due.forEach(function (item) {
      item.entry.fn();
      item.entry.due = now + item.entry.ms;
    });
  }

  const observed = [];
  for (const step of steps) {
    if (step.status !== undefined) statusPayload = step.status;
    if (step.age !== undefined) advance(step.age);
    if (step.toggle) click("activity-toggle");
    if (step.close) click("activity-close");
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
    // A `status` step means "the page asks the server and gets this". Setting
    // the payload alone would not fetch anything -- app.js asks on load, on
    // approval.required, on run.completed, and on visibilitychange -- so the
    // step would silently assert against a payload nobody read. Firing the
    // document listener is the same read a backgrounded-then-focused tab makes,
    // and it is what makes a status step actually reach the code under test.
    //
    // `focus: false` opts out, for the case where the test wants to model a
    // read that is already in flight (see the stale-snapshot test).
    if (step.status !== undefined && step.focus !== false) {
      (doc._documentListeners["visibilitychange"] || []).forEach(function (handler) {
        handler({ type: "visibilitychange" });
      });
    }
    if (step.escape) {
      (doc._documentListeners["keydown"] || []).forEach(function (handler) {
        handler({ key: "Escape" });
      });
    }
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
    await drain();
    const line = readLine();
    // Per-step, not just final: the sheet's state is the thing several tests
    // assert, and only the transition is interesting -- a run of toggles that
    // ends closed looks identical to one that never opened.
    line.open = doc._body.getAttribute("data-activity-open") === "true";
    line.ariaExpanded = doc._nodes["activity-toggle"].getAttribute("aria-expanded");
    observed.push(line);
  }

  process.stdout.write(
    JSON.stringify({
      steps: observed,
      storage: storage,
      intervalsRunning: intervals.size,
    })
  );
}

main().catch(function (error) {
  process.stderr.write(String((error && error.stack) || error));
  process.exit(1);
});
