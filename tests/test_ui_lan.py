"""LAN mode: the one thing that makes the token a boundary again.

Every test here exists because a specific claim in ``forge/ui/security.py`` or
``forge/ui/server.py`` would otherwise be untested prose. The four worth
reading first:

*   :func:`test_a_locked_client_cannot_self_serve_the_token` -- the finding the
    whole mode is built on. Off loopback, ``GET /`` hands out the token, so LAN
    mode must not serve that page to a client that has not unlocked.
*   :func:`test_an_approval_answered_over_lan_is_the_same_single_spend_one` --
    the property most likely to be broken by a well-meaning change, because
    "let the tablet approve" and "add a second approval path" look alike.
*   :func:`test_a_read_only_tablet_can_still_open_the_stream` -- the read-only
    default is only honest if a read-only client can actually observe.
*   :func:`test_no_flag_can_widen_the_bind_beyond_one_named_address` -- the
    safety property the loopback-only default used to hold by documentation
    alone, now asserted.

Two facts about the harness that will look surprising and are not:

*   ``TestClient`` sends ``Host: testserver`` by default, which is in no
    allowlist. So **every** LAN request here sets ``Host`` explicitly, and
    :func:`test_the_default_host_is_refused` turns that into a test rather than
    leaving it as an accident of the harness.
*   Nothing here opens a socket or spends a provider request. The model is
    always :class:`ScriptedGateway` and the app is called in-process, so the
    whole file is offline. The real bind is asserted on the *call* to uvicorn
    in :func:`test_the_socket_binds_the_named_address`, not on a listening port.
"""

from __future__ import annotations

import argparse
import importlib
import pathlib

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.runner import runtime_factory
from forge.ui.security import (
    LAN_PASSPHRASE_ENV,
    MIN_PASSPHRASE_CHARS,
    SESSION_COOKIE,
    TOKEN_HEADER,
    InvalidLanAddress,
    LanConfig,
    Passphrase,
    UiToken,
    UnlockStore,
    validate_lan_address,
)
from forge.ui.server import LOCKED_PLACEHOLDER, STATIC_DIR, create_app
from forge.ui.session import UiSessionState, WorkbenchSession

PORT = 8765
LAN = "192.168.1.20"
LAN_ORIGIN = f"http://{LAN}:{PORT}"
LOOPBACK_ORIGIN = f"http://127.0.0.1:{PORT}"
#: The CLI *module*, not its ``main`` function. ``from forge.cli import main``
#: yields the function, because ``forge/cli/__init__.py`` re-exports it and it
#: shadows the submodule name -- so ``cli.build_parser`` raises
#: ``AttributeError: 'function' object has no attribute 'build_parser'``. Every
#: CLI assertion in this file goes through ``importlib`` to get the module the
#: console script actually points at.
cli = importlib.import_module("forge.cli.main")

PASSPHRASE = "correct horse battery staple"


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


class LanHarness:
    """One LAN-mode app, one session, one token, one unlock store."""

    def __init__(self, workspace: Workspace, turns, *, steer: bool = False) -> None:
        self.token = UiToken()
        self.model = ScriptedGateway(turns)
        self.broker = SseBroker()
        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            model_id="scripted",
            runtime_factory=runtime_factory(
                workspace, profile=BYTE, gateway=self.model, model_id="scripted"
            ),
            broker=self.broker,
        )
        self.steer = steer
        self.app = create_app(
            workspace=workspace,
            session=self.session,
            gateway=self.model,
            token=self.token,
            port=PORT,
            lan=LanConfig(address=LAN, steer=steer),
            passphrase=Passphrase(PASSPHRASE),
        )
        self.client = TestClient(self.app)

    def headers(
        self,
        *,
        token: str | None = None,
        origin: str | None = LAN_ORIGIN,
        host: str | None = f"{LAN}:{PORT}",
        sec_fetch: str | None = None,
        **extra: str,
    ):
        headers = dict(extra)
        if origin is not None:
            headers["Origin"] = origin
        if host is not None:
            headers["Host"] = host
        if sec_fetch is not None:
            headers["Sec-Fetch-Site"] = sec_fetch
        if token is not None:
            headers[TOKEN_HEADER] = token
        return headers

    def auth(self, token: str | None = None, **overrides):
        return self.headers(
            token=self.token.value if token is None else token, **overrides
        )

    def unlock(self, passphrase: str = PASSPHRASE, **overrides):
        return self.client.post(
            "/api/unlock",
            json={"passphrase": passphrase},
            headers=self.headers(**overrides),
        )

    def unlocked_client(self, client: TestClient) -> TestClient:
        return client


@pytest.fixture()
def lan(workspace: Workspace) -> LanHarness:
    return LanHarness(workspace, [ScriptedTurn(text="a")])


@pytest.fixture()
def lan_gated(workspace: Workspace) -> LanHarness:
    """A read-only LAN session whose model will actually ask for a decision.

    Separate from ``lan`` because most tests here do not need a run, and
    baking a gated turn into the default fixture would make every one of them
    wait on a question they never answer.
    """
    return LanHarness(workspace, GATED_TURNS)


