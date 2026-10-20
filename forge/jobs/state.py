"""Forge job contract: states, permissions, budget, and claim ownership.

Phase A of the Forge 2.0 roadmap (GitHub issue #3). This module defines the
*shape* of a Jira -> Forge -> Byte job and the deterministic transitions between
its states.

It is a pure data model:

*   no subprocess, no network, no credentials, no persistence, no execution --
    so "unattended execution must not be enabled here" is true by construction;
*   no clock and no timers -- so nothing here silently expires or completes a
    job, which is the property that keeps a crash from becoming a silent
    ``done``.

Every guarantee that *depends* on durable, atomic, cross-process storage is
deliberately **not claimed here**. Those are named as interfaces
(:class:`JobStore`, :class:`BudgetLedger`, and the idempotency
:class:`~forge.jobs.idempotency.DedupeStore`) and deferred to a later
requirement. The value does what a value can: it refuses a second claim of an
already-owned job and refuses a budget-less transition. It cannot make two
processes race-proof, and it says so rather than pretending.

The design follows the invariants already settled in the codebase, so the job
layer does not introduce a second, weaker copy of them:

*   **Narrowing-only permissions.** ``allowed_tools`` must be a subset of the
    Forge-wide baseline. There is no field that widens it, and ``new_job``
    refuses an out-of-baseline tool at construction -- the same structural
    guarantee ``AgentProfile.allows`` provides for agents.
*   **Narrowing-only budget.** A per-job budget is a ceiling; it may not exceed
    the global monthly ceiling, enforced at construction.
*   **Fail closed on budget.** A missing or exhausted budget refuses the
    transitions that lead to paid execution (``claim`` and ``start``) as a
    :class:`BudgetRefused`, distinct from an invalid-state refusal.
*   **Single-consumer claim.** ``claim`` sets an owner; ``start`` and
    ``release`` require that owner; a second ``claim`` of an owned job raises
    :class:`ClaimConflict`.
*   **Explicit terminal states.** ``done`` and ``failed`` are reached only by an
    explicit ``complete`` or ``fail`` event. There is no default that marks a
    job done.
*   **Operator-controlled repo allowlist.** ``new_job`` accepts an
    ``allowed_repos`` sequence supplied by the Forge layer's operator
    configuration. The allowlist is never read from the ticket; the ticket can
    name a repo, Forge decides whether it is allowed.

The Jira payload becomes a job only through :func:`new_job`, which is the single
validation boundary. A ticket cannot declare its own permissions, its own
budget, or its own allowlist: those are Forge inputs, and the model is shaped so
they cannot arrive any other way.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum

__all__ = [
    "Job",
    "JobState",
    "JobEvent",
    "TERMINAL_STATES",
    "BASELINE_TOOLS",
    "BudgetCheck",
    "BudgetLedger",
    "JobStore",
    "JobTransitionError",
    "InvalidTransition",
    "BudgetRefused",
    "ClaimConflict",
    "new_job",
    "transition",
]


# --------------------------------------------------------------------------
# States and events
# --------------------------------------------------------------------------


class JobState(str, Enum):
    """The states of one job, in lifecycle order.

    ``done`` and ``failed`` are terminal. ``blocked`` and ``review`` are the two
    states where external input (a missing prerequisite, or a human review) is
    awaited. Both are recoverable -- neither implies the job is over.
    """

    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"


#: States from which no event can move a job. A job in one of these is finished.
TERMINAL_STATES: tuple[JobState, ...] = (JobState.DONE, JobState.FAILED)


class JobEvent(str, Enum):
    """The events the job accepts. Each transition is an explicit event.

    ``complete`` and ``fail`` are the only routes into a terminal state.
    ``release`` exists as the recovery path for a claim that was taken but never
    started; it returns the job to ``ready`` and is gated on the owner matching.
    """

    CLAIM = "claim"
    START = "start"
    RELEASE = "release"
    BLOCK = "block"
    UNBLOCK = "unblock"
    SUBMIT = "submit"
    REVISE = "revise"
    COMPLETE = "complete"
    FAIL = "fail"


#: The Forge-wide tool baseline a job's ``allowed_tools`` narrows. Kept equal to
#: ``forge.agents.byte.BYTE_TOOLS``; ``tests/test_job_state.py`` asserts the two
#: do not drift, because the job layer must not build on a different baseline
#: than the agent that executes the jobs.
BASELINE_TOOLS: tuple[str, ...] = (
    "read_file",
    "search_files",
    "search_content",
    "write_file",
    "patch_file",
    "exec_command",
)


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheck:
    """A pre-computed answer to "may this job spend, right now?".

    Produced by the Forge layer from a :class:`BudgetLedger`, then handed to
    :func:`transition` for the ``claim`` and ``start`` events. Keeping the check
    a value rather than a call keeps the state machine pure (no IO) while giving
    the Forge layer a clear contract: compute it, pass it, and an absent or
    failing check refuses the transition.

    Attributes:
        per_job_allowed: The job's own remaining budget is positive.
        global_allowed: The Forge-wide monthly budget still has capacity.
        detail: Operator-safe reason, when a limb is false.
    """

    per_job_allowed: bool
    global_allowed: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.per_job_allowed and self.global_allowed

    @classmethod
    def approved(cls) -> "BudgetCheck":
        return cls(True, True)

    @classmethod
    def denied(cls, detail: str) -> "BudgetCheck":
        return cls(False, False, detail)


class BudgetLedger:
    """Interface for the durable budget ledger, wired in a later phase.

    The pure module cannot sum spend across processes, so it does not try. The
    Forge layer implements this interface against durable storage and turns its
    result into a :class:`BudgetCheck`. Until that exists, ``claim`` and
    ``start`` fail closed (no ledger -> no authorization -> :class:`BudgetRefused`).

    The actual accounting of cents should use a fixed-point/decimal representation;
    this contract carries ceilings as floats only because the *enforcement* lives
    behind the interface, and a ceiling comparison is all the model needs.
    """

    def check(self, job_id: str) -> BudgetCheck:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """One job, an immutable value.

    Attributes:
        id: Opaque unique identifier, minted by the Forge intake layer.
        ticket_ref: The originating Jira key. Opaque to this module; it is
            carried for audit and for reporting back, never interpreted.
        repo: The single repository/workspace the job targets. Validated against
            the operator-controlled allowlist in :func:`new_job`, never trusted
            because a ticket named it.
        state: Current :class:`JobState`.
        allowed_tools: Tools the job may use, a subset of the baseline. Enforced
            at construction; no field can widen it.
        budget: Per-job spending ceiling, or ``None`` for unset. ``None`` means
            the job cannot pass its budget guard, so it cannot execute.
        owner: The claim holder, or ``None`` before a successful ``claim``.
            Sets the identity that ``start`` and ``release`` must match.
    """

    id: str
    ticket_ref: str
    repo: str
    state: JobState = JobState.READY
    allowed_tools: tuple[str, ...] = ()
    budget: float | None = None
    owner: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def allows(self, tool: str) -> bool:
        """Whether ``tool`` is within this job's (narrowed) permissions."""
        return tool in self.allowed_tools


