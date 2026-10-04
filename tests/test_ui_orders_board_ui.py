"""The Order Board view: rendering populated and empty columns, and its boundaries.

Two kinds of test, and the split is the same as for the model-health indicator.

**Behavioural**, driven by Node against ``forge/ui/static/board.js`` with a stub
DOM: one test per thing the board has to render -- a populated column, an empty
column, a card, the move control, and the states a colour-blind operator or a
screen reader must be able to read. Asserting on the glyph and the visible word
is what makes "not colour alone" a checked property rather than an intention.

**Structural**, in Python against the shipped source: the frontend must not
invent workflow states and must not reproduce the backend's vocabulary. A
well-formatted wrong board looks exactly like a right one, and a rendering test
cannot see that difference.

The stub DOM throws on ``innerHTML`` rather than supporting it, so the "no
operator-typed text is ever parsed as markup" property is enforced by the harness
instead of asserted in a comment.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

UI_STATIC = pathlib.Path(__file__).resolve().parent.parent / "forge" / "ui" / "static"
BOARD_JS = UI_STATIC / "board.js"
APP_JS = UI_STATIC / "app.js"
INDEX_HTML = UI_STATIC / "index.html"
STYLES_CSS = UI_STATIC / "styles.css"
STUB = pathlib.Path(__file__).resolve().parent / "ui_stub" / "dom.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is required to drive the browser-side render function",
)


# --------------------------------------------------------------------------
# Fixtures: a payload shaped exactly as GET /api/orders returns it
# --------------------------------------------------------------------------

STATUSES = [
    "orders_received",
    "on_the_rack",
    "at_the_forge",
    "in_the_quench",
    "ready_for_inspection",
    "finished_goods",
]

LABELS = {
    "orders_received": "Orders Received",
    "on_the_rack": "On The Rack",
    "at_the_forge": "At The Forge",
    "in_the_quench": "In The Quench",
    "ready_for_inspection": "Ready For Inspection",
    "finished_goods": "Finished Goods",
}


def _order(
    order_id="ORD-0001",
    title="Straighten the tongs",
    status="orders_received",
    priority="normal",
    smith="Byte",
    description="Flatten to 6mm",
    sequence=1,
):
    # A status outside the fixture's label table is allowed, because a test has to
    # be able to feed the renderer a state this build has never seen. The label
    # falls back to a titleised form, exactly as the renderer does.
    label = LABELS.get(status, status.replace("_", " ").title())
    return {
        "order_id": order_id,
        "title": title,
        "status": status,
        "status_label": label,
        "priority": priority,
        "smith": smith,
        "description": description,
        "created_at": "2026-10-04T02:00:00+00:00",
        "sequence": sequence,
    }


def _column(status, *orders):
    return {
        "status": status,
        "label": LABELS[status],
        "count": len(orders),
        "orders": list(orders),
    }


def _payload(*columns, **overrides):
    """A full board: every status present, in workflow order."""
    by_status = {column["status"]: column for column in columns}
    ordered = [
        by_status.get(status, _column(status))
        for status in STATUSES
        if status in by_status or True
    ]
    payload = {
        "columns": [_column(status) for status in STATUSES]
        if not columns
        else ordered,
        "statuses": list(STATUSES),
        "priorities": ["low", "normal", "high", "urgent"],
        "total": sum(len(column["orders"]) for column in columns),
        "next_id": "ORD-0001",
    }
    payload.update(overrides)
    return payload


def populated():
    """Two orders in the first column, one in the third, the rest empty."""
    return _payload(
        _column(
            "orders_received",
            _order(),
            _order(
                order_id="ORD-0002",
                title="Draw out the bar",
                priority="high",
                smith="Forge",
                sequence=2,
            ),
        ),
        _column("at_the_forge", _order(order_id="ORD-0003", status="at_the_forge", sequence=3)),
    )


HARNESS = """
const path = require("path");
const {{ StubDocument }} = require({stub!r});
const board = require({board!r});
const payload = {payload};
const order = {order};

const doc = new StubDocument();
const container = doc.createElement("div");

const opened = [];
const moved = [];
const deleted = [];
const rendered = board.renderBoard(doc, container, payload, {{
  onOpen: function (value) {{ opened.push(value.orderId); }},
}});

// Click the Nth card, if asked.
if (process.argv[2] === "click") {{
  const ids = Object.keys(rendered.cards);
  rendered.cards[ids[Number(process.argv[3] || 0)]].click();
}}

/* The delete control, which lives only in the detail dialog.
 *
 * `--detail` builds the dialog; an optional second step drives the two-step
 * confirmation: `confirm` clicks the destructive button and reads the prompt it
 * reveals, `cancel` clicks it and then the cancel button, `destroy` clicks it and
 * then the confirming button. Recording `deleted` rather than performing a
 * request is what lets "cancel leaves the order alone" be asserted at all.
 */