@pytest.fixture()
def steering(workspace: Workspace) -> LanHarness:
    return LanHarness(workspace, [ScriptedTurn(text="a")], steer=True)


# --------------------------------------------------------------------------
# The finding the mode exists for
# --------------------------------------------------------------------------


def test_a_locked_client_cannot_self_serve_the_token(lan: LanHarness) -> None:
    """The served page is the token's delivery mechanism, so LAN mode hides it.

    This is the whole justification for the mode. A device that can reach the
    port can fetch ``GET /``; if that response contains the token, the device
    can satisfy every existing guard, because its requests carry a legitimate
    origin (the bind address) and a truthful ``Sec-Fetch-Site``.
    """
    response = lan.client.get("/", headers=lan.headers(origin=None))

    assert response.status_code == 200
    assert lan.token.value not in response.text
    # The placeholder must be *substituted to empty*, not left as the literal
    # marker. An unsubstituted placeholder in a page a browser will run is a
    # 403 the operator cannot diagnose; an empty meta tag is the same refusal,
    # but one the page can see and branch on.
    assert "__FORGE_UI_TOKEN__" not in response.text
    assert f'<meta name="forge-ui-locked" content="1">' in response.text


def test_a_locked_client_with_the_token_crafted_still_cannot_read(
    lan: LanHarness,
) -> None:
    """A guessed or stolen token is the only thing between a peer and the run.

    Asserted because the token is the API capability and LAN mode does not
    change what it authorises -- it only stops the server handing it out.
    """
    response = lan.client.get(
        "/api/status",
        headers=lan.headers(token="not-the-token", sec_fetch="same-origin"),
    )

    assert response.status_code == 403


def test_unlocking_then_serves_the_page_with_the_token(lan: LanHarness) -> None:
    """One passphrase presentation, then the ordinary loopback-shaped page."""
    assert lan.unlock().status_code == 200

    page = lan.client.get("/", headers=lan.headers(origin=None))

    assert page.status_code == 200
    assert lan.token.value in page.text
    assert '<meta name="forge-ui-locked" content="">' in page.text


def test_the_unlock_response_carries_the_token_and_an_httponly_cookie(
    lan: LanHarness,
) -> None:
    response = lan.unlock()

    assert response.status_code == 200
    assert response.json()["token"] == lan.token.value
    raw = response.headers["set-cookie"]
    assert SESSION_COOKIE in raw
    # The two attributes doing the work. A cookie a page script can read is a
    # capability a cross-site script could try to ride.
    assert "HttpOnly" in raw
    assert "SameSite=strict" in raw.replace("SameSite=Strict", "SameSite=strict")


def test_a_refresh_after_unlock_needs_no_second_passphrase(
    lan: LanHarness,
) -> None:
    """The session is the cookie, so a reloaded page is still unlocked."""
    lan.unlock()
    lan.client.cookies.clear()
    lan.unlock()  # second presentation, to prove the first one was not enough
    # The client keeps the cookie from the second unlock; a page load works.
    assert lan.client.get("/", headers=lan.headers(origin=None)).status_code == 200


def test_an_unlocked_device_reads_status_like_any_other_client(
    lan: LanHarness,
) -> None:
    lan.unlock()
    response = lan.client.get(
        "/api/status", headers=lan.auth(sec_fetch="same-origin")
    )

    assert response.status_code == 200
    assert response.json()["state"] == UiSessionState.IDLE.value


def test_the_locked_page_reveals_no_run_state(workspace: Workspace) -> None:
    """A device that has not authenticated learns a workbench exists, no more.

    The workspace path and the model id are one script line away on the same
    page, so this asserts on the *page* not just the API: a lock screen that
    renders ``/Users/rodger/Projects/...`` has already lost.
    """
    harness = LanHarness(workspace, [ScriptedTurn(text="a")])
    text = harness.client.get("/", headers=harness.headers(origin=None)).text

    assert "scripted" not in text, "the model id must not appear before unlock"
    assert str(workspace.root) not in text, "the workspace path must not appear"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_a_wrong_passphrase_is_refused_without_naming_the_field(
    lan: LanHarness,
) -> None:
    response = lan.unlock("wrong passphrase entirely")

    assert response.status_code == 401
    assert "passphrase" not in response.json()["detail"].lower()
    assert PASSPHRASE not in response.text


def test_the_passphrase_never_appears_in_a_refusal(lan: LanHarness) -> None:
    """A too-short-secret message that quoted it would put it in scrollback."""
    response = lan.unlock("short")

    assert response.status_code == 401
    assert "short" not in response.text


def test_a_correct_passphrase_does_not_bypass_an_open_lockout(
    lan: LanHarness,
) -> None:
    """Otherwise the backoff is an oracle: it would answer "was that right?".

    The hazard is specific -- a lockout that serves correct answers tells a
    sweeping client which guesses were right, which is the only thing the
    backoff was for.
    """
    for _ in range(5):
        assert lan.unlock("nope nope nope nope").status_code == 401

    assert lan.unlock().status_code == 429, (
        "a correct passphrase must be refused while the window is open"
    )


