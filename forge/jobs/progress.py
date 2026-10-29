"""FORGE-14 pure contract: checkpoints, external-operation classification,
bounded retry, and recovery decisions.

This module is to :mod:`forge.jobs.checkpoint_store` what
:mod:`forge.jobs.state` is to :mod:`forge.jobs.store`: the pure, referentially
transparent value layer that a durable store loads, calls, and writes back. It
is stdlib-only, carries no IO, no clock, no persistence, and no execution, so
"no unattended network/credential/time behaviour lives here" is true by
construction.

Three concerns, each a pure function or frozen value:

*   **Checkpoints** (:class:`Checkpoint`) are the step-level progress trace --
    where a job got to. They are append-only, ordered by ``seq``, and carry a
    ``generation`` (the lease-fence lineage that wrote them) for audit, not for
    validity.

*   **External operations** (:class:`ExternalOpRecord`) are the authoritative,
    durable record of one external side effect. A row is written with state
    ``PENDING`` *before* the call is dispatched and only then advanced --
    ``COMMITTED`` (with evidence), ``UNCERTAIN``, ``FAILED``, or ``SKIPPED`` by
    an operator. This is what makes crash recovery possible: a crash between
    "before" and "after" always leaves a ``PENDING`` record, and ``PENDING`` is
    the signal that the outcome is unresolved.

*   **Retry and recovery decisions** (:func:`classify_recovery`) map a failure
    to exactly one of ``RETRY`` / ``FAIL`` / ``ESCALATE`` against a bounded,
    deterministic backoff. A side effect whose outcome is uncertain is *never*
    retried automatically -- it escalates to an operator.

The safety invariant that governs everything here: **never automatically retry
an uncertain non-idempotent external operation.** Every external op defaults to
:class:`SideEffectClass.NON_IDEMPOTENT`; :class:`~SideEffectClass.IDEMPOTENT_GUARDED`
is only asserted by a caller that can demonstrate verified replay protection.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

__all__ = [
    "CheckpointKind",
    "SideEffectClass",
    "FailureClass",
    "RecoveryDecision",
    "OpState",
    "OpAction",
    "ResolutionKind",
    "ReconcileVerdict",
    "RetryPolicy",
    "DEFAULT_RETRY_POLICY",
    "RetryResult",
    "Checkpoint",
    "ExternalOpRecord",
    "ResumePlan",
    "DEFAULT_SIDE_EFFECT_CLASS",
    "OpTransitionError",
    "InvalidOpTransition",
    "OpEvidenceRequired",
    "backoff_delay",
    "classify_recovery",
    "side_effect_class_for",
    "advance_op_state",
    "compute_resume_plan",
    "op_key",
]


#: Field separator that cannot appear in a well-formed job id, step id, or
#: operation kind (matches the discipline in ``idempotency.py``).
_SEP = "\x1f"


# --------------------------------------------------------------------------
# Kinds and classes
# --------------------------------------------------------------------------


class CheckpointKind(str, Enum):
    """Where a checkpoint sits relative to the work it brackets.

    ``STEP_DONE`` marks a completed pure/deterministic step -- a safe resume
    marker. ``SIDE_EFFECT_BEFORE`` / ``SIDE_EFFECT_AFTER`` bracket an external
    operation. ``PHASE`` is a coarse milestone.
    """

    STEP_DONE = "step_done"
    SIDE_EFFECT_BEFORE = "side_effect_before"
    SIDE_EFFECT_AFTER = "side_effect_after"
    PHASE = "phase"


class SideEffectClass(str, Enum):
    """How safe it is to re-run a unit of work.

    ``DETERMINISTIC`` is pure computation (no external effect). An external
    operation is either ``IDEMPOTENT_GUARDED`` (re-running is safe because an
    idempotency key reconciles to the same outcome) or ``NON_IDEMPOTENT``
    (re-running may duplicate the effect). Every external op defaults to
    ``NON_IDEMPOTENT``; ``IDEMPOTENT_GUARDED`` is only asserted by a caller
    that can demonstrate the protection.
    """

    DETERMINISTIC = "deterministic"
    IDEMPOTENT_GUARDED = "idempotent_guarded"
    NON_IDEMPOTENT = "non_idempotent"


class FailureClass(str, Enum):
    """The class of a failure, which decides retry vs escalate vs fail."""

    TRANSIENT = "transient"
    DETERMINISTIC_REFUSAL = "deterministic_refusal"
    SIDE_EFFECT_UNCERTAIN = "side_effect_uncertain"
    EXTERNAL_ERROR = "external_error"


class RecoveryDecision(str, Enum):
    """The forward path after a failure.

    ``RETRY`` waits (persisted backoff) and re-runs. ``FAIL`` is terminal.
    ``ESCALATE`` hands the decision to an operator -- the only path for an
    uncertain non-idempotent side effect, which is never retried automatically.
    """

    RETRY = "retry"
    FAIL = "fail"
    ESCALATE = "escalate"


class OpState(str, Enum):
    """The lifecycle of one external-operation record.

    ``PENDING`` is written before dispatch. ``COMMITTED`` requires evidence of
    the outcome. ``UNCERTAIN`` means the outcome cannot be confirmed -- it must
    be resolved by an operator. ``FAILED`` and ``SKIPPED`` are absorbing.
    """

    PENDING = "pending"
    COMMITTED = "committed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    SKIPPED = "skipped"


class OpAction(str, Enum):
    """A requested advance of an external-operation record.

    ``COMMIT`` / ``MARK_UNCERTAIN`` / ``MARK_FAILED`` are worker actions driven
    by execution. ``RESOLVE_REPLAY`` / ``RESOLVE_SKIP`` / ``RESOLVE_FAIL`` are
    operator resolutions, legal only from ``UNCERTAIN``.
    """

    COMMIT = "commit"
    MARK_UNCERTAIN = "mark_uncertain"
    MARK_FAILED = "mark_failed"
    RESOLVE_REPLAY = "resolve_replay"
    RESOLVE_SKIP = "resolve_skip"
    RESOLVE_FAIL = "resolve_fail"


class ResolutionKind(str, Enum):
    """The operator's decision on an uncertain operation (audit shape)."""

    REPLAY = "replay"
    SKIP = "skip"
    FAIL = "fail"


