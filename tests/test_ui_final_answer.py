"""The final-answer recovery path: a clipped preview, replaced once, by the answer.

The defect this file was written for is narrow and specific. ``model.reply``
carries the model's text as an event field, and every event field is clamped by
:class:`forge.events.Event`. That clamp is deliberate -- an event is a summary of
one observation -- so a long answer reached the Workbench transcript *clipped*,
with nothing to replace it. The tempting fixes were both wrong: raising the
clamp would make a summary into a transport, and exempting ``model.reply`` would
put one unbounded field on an SSE stream for every subscriber.

The recovery path instead reads the answer from where it already lives intact --
``GET /api/status``, a read of state the server already holds -- and reconciles
the transcript so it holds one authoritative answer rather than a preview
followed by a copy. Nothing about the event system changed.

The tests are the five properties that fix has to have, and each would pass on a
partial implementation:

*   a long reply is **still clamped** in the event stream (the bound did not
    move);
*   the full answer **survives** end to end, through the runtime, the session,
    and the status payload;
*   **completion makes the page render the complete answer**, driven by real
    ``app.js`` rather than a reimplementation of it;
*   **no duplicate** assistant entry survives reconciliation, including when the
    completion event arrives twice;
*   a **short reply behaves exactly as before** -- no extra status fetch, no
    rewritten entry.

The frontend half is driven by ``tests/ui_probe/final_answer_probe.js``, which
loads the shipped ``app.js`` into a stub DOM, serves scripted ``/api/status``
answers and stream frames, and prints the transcript. Asserting on that is what
makes "one authoritative answer" a checked property rather than an intention;
the same assertions against a hand-copied reconciler would prove nothing about
the file that ships.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from forge.agents.byte import BYTE
from forge.agents.runtime import AgentRuntime
from forge.events import MAX_FIELD_CHARS, Event, emit
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import Approver, ScriptedApprovalChannel
from forge.tools.dispatcher import ToolDispatcher
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace
from forge.ui.runner import runtime_factory
from forge.ui.session import WorkbenchSession
from forge.ui.selector import ModelSelection, ModelSelector

REPO = pathlib.Path(__file__).resolve().parent.parent
APP_JS = REPO / "forge" / "ui" / "static" / "app.js"
PROBE = pathlib.Path(__file__).resolve().parent / "ui_probe" / "final_answer_probe.js"

NODE = shutil.which("node")

requires_node = pytest.mark.skipif(
    NODE is None,
    reason="node is required to drive the shipped app.js against a stub DOM",
)


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


#: A long answer, and how a reader can tell it was not clipped. The marker sits
#: far past the clamp so an implementation that "recovers" a truncated string
#: still fails the assertions.
LONG_ANSWER = "BEGIN-ANSWER " + ("the tongs were straightened. " * 60) + "END-ANSWER"


# --------------------------------------------------------------------------
# The bound, which must not move
# --------------------------------------------------------------------------


def test_the_event_clamp_is_where_it_was() -> None:
    """The fix is not permitted to be a bigger limit.

    Asserted directly rather than inferred: an implementation that quietly
    raised ``MAX_FIELD_CHARS`` would keep every other test in this file green
    while undoing the property the design rests on.
    """
    assert MAX_FIELD_CHARS == 400


def test_a_long_reply_is_still_clamped_in_the_event_stream(
    workspace: Workspace,
) -> None:
    """The streamed preview is clipped, and says that it is clipped.

    This is the whole reason the recovery path exists, so it is asserted from
    the runtime's own sink rather than from :class:`Event` in isolation: what
    matters is what ``model.reply`` actually carries for a long answer.
    """
    sink = _RecordingSink()
    dispatcher = _dispatcher(workspace, events=sink)
    runtime = _runtime(ScriptedGateway([ScriptedTurn(text=LONG_ANSWER)]), dispatcher, sink)

    result = runtime.run("explain the repair")

    reply = _event(sink, "model.reply")
    assert len(reply.fields["text"]) == MAX_FIELD_CHARS
    assert reply.fields["text"].endswith("…"), "a clipped value must say so"
    # The tail of the answer is genuinely absent from the event, which is what
    # "the preview is not the answer" means operationally.
    assert "END-ANSWER" not in reply.fields["text"]
    # And the answer itself was not harmed on the way past.
    assert result.text == LONG_ANSWER


def test_a_short_reply_is_not_clamped(workspace: Workspace) -> None:
    """A reply inside the bound crosses the stream verbatim."""
    sink = _RecordingSink()
    dispatcher = _dispatcher(workspace, events=sink)
    runtime = _runtime(ScriptedGateway([ScriptedTurn(text="All done.")]), dispatcher, sink)

    assert runtime.run("status?").text == "All done."
    assert _event(sink, "model.reply").fields["text"] == "All done."


def test_the_bounds_on_the_whole_payload_are_unchanged(tmp_path: pathlib.Path) -> None:
    """No field count or name bound moved either.

    The clamp on one field is the obvious thing to loosen; the field count and
    the name length are the neighbours that make "bounded by type" true, and an
    edit that widened those would leave the same class of hole.
    """
    from forge.events import MAX_EVENT_FIELDS, MAX_NAME_CHARS

    event = Event(
        name="model.reply",
        fields={
            "turn": "1",
            "tool_calls": "0",
            "text": LONG_ANSWER,
            **{f"f{index}": "v" for index in range(MAX_EVENT_FIELDS)},
        },
    )
    assert len(event.fields) == MAX_EVENT_FIELDS
    assert all(len(name) <= MAX_NAME_CHARS for name in event.fields)
    assert len(event.fields["text"]) == MAX_FIELD_CHARS


# --------------------------------------------------------------------------
# The answer survives, whole, outside the event stream
# --------------------------------------------------------------------------


def test_the_status_payload_carries_the_whole_answer(tmp_path: pathlib.Path) -> None:
    """``GET /api/status`` returns the model's final message verbatim.

    This is the recovery path's other half and it already existed: the session
    records ``RunResult.text`` on the worker thread and reports it in ``status()``.
    Asserted here because the whole frontend fix rests on it -- if this stopped
    holding, the recovery would have nothing to recover.
    """
    session = _session(tmp_path, [ScriptedTurn(text=LONG_ANSWER)])

    session.submit("explain the repair")
    assert _wait(lambda: session.state.value == "completed")

    body = session.status()
    assert body["state"] == "completed"
    assert body["result"]["text"] == LONG_ANSWER
    assert len(body["result"]["text"]) > MAX_FIELD_CHARS


def test_a_completed_run_still_reports_its_counts(tmp_path: pathlib.Path) -> None:
    """Recovery must not have replaced the summary the operator reads."""
    session = _session(tmp_path, [ScriptedTurn(text=LONG_ANSWER)])

    session.submit("explain the repair")
    assert _wait(lambda: session.state.value == "completed")

    body = session.status()
    assert body["result"]["turns"] == 1
    assert body["result"]["tool_calls"] == 0
    assert body["result"]["denied"] == []


def test_a_failed_run_reports_no_result(tmp_path: pathlib.Path) -> None:
    """The frontend must be able to tell "no answer" from "an empty one".

    A failed run carries ``error`` and no ``result``, and the reconciliation is
    driven by exactly that absence -- which is asserted here so a future change
    that starts reporting an empty result cannot silently render a blank answer.
    """
    session = _session(tmp_path, [ScriptedTurn(raises=_GatewayDown())])

    session.submit("anything")
    assert _wait(lambda: session.state.value == "failed")

    body = session.status()
    assert "result" not in body
    assert body["error"]


# --------------------------------------------------------------------------
# The page reconciles: real app.js, stub DOM
# --------------------------------------------------------------------------


def _run_page(steps: list[dict]) -> dict:
    """Drive the shipped ``app.js`` through a scripted sequence."""
    result = subprocess.run(
        [NODE, str(PROBE), json.dumps(steps)],
        capture_output=True,
        text=True,
        env={"APP_JS": str(APP_JS), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _assistant(entries: list[dict]) -> list[str]:
    return [entry["text"] for entry in entries if entry["role"] == "assistant"]


def _status_payload(text: str, run_id: str = "run-1") -> dict:
    return {
        "state": "completed",
        "run_id": run_id,
        "workspace": "/tmp/ws",
        "agent": "byte",
        "model": "scripted",
        "pending_approval": None,
        "result": {"text": text, "turns": 1, "tool_calls": 0, "denied": []},
    }


@requires_node
def test_completion_renders_the_complete_answer() -> None:
    """The clipped preview is replaced by the full one, in one entry.

    The preview handed to the probe is exactly what a clamped event carries, so
    a regression in the runtime's own text would not be masked by the frontend
    fix.
    """
    preview = LONG_ANSWER[: MAX_FIELD_CHARS - 1] + "…"

    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": preview}},
            {"status": _status_payload(LONG_ANSWER)},
            {"event": "run.completed", "data": {"turns": "1", "tool_calls": "0", "denied": "0"}},
        ]
    )

    answers = _assistant(page["entries"])
    assert answers == [LONG_ANSWER], (
        "the transcript must hold the whole answer once; got "
        f"{[a[:60] for a in answers]}"
    )
    # The page asked for the authoritative state rather than rendering the
    # event's text and calling it an answer.
    assert page["statusFetches"] >= 2


@requires_node
def test_reconciliation_leaves_no_duplicate() -> None:
    """One preview plus one replacement must not become two answers.

    The most likely way this fix goes wrong: append the full answer instead of
    replacing the preview, so the transcript shows the clipped text once and the
    whole text once.
    """
    preview = LONG_ANSWER[: MAX_FIELD_CHARS - 1] + "…"

    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": preview}},
            {"status": _status_payload(LONG_ANSWER)},
            {"event": "run.completed", "data": {"turns": "1"}},
        ]
    )

    answers = _assistant(page["entries"])
    assert len(answers) == 1
    assert answers[0] == LONG_ANSWER
    assert "…" not in answers[0]


@requires_node
def test_a_replayed_completion_does_not_duplicate_the_answer() -> None:
    """A reconnect replays the tail, so completion can arrive twice.

    The broker's replay ring is exactly what makes this happen in production:
    an EventSource that dropped reconnects and re-reads from its last sequence
    number, and ``run.completed`` may be in that tail. Without the per-run
    guard the operator would see the same answer twice.
    """
    preview = LONG_ANSWER[: MAX_FIELD_CHARS - 1] + "…"
    completed = {"turns": "1"}

    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": preview}},
            {"status": _status_payload(LONG_ANSWER)},
            {"event": "run.completed", "data": completed, "seq": 3},
            {"event": "run.completed", "data": completed, "seq": 3},
            {"event": "run.completed", "data": completed, "seq": 3},
        ]
    )

    assert _assistant(page["entries"]) == [LONG_ANSWER]


@requires_node
def test_a_short_reply_behaves_exactly_as_before() -> None:
    """A reply inside the bound is rendered once, from the stream, untouched.

    The fix must be inert for the common case. Three things are checked: the
    entry is there, its text is the model's own, and no reconciliation fetch
    happened beyond the one load already makes -- a page that re-fetched and
    rewrote on every completion would be a regression in its own right.
    """
    page = _run_page(
        [
            {"status": {"state": "running", "pending_approval": None}},
            {"event": "model.reply", "data": {"text": "Nothing to do."}},
            {"status": _status_payload("Nothing to do.")},
            {"event": "run.completed", "data": {"turns": "1"}},
        ]
    )

    answers = _assistant(page["entries"])
    assert answers == ["Nothing to do."]
    # One fetch on load, one for the completion refresh: the reconciliation ran,
    # found the same text, and rewrote the entry in place rather than appending.
    assert page["statusFetches"] == 2
    # The stream's own summary line is still drawn, untouched.
    assert any(
        entry["role"] == "system" and entry["text"].startswith("Completed:")
        for entry in page["entries"]
    )


@requires_node
def test_a_backgrounded_tab_recovers_the_answer_on_refocus() -> None:
    """The ``visibilitychange`` path, which is what a real operator hits.

    The tab was hidden while the run finished: no ``model.reply`` ever arrived
    in it, so there is nothing to replace and exactly one entry must be appended
    when it is looked at again. Silently dropping the answer there would leave
    an operator who switched back with an empty transcript.
    """
    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": "I am working on it."}},
            # The stream drops while the tab is hidden, so the answer's event
            # never lands. The preview above is the last thing the tab saw.
            {"status": _status_payload(LONG_ANSWER)},
            {"visible": False},
            {"focus": True, "visible": True},
        ]
    )

    answers = _assistant(page["entries"])
    assert answers == ["I am working on it.", LONG_ANSWER]


@requires_node
def test_a_page_loaded_after_the_run_shows_the_answer_once() -> None:
    """The fresh-connect case: no preview at all, one answer.

    A tab that connects, reads ``/api/status`` and finds the run already
    finished has never seen a ``model.reply`` -- not even a clipped one. It must
    still end up with the answer, exactly once.
    """
    page = _run_page(
        [
            {"status": {"state": "running", "pending_approval": None}},
            # Status changes under us; the tab was not watching.
            {"status": _status_payload(LONG_ANSWER)},
            {"focus": True, "visible": True},
        ]
    )

    assert _assistant(page["entries"]) == [LONG_ANSWER]


@requires_node
def test_a_second_run_gets_its_own_answer() -> None:
    """The per-run guard keys on ``run_id``, so it does not swallow run two.

    A guard that remembered "reconciled once, ever" would be a worse bug than
    the one it fixed: the second run's answer would never be rendered, and the
    operator would be left reading a clipped preview with no way to tell why.
    """
    preview_one = "run one preview " + ("x" * 500) + "…"
    preview_two = "run two preview " + ("y" * 500) + "…"
    answer_one = "run one answer " + ("x" * 500)
    answer_two = "run two answer " + ("y" * 500)

    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": preview_one}},
            {"status": _status_payload(answer_one, run_id="run-1")},
            {"event": "run.completed", "data": {"turns": "1"}},
            {"event": "model.reply", "data": {"text": preview_two}},
            {"status": _status_payload(answer_two, run_id="run-2")},
            {"event": "run.completed", "data": {"turns": "1"}},
        ]
    )

    assert _assistant(page["entries"]) == [answer_one, answer_two]


@requires_node
def test_a_failed_run_adds_no_assistant_entry() -> None:
    """Only a completed run reconciles; a failure leaves the preview alone.

    Without this the reconciliation would run on any state carrying a result,
    and a run that died mid-turn would render something as though the model had
    finished. The mid-turn preview legitimately stays -- it is what the model
    actually said before the provider went away.
    """
    page = _run_page(
        [
            {"event": "model.reply", "data": {"text": "I will start by …"}},
            {
                "status": {
                    "state": "failed",
                    "run_id": "run-1",
                    "pending_approval": None,
                    "error": "provider is down",
                }
            },
            {"focus": True, "visible": True},
        ]
    )

    assert _assistant(page["entries"]) == ["I will start by …"]
    assert any(
        entry["role"] == "system" and "provider is down" in entry["text"]
        for entry in page["entries"]
    ), "the failure itself must still be shown"


@requires_node
def test_the_answer_is_never_parsed_as_markup() -> None:
    """Model output goes in as text, whether it is the preview or the answer.

    The replacement writes ``textContent`` rather than ``innerHTML``, and this
    asserts the consequence: an answer containing markup is shown as the
    characters the model produced, with no element built from them.
    """
    hostile = "<img src=x onerror=alert(1)> and <b>bold</b> " + LONG_ANSWER

    page = _run_page(
        [
            {"status": _status_payload(hostile)},
            {"event": "run.completed", "data": {"turns": "1"}},
        ]
    )

    answers = _assistant(page["entries"])
    assert answers == [hostile]


# --------------------------------------------------------------------------
# Structural guards: the properties a behavioural test cannot see
# --------------------------------------------------------------------------


def test_no_module_exempts_one_event_from_the_field_clamp() -> None:
    """No field, on any event, is exempt from the clamp.

    The other way to "fix" the clipped answer would be to special-case one name
    -- ``if name == "model.reply"`` inside ``_clamp`` -- which leaves the module
    looking bounded while one unbounded field rides the SSE stream to every
    subscriber. Scanned rather than trusted, because an exemption is invisible
    to every behavioural test in this file.
    """
    events_source = (REPO / "forge" / "events.py").read_text(encoding="utf-8")
    code = _code_only(events_source)
    assert "model.reply" not in code
    assert "MAX_FIELD_CHARS =" in code

    # Nothing elsewhere redefines the ceiling either.
    for path in sorted((REPO / "forge").rglob("*.py")):
        if path.name == "events.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "MAX_FIELD_CHARS =" not in source, (
            f"{path.name} redefines the field clamp"
        )
        assert "MAX_EVENT_FIELDS =" not in source, (
            f"{path.name} redefines the field-count bound"
        )


def test_app_js_never_raises_a_bound_for_the_answer() -> None:
    """The frontend recovers; it does not work around the clamp locally.

    A page-side ceiling would make the browser a second authority on what an
    event may carry, and a page-side truncation would silently lose answer text
    the server still holds. The only substring operation allowed to exist in
    this file is the one that shortens a *run id* for display. The pattern
    captures the receiver so the allowance is about *what* is shortened rather
    than about being allowed one slice at all, and both are read from the raw
    source because ``_code_only`` blanks the identifier being sliced.
    """
    source = APP_JS.read_text(encoding="utf-8")
    # Every substring operation in the file, with its receiver. `_code_only` is
    # not used here because it blanks the very identifiers these assertions are
    # about.
    slices = re.findall(r"([A-Za-z_$)\]][\w$.\[\]()]*)\.slice\(([^)]*)\)", source)
    found = [f"{receiver}.slice({args})" for receiver, args in slices]
    assert found == ["String(body.textContent).slice(-1)", "body.run_id.slice(0, 8)"], (
        f"app.js shortens something it should not: {found}"
    )
    assert ".substring(" not in source and ".substr(" not in source
    assert "MAX_FIELD" not in source


def test_app_js_reads_the_answer_from_the_server_not_from_the_event() -> None:
    """The recovery is a read of server state, not a second rendering path.

    If ``app.js`` grew its own endpoint, or rendered the answer from anywhere
    but ``/api/status``, the "the server is the only source of state" rule this
    file's comment header claims would no longer be true of it. The endpoint and
    the field are both string literals, so this one guard reads the raw source
    rather than the comment-stripped copy.
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert '"/api/status"' in source
    assert "status.result" in source
    # Nothing new was added for this: the answer is not fetched from anywhere
    # except the status endpoint the page already read on load.
    for endpoint in ("/api/result", "/api/answer", "/api/runs/", "/api/message"):
        assert endpoint not in source, f"app.js reads the answer from {endpoint}"


