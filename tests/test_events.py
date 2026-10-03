"""Tests for the observational event seam.

Three groups.

**Bounds.** An :class:`Event` must be inert and small no matter what a caller
tries to put in it. These are behavioural tests on the clamping, because a bound
nobody tests is a bound somebody widens.

**Inertness.** Observing must not change anything. The same run, with a sink and
without, produces the same results and the same exceptions; a sink that raises
does not fail the run. If these ever fail, ``EventSink`` has become a
participant in the run, which is the thing the design forbids.

**Structure.** The guarantees above are only real if ``forge/events.py`` cannot
reach a classifier, an approver, or an executor. That is invisible to a
behavioural test, so it is asserted by scanning imports.

No test here makes a network call. The gateway is always
:class:`ScriptedGateway`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from forge.agents.byte import BYTE, assemble_system_prompt
from forge.agents.runtime import AgentRuntime, MaxTurnsExceeded
from forge.events import (
    MAX_EVENT_FIELDS,
    MAX_FIELD_CHARS,
    MAX_NAME_CHARS,
    Event,
    EventSink,
    NullEventSink,
    emit,
)
from forge.models.base import GatewayError, ToolCall
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import Approver, ScriptedApprovalChannel
from forge.tools.dispatcher import ToolDispatcher, ToolStatus
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class RecordingSink:
    """A sink that keeps what it was given."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def names(self) -> list[str]:
        return [event.name for event in self.events]


class ExplodingSink:
    """A sink that fails on every call."""

    def emit(self, event: Event) -> None:
        raise RuntimeError("the observer is broken")


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


def _dispatcher(workspace: Workspace, *, events=None, responses=None) -> ToolDispatcher:
    return ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel(responses if responses else [])),
        executor=Executor(workspace),
        agent_name=BYTE.name,
        allowed_tools=BYTE.allowed_tools,
        system_prompt=assemble_system_prompt(BYTE, str(workspace.root)),
        events=events,
    )


def _runtime(
    gateway: ScriptedGateway,
    dispatcher: ToolDispatcher,
    events=None,
) -> AgentRuntime:
    return AgentRuntime(
        BYTE,
        gateway=gateway,
        dispatcher=dispatcher,
        model_id="scripted",
        events=events if events is not None else dispatcher.events,
    )


def _call(name: str = "read_file", arguments: str = '{"path": "a.txt"}') -> ToolCall:
    return ToolCall(id="call_1", name=name, arguments=arguments)


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_field_count_is_capped() -> None:
    event = Event(name="x", fields={f"k{i}": "v" for i in range(MAX_EVENT_FIELDS + 10)})
    assert len(event.fields) == MAX_EVENT_FIELDS


def test_field_values_are_clamped() -> None:
    event = Event(name="x", fields={"blob": "y" * (MAX_FIELD_CHARS * 3)})
    assert len(event.fields["blob"]) == MAX_FIELD_CHARS
    assert event.fields["blob"].endswith("…"), "a clipped value must say so"


def test_exact_length_is_not_clipped() -> None:
    text = "y" * MAX_FIELD_CHARS
    assert Event(name="x", fields={"blob": text}).fields["blob"] == text


def test_event_name_is_clamped() -> None:
    event = Event(name="n" * (MAX_NAME_CHARS * 2), fields={})
    assert len(event.name) == MAX_NAME_CHARS


def test_field_names_are_clamped() -> None:
    event = Event(name="x", fields={"k" * (MAX_NAME_CHARS * 2): "v"})
    assert all(len(key) <= MAX_NAME_CHARS for key in event.fields)


def test_non_string_values_are_rendered_and_clamped() -> None:
    """A caller must not be able to blow the bound by passing an object."""

    class Verbose:
        def __str__(self) -> str:
            return "z" * (MAX_FIELD_CHARS * 2)

    event = Event(name="x", fields={"n": 12, "obj": Verbose()})
    assert event.fields["n"] == "12"
    assert len(event.fields["obj"]) == MAX_FIELD_CHARS


def test_every_field_is_a_string() -> None:
    """The property that makes an event inert: nothing in it has behaviour."""
    event = Event(name="x", fields={"a": 1, "b": None, "c": object()})
    assert all(isinstance(value, str) for value in event.fields.values())


def test_long_keys_colliding_after_clamping_keep_the_first_value() -> None:
    """Truncation can map two distinct names onto one; the survivor is the first.

    A dict literal cannot express duplicate keys, so this is the real form of
    the hazard: two names long enough to truncate to the same string.
    """
    collision = "k" * (MAX_NAME_CHARS * 2)
    event = Event(name="x", fields={collision: "first", collision + "z": "second"})
    assert list(event.fields.items()) == [(collision[: MAX_NAME_CHARS - 1] + "…", "first")]


