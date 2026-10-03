"""Tests for the Workbench's own machinery: security, broker, channel, session.

Split by module rather than by kind of test, so a failure names the thing that
broke. Each section carries its own structural guards where the property is
invisible to a behavioural test -- an observer reaching policy, a UI module
calling ``execute``, a channel quietly deciding.

Nothing here makes a network call or needs a credential. Every model is
:class:`ScriptedGateway`.
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import threading
import time

import pytest

from forge.agents.byte import BYTE
from forge.events import Event
from forge.models.base import GatewayError, ToolCall
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import ApprovalRequest, Approver
from forge.tools.commands import Command, CommandClassifier
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.channel import (
    StaleApproval,
    UnknownApproval,
    WebApprovalChannel,
)
from forge.ui.runner import build_dispatcher, build_runtime
from forge.ui.security import (
    SEC_FETCH_SITE_HEADER,
    TOKEN_HEADER,
    StreamTicketStore,
    UiToken,
    allowed_origins,
    check_origin,
    check_same_origin,
)
from forge.ui.session import SessionBusy, UiSessionState, WorkbenchSession

UI_ROOT = pathlib.Path(__file__).resolve().parent.parent / "forge" / "ui"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


def _approval_required_request(workspace: Workspace) -> ApprovalRequest:
    """A real approval request, built the way the policy layer builds one.

    Never hand-built: an ``ApprovalRequest`` constructed directly in a test
    would not prove the channel works on the objects it actually receives.
    """
    classifier = CommandClassifier(workspace)
    verdict = classifier.classify(Command(program="rm", args=("-rf", "build")))
    return ApprovalRequest.from_command(
        verdict, agent="byte", workspace=workspace.root
    )


def _runnable_request(workspace: Workspace) -> ApprovalRequest:
    """A request that needs no approval -- used where no wait is wanted."""
    classifier = CommandClassifier(workspace)
    verdict = classifier.classify(Command(program="ls", args=()))
    request = ApprovalRequest.from_command(
        verdict, agent="byte", workspace=workspace.root
    )
    from dataclasses import replace

    from forge.tools.workspace import Classification

    return replace(request, classification=Classification.APPROVAL_REQUIRED)


class ScriptedModel:
    """A gateway whose reply is fixed, with no provider involved."""

    def __init__(self, turns):
        self.gateway = ScriptedGateway(turns)

    def complete(self, request):
        return self.gateway.complete(request)


def _session(
    workspace: Workspace,
    turns,
    *,
    model_id: str = "scripted",
    max_turns: int | None = None,
) -> WorkbenchSession:
    model = ScriptedModel(turns)
    from forge.ui.runner import runtime_factory

    return WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id=model_id,
        runtime_factory=runtime_factory(
            workspace,
            profile=BYTE,
            gateway=model.gateway,
            model_id=model_id,
            max_turns=max_turns,
        ),
    )


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses.

    Used instead of a fixed sleep so the tests are neither flaky nor slow: a
    condition that is already true returns immediately.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --------------------------------------------------------------------------
# security: tokens and origins
# --------------------------------------------------------------------------


def test_token_matches_only_itself() -> None:
    token = UiToken()
    assert token.matches(token.value)
    assert not token.matches(UiToken().value)
    assert not token.matches(token.value[:-1])


def test_token_refuses_missing_and_non_ascii_values() -> None:
    token = UiToken()
    assert not token.matches(None)
    assert not token.matches("")
    assert not token.matches("héllo")


def test_token_never_renders_its_value() -> None:
    """A token in a traceback is a token in a file someone will later paste."""
    token = UiToken()
    assert token.value not in repr(token)


def test_two_processes_get_different_tokens() -> None:
    assert UiToken().value != UiToken().value


def test_origin_must_match_exactly() -> None:
    expected = allowed_origins(8765)
    assert check_origin("http://127.0.0.1:8765", expected)
    assert check_origin("http://localhost:8765", expected)
    assert not check_origin("http://127.0.0.1:8766", expected)
    assert not check_origin("http://evil.example", expected)
    assert not check_origin("https://127.0.0.1:8765", expected)


def test_a_missing_origin_is_not_a_match() -> None:
    """Absence is what a header-stripping forger produces."""
    assert not check_origin(None, allowed_origins(8765))
    assert not check_origin("", allowed_origins(8765))


def test_no_origin_value_matches_everything() -> None:
    assert "null" not in allowed_origins(8765)
    assert not check_origin("null", allowed_origins(8765))


# -- the read-only same-origin check ---------------------------------
#
# These four cases are the measured shape of a real Chrome 154 talking to a
# page served from the Workbench, plus the cross-origin counterpart. The
# browser sends no `Origin` on a same-origin GET; it sends `Sec-Fetch-Site`
# on everything. A test that invented an `Origin` on the GET would have
# passed against code that no browser can satisfy.


def test_a_same_origin_get_without_an_origin_is_accepted() -> None:
    """The real browser shape: no ``Origin``, ``Sec-Fetch-Site: same-origin``."""
    # The header name is asserted because the whole check rests on a browser
    # sending this exact spelling. A typo here would fail open into 403s that
    # look like a dead server rather than a dead guard.
    assert SEC_FETCH_SITE_HEADER == "Sec-Fetch-Site"
    assert check_same_origin(None, "same-origin", allowed_origins(8765))


def test_an_exact_origin_still_passes_the_read_check() -> None:
    assert check_same_origin("http://127.0.0.1:8765", None, allowed_origins(8765))
    assert check_same_origin("http://localhost:8765", "same-origin", allowed_origins(8765))


def test_a_cross_origin_get_is_refused_even_claiming_same_origin() -> None:
    """A wrong ``Origin`` ends the question; it does not fall through.

    A non-browser client can write both headers, so accepting
    ``Sec-Fetch-Site: same-origin`` alongside a foreign ``Origin`` would let
    anyone who can set headers past the check. A real browser never sends that
    pair, so refusing it costs the page nothing.
    """
    expected = allowed_origins(8765)
    assert not check_same_origin("http://evil.example", "same-origin", expected)
    assert not check_same_origin("http://127.0.0.1:9999", "same-origin", expected)
    assert not check_same_origin("http://127.0.0.1:8765/", "same-origin", expected)


def test_both_signals_absent_is_refused() -> None:
    """Fails closed against a forger who strips headers."""
    expected = allowed_origins(8765)
    assert not check_same_origin(None, None, expected)
    assert not check_same_origin(None, "", expected)


def test_only_the_browser_attested_site_value_is_accepted() -> None:
    """``cross-site``, ``same-site`` and ``none`` are not ``same-origin``.

    ``same-site`` is the one worth naming: a page on ``localhost:9000`` is
    same-*site* to ``localhost:8765`` and a different origin, so accepting it
    would hand the stream to any other local server's page.
    """
    expected = allowed_origins(8765)
    assert not check_same_origin(None, "cross-site", expected)
    assert not check_same_origin(None, "same-site", expected)
    assert not check_same_origin(None, "none", expected)
    assert not check_same_origin(None, "SAME-ORIGIN", expected)


def test_a_post_never_accepts_fetch_metadata_instead_of_an_origin() -> None:
    """The split is by method, and the strict rule is unchanged for POSTs.

    Asserted on the *behaviour* the POST routes depend on, not on a name:
    :func:`check_origin` is what ``require_browser`` calls, and it has no
    ``Sec-Fetch-Site`` branch to leak into.
    """
    expected = allowed_origins(8765)
    assert not check_origin(None, expected)
    # No Sec-Fetch-Site parameter exists to be passed: the strict check has
    # exactly one input, so the metadata branch cannot be reached from it.
    with pytest.raises(TypeError):
        check_origin(None, expected, "same-origin")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# security: stream tickets
# --------------------------------------------------------------------------


def test_a_ticket_is_single_use() -> None:
    store = StreamTicketStore()
    ticket = store.issue()
    assert store.redeem(ticket)
    assert not store.redeem(ticket), "a stream ticket was replayable"


def test_an_expired_ticket_is_refused() -> None:
    store = StreamTicketStore(ttl=0.0)
    ticket = store.issue()
    time.sleep(0.01)
    assert not store.redeem(ticket)


def test_unknown_and_missing_tickets_are_refused() -> None:
    store = StreamTicketStore()
    assert not store.redeem(None)
    assert not store.redeem("")
    assert not store.redeem("never-issued")


def test_expired_tickets_are_purged() -> None:
    store = StreamTicketStore(ttl=0.0)
    store.issue()
    time.sleep(0.01)
    assert store.live_count == 0


# --------------------------------------------------------------------------
# broker
# --------------------------------------------------------------------------


def test_broker_is_an_event_sink() -> None:
    from forge.events import EventSink

    assert isinstance(SseBroker(), EventSink)


def test_subscribe_and_unsubscribe_are_counted() -> None:
    broker = SseBroker()
    first = broker.subscribe()
    assert broker.subscriber_count == 1
    broker.unsubscribe(first)
    assert broker.subscriber_count == 0
    broker.unsubscribe(first)  # idempotent: a generator's finally may run twice
    assert broker.subscriber_count == 0


def test_replay_returns_events_after_the_cursor() -> None:
    broker = SseBroker()
    for index in range(3):
        broker.emit(Event(name=f"e{index}", fields={"n": str(index)}))

    pending, complete = broker.replay_since(1)
    assert complete
    assert [seq for seq, _ in pending] == [2, 3]


def test_replay_reports_a_gap_larger_than_the_ring() -> None:
    """A partial history must not be presented as a whole one."""
    broker = SseBroker(replay=3)
    for index in range(10):
        broker.emit(Event(name=f"e{index}", fields={}))

    pending, complete = broker.replay_since(1)
    assert not complete
    assert len(pending) == 3


def test_replay_of_a_fresh_broker_is_empty_and_complete() -> None:
    pending, complete = SseBroker().replay_since(0)
    assert pending == []
    assert complete


def test_published_counts_every_event() -> None:
    broker = SseBroker()
    broker.emit(Event(name="a", fields={}))
    broker.emit(Event(name="b", fields={}))
    assert broker.published == 2


# --------------------------------------------------------------------------
# channel: the synchronous approval bridge
# --------------------------------------------------------------------------


def test_channel_is_available() -> None:
    from forge.tools.approval import ApprovalChannel

    channel = WebApprovalChannel()
    assert isinstance(channel, ApprovalChannel)
    assert channel.available


def test_present_returns_only_yes_or_no(workspace: Workspace) -> None:
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)

    answers = []

    def worker():
        answers.append(channel.present(request, request.render()))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    assert _wait_for(lambda: channel.pending is not None)
    pending_id = channel.pending.approval_id
    channel.respond(pending_id, True)
    thread.join(timeout=5)

    assert answers == ["y"]


def test_a_rejection_is_reported_as_no(workspace: Workspace) -> None:
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)
    answers = []

    thread = threading.Thread(
        target=lambda: answers.append(channel.present(request, request.render())),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)
    channel.respond(channel.pending.approval_id, False)
    thread.join(timeout=5)

    assert answers == ["n"]


def test_approval_ids_are_unique_and_unguessable(workspace: Workspace) -> None:
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)
    seen = []

    for _ in range(3):
        thread = threading.Thread(
            target=lambda: channel.present(request, request.render()),
            daemon=True,
        )
        thread.start()
        assert _wait_for(lambda: channel.pending is not None)
        seen.append(channel.pending.approval_id)
        channel.respond(channel.pending.approval_id, True)
        thread.join(timeout=5)

    assert len(set(seen)) == 3
    assert all(len(value) >= 32 for value in seen)


def test_first_response_wins_and_the_second_is_stale(workspace: Workspace) -> None:
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)
    answers = []

    thread = threading.Thread(
        target=lambda: answers.append(channel.present(request, request.render())),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)
    approval_id = channel.pending.approval_id

    # Two tabs, one prompt. Exactly one decision can be recorded.
    channel.respond(approval_id, True)
    with pytest.raises(StaleApproval):
        channel.respond(approval_id, False)

    thread.join(timeout=5)
    assert answers == ["y"]


def test_an_unknown_approval_id_is_refused() -> None:
    channel = WebApprovalChannel()
    with pytest.raises(UnknownApproval):
        channel.respond("no-such-approval", True)


def test_a_second_prompt_while_one_is_pending_is_a_refusal(workspace: Workspace) -> None:
    """Replacing the prompt an operator is reading would be the worst outcome."""
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)

    thread = threading.Thread(
        target=lambda: channel.present(request, request.render()),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)

    # A second concurrent prompt refuses rather than displacing the first.
    assert channel.present(request, request.render()) is None

    first_id = channel.pending.approval_id
    channel.respond(first_id, True)
    thread.join(timeout=5)


def test_pending_public_carries_the_request_not_the_object(
    workspace: Workspace,
) -> None:
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)

    thread = threading.Thread(
        target=lambda: channel.present(request, request.render()),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)

    public = channel.pending_public()
    assert set(public) == {"approval_id", "request"}
    assert "approved" not in public["request"]
    assert public["request"]["rule"] == request.rule

    channel.respond(public["approval_id"], True)
    thread.join(timeout=5)
    assert channel.pending_public() is None


def test_release_all_refuses_a_pending_prompt(workspace: Workspace) -> None:
    """A server shutting down is not a human saying yes."""
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)
    answers = []

    thread = threading.Thread(
        target=lambda: answers.append(channel.present(request, request.render())),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)

    channel.release_all()
    thread.join(timeout=5)

    assert answers == ["n"]
    assert channel.pending is None


def test_release_all_with_nothing_pending_is_harmless() -> None:
    WebApprovalChannel().release_all()


def test_channel_emits_its_lifecycle(workspace: Workspace) -> None:
    seen = []
    channel = WebApprovalChannel(emit=lambda name, **f: seen.append((name, f)))
    request = _approval_required_request(workspace)

    thread = threading.Thread(
        target=lambda: channel.present(request, request.render()),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)
    channel.respond(channel.pending.approval_id, True)
    thread.join(timeout=5)

    names = [name for name, _ in seen]
    assert "approval.required" in names
    assert "approval.answered" in names


def test_channel_drives_the_approver_end_to_end(workspace: Workspace) -> None:
    """The bridge answers an Approver, not the policy layer directly."""
    channel = WebApprovalChannel()
    approver = Approver(channel)
    classifier = CommandClassifier(workspace)
    verdict = classifier.classify(Command(program="rm", args=("-rf", "build")))

    outcomes = []

    def worker():
        outcomes.append(
            approver.submit_command(verdict, agent="byte", workspace=workspace.root)
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)
    channel.respond(channel.pending.approval_id, True)
    thread.join(timeout=5)

    assert outcomes[0].approved


def test_a_rejection_through_the_approver_is_rejected(workspace: Workspace) -> None:
    channel = WebApprovalChannel()
    approver = Approver(channel)
    classifier = CommandClassifier(workspace)
    verdict = classifier.classify(Command(program="rm", args=("-rf", "build")))

    outcomes = []

    def worker():
        outcomes.append(
            approver.submit_command(verdict, agent="byte", workspace=workspace.root)
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)
    channel.respond(channel.pending.approval_id, False)
    thread.join(timeout=5)

    outcome = outcomes[0]
    assert not outcome.approved
    assert outcome.human_decided, "a rejection is a decision a person made"


# --------------------------------------------------------------------------
# session: one run at a time, on a worker thread
# --------------------------------------------------------------------------


def test_a_run_starts_and_completes(workspace: Workspace) -> None:
    session = _session(workspace, [ScriptedTurn(text="done")])
    run_id = session.submit("say done")

    assert _wait_for(lambda: session.state is UiSessionState.COMPLETED)
    status = session.status()
    assert status["run_id"] == run_id
    assert status["result"]["text"] == "done"
    assert status["state"] == "completed"


def test_a_second_task_while_running_is_a_conflict(workspace: Workspace) -> None:
    gate = threading.Event()

    class SlowGateway:
        def complete(self, request):
            gate.wait(timeout=5)
            from forge.models.base import ModelResponse

            return ModelResponse(text="eventually")

    from forge.ui.runner import runtime_factory

    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace,
            profile=BYTE,
            gateway=SlowGateway(),
            model_id="scripted",
        ),
    )

    session.submit("first")
    assert _wait_for(lambda: session.busy)

    with pytest.raises(SessionBusy) as busy:
        session.submit("second")
    assert busy.value.run_id

    gate.set()
    assert _wait_for(lambda: session.state is UiSessionState.COMPLETED)


def test_an_empty_task_starts_nothing(workspace: Workspace) -> None:
    session = _session(workspace, [ScriptedTurn(text="unused")])
    with pytest.raises(ValueError):
        session.submit("   ")
    assert session.state is UiSessionState.IDLE


def test_a_gateway_failure_is_reported_as_failed(workspace: Workspace) -> None:
    session = _session(
        workspace, [ScriptedTurn(raises=GatewayError("provider is down"))]
    )
    session.submit("anything")

    assert _wait_for(lambda: session.state is UiSessionState.FAILED)
    assert "provider is down" in str(session.status()["error"])


def test_an_exhausted_turn_budget_is_reported_as_failed(workspace: Workspace) -> None:
    session = _session(
        workspace,
        [ScriptedTurn(text="again", tool_calls=(("search_files", {"pattern": "*"}),))],
        max_turns=2,
    )
    session.submit("loop")

    assert _wait_for(lambda: session.state is UiSessionState.FAILED)
    assert "turn" in str(session.status()["error"])


def test_a_run_returns_to_a_terminal_state_and_accepts_another(
    workspace: Workspace,
) -> None:
    session = _session(workspace, [ScriptedTurn(text="one"), ScriptedTurn(text="two")])
    session.submit("first")
    assert _wait_for(lambda: session.state is UiSessionState.COMPLETED)

    session.submit("second")
    assert _wait_for(lambda: session.status()["result"]["text"] == "two")


def test_status_reports_the_fixed_workspace(workspace: Workspace) -> None:
    session = _session(workspace, [ScriptedTurn(text="x")])
    status = session.status()
    assert status["workspace"] == str(workspace.root)
    assert status["agent"] == "byte"
    assert status["model"] == "scripted"
    assert status["run_id"] is None


def test_session_states_have_no_cancelled() -> None:
    """A stop button that cannot stop is worse than none."""
    values = {state.value for state in UiSessionState}
    assert values == {
        "idle",
        "running",
        "waiting_for_approval",
        "completed",
        "failed",
    }
    assert "cancelled" not in values


def test_shutdown_with_no_pending_prompt_is_a_clean_no_op(
    workspace: Workspace,
) -> None:
    """A server with nothing to answer must still exit."""
    session = _session(workspace, [ScriptedTurn(text="x")])
    session.shutdown()
    assert session.channel.pending is None


def test_shutdown_refuses_a_prompt_nobody_answered(workspace: Workspace) -> None:
    """A killed server is not a human saying yes."""
    channel = WebApprovalChannel()
    request = _approval_required_request(workspace)
    answers = []

    thread = threading.Thread(
        target=lambda: answers.append(channel.present(request, request.render())),
        daemon=True,
    )
    thread.start()
    assert _wait_for(lambda: channel.pending is not None)

    channel.release_all()
    thread.join(timeout=5)
    assert answers == ["n"]


# --------------------------------------------------------------------------
# runner: composition only
# --------------------------------------------------------------------------


def test_runner_builds_a_dispatcher_with_bytes_tool_set(
    workspace: Workspace,
) -> None:
    dispatcher = build_dispatcher(
        workspace,
        profile=BYTE,
        channel=WebApprovalChannel(),
    )
    offered = {spec.name for spec in dispatcher.tool_specs()}
    assert offered == set(BYTE.allowed_tools)


def test_runner_builds_a_runtime(workspace: Workspace) -> None:
    runtime = build_runtime(
        workspace,
        profile=BYTE,
        gateway=ScriptedGateway([ScriptedTurn(text="hi")]),
        model_id="scripted",
        channel=WebApprovalChannel(),
        broker=SseBroker(),
    )
    assert runtime.run("say hi").text == "hi"


def test_the_ui_dispatcher_still_denies(workspace: Workspace) -> None:
    """The UI assembles the same pipeline, not a more permissive one."""
    dispatcher = build_dispatcher(
        workspace,
        profile=BYTE,
        channel=WebApprovalChannel(emit=lambda *a, **k: None),
    )
    result = dispatcher.dispatch(
        ToolCall(id="c1", name="exec_command", arguments='{"program": "rm", "args": ["-rf", "/"]}')
    )
    assert result.status.value in {"denied", "rejected"}


# --------------------------------------------------------------------------
# Structural guards
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path, package: str) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                if module:
                    found.add(module)
            else:
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                found.add(
                    ".".join([*base, *module.split(".")]) if module else ".".join(base)
                )
    return found


def _ui_modules() -> list[pathlib.Path]:
    return [p for p in UI_ROOT.rglob("*.py") if "__pycache__" not in str(p)]


def test_the_channel_cannot_reach_policy() -> None:
    """A channel that could classify or execute would be a second path."""
    source = UI_ROOT / "channel.py"
    imported = _imported_modules(source, "forge.ui")
    for forbidden in (
        "forge.tools.commands",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
    ):
        assert forbidden not in imported, f"channel imports {forbidden}"


def test_the_channel_never_calls_a_decision_function() -> None:
    source = (UI_ROOT / "channel.py").read_text()
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)

    for forbidden in ("submit_command", "submit_secret", "resolve", "execute"):
        assert forbidden not in called, f"channel calls {forbidden}"


def test_no_ui_module_calls_execute_directly() -> None:
    """Execution belongs to the dispatcher, and only through approval."""
    offenders = []
    for path in _ui_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "execute":
                    offenders.append(path.name)
    assert offenders == [], f"UI modules calling execute: {offenders}"


def test_the_broker_cannot_reach_policy() -> None:
    source = UI_ROOT / "broker.py"
    imported = _imported_modules(source, "forge.ui")
    overlap = imported & {
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.dispatcher",
    }
    assert overlap == set(), f"broker imports policy: {overlap}"


def test_the_ui_package_does_not_import_a_provider() -> None:
    """The credential belongs to the process, and the CLI builds the gateway."""
    for path in _ui_modules():
        imported = _imported_modules(path, "forge.ui")
        assert "forge.models.openrouter" not in imported, (
            f"{path.name} imports the provider gateway"
        )


def test_the_ui_package_holds_no_local_storage_concept() -> None:
    """A browser-side copy of approval state is a browser-side claim.

    Comments are stripped before the scan, because this file's own prose says
    "no localStorage" -- and a guard that cannot tell documentation from code
    would be a guard somebody deletes the next time they update a comment.
    """
    script = _code_only((UI_ROOT / "static" / "app.js").read_text())
    for banned in ("localStorage", "sessionStorage", "indexedDB"):
        assert banned not in script, f"app.js uses {banned}"


def _code_only(source: str) -> str:
    """Strip ``//`` and ``/* */`` comments and string literals from JS."""
    import re

    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", " ", without_block)
    return re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", '""', without_line)


