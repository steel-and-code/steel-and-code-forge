"""The Order Board: one durable, workspace-scoped set of work items.

This module is the whole of the Order Board's behaviour. It exists so the HTTP
surface stays thin, the same way :mod:`forge.ui.models` exists for model health:
the routes validate nothing, decide nothing, and hold no workflow vocabulary of
their own.

Five decisions live here, and each is a decision rather than a detail:

**A JSON file in the workspace, not a database.** ``.forge/orders.json``, relative
to the workspace root, rewritten whole on every mutation. ``docs/architecture.md``
lists a persistence layer as explicitly out of scope, and
``docs/agent-contract.md`` already settled the same question for durable project
memory: plain human-readable files inside the workspace, reviewable in a diff,
hand-editable by an operator. Orders are workspace-scoped for the same reason
project memory is -- they mean nothing outside the repository they were made in.

**Atomic writes and a lock.** The whole document is serialised to a temporary
file in the same directory and moved into place with :func:`os.replace`, which is
atomic on POSIX and on Windows. A half-written board after a crash is the one
failure mode that would make the operator distrust every Order on it, and a
half-written file is what a plain ``write`` produces on a full disk. The lock is
held across read-modify-write so two concurrent requests cannot interleave and
lose one of their changes; the UI serves one process and one operator, and the
cost is a few microseconds.

**A malformed file fails visibly.** :class:`OrderStoreError` propagates rather
than being caught and answered as an empty board. An empty board is a
trustworthy-looking statement -- "there is nothing here" -- and serving that on
top of a corrupt file would erase Orders silently and tell the operator the
correct thing to do next, which is nothing. A 500 with the reason names the file
and the problem.

**The next id comes from persisted state, never from a length.** The sequence
lives in the document as ``next_id``, and on load it is reconciled against the
orders actually present: the next number is the greater of the stored counter
and one past the highest number any existing id carries. So a hand-edited file
that added ``ORD-0042`` without bumping the counter does not mint ``ORD-0043``
for an existing order's number, and a restart never resets the sequence. See
:func:`_next_number`.

**Movement is not editing.** :meth:`OrderStore.move` is the only method that
writes ``status``. There is deliberately no way to change an order's status as a
side effect of editing its title, which leaves room for transition rules,
approvals, or agent automation to attach to movement without changing the edit
contract at all.

The workflow vocabulary here -- ``orders_received`` through ``finished_goods`` --
is a set of **stable identifiers**, not a UI theme. The blacksmith wording an
operator reads is a presentation decision and lives in ``board.js``;
``STATUSES`` is exported so the frontend can be asserted to consume it rather
than reproduce it. Nothing in this module decides what an Order *means* for
agent execution: no Order here can start a run, spend a model call, or touch the
dispatcher.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "Order",
    "OrderBoard",
    "OrderStore",
    "OrderStoreError",
    "UnknownOrder",
    "InvalidOrder",
    "UnknownStatus",
    "STATUSES",
    "PRIORITIES",
    "DEFAULT_STATUS",
    "DEFAULT_PRIORITY",
    "ID_PREFIX",
    "ID_WIDTH",
    "SCHEMA_VERSION",
    "orders_path",
    "STATUS_LABELS",
]

#: The workflow states, **in board order**. Order is meaningful -- it is the left
#: to right sequence an operator reads the shop in -- so it is a tuple and the
#: routes publish it verbatim rather than sorting a set.
STATUSES: tuple[str, ...] = (
    "orders_received",
    "on_the_rack",
    "at_the_forge",
    "in_the_quench",
    "ready_for_inspection",
    "finished_goods",
)

#: Presentation labels for the identifiers above. Exported so a test can assert
#: the two vocabularies line up one-to-one, and so the mapping is one visible
#: table rather than a switch statement somewhere in the frontend. No test in
#: this repository asserts a *word*; this exists so a missing label is a visible
#: gap rather than a column that renders with no heading.
STATUS_LABELS: dict[str, str] = {
    "orders_received": "Orders Received",
    "on_the_rack": "On The Rack",
    "at_the_forge": "At The Forge",
    "in_the_quench": "In The Quench",
    "ready_for_inspection": "Ready For Inspection",
    "finished_goods": "Finished Goods",
}

#: Priority, low to high. A tuple for the same reason ``STATUSES`` is: the
#: board and the edit control both need a deterministic order.
PRIORITIES: tuple[str, ...] = ("low", "normal", "high", "urgent")

DEFAULT_STATUS = STATUSES[0]
DEFAULT_PRIORITY = "normal"

#: ``ORD-0001``. Human-readable, so an Order can be named in a conversation with
#: an operator, and zero-padded so a lexicographic sort is also a numeric sort.
ID_PREFIX = "ORD-"
ID_WIDTH = 4

#: Written into the file. Present so a future format change can be detected
#: rather than guessed at from the shape of the payload.
SCHEMA_VERSION = 1

#: Longest title, smith, and description accepted. Generous enough that a real
#: order description fits, short enough that one field cannot be used to write a
#: document into the operator's board. Enforced on write and on load, so a
#: hand-edited file cannot reintroduce an unbounded field.
MAX_TITLE = 200
MAX_SMITH = 80
MAX_DESCRIPTION = 2000

#: What an identifier looks like, when it was written by Forge. Used only for
#: reporting the sequence; see :func:`_number_of` for what is actually accepted.
_ID_PATTERN = re.compile(r"^ORD-(\d+)$")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class OrderStoreError(RuntimeError):
    """The board file exists but could not be used.

    Distinct from every "your request was wrong" error so the server answers 500
    for this and 4xx for the rest. A corrupt board is not the operator's fault
    in the request they just made, and answering 422 would send them looking for
    a typo in a form they did not touch.
    """


class UnknownOrder(OrderStoreError):
    """No order carries that id."""

    def __init__(self, order_id: str) -> None:
        super().__init__(f"no order with id {order_id!r}")
        self.order_id = order_id


class InvalidOrder(ValueError):
    """A field the operator supplied is not one Forge accepts.

    A ``ValueError`` because the server turns it into 422, matching how the rest
    of the surface reports a rejected body.
    """


class UnknownStatus(InvalidOrder):
    """A status outside :data:`STATUSES`."""

    def __init__(self, status: str) -> None:
        super().__init__(
            f"{status!r} is not a workflow state; expected one of "
            f"{', '.join(STATUSES)}"
        )
        self.status = status


# --------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Order:
    """One work item on the board.

    Frozen, and every mutation returns a new instance, so a caller holding an
    ``Order`` cannot be surprised by a later edit -- and a renderer reading an
    order cannot change it either. The board is shared state that several parts
    of one page read at once.

    Attributes:
        order_id: ``ORD-0001``. Stable for the life of the order; never reused,
            never renumbered.
        title: Required, one line.
        status: One of :data:`STATUSES`.
        priority: One of :data:`PRIORITIES`.
        smith: Free text. Empty means unassigned, which is a normal state and is
            displayed as such rather than as a blank field.
        description: Optional free text.
        created_at: ISO-8601 UTC, second resolution, assigned on creation and
            never rewritten. An edit must not make an order look newly received.
    """

    order_id: str
    title: str
    status: str
    priority: str
    smith: str
    description: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        """The JSON shape, and the shape the API returns verbatim.

        No derived fields and no renamed ones. ``sequence`` is included because
        a board needs the order column reads in; ``status_label`` is included so
        the frontend does not have to carry a second copy of the presentation
        vocabulary and be able to disagree with this one.
        """
        return {
            "order_id": self.order_id,
            "title": self.title,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "priority": self.priority,
            "smith": self.smith,
            "description": self.description,
            "created_at": self.created_at,
            "sequence": _number_of(self.order_id) or 0,
        }


def _now() -> str:
    """UTC, second resolution, matching the model-health stamp's format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _number_of(order_id: object) -> int | None:
    """The sequence number in ``order_id``, or ``None`` if there isn't one.

    Deliberately permissive about padding: ``ORD-7`` yields ``7``. An operator
    hand-editing the file will not match Forge's zero-padding, and refusing to
    read their edit would make the conservative next-id rule impossible -- the
    whole point of reconciling against existing ids is to notice an order Forge
    did not write. Only ``ORD`` followed by digits counts; an arbitrary string
    yields ``None`` and is simply skipped, which is the conservative direction:
    the counter advances on what can be understood, and an unparseable id can
    never cause one to be reused.
    """
    if not isinstance(order_id, str):
        return None
    match = _ID_PATTERN.match(order_id)
    if match:
        return int(match.group(1))
    loose = re.match(r"^ORD-0*(\d+)$", order_id)
    return int(loose.group(1)) if loose else None


