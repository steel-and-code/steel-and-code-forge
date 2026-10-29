"""FORGE-14 durable checkpoint / external-operation / retry store.

This module is the durable half of the FORGE-14 checkpoint-and-recovery layer:
it persists the pure contracts in :mod:`forge.jobs.progress` (checkpoints,
external-operation records, the lifetime retry counter) against the same SQLite
file and migration stream as :mod:`forge.jobs.store` and
:mod:`forge.jobs.intake`. It is the *store that delegates to the value layer* --
it loads rows, calls a pure function, and writes the successor -- exactly as
:mod:`forge.jobs.store` does for :mod:`forge.jobs.state`.

Safety properties carried from the family of stores:

*   **Atomic, fenced writes.** Every worker-driven write (checkpoint, external-op
    advance, failure record) runs in one ``BEGIN IMMEDIATE`` transaction and is
    refused with :class:`~forge.jobs.store.LeaseExpired` unless the presented
    lease generation is live and un-superseded. A rejected write records one
    ``job_history`` refusal row and re-raises; it never touches a progress
    ledger and never leaves one half-written.
*   **External operations persist *before* dispatch.** ``record_op_before``
    commits the ``PENDING`` row and only then returns, so a crash after the call
    is dispatched always leaves a durable ``PENDING`` record the next worker
    finds.
*   **Uncertain non-idempotent operations are never replayed automatically.**
    They surface in the resume plan as ``blocked_ops``; only an explicit
    operator :meth:`resolve_op` (with a mandatory reason) moves them forward.
*   **Lifetime retry ceiling.** The ``attempt_state.attempt`` counter is
    monotonic for the job's whole life and is never reset or replenished by any
    operation here -- operator recovery cannot buy more attempts.
*   **No clock-driven transitions.** Nothing here sleeps, times out, or flips a
    job into a terminal state. ``next_retry_at`` is a persisted hint the
    executor honours; it is never a timer that fires inside the store.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from .progress import (
    DEFAULT_RETRY_POLICY,
    Checkpoint,
    CheckpointKind,
    ExternalOpRecord,
    FailureClass,
    OpAction,
    OpState,
    OpTransitionError,
    RecoveryDecision,
    ResolutionKind,
    ResumePlan,
    RetryPolicy,
    RetryResult,
    SideEffectClass,
    advance_op_state,
    classify_recovery,
    compute_resume_plan,
    op_key,
    side_effect_class_for,
)
from .state import Actor, ActorRole, AuthorizationRefused
from .store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    ConcurrencyConflict,
    JobNotFound,
    JobStoreError,
    LeaseExpired,
    _connect,
    _enforce_lease,
    _record_history,
)

__all__ = [
    "CheckpointConflict",
    "OpAlreadyExists",
    "OpNotFound",
    "SqliteCheckpointStore",
]


class CheckpointConflict(JobStoreError):
    """A checkpoint with the same ``(job_id, seq)`` already exists."""


class OpAlreadyExists(JobStoreError):
    """An external-operation record with the same ``op_key`` already exists."""


class OpNotFound(JobStoreError):
    """The requested ``op_key`` is not present in the store."""


#: Worker actions legal on :meth:`SqliteCheckpointStore.record_op_outcome`.
#: Operator ``RESOLVE_*`` actions must go through :meth:`resolve_op`.
_WORKER_ACTIONS = (OpAction.COMMIT, OpAction.MARK_UNCERTAIN, OpAction.MARK_FAILED)

#: ResolutionKind -> OpAction for operator resolutions.
_RESOLVE_ACTIONS = {
    ResolutionKind.REPLAY: OpAction.RESOLVE_REPLAY,
    ResolutionKind.SKIP: OpAction.RESOLVE_SKIP,
    ResolutionKind.FAIL: OpAction.RESOLVE_FAIL,
}


class SqliteCheckpointStore:
    """Durable checkpoint/operation/retry store over one SQLite connection.

    Shares the database file and migrations with :class:`SqliteJobStore` and
    :class:`SqliteIntakeStore`; a fresh file is migrated to the current schema
    on open. ``clock`` is the timestamp source (default ``time.time``); inject a
    deterministic callable in tests.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    ) -> None:
        self._clock = clock
        self._retry_policy = retry_policy
        self._conn = _connect(path, busy_timeout_ms)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteCheckpointStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- helpers -----------------------------------------------------------

    def _job_row(self, job_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT id, state, owner, lease_generation, lease_expires_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return row

    def _audit_refusal(
        self,
        *,
        job_id: str,
        event: str,
        row: sqlite3.Row,
        outcome: str,
        at: float,
        reason: str | None = None,
    ) -> None:
        _record_history(
            self._conn,
            job_id=job_id,
            event=event,
            from_state=row["state"],
            to_state=row["state"],
            owner=row["owner"],
            outcome=outcome,
            at=at,
            actor=None,
            reason=reason,
        )

    # -- checkpoints -------------------------------------------------------

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Append one checkpoint, fenced and atomic.

        ``checkpoint.generation`` is the presented lease generation; the write
        is refused as :class:`LeaseExpired` unless it matches the live lease on
        the job. A duplicate ``(job_id, seq)`` raises :class:`CheckpointConflict`.
        """
        if not isinstance(checkpoint, Checkpoint):
            raise TypeError(f"write_checkpoint expects a Checkpoint, got {checkpoint!r}")
        job_id = checkpoint.job_id
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._job_row(job_id)
            now = self._clock()
            try:
                _enforce_lease(job_id, row, checkpoint.generation, now)
            except LeaseExpired as exc:
                self._audit_refusal(
                    job_id=job_id, event="checkpoint", row=row,
                    outcome=type(exc).__name__, at=now,
                )
                self._conn.execute("COMMIT")
                raise
            try:
                self._conn.execute(
                    "INSERT INTO checkpoints (job_id, seq, attempt, kind, step_id, "
                    "side_effect_class, idempotency_key, outcome, evidence, "
                    "generation, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        checkpoint.job_id,
                        checkpoint.seq,
                        checkpoint.attempt,
                        checkpoint.kind.value,
                        checkpoint.step_id,
                        checkpoint.side_effect_class.value
                        if checkpoint.side_effect_class is not None
                        else None,
                        checkpoint.idempotency_key,
                        checkpoint.outcome,
                        checkpoint.evidence,
                        checkpoint.generation,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CheckpointConflict(
                    f"checkpoint (job_id={job_id!r}, seq={checkpoint.seq!r}) already exists"
                ) from exc
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def load_checkpoints(self, job_id: str) -> tuple[Checkpoint, ...]:
        """The job's checkpoints in ``seq`` order (may be empty)."""
        rows = self._conn.execute(
            "SELECT job_id, seq, attempt, kind, step_id, side_effect_class, "
            "idempotency_key, outcome, evidence, generation FROM checkpoints "
            "WHERE job_id = ? ORDER BY seq",
            (job_id,),
        ).fetchall()
        return tuple(_checkpoint_from_row(r) for r in rows)

    # -- external operations ----------------------------------------------

    def record_op_before(
        self,
        job_id: str,
        *,
        step_id: str,
        op_kind: str,
        idempotency_key: str | None = None,
        attempt: int = 1,
        generation: int,
        side_effect_class: SideEffectClass | None = None,
    ) -> str:
        """Durably record a ``PENDING`` external operation *before* dispatch.

        Commits the row and returns its ``op_key`` in the same transaction, so
        the caller must not dispatch the external call until this returns. The
        default side-effect class comes from
        :func:`~forge.jobs.progress.side_effect_class_for` (non-idempotent,
        fail-closed); a caller may pass ``IDEMPOTENT_GUARDED`` only if it has
        demonstrated verified replay protection.
        """
        sclass = side_effect_class if side_effect_class is not None else side_effect_class_for(op_kind)
        if sclass not in (SideEffectClass.IDEMPOTENT_GUARDED, SideEffectClass.NON_IDEMPOTENT):
            raise ValueError(
                f"external operations cannot be {sclass.value!r}; "
                "use IDEMPOTENT_GUARDED or NON_IDEMPOTENT"
            )
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        key = op_key(job_id, step_id, op_kind)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._job_row(job_id)
            now = self._clock()
            try:
                _enforce_lease(job_id, row, generation, now)
            except LeaseExpired as exc:
                self._audit_refusal(
                    job_id=job_id, event="record_op", row=row,
                    outcome=type(exc).__name__, at=now,
                )
                self._conn.execute("COMMIT")
                raise
            try:
                self._conn.execute(
                    "INSERT INTO external_ops (op_key, job_id, step_id, op_kind, "
                    "side_effect_class, idempotency_key, state, evidence, attempt, "
                    "generation, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, ?)",
                    (
                        key,
                        job_id,
                        step_id,
                        op_kind,
                        sclass.value,
                        idempotency_key,
                        attempt,
                        generation,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OpAlreadyExists(key) from exc
            self._conn.execute("COMMIT")
            return key
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def record_op_outcome(
        self,
        op_key_: str,
        action: OpAction,
        generation: int,
        *,
        evidence: str | None = None,
    ) -> ExternalOpRecord:
        """Advance a worker-owned operation's state, fenced and conditional.

        ``action`` is one of ``COMMIT`` (requires ``evidence``), ``MARK_UNCERTAIN``
        or ``MARK_FAILED``. The advance is applied only if the row is still in
        its read state and generation (a compare-and-swap), so a stale or
        contradictory update is refused rather than merged.
        """
        if action not in _WORKER_ACTIONS:
            raise ValueError(
                f"action {action.value!r} is an operator resolution; use resolve_op()"
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            oprow = self._conn.execute(
                "SELECT op_key, job_id, step_id, op_kind, side_effect_class, "
                "idempotency_key, state, evidence, attempt, generation "
                "FROM external_ops WHERE op_key = ?",
                (op_key_,),
            ).fetchone()
            if oprow is None:
                raise OpNotFound(op_key_)
            jobrow = self._job_row(oprow["job_id"])
            now = self._clock()
            try:
                _enforce_lease(oprow["job_id"], jobrow, generation, now)
                successor = advance_op_state(
                    OpState(oprow["state"]), action, evidence=evidence
                )
            except LeaseExpired as exc:
                self._audit_refusal(
                    job_id=oprow["job_id"], event="record_op", row=jobrow,
                    outcome=type(exc).__name__, at=now,
                )
                self._conn.execute("COMMIT")
                raise
            except OpTransitionError as exc:
                self._audit_refusal(
                    job_id=oprow["job_id"], event="record_op", row=jobrow,
                    outcome=type(exc).__name__, at=now,
                )
                self._conn.execute("COMMIT")
                raise

            new_evidence = evidence if action is OpAction.COMMIT else None
            updated = self._conn.execute(
                "UPDATE external_ops SET state = ?, evidence = ?, updated_at = ? "
                "WHERE op_key = ? AND state = ? AND generation = ?",
                (
                    successor.value,
                    new_evidence,
                    now,
                    op_key_,
                    oprow["state"],
                    oprow["generation"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"operation {op_key_!r} changed between read and write"
                )
            self._conn.execute("COMMIT")
            return ExternalOpRecord(
                op_key=op_key_,
                job_id=oprow["job_id"],
                step_id=oprow["step_id"],
                op_kind=oprow["op_kind"],
                side_effect_class=SideEffectClass(oprow["side_effect_class"]),
                idempotency_key=oprow["idempotency_key"],
                state=successor,
                evidence=new_evidence,
                attempt=oprow["attempt"],
                generation=oprow["generation"],
            )
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def load_ops(self, job_id: str) -> tuple[ExternalOpRecord, ...]:
        """The job's external-operation records in write order (may be empty)."""
        rows = self._conn.execute(
            "SELECT op_key, job_id, step_id, op_kind, side_effect_class, "
            "idempotency_key, state, evidence, attempt, generation "
            "FROM external_ops WHERE job_id = ? ORDER BY created_at, op_key",
            (job_id,),
        ).fetchall()
        return tuple(_op_from_row(r) for r in rows)

    # -- retry / attempt accounting ---------------------------------------

    def record_failure(
        self,
        job_id: str,
        failure_class: FailureClass,
        generation: int,
        policy: RetryPolicy | None = None,
    ) -> RetryResult:
        """Record a failed execution attempt and decide retry / fail / escalate.

        Increments the job's lifetime attempt counter and classifies the failure
        under ``policy`` (default :data:`DEFAULT_RETRY_POLICY`). On ``RETRY`` the
        persisted ``next_retry_at``/``backoff_sec`` are set; on ``FAIL`` the
        terminal kind is recorded. The job's own state is **not** changed here --
        the executor drives the existing ``fail``/``block`` events based on the
        returned decision.
        """
        if not isinstance(failure_class, FailureClass):
            raise TypeError(f"failure_class must be a FailureClass, got {failure_class!r}")
        policy = policy if policy is not None else self._retry_policy
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            jobrow = self._job_row(job_id)
            arow = self._conn.execute(
                "SELECT attempt, failure_class, next_retry_at, backoff_sec, "
                "terminal_kind, updated_at FROM attempt_state WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            now = self._clock()
            try:
                _enforce_lease(job_id, jobrow, generation, now)
            except LeaseExpired as exc:
                self._audit_refusal(
                    job_id=job_id, event="record_failure", row=jobrow,
                    outcome=type(exc).__name__, at=now,
                )
                self._conn.execute("COMMIT")
                raise

            attempts_made = (arow["attempt"] if arow is not None else 0) + 1
            result = classify_recovery(failure_class, attempts_made, policy)

            next_delay = result.next_delay
            next_retry_at = now + next_delay if next_delay is not None else None
            backoff_sec = next_delay
            terminal_kind = None
            if result.decision is RecoveryDecision.FAIL:
                terminal_kind = (
                    "deterministic_refusal"
                    if failure_class is FailureClass.DETERMINISTIC_REFUSAL
                    else "retries_exhausted"
                )

            self._conn.execute(
                "INSERT INTO attempt_state (job_id, attempt, failure_class, "
                "next_retry_at, backoff_sec, terminal_kind, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "attempt = excluded.attempt, "
                "failure_class = excluded.failure_class, "
                "next_retry_at = excluded.next_retry_at, "
                "backoff_sec = excluded.backoff_sec, "
                "terminal_kind = excluded.terminal_kind, "
                "updated_at = excluded.updated_at",
                (
                    job_id,
                    attempts_made,
                    failure_class.value,
                    next_retry_at,
                    backoff_sec,
                    terminal_kind,
                    now,
                ),
            )
            self._conn.execute("COMMIT")
            return result
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def attempt_count(self, job_id: str) -> int:
        """The job's lifetime attempt count so far (0 before the first failure)."""
        row = self._conn.execute(
            "SELECT attempt FROM attempt_state WHERE job_id = ?", (job_id,)
        ).fetchone()
        return 0 if row is None else row["attempt"]

    # -- operator resolution ----------------------------------------------

    def resolve_op(
        self,
        op_key_: str,
        actor: Actor,
        resolution: ResolutionKind,
        reason: str,
    ) -> ExternalOpRecord:
        """Operator resolves one uncertain operation (REPLAY / SKIP / FAIL).

        Requires an OPERATOR-role actor and a non-empty reason (recorded verbatim
        in the ``op_resolutions`` audit). ``REPLAY`` re-arms the same logical
        ``op_key`` as a new ``PENDING`` attempt (``attempt`` increments) -- the
        authorization is scoped to this one operation and one retry. ``SKIP`` and
        ``FAIL`` are terminal for the operation.
        """
        if not isinstance(actor, Actor) or actor.role is not ActorRole.OPERATOR:
            raise AuthorizationRefused("operation resolution requires the operator role")
        if not isinstance(resolution, ResolutionKind):
            raise TypeError("resolution must be a ResolutionKind")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("resolve_op requires a non-empty reason")
        action = _RESOLVE_ACTIONS[resolution]
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            oprow = self._conn.execute(
                "SELECT op_key, job_id, step_id, op_kind, side_effect_class, "
                "idempotency_key, state, evidence, attempt, generation "
                "FROM external_ops WHERE op_key = ?",
                (op_key_,),
            ).fetchone()
            if oprow is None:
                raise OpNotFound(op_key_)
            jobrow = self._job_row(oprow["job_id"])
            now = self._clock()
            try:
                successor = advance_op_state(OpState(oprow["state"]), action)
            except OpTransitionError as exc:
                self._audit_refusal(
                    job_id=oprow["job_id"], event="resolve_op", row=jobrow,
                    outcome=type(exc).__name__, at=now, reason=reason,
                )
                self._conn.execute("COMMIT")
                raise

            new_attempt = oprow["attempt"] + 1 if resolution is ResolutionKind.REPLAY else oprow["attempt"]
            updated = self._conn.execute(
                "UPDATE external_ops SET state = ?, attempt = ?, updated_at = ? "
                "WHERE op_key = ? AND state = ?",
                (successor.value, new_attempt, now, op_key_, oprow["state"]),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"operation {op_key_!r} changed between read and write"
                )
            self._conn.execute(
                "INSERT INTO op_resolutions (op_key, job_id, resolution, "
                "actor_identity, actor_role, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    op_key_,
                    oprow["job_id"],
                    resolution.value,
                    actor.identity,
                    actor.role.value,
                    reason,
                    now,
                ),
            )
            self._conn.execute("COMMIT")
            return ExternalOpRecord(
                op_key=op_key_,
                job_id=oprow["job_id"],
                step_id=oprow["step_id"],
                op_kind=oprow["op_kind"],
                side_effect_class=SideEffectClass(oprow["side_effect_class"]),
                idempotency_key=oprow["idempotency_key"],
                state=successor,
                evidence=oprow["evidence"],
                attempt=new_attempt,
                generation=oprow["generation"],
            )
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    # -- resume ------------------------------------------------------------

    def resume_plan(self, job_id: str) -> ResumePlan:
        """Compute the resume plan for a job from its ledger.

        Raises :class:`JobNotFound` if the job is absent. A job with an
        unresolved non-idempotent operation returns ``can_proceed == False`` and
        lists those operations in ``blocked_ops``; nothing here dispatches work
        -- the executor honours the plan.
        """
        self._job_row(job_id)
        return compute_resume_plan(self.load_checkpoints(job_id), self.load_ops(job_id))


# --------------------------------------------------------------------------
# Row reconstruction
# --------------------------------------------------------------------------


def _checkpoint_from_row(r: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        job_id=r["job_id"],
        seq=r["seq"],
        attempt=r["attempt"],
        kind=CheckpointKind(r["kind"]),
        step_id=r["step_id"],
        side_effect_class=(
            SideEffectClass(r["side_effect_class"])
            if r["side_effect_class"] is not None
            else None
        ),
        idempotency_key=r["idempotency_key"],
        outcome=r["outcome"],
        evidence=r["evidence"],
        generation=r["generation"],
    )


def _op_from_row(r: sqlite3.Row) -> ExternalOpRecord:
    return ExternalOpRecord(
        op_key=r["op_key"],
        job_id=r["job_id"],
        step_id=r["step_id"],
        op_kind=r["op_kind"],
        side_effect_class=SideEffectClass(r["side_effect_class"]),
        idempotency_key=r["idempotency_key"],
        state=OpState(r["state"]),
        evidence=r["evidence"],
        attempt=r["attempt"],
        generation=r["generation"],
    )