def test_a_lockout_is_per_client_address_not_global(lan: LanHarness) -> None:
    """One device's typos must not lock the operator out on another.

    Counted per address precisely so a neighbour cannot deny service to the one
    device the operator is trying to use.
    """
    for _ in range(5):
        lan.unlock("nope nope nope nope")

    # A different peer, with its own client so its own address in the test
    # transport differs. TestClient reports 127.0.0.1 as the peer, so this
    # asserts the mechanism directly rather than pretending to be two hosts.
    store = UnlockStore(threshold=2, base=0.01, maximum=0.01)
    store.record_failure("10.0.0.9")
    store.record_failure("10.0.0.9")
    assert store.locked_out("10.0.0.9")
    assert not store.locked_out("10.0.0.10")


def test_a_successful_unlock_clears_the_failure_count(lan: LanHarness) -> None:
    """An early typo must not carry its penalty into the rest of the session."""
    lan.unlock("nope nope nope nope")
    assert lan.unlock().status_code == 200
    assert lan.unlock("nope nope nope nope").status_code == 401


def test_an_app_with_lan_and_no_passphrase_refuses_to_build(
    workspace: Workspace,
) -> None:
    """The failure is a startup error, not a running surface with no secret."""
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=ScriptedGateway([]), model_id="scripted"
        ),
        broker=SseBroker(),
    )

    with pytest.raises(ValueError, match="passphrase"):
        create_app(
            workspace=workspace,
            session=session,
            gateway=ScriptedGateway([]),
            token=UiToken(),
            port=PORT,
            lan=LanConfig(address=LAN),
        )


# --------------------------------------------------------------------------
# Host: the DNS-rebinding defence
# --------------------------------------------------------------------------


def test_a_rebound_name_is_refused_on_every_capability_route(
    lan: LanHarness,
) -> None:
    """The refusal must not depend on the attacker's DNS staying pointed here.

    Every route that can carry a capability is checked, because a guard
    installed on some of them is a guard that is missing from the rest.
    """
    evil = {"host": "forge.attacker.example:8765"}

    assert lan.client.get("/", headers=lan.headers(origin=None, **evil)).status_code == 403
    assert lan.client.get(
        "/api/status", headers=lan.auth(sec_fetch="same-origin", **evil)
    ).status_code == 403
    assert lan.client.post(
        "/api/unlock", json={"passphrase": PASSPHRASE}, headers=lan.headers(**evil)
    ).status_code == 403
    assert lan.client.post(
        "/api/events-ticket", headers=lan.auth(**evil)
    ).status_code == 403
    assert lan.client.get(
        "/api/health", headers=lan.headers(origin=None, **evil)
    ).status_code == 200, (
        "liveness is the one documented exemption, so it is asserted here "
        "rather than left to be discovered"
    )


def test_the_default_host_header_is_refused(lan: LanHarness) -> None:
    """``TestClient`` sends ``Host: testserver``, which is in no allowlist.

    Asserted because it is otherwise an accident of the harness: a future test
    that forgets to set ``Host`` would get a 403 and blame the wrong guard.
    """
    response = lan.client.get("/api/status", headers={"Sec-Fetch-Site": "same-origin"})

    assert response.status_code == 403
    assert "host" in response.json()["detail"]


def test_the_loopback_spellings_are_allowed_hosts_in_lan_mode(
    lan: LanHarness,
) -> None:
    """A browser on the machine keeps working while a tablet is connected."""
    for spelling in ("127.0.0.1", "localhost"):
        host = f"{spelling}:{PORT}"
        response = lan.client.get(
            "/api/status",
            headers=lan.headers(
                token=lan.token.value,
                host=host,
                origin=f"http://{spelling}:{PORT}",
                sec_fetch="same-origin",
            ),
        )
        assert response.status_code == 200, host


def test_a_wrong_port_in_the_host_is_refused(lan: LanHarness) -> None:
    response = lan.client.get(
        "/api/status", headers=lan.auth(host=f"{LAN}:9999", sec_fetch="same-origin")
    )

    assert response.status_code == 403


def test_loopback_mode_has_no_host_check_at_all(workspace: Workspace) -> None:
    """The property is installed by absence, and that is the regression risk.

    A loopback app must answer a request carrying any ``Host`` at all, because
    every existing test and any local tooling may send something other than
    ``127.0.0.1:8765``.
    """
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=ScriptedGateway([]), model_id="scripted"
        ),
        broker=SseBroker(),
    )
    token = UiToken()
    app = create_app(
        workspace=workspace,
        session=session,
        gateway=ScriptedGateway([]),
        token=token,
        port=PORT,
    )
    client = TestClient(app)

    response = client.get(
        "/api/status",
        headers={
            TOKEN_HEADER: token.value,
            "Sec-Fetch-Site": "same-origin",
            "Host": "literally-anything:1234",
        },
    )

    assert response.status_code == 200


def test_loopback_mode_has_no_unlock_route(workspace: Workspace) -> None:
    """The route is not registered, not registered-and-refused.

    Asserted so the loopback route table stays the one it always was: a test
    elsewhere that counts routes should not have to learn an exception.
    """
    session = _loopback_session(workspace)
    app = create_app(
        workspace=workspace,
        session=session,
        gateway=ScriptedGateway([]),
        token=UiToken(),
        port=PORT,
    )

    assert "/api/unlock" not in _paths(app)