class JobStore:
    """Interface for durable, atomic job persistence, wired in a later phase.

    :func:`transition` is the referentially-transparent core. The store is what
    makes it safe across processes: a ``claim`` must be a compare-and-swap, and
    every transition must be transactional, so a crash between reading a job and
    writing its successor cannot produce two workers holding the same job.

    **Exactly-once execution is not claimed by this module.** Until a store
    exists whose ``claim`` is atomic and durable, two processes can, in
    principle, both read a ``ready`` job and both attempt the value-level claim
    -- the second then fails the value check, but there is no cross-process
    serialisation to make the first unique. The interface below is the contract
    that later phase must satisfy; it is deliberately not implemented here.
    """

    def load(self, job_id: str) -> Job | None:  # pragma: no cover - interface
        raise NotImplementedError

    def claim(self, job_id: str, owner: str) -> Job:  # pragma: no cover - interface
        """Atomically claim ``job_id`` for ``owner`` or raise on conflict."""
        raise NotImplementedError

    def transition(  # pragma: no cover - interface
        self, job_id: str, event: JobEvent, **ctx: object
    ) -> Job:
        """Apply one event to the stored job within a transaction."""
        raise NotImplementedError

    def save(self, job: Job) -> Job:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class JobTransitionError(Exception):
    """Base for every refusal the state machine can produce.

    A refused transition raises rather than returning a quietly-unchanged job:
    a caller that asked for something impossible should not be handed back the
    same job and left to notice nothing happened.
    """


