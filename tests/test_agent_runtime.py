"""Tests for the agent runtime: the tool-call loop, and what it must not hold.

Two groups. The first is behaviour -- the loop, the turn accounting, the error
paths. The second is **structural**: assertions about which modules import what,
which is how the central design property is kept from decaying.

That structural group exists because the property is invisible to behavioural
tests. A runtime that gained a classifier reference would still pass every loop
test in the first group; only an import scan notices. The property is that the
runtime holds no opinion about permissions -- ``AgentRuntime`` must never import
``CommandClassifier``, ``Approver``, or ``Executor``, and
``forge/tools/dispatcher.py`` must be the only runtime path to
``forge/tools/execution.py``.

The gateway is always :class:`ScriptedGateway`. No test in this file makes a
network call, needs a credential, or costs money.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from forge.agents import runtime as runtime_module
from forge.agents.byte import BLOCKS, BYTE, assemble_system_prompt
from forge.agents.profile import AgentProfile
from forge.agents.runtime import (
    MAX_TOOL_TURNS,
    AgentRuntime,
    MaxTurnsExceeded,
)
from forge.models.base import GatewayError
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import Approver, ScriptedApprovalChannel
from forge.tools.dispatcher import ToolDispatcher, ToolStatus
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def dispatcher_factory(workspace: Workspace):
    """Build a dispatcher with a scripted channel.

    The channel is returned too, so a test can count how many prompts were
    actually shown -- which is how the denial-cache tests prove a second prompt
    did not happen.
    """

    def build(responses: list[str | None] | None = None, *, agent: str = "byte"):
        channel = ScriptedApprovalChannel(responses if responses is not None else [])
        dispatcher = ToolDispatcher(
            workspace,
            approver=Approver(channel),
            executor=Executor(workspace),
            agent_name=agent,
            allowed_tools=BYTE.allowed_tools,
            system_prompt=assemble_system_prompt(BYTE, str(workspace.root)),
        )
        return dispatcher, channel

    return build


def _runtime(gateway: ScriptedGateway, dispatcher: ToolDispatcher, **kw) -> AgentRuntime:
    return AgentRuntime(
        BYTE, gateway=gateway, dispatcher=dispatcher, model_id="scripted", **kw
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_text_only_response_ends_the_run(dispatcher_factory) -> None:
    """A model that answers immediately finishes after one turn."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway([ScriptedTurn(text="Nothing to do.")])

    result = _runtime(gateway, dispatcher).run("status?")

    assert result.text == "Nothing to do."
    assert result.turns == 1
    assert result.tool_calls == 0


def test_tool_call_dispatches_appends_and_continues(dispatcher_factory, workspace) -> None:
    """A tool call is dispatched, its result appended, and the loop continues."""
    (workspace.root / "notes.txt").write_text("hello\n")
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "notes.txt"}),)),
            ScriptedTurn(text="The file says hello."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("what does notes.txt say?")

    assert result.text == "The file says hello."
    assert result.turns == 2
    assert result.tool_calls == 1
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content


def test_multiple_tool_calls_in_one_turn_count_as_one_turn(dispatcher_factory) -> None:
    """Turns are model round-trips, not tool calls.

    A response carrying five calls costs one turn: the round-trip is what costs
    tokens, and a model that batches well should not be penalised for it.
    """
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("read_file", {"path": "a.txt"}),
                    ("read_file", {"path": "b.txt"}),
                    ("read_file", {"path": "c.txt"}),
                )
            ),
            ScriptedTurn(text="done"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read three files")

    assert result.turns == 2
    assert result.tool_calls == 3


def test_max_turn_limit_raises(dispatcher_factory) -> None:
    """Exhausting the budget raises rather than returning a truncated success."""
    dispatcher, _ = dispatcher_factory()
    # One turn, repeated forever: the model never finishes.
    gateway = ScriptedGateway([ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),))])

    with pytest.raises(MaxTurnsExceeded) as caught:
        _runtime(gateway, dispatcher, max_turns=3).run("loop forever")

    assert caught.value.turns == 3
    assert "3 permitted turns" in str(caught.value)


