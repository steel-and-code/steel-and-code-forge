/* Forge Workbench — page behaviour.
 *
 * Four endpoints and one stream:
 *
 *   GET  /api/status           token + same-origin -> current state, approval
 *   GET  /api/models           token + same-origin -> cached model health
 *   POST /api/models/verify    token + origin      -> one bounded re-check
 *   POST /api/run              token + origin      -> 202, run id
 *   POST /api/events-ticket    token + origin      -> one-shot stream ticket
 *   GET  /api/events?ticket=   ticket + same-origin -> SSE activity stream
 *   POST /api/approvals/{id}   token + origin      -> the decision
 *
 * "origin" and "same-origin" are different server-side checks, and the split
 * follows what a browser actually sends. A browser attaches `Origin` to a
 * POST and attaches **nothing** to a same-origin GET — so the two GETs are
 * authorized on `Sec-Fetch-Site: same-origin`, which it always sends. This
 * file does not need to do anything about that: it is the browser attaching
 * the header, and no fetch option here can add or override it.
 *
 * The rules this file keeps, and why they are not just style choices:
 *
 * - NO STORAGE. No localStorage, no sessionStorage. A refresh asks
 *   /api/status and renders the server's answer. A pending approval is
 *   recovered from there, with the same id it had before the reload, so an
 *   operator who refreshes mid-decision can still answer the prompt they were
 *   reading.
 *
 * - THE TOKEN IS IN A HEADER ONLY. The SSE stream uses a short-lived ticket
 *   instead, because EventSource cannot set headers. The token is read from a
 *   meta tag the server filled in, kept in a closure, and never put in a URL.
 *
 * - THE SERVER IS THE ONLY SOURCE OF STATE. Nothing here decides anything. A
 *   click posts a decision and then waits to be told what happened; if the
 *   answer is 409, the card says so rather than pretending it worked.
 *
 * - MODEL HEALTH IS DRAWN, NEVER DECIDED. This file fetches the cached
 *   snapshot and hands it to ForgeHealth, which maps the backend's health
 *   strings to glyphs and words. No health value is computed, compared, or
 *   inferred here, and no model or provider name appears in either file -- those
 *   live in models.toml. `tests/test_ui_model_health_ui.py` enforces that.
 *
 * - REFRESHING HEALTH DOES NOT RE-CHECK IT. `refreshModels` reads the cached
 *   snapshot. Only the explicit Re-check button POSTs, because that is the one
 *   request that makes the server contact providers and present credentials. A
 *   page that re-checked on load or on visibility would turn a backgrounded tab
 *   into a repeating credential-bearing request.
 */