def format_order_id(number: int) -> str:
    """``1`` -> ``ORD-0001``.

    Wider than :data:`ID_WIDTH` once the number outgrows the padding rather than
    truncating, so id 10001 is ``ORD-10001`` and stays sort-ordered.
    """
    return f"{ID_PREFIX}{number:0{ID_WIDTH}d}"


def _next_number(orders: list[Order], stored: object) -> int:
    """The next sequence number to mint, reconciled from both sources.

    The stored counter and the highest id actually present are two independent
    facts about the same sequence, and either can be ahead of the other after a
    hand edit or an interrupted write. Taking the greater of the two is the only
    answer that can never reuse a number:

    * a file whose ``next_id`` is behind its contents (an order added by hand)
      is corrected upward, so the new order cannot collide with it;
    * a file whose ``next_id`` is ahead of its contents (orders deleted, or a
      counter bumped in advance) is respected, so numbering does not walk
      backwards and re-issue numbers a human has already seen.

    ``len(orders) + 1`` is deliberately not used anywhere here. It is right only
    while the file is exactly as Forge last wrote it, and a board is a thing
    people edit by hand.
    """
    highest = 0
    for order in orders:
        number = _number_of(order.order_id)
        if number is not None:
            highest = max(highest, number)

    stored_number = _number_of(stored)
    if stored_number is None:
        # Absent or unreadable: derive from the orders alone. Losing the counter
        # costs nothing but continuity; reusing a number costs an Order.
        return highest + 1

    return max(stored_number, highest + 1)