def test_event_is_frozen() -> None:
    event = Event(name="x", fields={"a": "b"})
    with pytest.raises(Exception):
        event.name = "y"  # type: ignore[misc]


def test_to_public_dict_carries_the_name_apart() -> None:
    """A field must not be able to shadow the dispatch key."""
    event = Event(name="tool.result", fields={"tool": "exec_command"})
    body = event.to_public_dict()
    assert body["event"] == "tool.result"
    assert body["tool"] == "exec_command"


def test_get_returns_a_default_for_absent_fields() -> None:
    assert Event(name="x", fields={}).get("nope", "fallback") == "fallback"


# --------------------------------------------------------------------------
# The emit helper
# --------------------------------------------------------------------------


def test_emit_with_no_sink_is_a_no_op() -> None:
    emit(None, "run.started", agent="byte")


def test_emit_delivers_one_event() -> None:
    sink = RecordingSink()
    emit(sink, "run.started", agent="byte")
    assert sink.names() == ["run.started"]
    assert sink.events[0].get("agent") == "byte"


def test_emit_swallows_a_broken_sink() -> None:
    """An observer that raises must not become a participant in the run."""
    emit(ExplodingSink(), "run.started")


def test_null_sink_accepts_everything() -> None:
    sink = NullEventSink()
    emit(sink, "run.started")
    assert isinstance(sink, EventSink)


def test_recording_sink_satisfies_the_protocol() -> None:
    assert isinstance(RecordingSink(), EventSink)


# --------------------------------------------------------------------------
# Inertness: the same run either way
# --------------------------------------------------------------------------


def test_a_run_is_identical_with_and_without_a_sink(workspace: Workspace) -> None:
    """The central property. Observation changes nothing observable but events."""
    (workspace.root / "a.txt").write_text("hello\n", encoding="utf-8")

    def run(dispatcher: ToolDispatcher) -> tuple:
        gateway = ScriptedGateway(
            [
                ScriptedTurn(
                    text="reading",
                    tool_calls=(("read_file", {"path": "a.txt"}),),
                ),
                ScriptedTurn(text="done"),
            ]
        )
        result = _runtime(gateway, dispatcher).run("read a.txt")
        return result.text, result.turns, result.tool_calls, result.state.transcript_text()

    quiet = run(_dispatcher(workspace))
    sink = RecordingSink()
    observed = run(_dispatcher(workspace, events=sink))

    assert quiet == observed, "a sink changed the run's result"
    assert sink.names(), "the sink received nothing"


def test_a_broken_sink_does_not_fail_the_run(workspace: Workspace) -> None:
    gateway = ScriptedGateway([ScriptedTurn(text="fine")])
    runtime = _runtime(gateway, _dispatcher(workspace, events=ExplodingSink()))
    assert runtime.run("anything").text == "fine"


def test_gateway_failure_still_propagates_with_a_sink(workspace: Workspace) -> None:
    sink = RecordingSink()
    gateway = ScriptedGateway([ScriptedTurn(raises=GatewayError("provider is down"))])
    runtime = _runtime(gateway, _dispatcher(workspace, events=sink))

    with pytest.raises(GatewayError):
        runtime.run("anything")

    assert "run.failed" in sink.names()


def test_max_turns_still_raises_with_a_sink(workspace: Workspace) -> None:
    sink = RecordingSink()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                text="again",
                tool_calls=(("search_files", {"pattern": "*"}),),
            )
        ]
    )
    runtime = _runtime(gateway, _dispatcher(workspace, events=sink))
    runtime.max_turns = 2

    with pytest.raises(MaxTurnsExceeded):
        runtime.run("loop forever")

    assert "run.failed" in sink.names()


def test_a_run_reports_its_own_lifecycle(workspace: Workspace) -> None:
    sink = RecordingSink()
    gateway = ScriptedGateway([ScriptedTurn(text="all done")])
    _runtime(gateway, _dispatcher(workspace, events=sink)).run("hi")

    names = sink.names()
    assert names[0] == "run.started"
    assert "model.reply" in names
    assert names[-1] == "run.completed"


# --------------------------------------------------------------------------
# Dispatcher observation
# --------------------------------------------------------------------------


