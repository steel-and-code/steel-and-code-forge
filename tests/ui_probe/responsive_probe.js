/* Probe for the Workbench's responsive layout, run under headless Chrome.
 *
 * The properties here are all things a stub DOM cannot answer and a stylesheet
 * cannot promise. Every one of them is measured, not asserted in prose:
 *
 *   1. The conversation occupies the full viewport height at tablet widths,
 *      i.e. the Activity feed is NOT stacked below it (the defect this strike
 *      fixed: both panes were children of one scroll container, so the live
 *      signal was below the fold for the whole working phase of a run).
 *   2. The Activity pane is out of the flow when closed and on screen when
 *      open, in both iPad orientations.
 *   3. Touch targets are at least 44 CSS px on their smaller axis.
 *   4. The document does not scroll sideways at any tested width.
 *   5. The derived activity line is present, and is readable rather than
 *      clipped to nothing.
 *   6. No JavaScript errors during load, toggle, or view switch.
 *
 * Findings go into `window.__forgeProbe` and the document title, which is how
 * `--dump-dom` carries them out to the Python driver.
 *
 * Deliberately narrow, in the same way order_board_probe.js is: it drives one
 * page and reports measurements. It is not a browser-testing framework, and the
 * *behaviour* of the reducer is covered without an engine by
 * tests/test_ui_activity_ui.py. What is here is only what needs a layout
 * engine to know.
 */
