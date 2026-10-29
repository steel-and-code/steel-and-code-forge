"""Regression tests for the Workbench's live run-status (FORGE-9 repair).

These pin the behaviour that broke: while a Byte run is still in flight, the
runtime and dispatcher emit ``run.*`` / ``model.*`` / ``tool.*`` lifecycle
events to the session's record-updating funnel, so ``GET /api/status`` advances
(turns, tool calls, activity, liveness) before the run completes -- and each
event still reaches the SSE broker exactly once.

The gateway here is the same :class:`~forge.models.scripted.ScriptedGateway`
every other UI test uses, wrapped so a specific turn blocks on a
:class:`threading.Event`. That deterministic block is what lets a test read
``status()`` mid-run rather than racing the worker thread.
"""

from __future__ import annotations

import pathlib
import threading
import time
from typing import cast

import pytest

from forge.agents.byte import BYTE
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.workspace import Workspace
from forge.ui.broker import SseBroker
from forge.ui.runner import runtime_factory
from forge.ui.session import WorkbenchSession


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


class GatedGateway:
    """Wrap ScriptedGateway and block before the ``block_on_call``-th ``complete``.

    ``blocked`` is set the moment the worker reaches the gated turn; the worker
    then waits on ``release`` so a test can read ``status()`` while the run is
    genuinely still RUNNING (rather than racing it to completion).
    """

    def __init__(self, turns, block_on_call: int) -> None:
        self.inner = ScriptedGateway(turns)
        self.block_on_call = block_on_call
        self.calls = 0
        self.release = threading.Event()
        self.blocked = threading.Event()

    def complete(self, request):
        self.calls += 1
        if self.calls == self.block_on_call:
            self.blocked.set()
            if not self.release.wait(timeout=20):
                raise RuntimeError("gated gateway was never released")
        return self.inner.complete(request)


def _make_session(workspace: Workspace, gateway):
    broker = SseBroker()
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id="scripted",
        runtime_factory=runtime_factory(
            workspace, profile=BYTE, gateway=gateway, model_id="scripted"
        ),
        broker=broker,
    )
    return session, broker


def _wait_state(session: WorkbenchSession, state: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.state.value == state:
            return
        time.sleep(0.005)
    raise AssertionError(f"session did not reach {state!r}; is {session.state.value!r}")


def test_mid_run_status_reports_live_progress(workspace: Workspace) -> None:
    """While a run is in flight, status() shows real turns/tool calls/activity."""
    (workspace.root / "notes.txt").write_text("hello")
    gateway = GatedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "notes.txt"}),)),
            ScriptedTurn(text="all done"),
        ],
        block_on_call=2,
    )
    session, _ = _make_session(workspace, gateway)

    session.submit("read the notes then answer")
    assert gateway.blocked.wait(timeout=5), "worker never reached the gated turn"

    status = session.status()
    assert status["state"] == "running"
    # One model.reply and one tool dispatch have already happened on turn 1.
    assert status["turns"] == 1, status
    assert status["tool_calls"] == 1, status
    # Activity moved off its initial "Starting" placeholder.
    assert status["activity"] not in ("Starting", "Idle"), status

    gateway.release.set()
    _wait_state(session, "completed")
    final = session.status()
    assert cast(dict, final["result"])["text"] == "all done"
    assert final["turns"] == 2
    assert final["tool_calls"] == 1


def test_lifecycle_events_reach_the_broker_exactly_once(workspace: Workspace) -> None:
    """The repair routes events through the funnel, not around it -- no double emit."""
    (workspace.root / "notes.txt").write_text("hello")
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "notes.txt"}),)),
            ScriptedTurn(text="done"),
        ]
    )
    session, broker = _make_session(workspace, gateway)

    session.submit("read then answer")
    _wait_state(session, "completed")

    events, complete = broker.replay_since(0)
    assert complete, "broker ring could not be drained whole"
    counts: dict[str, int] = {}
    for _, event in events:
        counts[event.name] = counts.get(event.name, 0) + 1

    # Each lifecycle event is published exactly once; model.reply is once per turn.
    assert counts["run.started"] == 1, counts
    assert counts["run.completed"] == 1, counts
    assert counts["tool.call"] == 1, counts
    assert counts["tool.result"] == 1, counts
    assert counts["model.reply"] == 2, counts
    assert counts["run.submitted"] == 1, counts


def test_status_reads_are_thread_safe_during_a_run(workspace: Workspace) -> None:
    """Concurrent status() readers against an active run: no deadlock, consistent counts."""
    (workspace.root / "notes.txt").write_text("hello")
    # Enough tool-call turns to keep the worker emitting while readers hammer.
    turns = [ScriptedTurn(tool_calls=(("read_file", {"path": "notes.txt"}),)) for _ in range(20)]
    turns.append(ScriptedTurn(text="finished"))
    session, _ = _make_session(workspace, ScriptedGateway(turns))

    session.submit("read repeatedly")

    errors: list[BaseException] = []
    seen: list[tuple[int, int]] = []

    def reader() -> None:
        try:
            prev = (-1, -1)
            for _ in range(200):
                t = session.status()["turns"]
                tc = session.status()["tool_calls"]
                # turns and tool_calls only move forward within one reader.
                assert (t, tc) >= prev, f"counts regressed: {(t, tc)} after {prev}"
                prev = (t, tc)
        except BaseException as exc:  # noqa: BLE001 - collect, don't fail mid-test
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads), "a reader thread is stuck"
    _wait_state(session, "completed")
    final = session.status()
    assert final["turns"] == 21
    assert final["tool_calls"] == 20
    assert cast(dict, final["result"])["text"] == "finished"