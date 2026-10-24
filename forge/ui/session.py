"""One Workbench run: its state machine, and the worker thread it runs on.

:class:`WorkbenchSession` exists because of a single structural fact: the
runtime is synchronous and blocking, while the surface that watches it is
asynchronous. The reconciliation is a thread, and this class is where the thread
lives.

*   :meth:`WorkbenchSession.submit` starts **one** daemon thread and returns
    immediately. The event loop is never inside ``runtime.run()``.
*   State lives in one place, under a lock, and only moves forward through
    :class:`UiSessionState`. There is no cancellation and no ``CANCELLED``:
    stopping a run that is blocked in an approval prompt would need cooperation
    from the executor and the classifier, and a Stop button that cannot reliably
    stop is worse than no button at all.
*   A second task while a run is in flight is a **conflict**. Not a queue. One
    process, one run; queueing would imply the second task eventually runs, and
    an operator who typed a second task deserves to know immediately that it did
    not.

The five states are exactly the ones the design settled on:

``IDLE``
    Nothing has been submitted, or the last run finished and the operator has
    not started another.
``RUNNING``
    A worker thread is inside the runtime.
``WAITING_FOR_APPROVAL``
    The worker is blocked in ``WebApprovalChannel.present()``. Set by the
    channel's callback, not by polling -- the session learns about a pending
    prompt the same way the browser does, through a callback rather than by
    looking inside something it does not own.
``COMPLETED``
    The runtime returned.
``FAILED``
    It raised. A gateway failure and an exhausted turn budget are both reported
    as a failure rather than a quiet stop, because a run that simply stopped
    emitting is indistinguishable from a hang.

Everything a status request needs is derived here, including the pending
approval. That is what makes a refresh work with no browser-side storage: the
tab asks the server what is true now, and the server answers from the one
place that knows.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from ..agents.profile import AgentProfile
from ..agents.runtime import AgentRuntime, MaxTurnsExceeded
from ..agents.state import ConversationStateStore
from ..events import emit
from ..external.base import ExternalResult
from ..models.base import GatewayError
from ..tools.workspace import Workspace
from .broker import SseBroker
from .channel import WebApprovalChannel

__all__ = ["UiSessionState", "WorkbenchSession", "SessionBusy", "RunSummary"]


class UiSessionState(str, Enum):
    """The five states a Workbench session can be in.

    A ``str`` enum for the same reason :class:`~forge.tools.workspace.
    Classification` is one: the value serialises into JSON and a page can
    compare it without knowing Python.

    ``CANCELLED`` is deliberately absent. It exists in most designs of this
    shape and is wrong here, because nothing in the current runtime or executor
    supports cooperative cancellation -- see the module docstring.
    """

    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionBusy(RuntimeError):
    """A run is already in flight in this process.

    Carries the active ``run_id`` so the caller can tell the operator *which*
    run they collided with, rather than refusing an action that looks like it
    did nothing at all.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"a run is already in progress ({run_id}); one workbench process "
            f"serves one run at a time"
        )
        self.run_id = run_id


@dataclass(frozen=True)
class RunSummary:
    """What one finished run produced, as the page sees it.

    Attributes:
        text: The model's final message. Its own words.
        turns: Model round-trips.
        tool_calls: Individual dispatches.
        denied: Permission refusals, as ``(tool, detail)`` pairs. Carried
            because an operator needs to know what was refused without reading
            the transcript.
    """

    text: str
    turns: int
    tool_calls: int
    denied: tuple[tuple[str, str], ...] = ()


@dataclass
class _RunRecord:
    """Mutable bookkeeping for the run in flight.

    Everything here is written on the worker thread and read from a request
    handler, so each field that matters is written once and never mutated
    afterwards -- with the exception of ``state``, which moves through
    :meth:`WorkbenchSession._set_state` under the session lock.
    """

    run_id: str
    task: str
    started: str
    thread: threading.Thread | None = None
    summary: RunSummary | None = None
    hermes: ExternalResult | None = None
    error: str | None = None
    finished_at: str | None = None
    max_turns: int | None = None
    turns: int = 0
    tool_calls: int = 0
    activity: str = "Starting"
    last_event_at: float | None = None
    history: list[dict[str, object]] = field(default_factory=list)


