"""Security regression tests for session-scoped approval grants.

Two layers are pinned:

*   :class:`SessionGrant` in isolation -- it matches only the repository
    test-runner scope, never a denial rule, never a shell form, never a remote
    scope, and it exhausts its budget and honours its expiry.
*   Integration -- :class:`Approver` consults the grant only for an
    approval-required command and only after DENY and ALLOW are settled; the
    dispatcher executes a session-authorized command and records it in the
    event stream; ``grant=None`` is byte-identical to today's behaviour.

Nothing here runs a real test framework. The dispatcher test injects a stub
executor so the session-authorized path is exercised end to end without
launching pytest.
"""

from __future__ import annotations

import pathlib
import time
from types import SimpleNamespace

import pytest

from forge.models.base import ToolCall
from forge.tools.approval import (
    Approver,
    ApprovalDecision,
    ApprovalOutcome,
    ScriptedApprovalChannel,
)
from forge.tools.commands import Command, CommandClassifier, CommandVerdict
from forge.tools.dispatcher import ToolDispatcher, ToolStatus
from forge.tools.execution import ExecutionState
from forge.tools.session_grant import (
    DEFAULT_SESSION_GRANT_BUDGET,
    REMOTE_SCOPES,
    ScopeKind,
    SessionGrant,
)
from forge.tools.workspace import Workspace

TESTS = ScopeKind.TESTS


def _grant(
    scopes=frozenset({TESTS}),
    budget: int = 10,
    expires_at: float | None = None,
) -> SessionGrant:
    return SessionGrant(scopes=scopes, budget=budget, expires_at=expires_at)


# --------------------------------------------------------------------------
# SessionGrant: structural matching
# --------------------------------------------------------------------------


@pytest.mark.parametrize("program", ["pytest", "tox", "nox"])
def test_bare_test_programs_authorize(program: str) -> None:
    auth = _grant().authorize(
        rule="cmd.repo_runner", program=program, args=("tests/",), is_shell=False
    )
    assert auth is not None
    assert auth.scope is TESTS


def test_python_m_pytest_authorizes_via_module_rule() -> None:
    auth = _grant().authorize(
        rule="cmd.repo_runner_module",
        program="python",
        args=("-m", "pytest", "-q"),
        is_shell=False,
    )
    assert auth is not None
    assert auth.scope is TESTS


@pytest.mark.parametrize(
    "program,args",
    [
        ("npm", ("test",)),
        ("pnpm", ("test",)),
        ("yarn", ("test",)),
        ("cargo", ("test",)),
        ("go", ("test",)),
    ],
)
def test_test_subcommands_authorize(program: str, args: tuple[str, ...]) -> None:
    auth = _grant().authorize(
        rule="cmd.repo_runner", program=program, args=args, is_shell=False
    )
    assert auth is not None


@pytest.mark.parametrize(
    "program,args",
    [
        ("make", ()),
        ("make", ("build",)),
        ("npm", ("run", "dev")),
        ("npm", ("start",)),
        ("npm", ("rebuild",)),
        ("cargo", ("build",)),
        ("cargo", ("run",)),
        ("go", ("run", "/tmp/x")),
        ("go", ("generate",)),
    ],
)
def test_build_and_run_forms_never_authorize(
    program: str, args: tuple[str, ...]
) -> None:
    assert (
        _grant().authorize(
            rule="cmd.repo_runner", program=program, args=args, is_shell=False
        )
        is None
    )


@pytest.mark.parametrize(
    "rule,program,args",
    [
        ("cmd.git_commit", "git", ("commit", "-m", "x")),
        ("cmd.git_push", "git", ("push",)),
        ("cmd.delete_file", "rm", ("x",)),
        ("cmd.unknown", "some-tool", ()),
        ("cmd.net_egress", "ssh", ("spacmonkey", "cat /etc/shadow")),
    ],
)
def test_non_test_rules_never_authorize(
    rule: str, program: str, args: tuple[str, ...]
) -> None:
    assert (
        _grant().authorize(rule=rule, program=program, args=args, is_shell=False)
        is None
    )


def test_secret_rules_never_authorize() -> None:
    # A secret escalation replaces the deciding rule with the secret rule, so
    # the grant's rule matching must not recognise it. The literal is preserved
    # even though the exact secret rule id is producer-side.
    assert (
        _grant().authorize(
            rule="secret.src_env", program="grep", args=(), is_shell=False
        )
        is None
    )