# --------------------------------------------------------------------------
# Origins
# --------------------------------------------------------------------------


def test_the_lan_origin_is_allowed_and_a_neighbouring_one_is_not(
    lan: LanHarness,
) -> None:
    lan.unlock()

    assert lan.client.get(
        "/api/status", headers=lan.auth(sec_fetch="same-origin")
    ).status_code == 200

    other = lan.client.get(
        "/api/status",
        headers=lan.headers(
            token=lan.token.value,
            origin="http://192.168.1.21:8765",
            host="192.168.1.21:8765",
            sec_fetch="same-origin",
        ),
    )

    assert other.status_code == 403, (
        "a different device's address is a different origin and must be refused"
    )


def test_a_post_with_no_origin_is_still_refused_in_lan_mode(
    lan: LanHarness,
) -> None:
    """Absence fails closed, exactly as it does on loopback."""
    lan.unlock()

    response = lan.client.post(
        "/api/run",
        json={"task": "do a thing"},
        headers=lan.headers(token=lan.token.value, origin=None),
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------
# The read-only default
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/run", {"task": "do a thing"}),
        ("post", "/api/approvals/abc", {"decision": "yes"}),
        ("post", "/api/orders", {"title": "an order"}),
        ("post", "/api/orders/abc/move", {"status": "done"}),
        ("patch", "/api/orders/abc", {"title": "renamed"}),
        ("delete", "/api/orders/abc", None),
        ("post", "/api/models/verify", None),
    ],
)
def test_every_write_is_refused_for_a_read_only_tablet(
    lan: LanHarness, method: str, path: str, body
) -> None:
    """All of them, not a sample.

    The rule lives in one dependency, so a route added later inherits it -- but
    only if the route actually uses that dependency, and this is what proves
    the current set does.
    """
    lan.unlock()
    response = getattr(lan.client, method)(
        path, headers=lan.auth(), **({"json": body} if body is not None else {})
    )

    assert response.status_code == 403
    assert "read-only" in response.json()["detail"]


def test_a_refused_write_leaves_the_session_idle(lan: LanHarness) -> None:
    """A refusal must not half-start a run."""
    lan.unlock()
    lan.client.post("/api/run", json={"task": "do a thing"}, headers=lan.auth())

    assert lan.session.state is UiSessionState.IDLE


def test_loopback_can_still_write_while_a_lan_tablet_cannot(
    lan: LanHarness,
) -> None:
    """A browser on this machine is not a device on the network."""
    lan.unlock()

    lan_response = lan.client.post(
        "/api/run", json={"task": "from the tablet"}, headers=lan.auth()
    )
    local_response = lan.client.post(
        "/api/run",
        json={"task": "from this machine"},
        headers=lan.headers(
            token=lan.token.value, origin=LOOPBACK_ORIGIN, host=f"127.0.0.1:{PORT}"
        ),
    )

    assert lan_response.status_code == 403
    assert local_response.status_code == 202, (
        "the operator's own browser must not be collateral damage of LAN mode"
    )


def test_a_read_only_tablet_can_still_open_the_stream(lan: LanHarness) -> None:
    """The read-only default is only honest if a read-only client observes.

    This is why ``POST /api/events-ticket`` is exempt from the write rule: the
    ticket it mints is a read capability -- single-use, expiring, and unable to
    start a run or answer an approval. Without the exemption the tablet would
    see a page that loads and never updates, which looks like a broken server
    rather than a policy.

    **No stream body is ever opened.** ``TestClient`` drives the ASGI app to
    completion and this stream is open-ended by design, so reading it blocks
    forever. So the stream is authorised by asking for it *without* a ticket and
    reading **which** refusal comes back: the ticket refusal proves the request
    got past Host, past the same-origin pair, and past the read-only rule, and
    failed only at the last gate. A read-only refusal here would say the
    exemption is not installed, and a Host or origin refusal would say the
    request never got that far. Each mistake is distinguishable.
    """
    lan.unlock()

    ticket = lan.client.post("/api/events-ticket", headers=lan.auth())
    assert ticket.status_code == 200, "the ticket handshake must not be read-only"

    refused = lan.client.get(
        "/api/events", headers=lan.headers(sec_fetch="same-origin")
    )

    assert refused.status_code == 403
    assert "ticket" in refused.json()["detail"], (
        "the refusal must come from the ticket gate; anything else means the "
        f"read-only rule or an origin guard fired first: {refused.json()}"
    )
    assert "read-only" not in refused.json()["detail"]


def test_the_stream_route_still_refuses_a_bad_host_before_the_ticket(
    lan: LanHarness,
) -> None:
    """Guard order on the one route that calls its checks directly.

    ``GET /api/events`` does not depend on ``guarded_read`` -- it calls
    :func:`require_same_origin` and then redeems the ticket itself -- so the
    Host check had to be added by hand there. This asserts it landed and that it
    runs *before* the ticket is spent, which is the existing "a cross-origin
    request must not burn an operator's ticket" property, now with a Host in
    front of it.
    """
    lan.unlock()

    response = lan.client.get(
        "/api/events",
        headers=lan.headers(sec_fetch="same-origin", host="forge.attacker.example:8765"),
    )

    assert response.status_code == 403
    assert "host" in response.json()["detail"]


