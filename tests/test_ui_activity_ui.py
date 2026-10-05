"""The Workbench's derived activity line: what it says, and what it refuses to say.

The activity line is the one piece of UI in this repo whose failure mode is not
an error but a *plausible lie*. A status line that says "finished" about a
refused tool, or that renders a percentage, is worse than no line at all,
because the operator stops reading the feed underneath it. So the tests here
assert the exact strings, and they assert the absences explicitly -- an
assertion that only checks the happy path would pass just as happily on a
fabricated progress bar.

**Why a Node probe rather than a DOM test.** ``app.js`` is an IIFE that reads the
DOM, fetches, and listens on an ``EventSource``. ``tests/ui_probe/
activity_probe.js`` supplies all three and drives a scripted sequence, printing
the label after each step. That makes the assertion about the *text an operator
would read*, not about the presence of a function call.

**The clock is the probe's.** ``Date.now()`` in the sandbox is the probe's, so
"no activity for 47s" is asserted by advancing a number rather than by sleeping
47 seconds. A test that waits is a test that eventually gets deleted.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
from typing import TypedDict

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = pathlib.Path(__file__).resolve().parent / "ui_probe" / "activity_probe.js"
APP_JS = REPO / "forge" / "ui" / "static" / "app.js"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to drive app.js under a stub DOM"
)


class Step(TypedDict, total=False):
    """One probe step, as the probe's docstring describes them."""

    status: dict[str, object]
    event: str
    data: dict[str, object]
    age: int
    toggle: bool
    close: bool
    escape: bool
    focus: bool


class Observed(TypedDict):
    label: str
    kind: str
    open: bool
    ariaExpanded: str | None


class Report(TypedDict):
    steps: list[Observed]
    storage: list[str]
    intervalsRunning: int


def drive(*steps: Step) -> Report:
    """Run the probe over ``steps`` and return its report.

    A probe failure -- a JS exception, an unregistered event name -- raises here
    rather than returning an empty report, so a broken probe is an error and not
    a silently passing test.
    """
    env = dict(os.environ)
    env["APP_JS"] = str(APP_JS)
    result = subprocess.run(
        [str(NODE), str(PROBE), json.dumps(list(steps))],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"the activity probe failed:\n{result.stderr.strip()}\n"
            f"steps were: {json.dumps(list(steps))}"
        )
    return json.loads(result.stdout)


def labels(report: Report) -> list[str]:
    return [step["label"] for step in report["steps"]]


def kinds(report: Report) -> list[str]:
    return [step["kind"] for step in report["steps"]]


# -- the whole vocabulary -------------------------------------------------


def test_every_event_in_the_vocabulary_reduces_to_its_exact_label() -> None:
    """One run, one step per event, asserted as a list.

    Written as a single test on purpose. These labels are the contract: a test
    per label would let a change to one be accepted while the sequence around it
    silently shifted, and the sequence is what an operator actually reads.
    """
    report = drive(
        {"event": "run.submitted", "data": {"run_id": "a1", "task": "t"}},
        {"event": "run.started", "data": {"agent": "Byte", "model": "m"}},
        {"event": "model.reply", "data": {"tool_calls": "2", "turn": "1", "text": "x"}},
        {"event": "tool.call", "data": {"tool": "read_file", "call_id": "c1"}},
        {
            "event": "tool.result",
            "data": {"tool": "read_file", "status": "ok", "stdout_bytes": "10"},
        },
        {"event": "approval.required", "data": {"approval_id": "ap1"}},
        {"event": "approval.answered", "data": {"approval_id": "ap1"}},
        {"event": "run.completed", "data": {"turns": "2", "tool_calls": "1"}},
    )

    assert labels(report) == [
        "Starting",
        "Byte is working",
        "Byte is choosing a tool",
        "Running read_file",
        "read_file finished",
        "Waiting for your approval",
        "Resuming after your decision",
        "Completed",
    ]
    assert kinds(report) == [
        "work",
        "work",
        "work",
        "work",
        "good",
        "wait",
        "work",
        "good",
    ]