class ReconcileVerdict(str, Enum):
    """A reconciliation result, produced by a later effector (FORGE-16).

    ``ABSENT`` does **not** mean "never happened" -- for a non-idempotent op it
    is treated as unresolved for safety. Only ``CONFIRMED`` (found, matches
    evidence) or an explicit operator ``REPLAY`` moves an uncertain op forward.
    """

    CONFIRMED = "confirmed"
    ABSENT = "absent"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Retry policy and results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """Frozen retry/backoff parameters (no timers, no jitter, no deadline).

    ``max_attempts`` is a lifetime ceiling: it bounds attempts across the whole
    job life and is never replenished. ``initial_delay_sec`` /
    ``backoff_factor`` / ``max_delay_sec`` shape the delay before the next
    attempt. The delay is deterministic -- repeated calls are identical.
    """

    max_attempts: int = 3
    initial_delay_sec: float = 30.0
    backoff_factor: float = 2.0
    max_delay_sec: float = 300.0

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an integer")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        for name in ("initial_delay_sec", "backoff_factor", "max_delay_sec"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.initial_delay_sec < 0:
            raise ValueError("initial_delay_sec must be non-negative")
        if self.backoff_factor < 1.0:
            raise ValueError("backoff_factor must be at least 1.0")
        if self.max_delay_sec < self.initial_delay_sec:
            raise ValueError("max_delay_sec must be at least initial_delay_sec")


#: The Forge-wide default retry policy: at most three lifetime attempts, with a
#: deterministic 30/60/120/240/300s backoff.
DEFAULT_RETRY_POLICY: RetryPolicy = RetryPolicy()


@dataclass(frozen=True)
class RetryResult:
    """The outcome of :func:`classify_recovery`.

    ``next_delay`` is non-``None`` exactly when ``decision`` is ``RETRY``.
    ``reason`` is non-``None`` exactly when ``decision`` is ``FAIL`` or
    ``ESCALATE``.
    """

    decision: RecoveryDecision
    next_delay: float | None = None
    reason: str | None = None


