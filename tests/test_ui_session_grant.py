"""Workbench integration for session-scoped test approvals.

Pins two layers:

*   The shared builder ``build_test_grant`` -- it returns exactly the ``TESTS``
    scope with the default 25-authorization budget and a 60-minute monotonic
    expiry, and it exhausts / expires at the unit level.
*   The Workbench wiring -- ``build_dispatcher``/``build_runtime``/
    ``runtime_factory`` thread the grant into the approver and default it to
    ``None`` (opt-out); the HTTP surface turns a checked ``session_tests`` field
    into a fresh per-run grant and never carries it into the next run.

Nothing here runs a real test framework. The HTTP tests drive a scripted model
whose tool call is ``pytest`` (an approval-required test runner); opt-in turns
that into a session authorization (no prompt), opt-out leaves it gated (a
prompt), and a second run does not inherit the first run's grant.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from fastapi.testclient import TestClient

from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.session_grant import (
    DEFAULT_SESSION_GRANT_BUDGET,
    DEFAULT_SESSION_GRANT_EXPIRY_SECONDS,
    ScopeKind,
    build_test_grant,
)
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.channel import WebApprovalChannel
from forge.ui.runner import build_dispatcher, build_runtime, runtime_factory
from forge.ui.security import TOKEN_HEADER, UiToken
from forge.ui.server import create_app
from forge.ui.session import UiSessionState, WorkbenchSession

PORT = 8766
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


# --------------------------------------------------------------------------
# build_test_grant: defaults, budget, expiry
# --------------------------------------------------------------------------


def test_build_test_grant_defaults_to_tests_budget_and_expiry() -> None:
    grant = build_test_grant()
    assert grant.scopes == frozenset({ScopeKind.TESTS})
    assert grant.budget == DEFAULT_SESSION_GRANT_BUDGET
    assert DEFAULT_SESSION_GRANT_BUDGET == 25
    assert grant.expires_at is not None
    remaining = grant.expires_at - time.monotonic()
    # Counted from *now*: a little below the ceiling (time already passed), but
    # unambiguously a 60-minute horizon rather than none or an hour-plus.
    assert 0 < remaining <= DEFAULT_SESSION_GRANT_EXPIRY_SECONDS


def test_build_test_grant_exhausts_its_budget() -> None:
    grant = build_test_grant(budget=1, expires_after=None)
    assert (
        grant.authorize(rule="cmd.repo_runner", program="pytest", args=(), is_shell=False)
        is not None
    )
    assert grant.remaining == 0
    assert (
        grant.authorize(rule="cmd.repo_runner", program="pytest", args=(), is_shell=False)
        is None
    )


def test_build_test_grant_honours_expiry() -> None:
    # A negative horizon is already in the past, so nothing should match.
    grant = build_test_grant(expires_after=-1.0)
    assert (
        grant.authorize(rule="cmd.repo_runner", program="pytest", args=(), is_shell=False)
        is None
    )


def test_build_test_grant_with_no_expiry_has_no_wall_clock_cap() -> None:
    grant = build_test_grant(expires_after=None)
    assert grant.expires_at is None
    assert (
        grant.authorize(rule="cmd.repo_runner", program="pytest", args=(), is_shell=False)
        is not None
    )


# --------------------------------------------------------------------------
# Runner threading: the grant reaches Approver, and defaults to opt-out
# --------------------------------------------------------------------------


def test_build_dispatcher_defaults_to_no_grant(workspace: Workspace) -> None:
    dispatcher = build_dispatcher(workspace, profile=BYTE, channel=WebApprovalChannel())
    assert dispatcher.approver.grant is None


def test_build_dispatcher_threads_the_grant(workspace: Workspace) -> None:
    grant = build_test_grant()
    dispatcher = build_dispatcher(
        workspace, profile=BYTE, channel=WebApprovalChannel(), grant=grant
    )
    assert dispatcher.approver.grant is grant


def test_build_runtime_defaults_to_no_grant(workspace: Workspace) -> None:
    runtime = build_runtime(
        workspace,
        profile=BYTE,
        gateway=ScriptedGateway([ScriptedTurn(text="hi")]),
        model_id="scripted",
        channel=WebApprovalChannel(),
        events=SseBroker(),
    )
    assert runtime.dispatcher.approver.grant is None


def test_runtime_factory_is_per_call_not_remembered(workspace: Workspace) -> None:
    factory = runtime_factory(
        workspace,
        profile=BYTE,
        gateway=ScriptedGateway([ScriptedTurn(text="hi")]),
        model_id="scripted",
    )

    first = factory(SseBroker(), WebApprovalChannel(), build_test_grant())
    assert first.dispatcher.approver.grant is not None

    # A later call with no grant must not inherit the earlier one -- the grant
    # is an argument, never a remembered default.
    second = factory(SseBroker(), WebApprovalChannel(), None)
    assert second.dispatcher.approver.grant is None


# --------------------------------------------------------------------------
# HTTP surface: opt-in, opt-out, isolation
# --------------------------------------------------------------------------

# A run whose model answers with one approval-required test runner, then prose.
PYTEST_TURNS = [
    ScriptedTurn(
        text="I'll run the tests.",
        tool_calls=(("exec_command", {"program": "pytest", "args": ["-q"]}),),
    ),
    ScriptedTurn(text="Tests ran."),
]


class GrantHarness:
    def __init__(self, workspace: Workspace, turns) -> None:
        self.token = UiToken()
        self.model = ScriptedGateway(turns)
        self.broker = SseBroker()
        self.session = WorkbenchSession(
            workspace=workspace,
            profile=BYTE,
            model_id="scripted",
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

    def auth(self) -> dict[str, str]:
        return {"Origin": ORIGIN, TOKEN_HEADER: self.token.value}

    def submit(self, task: str, *, session_tests: bool = False):
        return self.client.post(
            "/api/run",
            json={"task": task, "session_tests": session_tests},
            headers=self.auth(),
        )

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
            if self.session.channel.pending is not None:
                return self.session.channel.pending
            time.sleep(0.01)
        return None


def test_opt_in_authorizes_tests_without_a_prompt(workspace: Workspace) -> None:
    harness = GrantHarness(workspace, PYTEST_TURNS)
    assert harness.submit("run the tests", session_tests=True).status_code == 202
    assert harness.wait_state(UiSessionState.COMPLETED)
    # No prompt was ever raised: the test runner was session-authorized.
    assert harness.session.channel.pending is None


def test_opt_out_keeps_tests_gated_behind_a_prompt(workspace: Workspace) -> None:
    harness = GrantHarness(workspace, PYTEST_TURNS)
    assert harness.submit("run the tests").status_code == 202
    assert harness.wait_state(UiSessionState.WAITING_FOR_APPROVAL)
    assert harness.session.channel.pending is not None


def test_a_new_run_does_not_inherit_the_previous_grant(workspace: Workspace) -> None:
    # Four scripted turns: two runs' worth. Run 1 opts in (pytest authorized),
    # run 2 does not (pytest must prompt), proving the grant died with run 1.
    turns = [
        ScriptedTurn(
            text="run the tests",
            tool_calls=(("exec_command", {"program": "pytest", "args": ["-q"]}),),
        ),
        ScriptedTurn(text="first done."),
        ScriptedTurn(
            text="run them again",
            tool_calls=(("exec_command", {"program": "pytest", "args": ["-q"]}),),
        ),
        ScriptedTurn(text="second, gated."),
    ]
    harness = GrantHarness(workspace, turns)

    assert harness.submit("first", session_tests=True).status_code == 202
    assert harness.wait_state(UiSessionState.COMPLETED)
    assert harness.session.channel.pending is None

    # Second run, default (no opt-in flag): the grant did not carry over.
    assert harness.submit("second").status_code == 202
    assert harness.wait_state(UiSessionState.WAITING_FOR_APPROVAL)
    assert harness.session.channel.pending is not None