def test_shell_form_never_authorizes() -> None:
    assert (
        _grant().authorize(
            rule="cmd.repo_runner", program="pytest", args=(), is_shell=True
        )
        is None
    )


def test_remote_scopes_never_authorize_regardless_of_grant() -> None:
    # Both remote capabilities are declared but never enabled. Even a grant
    # that names them must not authorise an SSH operation.
    for remote in REMOTE_SCOPES:
        grant = _grant(scopes=frozenset({remote}))
        assert (
            grant.authorize(
                rule="cmd.net_egress",
                program="ssh",
                args=("spacmonkey", "logs ollama"),
                is_shell=False,
            )
            is None
        )
    # And a grant mixing TESTS with a remote scope still refuses SSH.
    mixed = _grant(scopes=frozenset({TESTS, ScopeKind.REMOTE_READONLY}))
    assert (
        mixed.authorize(
            rule="cmd.net_egress", program="ssh", args=(), is_shell=False
        )
        is None
    )


def test_empty_scopes_authorize_nothing() -> None:
    assert (
        _grant(scopes=frozenset()).authorize(
            rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
        )
        is None
    )


def test_program_and_args_are_casefolded() -> None:
    assert (
        _grant().authorize(
            rule="cmd.repo_runner", program="Pytest", args=(), is_shell=False
        )
        is not None
    )
    assert (
        _grant().authorize(
            rule="cmd.repo_runner", program="npm", args=("TEST",), is_shell=False
        )
        is not None
    )


# --------------------------------------------------------------------------
# SessionGrant: budget and expiry
# --------------------------------------------------------------------------


def test_budget_decrements_and_exhausts() -> None:
    grant = _grant(budget=2)
    assert grant.authorize(
        rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
    )
    assert grant.authorize(
        rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
    )
    assert grant.remaining == 0
    assert (
        grant.authorize(
            rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
        )
        is None
    )


def test_non_matching_operations_do_not_spend_budget() -> None:
    grant = _grant(budget=2)
    assert (
        grant.authorize(
            rule="cmd.git_commit", program="git", args=("commit",), is_shell=False
        )
        is None
    )
    assert grant.remaining == 2


def test_wall_clock_expiry_stops_matching() -> None:
    grant = _grant(budget=10, expires_at=time.monotonic() + 1.0)
    assert grant.authorize(
        rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
    )
    grant = _grant(budget=10, expires_at=time.monotonic() - 1.0)
    assert (
        grant.authorize(
            rule="cmd.repo_runner", program="pytest", args=(), is_shell=False
        )
        is None
    )


def test_default_budget_is_bounded() -> None:
    assert DEFAULT_SESSION_GRANT_BUDGET > 0
    assert DEFAULT_SESSION_GRANT_BUDGET <= 25


# --------------------------------------------------------------------------
# Approver integration: the grant narrows only an approval-required prompt
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def classifier(workspace: Workspace) -> CommandClassifier:
    return CommandClassifier(workspace)


def _verdict(classifier: CommandClassifier, program: str, *args: str) -> CommandVerdict:
    return classifier.classify(Command(program=program, args=args))


def test_approver_grant_returns_session_authorized(classifier: CommandClassifier) -> None:
    approver = Approver(None, grant=_grant(budget=5))
    outcome = approver.submit_command(
        _verdict(classifier, "pytest", "tests/"),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    assert outcome.decision is ApprovalDecision.SESSION_AUTHORIZED
    assert outcome.decision.permitted
    assert not outcome.decision.human_decided
    assert outcome.scope == "tests"
    assert outcome.request is not None
    assert outcome.request.rule == "cmd.repo_runner"
    # The scoped authorization is still a single-spend, fingerprint-bound
    # capability, exactly like a human approval.
    outcome.claim()


def test_approver_without_grant_is_unchanged(classifier: CommandClassifier) -> None:
    pyt_v = _verdict(classifier, "pytest", "tests/")

    # No channel, no grant -> DENIED_UNATTENDED, as before.
    unattended = Approver(None).submit_command(
        pyt_v, agent="byte", workspace=classifier.workspace.root
    )
    assert unattended.decision is ApprovalDecision.DENIED_UNATTENDED
    assert unattended.scope is None

    # Human channel, no grant -> APPROVED by the operator, not scoped.
    human = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        pyt_v, agent="byte", workspace=classifier.workspace.root
    )
    assert human.decision is ApprovalDecision.APPROVED
    assert human.human_decided
    assert human.scope is None