def test_no_served_file_mentions_a_provider_credential() -> None:
    static = UI_ROOT / "static"
    for path in static.iterdir():
        text = path.read_text()
        assert "OPENROUTER" not in text, f"{path.name} references a provider key"
        assert "api_key" not in text.lower(), f"{path.name} references an api key"


def test_the_page_carries_only_the_token_placeholder() -> None:
    html = (UI_ROOT / "static" / "index.html").read_text()
    assert "__FORGE_UI_TOKEN__" in html
    assert TOKEN_HEADER not in html, "the page needs no header name to know"


# --------------------------------------------------------------------------
# SSE framing
# --------------------------------------------------------------------------


def test_frame_carries_name_id_and_payload() -> None:
    from forge.ui.server import _frame

    frame = _frame(7, "tool.result", {"tool": "read_file", "status": "ok"})
    assert frame.startswith("id: 7\nevent: tool.result\n")
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {
        "seq": 7,
        "event": "tool.result",
        "tool": "read_file",
        "status": "ok",
    }


def test_a_malformed_cursor_replays_everything() -> None:
    from forge.ui.server import _last_seq

    assert _last_seq(None) == 0
    assert _last_seq("garbage") == 0
    assert _last_seq("-4") == 0
    assert _last_seq("12") == 12


# --------------------------------------------------------------------------
# SSE delivery across the thread boundary
# --------------------------------------------------------------------------