def test_a_refused_tool_never_reads_as_success() -> None:
    """The safety-relevant one.

    A ``tool.result`` whose status means the operation did not happen must read
    as a refusal. If this ever says "finished", an operator glancing at the
    conversation header would believe a declined operation ran -- and would stop
    reading the feed row that says otherwise.
    """
    report = drive(
        {"event": "tool.call", "data": {"tool": "exec_command", "call_id": "c1"}},
        {
            "event": "tool.result",
            "data": {
                "tool": "exec_command",
                "status": "denied",
                "detail": "gated by rule",
            },
        },
    )

    assert labels(report)[-1] == "exec_command was refused"
    assert kinds(report)[-1] == "bad"
    # And explicitly not the success wording, in case someone rewords it later.
    assert "finished" not in labels(report)[-1]


@pytest.mark.parametrize(
    "status",
    ["denied", "rejected", "denied_unattended", "error"],
)
def test_every_refusal_status_is_a_refusal(status: str) -> None:
    """Each status in the feed's own refusal set, not just the first.

    These four are exactly ``BAD_STATUS`` in app.js, which is what colours the
    feed red. If the line's set and the feed's set ever diverge, the header and
    the log would tell an operator two different things about one operation.
    """
    report = drive(
        {
            "event": "tool.result",
            "data": {"tool": "write_file", "status": status},
        }
    )

    assert labels(report) == ["write_file was refused"]
    assert kinds(report) == ["bad"]


def test_an_unrecognised_tool_status_is_reported_not_smoothed_over() -> None:
    """A status this build does not know is shown, not flattened to success.

    If a new backend status appears, the line must say what it is rather than
    guess. This is the case where inventing a friendly label would be most
    tempting and most wrong.
    """
    report = drive(
        {"event": "tool.result", "data": {"tool": "list_dir", "status": "partial"}}
    )

    assert labels(report) == ["list_dir returned partial"]
    assert kinds(report) == ["warn"]


# -- what it must never say ----------------------------------------------


FORBIDDEN = [
    "%",
    "percent",
    "almost done",
    "nearly",
    "thinking",
    "estimating",
    "eta",
    "remaining time",
    "left to go",
    "step 3 of",
    "progress",
]


def test_the_line_never_claims_progress_or_eta() -> None:
    """No label may contain a progress or time-remaining claim.

    Asserted over the whole vocabulary rather than per-label, because the
    temptation to add "42%" or "almost done" is exactly the kind of change that
    arrives attached to a single new label and would otherwise pass review.
    """
    report = drive(
        {"event": "run.submitted", "data": {"run_id": "a", "task": "t"}},
        {"event": "run.started", "data": {"agent": "Byte", "model": "m"}},
        {"event": "model.reply", "data": {"tool_calls": "1", "text": "x"}},
        {"event": "tool.call", "data": {"tool": "exec_command", "call_id": "c"}},
        {"event": "tool.result", "data": {"tool": "exec_command", "status": "ok"}},
        {"event": "approval.required", "data": {"approval_id": "p"}},
        {"event": "run.completed", "data": {"turns": "3", "tool_calls": "3"}},
        {"event": "run.failed", "data": {"reason": "gateway_error", "detail": "500"}},
    )

    for text in labels(report):
        lowered = text.lower()
        for banned in FORBIDDEN:
            assert banned not in lowered, f"{text!r} contains {banned!r}"


def test_no_model_prose_can_reach_the_line() -> None:
    """``model.reply.text`` is never read by the reducer.

    The clamped model text is the one place in the event vocabulary where a
    model's own words -- potentially its reasoning -- could arrive. The line is
    a status; it must be built from event *names* and structural fields only.
    """
    secret = "FIRST I WILL REASON ABOUT THE PRIVATE FACTORY SECRETS"
    report = drive(
        {"event": "model.reply", "data": {"text": secret, "tool_calls": "1", "turn": "1"}},
        {"event": "model.reply", "data": {"text": secret, "tool_calls": "0", "turn": "2"}},
    )

    for text in labels(report):
        assert "SECRET" not in text
        assert "PRIVATE" not in text
        assert "REASON" not in text
    assert labels(report) == ["Byte is choosing a tool", "Byte is writing an answer"]


def test_no_tool_arguments_are_reachable() -> None:
    """The line reads a tool's *name*, never its arguments.

    ``tool.call`` deliberately publishes only ``tool`` and ``call_id`` --
    the dispatcher never emits arguments, because a model may put file contents
    or a command line in them. This asserts the line does not go looking for
    anything beyond what arrived.
    """
    report = drive(
        {
            "event": "tool.call",
            "data": {
                "tool": "exec_command",
                "call_id": "c1",
                "arguments": "rm -rf /",
                "detail": "the whole command line",
            },
        }
    )

    assert labels(report) == ["Running exec_command"]
    assert "rm -rf" not in labels(report)[0]
    assert "command line" not in labels(report)[0]