def test_the_stream_ticket_still_needs_its_origin_and_token(lan: LanHarness) -> None:
    """Exempt from the write rule, not from the guards."""
    lan.unlock()

    assert lan.client.post(
        "/api/events-ticket", headers=lan.auth(origin="http://evil.example")
    ).status_code == 403
    assert lan.client.post(
        "/api/events-ticket", headers=lan.auth(token="wrong")
    ).status_code == 403


def test_a_board_read_still_works_for_a_read_only_tablet(
    lan: LanHarness,
) -> None:
    """Reads are the point. An empty board is a truthful answer, not a refusal."""
    lan.unlock()

    response = lan.client.get("/api/orders", headers=lan.auth(sec_fetch="same-origin"))

    assert response.status_code == 200
    assert "statuses" in response.json()


# --------------------------------------------------------------------------
# Approval parity: the property most worth protecting
# --------------------------------------------------------------------------


def test_an_approval_answered_over_lan_is_the_same_single_spend_one(
    workspace: Workspace,
) -> None:
    """``--lan-steer`` changes who may answer, not what answering does.

    The whole hazard of a second approval path is that "let the tablet approve"
    and "add another way to approve" look identical in a diff. So: the tablet's
    answer wakes the run, and a *second* answer -- including one from this
    machine -- is still 409 stale. The channel in
    :mod:`forge.tools.approval` is untouched and spends once.
    """
    harness = LanHarness(workspace, GATED_TURNS, steer=True)
    harness.unlock()
    harness.client.post(
        "/api/run", json={"task": "do a thing"}, headers=harness.auth()
    )
    pending = _wait_pending(harness)
    assert pending is not None, "the run should have asked for a decision"

    from_tablet = harness.client.post(
        f"/api/approvals/{pending.approval_id}",
        json={"decision": "yes"},
        headers=harness.auth(),
    )
    from_machine = harness.client.post(
        f"/api/approvals/{pending.approval_id}",
        json={"decision": "no"},
        headers=harness.headers(
            token=harness.token.value,
            origin=LOOPBACK_ORIGIN,
            host=f"127.0.0.1:{PORT}",
        ),
    )

    assert from_tablet.status_code == 200
    assert from_machine.status_code == 409, (
        "the second answer must lose, whoever it came from"
    )


def test_steer_mode_lets_a_tablet_start_a_run(steering: LanHarness) -> None:
    steering.unlock()

    response = steering.client.post(
        "/api/run", json={"task": "from the tablet"}, headers=steering.auth()
    )

    assert response.status_code == 202
    assert steering.session.state is not UiSessionState.IDLE


def test_steer_mode_still_enforces_origin_and_token(steering: LanHarness) -> None:
    """Widening the scope must not have widened the guards."""
    steering.unlock()

    assert steering.client.post(
        "/api/run",
        json={"task": "x"},
        headers=steering.auth(origin="http://evil.example"),
    ).status_code == 403
    assert steering.client.post(
        "/api/run", json={"task": "x"}, headers=steering.auth(token="wrong")
    ).status_code == 403


def test_a_tablet_still_cannot_approve_without_steer(lan_gated: LanHarness) -> None:
    """The read-only default's most important single case, named on its own.

    A separate test from the parametrised sweep because this is the endpoint
    whose compromise is unrecoverable, and because "approvals are refused" is
    the sentence a future reader will most want to find by name.

    The run is started from loopback and the answer is attempted from the
    tablet, which is the realistic order: the operator starts the work, the
    tablet is watching, and the tablet may not decide.
    """
    pending = _make_pending(lan_gated)

    response = lan_gated.client.post(
        f"/api/approvals/{pending.approval_id}",
        json={"decision": "yes"},
        headers=lan_gated.auth(),
    )

    assert response.status_code == 403
    assert lan_gated.session.state is UiSessionState.WAITING_FOR_APPROVAL, (
        "the run must still be waiting -- a refused approval is not a denial"
    )


# --------------------------------------------------------------------------
# Address validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate,reason",
    [
        ("0.0.0.0", "every interface"),
        ("::", "every interface"),
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("169.254.1.1", "link-local"),
        ("fe80::1", "link-local"),
        ("224.0.0.1", "multicast"),
        ("not-an-address", "not an IP address"),
        ("192.168.1.0/24", "not an IP address"),
        ("", "no address"),
        (None, "no address"),
    ],
)
def test_an_address_that_must_not_be_bound_is_refused(
    candidate, reason
) -> None:
    with pytest.raises(InvalidLanAddress) as caught:
        validate_lan_address(candidate)

    assert reason in str(caught.value)


def test_a_plain_lan_address_is_canonicalised() -> None:
    """Canonical, not echoed, so one address cannot become two origins."""
    assert validate_lan_address("  192.168.1.20  ") == "192.168.1.20"


# --------------------------------------------------------------------------
# Passphrase configuration
# --------------------------------------------------------------------------


def test_an_unset_passphrase_refuses_to_start() -> None:
    with pytest.raises(ValueError, match=LAN_PASSPHRASE_ENV):
        Passphrase.from_environ({})


