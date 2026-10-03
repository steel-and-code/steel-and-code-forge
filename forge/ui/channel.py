"""The approval bridge: a browser answering a question the runtime asked.

:class:`WebApprovalChannel` is an ordinary
:class:`~forge.tools.approval.ApprovalChannel`. ``present()`` is synchronous,
returns a plain response string, and knows nothing about classifiers,
executors, or what the operation will do. Every policy decision stays in
``forge/tools/approval.py``, and none of it was changed to make a UI possible.

The awkward part of a browser approval is timing, and it is worth being precise
about where the wait happens:

*   The runtime runs on a **worker thread**, started by
    :class:`~forge.ui.session.WorkbenchSession`.
*   ``present()`` is called *on that thread*, from inside ``Approver.resolve``,
    and blocks it on a :class:`threading.Event`.
*   The **ASGI event loop is never blocked**. It keeps serving ``GET
    /api/status``, the SSE stream, and -- the point of the exercise -- ``POST
    /api/approvals/{id}``, which is what wakes the worker.

Making the channel ``async`` would have been the wrong repair. ``ApprovalChannel``
is a synchronous protocol used by a synchronous policy layer, and an async
version would either fork the policy into two implementations or drag the
whole tool pipeline into a coroutine world to satisfy a surface that is one
local browser tab.

The wait is bounded by exactly one thing: the human answering, or the server
shutting down (:meth:`WebApprovalChannel.release_all`). There is no timeout
that turns into an approval and no timeout that invents a decision -- an
unanswered prompt with nobody present is a rejection, which is what
``parse_response(None)`` already means everywhere else in Forge.

Four rules hold, and each is enforced rather than documented:

*   **One pending approval per channel.** The dispatcher dispatches
    sequentially, so two concurrent prompts would mean two concurrent
    operations. The second ``present()`` returns ``None`` -- a refusal --
    rather than replacing the first, because silently swapping the question an
    operator is looking at is the worst possible outcome here.
*   **First valid response wins.** ``respond()`` claims the pending record
    under a lock, so of two tabs racing, exactly one gets through and the other
    is told it is stale.
*   **An approval id is single-use and unguessable.** ``secrets.token_urlsafe``
    and claimed-once. The id is the whole authorisation for "the person at this
    browser saw this prompt", so it is treated as a capability rather than as
    an identifier.
*   **The channel returns only yes or no.** ``present()`` hands back ``"y"`` or
    ``"n"`` and nothing else, so ``parse_response`` -- unchanged -- is the only
    thing that decides.

Note what this class is not: it is not a way to grant a permission. It carries
an answer a human gave, and the single-spend authority in
``ApprovalOutcome.claim()`` remains the only thing that authorises an
operation. A browser can say "yes"; only Forge decides that yes means allowed.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Callable

from ..tools.approval import ApprovalRequest, render_prompt

__all__ = [
    "WebApprovalChannel",
    "PendingApproval",
    "UnknownApproval",
    "StaleApproval",
    "APPROVAL_ID_BYTES",
    "AFFIRMATIVE",
    "NEGATIVE",
]

#: Entropy in an approval id. The id is a bearer capability for "the operator
#: saw this prompt", so it is cryptographically random rather than a counter:
#: a guessable id would let anything on the machine answer a pending prompt.
APPROVAL_ID_BYTES = 32

#: The only two strings ``present()`` ever returns. Both are accepted by
#: ``parse_response``; ``"n"`` and ``None`` are both rejections, and the
#: distinction is preserved in the outcome's ``operator_response`` for whoever
#: audits it.
AFFIRMATIVE = "y"
NEGATIVE = "n"


class UnknownApproval(Exception):
    """No pending approval has that id.

    Either the id was never issued, or it belonged to an approval that has since
    been settled and retired. A caller cannot tell which, and does not need to:
    either way there is nothing here to answer.
    """


class StaleApproval(Exception):
    """That approval was already answered.

    The human decision is made. This is what a second tab receives after the
    first one answered, and it is deliberately a conflict rather than a success
    -- a second tab that reads 200 would believe its click settled something,
    which is exactly the two-tabs-one-approval ambiguity the single-use
    capability exists to prevent.
    """


@dataclass
class PendingApproval:
    """One prompt, waiting for an answer.

    Mutable and shared rather than a value passed between threads: the worker
    writes it under the channel's lock and the request handler reads it under
    the same lock. ``answered`` and ``event`` are the two pieces of state the
    answer races on.

    Attributes:
        approval_id: The capability a browser presents.
        request: The request as shown. Its ``to_public_dict`` is what the page
            renders -- the browser never receives the object.
        prompt: The rendered text, kept for the event payload.
        event: What the worker thread waits on.
        answered: Whether the answer has been recorded.
        approved: The decision. ``False`` until someone answers, and ``False``
            again on release, so the default is a refusal.
    """

    approval_id: str
    request: dict[str, object]
    prompt: str
    event: threading.Event = field(default_factory=threading.Event)
    answered: bool = False
    approved: bool = False

    def settle(self, approved: bool) -> None:
        """Record the decision and wake the worker.

        Called once per record. Setting the flag before signalling is what makes
        the wake safe: a worker that wakes on the event always reads a decision
        that is already there.
        """
        self.answered = True
        self.approved = approved
        self.event.set()

    def to_public_dict(self) -> dict[str, object]:
        """What the page may see.

        Only the request's own public projection. Never the channel, never the
        runtime, never an executor. The decision itself is deliberately absent
        -- a status response must not be a way to read how a pending prompt
        would resolve.
        """
        return {"approval_id": self.approval_id, "request": self.request}


class WebApprovalChannel:
    """An approval channel backed by whoever is watching the browser.

    Args:
        emit: Observational sink, so the page learns an approval is pending.
            Optional; without one the channel still works and the page can
            discover the pending approval from ``GET /api/status``.
        on_state_change: Called with ``True`` when a prompt becomes pending and
            ``False`` when it is settled or retired. This is how the session
            moves between ``RUNNING`` and ``WAITING_FOR_APPROVAL`` without the
            channel importing the session -- the dependency points the other
            way, which is what keeps this class testable on its own.

    Threads: ``present`` is called on the worker thread; ``respond`` and
    ``release_all`` are called from request handlers on the event loop. All
    shared state is under ``_lock``, and the only blocking wait is
    ``PendingApproval.event.wait()``, which blocks the worker and nothing else.
    """

    def __init__(
        self,
        *,
        emit: Callable[..., None] | None = None,
        on_state_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._emit = emit
        self._on_state_change = on_state_change
        self._lock = threading.Lock()
        self._pending: PendingApproval | None = None
        self._retired: set[str] = set()
        self._answered_count = 0

    # -- ApprovalChannel -------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether a human can be asked.

        ``True`` unconditionally: the Workbench surface exists because a person
        is present, and a browser that has not connected yet is a page that will
        connect. This is the same claim :class:`TerminalApprovalChannel` makes
        about a TTY, and the answer if the human never arrives is a rejection,
        not a hang.

        One honest consequence: a Workbench process left running with no browser
        open will prompt into the void, and the worker's ``present()`` will sit
        there. That is a process nobody is watching; ``release_all`` on shutdown
        is what stops it.
        """
        return True

    def present(self, request: ApprovalRequest, prompt: str) -> str | None:
        """Show the prompt, block this thread, return the answer.

        Returns ``"y"`` or ``"n"``. ``None`` -- a refusal -- is returned only if
        a second prompt arrives while one is already pending, or if the server
        shut down under the wait. Never raises: ``Approver.resolve`` treats a
        non-approval as a rejection and carries on.
        """
        approval_id = secrets.token_urlsafe(APPROVAL_ID_BYTES)

        with self._lock:
            if self._pending is not None:
                # Two prompts at once. The dispatcher is sequential so this is a
                # guard rather than a path, and the safe direction is refusal:
                # the operator is looking at the first prompt, and replacing it
                # would mean they approve something they did not read.
                return None
            pending = PendingApproval(
                approval_id=approval_id,
                request=request.to_public_dict(),
                prompt=prompt,
            )
            self._pending = pending

        self._notify(True)
        if self._emit is not None:
            self._emit(
                "approval.required",
                approval_id=approval_id,
                agent=request.agent,
                tool=request.tool,
                rule=request.rule,
                classification=request.classification.value,
                operation=request.operation,
                detail=request.detail,
            )

        # The blocking wait. On this worker thread, never on the event loop.
        pending.event.wait()

        with self._lock:
            answer = AFFIRMATIVE if pending.approved else NEGATIVE
            self._pending = None
            self._answered_count += 1

        self._notify(False)
        return answer

    # -- answering -------------------------------------------------------

    def respond(self, approval_id: str, approved: bool) -> None:
        """Record a human decision and wake the worker.

        Raises:
            UnknownApproval: no pending approval has that id.
            StaleApproval: it was already answered. This is the second tab.
        """
        with self._lock:
            pending = self._pending
            if pending is None or pending.approval_id != approval_id:
                if approval_id in self._retired:
                    raise StaleApproval(
                        "this approval was already answered; approval is "
                        "per-operation and is single-use"
                    )
                raise UnknownApproval("no pending approval has that id")

            if pending.answered:
                # Unreachable via respond() under the lock, kept so the
                # single-use property is enforced at the record as well as at
                # the registry.
                raise StaleApproval("this approval was already answered")

            pending.settle(approved)
            self._retired.add(approval_id)
            # Keep the retired set bounded. It exists only so a duplicate POST
            # gets "stale" rather than "unknown"; past a few hundred entries
            # that distinction is not worth unbounded memory.
            if len(self._retired) > 512:
                self._retired = set(list(self._retired)[-256:])

        if self._emit is not None:
            self._emit(
                "approval.answered",
                approval_id=approval_id,
                decision="approved" if approved else "rejected",
            )

    def release_all(self) -> None:
        """Settle every pending approval as a refusal and wake the worker.

        Called on shutdown. Without it, killing the process would leave a worker
        thread blocked forever on an event nobody will ever set -- the process
        would appear to hang on exit. Refusing is also the correct answer: the
        server going away is not a human saying yes.
        """
        with self._lock:
            pending = self._pending
            if pending is None:
                return
            self._pending = None
            pending.settle(False)
        self._notify(False)

    # -- observation -----------------------------------------------------

    @property
    def pending(self) -> PendingApproval | None:
        """The prompt awaiting an answer, or ``None``.

        Read under the lock so a status request during a refresh cannot observe
        a half-built record.
        """
        with self._lock:
            return self._pending

    def pending_public(self) -> dict[str, object] | None:
        """The pending approval as the page may see it, or ``None``.

        This is what makes a refresh work: the browser tab holds no approval
        state, so after a reload it asks the server what is outstanding and gets
        the same approval id it had before. The id is unchanged and still
        single-use, so the operator's decision after a refresh lands on the
        prompt they were actually looking at.
        """
        with self._lock:
            if self._pending is None:
                return None
            return self._pending.to_public_dict()

    def _notify(self, pending_now: bool) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(pending_now)
        except Exception:  # noqa: BLE001 - observation must not break approval
            return


def render_public_prompt(request: ApprovalRequest) -> str:
    """The prompt as text, for a consumer that wants the rendered form.

    Exposed so the event payload can carry exactly what the terminal shows. The
    page renders fields from the structured request rather than parsing this,
    because parsing a human-facing layout to recover data is how a UI breaks
    silently when someone rewords a prompt.
    """
    return render_prompt(request)