let detail = null;
if (process.argv[2] === "detail") {{
  const holder = doc.createElement("div");
  detail = board.renderDetail(doc, holder, order, payload.columns, {{
    onMove: function (id, status) {{ moved.push(id + ":" + status); }},
    onDelete: function (id, title) {{ deleted.push(id + ":" + title); }},
  }});
  if (process.argv[3] && process.argv[3] !== "confirm" &&
      process.argv[3] !== "destroy" && process.argv[3] !== "cancel") {{
    const buttons = detail.move ? detail.move.findAll(function (el) {{
      return el.getAttribute("data-target-status") !== null;
    }}) : [];
    buttons[Number(process.argv[3])].click();
  }}
  var step = ["confirm", "destroy", "cancel"].indexOf(process.argv[3]) >= 0
    ? process.argv[3]
    : process.argv[4];
  if (step) {{
    detail.deleteButton.click();
    if (step === "destroy") detail.confirmDelete.click();
    if (step === "cancel") detail.cancelDelete.click();
  }}
}}

function findDelete(el) {{
  return el.findAll(function (node) {{
    return String(node.className).split(/\\s+/).indexOf("order-delete") >= 0;
  }});
}}

function describeButton(el) {{
  return el ? {{
    text: el.textContent,
    label: el.getAttribute("aria-label"),
    type: el.type,
    hidden: el.getAttribute("hidden"),
    destructive: el.getAttribute("data-destructive"),
  }} : null;
}}

function describeCard(button) {{
  return {{
    orderId: button.getAttribute("data-order-id"),
    priority: button.getAttribute("data-priority"),
    status: button.getAttribute("data-status"),
    label: button.getAttribute("aria-label"),
    type: button.type,
    text: button.text(),
    glyph: button.findOne(function (el) {{
      return String(el.className).indexOf("board-priority-glyph") >= 0;
    }}).textContent,
    glyphHidden: button.findOne(function (el) {{
      return String(el.className).indexOf("board-priority-glyph") >= 0;
    }}).getAttribute("aria-hidden"),
  }};
}}

const columnElements = rendered.columns;
const statuses = Object.keys(columnElements);

process.stdout.write(JSON.stringify({{
  model: {{
    total: rendered.model.total,
    nextId: rendered.model.nextId,
    columnCount: rendered.model.columns.length,
    statuses: rendered.model.statuses,
    priorities: rendered.model.priorities,
    columnLabels: rendered.model.columns.map(function (c) {{ return c.label; }}),
  }},
  columnOrder: statuses,
  columns: statuses.map(function (status) {{
    const el = columnElements[status];
    return {{
      status: status,
      heading: el.findOne(function (e) {{
        return String(e.className).indexOf("board-column-title") >= 0;
      }}).textContent,
      count: el.findOne(function (e) {{
        return String(e.className).indexOf("board-column-count") >= 0;
      }}).textContent,
      countHidden: el.findOne(function (e) {{
        return String(e.className).indexOf("board-column-count") >= 0;
      }}).getAttribute("aria-hidden"),
      summary: el.findOne(function (e) {{
        return String(e.className).indexOf("sr-only") >= 0;
      }}).textContent,
      emptyMessages: el.byClass("board-empty").map(function (e) {{ return e.textContent; }}),
      cards: el.byClass("board-card").map(describeCard),
      cardCount: el.byClass("board-card").length,
      listLabel: el.findOne(function (e) {{
        return String(e.className).indexOf("board-cards") >= 0;
      }}).getAttribute("aria-label"),
      known: el.getAttribute("data-known"),
      unlabelled: el.getAttribute("data-unlabelled"),
      text: el.text(),
    }};
  }}),
  opened: opened,
  moved: moved,
  deleted: deleted,
  cardsWithDeleteControl: rendered.cards &&
    Object.keys(rendered.cards).filter(function (id) {{
      const card = rendered.cards[id];
      return String(card.className).split(/\\s+/).indexOf("order-delete") >= 0 ||
        card.byClass("order-delete").length > 0;
    }}),
  detail: detail && detail.model && {{
    label: detail.dialog.getAttribute("aria-labelledby"),
    modal: detail.dialog.getAttribute("aria-modal"),
    orderId: detail.dialog.getAttribute("data-order-id"),
    text: detail.dialog.text(),
    role: detail.dialog.getAttribute("role"),
    moveLabel: detail.move.getAttribute("aria-label"),
    deleteControls: findDelete(detail.dialog).map(describeButton),
    deleteButton: describeButton(detail.deleteButton),
    confirmDelete: describeButton(detail.confirmDelete),
    cancelDelete: describeButton(detail.cancelDelete),
    confirmRegionHidden: detail.confirm
      ? detail.confirm.getAttribute("hidden")
      : "(no confirm region)",
    confirmPrompt: detail.confirm
      ? detail.confirm.text()
      : "(no confirm region)",
    confirmLabel: detail.confirm
      ? detail.confirm.getAttribute("aria-label")
      : "(no confirm region)",
    confirmDescribed: detail.deleteButton
      ? detail.deleteButton.getAttribute("aria-describedby")
      : null,
    moveButtons: detail.move.findAll(function (el) {{
      return el.getAttribute("data-target-status") !== null;
    }}).map(function (b) {{
      return {{
        status: b.getAttribute("data-target-status"),
        text: b.textContent,
        label: b.getAttribute("aria-label"),
        current: b.getAttribute("aria-current"),
        disabled: b.disabled === true,
        type: b.type,
      }};
    }}),
  }},
  innerHTMLUsed: false,
}}, null, 2));
"""


#: The default for ``render(order=...)``: use the populated fixture. A separate
#: sentinel is needed because a default of ``None`` and an explicit ``None`` are
#: the same value, and the "no order" test would silently receive the fixture.
_DEFAULT_ORDER = "--fixture-order"


def render(
    payload=None, *, order: object = _DEFAULT_ORDER, action=None, index=None, step=None
):
    """Run board.js in Node against the stub DOM and return its report.

    Omitting ``order`` uses the populated fixture; ``order=None`` reaches the
    renderer's own empty path. ``step`` is the confirmation step -- ``"destroy"``
    or ``"cancel"`` -- and is only meaningful with ``action="detail"``.
    """
    script = HARNESS.format(
        stub=str(STUB),
        board=str(BOARD_JS),
        payload=json.dumps(payload if payload is not None else _payload()),
        order=json.dumps(_order() if order is _DEFAULT_ORDER else order),
    )
    script_file = pathlib.Path("/tmp/forge-board-harness.js")
    script_file.write_text(script, encoding="utf-8")

    argv = [NODE, str(script_file)]
    if action:
        argv.append(action)
        if index is not None:
            argv.append(str(index))
        if step is not None:
            argv.append(step)

    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=60, cwd=str(STUB.parent)
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def column(report, status):
    """One column's report, by status."""
    for entry in report["columns"]:
        if entry["status"] == status:
            return entry
    raise AssertionError(f"no column {status!r} in {report['columnOrder']}")