def test_an_unknown_event_falls_back_to_a_safe_label() -> None:
    """An event this build does not recognise must not be guessed at.

    ``stream.dropped`` is a real event with no case of its own, so it exercises
    the same ``default:`` branch a genuinely new backend event would.
    """
    report = drive({"event": "stream.dropped", "data": {"dropped": "7"}})

    assert labels(report) == ["Working"]
    assert kinds(report) == ["work"]


def test_an_event_missing_its_field_does_not_render_undefined() -> None:
    """A known event with a missing field must not print "undefined".

    Events arrive over SSE and a future emitter could omit a field. Rendering
    ``undefined`` into an operator-facing status is the sort of small dishonesty
    that makes a page untrustworthy, so the fallback text is a real phrase.
    """
    report = drive({"event": "tool.call", "data": {}})

    assert labels(report) == ["Running a tool"]
    assert "undefined" not in labels(report)[0]


# -- the liveness note ----------------------------------------------------


def test_no_note_before_the_threshold() -> None:
    """A note at 9 seconds would be noise.

    Below the threshold the line is exactly the base label. The threshold is
    asserted as a boundary from both sides rather than as one value, because a
    threshold that drifts to 1s would train an operator to ignore the one line
    that means something.
    """
    report = drive(
        {"event": "tool.call", "data": {"tool": "exec_command", "call_id": "c"}},
        {"age": 9000},
    )

    assert labels(report)[-1] == "Running exec_command"
    assert "No activity" not in labels(report)[-1]


def test_the_note_reports_only_elapsed_silence() -> None:
    """"No activity for 47s" -- the one claim it is allowed to make.

    The base label is still there: the note is appended, never substituted, so
    the last real fact about the run is not replaced by a clock.
    """
    report = drive(
        {"event": "tool.call", "data": {"tool": "exec_command", "call_id": "c"}},
        {"age": 47000},
    )

    assert labels(report)[-1] == "Running exec_command · No activity for 47s"


def test_every_event_resets_the_silence_count() -> None:
    """A new event clears the note entirely.

    Reset-on-event is what makes the note mean anything: a timer that carried
    forward would report "no activity for 47s" immediately after an activity,
    which is a lie about the stream.
    """
    report = drive(
        {"event": "tool.call", "data": {"tool": "exec_command", "call_id": "c"}},
        {"age": 47000},
        {"event": "tool.result", "data": {"tool": "exec_command", "status": "ok"}},
        {"age": 1000},
    )

    assert labels(report)[1].endswith("No activity for 47s")
    assert labels(report)[2] == "exec_command finished"
    assert labels(report)[3] == "exec_command finished"


def test_the_note_never_appears_while_idle() -> None:
    """No run in flight, no count.

    A page left open on an idle session must not tick a timer: it would be
    reporting silence about a run that does not exist.
    """
    report = drive({"status": {"state": "idle"}}, {"age": 60000})

    assert "No activity" not in labels(report)[-1]
    assert labels(report)[-1] == "Idle"


def test_a_finished_run_stops_the_timer() -> None:
    """Completion is terminal: the interval must not survive it.

    Checked through the probe's own count of live timers, because a page that
    keeps ticking after the answer arrives is a page that will still be ticking
    tomorrow.
    """
    report = drive(
        {"event": "run.started", "data": {"agent": "Byte", "model": "m"}},
        {"event": "run.completed", "data": {"turns": "1", "tool_calls": "0"}},
        {"age": 120000},
    )

    assert report["intervalsRunning"] == 0
    assert "No activity" not in labels(report)[-1]


def test_a_live_run_reported_by_the_server_does_tick() -> None:
    """A page that loads mid-run still reports liveness.

    The tab did not witness ``run.submitted``, but the server says a run is in
    flight, and the note is about the stream rather than about what this tab
    happened to see.
    """
    report = drive(
        {"status": {"state": "running", "pending_approval": None}},
        {"age": 25000},
    )

    assert labels(report)[-1] == "Byte is working · No activity for 25s"