class InvalidTransition(JobTransitionError):
    """The requested ``(state, event)`` is not a legal move.

    Covers both terminal-state moves and events that are wrong for the current
    state. The message names both, so the refusal is explainable.
    """


class BudgetRefused(JobTransitionError):
    """The transition would begin paid execution but budget failed closed."""


class ClaimConflict(JobTransitionError):
    """A claim is already held, or the supplied owner does not match it."""


# --------------------------------------------------------------------------
# Construction and validation
# --------------------------------------------------------------------------


def _require_identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain a NUL byte")


def new_job(
    *,
    job_id: str,
    ticket_ref: str,
    repo: str,
    allowed_tools: Iterable[str] = (),
    budget: float | None = None,
    baseline_tools: Iterable[str] = BASELINE_TOOLS,
    allowed_repos: Iterable[str] | None = None,
    global_budget: float | None = None,
) -> Job:
    """Construct a validated :class:`Job`.

    This is the single intake boundary. It enforces, at construction and
    therefore before any transition:

    *   non-empty, NUL-free identifiers (``job_id``, ``ticket_ref``, ``repo``);
    *   ``allowed_tools`` is a subset of ``baseline_tools`` -- narrowing only, no
        widening field exists;
    *   ``budget`` is ``None`` or a positive finite ceiling;
    *   ``global_budget``, when given, is a positive finite ceiling the per-job
        budget may not exceed -- the budget analogue of narrowing-only;
    *   ``allowed_repos``, when given, must contain ``repo``.

    ``allowed_repos`` and ``baseline_tools`` are supplied by the Forge layer.
    They are **never** derived from the ticket payload: the ticket may name a
    repo, but only Forge's operator configuration decides whether it is allowed.
    ``baseline_tools`` defaults to :data:`BASELINE_TOOLS`; a caller may pass a
    *narrower* baseline for a stricter context, but nothing here accepts a
    wider one above the default.

    Returns:
        A frozen :class:`Job` in :attr:`JobState.READY`, with no owner.

    Raises:
        ValueError: Any validation above is violated.
    """
    _require_identifier(job_id, "job_id")
    _require_identifier(ticket_ref, "ticket_ref")
    _require_identifier(repo, "repo")

    complete_baseline = set(baseline_tools)
    tools = tuple(sorted(set(allowed_tools)))
    for tool in tools:
        if tool not in complete_baseline:
            raise ValueError(
                f"allowed_tools contains {tool!r}, which is outside the baseline; "
                "a job may only narrow the Forge-wide baseline, never widen it"
            )

    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            raise ValueError(f"budget must be a number or None, got {budget!r}")
        if not math.isfinite(float(budget)) or float(budget) <= 0:
            raise ValueError(f"budget must be a positive finite number, got {budget!r}")
        budget = float(budget)

    if global_budget is not None:
        if (
            isinstance(global_budget, bool)
            or not isinstance(global_budget, (int, float))
            or not math.isfinite(float(global_budget))
            or float(global_budget) <= 0
        ):
            raise ValueError(
                f"global_budget must be a positive finite number or None, "
                f"got {global_budget!r}"
            )
        if budget is not None and budget > float(global_budget):
            raise ValueError(
                f"per-job budget {budget} exceeds the global ceiling "
                f"{global_budget}; a job budget may only narrow the global budget"
            )

    if allowed_repos is not None:
        allowed = set(allowed_repos)
        if repo not in allowed:
            raise ValueError(
                f"repo {repo!r} is not in the operator-controlled allowlist; "
                "repos are gated by Forge configuration, never by the ticket"
            )

    return Job(
        id=job_id,
        ticket_ref=ticket_ref,
        repo=repo,
        state=JobState.READY,
        allowed_tools=tools,
        budget=budget,
        owner=None,
    )


# --------------------------------------------------------------------------
# Transition logic
# --------------------------------------------------------------------------


def _invalid(state: JobState, event: JobEvent) -> InvalidTransition:
    return InvalidTransition(
        f"illegal transition: {event.value!r} is not valid from state "
        f"{state.value!r}"
    )


def _require_owner_present(owner: str | None) -> None:
    if owner is None or not owner.strip():
        raise ClaimConflict("a non-empty owner is required for this transition")


def _require_owner_match(owner: str | None, expected: str | None) -> None:
    _require_owner_present(owner)
    if owner != expected:
        raise ClaimConflict(
            f"owner {owner!r} does not match the job's claim owner {expected!r}"
        )