def test_max_turns_is_configurable_downward_only(dispatcher_factory) -> None:
    """A caller may lower the ceiling but never raise it past the constant."""
    dispatcher, _ = dispatcher_factory()

    lowered = _runtime(ScriptedGateway([]), dispatcher, max_turns=5)
    assert lowered.max_turns == 5

    attempted = _runtime(ScriptedGateway([]), dispatcher, max_turns=10_000)
    assert attempted.max_turns == MAX_TOOL_TURNS


def test_gateway_failure_propagates_rather_than_ending_the_run(dispatcher_factory) -> None:
    """A provider failure raises. It is not the model's to recover from."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [ScriptedTurn(raises=GatewayError("provider unreachable"))]
    )

    with pytest.raises(GatewayError):
        _runtime(gateway, dispatcher).run("anything")


def test_system_prompt_carries_the_workspace_and_no_classifier_rules(
    dispatcher_factory, workspace
) -> None:
    """The prompt states the workspace; it does not restate the safety layer."""
    dispatcher, _ = dispatcher_factory()
    prompt = dispatcher.system_prompt

    assert str(workspace.root) in prompt
    # Identity blocks present.
    assert "You are Byte" in prompt
    # No restatement of classifier rules: these would be a second, weaker copy
    # of the safety layer that drifts on every future improvement.
    for banned in ("sudo", "force push", "denied commands", "approval_required"):
        assert banned not in prompt, f"prompt restates classifier detail: {banned}"


def test_byte_instructions_match_the_contract_blocks() -> None:
    """All seven standing instruction blocks are present, in order."""
    assert len(BLOCKS) == 7
    joined = BYTE.instructions
    for index, block in enumerate(BLOCKS):
        assert block in joined, f"block {index} missing from the profile"

    assert joined.index("You are Byte") < joined.index("Investigate before")
    assert joined.index("Investigate before") < joined.index("You operate inside")


# --------------------------------------------------------------------------
# Approval integration
# --------------------------------------------------------------------------


def test_approval_required_command_prompts_and_runs(dispatcher_factory) -> None:
    """`git commit` is gated: the operator is asked, and approval runs it."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["commit", "-m", "x"]}),
                )
            ),
            ScriptedTurn(text="committed"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("commit")

    assert len(channel.presented) == 1
    assert result.tool_calls == 1
    assert not result.denied


def test_rejected_approval_is_returned_to_the_model(dispatcher_factory) -> None:
    """A rejection becomes a tool result the model can read, and nothing runs."""
    dispatcher, channel = dispatcher_factory(["n"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["commit", "-m", "x"]}),
                )
            ),
            ScriptedTurn(text="Understood, I did not commit."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("commit")

    assert len(channel.presented) == 1
    assert result.denied
    assert result.denied[0][0] == "exec_command"
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"rejected"' in tool_messages[0].content


def test_denied_command_never_consults_the_channel(dispatcher_factory) -> None:
    """A policy denial prompts nobody. There is no path to approve a denial."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(text="Cannot do that."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("list root files")

    assert channel.presented == []
    assert result.denied
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied"' in tool_messages[0].content


def test_unattended_approval_required_reports_distinctly(dispatcher_factory) -> None:
    """Unattended is its own status, not a rejection.

    A human saying no and nobody being there are different events, and an
    operator reading the transcript needs to tell them apart.
    """
    dispatcher, _ = dispatcher_factory()
    dispatcher.approver = Approver(None)  # unattended

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "pytest"},),)),
            ScriptedTurn(text="Could not run the tests."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("run the tests")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied_unattended"' in tool_messages[0].content


# --------------------------------------------------------------------------
# Denial cache
# --------------------------------------------------------------------------


def test_repeated_rejection_does_not_reprompt(dispatcher_factory) -> None:
    """An identical rejected call returns the cached result without prompting."""
    dispatcher, channel = dispatcher_factory(["n"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["push", "origin", "main"]}),
                )
            ),
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["push", "origin", "main"]}),
                )
            ),
            ScriptedTurn(text="I cannot push."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("push it")

    # One prompt, not two: the operator said no once and is not asked again.
    assert len(channel.presented) == 1
    assert result.turns == 3
    assert result.tool_calls == 2


def test_repeated_denial_does_not_invoke_the_classifier_again(dispatcher_factory) -> None:
    """A cached denial short-circuits before any classifier or channel work."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(text="done"),
        ]
    )

    calls: list[object] = []
    original = dispatcher._classifier.classify

    def counting(command):  # type: ignore[no-untyped-def]
        calls.append(command)
        return original(command)

    dispatcher._classifier.classify = counting  # type: ignore[method-assign]

    _runtime(gateway, dispatcher).run("escalate")

    assert len(calls) == 1, "the cached denial re-entered the classifier"
    assert channel.presented == []