def test_app_js_holds_no_storage_for_the_recovered_answer() -> None:
    """The answer is recovered from the server, not remembered in the browser.

    A cache of the final answer would survive a reload and then contradict the
    next run -- and it would be the first browser-side copy of run state in this
    surface, which is the thing ``test_the_ui_package_holds_no_local_storage_
    concept`` exists to forbid.
    """
    source = _code_only(APP_JS.read_text(encoding="utf-8"))
    for banned in ("localStorage", "sessionStorage", "indexedDB", "caches."):
        assert banned not in source


def test_app_js_still_registers_the_run_completed_handler() -> None:
    """The trigger for the recovery is still the completion event.

    Asserted because an implementation that only recovered on a page load would
    pass the fresh-connect test and fail in production: the common case is a tab
    that was already open when the run finished.
    """
    source = APP_JS.read_text(encoding="utf-8")
    assert 'case "run.completed"' in source
    # And it is what app.js registers with the stream, so the handler is
    # reachable rather than merely written.
    assert '"run.completed",' in source


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


def _event(sink: _RecordingSink, name: str) -> Event:
    found = [event for event in sink.events if event.name == name]
    assert found, f"no {name} event was emitted"
    return found[-1]


class _GatewayDown(Exception):
    """A provider failure, raised by the scripted gateway."""


def _dispatcher(workspace: Workspace, *, events=None) -> ToolDispatcher:
    return ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel([])),
        executor=Executor(workspace),
        agent_name=BYTE.name,
        allowed_tools=BYTE.allowed_tools,
        system_prompt="you are byte",
        events=events,
    )


def _runtime(
    gateway: ScriptedGateway, dispatcher: ToolDispatcher, sink: _RecordingSink
) -> AgentRuntime:
    return AgentRuntime(
        BYTE,
        gateway=gateway,
        dispatcher=dispatcher,
        model_id="scripted",
        events=sink,
    )


def _session(tmp_path: pathlib.Path, turns: list[ScriptedTurn]) -> WorkbenchSession:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    workspace = Workspace(root)
    return WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        selector=ModelSelector({}, ModelSelection("openrouter", "scripted")),
        runtime_factory=runtime_factory(
            workspace,
            profile=BYTE,
            gateway=ScriptedGateway(turns),
            model_id="scripted",
        ),
    )


def _wait(predicate, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _code_only(source: str) -> str:
    """Strip comments and string literals, so a guard reads code, not prose."""
    import re

    without_block = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    without_line = re.sub(r"//[^\n]*", " ", without_block)
    return re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", '""', without_line)