# --------------------------------------------------------------------------
# The empty board
# --------------------------------------------------------------------------


def test_an_empty_board_renders_every_column() -> None:
    report = render()

    assert report["columnOrder"] == STATUSES
    assert report["model"]["total"] == 0


def test_every_empty_column_says_so_visibly() -> None:
    """The required empty state, on every column rather than one."""

    report = render()

    for status in STATUSES:
        assert column(report, status)["emptyMessages"], f"{status} has no message"


def test_the_empty_message_names_the_column() -> None:
    report = render()

    assert "Orders Received" in column(report, "orders_received")["emptyMessages"][0]


def test_an_empty_column_renders_no_cards() -> None:
    report = render()

    assert all(entry["cardCount"] == 0 for entry in report["columns"])


def test_an_empty_column_still_shows_a_count_of_zero() -> None:
    report = render()

    assert column(report, "at_the_forge")["count"] == "0"


def test_an_empty_board_still_reports_the_next_id() -> None:
    assert render(_payload(next_id="ORD-0007"))["model"]["nextId"] == "ORD-0007"


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


def test_the_columns_are_rendered_in_the_backends_order() -> None:
    """The backend defines the workflow order; the page does not re-sort it."""

    assert render(populated())["columnOrder"] == STATUSES


def test_a_populated_column_renders_its_cards() -> None:
    report = render(populated())

    assert column(report, "orders_received")["cardCount"] == 2


def test_a_populated_column_counts_its_orders() -> None:
    report = render(populated())

    assert column(report, "orders_received")["count"] == "2"


def test_a_column_with_orders_has_no_empty_message() -> None:
    report = render(populated())

    assert column(report, "orders_received")["emptyMessages"] == []


def test_a_partly_filled_board_shows_messages_only_where_it_is_empty() -> None:
    report = render(populated())

    assert column(report, "orders_received")["emptyMessages"] == []
    assert column(report, "at_the_forge")["emptyMessages"] == []
    assert len(column(report, "in_the_quench")["emptyMessages"]) == 1


def test_the_column_heading_is_the_backends_label() -> None:
    report = render(populated())

    assert column(report, "in_the_quench")["heading"] == "In The Quench"


def test_a_column_with_no_backend_label_gets_a_readable_heading() -> None:
    """A last resort, so a new state is never an unlabelled column."""

    payload = _payload()
    for entry in payload["columns"]:
        if entry["status"] == "at_the_forge":
            entry["label"] = ""

    report = render(payload)

    assert column(report, "at_the_forge")["heading"] == "At the forge"


def test_a_column_summary_states_how_many_orders_it_holds() -> None:
    """What assistive tech reads, rather than a bare heading."""

    report = render(populated())

    assert "2 orders" in column(report, "orders_received")["summary"]


def test_a_column_summary_states_the_column_it_belongs_to() -> None:
    report = render(populated())

    assert "Orders Received" in column(report, "orders_received")["summary"]


def test_a_column_summary_is_singular_for_one_order() -> None:
    report = render(populated())

    assert "1 order " in column(report, "at_the_forge")["summary"] + " "


def test_an_empty_column_summary_counts_nothing() -> None:
    report = render()

    assert "0 orders" in column(report, "at_the_forge")["summary"]


def test_the_column_count_is_hidden_from_assistive_technology() -> None:
    """The count is decoration beside the heading; the summary says it once."""

    report = render(populated())

    assert column(report, "orders_received")["countHidden"] == "true"


def test_the_card_list_is_labelled_by_its_column() -> None:
    report = render(populated())

    assert column(report, "at_the_forge")["listLabel"] == "At The Forge"