def _require_budget(job: Job, budget_check: BudgetCheck | None) -> None:
    """Fail closed before any transition that could begin paid execution.

    Three independent grounds, all refusing:

    *   the job has no positive per-job ceiling (a missing budget is not a
        blank cheque);
    *   no :class:`BudgetCheck` was supplied (no ledger -> no authorization);
    *   the supplied check is not fully allowed (per-job or global exhausted).
    """
    if job.budget is None or job.budget <= 0:
        raise BudgetRefused(
            f"job {job.id!r} has no positive per-job budget ceiling; "
            "refusing to begin paid execution"
        )
    if budget_check is None:
        raise BudgetRefused(
            f"job {job.id!r} was not accompanied by a budget authorization; "
            "refusing to begin paid execution without one"
        )
    if not budget_check.ok:
        raise BudgetRefused(
            budget_check.detail or "budget check denied the transition"
        )


def transition(
    job: Job,
    event: JobEvent,
    *,
    owner: str | None = None,
    budget_check: BudgetCheck | None = None,
) -> Job:
    """Apply ``event`` to ``job``, returning the successor :class:`Job`.

    Deterministic and total: every ``(state, event)`` pair either returns a
    successor or raises a specific :class:`JobTransitionError`. There is no
    silent no-op -- a refused move is a named exception, never an unchanged job
    handed back as if nothing happened.

    Guards:

    *   ``claim`` sets ``owner`` (required, non-empty) and checks budget.
    *   ``start`` and ``release`` require ``owner`` to equal the claim holder.
    *   ``claim`` and ``start`` both check budget -- fail closed *before* paid
        execution begins, and a job may be re-checked at ``start`` even though
        it passed at ``claim``.
    *   ``complete`` and ``fail`` are the only routes into terminal states; a
        job is never marked done by a default.

    Args:
        job: The current job.
        event: The event to apply.
        owner: The actor's identity. Required and stored on ``claim``; must
            match ``job.owner`` on ``start`` and ``release``; ignored elsewhere.
        budget_check: Pre-computed budget authorization, required on ``claim``
            and ``start``.
    """
    state = job.state

    if state in TERMINAL_STATES:
        raise InvalidTransition(
            f"job is terminal ({state.value!r}); no event may move it"
        )

    if event is JobEvent.CLAIM:
        if state is not JobState.READY:
            raise _invalid(state, event)
        _require_owner_present(owner)
        _require_budget(job, budget_check)
        return replace(job, state=JobState.CLAIMED, owner=owner)

    if event is JobEvent.START:
        if state is not JobState.CLAIMED:
            raise _invalid(state, event)
        _require_owner_match(owner, job.owner)
        _require_budget(job, budget_check)
        return replace(job, state=JobState.RUNNING)

    if event is JobEvent.RELEASE:
        if state is not JobState.CLAIMED:
            raise _invalid(state, event)
        _require_owner_match(owner, job.owner)
        return replace(job, state=JobState.READY, owner=None)

    if event is JobEvent.BLOCK:
        if state is not JobState.RUNNING:
            raise _invalid(state, event)
        return replace(job, state=JobState.BLOCKED)

    if event is JobEvent.UNBLOCK:
        if state is not JobState.BLOCKED:
            raise _invalid(state, event)
        return replace(job, state=JobState.RUNNING)

    if event is JobEvent.SUBMIT:
        if state is not JobState.RUNNING:
            raise _invalid(state, event)
        return replace(job, state=JobState.REVIEW)

    if event is JobEvent.REVISE:
        if state is not JobState.REVIEW:
            raise _invalid(state, event)
        return replace(job, state=JobState.RUNNING)

    if event is JobEvent.COMPLETE:
        if state not in (JobState.RUNNING, JobState.REVIEW):
            raise _invalid(state, event)
        return replace(job, state=JobState.DONE)

    if event is JobEvent.FAIL:
        # Explicitly from any non-terminal state: a pre-flight rejection of an
        # unclaimed job is as legitimate a failure as a worker crash, and both
        # are recorded, never defaulted.
        if state not in (
            JobState.READY,
            JobState.CLAIMED,
            JobState.RUNNING,
            JobState.BLOCKED,
            JobState.REVIEW,
        ):
            raise _invalid(state, event)
        return replace(job, state=JobState.FAILED)

    raise _invalid(state, event)