@pytest.mark.parametrize(
    "program,args",
    [
        ("sudo", ("rm", "-rf", "/")),
        ("git", ("reset", "--hard")),
        ("env", ()),
    ],
)
def test_grant_never_widens_a_denial(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    approver = Approver(None, grant=_grant(budget=10))
    outcome = approver.submit_command(
        _verdict(classifier, program, *args),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert outcome.scope is None


def test_grant_does_not_authorize_recursive_delete(
    classifier: CommandClassifier,
) -> None:
    # `rm -rf src` is approval-required (an enumerable recursive delete), not a
    # denial -- but it is equally never a test runner, so the grant must leave
    # it gated rather than scope it away.
    approver = Approver(None, grant=_grant(budget=10))
    outcome = approver.submit_command(
        _verdict(classifier, "rm", "-rf", "src"),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    assert outcome.decision is not ApprovalDecision.SESSION_AUTHORIZED
    assert outcome.scope is None


def test_grant_does_not_change_secret_source_prompts(classifier: CommandClassifier) -> None:
    # A command whose deciding rule is not a test runner (here: an ordinary
    # network egress) must still reach the channel, not be scoped away.
    approver = Approver(None, grant=_grant(budget=10))
    outcome = approver.submit_command(
        _verdict(classifier, "ssh", "spacmonkey", "true"),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    assert outcome.scope is None


# --------------------------------------------------------------------------
# Dispatcher integration: execution and audit
# --------------------------------------------------------------------------


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:  # noqa: ANN001 - mirrors forge.events.EventSink
        self.events.append(event)

    def names(self) -> list[str]:
        return [e.name for e in self.events]


class _StubExecutor:
    """Records the outcome it is handed and fakes a completed execution."""

    def __init__(self) -> None:
        self.calls: list[ApprovalOutcome] = []

    def execute(self, outcome: ApprovalOutcome, request) -> SimpleNamespace:  # noqa: ANN001
        self.calls.append(outcome)
        return SimpleNamespace(
            state=ExecutionState.COMPLETED,
            exit_code=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            error=None,
        )


def test_session_authorized_command_executes_and_is_audited(workspace: Workspace) -> None:
    sink = _RecordingSink()
    stub = _StubExecutor()
    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(None, grant=_grant(budget=5)),
        executor=stub,  # type: ignore[arg-type] - duck-typed stub Executor
        agent_name="byte",
        allowed_tools=["exec_command"],
        events=sink,
    )

    result = dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="exec_command",
            arguments='{"program": "pytest", "args": ["-q"]}',
        )
    )

    assert result.status is ToolStatus.OK
    # The stub received the session-authorized outcome, not a denial.
    assert len(stub.calls) == 1
    assert stub.calls[0].decision is ApprovalDecision.SESSION_AUTHORIZED

    # The operation is recorded in the existing event stream.
    assert "tool.authorized" in sink.names()
    authorized = next(e for e in sink.events if e.name == "tool.authorized")
    assert authorized.get("scope") == "tests"
    assert authorized.get("rule") == "cmd.repo_runner"
    assert authorized.get("tool") == "exec_command"


def test_human_approval_is_not_recorded_as_scoped(workspace: Workspace) -> None:
    sink = _RecordingSink()
    stub = _StubExecutor()
    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel(["y"])),
        executor=stub,  # type: ignore[arg-type] - duck-typed stub Executor
        agent_name="byte",
        allowed_tools=["exec_command"],
        events=sink,
    )
    dispatcher.dispatch(
        ToolCall(
            id="c1",
            name="exec_command",
            arguments='{"program": "pytest", "args": ["-q"]}',
        )
    )
    # A human approval emits no "tool.authorized" event -- that marker is
    # reserved for session-scoped authorizations only.
    assert "tool.authorized" not in sink.names()
    assert stub.calls[0].decision is ApprovalDecision.APPROVED


# --------------------------------------------------------------------------
# Cross-layer pin: the grant's rule literals stay coupled to the classifier
# --------------------------------------------------------------------------


def test_test_runner_rule_ids_still_exist_in_classifier() -> None:
    """The grant matches rule ids as literals; a rename in ``commands.py`` must
    surface here rather than silently narrowing the grant to nothing."""
    from forge.tools.commands import APPROVAL_PROGRAMS

    ids = {rule.rule_id for rule in APPROVAL_PROGRAMS}
    assert "cmd.repo_runner" in ids
    assert "cmd.repo_runner_module" in ids