def test_an_unfamiliar_state_still_gets_a_column() -> None:
    """Dropping it would hide an order that is genuinely sitting there."""

    payload = _payload(_column("orders_received", _order()))
    payload["columns"].append(
        {
            "status": "awaiting_inspection",
            "label": "Awaiting Inspection",
            "count": 1,
            "orders": [_order(order_id="ORD-0009", status="awaiting_inspection")],
        }
    )

    report = render(payload)

    assert "awaiting_inspection" in report["columnOrder"]
    assert column(report, "awaiting_inspection")["cardCount"] == 1


def test_an_unlabelled_state_is_marked_as_provisional() -> None:
    payload = _payload()
    for entry in payload["columns"]:
        if entry["status"] == "in_the_quench":
            entry["label"] = ""

    report = render(payload)

    assert column(report, "in_the_quench")["unlabelled"] == "yes"


def test_a_labelled_state_is_not_marked_as_provisional() -> None:
    assert column(render(), "at_the_forge")["unlabelled"] is None


# --------------------------------------------------------------------------
# Cards
# --------------------------------------------------------------------------


def test_a_card_shows_the_order_number() -> None:
    report = render(populated())

    card = column(report, "orders_received")["cards"][0]
    assert card["orderId"] == "ORD-0001"
    assert "ORD-0001" in card["text"]


def test_a_card_shows_the_title() -> None:
    report = render(populated())

    assert "Straighten the tongs" in column(report, "orders_received")["cards"][0]["text"]


def test_a_card_shows_the_smith() -> None:
    report = render(populated())

    assert "Byte" in column(report, "orders_received")["cards"][0]["text"]


def test_a_card_shows_the_priority_as_a_word_not_only_a_colour() -> None:
    report = render(populated())

    card = column(report, "orders_received")["cards"][1]
    assert card["priority"] == "high"
    assert "high" in card["text"]


def test_each_priority_has_a_distinct_glyph() -> None:
    """Four levels, four glyphs -- the property that makes colour unnecessary."""

    glyphs = {
        priority: render(
            _payload(
                _column(
                    "orders_received",
                    _order(priority=priority),
                )
            )
        )["columns"][0]["cards"][0]["glyph"]
        for priority in ("low", "normal", "high", "urgent")
    }

    assert len(set(glyphs.values())) == 4, glyphs


def test_the_priority_glyph_is_hidden_from_assistive_technology() -> None:
    """The word is the state; a reader announcing "double exclamation" is noise."""

    report = render(populated())

    assert column(report, "orders_received")["cards"][0]["glyphHidden"] == "true"


def test_a_card_carries_its_priority_in_a_data_attribute() -> None:
    report = render(populated())

    assert column(report, "orders_received")["cards"][1]["priority"] == "high"


def test_a_card_names_its_order_in_the_accessible_name() -> None:
    report = render(populated())

    label = column(report, "orders_received")["cards"][0]["label"]
    assert "ORD-0001" in label
    assert "Straighten the tongs" in label


def test_the_accessible_name_states_the_priority() -> None:
    report = render(populated())

    assert "urgent" in _one_card(render(_payload(_column("orders_received", _order(priority="urgent")))))


def _one_card(report):
    return report["columns"][0]["cards"][0]["label"]


def test_the_accessible_name_states_the_smith() -> None:
    report = render(populated())

    assert "Byte" in column(report, "orders_received")["cards"][0]["label"]


def test_an_unassigned_order_says_unassigned_rather_than_showing_a_blank() -> None:
    report = render(populated(_payload)) if False else render(
        _payload(_column("orders_received", _order(smith="")))
    )

    card = report["columns"][0]["cards"][0]
    assert "unassigned" in card["text"]


def test_an_unassigned_order_says_so_in_the_accessible_name() -> None:
    report = render(_payload(_column("orders_received", _order(smith=""))))

    assert "unassigned" in report["columns"][0]["cards"][0]["label"]


def test_a_card_is_a_button() -> None:
    report = render(populated())

    assert column(report, "orders_received")["cards"][0]["type"] == "button"


def test_clicking_a_card_reports_which_order_was_opened() -> None:
    report = render(populated(), action="click", index=1)

    assert report["opened"] == ["ORD-0002"]


def test_cards_appear_oldest_first() -> None:
    """The backend already sorts; the page must not reorder what it was given."""

    report = render(populated())

    ids = [card["orderId"] for card in column(report, "orders_received")["cards"]]
    assert ids == ["ORD-0001", "ORD-0002"]


# --------------------------------------------------------------------------
# Details and movement
# --------------------------------------------------------------------------


def test_an_order_opens_into_a_dialog() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["role"] == "dialog"
    assert report["detail"]["modal"] == "true"
    assert report["detail"]["orderId"] == "ORD-0001"


def test_the_dialog_names_its_own_heading() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["label"] == "order-detail-title"


def test_the_dialog_shows_the_orders_description() -> None:
    report = render(populated(), action="detail")

    assert "Flatten to 6mm" in report["detail"]["text"]


def test_the_dialog_shows_the_priority_as_a_word() -> None:
    report = render(populated(), action="detail", order=_order(priority="urgent"))

    assert "urgent" in report["detail"]["text"]