def test_repeated_denied_turns_still_reach_max_turns(dispatcher_factory) -> None:
    """The denial cache does not exempt the model from the turn limit.

    The cache stops repeated prompts; it is not what stops a runaway. An agent
    looping on a denied call must still terminate.
    """
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),))]
    )

    with pytest.raises(MaxTurnsExceeded):
        _runtime(gateway, dispatcher, max_turns=25).run("keep escalating")

    # The prompt was shown at most once across all 25 turns.
    assert len(channel.presented) == 0


def test_different_failure_classes_are_not_cached(dispatcher_factory, tmp_path) -> None:
    """Malformed arguments are not cached: a retry with fixed arguments must work."""
    dispatcher, _ = dispatcher_factory()
    call = ("read_file", "{not valid json")

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(call,)),
            ScriptedTurn(tool_calls=(("read_file", {"path": "ok.txt"}),)),
            ScriptedTurn(text="done"),
        ]
    )
    (dispatcher.workspace.root / "ok.txt").write_text("fine\n")

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"invalid_arguments"' in tool_messages[0].content
    assert '"status":"ok"' in tool_messages[1].content


# --------------------------------------------------------------------------
# Tool errors
# --------------------------------------------------------------------------


def test_unknown_tool_is_reported_not_raised(dispatcher_factory) -> None:
    """A name with no registry entry is a value the model can read."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("rm_rf_everything", {}),)),
            ScriptedTurn(text="That tool does not exist."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("do something")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"unknown_tool"' in tool_messages[0].content
    assert result.text == "That tool does not exist."


def test_schema_violation_names_the_field(dispatcher_factory) -> None:
    """"invalid arguments" tells a model nothing; the field name does."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": 42}),)),
            ScriptedTurn(text="ok"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert "path must be a string" in tool_messages[0].content


def test_tool_internal_error_carries_no_traceback(dispatcher_factory, monkeypatch) -> None:
    """An internal failure is reported by class name, never as a traceback.

    A traceback reaches the model through the tool result, and carries file
    paths and possibly file contents on its way.
    """
    dispatcher, _ = dispatcher_factory()

    def exploding(**_: object):
        raise RuntimeError("boom at /etc/shadow with SECRET=abc123")

    monkeypatch.setattr(dispatcher, "_tool_read_file", exploding)
    dispatcher._registry["read_file"] = dispatcher._registry["read_file"].__class__(
        "read_file", "d", {}, exploding
    )

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),)),
            ScriptedTurn(text="ok"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"error"' in tool_messages[0].content
    assert "RuntimeError" in tool_messages[0].content
    assert "Traceback" not in tool_messages[0].content


def test_tool_not_allowed_when_profile_is_narrower(dispatcher_factory, workspace) -> None:
    """An agent configured without a tool cannot reach it by naming it."""
    dispatcher, _ = dispatcher_factory()
    # Rebuild the dispatcher with a narrower allowance.
    narrow = ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel([])),
        executor=Executor(workspace),
        agent_name="narrow-agent",
        allowed_tools=("read_file",),
    )
    narrow.system_prompt = "x"

    result = narrow.dispatch(
        __import__("forge.models.base", fromlist=["ToolCall"]).ToolCall(
            id="1", name="exec_command", arguments="{}"
        )
    )

    assert result.status is ToolStatus.TOOL_NOT_ALLOWED
    # And the tool is not even offered to the model.
    offered = {spec.name for spec in narrow.tool_specs()}
    assert offered == {"read_file"}


# --------------------------------------------------------------------------
# Session state and secrets
# --------------------------------------------------------------------------