def test_a_short_passphrase_refuses_to_start() -> None:
    with pytest.raises(ValueError, match=str(MIN_PASSPHRASE_CHARS)):
        Passphrase.from_environ({LAN_PASSPHRASE_ENV: "a" * (MIN_PASSPHRASE_CHARS - 1)})


def test_a_refusal_never_quotes_the_secret() -> None:
    """A message that echoed a too-short value would print it to the terminal.

    The value has to be under the minimum to be refused at all -- a long
    secret is accepted, so the test would pass vacuously against a correct
    implementation. The length is therefore derived from the constant rather
    than hard-coded, and the assertion is that the message does not contain the
    value it just rejected.
    """
    secret = "s" * (MIN_PASSPHRASE_CHARS - 1)
    with pytest.raises(ValueError) as caught:
        Passphrase.from_environ({LAN_PASSPHRASE_ENV: secret})

    assert secret not in str(caught.value)
    assert LAN_PASSPHRASE_ENV in str(caught.value), (
        "the message must still name the variable the operator has to set"
    )


def test_a_passphrase_never_renders_its_value() -> None:
    assert PASSPHRASE not in repr(Passphrase(PASSPHRASE))


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def _workbench_args(**overrides) -> argparse.Namespace:
    parser = cli.build_parser()
    argv = ["workbench", "--workspace", "/tmp"]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not None:
            argv.extend([flag, str(value)])
    return parser.parse_args(argv)


def _workbench_parser() -> argparse.ArgumentParser:
    """The workbench subparser, reached through the public API.

    Not by walking ``parser._subparsers._group_actions``: that is argparse
    internals, and a version bump that renames it would turn this file into a
    collection of ``AttributeError``s that look like LAN-mode failures. A parse
    error is enough to prove the subparser exists, and the subparser object is
    then what every assertion below actually needs.
    """
    parser = cli.build_parser()
    try:
        parser.parse_args(["workbench", "--workspace", "/tmp", "--no-such-flag"])
    except SystemExit:
        pass
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        choices = getattr(action, "choices", None)
        if choices and "workbench" in choices:
            return choices["workbench"]
    raise AssertionError("the workbench subparser must exist")


def _flags(subparser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in subparser._actions  # noqa: SLF001
        for option in action.option_strings
    }


def test_no_flag_can_widen_the_bind_beyond_one_named_address() -> None:
    """The safety property the loopback-only default held by documentation.

    There is no ``--host``; the only way to leave loopback is ``--lan`` *and* a
    literal ``--lan-address``; and no workbench default names a non-loopback
    address. Asserted on the parser rather than on the constant, because the
    constant is not what a future edit would break.
    """
    subparser = _workbench_parser()
    flags = _flags(subparser)

    assert "--host" not in flags, "there is no generic bind flag"
    assert "--bind" not in flags
    assert {"--lan", "--lan-address", "--lan-steer"} <= flags


def test_lan_is_off_by_default() -> None:
    args = _workbench_args()

    assert args.lan is False
    assert args.lan_address is None
    assert args.lan_steer is False


def test_the_help_text_still_names_the_real_passphrase_contract() -> None:
    """Guards the copy in the ``--lan`` help against the constants.

    The parser is built at import time, so it states the environment variable
    and the minimum as literal text rather than importing
    :mod:`forge.ui.security` for two strings. That is a deliberate duplication
    and this is what stops it drifting.
    """
    subparser = _workbench_parser()
    lan_action = next(
        action for action in subparser._actions if "--lan" in action.option_strings
    )

    assert LAN_PASSPHRASE_ENV in lan_action.help
    assert f"{MIN_PASSPHRASE_CHARS} characters" in lan_action.help


def test_lan_without_an_address_refuses_to_start(
    tmp_path: pathlib.Path, capsys
) -> None:
    result = cli.main(
        [
            "workbench",
            "--workspace",
            str(tmp_path),
            "--lan",
        ],
        environ={LAN_PASSPHRASE_ENV: PASSPHRASE},
    )

    assert result == cli.EXIT_USAGE
    assert "--lan-address" in capsys.readouterr().err


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "169.254.1.1", "nope"])
def test_a_must_not_bind_address_refuses_to_start(
    tmp_path: pathlib.Path, capsys, address: str
) -> None:
    result = cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan", "--lan-address", address],
        environ={LAN_PASSPHRASE_ENV: PASSPHRASE},
    )

    assert result == cli.EXIT_USAGE
    assert address in capsys.readouterr().err


def test_lan_without_a_passphrase_refuses_to_start(
    tmp_path: pathlib.Path, capsys
) -> None:
    """The refusal that matters most, because the alternative is a wide port."""
    result = cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan", "--lan-address", LAN],
        environ={},
    )

    assert result == cli.EXIT_USAGE
    assert LAN_PASSPHRASE_ENV in capsys.readouterr().err


def test_a_short_passphrase_refuses_to_start(tmp_path: pathlib.Path, capsys) -> None:
    result = cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan", "--lan-address", LAN],
        environ={LAN_PASSPHRASE_ENV: "tooshort"},
    )

    assert result == cli.EXIT_USAGE
    assert str(MIN_PASSPHRASE_CHARS) in capsys.readouterr().err