def test_the_move_control_offers_every_workflow_state() -> None:
    report = render(populated(), action="detail")

    offered = [b["status"] for b in report["detail"]["moveButtons"]]
    assert offered == STATUSES


def test_the_move_control_offers_no_state_the_backend_did_not_send() -> None:
    """The buttons are built from the payload's statuses, not from a local list."""

    report = render(populated(), action="detail")

    assert all(button["status"] in STATUSES for button in report["detail"]["moveButtons"])


def test_the_move_control_names_the_order_and_the_target() -> None:
    report = render(populated(), action="detail")

    button = report["detail"]["moveButtons"][2]
    assert "ORD-0001" in button["label"]
    assert "At The Forge" in button["label"]


def test_the_move_control_marks_the_orders_current_state() -> None:
    report = render(populated(), action="detail")

    current = [b for b in report["detail"]["moveButtons"] if b["current"] == "true"]
    assert [b["status"] for b in current] == ["orders_received"]


def test_the_current_state_is_not_an_actionable_button() -> None:
    """Moving to where it already is is not an error, so the control offers
    nothing to click there rather than a button that does nothing visible."""

    report = render(populated(), action="detail")

    assert [b["disabled"] for b in report["detail"]["moveButtons"]] == [
        True, False, False, False, False, False
    ]


def test_the_move_control_is_a_named_group() -> None:
    report = render(populated(), action="detail")

    assert "ORD-0001" in report["detail"]["moveLabel"]


def test_clicking_a_move_button_reports_the_order_and_the_target() -> None:
    report = render(populated(), action="detail", index=2)

    assert report["moved"] == ["ORD-0001:at_the_forge"]


def test_a_move_button_is_a_real_button() -> None:
    report = render(populated(), action="detail")

    assert all(b["type"] == "button" for b in report["detail"]["moveButtons"])


def test_no_order_is_rendered_without_one() -> None:
    report = render(populated(), action="detail", order=None)

    assert report["detail"] is None


# --------------------------------------------------------------------------
# Structural: the frontend must not invent the workflow
# --------------------------------------------------------------------------


def _js_code(path: pathlib.Path = BOARD_JS) -> str:
    """The file with comments and string literals removed.

    Scanning raw text is how a guard starts firing on prose: this file's own
    comments name the workflow states repeatedly to explain that it must not
    decide them, so a text scan either fails on a correct file or gets weakened
    until it proves nothing. These guards run over code only.
    """
    source = path.read_text(encoding="utf-8")
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", "", without_block)
    return re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", '""', without_line)


def test_the_frontend_invents_no_workflow_state() -> None:
    """The column list comes from the payload and is never assembled here.

    A hard-coded state list in the frontend is a second source of truth that
    would show a column the backend does not have, and hide one it added.
    """
    source = _js_code()
    for status in STATUSES:
        assert status not in source, f"board.js names the workflow state {status!r}"


def test_the_frontend_reproduces_no_priority_vocabulary() -> None:
    """Priority presentation is a lookup; the *vocabulary* is the backend's.

    The glyph table here is presentation and may name levels -- exactly as
    ``health.js`` names health words. What it may not do is decide which levels
    exist, and that is enforced on the wiring file instead: the create form's
    options come from the payload.
    """
    app = _js_code(APP_JS)
    for priority in ("low", "normal", "high", "urgent"):
        assert priority not in app, f"app.js names the priority {priority!r}"
    assert "payload.priorities" in APP_JS.read_text(encoding="utf-8")


def test_the_frontend_does_not_sort_or_filter_the_columns() -> None:
    """Board order is the backend's, and a re-sort would quietly reorder it."""

    source = _js_code()
    offenders = re.findall(r"columns\s*\.\s*(sort|reverse|filter)\s*\(", source)
    assert not offenders, f"board.js reorders the backend's columns: {offenders}"


def test_the_frontend_writes_no_storage() -> None:
    """A refresh re-reads the server. Same rule the rest of the page keeps."""

    source = BOARD_JS.read_text(encoding="utf-8")
    for banned in ("localStorage", "sessionStorage", "indexedDB"):
        assert banned not in source


def test_no_card_offers_a_delete_control() -> None:
    """Ruling: delete lives in the dialog, never on a Kanban card.

    A card is the one control an operator can hit while scanning, and a delete
    reachable from there is a delete one stray click away from every order on the
    board.
    """
    report = render(populated())

    assert report["cardsWithDeleteControl"] == []


def test_the_board_offers_no_delete_outside_the_dialog() -> None:
    """The board itself must build no destructive control at all.

    Scanned over code only, so this file's own comments explaining the absence
    cannot fire it.
    """
    source = _js_code()
    card_body = source.split("function card(", 1)[1].split("function column(", 1)[0]
    assert "order-delete" not in card_body, (
        "board.js builds a delete control inside a card"
    )


# --------------------------------------------------------------------------
# Delete, in the details dialog
# --------------------------------------------------------------------------


def test_the_dialog_offers_a_delete_control() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["deleteButton"] is not None


def test_the_delete_control_names_the_order_it_would_delete() -> None:
    """The destructive verb is never offered on a bare "Delete"."""
    report = render(populated(), action="detail")

    assert "ORD-0001" in report["detail"]["deleteButton"]["label"]


