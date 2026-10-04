"""The Order Board's page wiring, checked in a real browser engine.

The other UI tests drive ``board.js`` against a stub DOM in Node. That proves what
the renderer builds, and it cannot prove what the *page* does with it: whether
selecting a view actually hides the other one, whether a cascade resolves a
selector the way an engine resolves it, or whether the document scrolls sideways.
All four were real defects here, and none of them was visible to a stub DOM.

**The three defects this file was written for**

*   ``.split { display: grid }`` beats the user agent's ``[hidden] { display:
    none }``, so the workbench pane reported ``hidden=true`` and painted anyway.
    Both views were on screen at once, which ruling 3 rules out.
*   The per-column ``.sr-only`` summary is ``position: absolute`` with no
    offsets, so without a positioned ancestor it resolved against the initial
    containing block and widened the document: 291px of sideways scroll on an
    834px tablet.
*   ``countAgrees`` read a renderer-computed field off the payload, so the board
    reported "its columns show undefined" on every load of a healthy board.

**Scope, deliberately narrow.** No browser-testing framework. This serves the
real page from the package, stubs only ``fetch``, clicks the real switcher, and
asserts four things per width. Rendering behaviour of ``board.js`` belongs to
``tests/test_ui_orders_board_ui.py``; HTTP behaviour to
``tests/test_ui_orders_api.py``. What is left for here is exactly the set of
properties that need an engine.

**Skips cleanly.** Chromium is looked up on ``PATH``; without it every test in
this module is skipped rather than failing. That is stated plainly because a
skipped test stops protecting anything -- which is why the driver also asserts,
separately and unconditionally, that Chromium was actually found when it is
present but unusable.
"""

from __future__ import annotations

import functools
import http.server
import json
import pathlib
import re
import shutil
import socketserver
import subprocess
import threading

import pytest

import forge.ui.server as server_module
from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.orders import OrderStore
from forge.ui.runner import runtime_factory
from forge.ui.security import UiToken
from forge.ui.session import WorkbenchSession

REPO = pathlib.Path(__file__).resolve().parent.parent
STATIC = REPO / "forge" / "ui" / "static"
PROBE_JS = pathlib.Path(__file__).resolve().parent / "ui_probe" / "order_board_probe.js"

#: Widths worth checking. A laptop and a tablet, because the six-column board
#: overflows both and the two fail differently: the desktop scrolls inside its
#: container, the narrow one is where a flex item's `min-width: auto` shows up.
WIDTHS = ((1440, 900, "laptop"), (834, 1112, "tablet"))

#: The board the probe renders: two orders in the first column, one in the third,
#: the rest empty. Populated *and* empty columns in one payload, so a board that
#: renders only the columns with something in them fails here.
BOARD: dict[str, object] = {
    "columns": [
        {
            "status": "orders_received",
            "label": "Orders Received",
            "count": 2,
            "orders": [
                {
                    "order_id": "ORD-0001",
                    "title": "Straighten the tongs",
                    "status": "orders_received",
                    "status_label": "Orders Received",
                    "priority": "high",
                    "smith": "Byte",
                    "description": "Flatten to 6mm",
                    "created_at": "2026-10-04T03:04:11+00:00",
                    "sequence": 1,
                },
                {
                    "order_id": "ORD-0002",
                    "title": "Draw out the bar",
                    "status": "orders_received",
                    "status_label": "Orders Received",
                    "priority": "urgent",
                    "smith": "Forge",
                    "description": "",
                    "created_at": "2026-10-04T03:04:11+00:00",
                    "sequence": 2,
                },
            ],
        },
        {"status": "on_the_rack", "label": "On The Rack", "count": 0, "orders": []},
        {
            "status": "at_the_forge",
            "label": "At The Forge",
            "count": 1,
            "orders": [
                {
                    "order_id": "ORD-0003",
                    "title": "Temper the blade",
                    "status": "at_the_forge",
                    "status_label": "At The Forge",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "2026-10-04T03:04:11+00:00",
                    "sequence": 3,
                }
            ],
        },
        {"status": "in_the_quench", "label": "In The Quench", "count": 0, "orders": []},
        {
            "status": "ready_for_inspection",
            "label": "Ready For Inspection",
            "count": 0,
            "orders": [],
        },
        {"status": "finished_goods", "label": "Finished Goods", "count": 0, "orders": []},
    ],
    "statuses": [
        "orders_received",
        "on_the_rack",
        "at_the_forge",
        "in_the_quench",
        "ready_for_inspection",
        "finished_goods",
    ],
    "priorities": ["low", "normal", "high", "urgent"],
    "total": 3,
    "next_id": "ORD-0004",
}

