/* Probe for the Order Board's page wiring, run under headless Chrome.
 *
 * Four properties, each of which a rendering test against a stub DOM cannot see
 * and each of which was a real defect found this way:
 *
 *   1. Switching views leaves only the selected view painted.
 *   2. A populated board renders all six columns.
 *   3. No JavaScript errors during board load or view switch.
 *   4. The document does not scroll sideways at the tested widths.
 *
 * The findings are written into `window.__forgeProbe` and set as the document
 * title, which is how `--dump-dom` (see the Python driver) carries them out. A
 * separate file rather than inline script in the driver, so this stays readable
 * and node-checkable.
 *
 * Deliberately narrow. This is not a browser-testing framework: it drives one
 * page, asserts four things, and reports a pass/fail object. The rendering
 * behaviour of board.js is covered properly by tests/test_ui_orders_board_ui.py
 * against the stub DOM; this file exists only for what needs a real engine.
 */
(function () {
  "use strict";

  var RESULT = {
    jsErrors: [],
    viewSwitchClean: null,
    allSixColumns: null,
    columnCount: 0,
    pageOverflowX: 0,
    checked: [],
  };
  window.__forgeProbe = RESULT;

  window.addEventListener("error", function (event) {
    RESULT.jsErrors.push(String(event.message || "unknown error"));
  });
  window.addEventListener("unhandledrejection", function (event) {
    RESULT.jsErrors.push("unhandled rejection: " + String(event.reason));
  });

  /* The board data is injected by the driver. `fetch` is stubbed because the
   * check is about the page's behaviour given a payload, not about the HTTP
   * layer -- that is what tests/test_ui_orders_api.py covers.
   *
   * The stub keeps its own copy of the board so a DELETE can actually remove an
   * order from it. That is what lets the delete flow below be checked end to end:
   * a page that deleted optimistically and never re-read would still look right
   * if the stub did nothing.
   */
  window.__requests = [];
  window.__deleteShouldFail = false;
  window.__boardData = JSON.parse(JSON.stringify(window.__board));

  function respond(ok, status, body) {
    return Promise.resolve({
      ok: ok,
      status: status,
      json: function () {
        return Promise.resolve(body);
      },
    });
  }

  function forget(orderId) {
    var remaining = { columns: [], statuses: window.__board.statuses,
                      priorities: window.__board.priorities, next_id: window.__board.next_id };
    var total = 0;
    window.__boardData.columns.forEach(function (column) {
      var orders = column.orders.filter(function (order) {
        return order.order_id !== orderId;
      });
      total += orders.length;
      remaining.columns.push({
        status: column.status,
        label: column.label,
        count: orders.length,
        orders: orders,
      });
    });
    remaining.total = total;
    window.__boardData = remaining;
  }

  window.fetch = function (path, options) {
    var opts = options || {};
    var method = String(opts.method || "GET").toUpperCase();
    window.__requests.push(method + " " + String(path));

    if (String(path).indexOf("/api/orders") === 0) {
      if (method === "DELETE") {
        var orderId = decodeURIComponent(String(path).split("/").pop());
        if (window.__deleteShouldFail) {
          return respond(false, 500, { detail: "the board file is not valid JSON" });
        }
        var removed = null;
        window.__boardData.columns.forEach(function (column) {
          column.orders.forEach(function (order) {
            if (order.order_id === orderId) removed = order;
          });
        });
        forget(orderId);
        return respond(true, 200, removed || { order_id: orderId });
      }
      if (method === "GET") return respond(true, 200, window.__boardData);
      return respond(true, 201, {});
    }
    return respond(false, 0, { detail: "not stubbed" });
  };

  /* Elements in `root` that are still painting.

   * `hidden` is not enough to answer this: a `display` declaration can beat the
   * user agent's `[hidden]` rule, which is precisely how the workbench pane once
   * rendered on top of the board while reporting itself hidden. So this measures
   * geometry.
   */
  function painting(root) {
    if (!root) return ["(absent)"];
    var found = [];
    var all = [root].concat(Array.prototype.slice.call(root.querySelectorAll("*")));
    all.forEach(function (el) {
      var rect = el.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) found.push(el.tagName.toLowerCase());
    });
    return found;
  }

  function overflow() {
    return (
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth
    );
  }

  function report(ok, detail) {
    RESULT.checked.push(detail);
    if (!ok) RESULT.failures = (RESULT.failures || []).concat(detail);
  }

  function finish() {
    RESULT.ok = !RESULT.jsErrors.length && !(RESULT.failures || []).length;
    // The driver parses this line. Keep it on one line and JSON-shaped.
    document.title = "FORGE-PROBE " + JSON.stringify(RESULT);
  }

  function step() {
    var boardButton = document.getElementById("view-board");
    var workbenchButton = document.getElementById("view-workbench");
    var workbenchView = document.getElementById("workbench-view");
    var boardView = document.getElementById("board-view");

    if (!boardButton || !workbenchButton || !workbenchView || !boardView) {
      report(false, "the view switcher or its panes are missing from the page");
      finish();
      return;
    }

    // 1. Only the selected view paints.
    RESULT.boardPainted = painting(boardView).length;
    RESULT.workbenchPainted = painting(workbenchView).length;
    RESULT.viewSwitchClean =
      RESULT.boardPainted > 0 && RESULT.workbenchPainted === 0;
    report(
      RESULT.viewSwitchClean,
      RESULT.viewSwitchClean
        ? "board view paints, workbench paints nothing"
        : "with the board selected, workbench painted " +
            RESULT.workbenchPainted +
            " element(s)"
    );

    // And the other direction, because a toggle that only works one way is a
    // broken toggle.
    workbenchButton.click();
    var backToWorkbench =
      painting(workbenchView).length > 0 && painting(boardView).length === 0;
    RESULT.switchBack = backToWorkbench;
    report(
      backToWorkbench,
      backToWorkbench
        ? "switching back hides the board"
        : "switching back to the workbench left the board painted"
    );

    boardButton.click();

    // 2. All six columns.
    var columns = document.querySelectorAll(".board-column");
    RESULT.columnCount = columns.length;
    RESULT.columnHeadings = Array.prototype.map
      .call(columns, function (column) {
        var heading = column.querySelector(".board-column-title");
        return heading ? heading.textContent : "(no heading)";
      })
      .join("|");
    RESULT.allSixColumns = columns.length === 6;
    report(
      RESULT.allSixColumns,
      RESULT.allSixColumns
        ? "all six columns rendered"
        : "rendered " + columns.length + " columns, expected 6"
    );

    // Empty columns must carry their message. A board that renders six headings
    // and leaves four of them blank has not rendered an empty state, it has
    // rendered a broken one -- and the column count alone cannot tell the two
    // apart.
    RESULT.emptyMessages = document.querySelectorAll(".board-empty").length;
    RESULT.emptyMessageText = Array.prototype.map
      .call(document.querySelectorAll(".board-empty"), function (el) {
        return el.textContent;
      })
      .join("|");
    report(
      RESULT.emptyMessages === 4,
      RESULT.emptyMessages === 4
        ? "the four empty columns each show a message"
        : "found " +
            RESULT.emptyMessages +
            " empty-column messages, expected 4 (columns: " +
            RESULT.columnHeadings +
            ")"
    );

    // The board note, verbatim. A rendering check cannot see what the note says,
    // and the note once rendered "the board holds 3 orders but its columns show
    // undefined" on every load of a healthy board.
    var note = document.getElementById("board-note");
    RESULT.boardNote = note ? note.textContent : "(no note element)";
    report(
      RESULT.boardNote.indexOf("undefined") === -1,
      RESULT.boardNote.indexOf("undefined") === -1
        ? 'the board note reads "' + RESULT.boardNote + '"'
        : "the board note rendered an undefined value: " + RESULT.boardNote
    );

    // 4. No document-level horizontal overflow. The board itself scrolls, and
    // that is intended; the *page* must not.
    RESULT.pageOverflowX = overflow();
    report(
      RESULT.pageOverflowX <= 0,
      RESULT.pageOverflowX <= 0
        ? "no page-level horizontal overflow"
        : "document overflows sideways by " + RESULT.pageOverflowX + "px"
    );

    // 5. Delete, end to end: open an order, confirm, and watch the board.
    //
    //    Checked here rather than against the stub DOM because the property is
    //    about the *page*: that confirming issues one DELETE, that the card goes
    //    away only after the server answered, and that a refused delete leaves
    //    the card exactly where it was with the reason in the board note.
    var card = document.querySelector('.board-card[data-order-id="ORD-0002"]');
    if (!card) {
      report(false, "ORD-0002 has no card to open");
    } else {
      card.click();
      setTimeout(deleteFlow, 200);
      return;
    }

    // 3. No JavaScript errors at any point in the above.
    report(
      RESULT.jsErrors.length === 0,
      RESULT.jsErrors.length === 0
        ? "no JavaScript errors"
        : "JavaScript errors: " + RESULT.jsErrors.join("; ")
    );

    finish();
  }

  /* The delete flow, in three steps.
   *
   * Each step is a real click on the real control, with a tick between them: the
   * page works through promises, so measuring synchronously after a click reads a
   * pending request rather than the result of one.
   */
  function deleteFlow() {
    var trigger = document.querySelector(".order-delete-trigger");
    var confirmRegion = document.getElementById("order-delete-confirm");

    RESULT.deletePrompt = confirmRegion
      ? confirmRegion.textContent
      : "(no confirmation)";
    RESULT.confirmationHiddenBeforeClick = confirmRegion
      ? confirmRegion.hidden === true || confirmRegion.getAttribute("hidden") === "true"
      : null;
    report(
      RESULT.confirmationHiddenBeforeClick === true,
      "the delete confirmation is hidden until it is asked for"
    );
    report(
      RESULT.deletePrompt.indexOf("ORD-0002") >= 0 &&
        RESULT.deletePrompt.indexOf("Draw out the bar") >= 0,
      "the confirmation names the order number and its title: " + RESULT.deletePrompt
    );

    // Cancel first: cancelling must not issue a request at all.
    var before = window.__requests.length;
    var cancel = document.querySelector(".order-delete-cancel");
    if (cancel) cancel.click();

    setTimeout(function () {
      RESULT.cancelIssuedNothing = window.__requests.length === before;
      report(
        RESULT.cancelIssuedNothing,
        RESULT.cancelIssuedNothing
          ? "cancelling the confirmation issues no request"
          : "cancelling issued: " + window.__requests.slice(before).join(", ")
      );
      report(
        document.querySelector('.board-card[data-order-id="ORD-0002"]') !== null,
        "the order is still on the board after cancelling"
      );

      // Now the refused delete. The card must survive it, and the server's own
      // reason must be what the operator reads.
      window.__deleteShouldFail = true;
      if (trigger) trigger.click();
      setTimeout(refusedDelete, 200);
    }, 200);
  }

  function refusedDelete() {
    var confirm = document.querySelector(".order-delete-confirm-button");
    if (confirm) confirm.click();

    setTimeout(function () {
      RESULT.failedDeleteNote = (
        document.getElementById("board-note") || {}
      ).textContent;
      RESULT.failedDeleteCardVisible =
        document.querySelector('.board-card[data-order-id="ORD-0002"]') !== null;
      report(
        RESULT.failedDeleteCardVisible,
        RESULT.failedDeleteCardVisible
          ? "a refused delete leaves the card on the board"
          : "a refused delete removed the card from the board"
      );
      report(
        String(RESULT.failedDeleteNote).indexOf("Could not delete") === 0 &&
          String(RESULT.failedDeleteNote).indexOf("ORD-0002") >= 0,
        'a refused delete says so in the board note: "' +
          RESULT.failedDeleteNote +
          '"'
      );
      report(
        String(RESULT.failedDeleteNote).indexOf("not valid JSON") >= 0,
        "the board note carries the server's own reason, verbatim"
      );

      // And now the successful delete, on an order that has not been removed.
      window.__deleteShouldFail = false;
      var card = document.querySelector('.board-card[data-order-id="ORD-0003"]');
      if (!card) {
        report(false, "ORD-0003 has no card to delete");
        finish();
        return;
      }
      card.click();
      setTimeout(successfulDelete, 200);
    }, 250);
  }

  function successfulDelete() {
    var trigger = document.querySelector(".order-delete-trigger");
    if (!trigger) {
      report(false, "the dialog has no delete control to click");
      finish();
      return;
    }
    trigger.click();

    setTimeout(function () {
      var confirm = document.querySelector(".order-delete-confirm-button");
      if (!confirm) {
        report(false, "the confirmation has no confirming button");
        finish();
        return;
      }
      confirm.click();

      setTimeout(function () {
        RESULT.deleteRequests = window.__requests.filter(function (entry) {
          return entry.indexOf("DELETE") === 0;
        });
        RESULT.deletedCardGone =
          document.querySelector('.board-card[data-order-id="ORD-0003"]') === null;
        RESULT.overlayHidden = (
          document.getElementById("order-overlay") || {}
        ).hidden === true;
        RESULT.deleteNote = (document.getElementById("board-note") || {}).textContent;

        report(
          RESULT.deletedCardGone,
          RESULT.deletedCardGone
            ? "a confirmed delete removes the card from the board"
            : "a confirmed delete left the card on the board"
        );
        /* Two DELETEs, and two is right: the first was the *refused* delete, which
         * must reach the server to be refused. The property worth pinning is that
         * cancelling issued nothing and each confirmation issued exactly one, for
         * the order it named -- not that only one delete ever happens. */
        report(
          RESULT.deleteRequests.join("|") ===
            "DELETE /api/orders/ORD-0002|DELETE /api/orders/ORD-0003",
          "one DELETE per confirmation, each for the order it named: " +
            RESULT.deleteRequests.join(", ")
        );
        report(RESULT.overlayHidden, "a confirmed delete closes the dialog");
        report(
          String(RESULT.deleteNote).indexOf("ORD-0003") >= 0 &&
            String(RESULT.deleteNote).indexOf("Could not") === -1,
          'a confirmed delete says what it removed: "' + RESULT.deleteNote + '"'
        );
        report(
          document.querySelectorAll(".board-card").length === 2,
          "the board re-read from the server: found " +
            document.querySelectorAll(".board-card").length +
            " cards, expected 2"
        );

        report(
          RESULT.jsErrors.length === 0,
          RESULT.jsErrors.length === 0
            ? "no JavaScript errors"
            : "JavaScript errors: " + RESULT.jsErrors.join("; ")
        );

        finish();
      }, 300);
    }, 200);
  }

  // A tick between the click and the measurement: the board reads its data
  // asynchronously, so measuring synchronously after .click() reads a pending
  // promise rather than a rendered board.
  setTimeout(function () {
    try {
      document.getElementById("view-board").click();
      setTimeout(step, 300);
    } catch (error) {
      RESULT.jsErrors.push("probe threw: " + String(error));
      finish();
    }
  }, 150);
})();