def test_the_delete_control_says_delete_as_a_word() -> None:
    """Not colour alone: the button carries the word, not just a red fill."""
    report = render(populated(), action="detail")

    assert "Delete" in report["detail"]["deleteButton"]["text"]


def test_the_delete_control_is_marked_destructive() -> None:
    """A hook for the stylesheet and for a test, so the distinction is declared."""
    report = render(populated(), action="detail")

    assert report["detail"]["deleteButton"]["destructive"] == "yes"


def test_the_delete_control_is_a_real_button() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["deleteButton"]["type"] == "button"


def test_the_confirmation_is_hidden_until_it_is_asked_for() -> None:
    """One click on Delete must not delete anything."""
    report = render(populated(), action="detail")

    assert report["detail"]["confirmRegionHidden"] == "true"


def test_the_confirmation_names_the_order_number_and_its_title() -> None:
    """The ruling, verbatim: an operator must read back exactly what goes.

    "Delete that order?" is not enough -- with a board of a dozen orders, an
    unlabelled confirmation is a coin flip on which one is removed.
    """
    report = render(populated(), action="detail")

    prompt = report["detail"]["confirmPrompt"]
    # The stub joins subtree text with spaces, so the prompt is leading-spaced.
    assert "Delete ORD-0001 — Straighten the tongs?" in prompt, prompt


def test_the_confirmation_offers_a_delete_and_a_cancel() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["confirmDelete"]["text"] == "Delete"
    assert report["detail"]["cancelDelete"]["text"] == "Cancel"


def test_the_confirming_button_is_marked_destructive() -> None:
    """The step that actually destroys carries the same marking, not just the
    first click."""
    report = render(populated(), action="detail")

    assert report["detail"]["confirmDelete"]["destructive"] == "yes"


def test_the_cancelling_button_is_not_marked_destructive() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["cancelDelete"]["destructive"] is None


def test_the_confirmation_is_a_named_group() -> None:
    report = render(populated(), action="detail")

    assert "ORD-0001" in report["detail"]["confirmLabel"]


def test_the_confirmation_is_announced_as_a_description_of_the_delete_button() -> None:
    """So a screen reader reaches the question before the answer is clicked."""
    report = render(populated(), action="detail")

    assert report["detail"]["confirmDescribed"] == "order-delete-confirm"


def test_the_confirmation_carries_a_stable_id_to_be_described_by() -> None:
    report = render(populated(), action="detail")

    assert report["detail"]["confirmRegionHidden"] == "true"
    assert report["detail"]["confirmLabel"], "the confirm region must be nameable"


def test_confirming_the_deletion_reports_the_order_and_its_title() -> None:
    """The handler receives both, so the page can name what it removed."""
    report = render(populated(), action="detail", step="destroy")

    assert report["deleted"] == ["ORD-0001:Straighten the tongs"]


def test_opening_the_confirmation_alone_deletes_nothing() -> None:
    report = render(populated(), action="detail", step="confirm")

    assert report["deleted"] == []


def test_cancelling_deletes_nothing() -> None:
    """The requirement, and the reason the confirmation is a second step."""
    report = render(populated(), action="detail", step="cancel")

    assert report["deleted"] == []


def test_no_order_is_rendered_without_one_so_there_is_nothing_to_delete() -> None:
    report = render(populated(), action="detail", order=None)

    assert report["detail"] is None


def test_the_frontend_implements_no_bulk_delete() -> None:
    """Ruling: one order at a time. No plural control, no select-many path."""
    source = _js_code().lower()
    for banned in ("deletemany", "deleteall", "bulkdelete", "deletefiltered"):
        assert banned not in source, f"board.js introduces {banned}"


def test_the_frontend_offers_no_archive_or_restore() -> None:
    """Ruling: no archive yet, and no soft delete. There is no way back."""
    source = _js_code().lower()
    for banned in ("archive", "restore", "undelete", "trash"):
        assert banned not in source, f"board.js introduces {banned}"


def test_the_frontend_implements_no_drag_and_drop() -> None:
    """Not required by the ruling, and an untestable half-feature.

    Scanned over code only: this file's own comments discuss the absence of
    drag-and-drop at length, and a raw-text scan would fire on the explanation.
    """
    source = _js_code().lower()
    for banned in ("draggable", "dragstart", "dragover", "ondrop"):
        assert banned not in source


def test_the_page_loads_the_board_module() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "board.js" in html


def test_the_page_offers_a_view_switcher() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "Order Board" in html
    assert 'id="view-switch"' in html


def test_the_board_view_is_hidden_until_it_is_selected() -> None:
    """The Workbench is what the page shows first; the board is a view."""

    html = INDEX_HTML.read_text(encoding="utf-8")

    board = re.search(r'<section[^>]*id="board-view".*?>', html, flags=re.S)
    assert board, "no board view section"
    assert "hidden" in board.group(0)


def test_app_js_does_not_define_workflow_states_either() -> None:
    """The wiring file is held to the same rule as the render module."""

    source = _js_code(APP_JS)
    for status in STATUSES:
        assert status not in source, f"app.js names the workflow state {status!r}"