def backoff_delay(policy: RetryPolicy, attempt: int) -> float:
    """The delay (seconds) before the ``attempt``-th execution attempt.

    Deterministic and zero-jitter. ``attempt`` is 1-indexed; the delay grows
    geometrically and is capped at ``max_delay_sec``.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("attempt must be an integer")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    delay = policy.initial_delay_sec * (policy.backoff_factor ** (attempt - 1))
    return min(policy.max_delay_sec, delay)


def classify_recovery(
    failure_class: FailureClass,
    attempts_made: int,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> RetryResult:
    """Map a failure to a retry / fail / escalate decision against the lifetime cap.

    ``attempts_made`` is the 1-indexed count of execution attempts so far, the
    latest of which just failed. Rules:

    * ``DETERMINISTIC_REFUSAL`` -> ``FAIL`` always (retrying a refusal cannot help).
    * ``SIDE_EFFECT_UNCERTAIN`` -> ``ESCALATE`` always (never auto-retry).
    * ``TRANSIENT`` / ``EXTERNAL_ERROR`` -> ``RETRY`` while the lifetime cap has
      room, else ``FAIL`` (attempt budget exhausted).
    """
    if not isinstance(failure_class, FailureClass):
        raise TypeError("failure_class must be a FailureClass")
    if isinstance(attempts_made, bool) or not isinstance(attempts_made, int):
        raise ValueError("attempts_made must be an integer")
    if attempts_made < 1:
        raise ValueError("attempts_made must be at least 1")

    if failure_class is FailureClass.DETERMINISTIC_REFUSAL:
        return RetryResult(RecoveryDecision.FAIL, reason="deterministic refusal")
    if failure_class is FailureClass.SIDE_EFFECT_UNCERTAIN:
        return RetryResult(
            RecoveryDecision.ESCALATE,
            reason="side effect uncertain; operator resolution required",
        )
    if attempts_made < policy.max_attempts:
        return RetryResult(
            RecoveryDecision.RETRY,
            next_delay=backoff_delay(policy, attempts_made + 1),
        )
    return RetryResult(RecoveryDecision.FAIL, reason="attempt budget exhausted")


# --------------------------------------------------------------------------
# Side-effect classification
# --------------------------------------------------------------------------


#: Authoritative default classification for known external operations. Every
#: entry is ``NON_IDEMPOTENT``; an external op is ``IDEMPOTENT_GUARDED`` only
#: when the caller explicitly overrides it after demonstrating the protection.
#: Unknown operations fall through to ``NON_IDEMPOTENT`` (fail closed).
DEFAULT_SIDE_EFFECT_CLASS: Mapping[str, SideEffectClass] = MappingProxyType(
    {
        "git_push": SideEffectClass.NON_IDEMPOTENT,
        "pr_create": SideEffectClass.NON_IDEMPOTENT,
        "jira_comment": SideEffectClass.NON_IDEMPOTENT,
        "jira_transition": SideEffectClass.NON_IDEMPOTENT,
        "fs_write": SideEffectClass.NON_IDEMPOTENT,
    }
)


def side_effect_class_for(op_kind: str) -> SideEffectClass:
    """The default :class:`SideEffectClass` for ``op_kind``.

    Unknown operations default to ``NON_IDEMPOTENT`` -- the safe (fail-closed)
    classification for anything that reaches outside the process.
    """
    if not isinstance(op_kind, str) or not op_kind.strip():
        raise ValueError("op_kind must be a non-empty string")
    return DEFAULT_SIDE_EFFECT_CLASS.get(op_kind, SideEffectClass.NON_IDEMPOTENT)


# --------------------------------------------------------------------------
# External-operation state machine
# --------------------------------------------------------------------------


class OpTransitionError(Exception):
    """Base for every refusal the external-operation state machine produces."""


class InvalidOpTransition(OpTransitionError):
    """The requested ``(state, action)`` is not a legal move."""


class OpEvidenceRequired(OpTransitionError):
    """``COMMIT`` was requested without a returned external handle."""


#: Legal worker/operator advances per current state. Absorbing states
#: (COMMITTED, FAILED, SKIPPED) admit no action.
_OP_SUCCESSSORS: Mapping[OpState, Mapping[OpAction, OpState]] = MappingProxyType(
    {
        OpState.PENDING: MappingProxyType(
            {
                OpAction.COMMIT: OpState.COMMITTED,
                OpAction.MARK_UNCERTAIN: OpState.UNCERTAIN,
                OpAction.MARK_FAILED: OpState.FAILED,
            }
        ),
        OpState.UNCERTAIN: MappingProxyType(
            {
                OpAction.COMMIT: OpState.COMMITTED,
                OpAction.MARK_FAILED: OpState.FAILED,
                OpAction.RESOLVE_REPLAY: OpState.PENDING,
                OpAction.RESOLVE_SKIP: OpState.SKIPPED,
                OpAction.RESOLVE_FAIL: OpState.FAILED,
            }
        ),
    }
)


def advance_op_state(
    current: OpState, action: OpAction, *, evidence: str | None = None
) -> OpState:
    """Return the successor state for ``action`` from ``current``, or raise.

    Total over the :class:`OpState` x :class:`OpAction` table; a refusal is a
    named :class:`OpTransitionError`, never a silent no-op. ``COMMIT`` requires
    a non-empty ``evidence`` handle.
    """
    if not isinstance(current, OpState):
        raise TypeError("current must be an OpState")
    if not isinstance(action, OpAction):
        raise TypeError("action must be an OpAction")

    table = _OP_SUCCESSSORS.get(current)
    if table is None:
        raise InvalidOpTransition(
            f"state {current.value!r} is terminal; no action may move it"
        )
    successor = table.get(action)
    if successor is None:
        raise InvalidOpTransition(
            f"action {action.value!r} is not valid from state {current.value!r}"
        )
    if action is OpAction.COMMIT and (not isinstance(evidence, str) or not evidence.strip()):
        raise OpEvidenceRequired("COMMIT requires a returned external handle as evidence")
    return successor


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkpoint:
    """One step-level progress record (a value, never persisted here).

    ``evidence`` is a sanitized *reference only* (an artifact path, PR number,
    dedupe key) -- by construction there is no field shaped to hold a transcript
    or a secret. ``generation`` is the lease-fence lineage that wrote the record
    (audit, not resume validity).
    """

    job_id: str
    seq: int
    attempt: int
    kind: CheckpointKind
    step_id: str
    side_effect_class: SideEffectClass | None = None
    idempotency_key: str | None = None
    outcome: str | None = None
    evidence: str | None = None
    generation: int = 0


@dataclass(frozen=True)
class ExternalOpRecord:
    """The durable record of one external operation (a value).

    ``state`` is the authoritative outcome; ``evidence`` is a reference-only
    returned handle when ``COMMITTED``. ``generation`` is the fence lineage that
    created the record.
    """

    op_key: str
    job_id: str
    step_id: str
    op_kind: str
    side_effect_class: SideEffectClass
    idempotency_key: str | None
    state: OpState
    evidence: str | None
    attempt: int
    generation: int


@dataclass(frozen=True)
class ResumePlan:
    """What a re-claimed job may and may not resume.

    ``resume_seq`` is the last completed step (or ``None``). ``blocked_ops`` is
    the set of unresolved non-idempotent operations that must be resolved before
    dependent work may proceed; ``can_proceed`` is ``blocked_ops == empty``.
    """

    resume_seq: int | None
    blocked_ops: frozenset[str]

    @property
    def can_proceed(self) -> bool:
        return not self.blocked_ops


#: Checkpoint kinds that mark completed work (a safe resume marker).
_COMPLETED_KINDS = (CheckpointKind.STEP_DONE, CheckpointKind.SIDE_EFFECT_AFTER, CheckpointKind.PHASE)


def compute_resume_plan(
    checkpoints: Iterable[Checkpoint], ops: Iterable[ExternalOpRecord]
) -> ResumePlan:
    """Compute the resume plan from the loaded ledger.

    An unresolved operation -- one whose state is ``PENDING`` (crash-window) or
    ``UNCERTAIN`` -- blocks dependent work **only if it is non-idempotent**.
    Idempotent-guarded and deterministic work may always resume.
    """
    resume_seq: int | None = None
    for cp in checkpoints:
        if cp.kind in _COMPLETED_KINDS and (resume_seq is None or cp.seq > resume_seq):
            resume_seq = cp.seq

    blocked = frozenset(
        op.op_key
        for op in ops
        if op.side_effect_class is SideEffectClass.NON_IDEMPOTENT
        and op.state in (OpState.PENDING, OpState.UNCERTAIN)
    )
    return ResumePlan(resume_seq=resume_seq, blocked_ops=blocked)


def op_key(job_id: str, step_id: str, op_kind: str) -> str:
    """A stable, deterministic key for one ``(job, step, operation)`` unit.

    Does **not** include the attempt number, so a replayed operation retains the
    same logical key (its distinct execution attempt is recorded on the row and
    in the resolution audit).
    """
    canonical = f"{job_id}{_SEP}{step_id}{_SEP}{op_kind}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()