def _clean_text(value: object, *, field: str, limit: int, required: bool) -> str:
    """Normalise one free-text field, or refuse it.

    A non-string is refused rather than coerced. ``str(123)`` would let a JSON
    client put a number in a field the UI renders as prose, and the value would
    come back looking like text that was always there.
    """
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise InvalidOrder(f"{field} must be a string")

    if required and not text:
        raise InvalidOrder(f"{field} is required")
    if len(text) > limit:
        raise InvalidOrder(f"{field} must be at most {limit} characters")
    return text


def _checked_status(value: object) -> str:
    if not isinstance(value, str) or value not in STATUSES:
        raise UnknownStatus(value if isinstance(value, str) else str(value))
    return value


def _checked_priority(value: object) -> str:
    if not isinstance(value, str) or value not in PRIORITIES:
        raise InvalidOrder(
            f"{value!r} is not a priority; expected one of {', '.join(PRIORITIES)}"
        )
    return value


# --------------------------------------------------------------------------
# The board
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderBoard:
    """The whole board at one instant: every order, plus the next sequence.

    Immutable and returned whole from every mutation, so a reader holding a board
    sees a consistent set -- an order cannot appear in one status and another
    simultaneously, which is the only way a column count could disagree with the
    column contents on screen.
    """

    orders: tuple[Order, ...]
    next_id: int

    def columns(self) -> list[dict[str, object]]:
        """Every status in board order, each with its orders.

        Every status key is always present, including the ones with nothing in
        them. The board has a fixed set of columns, and a column the API omits
        when empty is a column the page cannot draw a heading for -- so the
        empty-state message would have nowhere to live.

        Oldest first within each column, by sequence number rather than by the
        timestamp string: two orders created in the same second then fall back
        to the order they were received in, which is the sequence anyway.
        """
        grouped: dict[str, list[Order]] = {status: [] for status in STATUSES}
        for order in self.orders:
            grouped[order.status].append(order)

        return [
            {
                "status": status,
                "label": STATUS_LABELS[status],
                "count": len(grouped[status]),
                "orders": [
                    order.as_dict()
                    for order in sorted(
                        grouped[status], key=lambda o: _number_of(o.order_id) or 0
                    )
                ],
            }
            for status in STATUSES
        ]

    def as_payload(self) -> dict[str, object]:
        """The ``GET /api/orders`` body.

        ``statuses`` is published alongside the columns so the frontend can be
        held to consuming the backend's vocabulary rather than reproducing it.
        """
        return {
            "columns": self.columns(),
            "statuses": list(STATUSES),
            "priorities": list(PRIORITIES),
            "total": len(self.orders),
            "next_id": format_order_id(self.next_id),
        }


