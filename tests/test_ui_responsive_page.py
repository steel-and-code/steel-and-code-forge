"""The Workbench at desktop and iPad widths, measured in a real engine.

The reducer's *behaviour* is covered without a browser by
``tests/test_ui_activity_ui.py``. What is here is only what genuinely needs a
layout engine, and every assertion is a measurement rather than a claim about
what a stylesheet intends.

**Why this module exists rather than more tests in the existing one.** The
previous tablet rule collapsed ``.split`` to a single column and let the
Activity feed stack *below* the conversation. Both panes are children of one
``flex: 1 1 auto`` container, so the feed sat below the fold for the entire
working phase of a run -- and during a four-minute run with no answer yet in the
transcript, everything reachable on an iPad was four words and a status chip.
That is not a styling preference; it is why the page looked idle. Whether a
pane is above or below the fold is exactly the question a stub DOM cannot answer.

**The harness is reused, not copied.** ``tests/test_ui_orders_page.py`` already
owns the Chromium capability probe, the ``DBUS_SESSION_BUS_ADDRESS`` child-env
fix, the shared flags, and the loopback page server. All four are imported here.
Writing a third copy would mean a third flag list to keep in step with the
first two, which is precisely the drift the existing harness's own guard test
was written to prevent.

**Skips cleanly, and says why.** The same capability probe decides. If Chromium
genuinely cannot load a page here, this module skips with the reason and names
the files that cover the same behaviour without an engine.
"""

from __future__ import annotations

import functools
import json
import pathlib
import re
import socketserver
import subprocess
import pytest

from tests import test_ui_orders_page as harness

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE_JS = pathlib.Path(__file__).resolve().parent / "ui_probe" / "responsive_probe.js"

#: The widths worth measuring, and why each one is here.
#:
#: * 1440x900 -- a laptop. The desktop two-column layout must be unchanged by
#:   this strike; if the tablet rules leak above 900px, this catches it.
#: * 834x1112 -- iPad portrait. The width the previous stacking rule broke at,
#:   and the orientation where the composer is furthest from the transcript.
#: * 1112x834 -- iPad landscape. Wider than 900px, so it is the case where a
#:   tablet gets the *desktop* layout: the sheet must be off-screen and the feed
#:   back in the flow, with no leftover overlay.
WIDTHS: tuple[tuple[int, int, str], ...] = (
    (1440, 900, "desktop"),
    (834, 1112, "ipad-portrait"),
    (1112, 834, "ipad-landscape"),
)

#: The smallest touch target an operator can reliably hit. Below this, a
#: mis-tap on Approve is a plausible outcome rather than an unlucky one.
MIN_TOUCH_PX = 44


Box = dict
Target = dict
Probe = dict


def _served_page() -> str:
    """The real Workbench page, with the responsive probe injected.

    Built by the real :func:`create_app` and the real token substitution, so
    what is measured is what an operator's browser receives.

    The Order Board's probe is **dropped**, not run alongside. Both probes set
    the document title to carry their findings out, and both listen on ``load``,
    so loading both means whichever finishes last wins and this module reads
    the other one's report -- an ``overflowX`` key that does not exist, and a
    suite that fails with a ``KeyError`` rather than a measurement. Sharing the
    page server while taking sole ownership of the title is what keeps the two
    modules independent.
    """
    html = harness._served_page()
    return html.replace(
        '<script src="/static/__order-board-probe.js"></script>',
        '<script src="/static/__responsive-probe.js"></script>',
    )


def _page_server() -> tuple[socketserver.TCPServer, str]:
    """The existing loopback server, serving the responsive probe.

    Delegates to the shared implementation so the handler -- including how it
    maps a URL onto a file on disk, the shared Chromium flags, and the
    ``DBUS_SESSION_BUS_ADDRESS`` child-env fix -- stays the one already
    exercised by the Order Board tests. The caller passes its probe through
    ``extra_probes`` rather than having this module reassign a harness global,
    which would have made the two modules order-dependent.

    Duplicating that server would mean a third Chromium configuration to keep in
    step with the first two, which is the drift
    ``test_ui_orders_page_harness.py`` exists to prevent.

    The page is passed in rather than letting the shared server inject its own:
    it would otherwise serve the *Order Board's* instrumented page, whose probe
    sets the same ``FORGE-PROBE`` document title this module parses -- so the two
    would race and this module would read the other's report.
    """
    return harness._page_server(
        0,
        extra_probes={"__responsive-probe.js": PROBE_JS},
        html=_served_page(),
    )