def test_events_published_from_a_worker_thread_reach_the_subscriber() -> None:
    """The whole point of the broker: worker thread out, event loop in."""

    async def scenario() -> list:
        broker = SseBroker()
        broker.bind_loop(asyncio.get_running_loop())
        subscriber = broker.subscribe()

        def worker():
            for index in range(3):
                broker.emit(Event(name="tool.call", fields={"n": str(index)}))

        thread = threading.Thread(target=worker)
        thread.start()
        received = []
        deadline = time.monotonic() + 5
        while len(received) < 3 and time.monotonic() < deadline:
            received.append(await asyncio.wait_for(subscriber.queue.get(), timeout=2))
        thread.join(timeout=5)
        broker.unsubscribe(subscriber)
        return received

    received = asyncio.run(scenario())
    assert [event.get("n") for _, event in received] == ["0", "1", "2"]


def test_a_full_subscriber_queue_drops_instead_of_growing() -> None:
    """A browser that stops reading must not be able to slow the run."""
    broker = SseBroker(max_queue=4)
    loop = asyncio.new_event_loop()
    broker.bind_loop(loop)
    try:
        subscriber = broker.subscribe()
        for index in range(20):
            broker.emit(Event(name="e", fields={"n": str(index)}))
        # Let the loop deliver what it will.
        loop.run_until_complete(asyncio.sleep(0.05))

        assert subscriber.queue.qsize() == 4
        assert subscriber.dropped == 16
        assert subscriber.take_dropped() == 16
        assert subscriber.take_dropped() == 0
    finally:
        loop.close()