def test_tool_call_and_result_are_observed(workspace: Workspace) -> None:
    sink = RecordingSink()
    _dispatcher(workspace, events=sink).dispatch(_call())
    assert sink.names() == ["tool.call", "tool.result"]


def test_tool_arguments_never_reach_the_sink(workspace: Workspace) -> None:
    """A model may put file contents in arguments; an activity log must not."""
    secret = "the-payload-the-operator-never-typed"
    sink = RecordingSink()
    _dispatcher(workspace, events=sink).dispatch(
        _call("write_file", '{"path": "b.txt", "content": "%s"}' % secret)
    )
    assert secret not in repr([event.fields for event in sink.events])


def test_read_file_content_never_reaches_the_sink(workspace: Workspace) -> None:
    body = "line-one\nline-two\n"
    (workspace.root / "a.txt").write_text(body, encoding="utf-8")
    sink = RecordingSink()
    _dispatcher(workspace, events=sink).dispatch(_call())

    assert body not in repr([event.fields for event in sink.events])


def test_exec_output_is_reported_as_counts_not_content(workspace: Workspace) -> None:
    # Approval-gated, so the channel answers yes -- the point of this test is
    # what the *event* carries, not whether the command was permitted.
    sink = RecordingSink()
    _dispatcher(workspace, events=sink, responses=["y"]).dispatch(
        _call(
            "exec_command",
            '{"program": "printf", "args": ["a-secret-value"]}',
        )
    )

    result_event = sink.events[-1]
    assert "a-secret-value" not in repr(result_event.fields)
    assert "stdout_bytes" in result_event.fields
    assert result_event.get("status") == ToolStatus.OK.value


def test_refusals_are_observed_too(workspace: Workspace) -> None:
    """A denial is exactly what an operator most needs to see."""
    sink = RecordingSink()
    _dispatcher(workspace, events=sink).dispatch(
        _call("exec_command", '{"program": "rm", "args": ["-rf", "/"]}')
    )
    result_event = sink.events[-1]
    assert result_event.get("status") == ToolStatus.DENIED.value
    assert result_event.get("detail")


def test_dispatcher_without_events_is_unchanged(workspace: Workspace) -> None:
    """The default stays the default: no sink attribute to trip over."""
    (workspace.root / "a.txt").write_text("hello\n", encoding="utf-8")
    dispatcher = _dispatcher(workspace)
    assert dispatcher.events is None
    assert dispatcher.dispatch(_call()).status is ToolStatus.OK


# --------------------------------------------------------------------------
# Structural guards
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path, package: str | None = None) -> set[str]:
    """Every module imported by ``path``, as dotted names.

    Relative imports are resolved against ``package``, because a forbidden
    import written as ``from ..tools.commands import ...`` is exactly the shape a
    future edit would take.
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
                parts = package.split(".")
                base = (
                    parts[: len(parts) - (node.level - 1)]
                    if node.level > 1
                    else parts
                )
                found.add(
                    ".".join([*base, *module.split(".")]) if module else ".".join(base)
                )

    return found


def test_events_module_reaches_no_policy() -> None:
    """An observer with a handle to policy is one edit away from holding it."""
    source = pathlib.Path(__file__).resolve().parent.parent / "forge" / "events.py"
    imported = _imported_modules(source)

    forbidden = {
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
        "forge.tools.dispatcher",
        "forge.agents.runtime",
        "forge.agents.state",
        "forge.models.base",
        "forge.models.openrouter",
    }
    overlap = imported & forbidden
    assert overlap == set(), f"forge/events.py imports policy: {overlap}"


def test_events_module_imports_only_stdlib() -> None:
    """``forge.events`` must stay installable with zero dependencies."""
    source = pathlib.Path(__file__).resolve().parent.parent / "forge" / "events.py"
    imported = _imported_modules(source)
    # ``__future__`` is a compiler directive rather than a real import, so it is
    # listed here explicitly rather than filtered out of the scan.
    assert imported <= {"dataclasses", "typing", "collections.abc", "__future__"}, (
        f"unexpected import: {imported}"
    )


def test_event_exposes_no_callable_field() -> None:
    """Nothing an observer receives has behaviour, so it has nothing to call."""
    event = Event(name="x", fields={"a": "b"})
    for value in (event, *event.fields.values()):
        assert not callable(value)


def test_sink_protocol_has_exactly_one_method() -> None:
    """A second method is a second responsibility, which is how policy grows."""
    methods = [
        name
        for name in vars(EventSink)
        if callable(getattr(EventSink, name, None)) and not name.startswith("_")
    ]
    assert methods == ["emit"], f"EventSink grew: {methods}"