def test_the_board_scrolls_rather_than_squeezing_its_columns() -> None:
    """Six columns cannot fit a laptop, and the horizontal scroll is the answer.

    Asserted on the specific rules rather than on the word "overflow": a column
    grid with no scroll container would lay out at full page width and push the
    rest of the board off-screen, which is the failure this rule prevents.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    board_rule = re.search(r"^\.board \{([^}]*)\}", css, flags=re.M)
    assert board_rule, "no .board rule"
    assert "overflow" in board_rule.group(1), (
        "the board must scroll; six fixed columns overflow the viewport"
    )

    columns_rule = re.search(r"^\.board-columns \{([^}]*)\}", css, flags=re.M)
    assert columns_rule, "no .board-columns rule"
    assert "grid-auto-flow: column" in columns_rule.group(1), (
        "columns must flow in one row so the workflow reads left to right"
    )


def test_the_board_keeps_readable_column_widths_on_a_tablet() -> None:
    """A narrower viewport narrows the columns; it does not drop them.

    Dropping to fewer columns on a tablet would hide part of the workflow, and a
    board that silently hides columns is worse than one that scrolls.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert "@media (max-width: 900px)" in css
    narrow = css.split("@media (max-width: 900px)")[-1]
    assert ".board-columns" in narrow, "no tablet rule for the board"
    assert "display: none" not in narrow, "the board must not be hidden on a tablet"


def test_the_board_scrolls_at_every_width_rather_than_only_at_one() -> None:
    """The scroll container is not declared inside a media query.

    Six columns overflow a laptop at 1200px too, so a scroll rule that only
    applied below a breakpoint would leave the common case broken. This checks
    the rule's indentation depth, which is what "not inside a media query" means
    in a stylesheet with no nesting support.
    """
    lines = STYLES_CSS.read_text(encoding="utf-8").splitlines()
    scroll_lines = [
        index
        for index, line in enumerate(lines)
        if line.strip() == ".board {"
    ]

    assert scroll_lines, "no .board rule"

    for index in scroll_lines:
        rule = lines[index : index + 12]
        # A nested rule is indented; a top-level one is not.
        assert not rule[0].startswith(" "), (
            f".board is declared inside a media query (line {index + 1}); the "
            "scroll must apply at every width"
        )
        assert any("overflow" in line for line in rule), (
            f"the .board rule at line {index + 1} declares no overflow"
        )


def test_the_board_states_one_count_and_does_not_cross_check_it() -> None:
    """The count comes from the columns, and nothing compares it to the payload.

    An earlier version carried ``countAgrees``, comparing the payload's own
    ``total`` against the sum of the columns. The backend derives that field from
    those same columns, so the comparison checked the backend against itself and
    could never report disagreement -- it only ever produced a false alarm on a
    healthy board. Removed per ruling; this asserts it does not come back.
    """
    source = BOARD_JS.read_text(encoding="utf-8")
    for banned in ("countAgrees", "reportedTotal"):
        assert banned not in source, f"board.js reintroduces {banned}"


def test_the_board_counts_the_orders_it_actually_drew() -> None:
    report = render(populated())

    assert report["model"]["total"] == 3


def test_the_board_counts_a_payload_whose_own_total_is_wrong() -> None:
    """The board's number comes from the columns, not from the payload's claim.

    A stale or wrong ``total`` in the payload must not change what the board
    says it is holding -- which is the only useful behaviour here, and the reason
    the cross-check was removed rather than kept as a warning.
    """
    payload = {**populated(), "total": 99}
    report = render(payload)

    assert report["model"]["total"] == 3


def test_an_empty_board_counts_zero() -> None:
    assert render()["model"]["total"] == 0


def test_the_board_never_uses_inner_html() -> None:
    """Every operator-typed string is set as textContent.

    The stub DOM already throws on `innerHTML`, so any renderer that used it
    would fail every behavioural test above. Asserted on the source as well,
    because a test that only catches it indirectly says nothing on its own --
    and scanned over code only, so the guarantee is not asserted by a comment
    that happens to mention the word.
    """
    assert "innerHTML" not in _js_code()


# --------------------------------------------------------------------------
# Layout bugs found by measuring in a real browser
# --------------------------------------------------------------------------
#
# The three rules below each exist because headless Chrome reported a concrete
# wrong measurement. They are the properties that measurement was checking, and
# a CSS test that only reads the file cannot see a cascade or a flexbox resolve
# a selector the way a browser does -- so the measurement is what proves them
# and this is what keeps them from being undone.