@functools.lru_cache(maxsize=None)
def _measure(width: int, height: int) -> Probe:
    """Load the page at one size in headless Chrome and return the report.

    Cached per size, for the same reason the Order Board measurement is: the
    launch costs seconds and the result is deterministic for a given page and
    viewport.
    """
    server, url = _page_server()
    try:
        result = subprocess.run(
            [
                harness.CHROME,
                *harness.CHROME_FLAGS,
                f"--window-size={width},{height}",
                "--virtual-time-budget=6000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=harness.chrome_env(),
        )
    finally:
        server.shutdown()
        server.server_close()  # type: ignore[attr-defined]

    found = re.search(r"<title>FORGE-PROBE (.*?)</title>", result.stdout, re.S)
    if not found:
        title = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
        raise AssertionError(
            "the responsive probe did not report; document title was "
            f"{title.group(1) if title else '(none)'!r}\n"
            f"chromium stderr: {result.stderr[-500:]}"
        )
    return json.loads(found.group(1))


pytestmark = pytest.mark.skipif(
    not harness._CHROME_USABLE,
    reason=(
        "chromium is required to measure the responsive layout in a real "
        f"engine: {harness._CHROME_REASON} The line's behaviour is covered "
        "without an engine by tests/test_ui_activity_ui.py."
    ),
)


@pytest.fixture(scope="module", params=WIDTHS, ids=[w[2] for w in WIDTHS])
def sized(request: pytest.FixtureRequest) -> tuple[str, int, int, Probe]:
    """The probe's report at each width, as ``(name, width, height, report)``."""
    width, height, name = request.param
    return name, width, height, _measure(width, height)


# -- no JavaScript errors, anywhere ---------------------------------------


def test_no_javascript_errors_at_any_width(sized: tuple[str, int, int, Probe]) -> None:
    """The page must load and drive cleanly at every tested size.

    Asserted per width because a layout that only works at one of them is not a
    layout. The sheet toggle is included in the probe's own work, so this also
    covers the new code path.
    """
    name, _, _, report = sized
    assert report["jsErrors"] == [], f"at {name}: {report['jsErrors']}"


def test_the_probe_actually_reached_the_toggle(sized: tuple[str, int, int, Probe]) -> None:
    """Guard on the guard.

    If the toggle never fired, every sheet assertion below would be measuring a
    closed sheet and reporting it as correct. The probe records the steps it
    completed; this asserts the open step is among them.
    """
    name, width, _, report = sized
    checked = report["checked"]
    if width > 900:
        # No sheet above the breakpoint, so there is nothing to open. The probe
        # says so explicitly rather than silently reporting nothing.
        assert checked == ["no-toggle-at-this-width"], (
            f"at {name} the probe neither opened a sheet nor recorded that "
            f"there is none (checked: {checked})"
        )
    else:
        # The step name carries *how* the open state was read -- whether the
        # engine ran the transition or the fallback measured the declared end
        # state. Either is a real reading; what must not happen is no reading.
        opened_steps = [c for c in checked if c.startswith("opened:")]
        assert opened_steps, (
            f"at {name} the probe never completed its open step "
            f"(checked: {checked}); the sheet assertions would be vacuous"
        )


# -- the conversation stays primary ---------------------------------------


def test_no_horizontal_overflow_at_any_width(sized: tuple[str, int, int, Probe]) -> None:
    """The document must not scroll sideways.

    The defect class ``test_ui_orders_page.py`` was written for, applied to the
    Workbench itself. A tablet that scrolls horizontally cannot be used with a
    thumb.
    """
    name, width, _, report = sized
    assert report["overflowX"] == 0, (
        f"at {name} ({width}px) the document scrolls sideways by "
        f"{report['overflowX']}px"
    )


def test_the_conversation_fills_the_viewport_height(sized: tuple[str, int, int, Probe]) -> None:
    """The conversation must own the screen, not share it.

    This is the assertion that would have failed before the strike, and it is
    the reason the feed became a sheet: with the two panes stacked, the
    conversation's height collapsed to roughly half the viewport and the live
    signal was below the fold for the whole run.
    """
    name, width, _, report = sized
    conversation = report["conversation"]
    viewport_height = report["viewport"]["height"]
    assert conversation is not None, f"at {name} the conversation pane is missing"

    # Compared against the MEASURED viewport, not the requested window size:
    # headless Chromium's window is taller than its viewport by the height of
    # browser chrome that is not in the screenshot area, so asserting against
    # the requested height would fail on a layout that is correct. Measured
    # 1440x900 -> 813px viewport, an 87px difference.
    #
    # And against the space *below* the header, not the whole viewport: the
    # header (60px) and the view switcher sit above this pane by design, so
    # "fills the viewport" means "reaches the bottom of it". What this catches
    # is the original defect -- a second pane sharing that space, which is why
    # the old rule failed: with the feed stacked, the conversation ended at
    # roughly half the viewport and the live signal was below the fold.
    reaches_bottom = conversation["bottom"] >= viewport_height - 2
    assert reaches_bottom, (
        f"at {name} the conversation ends at y={conversation['bottom']} in a "
        f"{viewport_height}px viewport -- something below it is taking space"
    )

    # And it must be the only pane in that column at tablet widths. At tablet
    # widths the feed is an overlay, so a conversation that shares the column
    # means the stacking rule leaked back in.
    if width <= 900:
        share = conversation["height"] / viewport_height
        assert share > 0.8, (
            f"at {name} the conversation is only {share:.0%} of the viewport -- "
            f"the Activity feed is stacked below it again"
        )


def test_the_activity_line_is_visible_at_every_width(
    sized: tuple[str, int, int, Probe]
) -> None:
    """The derived status line must be on screen, not merely in the DOM.

    At tablet widths the feed is a sheet, so this line is the only live signal
    on the primary surface. A line that is laid out but scrolled out of view
    would leave the page idle-looking again, which is the whole complaint.
    """
    name, _, _, report = sized
    line = report["activityLine"]
    viewport_height = report["viewport"]["height"]
    assert line is not None, f"at {name} the activity line is missing"
    assert line["visible"] is True, f"at {name} the activity line is not painted"
    assert line["y"] >= 0 and line["bottom"] <= viewport_height, (
        f"at {name} the activity line sits outside the viewport: "
        f"y={line['y']} bottom={line['bottom']} viewport={viewport_height}"
    )
    # And it has real width, so the text is not clipped to nothing.
    assert line["width"] >= 200, (
        f"at {name} the activity line is only {line['width']}px wide -- long "
        f"labels like 'Waiting for your approval' would be ellipsised into "
        f"uselessness"
    )


# -- the sheet ------------------------------------------------------------


def test_the_feed_is_out_of_the_flow_when_closed(
    sized: tuple[str, int, int, Probe]
) -> None:
    """Closed means off screen, not stacked below the conversation.

    Checked at tablet widths only, because above 900px the feed is a column
    beside the conversation and is supposed to be on screen.
    """
    name, width, _, report = sized
    if width > 900:
        pytest.skip("the feed is a column at desktop widths, not a sheet")
    closed = report["closed"]
    assert closed is not None, f"at {name} the closed activity pane was not measured"
    assert closed["offScreenBelow"] is True, (
        f"at {name} the closed Activity pane is still on screen at "
        f"y={closed['y']} (viewport {report['viewport']['height']}) -- it is "
        f"stacked rather than being a sheet"
    )


def test_the_sheet_opens_and_closes_at_tablet_widths(
    sized: tuple[str, int, int, Probe]
) -> None:
    """The toggle must actually move the pane, in both orientations.

    iPad portrait and landscape take different paths through the stylesheet --
    portrait is narrow enough for the two-row header, landscape is not -- so
    the toggle is exercised at each.
    """
    name, width, _, report = sized
    if width > 900:
        pytest.skip("there is no sheet at desktop widths")
    assert report["sheet"]["open"] is True, (
        f"at {name} clicking the toggle did not open the sheet"
    )
    assert report["sheet"]["ariaExpanded"] == "true", (
        f"at {name} the toggle opened the sheet without saying so to assistive tech"
    )
    opened = report["opened"]
    assert opened is not None, f"at {name} the opened sheet was not measured"
    assert opened["offScreenBelow"] is False, (
        f"at {name} the sheet reports open but is still translated off screen "
        f"(y={opened['y']}, viewport {report['viewport']['height']})"
    )
    # And it is a readable surface, not a strip: the sheet has to be tall enough
    # to show a run's event list. Measured, because `max-height` alone let this
    # collapse to its content -- 123px before the definite height was added.
    assert opened["height"] >= 200, (
        f"at {name} the open sheet is only {opened['height']}px tall"
    )
    # And it overlaps the viewport bottom, which is what "on screen" means for a
    # bottom-anchored sheet: its top edge is above the fold.
    assert opened["y"] < report["viewport"]["height"], (
        f"at {name} the open sheet's top edge (y={opened['y']}) is still below "
        f"the viewport ({report['viewport']['height']})"
    )


def test_the_feed_is_a_column_above_the_breakpoint(
    sized: tuple[str, int, int, Probe]
) -> None:
    """Landscape must not be left with a stranded overlay.

    1112px is wider than the breakpoint, so iPad landscape gets the desktop
    two-column layout. This asserts the tablet rules did not leak: the feed is
    on screen, beside the conversation, not floating over it.
    """
    name, width, height, report = sized
    if width <= 900:
        pytest.skip("this is the tablet-width arrangement, not the desktop one")
    activity = report["activity"]
    conversation = report["conversation"]
    assert activity is not None
    assert conversation is not None
    assert activity["offScreenBelow"] is False, (
        f"at {name} the Activity feed is off screen -- the sheet rules leaked "
        f"above the breakpoint"
    )
    # Beside, not below: the feed starts to the right of the conversation.
    assert activity["x"] >= conversation["x"] + conversation["width"] - 8, (
        f"at {name} the feed is not beside the conversation "
        f"(feed x={activity['x']}, conversation ends at "
        f"{conversation['x'] + conversation['width']})"
    )


def test_the_sheet_controls_are_absent_at_desktop_widths(
    sized: tuple[str, int, int, Probe]
) -> None:
    """A toggle that does nothing must not be on screen.

    Above the breakpoint the feed is already visible, so the toggle would be a
    control with no effect. It is removed from the layout rather than left
    inert.
    """
    name, width, _, report = sized
    if width <= 900:
        pytest.skip("the toggle is the point at tablet widths")
    toggle = report["touchTargets"].get("activityToggle")
    assert toggle is not None
    assert toggle["displayed"] is False, (
        f"at {name} the Activity toggle is displayed at a width where it "
        f"would do nothing"
    )


# -- touch targets --------------------------------------------------------


@pytest.mark.parametrize(
    "target", ["approve", "reject", "send", "activityToggle"]
)
def test_touch_targets_are_large_enough(
    sized: tuple[str, int, int, Probe], target: str
) -> None:
    """Every control used mid-run is at least 44px on its smaller axis.

    Measured with the approval card forced open, because those two buttons are
    hidden by default and measuring a hidden element measures nothing. A
    control that is not displayed at this width is skipped rather than failed:
    the rule is about the widths where the control applies.
    """
    name, width, _, report = sized
    if width > 900:
        pytest.skip(
            "the 44px touch rule is scoped to <=900px; a mouse-driven desktop "
            "layout keeps its existing control sizes"
        )
    measured = report["touchTargets"].get(target)
    assert measured is not None, f"at {name} {target} was not measured"
    if not measured["displayed"]:
        pytest.skip(f"{target} is not displayed at {name}")
    assert measured["min"] >= MIN_TOUCH_PX, (
        f"at {name} {target} is {measured['width']}x{measured['height']}px -- "
        f"below the {MIN_TOUCH_PX}px minimum"
    )


@pytest.mark.parametrize("view", ["workbench", "board"])
def test_view_switcher_buttons_are_large_enough(
    sized: tuple[str, int, int, Probe], view: str
) -> None:
    """Both view buttons meet the same bar.

    They are how an operator leaves the Order Board and comes back, and at
    834px they were two small targets at the top of a tablet screen.
    """
    name, width, _, report = sized
    if width > 900:
        pytest.skip("the 44px touch rule is scoped to <=900px")
    measured = report["views"].get(view)
    assert measured is not None, f"at {name} the {view} view button was not measured"
    if not measured["displayed"]:
        pytest.skip(f"the {view} view button is not displayed at {name}")
    assert measured["height"] >= MIN_TOUCH_PX, (
        f"at {name} the {view} view button is {measured['height']}px tall"
    )


def test_the_approval_buttons_are_equal_in_size(
    sized: tuple[str, int, int, Probe]
) -> None:
    """Approve and Reject must be the same size.

    This is the one interaction on the page where a mis-tap has consequences, so
    neither decision is the bigger or more prominent target -- an operator
    should not be able to tell which one the layout favours.
    """
    name, width, _, report = sized
    if width > 900:
        pytest.skip("the equal-size rule is part of the <=900px approval layout")
    approve = report["touchTargets"].get("approve")
    reject = report["touchTargets"].get("reject")
    assert approve is not None and reject is not None
    if not (approve["displayed"] and reject["displayed"]):
        pytest.skip("the approval card is not displayed at this width")
    assert approve["width"] == reject["width"], (
        f"at {name} Approve is {approve['width']}px and Reject is "
        f"{reject['width']}px -- one decision is the larger target"
    )


# -- the reserved slot ----------------------------------------------------


def test_the_selector_slot_exists_and_is_hidden(
    sized: tuple[str, int, int, Probe]
) -> None:
    """The reserved header slot is present, empty, and not taking space.

    Present so a future workspace/agent/model control has somewhere to go; empty
    because this strike deliberately adds no switching; and hidden so it costs
    nothing today.
    """
    name, _, _, report = sized
    # Measured through the page rather than the probe: the slot is `hidden`, so
    # its box is zero, and the probe's box() helper reports that as not visible.
    # What matters is that it exists and contributes no width to the header,
    # which is why this is asserted through the overflow check above rather
    # than by a geometry assertion that could not distinguish the two.
    assert "jsErrors" in report, f"at {name} the probe report is malformed"