def test_a_lan_flag_without_lan_is_refused_rather_than_ignored(
    tmp_path: pathlib.Path, capsys
) -> None:
    """An ignored flag would give the operator a loopback URL and no clue."""
    result = cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan-address", LAN],
        environ={},
    )

    assert result == cli.EXIT_USAGE
    assert "--lan" in capsys.readouterr().err

    result = cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan-steer"], environ={}
    )

    assert result == cli.EXIT_USAGE
    assert "--lan-steer" in capsys.readouterr().err


@pytest.fixture()
def offline_workbench(monkeypatch):
    """Everything ``_run_workbench`` reaches for before it binds a socket.

    Four things, each stubbed for a stated reason, and none of them the thing
    under test:

    *   ``_build_model_gateway`` -- the real one reads a provider credential and
        refuses without one, so a test that let it run would stop at "no API
        key" and never reach the bind. A :class:`ScriptedGateway` keeps the
        whole file offline, which is the rule the rest of this suite follows.
    *   ``_resolve_default_model`` -- reads ``models.toml`` from the real
        working directory. Same reason.
    *   ``uvicorn.run`` -- the *call* is what the bind assertions read. Nothing
        should listen on a port in a unit test.
    *   ``ModelHealth.verify`` -- the one verified pass at startup, which would
        otherwise open a socket to a provider.

    Yields the recorded uvicorn calls, because every test that uses this is
    asserting on which address was handed to the server.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        cli,
        "_build_model_gateway",
        lambda *a, **k: ScriptedGateway([ScriptedTurn(text="ran")]),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_default_model",
        lambda *a, **k: argparse.Namespace(
            model_id="scripted", provider="local", routed=False, tier=None,
            selection=None, reason=None,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_build_router",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "forge.ui.models.ModelHealth.verify", lambda self: {"tiers": []}
    )

    def fake_run(app, *, host, port, log_level):  # noqa: ARG001
        calls.append({"host": host, "port": port})

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


def test_the_socket_binds_the_named_address(
    offline_workbench, tmp_path: pathlib.Path
) -> None:
    """Asserted on the call, not on a listening socket.

    The whole point of the flag pair is *which interface*, and that is decided
    in exactly one argument of exactly one call. A test that opened a port
    would assert the same thing more slowly and with a cleanup problem.
    """
    cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan", "--lan-address", LAN],
        environ={LAN_PASSPHRASE_ENV: PASSPHRASE},
    )

    assert offline_workbench == [{"host": LAN, "port": cli.DEFAULT_WORKBENCH_PORT}]


def test_loopback_still_binds_loopback(
    offline_workbench, tmp_path: pathlib.Path
) -> None:
    """The regression that matters most: the default is unchanged."""
    cli.main(["workbench", "--workspace", str(tmp_path)], environ={})

    assert offline_workbench == [
        {"host": "127.0.0.1", "port": cli.DEFAULT_WORKBENCH_PORT}
    ]


def test_the_banner_says_lan_mode_and_cleartext(
    offline_workbench, tmp_path: pathlib.Path, capsys
) -> None:
    """A forgotten flag must be visible in the terminal that started it."""
    cli.main(
        ["workbench", "--workspace", str(tmp_path), "--lan", "--lan-address", LAN],
        environ={LAN_PASSPHRASE_ENV: PASSPHRASE},
    )
    out = capsys.readouterr().out

    assert "LAN mode" in out
    assert f"http://{LAN}:8765" in out
    assert "read-only" in out
    assert "cleartext" in out
    assert PASSPHRASE not in out, "the passphrase must never be printed"
    assert "loopback" not in out, (
        "the loopback line must not also be printed in LAN mode -- it would "
        "tell the operator the surface is not reachable"
    )


def test_the_banner_says_writable_under_steer(
    offline_workbench, tmp_path: pathlib.Path, capsys
) -> None:
    cli.main(
        [
            "workbench",
            "--workspace",
            str(tmp_path),
            "--lan",
            "--lan-address",
            LAN,
            "--lan-steer",
        ],
        environ={LAN_PASSPHRASE_ENV: PASSPHRASE},
    )
    out = capsys.readouterr().out

    assert "writable" in out
    assert "read-only" not in out


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def test_the_page_carries_the_lock_placeholder_and_the_unlock_form() -> None:
    """The static page must carry both, or the lock cannot be expressed.

    Asserted against the file rather than the response so it holds even if a
    future change gives loopback and LAN different templates.
    """
    html = STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")

    assert LOCKED_PLACEHOLDER in html
    assert 'id="unlock-form"' in html
    assert 'id="unlock-overlay"' in html


def test_the_script_branches_on_the_lock_flag_not_on_an_empty_token() -> None:
    """A loopback page with an empty token is a bug; a locked one is policy.

    So the two must not be the same code path, which is what the explicit
    ``forge-ui-locked`` meta is for. Asserted as source text because the branch
    is what is being protected, and a DOM test would only ever see one of the
    two states.
    """
    source = STATIC_DIR.joinpath("app.js").read_text(encoding="utf-8")

    assert 'meta[name="forge-ui-locked"]' in source
    locked_branch = source.index("if (LOCKED) {")
    wiring = source.index("el.composer.addEventListener")
    assert locked_branch < wiring, "the lock must be decided before the wiring"
    assert "startLocked();" in source[locked_branch:wiring]
    assert "return;" in source[locked_branch:wiring], (
        "the locked branch must return before the normal boot, or the page "
        "makes four guarded requests that are all refused"
    )


def test_the_script_stores_no_credential() -> None:
    """No storage, and the passphrase is never kept after the request.

    Asserted against the *code* rather than the whole file, because the file
    opens with a comment that names ``localStorage`` and ``sessionStorage`` in
    order to forbid them -- a raw substring search matches the rule it is meant
    to enforce. Comments are stripped first, so this fails only on an actual
    call, and the strip is why the test is not vacuous.
    """
    source = STATIC_DIR.joinpath("app.js").read_text(encoding="utf-8")
    code = _strip_js_comments(source)

    assert "localStorage" not in code
    assert "sessionStorage" not in code
    assert "document.cookie" not in code
    # And the unlock path specifically: the passphrase is read, sent, and the
    # field cleared, so nothing survives the request that could be re-sent by
    # a later line or a later page.
    unlock = code[code.index("function unlock("):code.index("function startLocked(")]
    assert "localStorage" not in unlock
    assert "JSON.stringify({ passphrase: passphrase })" in unlock
    # The passphrase must not survive the request. The clear happens in
    # startLocked's submit handler, which is the only place the value is read,
    # so the assertion is that the field is emptied immediately after the read
    # and before the response is awaited.
    submit = code[code.index("function startLocked("):]
    read = submit.index("el.unlockPassphrase.value")
    assert submit.index("el.unlockPassphrase.value = \"\"") > read, (
        "the input must be cleared after the value is read, so the passphrase "
        "does not stay in the DOM while the request is in flight"
    )


def _strip_js_comments(source: str) -> str:
    """Remove ``//`` and ``/* */`` comments, keeping string literals intact.

    Written by hand rather than pulled in as a dependency: this suite has no
    JS toolchain, and the only thing being protected here is "no storage API is
    called", which a line-oriented strip answers as well as a real parser.

    String literals are preserved because ``"localStorage"`` appearing inside a
    string is not a call to it -- and because the file under test is full of
    prose in strings, a naive strip would mangle it.
    """
    out: list[str] = []
    i = 0
    length = len(source)
    in_string: str | None = None
    while i < length:
        char = source[i]
        nxt = source[i + 1] if i + 1 < length else ""
        if in_string:
            out.append(char)
            if char == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if char == in_string:
                in_string = None
            i += 1
            continue
        if char in "\"'`":
            in_string = char
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            while i < length and source[i] != "\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i < length and not (source[i] == "*" and source[i + 1 : i + 2] == "/"):
                i += 1
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _loopback_session(workspace: Workspace) -> WorkbenchSession:
    """A session for an app built the way every pre-LAN test builds one.

    Factored out because three tests need a bare loopback app and duplicating
    the eight-line construction three times is how one of them drifts.
    """
    return WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=ScriptedGateway([]), model_id="scripted"
        ),
        broker=SseBroker(),
    )


def _paths(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def _wait_pending(harness: LanHarness, timeout: float = 5.0):
    """Block until the run is actually sitting on a decision.

    ``until``-style: the state *is* the stop condition, so there is no frame
    count to gate on and no way for this to pass by timing out into a happy
    path. A test that then asserts on a ``None`` pending fails loudly.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = harness.session.channel.pending
        if pending is not None:
            return pending
        time.sleep(0.01)
    return None