(function () {
  "use strict";

  var TOKEN =
    (document.querySelector('meta[name="forge-ui-token"]') || {}).content || "";

  var el = {
    workspace: document.getElementById("workspace"),
    agent: document.getElementById("agent"),
    model: document.getElementById("model"),
    stateChip: document.getElementById("state-chip"),
    stateLabel: document.getElementById("state-label"),
    models: document.getElementById("models"),
    modelsRecheck: document.getElementById("models-recheck"),
    modelsNote: document.getElementById("models-note"),
    streamHint: document.getElementById("stream-hint"),
    thread: document.getElementById("thread"),
    feed: document.getElementById("feed"),
    composer: document.getElementById("composer"),
    task: document.getElementById("task"),
    send: document.getElementById("send"),
    overlay: document.getElementById("approval-overlay"),
    approvalFields: document.getElementById("approval-fields"),
    approvalId: document.getElementById("approval-id"),
    approvalNote: document.getElementById("approval-note"),
    approve: document.getElementById("approve"),
    reject: document.getElementById("reject"),
  };

  // The pending approval id, as the server last reported it. Held in memory
  // only; a refresh re-reads it from /api/status rather than trusting this.
  var pendingApprovalId = null;
  var source = null;

  // -- rendering -------------------------------------------------------

  var STATE_TEXT = {
    idle: "idle",
    running: "running",
    waiting_for_approval: "awaiting your decision",
    completed: "completed",
    failed: "failed",
  };

  function setState(state) {
    var text = STATE_TEXT[state] || state || "unknown";
    el.stateChip.setAttribute("data-state", state || "idle");
    el.stateLabel.textContent = text;
  }

  function entry(role, text) {
    var li = document.createElement("li");
    li.className = "entry";
    li.setAttribute("data-role", role);

    var tag = document.createElement("span");
    tag.className = "role";
    tag.textContent = role;

    var body = document.createElement("div");
    body.className = "body";
    body.textContent = text;

    li.appendChild(tag);
    li.appendChild(body);
    return li;
  }

  function addMessage(role, text) {
    if (!text) return;
    el.thread.appendChild(entry(role, text));
    el.thread.scrollTop = el.thread.scrollHeight;
  }

  var FEED_KIND = {
    "run.started": "",
    "run.completed": "good",
    "run.failed": "bad",
    "run.submitted": "",
    "run.error": "bad",
    "tool.call": "",
    "tool.result": "",
    "approval.required": "warn",
    "approval.answered": "good",
    "session.state": "",
    "stream.dropped": "warn",
    "stream.truncated": "warn",
  };

  // Statuses that mean the operation was not allowed or not done. A tool that
  // came back denied is coloured as a refusal, not as neutral activity --
  // an operator glancing at this pane should notice it without reading.
  var BAD_STATUS = { denied: 1, rejected: 1, denied_unattended: 1, error: 1 };

  function addEvent(name, data) {
    if (el.feed.querySelector(".empty")) el.feed.innerHTML = "";

    var kind = FEED_KIND[name] || "";
    if (name === "tool.result") {
      if (BAD_STATUS[data.status]) kind = "bad";
      else if (data.status === "ok") kind = "good";
    }

    var li = document.createElement("li");
    li.className = "event";
    if (kind) li.setAttribute("data-kind", kind);

    var label = document.createElement("span");
    label.className = "name";
    label.textContent = name.replace(/^(run|tool|approval|session|stream)\./, "");

    // Bounded metadata only. The server already clamps every field; this side
    // just renders what arrives and never reaches for anything it did not get.
    var parts = [];
    Object.keys(data).forEach(function (key) {
      if (key === "seq" || key === "event") return;
      if (key === "tool" && name === "tool.call") return;
      var value = String(data[key]);
      parts.push(key + "=" + value);
    });

    var detail = document.createElement("span");
    detail.className = "detail";
    detail.textContent = parts.join("  ");

    li.appendChild(label);
    li.appendChild(detail);
    el.feed.appendChild(li);
    el.feed.scrollTop = el.feed.scrollHeight;
  }

  function renderPending(pending) {
    pendingApprovalId = pending ? pending.approval_id : null;

    if (!pending) {
      el.overlay.hidden = true;
      el.approve.disabled = false;
      el.reject.disabled = false;
      return;
    }

    var request = pending.request || {};
    el.approvalFields.innerHTML = "";

    // The same field set the terminal prompt shows, in the same order, so an
    // operator reads the same facts either way. Absent fields are stated
    // rather than omitted -- "not determinable" can be decided on; blank
    // cannot.
    [
      ["Requesting agent", request.agent],
      ["Operation", request.operation],
      ["Tool", request.tool],
      ["Workspace", request.workspace],
      ["Command or action", request.detail],
      ["Affected targets", (request.targets || []).join(", ") || "not determinable"],
      ["Invocation form", request.is_shell ? "shell string; not parsed" : null],
      ["Rule", request.rule],
      ["Classification", request.classification],
      ["Why it is gated", request.reason],
      ["Reason", request.intent || "not supplied"],
      ["Git context", request.git_context || "not supplied"],
    ].forEach(function (pair) {
      var dt = document.createElement("dt");
      dt.textContent = pair[0];
      var dd = document.createElement("dd");
      dd.textContent = pair[1] === null || pair[1] === undefined || pair[1] === ""
        ? "not supplied"
        : pair[1];
      el.approvalFields.appendChild(dt);
      el.approvalFields.appendChild(dd);
    });

    el.approvalId.textContent = "approval " + pending.approval_id;
    el.approvalNote.hidden = true;
    el.overlay.hidden = false;
  }

  function showNote(message) {
    el.approvalNote.textContent = message;
    el.approvalNote.hidden = false;
  }

  // -- transport -------------------------------------------------------

  function guardedHeaders(extra) {
    var headers = extra || {};
    headers["X-Forge-Token"] = TOKEN;
    return headers;
  }

  function guardedFetch(path, options) {
    var opts = options || {};
    opts.headers = guardedHeaders(opts.headers);
    return fetch(path, opts);
  }

  function refreshStatus() {
    return guardedFetch("/api/status")
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(function (status) {
        el.workspace.textContent = status.workspace || "—";
        el.agent.textContent = status.agent || "—";
        el.model.textContent = status.model || "—";
        setState(status.state);
        // The single source of truth for a pending approval. A refresh lands
        // here, so a reload mid-decision recovers the same id and the card
        // reappears.
        renderPending(status.pending_approval);
        if (status.error) addMessage("system", status.error);
      })
      .catch(function () {
        setState("idle");
      });
  }

  // -- model health -----------------------------------------------------
  //
  // Two requests, and the difference between them is the whole design of this
  // panel: one reads a value the server already holds, the other makes the
  // server go and ask a provider. Nothing here decides what health is.

  function showModelsNote(message) {
    if (!message) {
      el.modelsNote.hidden = true;
      el.modelsNote.textContent = "";
      return;
    }
    el.modelsNote.textContent = message;
    el.modelsNote.hidden = false;
  }

  function drawModels(payload) {
    if (!window.ForgeHealth || !el.models) return;
    window.ForgeHealth.renderHealth(document, el.models, payload, function (tier) {
      // Opening a tier is an operator asking "what is this one?". The answer is
      // already in the panel; this is only for a console-friendly trace of the
      // same thing, which costs nothing and makes a screenshot explainable.
      if (window.console && window.console.debug) {
        window.console.debug("forge model tier", tier.tier, tier.healthWord);
      }
    });
    // A successful read clears a previous failure note, but not the re-check
    // button's disabled state -- that is re-enabled by the re-check itself.
    showModelsNote("");
  }

  /* Read the cached snapshot. Never probes.
   *
   * Called on load, after a re-check, and when the tab becomes visible again. A
   * 503 is a reportable state -- an unreadable configuration -- so it is shown
   * as a note rather than swallowed: an operator looking at an empty indicator
   * would otherwise have no way to tell "nothing configured" from "Forge could
   * not read your configuration".
   */
  function refreshModels() {
    return guardedFetch("/api/models")
      .then(function (response) {
        if (response.status === 503) {
          return response.json().then(function (body) {
            showModelsNote(
              "Model health unavailable: " + (body.detail || "no registry loaded")
            );
            return null;
          });
        }
        if (!response.ok) throw new Error("models " + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (payload) drawModels(payload);
      })
      .catch(function () {
        showModelsNote("Could not read model health from the workbench.");
      });
  }

  /* Re-check: the one request that makes the server contact providers.
   *
   * Disabled while in flight so a second click cannot queue a second pass, and
   * the 409 from a run in flight is stated rather than treated as a failure --
   * the server refusing is the correct answer there, not an error to retry.
   */
  function recheckModels() {
    el.modelsRecheck.disabled = true;
    el.modelsRecheck.textContent = "Checking…";

    return guardedFetch("/api/models/verify", { method: "POST" })
      .then(function (response) {
        if (response.status === 409) {
          return response.json().then(function (body) {
            showModelsNote(
              "Not re-checked: " +
                (body.detail || "a run is in progress in this process.")
            );
            return null;
          });
        }
        if (response.status === 503) {
          return response.json().then(function (body) {
            showModelsNote(
              "Model health unavailable: " + (body.detail || "no registry loaded")
            );
            return null;
          });
        }
        if (!response.ok) throw new Error("verify " + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (payload) drawModels(payload);
        return payload;
      })
      .catch(function (error) {
        showModelsNote("Could not re-check model health: " + error.message);
      })
      .then(function (result) {
        el.modelsRecheck.disabled = false;
        el.modelsRecheck.textContent = "Re-check models";
        return result;
      });
  }

  function submit(task) {
    el.send.disabled = true;
    guardedFetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: task }),
    })
      .then(function (response) {
        if (response.status === 409) {
          return response.json().then(function (body) {
            showNote(
              "One run at a time. " + (body.detail || "A run is already in flight.")
            );
            addMessage(
              "system",
              "Task refused: a run is already in progress in this process."
            );
            el.send.disabled = false;
          });
        }
        if (!response.ok) throw new Error("run " + response.status);
        return response.json();
      })
      .then(function (body) {
        if (body && body.run_id) {
          addMessage("system", "Run " + body.run_id.slice(0, 8) + " accepted.");
          setState("running");
          // Deliberately left disabled for the rest of the run: one process
          // serves one run, and a button that reliably returns 409 is worse
          // than a field that plainly says what is happening.
          refreshStatus();
        }
      })
      .catch(function (error) {
        addMessage("system", "Could not start the run: " + error.message);
        el.send.disabled = false;
      });
  }

  function decide(approved) {
    if (!pendingApprovalId) return;
    el.approve.disabled = true;
    el.reject.disabled = true;

    guardedFetch("/api/approvals/" + encodeURIComponent(pendingApprovalId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: approved ? "yes" : "no" }),
    })
      .then(function (response) {
        if (response.status === 409) {
          // Someone answered first -- possibly this page in another tab. Say
          // so rather than leaving a card the operator thinks they dismissed.
          showNote(
            "This approval was already answered. Reloading the current state."
          );
          return refreshStatus();
        }
        if (response.status === 404) {
          showNote("That approval is no longer pending. Reloading current state.");
          return refreshStatus();
        }
        if (!response.ok) throw new Error("approval " + response.status);
        el.overlay.hidden = true;
        pendingApprovalId = null;
        refreshStatus();
      })
      .catch(function (error) {
        showNote("Could not record the decision: " + error.message);
        el.approve.disabled = false;
        el.reject.disabled = false;
      });
  }

  // -- stream ----------------------------------------------------------

  function handle(name, data) {
    switch (name) {
      case "run.started":
        addMessage("system", "Run started on " + (data.model || "the model") + ".");
        break;
      case "run.submitted":
        break;
      case "model.reply":
        addMessage("assistant", data.text || "");
        break;
      case "run.completed":
        addMessage(
          "system",
          "Completed: " +
            (data.turns || "?") +
            " turn(s), " +
            (data.tool_calls || "0") +
            " tool call(s), " +
            (data.denied || "0") +
            " refusal(s)."
        );
        el.send.disabled = false;
        break;
      case "run.failed":
      case "run.error":
        addMessage("system", "Run failed: " + (data.detail || data.reason || "unknown"));
        el.send.disabled = false;
        break;
      case "approval.required":
        // The card is driven by the server's state, not by this event: the id
        // that matters comes from /api/status so a refresh agrees with it.
        refreshStatus();
        break;
      case "approval.answered":
        el.overlay.hidden = true;
        pendingApprovalId = null;
        break;
      case "session.state":
        // Not while the card is up: the card is the more precise statement of
        // the same fact, and overwriting the chip from a stale event would
        // contradict what the operator is looking at.
        if (data.state && el.overlay.hidden) setState(data.state);
        break;
      case "stream.dropped":
        showNote(
          "Activity events were dropped while this tab fell behind (" +
            (data.dropped || "?") +
            "). The run is unaffected."
        );
        break;
      default:
        break;
    }
    addEvent(name, data || {});
  }

  function connect() {
    guardedFetch("/api/events-ticket", { method: "POST" })
      .then(function (response) {
        if (!response.ok) throw new Error("ticket " + response.status);
        return response.json();
      })
      .then(function (body) {
        if (source) source.close();
        source = new EventSource("/api/events?ticket=" + encodeURIComponent(body.ticket));

        source.onopen = function () {
          el.streamHint.textContent = "stream: live";
        };
        source.onerror = function () {
          // EventSource reconnects on its own, but the ticket is single-use, so
          // a reconnect needs a new one. Re-request rather than sit silent.
          el.streamHint.textContent = "stream: reconnecting";
          window.setTimeout(connect, 1500);
        };

        [
          "run.started", "run.submitted", "model.reply", "run.completed",
          "run.failed", "run.error", "tool.call", "tool.result",
          "approval.required", "approval.answered", "session.state",
          "stream.dropped", "stream.truncated",
        ].forEach(function (name) {
          source.addEventListener(name, function (event) {
            var data = {};
            try {
              data = JSON.parse(event.data);
            } catch (error) {
              data = { detail: "unparseable frame" };
            }
            handle(name, data);
          });
        });
      })
      .catch(function () {
        el.streamHint.textContent = "stream: unavailable";
      });
  }

  // -- wiring ----------------------------------------------------------

  el.composer.addEventListener("submit", function (event) {
    event.preventDefault();
    var task = el.task.value.trim();
    if (!task) return;
    addMessage("user", task);
    el.task.value = "";
    submit(task);
  });

  el.approve.addEventListener("click", function () { decide(true); });
  el.reject.addEventListener("click", function () { decide(false); });

  if (el.modelsRecheck) {
    el.modelsRecheck.addEventListener("click", function () { recheckModels(); });
  }

  el.feed.innerHTML = '<li class="empty">no activity yet</li>';
  refreshStatus().then(connect);
  // Health is read once on load and never polled. The snapshot only changes
  // when the operator asks for it, so re-fetching on a timer would be a request
  // that can only ever return the same answer.
  refreshModels();

  // A reconnection after the tab was backgrounded: the stream may have been
  // dropped, and the server may have moved on. Asking is cheap and keeps the
  // header honest.
  //
  // The health refresh here re-reads the *cached* snapshot. It must never
  // re-check: a tab left open in the background would otherwise send a
  // credential-bearing request every time it was focused.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    refreshStatus();
    refreshModels();
  });
})();