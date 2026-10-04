"""The Order Board API: the five routes, their contracts, and their guards.

These drive the real ASGI app through Starlette's ``TestClient``, so routing,
dependencies, status codes and JSON shapes are exercised as a browser would
exercise them. The model is always :class:`ScriptedGateway` and the board lives
in a ``tmp_path`` workspace, so no test here opens a socket, needs a credential,
or touches a developer's real ``.forge/orders.json``.

The tests worth reading first:

*   :func:`test_reading_the_board_requires_the_token` and its neighbours -- an
    Order is readable and changeable by anything that can reach the port, so the
    guards are the boundary, and they are asserted per route rather than once.
*   :func:`test_a_mutating_route_refuses_a_cross_origin_post` -- a forged
    submission from another page, which is the attack localhost does not stop.
*   :func:`test_a_corrupt_board_answers_500_rather_than_an_empty_board` -- the
    ruling that a malformed file is visible.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.orders import STATUSES, OrderStore, orders_path
from forge.ui.runner import runtime_factory
from forge.ui.security import SEC_FETCH_SITE_HEADER, TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import WorkbenchSession

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace_root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


class Harness:
    """One app, one board, one token."""

    def __init__(self, workspace_root: pathlib.Path) -> None:
        self.root = workspace_root
        self.workspace = Workspace(workspace_root)
        self.token = UiToken()
        self.model = ScriptedGateway([])
        self.broker = SseBroker()
        self.session = WorkbenchSession(
            workspace=self.workspace,
            profile=BYTE,
            model_id="scripted",
            runtime_factory=runtime_factory(
                self.workspace,
                profile=BYTE,
                gateway=self.model,
                model_id="scripted",
            ),
            broker=self.broker,
        )
        self.app = create_app(
            workspace=self.workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
        )
        self.client = TestClient(self.app)

    @property
    def store(self) -> OrderStore:
        return OrderStore(self.root)

    @property
    def orders_path(self) -> pathlib.Path:
        """Where the board lives, so a test can corrupt it deliberately."""
        return orders_path(self.root)

    # -- headers -------------------------------------------------------

    def headers(self, *, token: str | None = None, origin: str | None = ORIGIN, **extra):
        headers = dict(extra)
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers[TOKEN_HEADER] = token
        return headers

    def auth(self, token: str | None = None, **overrides):
        return self.headers(
            token=self.token.value if token is None else token, **overrides
        )

    def read_headers(self, **overrides):
        """What a same-origin browser GET actually sends.

        No ``Origin`` at all and ``Sec-Fetch-Site: same-origin``. Copied from the
        measurement in :mod:`forge.ui.security` rather than invented here -- a
        test that invents a header set proves nothing about the real page.

        Caller overrides are applied *after* these defaults, so a test can
        substitute a token or change the Origin without this helper silently
        dropping what it was asked for. Overrides go through :meth:`headers`
        rather than being merged into the dict, so ``token=`` still becomes the
        ``X-Forge-Token`` header instead of a header literally named ``token``.
        """
        overrides.setdefault("token", self.token.value)
        overrides.setdefault("origin", None)
        overrides.setdefault(SEC_FETCH_SITE_HEADER, "same-origin")
        return self.headers(**overrides)

    # -- requests ------------------------------------------------------

    def list_orders(self, **overrides):
        return self.client.get("/api/orders", headers=self.read_headers(**overrides))

    def create_order(self, **body):
        return self.client.post(
            "/api/orders", json=body, headers=self.auth()
        )

    def get_order(self, order_id: str, **overrides):
        return self.client.get(
            f"/api/orders/{order_id}", headers=self.read_headers(**overrides)
        )

    def edit_order(self, order_id: str, /, **body):
        # `order_id` is positional-only and the body is passed as a dict, so a
        # test can send a field literally named `order_id` and prove the server
        # ignores it instead of colliding with this parameter.
        return self.client.patch(
            f"/api/orders/{order_id}", json=dict(body), headers=self.auth()
        )

    def move_order(self, order_id: str, status: str, **overrides):
        return self.client.post(
            f"/api/orders/{order_id}/move",
            json={"status": status},
            headers=self.auth(**overrides),
        )

    def delete_order(self, order_id: str, **overrides):
        return self.client.delete(
            f"/api/orders/{order_id}", headers=self.auth(**overrides)
        )


@pytest.fixture()
def harness(workspace_root: pathlib.Path) -> Harness:
    return Harness(workspace_root)


def one_order(harness: Harness, **body) -> dict:
    """Create one order and assert it was accepted, returning it.

    ``body`` is merged over a default title rather than splatted alongside it, so
    a caller can override the title without this helper passing it twice.
    """
    created = harness.create_order(**{"title": "Straighten the tongs", **body})
    assert created.status_code == 201, created.text
    return created.json()


# --------------------------------------------------------------------------
# Listing and grouping
# --------------------------------------------------------------------------


def test_an_empty_board_lists_every_workflow_state(harness: Harness) -> None:
    response = harness.list_orders()

    assert response.status_code == 200
    assert [c["status"] for c in response.json()["columns"]] == list(STATUSES)


def test_an_empty_board_reports_no_orders(harness: Harness) -> None:
    assert harness.list_orders().json()["total"] == 0


def test_a_created_order_appears_in_the_first_column(harness: Harness) -> None:
    one_order(harness)

    columns = {c["status"]: c for c in harness.list_orders().json()["columns"]}

    assert columns["orders_received"]["count"] == 1
    assert columns["orders_received"]["orders"][0]["title"] == "Straighten the tongs"


def test_orders_are_grouped_by_status_over_the_api(harness: Harness) -> None:
    harness.create_order(title="One", status="orders_received")
    harness.create_order(title="Two", status="at_the_forge")
    harness.create_order(title="Three", status="finished_goods")

    columns = {c["status"]: c for c in harness.list_orders().json()["columns"]}

    assert columns["at_the_forge"]["orders"][0]["title"] == "Two"
    assert columns["finished_goods"]["orders"][0]["title"] == "Three"
    assert columns["orders_received"]["count"] == 1


def test_a_moved_order_leaves_its_column_over_the_api(harness: Harness) -> None:
    order = one_order(harness)

    harness.move_order(order["order_id"], "at_the_forge")

    columns = {c["status"]: c for c in harness.list_orders().json()["columns"]}
    assert columns["orders_received"]["count"] == 0
    assert columns["at_the_forge"]["count"] == 1


def test_the_listing_publishes_the_backend_status_vocabulary(harness: Harness) -> None:
    """Ruling: the frontend consumes this rather than reproducing it."""

    payload = harness.list_orders().json()

    assert payload["statuses"] == list(STATUSES)


def test_the_listing_publishes_the_priority_vocabulary(harness: Harness) -> None:
    assert harness.list_orders().json()["priorities"] == [
        "low",
        "normal",
        "high",
        "urgent",
    ]


def test_each_column_carries_a_heading_for_the_page(harness: Harness) -> None:
    columns = harness.list_orders().json()["columns"]

    assert all(column["label"] for column in columns)


def test_a_column_reports_its_count(harness: Harness) -> None:
    one_order(harness)
    one_order(harness, title="Second")

    columns = {c["status"]: c for c in harness.list_orders().json()["columns"]}

    assert columns["orders_received"]["count"] == 2


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


def test_creating_an_order_returns_201_and_the_order(harness: Harness) -> None:
    response = harness.create_order(title="Straighten the tongs", smith="Byte")

    assert response.status_code == 201
    assert response.json()["order_id"] == "ORD-0001"
    assert response.json()["title"] == "Straighten the tongs"
    assert response.json()["smith"] == "Byte"


def test_a_created_order_defaults_to_the_first_state_and_normal_priority(
    harness: Harness,
) -> None:
    body = one_order(harness)

    assert body["status"] == "orders_received"
    assert body["priority"] == "normal"


def test_a_created_order_records_a_creation_time(harness: Harness) -> None:
    assert one_order(harness)["created_at"].endswith("+00:00")


def test_creating_an_order_without_a_title_is_refused(harness: Harness) -> None:
    response = harness.client.post(
        "/api/orders", json={"smith": "Byte"}, headers=harness.auth()
    )

    assert response.status_code == 422


def test_creating_an_order_with_an_unknown_priority_is_refused(harness: Harness) -> None:
    response = harness.create_order(title="Blade", priority="whenever")

    assert response.status_code == 422


def test_creating_an_order_with_an_unknown_status_is_refused(harness: Harness) -> None:
    response = harness.create_order(title="Blade", status="on_the_anvil")

    assert response.status_code == 422


def test_a_refused_create_stores_nothing(harness: Harness) -> None:
    harness.create_order(title="   ")

    assert harness.store.board().orders == ()


def test_two_created_orders_get_different_ids(harness: Harness) -> None:
    first = one_order(harness)
    second = one_order(harness, title="Second")

    assert first["order_id"] != second["order_id"]


def test_created_ids_increase(harness: Harness) -> None:
    ids = [one_order(harness, title=f"Blade {n}")["order_id"] for n in range(3)]

    assert ids == ["ORD-0001", "ORD-0002", "ORD-0003"]


# --------------------------------------------------------------------------
# Read one
# --------------------------------------------------------------------------


def test_an_order_can_be_opened_by_id(harness: Harness) -> None:
    order = one_order(harness, description="Flatten to 6mm")

    response = harness.get_order(order["order_id"])

    assert response.status_code == 200
    assert response.json()["description"] == "Flatten to 6mm"


def test_opening_an_unknown_order_is_404(harness: Harness) -> None:
    assert harness.get_order("ORD-0404").status_code == 404


def test_an_order_carries_its_own_fields(harness: Harness) -> None:
    order = one_order(harness, smith="Byte", priority="high", description="Flatten")

    body = harness.get_order(order["order_id"]).json()

    assert body["order_id"] == "ORD-0001"
    assert body["smith"] == "Byte"
    assert body["priority"] == "high"
    assert body["description"] == "Flatten"
    assert body["status_label"] == "Orders Received"
    assert body["created_at"]


# --------------------------------------------------------------------------
# Edit
# --------------------------------------------------------------------------


def test_editing_returns_the_edited_order(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.edit_order(order["order_id"], title="Re-temper the blade")

    assert response.status_code == 200
    assert response.json()["title"] == "Re-temper the blade"


def test_an_edit_is_visible_on_the_next_read(harness: Harness) -> None:
    order = one_order(harness)
    harness.edit_order(order["order_id"], priority="urgent")

    assert harness.get_order(order["order_id"]).json()["priority"] == "urgent"


def test_an_edit_leaves_the_other_fields_alone(harness: Harness) -> None:
    order = one_order(harness, smith="Byte", description="Flatten")

    harness.edit_order(order["order_id"], title="Blade 2")

    body = harness.get_order(order["order_id"]).json()
    assert body["smith"] == "Byte"
    assert body["description"] == "Flatten"


def test_an_edit_cannot_change_the_status(harness: Harness) -> None:
    """Ruling 2 at the API boundary.

    ``status`` is not a field of the edit body. Sending it does not move the
    order -- FastAPI ignores an undeclared field -- and asserting that is
    stronger than asserting the field is absent from a schema.
    """
    order = one_order(harness)

    harness.edit_order(order["order_id"], status="finished_goods", title="Blade 2")

    assert harness.get_order(order["order_id"]).json()["status"] == "orders_received"


def test_editing_an_empty_body_is_refused(harness: Harness) -> None:
    order = one_order(harness)

    assert harness.edit_order(order["order_id"]).status_code == 422


def test_editing_an_unknown_order_is_404(harness: Harness) -> None:
    response = harness.edit_order("ORD-0404", title="Blade")

    assert response.status_code == 404


def test_editing_with_an_unknown_priority_is_refused(harness: Harness) -> None:
    order = one_order(harness)

    assert harness.edit_order(order["order_id"], priority="asap").status_code == 422


def test_a_refused_edit_changes_nothing(harness: Harness) -> None:
    order = one_order(harness, priority="low")

    harness.edit_order(order["order_id"], priority="asap")

    assert harness.get_order(order["order_id"]).json()["priority"] == "low"


def test_an_edit_does_not_change_the_creation_time(harness: Harness) -> None:
    order = one_order(harness)

    harness.edit_order(order["order_id"], title="Blade 2")

    assert (
        harness.get_order(order["order_id"]).json()["created_at"]
        == order["created_at"]
    )


def test_an_edit_does_not_change_the_order_id(harness: Harness) -> None:
    order = one_order(harness)

    harness.edit_order(order["order_id"], order_id="ORD-9999", title="Blade 2")

    assert harness.get_order(order["order_id"]).json()["order_id"] == "ORD-0001"


# --------------------------------------------------------------------------
# Move
# --------------------------------------------------------------------------


def test_moving_an_order_returns_the_moved_order(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.move_order(order["order_id"], "at_the_forge")

    assert response.status_code == 200
    assert response.json()["status"] == "at_the_forge"


def test_an_order_can_be_moved_to_every_workflow_state(harness: Harness) -> None:
    for status in STATUSES:
        order = one_order(harness, title=f"Blade {status}")
        assert harness.move_order(order["order_id"], status).json()["status"] == status


def test_moving_an_order_to_an_unknown_state_is_refused(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.move_order(order["order_id"], "ready_to_ship")

    assert response.status_code == 422


def test_a_refused_move_leaves_the_order_where_it_was(harness: Harness) -> None:
    order = one_order(harness)

    harness.move_order(order["order_id"], "ready_to_ship")

    assert harness.get_order(order["order_id"]).json()["status"] == "orders_received"


def test_moving_an_unknown_order_is_404(harness: Harness) -> None:
    assert harness.move_order("ORD-0404", "at_the_forge").status_code == 404


def test_moving_without_a_status_is_refused(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.client.post(
        f"/api/orders/{order['order_id']}/move", json={}, headers=harness.auth()
    )

    assert response.status_code == 422


def test_a_move_preserves_the_editable_fields(harness: Harness) -> None:
    order = one_order(harness, smith="Byte", description="Flatten")

    moved = harness.move_order(order["order_id"], "finished_goods").json()

    assert (moved["smith"], moved["description"]) == ("Byte", "Flatten")


def test_moving_to_the_orders_own_state_succeeds(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.move_order(order["order_id"], "orders_received")

    assert response.status_code == 200
    assert response.json()["status"] == "orders_received"


# --------------------------------------------------------------------------
# Persistence through the server
# --------------------------------------------------------------------------


def test_the_board_is_written_into_the_workspace(harness: Harness) -> None:
    one_order(harness)

    path = orders_path(harness.root)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["orders"][0]["title"] == (
        "Straighten the tongs"
    )


def test_an_order_survives_a_new_app_over_the_same_workspace(
    workspace_root: pathlib.Path,
) -> None:
    """Persistence, as the operator experiences it: restart Forge, keep the board."""

    first = Harness(workspace_root)
    first.create_order(title="Temper the blade")

    second = Harness(workspace_root)
    payload = second.list_orders().json()

    columns = {c["status"]: c for c in payload["columns"]}
    assert columns["orders_received"]["orders"][0]["title"] == "Temper the blade"
    assert payload["next_id"] == "ORD-0002"


def test_ids_continue_after_a_restart_through_the_api(
    workspace_root: pathlib.Path,
) -> None:
    Harness(workspace_root).create_order(title="One")

    second = Harness(workspace_root)

    assert second.create_order(title="Two").json()["order_id"] == "ORD-0002"


def test_a_corrupt_board_answers_500_rather_than_an_empty_board(
    harness: Harness,
) -> None:
    """The ruling, over HTTP.

    A 200 with six empty columns would be indistinguishable from a board the
    operator just cleared, and would be the reading of it that costs them their
    orders.
    """
    path = orders_path(harness.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    response = harness.list_orders()

    assert response.status_code == 500
    assert "orders.json" in response.json()["detail"]


def test_a_corrupt_board_leaves_the_file_alone(harness: Harness) -> None:
    path = orders_path(harness.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    harness.list_orders()

    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_write_against_a_corrupt_board_is_500_and_overwrites_nothing(
    harness: Harness,
) -> None:
    path = orders_path(harness.root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    assert harness.create_order(title="Blade").status_code == 500
    assert path.read_text(encoding="utf-8") == "{not json"


def test_two_apps_sharing_a_workspace_share_one_board(
    workspace_root: pathlib.Path,
) -> None:
    first = Harness(workspace_root)
    order = first.create_order(title="Blade").json()

    second = Harness(workspace_root)
    second.move_order(order["order_id"], "in_the_quench")

    columns = {c["status"]: c for c in first.list_orders().json()["columns"]}
    assert columns["in_the_quench"]["count"] == 1


# --------------------------------------------------------------------------
# Delete
# --------------------------------------------------------------------------
#
# A command, so it takes the strict exact-Origin + token guard pair, answers the
# deleted Order rather than a bare 200, and never turns an unreadable board into
# an empty one.


def test_deleting_an_order_returns_200_and_the_deleted_order(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.delete_order(order["order_id"])

    assert response.status_code == 200
    assert response.json()["order_id"] == order["order_id"]
    assert response.json()["title"] == order["title"]


def test_the_deleted_order_carries_the_full_published_shape(harness: Harness) -> None:
    """The same nine fields a create returns, so a client can name what it removed."""

    order = one_order(harness)

    assert set(harness.delete_order(order["order_id"]).json()) == set(order)


def test_a_deleted_order_is_gone_over_the_api(harness: Harness) -> None:
    order = one_order(harness)

    harness.delete_order(order["order_id"])

    assert harness.get_order(order["order_id"]).status_code == 404


def test_a_deleted_order_leaves_the_board_over_the_api(harness: Harness) -> None:
    kept = one_order(harness, title="Keep")
    doomed = one_order(harness, title="Slate")

    harness.delete_order(doomed["order_id"])

    listed = harness.list_orders().json()
    assert listed["total"] == 1
    assert [o["order_id"] for c in listed["columns"] for o in c["orders"]] == [
        kept["order_id"]
    ]


def test_deleting_an_unknown_order_is_404(harness: Harness) -> None:
    assert harness.delete_order("ORD-0404").status_code == 404


def test_deleting_an_already_deleted_order_is_404(harness: Harness) -> None:
    """Deleting twice is a refusal, not an idempotent success.

    A second 200 would tell a client its delete worked when the order it named
    had already been gone -- and would hide a stale page behind a green answer.
    """
    order = one_order(harness)
    assert harness.delete_order(order["order_id"]).status_code == 200

    assert harness.delete_order(order["order_id"]).status_code == 404


def test_a_refused_delete_leaves_the_order_on_the_board(harness: Harness) -> None:
    order = one_order(harness)

    assert harness.delete_order("ORD-0404").status_code == 404

    assert harness.get_order(order["order_id"]).status_code == 200


def test_a_delete_persists_through_the_file(harness: Harness) -> None:
    """Asserted on the file, not on the next read of the same store.

    A route that deleted from a cache would answer 200 and leave the order in
    ``.forge/orders.json``, which is the only copy that survives a restart.
    """
    order = one_order(harness)

    harness.delete_order(order["order_id"])

    document = json.loads(orders_path(harness.root).read_text(encoding="utf-8"))
    assert [entry["order_id"] for entry in document["orders"]] == []


def test_a_delete_survives_a_fresh_store_over_the_same_workspace(
    workspace_root: pathlib.Path,
) -> None:
    """The reload case, through the app: a new store sees the deletion."""
    first = Harness(workspace_root)
    created = first.create_order(title="Slate").json()

    assert first.delete_order(created["order_id"]).status_code == 200

    second = Harness(workspace_root)
    assert second.get_order(created["order_id"]).status_code == 404


def test_deleting_the_highest_order_over_the_api_does_not_reuse_its_number(
    harness: Harness,
) -> None:
    for n in range(3):
        one_order(harness, title=f"Blade {n}")

    assert harness.delete_order("ORD-0003").status_code == 200

    assert one_order(harness, title="Four")["order_id"] == "ORD-0004"


def test_deleting_a_middle_order_over_the_api_does_not_reuse_a_number(
    harness: Harness,
) -> None:
    for n in range(3):
        one_order(harness, title=f"Blade {n}")

    assert harness.delete_order("ORD-0002").status_code == 200

    assert one_order(harness, title="Four")["order_id"] == "ORD-0004"


def test_the_board_reports_a_monotonic_next_id_after_a_delete(harness: Harness) -> None:
    one_order(harness)
    one_order(harness)

    harness.delete_order("ORD-0002")

    assert harness.list_orders().json()["next_id"] == "ORD-0003"


def test_a_delete_over_a_corrupt_board_answers_500_and_keeps_the_file(
    harness: Harness,
) -> None:
    """The ruling the board was built on, on the new route.

    A delete that treated an unreadable file as an empty board would answer 200,
    report the board clean, and leave the operator with a corrupt file and no
    orders -- the exact outcome the empty-board refusal exists to prevent.
    """
    order = one_order(harness)
    harness.orders_path.write_text("{not json", encoding="utf-8")

    response = harness.delete_order(order["order_id"])

    assert response.status_code == 500
    assert "detail" in response.json()
    assert harness.orders_path.read_text(encoding="utf-8") == "{not json"


def test_a_corrupt_board_is_not_reported_as_empty_after_a_failed_delete(
    harness: Harness,
) -> None:
    one_order(harness)
    harness.orders_path.write_text("{not json", encoding="utf-8")

    harness.delete_order("ORD-0001")

    assert harness.list_orders().status_code == 500


# --------------------------------------------------------------------------
# Delete: authorization
# --------------------------------------------------------------------------


def test_deleting_an_order_requires_the_token(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.client.delete(
        f"/api/orders/{order['order_id']}", headers=harness.headers(origin=None)
    )

    assert response.status_code == 403


def test_deleting_an_order_refuses_a_forged_token(harness: Harness) -> None:
    order = one_order(harness)

    assert harness.delete_order(order["order_id"], token="nope").status_code == 403


def test_a_delete_refuses_a_cross_origin_request(harness: Harness) -> None:
    """The forged-submission case, on the most destructive verb the surface has.

    A ``fetch`` DELETE is a non-simple method, so the browser attaches ``Origin``
    on it exactly as it does on a POST -- which is why this route can take the
    strict exact-Origin check without refusing the Workbench's own page.
    """
    order = one_order(harness)

    response = harness.delete_order(order["order_id"], origin="http://evil.example")

    assert response.status_code == 403


def test_a_refused_delete_does_not_remove_the_order(harness: Harness) -> None:
    """The guard must run before the store, not after.

    Asserted rather than assumed: a route that deleted and then answered 403
    would pass every other guard test in this module.
    """
    order = one_order(harness)

    harness.delete_order(order["order_id"], origin="http://evil.example")

    assert harness.get_order(order["order_id"]).status_code == 200


def test_a_delete_without_any_origin_is_refused(harness: Harness) -> None:
    """Absence fails closed, the same rule the read routes hold."""

    order = one_order(harness)

    assert harness.delete_order(order["order_id"], origin=None).status_code == 403


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_reading_the_board_requires_the_token(harness: Harness) -> None:
    response = harness.client.get("/api/orders", headers=harness.headers(origin=None))

    assert response.status_code == 403


def test_reading_the_board_refuses_a_forged_token(harness: Harness) -> None:
    response = harness.list_orders(token="not-the-token")

    assert response.status_code == 403


def test_reading_one_order_requires_the_token(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.client.get(
        f"/api/orders/{order['order_id']}", headers=harness.headers(origin=None)
    )

    assert response.status_code == 403


def test_creating_an_order_requires_the_token(harness: Harness) -> None:
    response = harness.client.post(
        "/api/orders", json={"title": "Blade"}, headers=harness.headers(origin=None)
    )

    assert response.status_code == 403


def test_creating_an_order_refuses_a_forged_token(harness: Harness) -> None:
    response = harness.client.post(
        "/api/orders", json={"title": "Blade"}, headers=harness.headers(token="nope")
    )

    assert response.status_code == 403


def test_editing_an_order_requires_the_token(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.client.patch(
        f"/api/orders/{order['order_id']}",
        json={"title": "Blade 2"},
        headers=harness.headers(origin=None),
    )

    assert response.status_code == 403


def test_moving_an_order_requires_the_token(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.client.post(
        f"/api/orders/{order['order_id']}/move",
        json={"status": "at_the_forge"},
        headers=harness.headers(origin=None),
    )

    assert response.status_code == 403


def test_a_mutating_route_refuses_a_cross_origin_post(harness: Harness) -> None:
    """The forged-submission case, which a token alone does not stop.

    A page on another origin cannot read the token, so this is defence in depth
    -- but it is the defence that stops a non-browser client which somehow has
    one, and it costs nothing to assert.
    """
    response = harness.client.post(
        "/api/orders",
        json={"title": "Blade"},
        headers=harness.auth(origin="http://evil.example"),
    )

    assert response.status_code == 403


def test_a_move_refuses_a_cross_origin_post(harness: Harness) -> None:
    order = one_order(harness)

    response = harness.move_order(order["order_id"], "at_the_forge", origin="http://evil.example")

    assert response.status_code == 403


def test_a_read_refuses_a_cross_origin_get_with_an_attacker_origin(
    harness: Harness,
) -> None:
    response = harness.client.get(
        "/api/orders", headers=harness.auth(origin="http://evil.example")
    )

    assert response.status_code == 403


def test_a_read_refuses_a_request_with_neither_origin_nor_fetch_metadata(
    harness: Harness,
) -> None:
    """Both headers stripped. Absence fails closed."""

    response = harness.client.get("/api/orders", headers=harness.auth(origin=None))

    assert response.status_code == 403


def test_a_read_refuses_a_cross_site_fetch_metadata_header(harness: Harness) -> None:
    response = harness.client.get(
        "/api/orders",
        headers=harness.auth(origin=None, **{SEC_FETCH_SITE_HEADER: "cross-site"}),
    )

    assert response.status_code == 403


def test_the_board_sends_no_cors_header(harness: Harness) -> None:
    """Absence is the policy; there is no setting that turns it on."""

    response = harness.list_orders()

    assert "access-control-allow-origin" not in {
        key.lower() for key in response.headers
    }


def test_a_refused_create_does_not_write_an_order(harness: Harness) -> None:
    harness.client.post(
        "/api/orders", json={"title": "Blade"}, headers=harness.headers(origin=None)
    )

    assert harness.store.board().orders == ()


def test_the_board_is_not_readable_without_a_token_from_a_fresh_process(
    workspace_root: pathlib.Path,
) -> None:
    """The token is per-process, so a stale page cannot read a restarted board.

    Same class of property as the stream ticket: a page served by a Workbench
    that has since died cannot drive the new one.
    """
    first = Harness(workspace_root)
    stale_token = first.token.value
    first.create_order(title="Blade")

    second = Harness(workspace_root)
    response = second.client.get(
        "/api/orders",
        headers=second.headers(token=stale_token, origin=None,
                                **{SEC_FETCH_SITE_HEADER: "same-origin"}),
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# Boundaries held
# --------------------------------------------------------------------------


def test_there_is_no_bulk_delete_route(harness: Harness) -> None:
    """Ruling: one order at a time, and no way to ask for more.

    ``DELETE /api/orders`` would be the bulk door, so it has to be a refusal.
    """
    one_order(harness)

    assert harness.client.delete("/api/orders", headers=harness.auth()).status_code >= 400


def test_there_is_no_put_route(harness: Harness) -> None:
    """PUT stays refused; the edit verb is PATCH and deletion is DELETE."""

    order = one_order(harness)

    response = harness.client.put(
        f"/api/orders/{order['order_id']}", json={"title": "x"}, headers=harness.auth()
    )

    assert response.status_code >= 400


def test_no_route_archives_or_soft_deletes_an_order(harness: Harness) -> None:
    """Ruling: no archive, no soft delete, no history. The delete is a hard one.

    Asserted on the stored document rather than on a status code: a route that
    answered 200 while marking the order deleted-in-place would pass every other
    assertion in this module.
    """
    order = one_order(harness)

    assert harness.delete_order(order["order_id"]).status_code == 200

    document = json.loads(orders_path(harness.root).read_text(encoding="utf-8"))
    assert document["orders"] == [], "the order was not actually removed"
    assert "deleted" not in json.dumps(document).lower()


def test_an_order_carries_no_model_cost_or_git_field(harness: Harness) -> None:
    """The exclusions, asserted on the shape the API publishes."""

    body = one_order(harness)

    assert set(body) == {
        "order_id",
        "title",
        "status",
        "status_label",
        "priority",
        "smith",
        "description",
        "created_at",
        "sequence",
    }


def test_creating_an_order_does_not_start_a_run(harness: Harness) -> None:
    """No automatic agent execution.

    The board is inert: creating and moving orders leaves the session exactly as
    it found it, and no model is called.
    """
    order = one_order(harness)
    harness.move_order(order["order_id"], "finished_goods")

    assert harness.session.state.value == "idle"
    assert harness.model.call_count == 0


def test_the_orders_routes_are_mounted_only_on_the_orders_path(harness: Harness) -> None:
    response = harness.client.get("/api/orders.json", headers=harness.read_headers())

    assert response.status_code == 404


def test_the_health_endpoint_is_unaffected_by_the_board(harness: Harness) -> None:
    """The board is additive; the existing surface answers as it did."""

    one_order(harness)

    assert harness.client.get("/api/health").json() == {
        "ok": True,
        "service": "forge-workbench",
    }


def test_the_status_endpoint_is_unaffected_by_the_board(harness: Harness) -> None:
    one_order(harness)

    status = harness.client.get("/api/status", headers=harness.read_headers())

    assert status.status_code == 200
    assert status.json()["state"] == "idle"


def test_the_server_module_imports_nothing_that_could_run_an_orders_work() -> None:
    """Structural, on the server module's imports.

    The board is reachable from routes in ``forge/ui/server.py``, so the check is
    that adding them did not require the server to import a dispatcher, an agent,
    or a gateway-building helper. Inertness here is a property of the import
    graph, not of a comment.
    """
    import ast

    server_path = (
        pathlib.Path(__file__).resolve().parent.parent / "forge" / "ui" / "server.py"
    )
    tree = ast.parse(server_path.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not {
        name
        for name in imported
        if name.startswith("forge.agents")
        or name.startswith("forge.tools.dispatcher")
        or name.startswith("forge.tools.execution")
    }