# -- the sheet ------------------------------------------------------------


def test_the_sheet_opens_and_closes_and_stays_in_step_with_aria() -> None:
    """`data-activity-open` and `aria-expanded` must never disagree.

    They are the same fact stated twice -- once for CSS, once for assistive
    tech -- and a page where the button says "collapsed" while the sheet is open
    is worse than one with no button.
    """
    report = drive({"toggle": True}, {"close": True}, {"toggle": True}, {"escape": True})

    states = [(step["open"], step["ariaExpanded"]) for step in report["steps"]]
    assert states == [(True, "true"), (False, "false"), (True, "true"), (False, "false")]


def test_escape_only_closes_an_open_sheet() -> None:
    """Escape with no sheet open belongs to the approval dialog.

    The approval card is the only thing on this page that blocks progress, so
    nothing may quietly take its escape key. The stub records the document's own
    listeners, so this asserts the handler declines rather than fires blindly.
    """
    report = drive({"event": "run.started", "data": {"agent": "B", "model": "m"}}, {"escape": True})

    assert report["steps"][-1]["open"] is False
    # The run is still shown as running: escape closed nothing that mattered.
    assert report["steps"][-1]["kind"] == "work"


def test_the_sheet_keeps_receiving_events_while_closed() -> None:
    """Closing the sheet must not stop the feed recording events.

    A sheet that only listened while open would show an empty panel for the run
    it was opened to watch. This asserts the *line* keeps advancing with the
    sheet closed, which is the same code path the feed row uses.
    """
    report = drive(
        {"event": "tool.call", "data": {"tool": "read_file", "call_id": "c"}},
        {"close": True},
        {"event": "tool.result", "data": {"tool": "read_file", "status": "ok"}},
    )

    assert report["steps"][-1]["label"] == "read_file finished"
    assert report["steps"][-1]["open"] is False


# -- the tab's own promises ----------------------------------------------


def test_nothing_is_written_to_browser_storage() -> None:
    """The NO STORAGE rule still holds for the new code.

    The probe hands app.js working localStorage, sessionStorage, and indexedDB
    objects that record every call, so this is an assertion about the new code
    rather than a grep that could be satisfied by a differently-named API.
    """
    report = drive(
        {"event": "run.submitted", "data": {"run_id": "a", "task": "t"}},
        {"event": "run.started", "data": {"agent": "B", "model": "m"}},
        {"event": "tool.call", "data": {"tool": "read_file", "call_id": "c"}},
        {"toggle": True},
        {"age": 20000},
    )

    assert report["storage"] == []


def test_a_stale_status_read_does_not_overwrite_a_newer_event() -> None:
    """The snapshot-vs-event race.

    ``approval.required`` calls ``refreshStatus``. That request is issued at the
    moment the approval opens, so its answer describes the state the server had
    when it was asked -- which, for a read that is answered a moment later, can
    already be stale. Without a guard, the line briefly read "Idle" while an
    approval card was open: exactly the failure this line was added to prevent.

    Modelled as a read that is *already in flight*: the event arrives, and only
    then does the pre-approval snapshot resolve. The epoch stamped at request
    time no longer matches, so the stale answer is discarded rather than drawn.
    """
    report = drive(
        {"event": "approval.required", "data": {"approval_id": "p1"}},
        # `focus: false`: this is the in-flight read resolving to the state the
        # server held BEFORE the approval, not a fresh read after it.
        {"status": {"state": "idle", "pending_approval": None}, "focus": False},
    )

    assert labels(report)[-1] == "Waiting for your approval"
    assert kinds(report)[-1] == "wait"


def test_a_fresh_status_read_does_reconcile_the_line() -> None:
    """The guard must not make the line deaf to the server.

    The mirror of the test above. If the epoch check were simply "ignore any
    status", a tab whose stream had dropped would keep showing a stale label for
    a run that had already finished -- so a read that finds no newer event is
    applied normally.
    """
    report = drive(
        {"event": "run.started", "data": {"agent": "Byte", "model": "m"}},
        {"status": {"state": "completed", "pending_approval": None, "result": None}},
    )

    assert labels(report)[-1] == "Completed"
    assert kinds(report)[-1] == "good"
    # And the timer stops: the server says nothing is in flight.
    assert report["intervalsRunning"] == 0