def test_session_state_stays_in_process(dispatcher_factory, workspace, tmp_path) -> None:
    """A run creates no file. Session memory is session-only by decision."""
    dispatcher, _ = dispatcher_factory()
    before = set(p for p in workspace.root.rglob("*"))
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("write_file", {"path": "out.txt", "content": "x"}),)),
            ScriptedTurn(text="wrote it"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("write a file")

    after = set(p for p in workspace.root.rglob("*"))
    # The only new file is the one the tool was asked to write.
    assert after - before == {workspace.root / "out.txt"}
    assert result.state.messages  # in memory, returned for inspection


def test_no_provider_key_reaches_model_context(dispatcher_factory) -> None:
    """Whatever a run sends to the model, it is not an environment value.

    Asserted over the whole rendered transcript rather than over dataclass
    fields: a credential smuggled into a JSON tool result would live inside a
    string, and field inspection would miss it.
    """
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),)),
            ScriptedTurn(text="done"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read a file")

    transcript = result.state.transcript_text()
    assert "FORGE_OPENROUTER_API_KEY" not in transcript
    assert "Authorization" not in transcript
    assert "Bearer " not in transcript


def test_reading_a_structural_secret_is_denied_without_reading_it(
    dispatcher_factory, workspace
) -> None:
    """`.env` is denied, and its contents never reach the transcript."""
    (workspace.root / ".env").write_text("TOKEN=super-secret-value\n")
    dispatcher, channel = dispatcher_factory(["y"])

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": ".env"}),)),
            ScriptedTurn(text="I cannot read that file."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read .env")

    assert "super-secret-value" not in result.state.transcript_text()
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied"' in tool_messages[0].content


def test_process_output_is_bounded_and_model_visible(
    dispatcher_factory, workspace
) -> None:
    """stdout reaches the model bounded, secrets and all.

    This documents the v1 limitation honestly rather than pretending a redaction
    guarantee exists. Forge prevents *known* secret sources from being read on
    purpose; arbitrary subprocess output cannot be guaranteed secret-free, and v1
    does not attempt to fix that. What it does is bound the output.

    `/usr/bin/echo` is used because the Executor's PATH is fixed to the trusted
    system roots -- a workspace script would fail to launch, which is the
    executor's own documented limitation rather than anything this test is about.
    """
    dispatcher, _ = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    (
                        "exec_command",
                        {
                            "program": "/usr/bin/echo",
                            "args": ["TOKEN=from-process-output"],
                        },
                    ),
                )
            ),
            ScriptedTurn(text="saw the output"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("run a command")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert "from-process-output" in tool_messages[0].content
    # Bounded output is returned whole here because it is small. The bound
    # itself is the Executor's, and this asserts the shape carries it.
    assert '"stdout"' in tool_messages[0].content
    assert '"stdout_truncated"' in tool_messages[0].content


# --------------------------------------------------------------------------
# Structural guards
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path, package: str | None = None) -> set[str]:
    """Every module imported by ``path``, as dotted names.

    Relative imports are resolved against ``package`` rather than skipped. That
    matters: a forbidden import written as ``from ..tools.commands import ...``
    is exactly the shape a future edit would take, and a scan that only handled
    ``node.level == 0`` would sail straight past it. That is a mistake this test
    suite actually made and corrected -- the guard was verified by injecting the
    violation and watching it pass.
    """
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
            elif package:
                # Walk up `level - 1` packages, then descend into `module`.
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                found.add(".".join([*base, *module.split(".")]) if module else ".".join(base))

    return found


def cli_main_path() -> str:
    """Filesystem path of ``forge/cli/main.py``.

    Resolved from the package root rather than imported, because
    ``from forge.cli import main`` binds the *function* (the package re-exports
    it) and a function has no ``__file__``.
    """
    return str(
        pathlib.Path(__file__).resolve().parent.parent / "forge" / "cli" / "main.py"
    )