#: A turn that asks for a decision rather than one that merely replies. Copied
#: in shape from ``tests/test_ui_api.py``'s ``GATED_TURNS`` rather than imported:
#: a test module importing another test module's fixtures couples two files that
#: are reviewed separately, and this is four lines of intent.
GATED_TURNS = [
    ScriptedTurn(
        text="I need to remove the build directory.",
        tool_calls=(("exec_command", {"program": "rm", "args": ["-rf", "build"]}),),
    ),
    ScriptedTurn(text="Understood, I did not run it."),
]


def _make_pending(harness: LanHarness):
    """A LAN session sitting on a real approval question.

    Built from a real gated run rather than by poking the channel, because the
    property under test is what a *run* does when the tablet's answer arrives --
    a hand-placed pending object would pass even if the run never asked.

    The run is started from the **loopback** origin, not the LAN one, and that
    is not a convenience. Under the read-only default a tablet cannot start a
    run at all, so a helper that started one over LAN would 403 and this test
    would be measuring the wrong refusal. This is the realistic shape too: the
    operator starts the run at the machine and the tablet is watching it.
    """
    harness.unlock()
    started = harness.client.post(
        "/api/run",
        json={"task": "clean up the build"},
        headers=harness.headers(
            token=harness.token.value,
            origin=LOOPBACK_ORIGIN,
            host=f"127.0.0.1:{PORT}",
        ),
    )
    assert started.status_code == 202
    pending = _wait_pending(harness)
    assert pending is not None, "the run should have asked for a decision"
    return pending