def _order_from_dict(raw: object, *, where: str) -> Order:
    """Rebuild one order from the file, refusing anything unusable.

    Validation on load is the counterpart to ruling 1: if the file is malformed,
    the board says so rather than starting empty. A hand-edited order missing a
    field is therefore a visible error naming the field, not an order that
    silently becomes a blank card.
    """
    if not isinstance(raw, dict):
        raise OrderStoreError(f"{where}: an order entry is not an object")

    order_id = raw.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise OrderStoreError(f"{where}: an order entry has no usable order_id")

    try:
        return Order(
            order_id=order_id,
            title=_clean_text(
                raw.get("title"), field="title", limit=MAX_TITLE, required=True
            ),
            status=_checked_status(raw.get("status")),
            priority=_checked_priority(raw.get("priority")),
            smith=_clean_text(
                raw.get("smith"), field="smith", limit=MAX_SMITH, required=False
            ),
            description=_clean_text(
                raw.get("description"),
                field="description",
                limit=MAX_DESCRIPTION,
                required=False,
            ),
            created_at=str(raw.get("created_at") or ""),
        )
    except InvalidOrder as invalid:
        raise OrderStoreError(f"{where}: order {order_id}: {invalid}") from invalid


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


def orders_path(workspace_root: Path) -> Path:
    """``.forge/orders.json`` under the workspace root.

    Derived from the root rather than passed in, so there is exactly one place
    that decides where a board lives and no caller can aim it somewhere else --
    the same reasoning that keeps the workspace root itself un-changeable by a
    request.
    """
    return Path(workspace_root) / ".forge" / "orders.json"