def test_runtime_module_holds_no_policy_objects() -> None:
    """The runtime must not be able to reach a classifier, approver, or executor.

    A runtime that could reach one would be one edit away from holding a second
    opinion about what is allowed, which is how "approval narrows a permission,
    it never widens one" decays.
    """
    source = pathlib.Path(runtime_module.__file__ or "")
    imported = _imported_modules(source, package="forge.agents")

    for forbidden in (
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
    ):
        assert forbidden not in imported, f"runtime imports {forbidden}"

    for relative in ("commands", "approval", "execution", "secrets", "workspace"):
        assert not any(
            name.endswith(f".{relative}") for name in imported
        ), f"runtime imports {relative}"


def test_agent_runtime_has_no_executor_attribute(dispatcher_factory) -> None:
    """The instance cannot reach an executor even if one were passed in."""
    dispatcher, _ = dispatcher_factory()
    runtime = AgentRuntime(
        BYTE,
        gateway=ScriptedGateway([]),
        dispatcher=dispatcher,
        model_id="scripted",
    )

    for attribute in ("executor", "approver", "classifier", "_classifier"):
        assert not hasattr(runtime, attribute), f"runtime exposes {attribute}"

def test_dispatcher_is_the_only_runtime_path_to_execution() -> None:
    """Only the dispatcher may import `forge.tools.execution`.

    Scoped to the *runtime* packages -- ``forge/agents`` and ``forge/models``.
    The CLI is excluded deliberately and correctly: `forge byte` wires an
    ``Executor`` into a dispatcher, which is the sanctioned composition rather
    than a second path to a process. What must not happen is an agent module
    reaching an executor on its own.
    """
    offenders: list[str] = []
    package_root = pathlib.Path(__file__).resolve().parent.parent / "forge"

    runtime_packages = ("agents", "models")

    for package in runtime_packages:
        for path in (package_root / package).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            parts = list(path.relative_to(package_root.parent).parts)
            imported = _imported_modules(path, package=".".join(parts[:-1]))
            if "forge.tools.execution" in imported or any(
                name.endswith(".execution") for name in imported
            ):
                offenders.append("/".join(parts))

    assert offenders == [], f"runtime modules importing execution: {offenders}"


def test_cli_wires_an_executor_only_through_a_dispatcher() -> None:
    """`forge byte` may construct an executor, but only to hand to a dispatcher.

    The CLI is allowed to assemble the pipeline. It is not allowed to call
    ``execute`` itself, which would be a second path to a process that bypasses
    approval entirely.
    """
    source = pathlib.Path(cli_main_path()).read_text()
    tree = ast.parse(source)

    imported = _imported_modules(pathlib.Path(cli_main_path()), package="forge.cli")
    assert "forge.tools.execution" in imported, "the CLI wires an executor"

    # No direct call to Executor.execute anywhere in the CLI.
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
            elif isinstance(func, ast.Name):
                calls.append(func.id)

    assert "execute" not in calls, (
        "the CLI calls execute directly; execution belongs to the dispatcher"
    )


def test_model_gateway_package_holds_no_policy() -> None:
    """The model layer cannot hold permission policy.

    A gateway that could widen a permission would collapse the separation the
    safety stack rests on: the model decides what it wants, Forge decides whether
    it is allowed.
    """
    package_root = pathlib.Path(__file__).resolve().parent.parent / "forge" / "models"
    forbidden = {
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
        "forge.tools.dispatcher",
    }

    for path in package_root.glob("*.py"):
        imported = _imported_modules(path)
        overlap = imported & forbidden
        assert overlap == set(), f"{path.name} imports policy: {overlap}"


def test_gateway_protocol_has_exactly_one_method() -> None:
    """One method, so the runtime stays provider-neutral.

    Every additional gateway method is a reason the runtime must implement
    provider-specific behaviour, which is what `ModelGateway` exists to prevent.
    """
    from forge.models.base import ModelGateway

    methods = [
        name
        for name in vars(ModelGateway)
        if callable(getattr(ModelGateway, name, None)) and not name.startswith("_")
    ]
    assert methods == ["complete"], f"gateway protocol grew: {methods}"


def test_scripted_gateway_needs_no_credential() -> None:
    """The offline path requires no key, which is why tests can use it."""
    gateway = ScriptedGateway([ScriptedTurn(text="ok")])
    from forge.models.base import CompletionRequest

    response = gateway.complete(
        CompletionRequest(system="s", messages=(), model="m")
    )
    assert response.text == "ok"
