"""The Order Board store: ids, edits, movement, and the file it leaves behind.

These drive :class:`forge.ui.orders.OrderStore` against a real directory. No
mock, no in-memory fake: the properties that matter here -- atomic replacement,
surviving a restart, refusing a hand-edited file it cannot understand -- are
properties of the *file*, and a fake filesystem would test the fake.

The tests worth reading first:

*   :func:`test_the_next_id_never_reuses_a_number_after_a_restart` -- the
    ruling that a restart must not reset numbering, checked by constructing a
    fresh store over the same path.
*   :func:`test_a_hand_edited_counter_behind_its_orders_is_corrected_upward` --
    the conservative reconciliation, from the other direction.
*   :func:`test_a_malformed_board_fails_visibly_rather_than_reading_as_empty`
    -- the ruling that a corrupt file is an error, not an empty board.
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import pathlib
import sys
import threading

import pytest

import forge.ui.orders
from forge.ui.orders import (
    DEFAULT_PRIORITY,
    PRIORITIES,
    STATUSES,
    InvalidOrder,
    Order,
    OrderBoard,
    OrderStore,
    OrderStoreError,
    UnknownOrder,
    UnknownStatus,
    _number_of,
    format_order_id,
    orders_path,
)

#: Top-level module names a stdlib-only module may import. Built from
#: ``sys.stdlib_module_names`` rather than hand-listed, so it stays correct on
#: the interpreter the suite runs on rather than on the one it was written on.
_STDLIB_ROOTS = set(sys.stdlib_module_names) | {"forge"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def store(workspace: pathlib.Path) -> OrderStore:
    return OrderStore(workspace)


def write_board(workspace: pathlib.Path, document: object) -> pathlib.Path:
    """Put a hand-written document where the board belongs."""
    path = orders_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def document_of(store: OrderStore) -> dict:
    return json.loads(store.path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_a_new_order_is_created(store: OrderStore) -> None:
    order = store.create(title="Straighten the tongs", smith="Byte")

    assert order.order_id == "ORD-0001"
    assert order.title == "Straighten the tongs"
    assert order.smith == "Byte"
    assert order.status == STATUSES[0]
    assert order.priority == DEFAULT_PRIORITY


def test_a_created_order_is_in_the_first_workflow_state(store: OrderStore) -> None:
    assert store.create(title="Blade").status == "orders_received"


def test_a_created_order_records_when_it_arrived(store: OrderStore) -> None:
    order = store.create(title="Blade")

    # Second resolution, UTC, and therefore ending in +00:00. Asserted by shape
    # rather than by parsing so the test cannot pass on a timestamp that is
    # merely a string.
    assert order.created_at.endswith("+00:00")
    assert len(order.created_at) == 25  # 2026-10-04T02:00:00+00:00


def test_a_title_is_required(store: OrderStore) -> None:
    with pytest.raises(InvalidOrder):
        store.create(title="   ")


def test_a_title_must_be_text_not_a_number(store: OrderStore) -> None:
    with pytest.raises(InvalidOrder):
        store.create(title=42)  # type: ignore[arg-type]


def test_an_absent_description_is_empty_rather_than_missing(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert order.description == ""
    assert order.smith == ""


def test_an_unknown_priority_is_refused(store: OrderStore) -> None:
    with pytest.raises(InvalidOrder):
        store.create(title="Blade", priority="whenever")


def test_an_unknown_status_is_refused(store: OrderStore) -> None:
    with pytest.raises(UnknownStatus):
        store.create(title="Blade", status="on_the_anvil")


def test_every_workflow_state_is_accepted_by_create(store: OrderStore) -> None:
    for status in STATUSES:
        assert store.create(title=f"Blade {status}", status=status).status == status


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_a_created_order_survives_a_fresh_store_over_the_same_path(
    workspace: pathlib.Path,
) -> None:
    OrderStore(workspace).create(title="Temper the blade")

    # A new process: a new store object over the same file, nothing carried over.
    reloaded = OrderStore(workspace)
    assert [order.title for order in reloaded.board().orders] == ["Temper the blade"]


def test_the_board_lives_at_dot_forge_orders_json_in_the_workspace(
    workspace: pathlib.Path,
) -> None:
    store = OrderStore(workspace)
    store.create(title="Blade")

    assert store.path == workspace / ".forge" / "orders.json"
    assert store.path.exists()


def test_a_missing_file_is_an_empty_board_not_an_error(workspace: pathlib.Path) -> None:
    board = OrderStore(workspace).board()

    assert board.orders == ()
    assert board.next_id == 1


def test_the_stored_document_names_the_schema_version(store: OrderStore) -> None:
    store.create(title="Blade")

    assert document_of(store)["version"] == 1


def test_the_stored_document_is_human_readable(store: OrderStore) -> None:
    """A person must be able to open this file and understand it.

    Ruling 1's reason for a JSON file over a database. Asserted as indentation
    rather than as a claim in a docstring.
    """
    store.create(title="Blade", smith="Byte")

    text = store.path.read_text(encoding="utf-8")
    assert '\n  "orders"' in text
    assert json.loads(text)["orders"][0]["smith"] == "Byte"


def test_a_write_leaves_no_temporary_file_behind(store: OrderStore) -> None:
    store.create(title="Blade")

    leftovers = sorted(p.name for p in store.path.parent.iterdir())
    assert leftovers == ["orders.json"]


# --------------------------------------------------------------------------
# Order ids
# --------------------------------------------------------------------------


def test_ids_increase_across_creates(store: OrderStore) -> None:
    ids = [store.create(title=f"Blade {n}").order_id for n in range(3)]

    assert ids == ["ORD-0001", "ORD-0002", "ORD-0003"]


def test_ids_are_unique(store: OrderStore) -> None:
    ids = [store.create(title=f"Blade {n}").order_id for n in range(5)]

    assert len(set(ids)) == len(ids)


def test_ids_are_stable_across_edits_and_moves(store: OrderStore) -> None:
    order = store.create(title="Blade")

    store.edit(order.order_id, title="Blade, resharpened")
    store.move(order.order_id, "at_the_forge")

    assert store.get(order.order_id).order_id == "ORD-0001"


def test_ids_are_zero_padded_to_a_fixed_width() -> None:
    assert format_order_id(7) == "ORD-0007"
    assert format_order_id(1234) == "ORD-1234"


def test_an_id_wider_than_the_padding_is_not_truncated() -> None:
    """Id 10001 is ``ORD-10001``, not squeezed back to four digits.

    Truncating would mint a number that already belongs to another order, which
    is the one thing the sequence exists to prevent.
    """

    assert format_order_id(10001) == "ORD-10001"


def test_wide_ids_still_sort_in_numeric_order() -> None:
    """Zero-padding is only worth anything if ordering survives past it.

    Checked as parsed sequence numbers rather than as string comparison, because
    the string comparison stops agreeing with the numeric one exactly where the
    padding runs out -- which is why the width is a minimum, not a maximum.
    """

    ids = [format_order_id(n) for n in (9999, 10001, 12345)]
    numbers = [_number_of(order_id) for order_id in ids]

    assert numbers == [9999, 10001, 12345]


def test_the_next_id_never_reuses_a_number_after_a_restart(
    workspace: pathlib.Path,
) -> None:
    """The ruling, from the direction that matters.

    Three orders created, then a brand-new store over the same path. The fourth
    must not be ORD-0001 again, which is what a counter held only in memory or
    derived from a length would produce after a restart.
    """
    first = OrderStore(workspace)
    for n in range(3):
        first.create(title=f"Blade {n}")

    second = OrderStore(workspace)
    assert second.create(title="Blade 3").order_id == "ORD-0004"


def test_the_next_id_ignores_a_deleted_order_rather_than_reusing_it(
    workspace: pathlib.Path,
) -> None:
    """Deleting the last order must not make the next one its number.

    There is no delete feature in the MVP, but the file is hand-editable, and
    the counter has to be safe against an operator doing exactly this.
    """
    store = OrderStore(workspace)
    store.create(title="One")
    store.create(title="Two")

    document = document_of(store)
    document["orders"] = document["orders"][:1]
    store.path.write_text(json.dumps(document), encoding="utf-8")

    assert store.create(title="Three").order_id == "ORD-0003"


def test_a_hand_edited_counter_behind_its_orders_is_corrected_upward(
    workspace: pathlib.Path,
) -> None:
    """The conservative reconciliation, in the dangerous direction.

    An operator adds ORD-0042 by hand and does not bump ``next_id``. Minting
    ORD-0002 here would create a second order with a number already in use, so
    the next id must jump past 42.
    """
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0002",
            "orders": [
                {
                    "order_id": "ORD-0001",
                    "title": "Blade",
                    "status": "orders_received",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "2026-10-04T02:00:00+00:00",
                },
                {
                    "order_id": "ORD-0042",
                    "title": "Added by hand",
                    "status": "at_the_forge",
                    "priority": "high",
                    "smith": "Byte",
                    "description": "",
                    "created_at": "2026-10-04T02:05:00+00:00",
                },
            ],
        },
    )

    assert OrderStore(workspace).create(title="Next").order_id == "ORD-0043"


def test_a_hand_edited_counter_ahead_of_its_orders_is_respected(
    workspace: pathlib.Path,
) -> None:
    """The other direction: a bumped counter must not walk backwards.

    An operator deletes orders by hand, leaving the counter high. Numbering
    should continue from where the file says, not restart from the survivors --
    otherwise a human who saw ORD-0007 in a conversation sees it again.
    """
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0500",
            "orders": [
                {
                    "order_id": "ORD-0001",
                    "title": "Blade",
                    "status": "orders_received",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "2026-10-04T02:00:00+00:00",
                }
            ],
        },
    )

    assert OrderStore(workspace).create(title="Next").order_id == "ORD-0500"


def test_a_hand_edited_id_without_padding_is_still_understood(
    workspace: pathlib.Path,
) -> None:
    """An operator's ``ORD-7`` must count toward the sequence.

    Refusing to read it would make the conservative rule impossible: an id Forge
    could not parse is an id Forge could not protect from being reused.
    """
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0001",
            "orders": [
                {
                    "order_id": "ORD-7",
                    "title": "Blade",
                    "status": "orders_received",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "2026-10-04T02:00:00+00:00",
                }
            ],
        },
    )

    assert OrderStore(workspace).create(title="Next").order_id == "ORD-0008"


def test_an_id_naming_no_number_does_not_advance_the_counter(
    workspace: pathlib.Path,
) -> None:
    """An unparseable id yields nothing, so the counter stays where it is.

    The conservative direction: the worst outcome is a gap in the sequence, never
    a collision. Refusing the whole file over one bad string would make a board
    unopenable for a reason the operator cannot see a remedy for in an error
    message.
    """
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0003",
            "orders": [
                {
                    "order_id": "mystery",
                    "title": "Blade",
                    "status": "orders_received",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "2026-10-04T02:00:00+00:00",
                }
            ],
        },
    )

    assert OrderStore(workspace).create(title="Next").order_id == "ORD-0003"


def test_an_order_id_that_is_not_a_string_is_refused(
    workspace: pathlib.Path,
) -> None:
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0002",
            "orders": [
                {
                    "order_id": 17,
                    "title": "Blade",
                    "status": "orders_received",
                    "priority": "normal",
                    "smith": "",
                    "description": "",
                    "created_at": "",
                }
            ],
        },
    )

    with pytest.raises(OrderStoreError):
        OrderStore(workspace).board()


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------


def test_an_edit_changes_the_title(store: OrderStore) -> None:
    order = store.create(title="Straighten the tongs")

    assert store.edit(order.order_id, title="Straighten and re-temper").title == (
        "Straighten and re-temper"
    )


def test_an_edit_changes_the_priority(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert store.edit(order.order_id, priority="urgent").priority == "urgent"


def test_an_edit_changes_the_smith(store: OrderStore) -> None:
    order = store.create(title="Blade", smith="Byte")

    assert store.edit(order.order_id, smith="Forge").smith == "Forge"


def test_an_edit_changes_the_description(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert store.edit(order.order_id, description="Flatten to 6mm").description == (
        "Flatten to 6mm"
    )


def test_an_edit_changes_several_fields_at_once(store: OrderStore) -> None:
    order = store.create(title="Blade", description="first")

    edited = store.edit(
        order.order_id,
        title="Blade 2",
        priority="high",
        smith="Forge",
        description="second",
    )

    assert (edited.title, edited.priority, edited.smith, edited.description) == (
        "Blade 2",
        "high",
        "Forge",
        "second",
    )


def test_an_edit_leaves_the_fields_it_was_not_given_alone(store: OrderStore) -> None:
    order = store.create(title="Blade", smith="Byte", description="Flatten")

    edited = store.edit(order.order_id, priority="high")

    assert edited.title == "Blade"
    assert edited.smith == "Byte"
    assert edited.description == "Flatten"


def test_an_edit_can_clear_the_smith_explicitly(store: OrderStore) -> None:
    order = store.create(title="Blade", smith="Byte")

    assert store.edit(order.order_id, smith="").smith == ""


def test_an_edit_does_not_change_the_creation_time(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert store.edit(order.order_id, title="Blade 2").created_at == order.created_at


def test_an_edit_cannot_move_an_order(store: OrderStore) -> None:
    """Ruling 2, asserted on the signature rather than in a comment.

    ``status`` is not a keyword this method accepts, so there is no call that
    can smuggle a move in through an edit.
    """
    order = store.create(title="Blade")

    with pytest.raises(TypeError):
        store.edit(order.order_id, status="at_the_forge")  # type: ignore[call-arg]


def test_an_edit_cannot_change_the_order_id(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(TypeError):
        store.edit(order.order_id, order_id="ORD-9999")  # type: ignore[call-arg]


def test_an_edit_requires_at_least_one_field(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(InvalidOrder):
        store.edit(order.order_id)


def test_an_edit_refuses_an_empty_title(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(InvalidOrder):
        store.edit(order.order_id, title="")


def test_an_edit_refuses_an_unknown_priority(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(InvalidOrder):
        store.edit(order.order_id, priority="asap")


def test_an_edit_of_an_unknown_order_is_reported(store: OrderStore) -> None:
    with pytest.raises(UnknownOrder):
        store.edit("ORD-0404", title="Blade")


def test_a_refused_edit_leaves_the_stored_order_untouched(store: OrderStore) -> None:
    order = store.create(title="Blade", priority="low")

    with pytest.raises(InvalidOrder):
        store.edit(order.order_id, priority="asap")

    assert store.get(order.order_id).priority == "low"


def test_an_edited_order_survives_a_reload(workspace: pathlib.Path) -> None:
    store = OrderStore(workspace)
    order = store.create(title="Blade")

    OrderStore(workspace).edit(order.order_id, title="Blade 2", smith="Forge")

    reloaded = OrderStore(workspace).get(order.order_id)
    assert (reloaded.title, reloaded.smith) == ("Blade 2", "Forge")


# --------------------------------------------------------------------------
# Move
# --------------------------------------------------------------------------


def test_an_order_moves_into_a_state(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert store.move(order.order_id, "on_the_rack").status == "on_the_rack"


@pytest.mark.parametrize("target", STATUSES)
def test_an_order_can_be_moved_into_every_workflow_state(
    store: OrderStore, target: str
) -> None:
    order = store.create(title="Blade")

    assert store.move(order.order_id, target).status == target


def test_an_order_can_be_moved_between_any_two_states(store: OrderStore) -> None:
    """Every ordered pair, not just the next column.

    The workflow has no transition rules, so "from here to there" is the
    property that matters -- a board where a card could only advance one step
    would need rules it does not have.
    """
    for origin in STATUSES:
        for target in STATUSES:
            order = store.create(title=f"{origin}->{target}", status=origin)
            assert store.move(order.order_id, target).status == target


def test_moving_an_order_out_of_one_state_puts_it_in_no_other(
    store: OrderStore,
) -> None:
    order = store.create(title="Blade")

    store.move(order.order_id, "in_the_quench")
    columns = {column["status"]: column for column in store.board().columns()}

    assert columns["in_the_quench"]["count"] == 1
    assert columns["orders_received"]["count"] == 0


def test_a_move_to_the_orders_own_state_succeeds_and_changes_nothing(
    store: OrderStore,
) -> None:
    order = store.create(title="Blade", smith="Byte", priority="high")

    moved = store.move(order.order_id, "orders_received")

    assert moved.status == "orders_received"
    assert moved.smith == "Byte"
    assert moved.priority == "high"


def test_a_move_preserves_the_orders_editable_fields(store: OrderStore) -> None:
    order = store.create(title="Blade", smith="Byte", description="Flatten")

    moved = store.move(order.order_id, "finished_goods")

    assert (moved.title, moved.smith, moved.description) == (
        "Blade",
        "Byte",
        "Flatten",
    )


def test_a_move_preserves_the_creation_time(store: OrderStore) -> None:
    order = store.create(title="Blade")

    assert store.move(order.order_id, "at_the_forge").created_at == order.created_at


def test_a_move_to_an_unknown_state_is_refused(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(UnknownStatus):
        store.move(order.order_id, "ready_to_ship")


def test_a_move_to_the_status_it_already_has_is_not_an_unknown_state(
    store: OrderStore,
) -> None:
    """The two look identical to a naive check, and only one is an error."""

    order = store.create(title="Blade")

    assert store.move(order.order_id, order.status).status == order.status


def test_a_move_of_an_unknown_order_is_reported(store: OrderStore) -> None:
    with pytest.raises(UnknownOrder):
        store.move("ORD-0404", "at_the_forge")


def test_a_refused_move_leaves_the_stored_order_untouched(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(UnknownStatus):
        store.move(order.order_id, "ready_to_ship")

    assert store.get(order.order_id).status == "orders_received"


def test_a_moved_order_survives_a_reload(workspace: pathlib.Path) -> None:
    store = OrderStore(workspace)
    order = store.create(title="Blade")

    OrderStore(workspace).move(order.order_id, "at_the_forge")

    assert OrderStore(workspace).get(order.order_id).status == "at_the_forge"


def test_a_move_that_crosses_a_restart_keeps_the_sequence(
    workspace: pathlib.Path,
) -> None:
    """State and numbering are both durable, and they survive together."""

    store = OrderStore(workspace)
    order = store.create(title="Blade")

    OrderStore(workspace).move(order.order_id, "finished_goods")
    second = OrderStore(workspace)

    assert second.get(order.order_id).status == "finished_goods"
    assert second.create(title="Next").order_id == "ORD-0002"


# --------------------------------------------------------------------------
# Grouping and the payload
# --------------------------------------------------------------------------


def test_the_board_reports_every_workflow_state_even_when_empty(
    store: OrderStore,
) -> None:
    """Every column is always present, so the empty state has somewhere to be."""

    columns = store.board().columns()

    assert [column["status"] for column in columns] == list(STATUSES)


def test_orders_are_grouped_by_their_status(store: OrderStore) -> None:
    store.create(title="One", status="orders_received")
    store.create(title="Two", status="at_the_forge")
    store.create(title="Three", status="finished_goods")

    columns = {c["status"]: c for c in store.board().columns()}

    assert columns["orders_received"]["count"] == 1
    assert [o["title"] for o in columns["at_the_forge"]["orders"]] == ["Two"]
    assert columns["finished_goods"]["count"] == 1


def test_the_columns_are_in_workflow_order(store: OrderStore) -> None:
    """Order is part of the contract: the board reads left to right as a shop."""

    assert [c["status"] for c in store.board().columns()] == list(STATUSES)


def test_a_column_counts_the_orders_it_holds(store: OrderStore) -> None:
    store.create(title="One")
    store.create(title="Two")

    columns = {c["status"]: c for c in store.board().columns()}

    assert columns["orders_received"]["count"] == 2


def test_the_orders_in_a_column_are_oldest_first(store: OrderStore) -> None:
    for title in ("first", "second", "third"):
        store.create(title=title)

    orders = store.board().columns()[0]["orders"]

    assert [o["title"] for o in orders] == ["first", "second", "third"]


def test_older_orders_come_before_newer_ones_after_a_move(store: OrderStore) -> None:
    first = store.create(title="first")
    store.create(title="second")

    store.move(first.order_id, "at_the_forge")
    store.move(first.order_id, "orders_received")

    orders = store.board().columns()[0]["orders"]

    assert [o["title"] for o in orders] == ["first", "second"]


def test_the_payload_publishes_the_status_vocabulary(store: OrderStore) -> None:
    """So the frontend can be held to consuming it rather than reproducing it."""

    payload = store.board().as_payload()

    assert payload["statuses"] == list(STATUSES)
    assert payload["priorities"] == list(PRIORITIES)


def test_the_payload_reports_the_next_id_for_an_operator_to_see(store: OrderStore) -> None:
    store.create(title="One")

    assert store.board().as_payload()["next_id"] == "ORD-0002"


def test_the_payload_counts_the_orders_on_the_board(store: OrderStore) -> None:
    store.create(title="One")
    store.create(title="Two")

    assert store.board().as_payload()["total"] == 2


def test_an_order_carries_its_sequence_for_display(store: OrderStore) -> None:
    order = store.create(title="One")

    assert order.as_dict()["sequence"] == 1


def test_an_order_carries_its_own_status_label(store: OrderStore) -> None:
    order = store.create(title="One")

    assert order.as_dict()["status_label"] == "Orders Received"


def test_a_board_reports_no_columns_holding_an_order_twice(store: OrderStore) -> None:
    order = store.create(title="One", status="orders_received")

    store.move(order.order_id, "at_the_forge")

    columns = store.board().columns()
    assert sum(column["count"] for column in columns) == store.board().as_payload()[
        "total"
    ]


def test_a_board_is_a_value_not_a_live_view(store: OrderStore) -> None:
    """Two reads, no write between them, identical content.

    The board is re-read from the file per call rather than cached, which is what
    makes a hand-edited change appear without a restart.
    """
    store.create(title="One")

    assert store.board().orders == store.board().orders


# --------------------------------------------------------------------------
# A malformed file fails visibly
# --------------------------------------------------------------------------


def test_a_malformed_board_fails_visibly_rather_than_reading_as_empty(
    store: OrderStore,
) -> None:
    store.create(title="Blade")
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OrderStoreError):
        store.board()


def test_a_malformed_file_names_the_path_and_leaves_the_board_alone(
    store: OrderStore,
) -> None:
    store.create(title="Blade")
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OrderStoreError) as caught:
        store.board()

    assert str(store.path) in str(caught.value)
    assert store.path.read_text(encoding="utf-8") == "{not json", (
        "the file must not be rewritten over a parse failure"
    )


def test_a_file_that_is_not_a_json_object_is_refused(store: OrderStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]", encoding="utf-8")

    with pytest.raises(OrderStoreError):
        store.board()


def test_a_file_whose_orders_is_not_a_list_is_refused(store: OrderStore) -> None:
    write_board(store.path.parent.parent, {"version": 1, "orders": {"a": 1}})

    with pytest.raises(OrderStoreError):
        OrderStore(store.path.parent.parent).board()


def test_an_order_with_a_missing_title_is_refused(workspace: pathlib.Path) -> None:
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0002",
            "orders": [{"order_id": "ORD-0001", "status": "orders_received"}],
        },
    )

    with pytest.raises(OrderStoreError):
        OrderStore(workspace).board()


def test_an_order_with_an_unknown_status_is_refused(workspace: pathlib.Path) -> None:
    write_board(
        workspace,
        {
            "version": 1,
            "next_id": "ORD-0002",
            "orders": [
                {
                    "order_id": "ORD-0001",
                    "title": "Blade",
                    "status": "on_the_anvil",
                    "priority": "normal",
                }
            ],
        },
    )

    with pytest.raises(OrderStoreError):
        OrderStore(workspace).board()


def test_a_repeated_order_id_is_refused(workspace: pathlib.Path) -> None:
    """Two orders sharing an id would make every lookup ambiguous."""

    entry = {
        "order_id": "ORD-0001",
        "title": "Blade",
        "status": "orders_received",
        "priority": "normal",
        "smith": "",
        "description": "",
        "created_at": "",
    }
    write_board(workspace, {"version": 1, "next_id": "ORD-0002", "orders": [entry, dict(entry)]})

    with pytest.raises(OrderStoreError):
        OrderStore(workspace).board()


def test_a_file_from_a_newer_forge_is_refused_rather_than_misread(
    workspace: pathlib.Path,
) -> None:
    write_board(workspace, {"version": 99, "orders": []})

    with pytest.raises(OrderStoreError):
        OrderStore(workspace).board()


def test_a_write_over_a_corrupt_file_is_refused_rather_than_replacing_it(
    store: OrderStore,
) -> None:
    """The ruling's point: the corrupt file is never overwritten by an empty board.

    A create that succeeded here would look like the board had been emptied, and
    the original contents would be gone.
    """
    store.create(title="Blade")
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OrderStoreError):
        store.create(title="Another")

    assert store.path.read_text(encoding="utf-8") == "{not json"


def test_an_unwritable_location_is_reported_rather_than_swallowed(
    tmp_path: pathlib.Path,
) -> None:
    """A directory where the file should be is a write failure, not a crash."""

    blocked = tmp_path / "blocked"
    (blocked / ".forge" / "orders.json").mkdir(parents=True)

    with pytest.raises(OrderStoreError):
        OrderStore(blocked).create(title="Blade")


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------
#
# The store serialises every mutation with one lock held across a
# read-modify-write. These tests exist because that claim is otherwise only an
# inspection: a lock that is taken for the read and released before the write
# would pass every test above, because no test above has ever had two writers.


def test_two_simultaneous_creates_both_survive(workspace: pathlib.Path) -> None:
    """The interleaving the lock exists to prevent.

    Without it, both threads read an empty board, both mint ``ORD-0001``, and
    both write -- leaving one order on disk and the other silently gone.
    """
    store = OrderStore(workspace)
    barrier = threading.Barrier(2)

    def create(title: str) -> list[str]:
        made: list[str] = []
        barrier.wait()
        order = store.create(title=title)
        made.append(order.order_id)
        return made

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["First", "Second"]))

    titles = sorted(order.title for order in store.board().orders)
    assert titles == ["First", "Second"], "one create was lost"
    assert len(store.board().orders) == 2


def test_simultaneous_creates_produce_unique_ids(workspace: pathlib.Path) -> None:
    """Distinct ids, and a counter that advanced past both.

    A duplicate here would be two Orders sharing an id, which makes every later
    lookup ambiguous -- so this is a separate assertion rather than a detail of
    the survival test.
    """
    store = OrderStore(workspace)
    barrier = threading.Barrier(2)

    def create(n: int) -> str:
        barrier.wait()
        return store.create(title=f"Blade {n}").order_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))

    assert len(set(ids)) == 2, f"both threads minted the same id: {ids}"
    stored = [order.order_id for order in store.board().orders]
    assert sorted(stored) == sorted(ids)
    assert store.board().next_id > max(_number_of(i) or 0 for i in ids)


def test_many_simultaneous_creates_lose_nothing(workspace: pathlib.Path) -> None:
    """Wider than the pairwise case, and against one shared store.

    More threads than the lock's critical section admits at once, so this is the
    case where a lock held for the write but not the read would duplicate ids
    repeatedly rather than once.
    """
    store = OrderStore(workspace)
    workers = 8

    def create(n: int) -> str:
        return store.create(title=f"Blade {n}").order_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        ids = list(pool.map(create, range(workers)))

    board = store.board()
    assert len(board.orders) == workers, "an order was lost under contention"
    assert len({order.order_id for order in board.orders}) == workers
    assert len(set(ids)) == workers


def test_the_document_stays_valid_under_concurrent_writes(
    workspace: pathlib.Path,
) -> None:
    """Every intermediate file is parseable, not just the last one.

    This is the atomicity claim, tested through the property an operator would
    observe: a reader at any moment sees a whole board, never a half-written
    one. The store rewrites through a temp file and ``os.replace``, so a reader
    racing the writers either sees the previous document or the next -- and never
    a truncated one.
    """
    store = OrderStore(workspace)
    path = store.path
    workers = 6
    reads_per_worker = 40
    stop = threading.Event()
    bad: list[str] = []
    reads = [0]

    def writer(n: int) -> None:
        store.create(title=f"Blade {n}")

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # Before the first write there is no file, which is an empty
                # board rather than a broken one.
                continue
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                bad.append(f"unparseable document: {exc}")
                stop.set()
                return
            if not isinstance(document, dict) or not isinstance(
                document.get("orders"), list
            ):
                bad.append(f"malformed document: {list(document)[:5]}")
                stop.set()
                return
            reads[0] += 1

    readers = [
        threading.Thread(target=reader, daemon=True) for _ in range(2)
    ]
    for thread in readers:
        thread.start()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(writer, range(workers)))
    finally:
        stop.set()
        for thread in readers:
            thread.join(timeout=5)

    assert not bad, bad[0]
    assert reads[0] > 0, "the reader never observed the file; the test proved nothing"


def test_a_create_racing_an_edit_leaves_the_edit_intact(
    workspace: pathlib.Path,
) -> None:
    """The lost-update case across two different operations.

    An edit that read the board before a create landed, then wrote it back, would
    delete the created order. Both mutations hold the same lock, so the edit
    either sees the new order or the create sees the edit -- never neither.
    """
    store = OrderStore(workspace)
    existing = store.create(title="Existing")
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def edit() -> None:
        barrier.wait()
        outcome["edited"] = store.edit(existing.order_id, title="Edited")

    def create() -> None:
        barrier.wait()
        outcome["created"] = store.create(title="Concurrent")

    threads = [threading.Thread(target=edit), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    titles = sorted(order.title for order in store.board().orders)
    assert titles == ["Concurrent", "Edited"], f"an update was lost: {titles}"


def test_a_create_racing_a_move_leaves_the_order_in_exactly_one_column(
    workspace: pathlib.Path,
) -> None:
    """Movement is not editing, but both write the whole document.

    A move that read before a create landed would write back a board without it.
    """
    store = OrderStore(workspace)
    existing = store.create(title="Existing")
    barrier = threading.Barrier(2)

    def move() -> None:
        barrier.wait()
        store.move(existing.order_id, "at_the_forge")

    def create() -> None:
        barrier.wait()
        store.create(title="Concurrent")

    threads = [threading.Thread(target=move), threading.Thread(target=create)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    board = store.board()
    columns = {column["status"]: column for column in board.columns()}
    assert board.as_payload()["total"] == len(board.orders) == 2
    assert columns["at_the_forge"]["count"] == 1
    assert sum(column["count"] for column in board.columns()) == 2, (
        "an order is in no column or in two"
    )


def test_concurrent_writers_do_not_corrupt_the_stored_document(
    workspace: pathlib.Path,
) -> None:
    """The file is valid JSON with a well-formed sequence after the dust settles."""

    store = OrderStore(workspace)
    workers = 6

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda n: store.create(title=f"Blade {n}"), range(workers)))

    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert isinstance(document["next_id"], str)
    ids = [order["order_id"] for order in document["orders"]]
    assert len(ids) == workers
    assert len(set(ids)) == workers
    assert document["next_id"] == store.board().as_payload()["next_id"]
    # No temp file survives the contention.
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["orders.json"]


# --------------------------------------------------------------------------
# Inertness
# --------------------------------------------------------------------------


def test_the_architecture_document_records_the_boards_storage_and_routes() -> None:
    """Section 3b must keep stating the decisions a reader would otherwise
    reverse-engineer.

    Documentation drifts silently, and this one is worth holding: the next person
    to add an Order feature needs to know the board is workspace-scoped, that the
    write is atomic under a lock, and that movement is deliberately a separate
    route. Each fact below is asserted against the document so removing one is a
    visible test failure rather than a quiet divergence from the code.
    """
    doc = (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")

    assert "3b. The Order Board" in doc, "the Order Board section is missing"
    section = doc.split("### 3b. The Order Board", 1)[1].split("\n### ", 1)[0]
    # Prose is hard-wrapped, so a phrase can straddle a newline. Collapsing
    # whitespace before matching keeps these guards about the fact being
    # documented rather than about where the author happened to wrap.
    flat = " ".join(section.split())

    # Workspace-scoped, at a named path.
    assert "<workspace>/.forge/orders.json" in flat
    assert "workspace-scoped" in flat.lower()

    # Atomic whole-document write, under a lock.
    assert "os.replace" in flat, "the atomic rename must be documented"
    assert "fsync" in flat
    assert "threading.Lock" in flat
    assert "read-modify-write" in flat

    # Monotonic ids, gaps valid, never intentionally reused.
    assert "monotonic" in flat
    assert "gap" in flat.lower(), "a gap in the sequence must be called normal"
    assert "never intentionally reused" in flat, (
        "the 'an id is never reused' decision must be stated, not implied"
    )

    # The five routes and the read-versus-command split.
    for route in (
        "/api/orders",
        "/api/orders/{id}",
        "/api/orders/{id}/move",
    ):
        assert route in doc, f"{route} is not in the route table"
    assert "PATCH" in flat, "the edit route must be documented"

    # Movement separate from editing.
    assert "Movement is not editing" in flat

    # No execution semantics, and the named exclusions.
    assert "no execution semantics" in flat
    for excluded in ("delete", "drag-and-drop", "history", "Git metadata"):
        assert excluded in flat, f"the exclusion of {excluded} is not recorded"


def test_the_route_table_lists_every_orders_route_with_its_guard() -> None:
    """Each Orders route carries a guard in the table, not just a path.

    A route listed without a guard column would be a route whose authorisation is
    unstated, which is the thing most worth noticing in this table.
    """
    doc = (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")

    orders_rows = [
        line
        for line in doc.splitlines()
        if line.strip().startswith("| `/api/orders")
    ]

    assert len(orders_rows) == 5, f"expected 5 Orders routes, found {len(orders_rows)}"

    for line in orders_rows:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"malformed route row: {line}"
        route, method, kind, guard = cells
        assert method in ("GET", "POST", "PATCH"), route
        assert kind in ("observation", "command"), route
        assert "token" in guard, f"{route} lists no token guard"
        if kind == "command":
            assert "exact `Origin`" in guard, f"{route} is a command without the strict guard"
        else:
            assert "read-only same-origin pair" in guard, route


def test_the_docs_do_not_still_claim_persistence_is_absent() -> None:
    """`architecture.md` previously listed "Database or persistence layer" as
    entirely out of scope, which stopped being true when the board shipped.

    The replacement has to actually be there, or this assertion would pass against
    a document that had simply deleted the claim.
    """
    doc = (
        pathlib.Path(__file__).resolve().parent.parent / "docs" / "architecture.md"
    ).read_text(encoding="utf-8")

    assert "- Database or persistence layer." not in doc
    assert "A **database**" in doc, (
        "the out-of-scope entry should now name what is actually excluded"
    )


def test_the_store_imports_nothing_that_could_run_agent_work() -> None:
    """No Order may be able to reach the model layer, the tools, or a session.

    Checked on the module's imports rather than by reading its prose, because the
    property that matters is what the module can reach, and a comment asserting
    inertness is exactly the kind of claim that decays quietly.
    """
    source = (
        pathlib.Path(forge.ui.orders.__file__).read_text(encoding="utf-8")
    )
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; its module is named only by the
            # trailing part, so resolve it against this package.
            if node.level:
                base = "forge.ui" if node.module is None else f"forge.ui.{node.module}"
                imported.add(base)
                imported.update(f"forge.ui.{node.module}.{a.name}" for a in node.names)
            elif node.module:
                imported.add(node.module)

    forbidden = {
        "forge.models",
        "forge.models.base",
        "forge.models.router",
        "forge.models.scripted",
        "forge.models.verify",
        "forge.tools",
        "forge.tools.dispatcher",
        "forge.tools.execution",
        "forge.tools.approval",
        "forge.ui.session",
        "forge.ui.runner",
        "forge.agents.byte",
    }
    assert not (imported & forbidden), f"orders can reach {sorted(imported & forbidden)}"


def test_the_store_reaches_nothing_outside_the_standard_library_and_stdlib_typing() -> None:
    """The dependency rule this repository holds everywhere else.

    ``forge`` core is stdlib-only by design (``pyproject.toml``); a store that
    imported a third-party package would make an install of ``forge`` without the
    workbench extras fail for a reason nobody asked for.
    """
    source = pathlib.Path(forge.ui.orders.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots.add(node.module.split(".")[0])

    assert not roots - _STDLIB_ROOTS, f"third-party import: {sorted(roots - _STDLIB_ROOTS)}"


def test_the_store_holds_no_delete(store: OrderStore) -> None:
    """No delete feature in this MVP.

    Asserted as an absent attribute, so adding one later is a deliberate change
    that fails this test rather than a method nobody notices appearing.
    """
    assert not [name for name in dir(OrderStore) if "delete" in name or "remove" in name]


def test_an_order_is_immutable(store: OrderStore) -> None:
    order = store.create(title="Blade")

    with pytest.raises(Exception):
        order.title = "Something else"  # type: ignore[misc]


def test_a_board_is_immutable(store: OrderStore) -> None:
    board = store.board()

    with pytest.raises(Exception):
        board.orders = ()  # type: ignore[misc]


def test_an_order_can_be_constructed_directly_for_a_caller() -> None:
    """The dataclass is usable on its own, which keeps it a value not a service."""

    order = Order(
        order_id="ORD-0001",
        title="Blade",
        status="orders_received",
        priority="normal",
        smith="",
        description="",
        created_at="2026-10-04T02:00:00+00:00",
    )

    assert order.as_dict()["order_id"] == "ORD-0001"


def test_an_empty_board_reports_one_as_its_next_id() -> None:
    assert OrderBoard(orders=(), next_id=1).next_id == 1