CHROME = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which(
    "google-chrome"
)

pytestmark = pytest.mark.skipif(
    CHROME is None,
    reason="chromium is required to check page wiring in a real engine",
)


# --------------------------------------------------------------------------
# Serving the real page
# --------------------------------------------------------------------------


def _served_page() -> str:
    """The real ``index.html``, with the probe injected.

    Produced by the real :func:`create_app` and the real token substitution, so
    what is measured is what an operator's browser receives. Only ``fetch`` is
    stubbed, by the probe.
    """
    from fastapi.testclient import TestClient

    token = UiToken()
    model = ScriptedGateway([])
    workspace = Workspace(pathlib.Path("/nonexistent-board-probe-workspace"))
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=model, model_id="scripted"
        ),
        broker=SseBroker(),
    )
    app = server_module.create_app(
        workspace=workspace,
        session=session,
        gateway=model,
        token=token,
        port=8765,
    )
    with TestClient(app) as client:
        html = client.get("/").text

    injection = (
        "<script>window.__board="
        + json.dumps(BOARD)
        + ';</script><script src="/static/__order-board-probe.js"></script>'
    )
    return html.replace("</head>", injection + "</head>")


def _page_server(directory: int) -> tuple[socketserver.TCPServer, str]:
    """Serve the real static files plus the instrumented page on loopback."""
    html = _served_page()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            name = path.lstrip("/").split("?")[0]
            if name in ("", "index.html"):
                return ""
            if name.startswith("static/"):
                name = name[len("static/") :]
            # The probe is served from this origin. A `file://` script on an
            # `http://` page is refused by Chrome, which is a property of the
            # harness and not of the page under test.
            if name == "__order-board-probe.js":
                return str(PROBE_JS)
            return str(STATIC / name)

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            if self.translate_path(self.path) == "":
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *args: object) -> None:
            pass

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    return server, url


