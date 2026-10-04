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
 * And five more for the Order Board, which follow the same split:
 *
 *   GET  /api/orders           token + same-origin -> every order, grouped
 *   GET  /api/orders/{id}      token + same-origin -> one order
 *   POST /api/orders           token + origin      -> create, 201
 *   PATCH /api/orders/{id}     token + origin      -> edit title/priority/smith
 *   POST /api/orders/{id}/move token + origin      -> move between states
 *   DELETE /api/orders/{id}   token + origin      -> delete one order, 200
 *
 * Movement is its own route and is deliberately not a field of the edit body:
 * an edit must not be able to become a transition by accident.
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
 *
 * - A CLIPPED PREVIEW IS REPLACED, NOT KEPT. `model.reply` arrives clamped by
 *   the server's event bounds, so a long answer reaches the transcript
 *   clipped. The event stream stays bounded; the authoritative answer is read
 *   from `/api/status` when the run completes and written into that same entry
 *   -- one final answer in the transcript, never a preview followed by a copy.
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

  // The transcript node holding the *clipped* `model.reply` preview of the
  // answer, and the run whose final answer has already been written into it.
  // Both are in-memory facts about what this tab has drawn, not about the run
  // -- which is why a refresh asks the server rather than trusting them.
  var assistantPreview = null;
  var finalAnswerRun = null;

  /* Write the authoritative final answer into the transcript.
   *
   * Called after `refreshStatus`, from two places -- `run.completed`, and
   * `refreshStatus` itself when the server reports a finished run. The second
   * is what makes a page that loaded after the run, or a reconnect that replayed
   * the tail, show the answer at all.
   */
  function renderFinalAnswer(status) {
    if (!status || status.state !== "completed") return;
    var result = status.result;
    if (!result || !result.text) return;
    reconcileFinalAnswer(status.run_id, result.text);
  }

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

  /* Append one entry, and return it.
   *
   * Returns the element so a caller that will later *replace* what it said can
   * find it. `addMessage` itself stays a one-way append: the reconciliation
   * below is the only thing in this file that edits an entry after the fact.
   */
  function addMessage(role, text) {
    if (!text) return null;
    var node = entry(role, text);
    el.thread.appendChild(node);
    el.thread.scrollTop = el.thread.scrollHeight;
    return node;
  }

  /* The authoritative final answer, recovered from /api/status.
   *
   * A `model.reply` event carries a *bounded preview*: every event field is
   * clamped by `forge.events.Event`, so a long answer arrives clipped. The
   * clamp is deliberate and stays -- an event is a summary of one observation,
   * and a page should not be able to pull a whole answer through it. So the
   * preview is not the answer, and the transcript must not be left holding it.
   *
   * The server holds the real thing: `RunResult.text` is returned verbatim in
   * `GET /api/status`, unbounded, because that endpoint is a *read of state the
   * server already has* rather than an event payload. On completion this reads
   * it back and rewrites the preview entry in place.
   *
   * Four properties, each of which was a bug first:
   *
   * - ONE ANSWER, NOT TWO. The clipped preview is replaced, never followed by
   *   a second entry. An operator reading the transcript must not see the
   *   clipped text once and the full text once and have to guess which was
   *   which.
   * - AN UNCLIPPED PREVIEW IS LEFT ALONE. The server marks a clipped value with
   *   a trailing ellipsis (`forge.events._clamp` does it, and
   *   `tests/test_events.py` asserts it), so a preview without one arrived
   *   whole. If it is also identical to the answer there is nothing to do, and
   *   if it is not -- a mid-turn message, and the tab missed the final turn's
   *   event -- it is real transcript content that must not be overwritten.
   *   Clobbering it would lose what the model actually said in order to save
   *   one entry.
   * - REPLACEMENT IS BY DOM NODE, NOT BY TEXT MATCH. Matching the preview's
   *   text to find it would fail exactly when it matters -- a clipped preview
   *   is not a prefix of what replaces it if the model reworded, and matching
   *   would then append and leave both.
   * - IDEMPOTENT PER RUN. Reconciliation is driven by `run.completed`, which a
   *   reconnect replays and `refreshStatus` can also reach; a `run_id` already
   *   reconciled is a no-op, so neither leaves a second copy. That also makes a
   *   page loaded *after* the run finished render the answer once, from the
   *   status it fetched on load.
   */

  // The marker `forge.events._clamp` appends to a value it truncated. Used only
  // to recognise a clipped preview, never to decide how much text to keep.
  var CLIPPED = "…";

  function entryBody(node) {
    var kids = node.children || [];
    for (var i = 0; i < kids.length; i++) {
      var classes = String(kids[i].className || "").split(/\s+/);
      if (classes.indexOf("body") >= 0) return kids[i];
    }
    return null;
  }

  function reconcileFinalAnswer(runId, text) {
    if (!text) return;
    if (runId && runId === finalAnswerRun) return;

    var preview = assistantPreview;
    assistantPreview = null;
    finalAnswerRun = runId || finalAnswerRun;

    var body = preview ? entryBody(preview) : null;
    if (!body) {
      // Nothing to replace -- the preview never arrived, or this page was
      // loaded after the run finished. One entry either way.
      addMessage("assistant", text);
    } else if (body.textContent === text) {
      // Already the whole answer. Rewriting it would be a no-op with a
      // scrollbar jump, and re-reading the status must not change the page.
    } else if (String(body.textContent).slice(-1) === CLIPPED) {
      // textContent, exactly as the entry was built: an assistant answer is
      // model output and must never be parsed as markup.
      body.textContent = text;
    } else {
      // A complete mid-turn message that is not the answer. It stays, and the
      // answer goes after it.
      addMessage("assistant", text);
    }
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
        // A finished run's answer is read here too, not only on run.completed:
        // the preview in the transcript is clipped by the event clamp, so a
        // page loaded after the run -- or one whose stream missed the event --
        // would otherwise have no final answer at all.
        renderFinalAnswer(status);
        return status;
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
        // A bounded preview, kept as-is. The authoritative answer is read from
        // /api/status when the run completes; until then this is what the
        // operator can see, and truncating it twice would only lose more.
        assistantPreview = addMessage("assistant", data.text || "");
        break;
      case "run.completed":
        // The turn/tool counts come from the event; the *answer* does not, so
        // the summary line is written first and the transcript is repaired
        // from the server's own result. Ordering matters: the repair is the
        // last thing an operator reads.
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
        // The counts above are the event's; the answer is not in it, so the
        // authoritative one is read back from the server. `refreshStatus`
        // performs the reconciliation, which is why the fix is a read rather
        // than a second rendering path.
        refreshStatus();
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

  // -- the Order Board -------------------------------------------------
  //
  // Six endpoints, one pure renderer, and no state of its own. `board.js` holds
  // every decision about how an order looks; this section only fetches, calls the
  // renderer, and posts. The rules it keeps:
  //
  //  - THE SERVER IS THE ONLY SOURCE OF STATE. The board re-reads
  //    GET /api/orders on open and after every command. Nothing is cached, so a
  //    refresh shows the server's answer rather than a stale local copy.
  //
  //  - IT NEVER NAMES A WORKFLOW STATE. The columns, the move buttons, and the
  //    priority options are all built from the payload's own `statuses` and
  //    `priorities`. A state added on the backend appears here without a frontend
  //    change, and one removed here cannot linger.
  //
  //  - A REFUSED COMMAND SAYS SO. A 422 or 404 puts the server's own detail in
  //    the board note rather than optimistically moving a card the server did not
  //    move. A refused delete in particular leaves the card exactly where it was.
  //
  //  - ORDERS DO NOT RUN ANYTHING. Nothing here starts a run or reaches the
  //    conversation pane, and deleting an Order triggers no agent behaviour. The
  //    board is inert by construction, not by a flag.

  var board = {
    el: {
      view: document.getElementById("board-view"),
      workbench: document.getElementById("workbench-view"),
      switcher: document.getElementById("view-switch"),
      container: document.getElementById("board"),
      note: document.getElementById("board-note"),
      form: document.getElementById("order-form"),
      title: document.getElementById("order-title"),
      priority: document.getElementById("order-priority"),
      smith: document.getElementById("order-smith"),
      overlay: document.getElementById("order-overlay"),
      detail: document.getElementById("order-detail"),
      close: document.getElementById("order-close"),
    },
    // The last payload, kept only to render the detail dialog and to know which
    // statuses to offer. Never a source of truth: every command re-reads.
    payload: null,
    open: false,
  };

  function boardNote(message) {
    if (!board.el.note) return;
    board.el.note.textContent = message || "";
  }

  function boardRequest(path, options) {
    var opts = options || {};
    opts.headers = guardedHeaders(opts.headers);
    return fetch(path, opts).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (body) {
          // The server's own detail, verbatim. An error message invented here
          // would be a second opinion about why the server refused something.
          if (!response.ok) {
            var error = new Error(body.detail || "request " + response.status);
            error.status = response.status;
            throw error;
          }
          return body;
        });
    });
  }

  function drawBoard(payload) {
    if (!window.ForgeBoard || !board.el.container) return;
    board.payload = payload;
    var rendered = window.ForgeBoard.renderBoard(
      document,
      board.el.container,
      payload,
      { onOpen: openOrder }
    );
    fillPriorities(payload.priorities || []);

    // One number, counted by the renderer from the columns it just drew. There
    // is deliberately no comparison against the payload's own `total`: the
    // backend derived that field from these same columns, so comparing them
    // checks the backend against itself and can only ever report agreement.
    // (It did exactly that, and reported a mismatch on every load of a healthy
    // board, because the check was reading a field the renderer had computed.)
    var model = rendered.model;
    if (model.total === 0) {
      boardNote("No orders yet. Create one above.");
    } else {
      boardNote(model.total + (model.total === 1 ? " order" : " orders"));
    }
  }

  /* The priority control, built from the server's list.

   * Filled once and only from `payload.priorities`, so the form cannot offer a
   * value the backend would refuse. An empty list leaves the control with no
   * options rather than a hard-coded fallback.
   */
  function fillPriorities(priorities) {
    var select = board.el.priority;
    if (!select || select.getAttribute("data-filled") === "yes") return;
    while (select.firstChild) select.removeChild(select.firstChild);
    priorities.forEach(function (priority) {
      var option = document.createElement("option");
      option.value = priority;
      option.textContent = priority;
      select.appendChild(option);
    });
    select.setAttribute("data-filled", "yes");
  }

  function refreshBoard() {
    return boardRequest("/api/orders")
      .then(drawBoard)
      .catch(function (error) {
        // A board that cannot be read is reported, not shown empty. An empty
        // board here would read as "you have no orders", which is a different
        // and much worse thing to believe.
        boardNote("Could not read the board: " + error.message);
      });
  }

  function openOrder(card) {
    if (!board.el.detail) return;
    window.ForgeBoard.renderDetail(
      document,
      board.el.detail,
      {
        order_id: card.orderId,
        title: card.title,
        status: card.status,
        status_label: card.statusLabel,
        priority: card.priority,
        smith: card.smith,
        description: card.description,
        created_at: card.createdAt,
        sequence: card.sequence,
      },
      // The backend's own status/label pairs, so a move button spells a state
      // exactly as the column heading for it does.
      (board.payload && board.payload.columns) || [],
      { onMove: moveOrder, onDelete: deleteOrder }
    );
    board.el.overlay.hidden = false;
  }

  function closeOrder() {
    board.el.overlay.hidden = true;
    if (board.el.detail) {
      while (board.el.detail.firstChild) {
        board.el.detail.removeChild(board.el.detail.firstChild);
      }
    }
  }

  function moveOrder(orderId, status) {
    return boardRequest("/api/orders/" + encodeURIComponent(orderId) + "/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status }),
    })
      .then(function () {
        closeOrder();
        boardNote("Moved " + orderId + ".");
        return refreshBoard();
      })
      .catch(function (error) {
        boardNote("Could not move " + orderId + ": " + error.message);
      });
  }

  function deleteOrder(orderId, title) {
    /* Remove one order, permanently.

     * Two things this deliberately does not do. It does not touch the card before
     * the server has answered -- a card that vanishes on click and comes back when
     * the delete is refused is worse than one that never moved. And it does not
     * re-read the board to decide whether the delete worked: the server's answer
     * is the only authority, so a 200 that removed a different order still leaves
     * the board showing the server's truth.
     */
    return boardRequest("/api/orders/" + encodeURIComponent(orderId), {
      method: "DELETE",
    })
      .then(function (order) {
        closeOrder();
        // The server's own payload, not the title we were handed: an order edited
        // in another tab is deleted by the name it had when it went.
        return refreshBoard().then(function () {
          boardNote("Deleted " + (order && order.order_id ? order.order_id : orderId) + ".");
        });
      })
      .catch(function (error) {
        // The dialog stays open and the card stays on the board, so the operator
        // can read the reason against the order it belongs to and try again.
        boardNote("Could not delete " + orderId + ": " + error.message);
      });
  }

  function createOrder(event) {
    event.preventDefault();
    var title = board.el.title.value.trim();
    if (!title) return;

    var body = { title: title };
    if (board.el.priority && board.el.priority.value) {
      body.priority = board.el.priority.value;
    }
    if (board.el.smith && board.el.smith.value.trim()) {
      body.smith = board.el.smith.value.trim();
    }

    return boardRequest("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (order) {
        board.el.title.value = "";
        board.el.smith.value = "";
        return refreshBoard().then(function () {
          boardNote("Created " + order.order_id + ".");
        });
      })
      .catch(function (error) {
        boardNote("Could not create the order: " + error.message);
      });
  }

  function selectView(name) {
    board.open = name === "board";
    board.el.workbench.hidden = board.open;
    board.el.view.hidden = !board.open;
    Array.prototype.forEach.call(
      board.el.switcher.querySelectorAll(".view-button"),
      function (button) {
        button.setAttribute(
          "aria-pressed",
          button.getAttribute("data-view") === name ? "true" : "false"
        );
      }
    );
    // Read on open rather than on a timer: the board only changes when someone
    // changes it, and a poll could only ever return the same answer.
    if (board.open) refreshBoard();
  }

  if (board.el.form) {
    board.el.form.addEventListener("submit", createOrder);
  }
  if (board.el.close) {
    board.el.close.addEventListener("click", closeOrder);
  }
  if (board.el.switcher) {
    board.el.switcher.addEventListener("click", function (event) {
      var button = event.target.closest
        ? event.target.closest(".view-button")
        : null;
      if (button) selectView(button.getAttribute("data-view"));
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