class OrderStore:
    """Orders on disk, in memory, and the two operations that keep them equal.

    Args:
        workspace_root: The run's workspace root. The board is stored beneath it
            and nowhere else.
        path: Overrides the file location. For tests, and for nothing else -- the
            server does not expose a way to set it.

    Thread-safe. Every mutation is a read-modify-write under one lock, and every
    reader takes the same lock, so a concurrent create cannot interleave with a
    move and lose either.
    """

    def __init__(self, workspace_root: Path, *, path: Path | None = None) -> None:
        self._path = Path(path) if path is not None else orders_path(workspace_root)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Where the board is stored."""
        return self._path

    # -- reads ---------------------------------------------------------

    def board(self) -> OrderBoard:
        """The whole board, read from disk.

        Read fresh on every call rather than served from a cache, because the
        file is the operator's to hand-edit and a cache would make a corrected
        order invisible until the next restart -- the opposite of the reason
        human-readable storage was chosen. The board is small enough that
        re-reading it per request costs nothing worth caching around.
        """
        with self._lock:
            return self._load_locked()

    def get(self, order_id: str) -> Order:
        """One order by id, or :class:`UnknownOrder`."""
        with self._lock:
            board = self._load_locked()
        for order in board.orders:
            if order.order_id == order_id:
                return order
        raise UnknownOrder(order_id)

    # -- writes --------------------------------------------------------

    def create(
        self,
        *,
        title: str,
        priority: str = DEFAULT_PRIORITY,
        status: str = DEFAULT_STATUS,
        smith: str = "",
        description: str = "",
    ) -> Order:
        """Create one order in the first workflow state.

        ``status`` defaults to the first state and is accepted as an argument
        only so a create and a move are testable through the same door. A caller
        that passes an unknown state is refused here rather than being quietly
        corrected -- a create route that silently fixed a bad status would hide
        the bug in whatever computed it.
        """
        clean_title = _clean_text(
            title, field="title", limit=MAX_TITLE, required=True
        )
        clean_priority = _checked_priority(priority)
        clean_status = _checked_status(status)
        clean_smith = _clean_text(smith, field="smith", limit=MAX_SMITH, required=False)
        clean_description = _clean_text(
            description, field="description", limit=MAX_DESCRIPTION, required=False
        )

        with self._lock:
            board = self._load_locked()
            order = Order(
                order_id=format_order_id(board.next_id),
                title=clean_title,
                status=clean_status,
                priority=clean_priority,
                smith=clean_smith,
                description=clean_description,
                created_at=_now(),
            )
            orders = (*board.orders, order)
            self._store_locked(orders, board.next_id + 1)
        return order

    def edit(
        self,
        order_id: str,
        *,
        title: str | None = None,
        priority: str | None = None,
        smith: str | None = None,
        description: str | None = None,
    ) -> Order:
        """Change an order's editable fields.

        ``status`` is **not** a parameter, on purpose. Movement has its own door
        (:meth:`move`), so a title edit cannot move an order and there is no
        version of this call that can.

        Only fields that are not ``None`` are changed, so a caller updating the
        smith does not blank the description. Clearing a field is an explicit
        empty string.
        """
        updates: dict[str, object] = {}
        if title is not None:
            updates["title"] = _clean_text(
                title, field="title", limit=MAX_TITLE, required=True
            )
        if priority is not None:
            updates["priority"] = _checked_priority(priority)
        if smith is not None:
            updates["smith"] = _clean_text(
                smith, field="smith", limit=MAX_SMITH, required=False
            )
        if description is not None:
            updates["description"] = _clean_text(
                description,
                field="description",
                limit=MAX_DESCRIPTION,
                required=False,
            )

        if not updates:
            raise InvalidOrder("an edit must change at least one field")

        with self._lock:
            board = self._load_locked()
            existing = self._find(board, order_id)
            # `created_at` and `order_id` are not in `updates`, so the new order
            # keeps the creation time it was received with.
            updated = replace(existing, **updates)
            orders = tuple(
                updated if order.order_id == order_id else order
                for order in board.orders
            )
            self._store_locked(orders, board.next_id)
        return updated

    def move(self, order_id: str, status: str) -> Order:
        """Move one order to ``status``.

        Moving to the state an order is already in succeeds and changes
        nothing. It is the same intent expressed twice, not a conflict, and a
        page that re-sends the state it just displayed should not have to
        distinguish "you asked for this" from "something is wrong".

        No transition rules are enforced. The workflow is currently a free move
        between any two states, and adding rules later belongs on this method
        rather than in the route, so a rule cannot be bypassed by another caller.
        """
        target = _checked_status(status)

        with self._lock:
            board = self._load_locked()
            existing = self._find(board, order_id)
            moved = replace(existing, status=target)
            orders = tuple(
                moved if order.order_id == order_id else order
                for order in board.orders
            )
            self._store_locked(orders, board.next_id)
        return moved

    # -- the file ------------------------------------------------------

    @staticmethod
    def _find(board: OrderBoard, order_id: str) -> Order:
        for order in board.orders:
            if order.order_id == order_id:
                return order
        raise UnknownOrder(order_id)

    def _load_locked(self) -> OrderBoard:
        """Read and validate the file. A missing file is an empty board.

        Every other failure raises :class:`OrderStoreError` rather than
        degrading to an empty board, and the server answers 500 with the reason.
        Serving an empty board over a corrupt file is the one behaviour that
        would lose orders without trace.
        """
        path = self._path
        if not path.exists():
            return OrderBoard(orders=(), next_id=1)

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OrderStoreError(f"could not read {path}: {exc}") from exc

        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise OrderStoreError(
                f"{path} is not valid JSON ({exc}); the board was left unchanged"
            ) from exc

        if not isinstance(document, dict):
            raise OrderStoreError(
                f"{path} does not contain a JSON object at the top level; the "
                "board was left unchanged"
            )

        version = document.get("version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise OrderStoreError(
                f"{path} has version {version!r}; this Forge reads version "
                f"{SCHEMA_VERSION}"
            )

        raw_orders = document.get("orders", [])
        if not isinstance(raw_orders, list):
            raise OrderStoreError(f"{path}: 'orders' is not a list")

        orders = tuple(
            _order_from_dict(entry, where=f"{path}: orders[{index}]")
            for index, entry in enumerate(raw_orders)
        )

        seen: set[str] = set()
        for order in orders:
            if order.order_id in seen:
                raise OrderStoreError(
                    f"{path}: order {order.order_id} appears more than once; "
                    "the board was left unchanged"
                )
            seen.add(order.order_id)

        return OrderBoard(
            orders=orders, next_id=_next_number(list(orders), document.get("next_id"))
        )

    def _store_locked(self, orders: tuple[Order, ...], next_id: int) -> None:
        """Write the whole document atomically.

        Temp file in the same directory, then :func:`os.replace`. Same directory
        matters: a rename across filesystems is not atomic, so a temp file in
        ``/tmp`` would reintroduce exactly the window this avoids.

        Serialised first, so a failure to serialise cannot leave a truncated
        temp file where the board should be.
        """
        document = {
            "version": SCHEMA_VERSION,
            "next_id": format_order_id(next_id),
            "orders": [
                {
                    "order_id": order.order_id,
                    "title": order.title,
                    "status": order.status,
                    "priority": order.priority,
                    "smith": order.smith,
                    "description": order.description,
                    "created_at": order.created_at,
                }
                for order in sorted(
                    orders, key=lambda o: _number_of(o.order_id) or 0
                )
            ],
        }
        payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"

        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / f".{self._path.name}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                # fsync before the rename: without it the rename can be durable
                # while the bytes it points at are not, which on an ext4 mount
                # with a full journal is a board that survived a power cut as an
                # empty file.
                os.fsync(handle.fileno())
            os.replace(temp, self._path)
        except OSError as exc:
            raise OrderStoreError(
                f"could not write {self._path}: {exc}; the board was left "
                "unchanged"
            ) from exc