class WorkbenchSession:
    """One run at a time, on one thread, observable through a broker.

    Args:
        workspace: The run's single fixed workspace root. Held for the process:
            no request can change it, because a browser-reachable parameter
            naming a directory would turn every other control into a
            suggestion.
        profile: Whose run this is.
        model_id: Resolved provider model identifier.
        runtime_factory: Builds an :class:`AgentRuntime` for one run, given the
            broker and the approval channel. Injected so the CLI can wire a real
            gateway and a test can wire :class:`ScriptedGateway` without either
            knowing about the other.
        broker: Where observational events go.
        channel: The approval bridge. When ``None``, a
            :class:`WebApprovalChannel` bound to this session is created, which
            is what lets the session drive the ``WAITING_FOR_APPROVAL``
            transition itself.
        hermes_runner: The external-Hermes execution path, from
            :func:`forge.ui.runner.build_hermes_runner`. When ``None``, the
            session has no Hermes path and :meth:`submit_hermes` refuses. It is
            a separate runner from ``runtime_factory`` because a Hermes run is a
            subprocess Forge cannot observe, not an ``AgentRuntime``; the two
            share the state machine and the broker but nothing else.

    Not thread-safe for construction and use at the same time: build it, then
    serve. After that, every public method is callable from the event loop.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        profile: AgentProfile,
        model_id: str,
        runtime_factory: Callable[[SseBroker, WebApprovalChannel], AgentRuntime],
        broker: SseBroker | None = None,
        channel: WebApprovalChannel | None = None,
        hermes_runner: Callable[[SseBroker, str, str | None], ExternalResult] | None = None,
    ) -> None:
        self.workspace = workspace
        self.profile = profile
        self.model_id = model_id
        self.broker = broker if broker is not None else SseBroker()
        self._runtime_factory = runtime_factory
        self._hermes_runner = hermes_runner
        self._conversation_states = ConversationStateStore()

        self._lock = threading.Lock()
        self._state = UiSessionState.IDLE
        self._record: _RunRecord | None = None

        if channel is not None:
            self.channel = channel
        else:
            self.channel = WebApprovalChannel(
                emit=self._emit_event, on_state_change=self._on_pending_change
            )

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> UiSessionState:
        with self._lock:
            return self._state

    def _set_state(self, state: UiSessionState) -> None:
        with self._lock:
            self._state = state
        self._emit_event("session.state", state=state.value)

    def _on_pending_change(self, pending_now: bool) -> None:
        """The channel telling us a prompt opened or closed.

        Only moves to ``WAITING_FOR_APPROVAL`` when a run is actually in flight.
        A pending approval with no run would be a state the page could not act
        on, and the session is the only thing that knows there is a run.
        """
        with self._lock:
            record = self._record
            running = record is not None and record.thread is not None and record.thread.is_alive()

        if not running:
            return
        self._set_state(
            UiSessionState.WAITING_FOR_APPROVAL
            if pending_now
            else UiSessionState.RUNNING
        )

    @property
    def busy(self) -> bool:
        """Whether a worker thread is live."""
        with self._lock:
            record = self._record
        return record is not None and record.thread is not None and record.thread.is_alive()

    # -- running ---------------------------------------------------------

    def submit(self, task: str) -> str:
        """Start one run on a worker thread. Returns the run id.

        Raises:
            ValueError: ``task`` is empty. Refused before anything starts,
                because a run launched with nothing to do would spend a model
                round-trip to discover that.
            SessionBusy: a run is already in flight.
        """
        cleaned = task.strip()
        if not cleaned:
            raise ValueError("no task given; nothing to run")

        run_id = uuid.uuid4().hex
        record = _RunRecord(run_id=run_id, task=cleaned, started=_now_iso())

        with self._lock:
            existing = self._record
            if (
                existing is not None
                and existing.thread is not None
                and existing.thread.is_alive()
            ):
                raise SessionBusy(existing.run_id)
            self._record = record

        self._set_state(UiSessionState.RUNNING)
        self._emit_event("run.submitted", run_id=run_id, task=cleaned)

        thread = threading.Thread(
            target=self._execute,
            args=(record,),
            name=f"forge-workbench-{run_id[:8]}",
            # Daemon: a forgotten browser tab must not stop the process exiting.
            # `release_all` is what makes an exit prompt rather than a hang.
            daemon=True,
        )
        record.thread = thread
        thread.start()
        return run_id

    def _execute(self, record: _RunRecord) -> None:
        """The worker body. Everything a run does happens on this thread.

        Exceptions are converted to a failed state, never swallowed: a run that
        ended for a reason nobody recorded is indistinguishable from a hang, and
        the page needs to be able to say which it was.
        """
        runtime = self._runtime_factory(self.broker, self.channel)
        with self._lock:
            record.max_turns = runtime.max_turns
        try:
            state = self._conversation_states.get(self.workspace.root, self.profile.name)
            result = runtime.run(record.task, state=state)
        except MaxTurnsExceeded as exc:
            self._fail(record, str(exc))
            return
        except GatewayError as exc:
            self._fail(record, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - worker boundary
            self._fail(record, f"{exc.__class__.__name__}: {exc}")
            return

        summary = RunSummary(
            text=result.text,
            turns=result.turns,
            tool_calls=result.tool_calls,
            denied=result.denied,
        )
        with self._lock:
            record.summary = summary
            record.turns = result.turns
            record.tool_calls = result.tool_calls
            record.activity = "Completed"
            record.finished_at = _now_iso()
        self._set_state(UiSessionState.COMPLETED)

    def submit_hermes(self, task: str, profile: str | None = None) -> str:
        """Start one text-only Hermes run on a worker thread. Returns the run id.

        The same one-run-at-a-time rule as :meth:`submit`, the same refusal on
        an empty task, and the same run id shape. What differs is the executor:
        the thread calls the injected Hermes runner, which spawns the external
        subprocess, rather than the native ``AgentRuntime``.

        Raises:
            ValueError: ``task`` is empty.
            SessionBusy: a run is already in flight.
            RuntimeError: this session was built without a Hermes runner, so
                there is no Hermes path to take.
        """
        if self._hermes_runner is None:
            raise RuntimeError(
                "this workbench session has no Hermes runner; the external "
                "Hermes path was not wired in"
            )

        cleaned = task.strip()
        if not cleaned:
            raise ValueError("no task given; nothing to run")

        run_id = uuid.uuid4().hex
        record = _RunRecord(run_id=run_id, task=cleaned, started=_now_iso())

        with self._lock:
            existing = self._record
            if (
                existing is not None
                and existing.thread is not None
                and existing.thread.is_alive()
            ):
                raise SessionBusy(existing.run_id)
            self._record = record

        self._set_state(UiSessionState.RUNNING)
        self._emit_event(
            "run.submitted", run_id=run_id, task=cleaned, profile=profile or ""
        )

        thread = threading.Thread(
            target=self._execute_hermes,
            args=(record, profile),
            name=f"forge-workbench-hermes-{run_id[:8]}",
            daemon=True,
        )
        record.thread = thread
        thread.start()
        return run_id

    def _execute_hermes(self, record: _RunRecord, profile: str | None) -> None:
        """The Hermes worker body, on its own thread.

        Mirrors :meth:`_execute` in shape: any exception is converted to a
        failed state, never swallowed. The difference is that the "runtime" is
        the injected Hermes runner -- a subprocess Forge cannot observe -- so
        there is no turn budget, no tool-call count, and no approval state to
        record. What survives is the parsed :class:`ExternalResult`, whose text,
        model, and session id become the run's observable outcome.
        """
        runner = self._hermes_runner
        if runner is None:  # pragma: no cover - guarded by submit_hermes
            self._fail(record, "no Hermes runner configured")
            return

        try:
            result = runner(self.broker, record.task, profile)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            self._fail(record, f"{exc.__class__.__name__}: {exc}")
            return

        with self._lock:
            record.hermes = result
            record.activity = "Completed" if result.ok else "Failed"
            record.finished_at = _now_iso()
        if result.ok:
            self._set_state(UiSessionState.COMPLETED)
        else:
            self._set_state(UiSessionState.FAILED)

    def _fail(self, record: _RunRecord, message: str) -> None:
        with self._lock:
            record.error = message
            record.finished_at = _now_iso()
        self._emit_event("run.error", detail=message)
        self._set_state(UiSessionState.FAILED)

    # -- observation -----------------------------------------------------

    def _emit_event(self, name: str, **fields: object) -> None:
        """Record and publish one observable event through the existing broker."""
        now = time.monotonic()
        with self._lock:
            record = self._record
            if record is not None:
                record.last_event_at = now
                if name == "model.reply":
                    record.turns = int(fields.get("turn", record.turns))
                elif name == "tool.call":
                    record.tool_calls += 1
                if name == "approval.required":
                    record.activity = "Waiting for approval"
                elif name == "tool.call":
                    record.activity = f"Running {fields.get('tool', 'a tool')}"
                elif name == "run.completed":
                    record.activity = "Completed"
                elif name in {"run.failed", "run.error"}:
                    record.activity = "Failed"
                elif name == "model.reply":
                    record.activity = "Model working"
                elif name == "run.started":
                    record.activity = "Model working"
        emit(self.broker, name, **fields)

    def status(self) -> dict[str, object]:
        """Everything a page needs to render the current state.

        Includes the pending approval, which is what makes a refresh work: the
        tab held no approval state, asks here, and gets the same approval id
        it had before the reload. No localStorage, because a browser-side copy
        of approval state is a browser-side claim about what Forge has decided,
        and Forge is the only thing that knows that.
        """
        with self._lock:
            state = self._state
            record = self._record

        body: dict[str, object] = {
            "state": state.value,
            "workspace": str(self.workspace.root),
            "agent": self.profile.name,
            "model": self.model_id,
            "pending_approval": self.channel.pending_public(),
        }

        if record is None:
            body["run_id"] = None
            return body

        body["run_id"] = record.run_id
        body["task"] = record.task
        body["started_at"] = record.started
        body["max_turns"] = record.max_turns
        body["turns"] = record.turns
        body["tool_calls"] = record.tool_calls
        body["activity"] = record.activity
        body["elapsed_seconds"] = _elapsed_seconds(record.started, record.finished_at)
        if record.last_event_at is not None:
            body["no_activity_seconds"] = max(0, int(time.monotonic() - record.last_event_at))
        if record.finished_at is not None:
            body["finished_at"] = record.finished_at
        if record.error is not None:
            body["error"] = record.error
        if record.summary is not None:
            body["result"] = {
                "text": record.summary.text,
                "turns": record.summary.turns,
                "tool_calls": record.summary.tool_calls,
                "denied": [
                    {"tool": tool, "detail": detail}
                    for tool, detail in record.summary.denied
                ],
            }
        if record.hermes is not None:
            body["hermes_result"] = {
                "text": record.hermes.text,
                "model": record.hermes.model,
                "session_id": record.hermes.session_id,
                "ok": record.hermes.ok,
            }
        return body

    # -- shutdown --------------------------------------------------------

    def shutdown(self) -> None:
        """Release any pending approval and leave the run to finish or not.

        Called on server shutdown. Refuses rather than approves a prompt nobody
        answered, and returns immediately rather than joining the worker -- a
        worker inside a model call cannot be interrupted, and blocking process
        exit on it would make Ctrl-C hang.
        """
        self.channel.release_all()


def _elapsed_seconds(started: str, finished: str | None) -> int:
    """Return elapsed wall time from the run's UTC timestamps."""
    start = datetime.fromisoformat(started).timestamp()
    end = datetime.fromisoformat(finished).timestamp() if finished else time.time()
    return max(0, int(end - start))


def _now_iso() -> str:
    """A UTC timestamp, seconds precision.

    ``datetime`` rather than ``time`` because this is rendered to a person
    reading a status panel, and a bare epoch number invites them to do
    arithmetic to understand it.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def workspace_of(root: Path) -> Workspace:
    """Build a :class:`Workspace` for ``root``.

    A named helper so the CLI and the tests resolve a workspace the same way,
    and so a bad root fails in one place with one message.
    """
    return Workspace(root)