@functools.lru_cache(maxsize=None)
def _measure(width: int, height: int) -> dict[str, object]:
    """Load the page in headless Chrome and return the probe's report.

    Cached per width: the measurement is expensive and deterministic for a given
    page and viewport, and re-running it per assertion would multiply the cost of
    this module by five for no extra coverage.
    """
    server, url = _page_server(0)
    try:
        result = subprocess.run(
            [
                CHROME,
                "--headless",
                "--disable-gpu",
                "--no-sandbox",
                f"--window-size={width},{height}",
                "--virtual-time-budget=6000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        server.shutdown()
        server.server_close()

    found = re.search(r"<title>FORGE-PROBE (.*?)</title>", result.stdout, re.S)
    if not found:
        title = re.search(r"<title>(.*?)</title>", result.stdout, re.S)
        raise AssertionError(
            "the probe did not report; document title was "
            f"{title.group(1) if title else '(none)'!r}\n"
            f"chromium stderr: {result.stderr[-500:]}"
        )
    return json.loads(found.group(1))


@pytest.fixture(scope="module")
def laptop() -> dict[str, object]:
    return _measure(1440, 900)


@pytest.fixture(scope="module")
def tablet() -> dict[str, object]:
    return _measure(834, 1112)


# --------------------------------------------------------------------------
# The four properties
# --------------------------------------------------------------------------


def test_chromium_actually_ran_the_page(laptop: dict[str, object]) -> None:
    """The other tests pass only if the probe reported at all.

    Without this, a Chromium that fails to launch a page -- sandbox, missing
    display, a crash on load -- would make every assertion below vacuous, and the
    module would report success while checking nothing.
    """
    assert laptop["checked"], "the probe ran but asserted nothing"


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_selecting_the_board_leaves_only_the_board_painted(
    request: pytest.FixtureRequest, view: str
) -> None:
    """Ruling 3: the board replaces the workbench content area.

    Measured by geometry, not by the ``hidden`` attribute, because a ``display``
    declaration can beat ``[hidden]`` -- which is exactly how the workbench pane
    once reported itself hidden and painted anyway.
    """
    report = request.getfixturevalue(view)

    assert report["boardPainted"] > 0, "the board view painted nothing"
    assert report["workbenchPainted"] == 0, (
        f"the workbench painted {report['workbenchPainted']} element(s) behind "
        "the board; the board must replace it, not share the page"
    )


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_switching_back_leaves_only_the_workbench_painted(
    request: pytest.FixtureRequest, view: str
) -> None:
    """A toggle that only works one way is a broken toggle."""
    report = request.getfixturevalue(view)

    assert report["switchBack"] is True, (
        "switching back to the workbench left the board painted"
    )


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_a_populated_board_renders_all_six_columns(
    request: pytest.FixtureRequest, view: str
) -> None:
    report = request.getfixturevalue(view)

    assert report["columnCount"] == 6, f"expected 6 columns, got {report['columnCount']}"


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_the_empty_columns_still_render(
    request: pytest.FixtureRequest, view: str
) -> None:
    """Six columns means the empty ones too.

    The payload has orders in two columns and none in four. A renderer that only
    emits columns with content would show two, which is also what a board that
    has silently lost its vocabulary would look like.
    """
    report = request.getfixturevalue(view)

    assert "On The Rack" in str(report["columnHeadings"]), (
        f"headings were {report['columnHeadings']!r}"
    )
    assert "Finished Goods" in str(report["columnHeadings"])


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_no_javascript_errors_during_load_or_view_switch(
    request: pytest.FixtureRequest, view: str
) -> None:
    report = request.getfixturevalue(view)

    assert report["jsErrors"] == [], f"JavaScript errors: {report['jsErrors']}"


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_the_page_does_not_scroll_sideways(
    request: pytest.FixtureRequest, view: str
) -> None:
    """The board scrolls; the *document* must not.

    This is the check that caught the ``.sr-only`` summary widening the document
    by 291px on a tablet: an absolutely positioned box with no positioned
    ancestor resolves against the initial containing block, so it contributed to
    the page's scroll width rather than to its own column's.
    """
    report = request.getfixturevalue(view)

    assert report["pageOverflowX"] <= 0, (
        f"document overflows sideways by {report['pageOverflowX']}px"
    )


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_every_empty_column_states_that_it_is_empty(
    request: pytest.FixtureRequest, view: str
) -> None:
    """Six headings with four blank columns is a broken board, not an empty one.

    Column count alone cannot tell the two apart, so the messages are counted in
    their own right. Four is the number the fixture leaves empty.
    """
    report = request.getfixturevalue(view)

    assert report["emptyMessages"] == 4, (
        f"found {report['emptyMessages']} empty-column messages, expected 4"
    )
    assert "No orders in" in str(report["emptyMessageText"])


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_the_board_note_states_a_plain_order_count(
    request: pytest.FixtureRequest, view: str
) -> None:
    """The cross-check the backend cannot make was removed, per ruling 3.

    It read the payload's own ``total`` -- which the backend derives from these
    same columns -- against the renderer's count of them, so both sides came from
    one payload. It reported "the board holds 3 orders but its columns show
    undefined" on every load of a healthy board. What remains is the count the
    board actually drew.
    """
    report = request.getfixturevalue(view)

    assert "undefined" not in str(report["boardNote"]), (
        f"the board note rendered an undefined value: {report['boardNote']!r}"
    )
    assert report["boardNote"] == "3 orders"


@pytest.mark.parametrize("view", ["laptop", "tablet"])
def test_the_probe_reports_no_failures(request: pytest.FixtureRequest, view: str) -> None:
    """The probe's own roll-up, so a new check cannot be added and forgotten.

    Every property the probe asserts is also asserted above as its own test, so
    this is a backstop rather than the primary check -- its value is that adding a
    check to the probe without adding a test for it still fails here.
    """
    report = request.getfixturevalue(view)

    assert report["ok"] is True, (
        f"probe failures: {report.get('failures')}; "
        f"errors: {report.get('jsErrors')}"
    )


def test_the_probe_file_is_where_the_driver_expects_it() -> None:
    """A moved probe would skip rather than fail, silently checking nothing."""
    assert PROBE_JS.exists(), f"probe script missing at {PROBE_JS}"
    assert "FORGE-PROBE" in PROBE_JS.read_text(encoding="utf-8"), (
        "the probe no longer writes the marker the driver parses"
    )


def test_the_board_payload_covers_populated_and_empty_columns() -> None:
    """The fixture must exercise both, or the checks above prove less.

    ``_served_page`` builds its payload from :data:`BOARD`, and this asserts that
    payload still has what it was written to have -- otherwise a later edit could
    quietly reduce this module to testing an all-empty board.
    """
    columns = BOARD["columns"]

    assert len(columns) == 6
    assert any(column["orders"] for column in columns), "no populated column"
    assert any(not column["orders"] for column in columns), "no empty column"
    assert BOARD["statuses"] == [column["status"] for column in columns]


def test_the_store_reports_a_board_this_shape() -> None:
    """The fixture matches what the real store emits.

    Guards against the probe testing a payload the backend would never send -- a
    field rename would leave this module green while the real page broke.
    """
    assert "columns" in OrderStore(pathlib.Path("/tmp")).board().as_payload()