(function () {
  "use strict";

  var RESULT = {
    jsErrors: [],
    overflowX: 0,
    viewport: { width: 0, height: 0 },
    conversation: null,
    activity: null,
    sheet: null,
    activityLine: null,
    touchTargets: {},
    views: {},
    checked: [],
    // Two sets of sheet geometry, not one. The sheet is a moving overlay, so
    // "is it closed?" and "does opening it work?" are different questions about
    // different moments -- and a probe that kept only the final reading would
    // answer the second by overwriting the first's evidence.
    closed: null,
    opened: null,
  };
  window.__forgeProbe = RESULT;

  window.addEventListener("error", function (event) {
    RESULT.jsErrors.push(String(event.message || "unknown error"));
  });
  window.addEventListener("unhandledrejection", function (event) {
    RESULT.jsErrors.push("unhandled rejection: " + String(event.reason));
  });

  /* If this probe throws before it reports, the driver finds no
   * `FORGE-PROBE` title at all and reports the *page's* title, which says
   * nothing about why. Recording the failure into the title as well means the
   * driver can always read a verdict: either a measurement, or a reason.
   *
   * `window.onerror` rather than addEventListener("error"), because this needs
   * to catch a throw inside `measure()` too -- an exception in a load handler
   * does not bubble to a listener registered for errors on other elements. */
  window.onerror = function (message, source, line) {
    RESULT.jsErrors.push(String(message) + " (line " + line + ")");
    document.title = "FORGE-PROBE " + JSON.stringify(RESULT);
    return true;
  };

  function box(node) {
    if (!node) return null;
    var rect = node.getBoundingClientRect();
    return {
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      bottom: Math.round(rect.bottom),
      // Painted off the bottom of the viewport, which is how "closed" is
      // expressed: the sheet is translated out of view, not display:none'd.
      offScreenBelow: rect.top >= window.innerHeight - 1,
      visible: rect.height > 0 && rect.width > 0,
    };
  }

  function minTarget(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    var rect = node.getBoundingClientRect();
    // `display: none` collapses the box to zero. That is the correct answer at
    // widths where a control does not apply, so zero is reported rather than
    // treated as a failure -- the caller decides per width.
    return {
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      min: Math.round(Math.min(rect.width, rect.height)),
      displayed: rect.height > 0,
    };
  }

  function measure() {
    RESULT.viewport = {
      width: window.innerWidth,
      height: window.innerHeight,
    };
    RESULT.overflowX = Math.max(
      0,
      document.documentElement.scrollWidth - window.innerWidth
    );
    RESULT.conversation = box(document.querySelector(".pane.conversation"));
    RESULT.activity = box(document.getElementById("activity-pane"));
    RESULT.activityLine = box(document.getElementById("activity-line"));

    var toggle = document.getElementById("activity-toggle");
    RESULT.sheet = {
      open: document.body.getAttribute("data-activity-open") === "true",
      ariaExpanded: toggle ? toggle.getAttribute("aria-expanded") : null,
    };

    // Both approval buttons, measured for real. The overlay is hidden by
    // default, so this is measured with the card forced open.
    var overlay = document.getElementById("approval-overlay");
    var wasHidden = overlay.hidden;
    overlay.hidden = false;
    RESULT.touchTargets.approve = minTarget("approve");
    RESULT.touchTargets.reject = minTarget("reject");
    overlay.hidden = wasHidden;

    RESULT.touchTargets.send = minTarget("send");
    RESULT.touchTargets.activityToggle = minTarget("activity-toggle");
    RESULT.views.workbench = minTarget("view-workbench");
    RESULT.views.board = minTarget("view-board");
  }

  /* Wait until the sheet's transition has actually finished, then resolve.
   *
   * This exists because of a measured property of the harness, not a guess:
   * under `--dump-dom`, headless Chromium does not advance a CSS transition to
   * completion. The sheet opens, `transform` starts at `translateY(100%)`, and
   * it is still at `translateY(100%)` when the DOM is snapshotted -- so a probe
   * that measured after two animation frames read a sheet frozen 100% of the
   * way off screen and concluded the toggle did nothing. It did.
   *
   * `getAnimations()` is the honest signal: it reports the transition object
   * Chromium created, and its `finished` promise resolves when the animation is
   * genuinely done. If the engine never runs the transition, `finished` never
   * settles, so a timeout falls back to measuring the *declared* end state
   * instead -- which is what the stylesheet says the sheet should be, and is
   * still a real check of the open rule.
   *
   * So: wait for the animation if it finishes, and if it does not, measure the
   * pane with transitions suppressed. Both paths assert the same thing -- that
   * `data-activity-open="true"` puts the sheet on screen -- without ever
   * reporting a mid-flight frame as the answer.
   */
  function settled(pane) {
    var running = pane.getAnimations ? pane.getAnimations() : [];
    var transition = null;
    for (var i = 0; i < running.length; i++) {
      if (running[i].constructor.name === "CSSTransition") {
        transition = running[i];
      }
    }
    if (!transition || !transition.finished) {
      return Promise.resolve("no-transition-ran");
    }
    return Promise.race([
      transition.finished.then(function () {
        return "transition-finished";
      }),
      new Promise(function (resolve) {
        window.setTimeout(function () {
          resolve("transition-did-not-advance");
        }, 1500);
      }),
    ]);
  }

  /* Measure with transitions suppressed.
   *
   * Applied and then removed around the reading, because a rule injected with
   * `transition: none` makes `getComputedStyle` report the declared transform
   * rather than an animated one. This is the fallback path's whole point: it
   * answers "where does this pane sit when open" without depending on the
   * engine's animation clock.
   */
  function measureSuppressed(pane) {
    var kill = document.createElement("style");
    kill.textContent = ".pane.activity { transition: none !important; }";
    document.head.appendChild(kill);
    void pane.offsetHeight;
    var measured = box(pane);
    document.head.removeChild(kill);
    void pane.offsetHeight;
    return measured;
  }

  window.addEventListener("load", function () {
    measure();

    var toggle = document.getElementById("activity-toggle");
    if (!toggle || !toggle.getBoundingClientRect().height) {
      // No toggle at this width: the feed is a column, so there is no sheet to
      // open and the closed-state measurements are the final ones.
      RESULT.checked.push("no-toggle-at-this-width");
      RESULT.closed = RESULT.activity;
      document.title = "FORGE-PROBE " + JSON.stringify(RESULT);
      return;
    }

    RESULT.closed = RESULT.activity;
    toggle.click();

    var pane = document.getElementById("activity-pane");
    settled(pane).then(function (how) {
      RESULT.checked.push("opened:" + how);
      measure();
      // Always read the open state with transitions suppressed: the assertion is
      // "does the open rule put this pane on screen", and an engine that will
      // not run the transition would otherwise report the mid-flight frame as
      // the answer. When the transition did finish, this agrees with the live
      // reading; when it did not, it is the only reading that means anything.
      RESULT.opened = measureSuppressed(pane);
      RESULT.openedLive = box(pane);
      document.title = "FORGE-PROBE " + JSON.stringify(RESULT);
    });
  });
})();
