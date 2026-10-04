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

  // The board data is injected by the driver. `fetch` is stubbed because the
  // check is about the page's behaviour given a payload, not about the HTTP
  // layer -- that is what tests/test_ui_orders_api.py covers.
  window.fetch = function (path) {
    if (String(path).indexOf("/api/orders") === 0) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve(window.__board);
        },
      });
    }
    return Promise.resolve({
      ok: false,
      status: 0,
      json: function () {
        return Promise.resolve({ detail: "not stubbed" });
      },
    });
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

    // 4. No document-level horizontal scroll. The board itself scrolls, and
    // that is intended; the *page* must not.
    RESULT.pageOverflowX = overflow();
    report(
      RESULT.pageOverflowX <= 0,
      RESULT.pageOverflowX <= 0
        ? "no page-level horizontal overflow"
        : "document overflows sideways by " + RESULT.pageOverflowX + "px"
    );

    // 3. No JavaScript errors at any point in the above.
    report(
      RESULT.jsErrors.length === 0,
      RESULT.jsErrors.length === 0
        ? "no JavaScript errors"
        : "JavaScript errors: " + RESULT.jsErrors.join("; ")
    );

    finish();
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