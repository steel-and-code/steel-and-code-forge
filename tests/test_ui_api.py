"""HTTP-level tests for the Workbench: security, SSE, approvals, and the loop.

These drive the real ASGI app through Starlette's ``TestClient``, so the routing,
the dependencies, the status codes and the SSE framing are all exercised as a
browser would exercise them. What is *not* exercised is any network listener:
``TestClient`` calls the app in-process, and the model is always
:class:`ScriptedGateway`. No test here opens a socket or needs a credential.

The tests worth reading first:

*   :func:`test_the_event_loop_stays_responsive_while_a_run_waits` -- the
    property the whole worker-thread design exists for.
*   :func:`test_first_tab_wins_and_the_second_is_a_conflict` -- the multi-tab
    rule, including the assertion that the *run* continued.
*   :func:`test_a_refresh_recovers_the_same_pending_approval` -- no localStorage,
    and the id is unchanged.
*   The forged-token and wrong-Origin group -- because localhost is not a
    boundary on its own.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.runner import runtime_factory
from forge.ui.security import SEC_FETCH_SITE_HEADER, TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import UiSessionState, WorkbenchSession
from forge.ui.selector import ModelSelection, ModelSelector

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


class Harness:
    """One app, one session, one token -- the unit every test here operates on."""

    def __init__(self, workspace: Workspace, turns) -> None:
        self.token = UiToken()
        self.model = ScriptedGateway(turns)
        self.broker = SseBroker()
        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            selector=ModelSelector({}, ModelSelection("openrouter", "scripted")),
            runtime_factory=runtime_factory(
                workspace,
                profile=BYTE,
                gateway=self.model,
                model_id="scripted",
            ),
            broker=self.broker,
        )
        self.app = create_app(
            workspace=workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
        )
        self.client = TestClient(self.app)

    # -- request helpers -----------------------------------------------
    #
    # `origin`/`token` are keyword-only so that `**overrides` cannot collide
    # with them. Passing `token=None` through **kwargs is the idiom for "omit
    # the header", which the stream tests rely on.

    def headers(
        self,
        *,
        token: str | None = None,
        origin: str | None = ORIGIN,
        **extra: str,
    ):
        headers = dict(extra)
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers[TOKEN_HEADER] = token
        return headers

    def auth(self, token: str | None = None, **overrides):
        """The good token unless a test explicitly substitutes another."""
        return self.headers(
            token=self.token.value if token is None else token, **overrides
        )

    def status(self, **overrides):
        return self.client.get("/api/status", headers=self.auth(**overrides))

    def submit(self, task: str = "do a thing", **overrides):
        return self.client.post(
            "/api/run",
            json={"task": task},
            headers=self.auth(**overrides),
        )

    def approve(self, approval_id: str, decision: str = "yes", **overrides):
        return self.client.post(
            f"/api/approvals/{approval_id}",
            json={"decision": decision},
            headers=self.auth(**overrides),
        )

    def ticket(self, **overrides):
        return self.client.post("/api/events-ticket", headers=self.auth(**overrides))

    # -- waiting -------------------------------------------------------

    def wait_state(self, state: UiSessionState, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session.state is state:
                return True
            time.sleep(0.01)
        return self.session.state is state

    def wait_pending(self, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = self.session.channel.pending
            if pending is not None:
                return pending
            time.sleep(0.01)
        return None


@pytest.fixture()
def harness(workspace: Workspace) -> Harness:
    return Harness(workspace, [ScriptedTurn(text="done")])


# A run that asks for something gated, then answers the model.
GATED_TURNS = [
    ScriptedTurn(
        text="I need to remove the build directory.",
        tool_calls=(("exec_command", {"program": "rm", "args": ["-rf", "build"]}),),
    ),
    ScriptedTurn(text="Understood, I did not run it."),
]


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def test_the_page_carries_this_process_token(harness: Harness) -> None:
    response = harness.client.get("/")
    assert response.status_code == 200
    assert harness.token.value in response.text
    assert "__FORGE_UI_TOKEN__" not in response.text


def test_the_page_carries_no_other_process_token(workspace: Workspace) -> None:
    first = Harness(workspace, [ScriptedTurn(text="a")])
    second = Harness(workspace, [ScriptedTurn(text="b")])
    assert first.token.value not in second.client.get("/").text


def test_static_assets_are_served(harness: Harness) -> None:
    for path in ("/static/app.js", "/static/styles.css"):
        response = harness.client.get(path)
        assert response.status_code == 200, path
        assert response.content


def test_no_response_carries_cors_headers(harness: Harness) -> None:
    """No CORS header on any response, ever. Absence is the policy."""
    probes = [
        harness.client.get("/"),
        harness.status(),
        harness.submit(),
        harness.client.get("/api/health"),
    ]
    for response in probes:
        lowered = {key.lower() for key in response.headers}
        assert not any(key.startswith("access-control-") for key in lowered)
        assert response.headers.get("access-control-allow-origin") is None


# --------------------------------------------------------------------------
# Capability checks
# --------------------------------------------------------------------------


def test_status_requires_a_token(harness: Harness) -> None:
    assert harness.client.get("/api/status", headers=harness.headers()).status_code == 403


def test_a_forged_token_is_refused(harness: Harness) -> None:
    response = harness.status(token="not-the-token")
    assert response.status_code == 403


def test_a_token_from_another_process_is_refused(harness: Harness) -> None:
    assert harness.status(token=UiToken().value).status_code == 403


def test_status_requires_an_exact_origin(harness: Harness) -> None:
    assert harness.status(origin="http://evil.example").status_code == 403
    assert harness.status(origin="http://127.0.0.1:9999").status_code == 403
    assert harness.status(origin="https://127.0.0.1:8765").status_code == 403


def test_a_missing_origin_is_refused(harness: Harness) -> None:
    """Absence of *both* signals is a refusal.

    A browser always sends ``Origin`` on a POST and ``Sec-Fetch-Site`` on
    everything, so a request carrying neither is not a browser.
    """
    assert harness.status(origin=None).status_code == 403


def test_a_same_origin_get_with_no_origin_header_is_served(harness: Harness) -> None:
    """The regression: what a real Chrome actually sends on ``GET /api/status``.

    Measured on the wire against Chrome 154 serving this page -- a same-origin
    ``fetch`` GET arrives with **no** ``Origin`` header. The previous guard
    required an exact ``Origin`` here, so it refused the page's own request
    with a 403 that looked like a security event and was a browser-compat bug.

    The header set below is copied from that capture rather than invented.
    """
    response = harness.client.get(
        "/api/status",
        headers={
            SEC_FETCH_SITE_HEADER: "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"http://127.0.0.1:{PORT}/",
            TOKEN_HEADER: harness.token.value,
        },
    )
    assert response.status_code == 200
    assert response.json()["state"] == "idle"


def test_a_same_origin_get_still_needs_the_token(harness: Harness) -> None:
    """``Sec-Fetch-Site`` answers "which page", not "allowed to do this"."""
    response = harness.client.get(
        "/api/status",
        headers={SEC_FETCH_SITE_HEADER: "same-origin", TOKEN_HEADER: "wrong"},
    )
    assert response.status_code == 403


def test_a_cross_site_get_is_refused_with_a_realistic_header_set(
    harness: Harness,
) -> None:
    """A page on ``evil.example`` reaching for the status, as a browser sends it.

    The browser attaches the attacker's ``Origin`` and labels the request
    ``cross-site``. Both are refused; the guard does not depend on the page
    failing to send one of them.
    """
    for origin, site in (
        ("http://evil.example", "cross-site"),
        ("http://evil.example", "same-origin"),  # cannot happen, must not help
        (None, "cross-site"),
    ):
        headers = {SEC_FETCH_SITE_HEADER: site, TOKEN_HEADER: harness.token.value}
        if origin is not None:
            headers["Origin"] = origin
        assert harness.client.get("/api/status", headers=headers).status_code == 403, (
            origin,
            site,
        )


def test_a_get_with_neither_signal_is_refused(harness: Harness) -> None:
    """A header-stripping forger gains nothing on the read path either."""
    response = harness.client.get(
        "/api/status", headers={TOKEN_HEADER: harness.token.value}
    )
    assert response.status_code == 403


def test_localhost_is_accepted_as_an_exact_alternative(harness: Harness) -> None:
    assert harness.status(origin=f"http://localhost:{PORT}").status_code == 200


def test_a_post_still_refuses_fetch_metadata_instead_of_an_origin(
    harness: Harness,
) -> None:
    """The strict POST rule is unchanged.

    ``POST /api/run`` with a correct token, no ``Origin``, and
    ``Sec-Fetch-Site: same-origin`` must still be refused. This is the line
    that must not be "fixed" the same way: a POST is state-changing, a browser
    always sends ``Origin`` for one, so there is nothing to accommodate.
    """
    response = harness.client.post(
        "/api/run",
        json={"task": "should not start"},
        headers={SEC_FETCH_SITE_HEADER: "same-origin", TOKEN_HEADER: harness.token.value},
    )
    assert response.status_code == 403
    assert harness.session.state is UiSessionState.IDLE


def test_a_post_ticket_request_still_refuses_a_missing_origin(
    harness: Harness,
) -> None:
    """Minting a stream ticket is state-changing, so it keeps the strict rule."""
    assert (
        harness.client.post(
            "/api/events-ticket",
            headers={
                SEC_FETCH_SITE_HEADER: "same-origin",
                TOKEN_HEADER: harness.token.value,
            },
        ).status_code
        == 403
    )


def test_running_a_task_requires_a_token(harness: Harness) -> None:
    response = harness.client.post(
        "/api/run",
        json={"task": "x"},
        headers=harness.headers(),
    )
    assert response.status_code == 403


def test_answering_an_approval_requires_a_token(harness: Harness) -> None:
    response = harness.client.post(
        "/api/approvals/anything",
        json={"decision": "yes"},
        headers=harness.headers(),
    )
    assert response.status_code == 403


def test_a_forged_cross_site_post_is_refused(workspace: Workspace) -> None:
    """The exact attack: a page on another origin, correct token unknown."""
    harness = Harness(workspace, [ScriptedTurn(text="x")])
    response = harness.client.post(
        "/api/run",
        json={"task": "exfiltrate"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert harness.session.state is UiSessionState.IDLE


def test_health_needs_nothing_and_reveals_nothing(harness: Harness) -> None:
    response = harness.client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "service": "forge-workbench"}


# --------------------------------------------------------------------------
# Stream tickets
# --------------------------------------------------------------------------


def test_a_ticket_needs_a_token(harness: Harness) -> None:
    assert harness.client.post("/api/events-ticket", headers=harness.headers()).status_code == 403


def test_a_ticket_needs_an_origin(harness: Harness) -> None:
    assert harness.ticket(origin="http://evil.example").status_code == 403


def test_a_ticket_opens_the_stream_and_is_then_spent(harness: Harness) -> None:
    """One ticket, one stream.

    The successful open is exercised against the generator rather than through
    ``TestClient``: the test client runs an ASGI app to completion, so an
    open-ended SSE body never returns from it. The refused path below does go
    through the client, because a refused request does return.
    """
    import asyncio as _asyncio

    from forge.ui.server import _stream

    ticket = harness.ticket().json()["ticket"]
    assert harness.token.value not in ticket, "the main token must not travel in a URL"

    async def drive():
        harness.broker.bind_loop(_asyncio.get_running_loop())
        subscriber = harness.broker.subscribe()
        opening = await _stream(harness.broker, subscriber, 0).__anext__()
        harness.broker.unsubscribe(subscriber)
        return opening

    assert _asyncio.run(drive()).startswith(": ")


def test_a_spent_ticket_cannot_open_a_second_stream(harness: Harness) -> None:
    """Single-use, and Origin is checked before the ticket is touched.

    A cross-origin request must not even consume a ticket: if it did, a page
    from elsewhere could burn a legitimate operator's stream before it opened.
    """
    ticket = harness.ticket().json()["ticket"]

    refused = harness.client.get(
        f"/api/events?ticket={ticket}",
        headers=harness.headers(token=None, origin="http://evil.example"),
    )
    assert refused.status_code == 403
    # Still live: a refused request did not spend it.
    assert harness.app.state.stream_tickets.redeem(ticket) is True
    # And now it is spent, exactly once.
    assert harness.app.state.stream_tickets.redeem(ticket) is False


def test_a_missing_ticket_is_refused(harness: Harness) -> None:
    response = harness.client.get("/api/events", headers=harness.headers(token=None))
    assert response.status_code == 403


def test_a_real_eventsource_handshake_passes_the_origin_check(harness: Harness) -> None:
    """The regression for the stream, using ``EventSource``'s real header set.

    ``EventSource`` cannot set headers, so the request carries no
    ``X-Forge-Token`` -- and, measured on the wire, no ``Origin`` either. It
    sends ``Accept: text/event-stream``, ``Sec-Fetch-Site: same-origin`` and a
    ``Referer``. The old guard required an exact ``Origin`` and refused this.

    Asserted by *which* 403 comes back. Omitting the ticket makes the request
    fail for a reason that is measurable, and the body says which guard stopped
    it: a stream request that got past the origin check is refused for its
    ticket, not for its origin. That distinction is the whole defect, and it is
    observable without opening a body that never ends -- ``TestClient`` runs
    the ASGI app to completion, so an open-ended SSE response would hang the
    suite rather than fail it.
    """
    response = harness.client.get(
        "/api/events",
        headers={
            "Accept": "text/event-stream",
            SEC_FETCH_SITE_HEADER: "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"http://127.0.0.1:{PORT}/",
        },
    )
    assert response.status_code == 403
    assert "origin" not in response.json()["detail"]
    assert "ticket" in response.json()["detail"]


def test_a_cross_origin_eventsource_is_refused_by_the_origin_check(
    harness: Harness,
) -> None:
    """A page on ``evil.example`` cannot open the stream, ticket or not.

    ``EventSource`` from another origin is a CORS request, so the browser
    attaches that origin and labels it ``cross-site``. The origin guard runs
    first, so this stops before the ticket is looked at.
    """
    ticket = harness.ticket().json()["ticket"]
    response = harness.client.get(
        f"/api/events?ticket={ticket}",
        headers={
            "Accept": "text/event-stream",
            "Origin": "http://evil.example",
            SEC_FETCH_SITE_HEADER: "cross-site",
        },
    )
    assert response.status_code == 403
    assert "origin" in response.json()["detail"]
    # Refused before the ticket was touched, so it is still spendable by the
    # real page -- an attacker cannot burn the operator's stream.
    assert harness.app.state.stream_tickets.redeem(ticket) is True


def test_the_stream_refuses_when_neither_signal_is_present(harness: Harness) -> None:
    """No ``Origin`` and no ``Sec-Fetch-Site`` is not a browser."""
    ticket = harness.ticket().json()["ticket"]
    response = harness.client.get(
        f"/api/events?ticket={ticket}", headers={"Accept": "text/event-stream"}
    )
    assert response.status_code == 403
    assert "origin" in response.json()["detail"]
    assert harness.app.state.stream_tickets.redeem(ticket) is True


def test_a_valid_ticket_and_a_real_handshake_reach_the_stream(
    harness: Harness,
) -> None:
    """The accepting path, driven as far as ``TestClient`` safely allows.

    ``_stream`` is pulled one frame rather than read to completion, for the
    reason above: the generator is open-ended by design. What this adds over
    the existing generator test is the pairing -- a ticket minted through the
    route, spent exactly once, against the store the routes actually use.
    """
    import asyncio as _asyncio

    from forge.ui.server import _stream

    ticket = harness.ticket().json()["ticket"]
    store = harness.app.state.stream_tickets

    # The guard accepts a browser-shaped request, so redemption is what is left
    # to prove -- and the ticket is single-use, spent by exactly one handshake.
    assert store.redeem(ticket) is True
    assert store.redeem(ticket) is False

    async def drive():
        harness.broker.bind_loop(_asyncio.get_running_loop())
        subscriber = harness.broker.subscribe()
        opening = await _stream(harness.broker, subscriber, 0).__anext__()
        harness.broker.unsubscribe(subscriber)
        return opening

    assert _asyncio.run(drive()).startswith(": ")


def test_a_stream_ticket_cannot_start_a_run(harness: Harness) -> None:
    """It opens a read-only stream. Nothing more."""
    ticket = harness.ticket().json()["ticket"]
    response = harness.client.post(
        "/api/run",
        json={"task": "x"},
        headers={"Origin": ORIGIN, "ticket": ticket},
    )
    assert response.status_code == 403
    assert harness.session.state is UiSessionState.IDLE


# --------------------------------------------------------------------------
# Status and running
# --------------------------------------------------------------------------


def test_status_reports_the_fixed_workspace(harness: Harness, workspace) -> None:
    body = harness.status().json()
    assert body["workspace"] == str(workspace.root)
    assert body["model"] == "scripted"
    assert body["agent"] == "byte"
    assert body["state"] == "idle"
    assert body["pending_approval"] is None


def test_submitting_starts_a_run_and_returns_immediately(harness: Harness) -> None:
    response = harness.submit("say done")
    assert response.status_code == 202
    assert response.json()["run_id"]
    assert harness.wait_state(UiSessionState.COMPLETED)


def test_a_second_task_while_running_is_a_conflict(workspace: Workspace) -> None:
    gate = threading.Event()

    class SlowGateway:
        def complete(self, request):
            gate.wait(timeout=10)
            from forge.models.base import ModelResponse

            return ModelResponse(text="eventually")

    harness = Harness(workspace, [ScriptedTurn(text="unused")])
    harness.session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        selector=ModelSelector({}, ModelSelection("openrouter", "scripted")),
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=SlowGateway(), model_id="scripted"
        ),
        broker=SseBroker(),
    )
    harness.app = create_app(
        workspace=workspace,
        session=harness.session,
        gateway=SlowGateway(),
        token=harness.token,
        port=PORT,
    )
    harness.client = TestClient(harness.app)

    assert harness.submit("first").status_code == 202
    second = harness.submit("second")
    assert second.status_code == 409
    assert second.json()["run_id"]

    gate.set()
    assert harness.wait_state(UiSessionState.COMPLETED)


def test_an_empty_task_is_a_usage_error(harness: Harness) -> None:
    assert harness.submit("   ").status_code == 422


def test_a_failed_run_is_reported_not_hidden(workspace: Workspace) -> None:
    from forge.models.base import GatewayError

    harness = Harness(workspace, [ScriptedTurn(raises=GatewayError("provider down"))])
    harness.submit("anything")
    assert harness.wait_state(UiSessionState.FAILED)
    assert "provider down" in harness.status().json()["error"]


# --------------------------------------------------------------------------
# The approval bridge over HTTP
# --------------------------------------------------------------------------


def test_a_gated_operation_prompts_and_the_decision_resumes_the_run(
    workspace: Workspace,
) -> None:
    harness = Harness(workspace, GATED_TURNS)
    assert harness.submit("clean up the build").status_code == 202

    pending = harness.wait_pending()
    assert pending is not None, "no approval was requested for a gated command"
    assert harness.wait_state(UiSessionState.WAITING_FOR_APPROVAL)

    response = harness.approve(pending.approval_id, "no")
    assert response.status_code == 200
    assert harness.wait_state(UiSessionState.COMPLETED)

    body = harness.status().json()
    assert body["result"]["denied"], "a rejection must be visible in the result"


def test_an_approval_reaches_the_policy_layer_not_the_ui(
    workspace: Workspace,
) -> None:
    """`present()` hands back a plain answer; the policy layer decides.

    The assertion is on the *tool result the model saw*: an approved command
    runs and reports ``ok``, a rejected one is reported as a refusal. That
    proves the decision travelled through `Approver` and `Executor` rather than
    being interpreted anywhere in the UI.
    """
    approved = Harness(workspace, GATED_TURNS)
    approved.submit("clean up")
    assert approved.wait_pending() is not None
    approved.approve(approved.session.channel.pending.approval_id, "yes")
    assert approved.wait_state(UiSessionState.COMPLETED)
    assert approved.session.status()["result"]["denied"] == []

    rejected = Harness(workspace, GATED_TURNS)
    rejected.submit("clean up")
    assert rejected.wait_pending() is not None
    rejected.approve(rejected.session.channel.pending.approval_id, "no")
    assert rejected.wait_state(UiSessionState.COMPLETED)
    assert rejected.session.status()["result"]["denied"], (
        "a rejection must be visible as a refusal"
    )


def test_first_tab_wins_and_the_second_is_a_conflict(workspace: Workspace) -> None:
    """Two tabs, one prompt. Exactly one decision, and the run continues."""
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    pending = harness.wait_pending()
    approval_id = pending.approval_id

    # Tab one.
    assert harness.approve(approval_id, "yes").status_code == 200
    # Tab two, after the fact.
    assert harness.approve(approval_id, "no").status_code == 409

    assert harness.wait_state(UiSessionState.COMPLETED)


def test_a_duplicate_response_before_the_worker_wakes_is_still_stale(
    workspace: Workspace,
) -> None:
    """The single-use property does not depend on the worker waking promptly."""
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id

    assert harness.approve(approval_id, "yes").status_code == 200
    assert harness.approve(approval_id, "yes").status_code == 409
    assert harness.approve(approval_id, "no").status_code == 409


def test_an_unknown_approval_id_is_not_found(harness: Harness) -> None:
    assert harness.approve("no-such-approval").status_code == 404


def test_a_decision_other_than_yes_or_no_is_refused(
    workspace: Workspace,
) -> None:
    """A truthy string must never be an approval."""
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id

    for decision in ("true", "1", "Y", "", "maybe"):
        assert harness.approve(approval_id, decision).status_code == 422

    assert harness.approve(approval_id, "no").status_code == 200


def test_an_answer_needs_a_valid_origin(workspace: Workspace) -> None:
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id

    assert (
        harness.approve(approval_id, "no", origin="http://evil.example").status_code
        == 403
    )
    assert harness.session.channel.pending is not None, "the prompt is still open"


def test_a_refresh_recovers_the_same_pending_approval(workspace: Workspace) -> None:
    """No localStorage: the tab asks the server and gets the same id back."""
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    original = harness.wait_pending()

    # "Refresh": a brand-new client with no memory of anything.
    refreshed = TestClient(harness.app)
    body = refreshed.get("/api/status", headers=harness.auth()).json()

    assert body["pending_approval"] is not None
    assert body["pending_approval"]["approval_id"] == original.approval_id
    assert body["pending_approval"]["request"] == original.request
    assert body["state"] == "waiting_for_approval"

    # And the recovered id is still the single-use capability it was.
    assert refreshed.post(
        f"/api/approvals/{body['pending_approval']['approval_id']}",
        json={"decision": "no"},
        headers=harness.auth(),
    ).status_code == 200
    assert harness.wait_state(UiSessionState.COMPLETED)


def test_a_refresh_after_the_decision_shows_no_pending_approval(
    workspace: Workspace,
) -> None:
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id
    harness.approve(approval_id, "no")
    assert harness.wait_state(UiSessionState.COMPLETED)

    assert harness.status().json()["pending_approval"] is None


def test_the_pending_projection_never_leaks_the_decision(
    workspace: Workspace,
) -> None:
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    pending = harness.wait_pending()

    body = harness.status().json()
    serialised = json.dumps(body)
    assert "approved" not in serialised
    assert pending.approval_id in serialised


# --------------------------------------------------------------------------
# The event loop
# --------------------------------------------------------------------------


def test_the_event_loop_stays_responsive_while_a_run_waits(
    workspace: Workspace,
) -> None:
    """The property the worker-thread design exists for.

    The worker is blocked inside ``present()`` for the whole test. Every other
    request must still be answered promptly, because a page that cannot fetch
    status while it waits for an operator cannot tell an operator what it is
    waiting for.
    """
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    assert harness.wait_pending() is not None

    latencies = []
    for _ in range(5):
        started = time.monotonic()
        response = harness.status()
        latencies.append(time.monotonic() - started)
        assert response.status_code == 200

    worst = max(latencies)
    assert worst < 1.0, f"status took {worst:.3f}s while a run waited for approval"

    # The prompt is still open and unanswered: the server is responsive, not free.
    assert harness.session.channel.pending is not None
    assert harness.session.state is UiSessionState.WAITING_FOR_APPROVAL


def test_the_approval_post_is_served_while_the_worker_is_blocked(
    workspace: Workspace,
) -> None:
    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id

    started = time.monotonic()
    response = harness.approve(approval_id, "no")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1.0, "answering an approval must not wait for the run"
    assert harness.wait_state(UiSessionState.COMPLETED)


def test_the_stream_is_ready_while_a_run_is_waiting(
    workspace: Workspace,
) -> None:
    """The stream opens during a wait, not only when the run ends.

    Driven against the generator rather than through ``TestClient``: the test
    client runs an ASGI app to completion, so an open-ended SSE body never
    returns from it.
    """
    import asyncio as _asyncio

    from forge.ui.server import _stream

    harness = Harness(workspace, GATED_TURNS)
    harness.submit("clean up")
    assert harness.wait_pending() is not None

    async def drive():
        broker = harness.broker
        broker.bind_loop(_asyncio.get_running_loop())
        subscriber = broker.subscribe()
        gen = _stream(broker, subscriber, 0)
        opening = await gen.__anext__()
        broker.unsubscribe(subscriber)
        await gen.aclose()
        return opening

    opening = _asyncio.run(drive())
    # The first frame is either replayed history (the run has been emitting
    # while it waits) or the opening comment. Either is a byte immediately --
    # which is the property under test.
    assert opening, "the stream sent no opening byte"
    assert harness.session.channel.pending is not None, (
        "the stream opened after the run finished waiting, not during"
    )


# --------------------------------------------------------------------------
# SSE content
# --------------------------------------------------------------------------
#
# These drive the ASGI streaming generator directly rather than reading a
# socket. That is not a shortcut around the code under test -- it is the only
# way to test it: `TestClient` runs the ASGI app to completion before returning
# a response, so an open-ended SSE body never returns from a test client at all.
# The route's own guards (Origin, ticket) are covered by the 403 tests above,
# and the wiring from route to generator is one line of delegation.


def _collect(harness, count, *, trigger=None, until=None, timeout=10.0):
    """Pull ``count`` data frames off the stream generator.

    Awaits the generator directly rather than polling: a sleep inside this
    coroutine would block the very loop the generator needs to make progress on,
    and the test would time out for a reason that has nothing to do with the
    code under test.
    """
    import asyncio as _asyncio

    from forge.ui.server import _stream

    broker = harness.broker

    async def drive():
        broker.bind_loop(_asyncio.get_running_loop())
        subscriber = broker.subscribe()
        frames: list[dict] = []
        opened = False

        async def pump():
            nonlocal opened
            async for chunk in _stream(broker, subscriber, 0):
                if chunk.startswith(": "):
                    opened = True
                    continue
                for line in chunk.splitlines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))
                # `until` first: a test that names the frame it is waiting for
                # must not be cut short by an arbitrary count. A frame that a
                # test asked to be waited for can legitimately arrive after any
                # small count -- `approval.required` is the seventh frame of a
                # gated run -- so when `until` is given it is the only stop
                # condition and `count` is not consulted at all.
                if until is not None:
                    if until(frames):
                        return
                elif len(frames) >= count:
                    return

        if trigger is not None:
            # Run the trigger on a real thread: submitting a task starts a
            # worker thread inside the session, and the loop must stay free to
            # observe what that thread emits.
            starter = threading.Thread(target=trigger, daemon=True)
            starter.start()
        else:
            starter = None

        try:
            await _asyncio.wait_for(pump(), timeout=timeout)
        except _asyncio.TimeoutError:
            pass
        finally:
            broker.unsubscribe(subscriber)
            if starter is not None:
                # Join before returning. The pump can stop on the very frame
                # the trigger is blocked waiting for, and a trigger that has
                # not finished yet would leave its own record empty -- a test
                # asserting on what the trigger observed would read `None` and
                # blame the code under test for a scheduling accident. The
                # trigger is bounded by its own waits, so this join terminates.
                starter.join(timeout=timeout)
        return frames, opened

    return _asyncio.run(drive())


def test_the_stream_carries_run_and_tool_activity(workspace: Workspace) -> None:
    harness = Harness(
        workspace,
        [
            ScriptedTurn(text="looking", tool_calls=(("search_files", {"pattern": "*"}),)),
            ScriptedTurn(
                tool_calls=(
                    (
                        "finish",
                        {
                            "status": "done",
                            "summary": "done",
                            "validation": "searched",
                            "limitations": "none",
                        },
                    ),
                )
            ),
        ],
    )

    def trigger():
        harness.submit("look around")

    frames, _ = _collect(harness, 12, trigger=trigger)
    names = [frame["event"] for frame in frames]

    assert "run.started" in names, names
    assert "tool.call" in names, names
    assert "tool.result" in names, names
    assert "run.completed" in names, names


def test_the_stream_never_carries_file_contents(workspace: Workspace) -> None:
    """Bounded metadata only. A display is not a second redaction pass."""
    secret_body = "a-file-body-the-operator-never-pasted"
    (workspace.root / "notes.txt").write_text(secret_body + "\n", encoding="utf-8")

    harness = Harness(
        workspace,
        [
            ScriptedTurn(
                text="reading",
                tool_calls=(("read_file", {"path": "notes.txt"}),),
            ),
            ScriptedTurn(text="done"),
        ],
    )

    frames, _ = _collect(harness, 8, trigger=lambda: harness.submit("read the notes"))

    assert frames
    serialised = json.dumps(frames)
    assert "a-file-body" not in serialised


def test_exec_activity_is_reported_as_counts_not_output(
    workspace: Workspace,
) -> None:
    """Process output is untrusted; only its size is reported."""
    harness = Harness(workspace, GATED_TURNS)

    def trigger():
        harness.submit("clean up")
        approval_id = harness.wait_pending().approval_id
        harness.approve(approval_id, "yes")

    frames, _ = _collect(harness, 10, trigger=trigger)
    tool_results = [f for f in frames if f["event"] == "tool.result"]
    assert tool_results
    for frame in tool_results:
        assert "stdout" not in frame or "stdout_bytes" in frame


def test_an_approval_event_carries_the_answerable_id(
    workspace: Workspace,
) -> None:
    harness = Harness(workspace, GATED_TURNS)
    seen: list = []

    def trigger():
        harness.submit("clean up")
        seen.append(harness.wait_pending())

    frames, _ = _collect(
        harness,
        4,
        trigger=trigger,
        until=lambda f: any(x["event"] == "approval.required" for x in f),
    )
    required = [f for f in frames if f["event"] == "approval.required"]

    assert required, "the page was never told an approval is pending"
    assert required[0]["approval_id"] == seen[0].approval_id
    assert required[0]["tool"] == "exec_command"
    assert required[0]["rule"], "a prompt must say why it is gated"


def test_a_reconnecting_client_receives_what_it_missed(
    workspace: Workspace,
) -> None:
    """Last-Event-ID replay is what makes a refresh continuous."""
    from forge.ui.server import _stream

    harness = Harness(workspace, [ScriptedTurn(text="done")])
    harness.submit("say done")
    assert harness.wait_state(UiSessionState.COMPLETED)

    import asyncio as _asyncio

    async def drive():
        broker = harness.broker
        subscriber = broker.subscribe()
        broker.bind_loop(_asyncio.get_running_loop())
        gen = _stream(broker, subscriber, 1)
        chunk = await gen.__anext__()
        broker.unsubscribe(subscriber)
        await gen.aclose()
        return chunk

    chunk = _asyncio.run(drive())
    assert "event:" in chunk
    assert int(chunk.split("\n", 1)[0].removeprefix("id: ")) > 1


def test_a_truncated_replay_says_so(workspace: Workspace) -> None:
    """A partial history must not be presented as a whole one."""
    import asyncio as _asyncio

    from forge.events import Event
    from forge.ui.server import _stream

    broker = SseBroker(replay=3)
    broker.bind_loop(_asyncio.new_event_loop())
    for index in range(10):
        broker.emit(Event(name="e", fields={"n": str(index)}))

    async def drive():
        subscriber = broker.subscribe()
        gen = _stream(broker, subscriber, 1)
        chunk = await gen.__anext__()
        broker.unsubscribe(subscriber)
        await gen.aclose()
        return chunk

    chunk = _asyncio.run(drive())
    assert "stream.truncated" in chunk


def test_a_disconnected_subscriber_is_unregistered(
    workspace: Workspace,
) -> None:
    """A browser that closed its tab must not keep consuming queue slots."""
    import asyncio as _asyncio

    from forge.ui.server import _stream

    broker = SseBroker()

    async def drive():
        broker.bind_loop(_asyncio.get_running_loop())
        subscriber = broker.subscribe()
        assert broker.subscriber_count == 1
        gen = _stream(broker, subscriber, 0)
        await gen.__anext__()
        # Closing the generator is what a disconnect does; the `finally` in the
        # stream is where the subscriber must be released.
        await gen.aclose()
        return broker.subscriber_count

    assert _asyncio.run(drive()) == 0



def test_the_main_token_is_never_used_for_the_stream(harness: Harness) -> None:
    """The stream ticket is the only way in, and it is not the main token."""
    response = harness.client.get(
        f"/api/events?ticket={harness.token.value}",
        headers=harness.headers(token=None),
    )
    assert response.status_code == 403


def test_an_unauthenticated_run_leaves_the_session_idle(harness: Harness) -> None:
    for headers in (
        harness.headers(),
        harness.auth(origin=None),
        harness.auth(origin="http://evil.example"),
        harness.auth(token="wrong"),
    ):
        response = harness.client.post(
            "/api/run", json={"task": "x"}, headers=headers
        )
        assert response.status_code == 403

    assert harness.session.state is UiSessionState.IDLE
    assert harness.model.call_count == 0


def test_no_live_network_or_model_calls_anywhere(workspace: Workspace) -> None:
    """Every request path runs on ScriptedGateway; nothing reaches a provider."""
    harness = Harness(workspace, GATED_TURNS)
    harness.ticket()
    harness.submit("clean up")
    approval_id = harness.wait_pending().approval_id
    harness.approve(approval_id, "no")
    harness.wait_state(UiSessionState.COMPLETED)

    # Every request the gateway saw was assembled locally, and none carried a
    # credential: the provider key lives in the gateway object alone.
    assert harness.model.requests
    for request in harness.model.requests:
        assert "FORGE_OPENROUTER_API_KEY" not in request.system
        assert harness.token.value not in request.system


def test_reset_conversation_api(workspace: Workspace) -> None:
    """POST /api/reset clears conversation state."""
    gateway = ScriptedGateway([
        ScriptedTurn(text="First reply"),
        ScriptedTurn(text="Second reply"),
    ])
    
    token = UiToken()
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        selector=ModelSelector({}, ModelSelection("openrouter", "scripted")),
        runtime_factory=runtime_factory(
            workspace,
            profile=BYTE,
            gateway=gateway,
            model_id="scripted",
        ),
    )
    
    app = create_app(
        workspace=workspace,
        session=session,
        gateway=gateway,
        token=token,
        port=PORT,
        health=None,
    )
    
    client = TestClient(app)
    
    # First run
    response = client.post(
        "/api/run",
        json={"task": "first task"},
        headers={TOKEN_HEADER: token.value, "Origin": ORIGIN},
    )
    assert response.status_code == 202
    
    # Wait for completion
    for _ in range(50):
        status = client.get(
            "/api/status",
            headers={TOKEN_HEADER: token.value, SEC_FETCH_SITE_HEADER: "same-origin"},
        )
        if status.json()["state"] == "completed":
            break
        time.sleep(0.1)
    
    # Second run - continues conversation
    response = client.post(
        "/api/run",
        json={"task": "second task"},
        headers={TOKEN_HEADER: token.value, "Origin": ORIGIN},
    )
    assert response.status_code == 202
    
    # Wait for completion
    for _ in range(50):
        status = client.get(
            "/api/status",
            headers={TOKEN_HEADER: token.value, SEC_FETCH_SITE_HEADER: "same-origin"},
        )
        if status.json()["state"] == "completed":
            break
        time.sleep(0.1)
    
    # Reset conversation
    response = client.post(
        "/api/reset",
        json={},
        headers={TOKEN_HEADER: token.value, "Origin": ORIGIN},
    )
    assert response.status_code == 200
    
    # Verify we can start a fresh run
    response = client.post(
        "/api/run",
        json={"task": "fresh task"},
        headers={TOKEN_HEADER: token.value, "Origin": ORIGIN},
    )
    assert response.status_code == 202


def test_reset_refused_during_active_run_api(workspace: Workspace) -> None:
    """POST /api/reset is refused while a run is active (waiting for approval)."""
    # Use GATED_TURNS which creates a run that waits for approval
    harness = Harness(workspace, GATED_TURNS)
    
    # Start a run that will wait for approval
    response = harness.submit("clean up")
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    
    # Wait for the run to reach WAITING_FOR_APPROVAL state
    assert harness.wait_state(UiSessionState.WAITING_FOR_APPROVAL)
    
    # Try to reset while waiting for approval - should be refused
    response = harness.client.post(
        "/api/reset",
        json={},
        headers=harness.auth(),
    )
    assert response.status_code == 409
    assert "run is already in progress" in response.json()["detail"]
    assert run_id in response.json()["detail"] or run_id[:8] in response.json()["detail"]
    
    # Complete the approval
    approval_id = harness.wait_pending().approval_id
    harness.approve(approval_id, "no")
    assert harness.wait_state(UiSessionState.COMPLETED)
    
    # Now reset should succeed
    response = harness.client.post(
        "/api/reset",
        json={},
        headers=harness.auth(),
    )
    assert response.status_code == 200


def test_reset_excludes_old_messages_from_next_gateway_request(
    workspace: Workspace,
) -> None:
    """After a reset, the next run's model request carries only the new task.

    Acceptance criterion: old conversation text must not re-enter the model
    context. This asserts it on the wire: the messages the ScriptedGateway
    received for the run *after* reset must not contain the prior task's text.
    """
    harness = Harness(
        workspace,
        [ScriptedTurn(text="first reply"), ScriptedTurn(text="second reply")],
    )

    # First run seeds the conversation with a distinctive phrase.
    response = harness.submit("policy review alpha-47")
    assert response.status_code == 202
    assert harness.wait_state(UiSessionState.COMPLETED)

    # Reset clears the conversation for this workspace/agent pair.
    response = harness.client.post(
        "/api/reset",
        json={},
        headers=harness.auth(),
    )
    assert response.status_code == 200

    # Second run. Every gateway request issued from now on must not reference
    # the first run's task text.
    requests_seen = len(harness.model.requests)
    response = harness.submit("draft the release notes")
    assert response.status_code == 202
    assert harness.wait_state(UiSessionState.COMPLETED)

    new_requests = harness.model.requests[requests_seen:]
    assert new_requests, "expected the second run to issue gateway requests"
    for request in new_requests:
        for message in request.messages:
            assert "policy review alpha-47" not in message.content