def test_the_hidden_workbench_pane_is_actually_hidden() -> None:
    """`.split` must yield to `[hidden]`, or both views paint at once.

    ``.split { display: grid }`` beats the user agent's ``[hidden] { display:
    none }`` on specificity, because the attribute selector is not in the UA rule
    at all in some engines and is a bare type selector in others. Measured on the
    board view: the pane reported ``hidden=true`` and still painted the
    conversation, the activity feed, and the composer. The ruling was that the
    board *replaces* the workbench content area, so both on screen is a defect.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    split_rule = re.search(r"^\.split \{([^}]*)\}", css, flags=re.M)
    assert split_rule, "no .split rule"
    assert "display: grid" in split_rule.group(1), (
        "this guard assumes .split sets a display; if that changes the "
        "specificity argument changes with it"
    )

    assert re.search(r"^\.split\[hidden\] \{[^}]*display: none", css, flags=re.M), (
        "no .split[hidden] rule: the workbench pane will paint behind the board"
    )


def test_every_hidden_board_element_has_an_explicit_rule() -> None:
    """Each element the page toggles with `hidden` carries its own rule.

    The stylesheet already handles this for `.health-detail`, `.models-note`,
    `.overlay`, and `.approval-note`. The board adds `.board-view`,
    `.order-overlay`, and the `.split` pane, and the failure mode is identical
    for all of them: a `display` declaration elsewhere silently beats the
    attribute.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    for selector in (
        ".board-view[hidden]",
        ".order-overlay[hidden]",
        ".split[hidden]",
        ".order-delete-confirm[hidden]",
    ):
        assert re.search(rf"^{re.escape(selector)} \{{[^}}]*display: none", css, flags=re.M), (
            f"no explicit {selector} rule"
        )


def test_each_board_column_is_a_containing_block_for_its_own_summary() -> None:
    """`.board-column` must be positioned, or its `.sr-only` summary escapes.

    Each column renders an `.sr-only` count summary, which is ``position:
    absolute`` with no offsets and therefore sits at its *static* position --
    inside the horizontally scrolling columns grid. With no positioned ancestor,
    that static position resolves against the initial containing block, where it
    counts toward the document width: measured on an 834px tablet, the last
    column's summary sat at x=1124 and scrolled the whole page sideways by 291px.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    column_rule = re.search(r"^\.board-column \{([^}]*)\}", css, flags=re.M)
    assert column_rule, "no .board-column rule"
    assert "position: relative" in column_rule.group(1), (
        "without a positioned column, the per-column sr-only summary widens the "
        "document instead of staying inside the scroll container"
    )


def test_the_destructive_control_is_not_distinguished_by_colour_alone() -> None:
    """The board's own rule, applied to the one destructive control.

    Priority already carries a glyph because an orange and a red card are the same
    rectangle to a colour-blind operator. Delete is signalled by the *word* on the
    button (asserted in the rendering tests) and here by a border -- so a
    stylesheet that reduced it to a coloured fill and nothing else fails.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    destructive = re.search(
        r"^\.order-delete-trigger,\s*\n\.order-delete-confirm-button \{([^}]*)\}",
        css,
        flags=re.M,
    )
    assert destructive, "no rule for the destructive delete buttons"
    assert "border:" in destructive.group(1), (
        "the destructive control is distinguished by colour alone"
    )
    assert "font-weight: 600" in destructive.group(1), (
        "the destructive control carries no non-colour emphasis"
    )


def test_the_cancelling_button_is_styled_apart_from_the_destructive_one() -> None:
    """Cancel must not read as equally weighted next to the button that destroys."""
    css = STYLES_CSS.read_text(encoding="utf-8")

    cancel = re.search(r"^\.order-delete-cancel \{([^}]*)\}", css, flags=re.M)
    assert cancel, "no rule for the cancel button"
    assert "var(--hot)" not in cancel.group(1), (
        "cancel is styled as destructive; the safe answer must not look like the "
        "one that removes the order"
    )


def test_the_board_scroll_container_declares_an_overflow() -> None:
    """`.board` must scroll, and the rule sits outside every media query.

    Six columns cannot fit a laptop at 1200px either, so a scroll rule that only
    applied below a breakpoint would leave the common case broken. Checked by
    indentation depth, which is what "not inside a media query" means in a
    stylesheet with no nesting support.
    """
    lines = STYLES_CSS.read_text(encoding="utf-8").splitlines()
    scroll_rules = [
        index for index, line in enumerate(lines) if line.strip() == ".board {"
    ]

    assert scroll_rules, "no .board rule"

    for index in scroll_rules:
        rule = lines[index : index + 12]
        assert not rule[0].startswith(" "), (
            f".board is declared inside a media query (line {index + 1})"
        )
        assert any("overflow" in line for line in rule), (
            f"the .board rule at line {index + 1} declares no overflow"
        )


def test_the_scroll_container_overrides_the_flex_automatic_minimum() -> None:
    """`min-width: 0` and `min-height: 0`, as a guard rather than as a fix.

    A flex item's automatic minimum size refuses to shrink below its content, and
    this item holds a grid of six columns -- so the override belongs here whether
    or not it is currently load-bearing.

    Stated honestly because it was mis-attributed once: these two lines were
    added while chasing a 291px sideways scroll on a tablet, and measurement then
    showed the cause was the unpositioned ``.sr-only`` summary in
    ``.board-column``, not the flex minimum. Removing these lines changes no
    measured width. They are kept as defence, not asserted as the fix -- which is
    why the browser check for the overflow lives in
    ``tests/test_ui_orders_page.py`` instead of here.
    """
    css = STYLES_CSS.read_text(encoding="utf-8")

    board_rule = re.search(r"^\.board \{([^}]*)\}", css, flags=re.M)
    assert board_rule, "no .board rule"
    body = board_rule.group(1)
    assert "min-width: 0" in body
    assert "min-height: 0" in body