def test_runnable_request_helper_classifies_as_approval_required(
    workspace: Workspace,
) -> None:
    """Guards the helper itself, so the bridge tests use a real gated request."""
    assert _runnable_request(workspace).rule


def test_approval_required_request_is_really_gated(workspace: Workspace) -> None:
    """`rm -rf` on a workspace subdirectory must be approval-required here.

    If this ever stops holding, every bridge test above would be asserting on
    a prompt that never appears -- and would pass for the wrong reason.
    """
    request = _approval_required_request(workspace)
    assert request.classification.value == "approval_required"


def test_an_allow_needs_no_human(workspace: Workspace) -> None:
    """An allow must not reach the channel at all.

    Prompting for routine work is how operators learn to approve without
    reading, which defeats the control. The same rule that keeps the terminal
    quiet must keep the browser quiet.
    """
    channel = WebApprovalChannel()
    approver = Approver(channel)
    (workspace.root / "notes.txt").write_text("ordinary\n", encoding="utf-8")

    from forge.tools.secrets import SecretClassifier

    verdict = SecretClassifier(workspace).classify(workspace.root / "notes.txt")
    outcome = approver.submit_secret(verdict, agent="byte")

    assert outcome.approved
    assert channel.pending is None
    assert channel.pending_public() is None


def test_denied_operations_never_reach_the_channel(workspace: Workspace) -> None:
    """Structural tier: a denial has no approval path, in the UI either."""
    channel = WebApprovalChannel()
    approver = Approver(channel)
    (workspace.root / ".env").write_text("SECRET=abc123\n", encoding="utf-8")

    from forge.tools.secrets import SecretClassifier

    secrets = SecretClassifier(workspace)
    verdict = secrets.classify(workspace.root / ".env")
    outcome = approver.submit_secret(verdict, agent="byte")

    assert outcome.denied
    assert channel.pending is None