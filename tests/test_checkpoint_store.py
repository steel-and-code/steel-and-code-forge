"""Tests for the FORGE-14 durable checkpoint/operation/retry store.

Exercises the fenced, atomic writes; the PENDING-before-dispatch durability
contract; the operator resolution path (REPLAY/SKIP/FAIL); the lifetime retry
ceiling (never replenished by recovery); and the restart/OS-process scenarios
review asked for. Concurrency uses ``sys.executable`` subprocesses polling a
shared ``go`` marker, matching the job-store suite.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

from forge.jobs.checkpoint_store import (
    CheckpointConflict,
    OpAlreadyExists,
    OpNotFound,
    SqliteCheckpointStore,
)
from forge.jobs.progress import (
    Checkpoint,
    CheckpointKind,
    ExternalOpRecord,
    FailureClass,
    OpAction,
    OpState,
    RecoveryDecision,
    ResolutionKind,
    RetryPolicy,
    SideEffectClass,
)
from forge.jobs.state import (
    Actor,
    ActorRole,
    AuthorizationRefused,
    BudgetCheck,
    JobEvent,
    new_job,
)
from forge.jobs.store import (
    DEFAULT_LEASE_TTL,
    JobNotFound,
    LeaseExpired,
    SqliteJobStore,
)

WORKER = Actor("alice")
OTHER = Actor("bob")
OPERATOR = Actor("operator", ActorRole.OPERATOR)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


class _SteppedClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _job(job_id: str = "job-1", *, budget: float | None = 10.0):
    return new_job(
        job_id=job_id,
        ticket_ref="OUT-123",
        repo="outpost",
        allowed_tools=("read_file", "write_file"),
        budget=budget,
    )


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "jobs.db")


def _claim(path, *, clock=None, lease_ttl=DEFAULT_LEASE_TTL, job_id="job-1") -> int:
    kwargs: dict = {"lease_ttl": lease_ttl}
    if clock is not None:
        kwargs["clock"] = clock
    js = SqliteJobStore(path, **kwargs)
    js.save(_job(job_id))
    rec = js.claim(job_id, WORKER, budget_check=BudgetCheck.approved())
    js.close()
    return rec.lease_generation


def _history(path, job_id="job-1"):
    conn = sqlite3.connect(path)
    try:
        return list(
            conn.execute(
                "SELECT event, outcome, owner, actor_identity FROM job_history "
                "WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
        )
    finally:
        conn.close()


def _attempt_row(path, job_id="job-1"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT attempt, failure_class, next_retry_at, backoff_sec, terminal_kind "
            "FROM attempt_state WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        conn.close()


def _checkpoint_count(path, job_id="job-1"):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE job_id = ?", (job_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _seqs(path, job_id="job-1"):
    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT seq FROM checkpoints WHERE job_id = ?", (job_id,)
            )
        }
    finally:
        conn.close()


def _ck(job_id="job-1", seq=1, kind=CheckpointKind.STEP_DONE, gen=1, **kw):
    fields = dict(job_id=job_id, seq=seq, attempt=1, kind=kind, step_id="s", generation=gen)
    fields.update(kw)
    return Checkpoint(**fields)


# --------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------


def test_write_and_load_checkpoint_roundtrips(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.write_checkpoint(
            _ck(gen=gen, kind=CheckpointKind.STEP_DONE, attempt=2, step_id="run")
        )
        cps = store.load_checkpoints("job-1")
        assert len(cps) == 1
        cp = cps[0]
        assert cp.seq == 1
        assert cp.kind is CheckpointKind.STEP_DONE
        assert cp.generation == gen
    finally:
        store.close()


def test_checkpoints_ordered_by_seq(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.write_checkpoint(_ck(seq=3, gen=gen))
        store.write_checkpoint(_ck(seq=1, gen=gen))
        store.write_checkpoint(_ck(seq=2, gen=gen))
        assert [c.seq for c in store.load_checkpoints("job-1")] == [1, 2, 3]
    finally:
        store.close()


def test_duplicate_seq_raises_checkpoint_conflict(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.write_checkpoint(_ck(seq=1, gen=gen))
        with pytest.raises(CheckpointConflict):
            store.write_checkpoint(_ck(seq=1, gen=gen))
    finally:
        store.close()


def test_stale_generation_checkpoint_write_is_fenced_and_audited(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(LeaseExpired):
            store.write_checkpoint(_ck(seq=1, gen=gen + 999))
    finally:
        store.close()
    # nothing written to the progress ledger, exactly one audit refusal row
    assert _checkpoint_count(path) == 0
    refusals = [r for r in _history(path) if r[0] == "checkpoint"]
    assert len(refusals) == 1
    assert refusals[0][1] == "LeaseExpired"


def test_expired_lease_checkpoint_write_is_fenced(path):
    clock = _SteppedClock(0.0)
    gen = _claim(path, clock=clock, lease_ttl=10.0)
    store = SqliteCheckpointStore(path, clock=clock)
    try:
        store.write_checkpoint(_ck(seq=1, gen=gen))  # live at t=0
        clock.advance(11.0)  # lease lapsed
        with pytest.raises(LeaseExpired):
            store.write_checkpoint(_ck(seq=2, gen=gen))
    finally:
        store.close()
    assert _seqs(path) == {1}


# --------------------------------------------------------------------------
# External operations
# --------------------------------------------------------------------------


def test_record_op_before_produces_pending_row_durably_before_dispatch(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    key = store.record_op_before(
        "job-1", step_id="push", op_kind="git_push", generation=gen
    )
    # A second independent connection sees the committed PENDING row at once,
    # proving the record is durable before the caller would dispatch the call.
    store.close()
    s2 = SqliteCheckpointStore(path)
    try:
        ops = s2.load_ops("job-1")
        assert len(ops) == 1
        assert ops[0].op_key == key
        assert ops[0].state is OpState.PENDING
        assert ops[0].side_effect_class is SideEffectClass.NON_IDEMPOTENT
    finally:
        s2.close()


def test_record_op_before_defaults_fail_closed_and_rejects_deterministic(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="x", op_kind="unknown-thing", generation=gen)
        ops = store.load_ops("job-1")
        assert ops[0].side_effect_class is SideEffectClass.NON_IDEMPOTENT
        assert ops[0].op_key == key
        with pytest.raises(ValueError):
            store.record_op_before(
                "job-1", step_id="y", op_kind="git_push", generation=gen,
                side_effect_class=SideEffectClass.DETERMINISTIC,
            )
    finally:
        store.close()


def test_record_op_before_duplicate_key_raises(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        with pytest.raises(OpAlreadyExists):
            store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
    finally:
        store.close()


def test_commit_requires_evidence_and_leaves_op_pending(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        from forge.jobs.progress import OpEvidenceRequired
        with pytest.raises(OpEvidenceRequired):
            store.record_op_outcome(key, OpAction.COMMIT, gen)
        assert store.load_ops("job-1")[0].state is OpState.PENDING
    finally:
        store.close()


def test_commit_with_evidence_and_uncertain_path(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        rec = store.record_op_outcome(key, OpAction.COMMIT, gen, evidence="pr#7")
        assert rec.state is OpState.COMMITTED
        assert rec.evidence == "pr#7"

        key2 = store.record_op_before("job-1", step_id="p2", op_kind="pr_create", generation=gen)
        rec2 = store.record_op_outcome(key2, OpAction.MARK_UNCERTAIN, gen)
        assert rec2.state is OpState.UNCERTAIN
    finally:
        store.close()


def test_contradictory_op_transition_is_refused(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        store.record_op_outcome(key, OpAction.COMMIT, gen, evidence="pr#7")
        # COMMITTED is absorbing; any further advance is refused
        from forge.jobs.progress import InvalidOpTransition
        with pytest.raises(InvalidOpTransition):
            store.record_op_outcome(key, OpAction.MARK_FAILED, gen)
        assert store.load_ops("job-1")[0].state is OpState.COMMITTED
    finally:
        store.close()


def test_stale_generation_op_write_is_fenced(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        with pytest.raises(LeaseExpired):
            store.record_op_outcome(key, OpAction.COMMIT, gen + 999, evidence="pr#7")
        assert store.load_ops("job-1")[0].state is OpState.PENDING
    finally:
        store.close()


# --------------------------------------------------------------------------
# Operator resolution
# --------------------------------------------------------------------------


def _uncertain_op(path, store, gen, step_id="push"):
    key = store.record_op_before("job-1", step_id=step_id, op_kind="git_push", generation=gen)
    store.record_op_outcome(key, OpAction.MARK_UNCERTAIN, gen)
    return key


def test_resolve_skip_marks_op_skipped(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        rec = store.resolve_op(key, OPERATOR, ResolutionKind.SKIP, "already handled")
        assert rec.state is OpState.SKIPPED
        assert rec.attempt == 1  # SKIP does not re-arm
    finally:
        store.close()


def test_resolve_replay_rearms_same_key_incremented_attempt(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        rec = store.resolve_op(key, OPERATOR, ResolutionKind.REPLAY, "authorize one retry")
        assert rec.op_key == key
        assert rec.state is OpState.PENDING
        assert rec.attempt == 2  # distinct execution attempt, same logical key
    finally:
        store.close()


def test_resolve_requires_operator_role(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        with pytest.raises(AuthorizationRefused):
            store.resolve_op(key, WORKER, ResolutionKind.SKIP, "nope")
    finally:
        store.close()


def test_resolve_requires_nonempty_reason(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        with pytest.raises(ValueError):
            store.resolve_op(key, OPERATOR, ResolutionKind.SKIP, "   ")
    finally:
        store.close()


def test_resolve_only_from_uncertain_state(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        from forge.jobs.progress import InvalidOpTransition
        with pytest.raises(InvalidOpTransition):
            store.resolve_op(key, OPERATOR, ResolutionKind.SKIP, "too soon")
    finally:
        store.close()


def test_resolution_is_audited(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        store.resolve_op(key, OPERATOR, ResolutionKind.REPLAY, "one retry authorized")
    finally:
        store.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT op_key, resolution, actor_role, reason FROM op_resolutions WHERE job_id = ?",
            ("job-1",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "replay"
    assert rows[0][2] == "operator"
    assert rows[0][3] == "one retry authorized"


# --------------------------------------------------------------------------
# Retry / lifetime ceiling
# --------------------------------------------------------------------------


def test_lifetime_ceiling_bounded_and_terminal(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        r1 = store.record_failure("job-1", FailureClass.TRANSIENT, gen)
        r2 = store.record_failure("job-1", FailureClass.TRANSIENT, gen)
        r3 = store.record_failure("job-1", FailureClass.TRANSIENT, gen)
        assert r1.decision is RecoveryDecision.RETRY
        assert r2.decision is RecoveryDecision.RETRY
        assert r3.decision is RecoveryDecision.FAIL
        assert store.attempt_count("job-1") == 3
    finally:
        store.close()
    row = _attempt_row(path)
    assert row[0] == 3
    assert row[4] == "retries_exhausted"


def test_recovery_does_not_replenish_attempts(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        for _ in range(3):
            store.record_failure("job-1", FailureClass.TRANSIENT, gen)
    finally:
        store.close()
    # operator recovers the (now terminal-exhausted) job back to ready and a new
    # worker re-claims: the lifetime counter is untouched.
    js = SqliteJobStore(path)
    js.recover("job-1", OPERATOR, "reset ownership", force=True)
    gen2 = js.claim("job-1", Actor("carol"), budget_check=BudgetCheck.approved()).lease_generation
    js.close()
    assert gen2 > gen
    still = SqliteCheckpointStore(path)
    try:
        assert still.attempt_count("job-1") == 3
    finally:
        still.close()


def test_retry_persists_backoff_and_next_retry_at(path):
    clock = _SteppedClock(100.0)
    gen = _claim(path, clock=clock)
    store = SqliteCheckpointStore(path, clock=clock)
    try:
        r = store.record_failure("job-1", FailureClass.TRANSIENT, gen)
        assert r.next_delay == 60.0
    finally:
        store.close()
    row = _attempt_row(path)
    assert row[1] == "transient"
    assert row[2] == pytest.approx(160.0)  # 100 + 60
    assert row[3] == pytest.approx(60.0)
    assert row[4] is None


def test_uncertain_side_effect_escalates_no_backoff(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        r = store.record_failure("job-1", FailureClass.SIDE_EFFECT_UNCERTAIN, gen)
        assert r.decision is RecoveryDecision.ESCALATE
    finally:
        store.close()
    row = _attempt_row(path)
    assert row[4] is None  # not terminal
    assert row[2] is None  # no retry scheduled


def test_deterministic_refusal_is_terminal(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        r = store.record_failure("job-1", FailureClass.DETERMINISTIC_REFUSAL, gen)
        assert r.decision is RecoveryDecision.FAIL
        assert r.reason == "deterministic refusal"
    finally:
        store.close()
    assert _attempt_row(path)[4] == "deterministic_refusal"


def test_stale_generation_failure_write_fenced_and_attempt_unchanged(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(LeaseExpired):
            store.record_failure("job-1", FailureClass.TRANSIENT, gen + 999)
        assert store.attempt_count("job-1") == 0
    finally:
        store.close()


# --------------------------------------------------------------------------
# Resume plan / crash recovery
# --------------------------------------------------------------------------


def test_resume_plan_blocks_unresolved_non_idempotent_op(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
        plan = store.resume_plan("job-1")
        assert plan.can_proceed is False
        assert key in plan.blocked_ops
        # operator resolves -> blocked clears
        store.record_op_outcome(key, OpAction.MARK_UNCERTAIN, gen)
        store.resolve_op(key, OPERATOR, ResolutionKind.SKIP, "skip it")
        assert store.resume_plan("job-1").can_proceed is True
    finally:
        store.close()


def test_idempotent_guarded_pending_does_not_block(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.record_op_before(
            "job-1", step_id="push", op_kind="git_push", generation=gen,
            side_effect_class=SideEffectClass.IDEMPOTENT_GUARDED,
        )
        assert store.resume_plan("job-1").can_proceed is True
    finally:
        store.close()


def test_crash_mid_side_effect_recovers_blocked(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    # SIDE_EFFECT_BEFORE + PENDING op written; crash happens before AFTER.
    store.write_checkpoint(_ck(seq=1, kind=CheckpointKind.SIDE_EFFECT_BEFORE, gen=gen))
    store.record_op_before("job-1", step_id="push", op_kind="git_push", generation=gen)
    store.close()  # simulate process death

    reopened = SqliteCheckpointStore(path)
    try:
        plan = reopened.resume_plan("job-1")
        assert plan.can_proceed is False
        ops = reopened.load_ops("job-1")
        assert ops[0].state is OpState.PENDING
    finally:
        reopened.close()


def test_deterministic_steps_resume_with_resume_seq(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        store.write_checkpoint(_ck(seq=1, kind=CheckpointKind.STEP_DONE, gen=gen))
        store.write_checkpoint(_ck(seq=2, kind=CheckpointKind.STEP_DONE, gen=gen))
        plan = store.resume_plan("job-1")
        assert plan.resume_seq == 2
        assert plan.can_proceed is True
    finally:
        store.close()


def test_resume_plan_missing_job_raises(path):
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(JobNotFound):
            store.resume_plan("absent")
    finally:
        store.close()


# --------------------------------------------------------------------------
# OS-process races
# --------------------------------------------------------------------------

_WRITE_CHECKPOINT_WORKER = r"""
import os, sys, time
go_file, result_file = sys.argv[3], sys.argv[4]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.checkpoint_store import SqliteCheckpointStore
from forge.jobs.progress import Checkpoint, CheckpointKind
s = SqliteCheckpointStore(sys.argv[1])
try:
    s.write_checkpoint(Checkpoint(
        job_id="job-1", seq=int(sys.argv[2]), attempt=1,
        kind=CheckpointKind.STEP_DONE, step_id="s", generation=int(sys.argv[5]),
    ))
    open(result_file, "w").write("ok")
except Exception as exc:
    open(result_file, "w").write(type(exc).__name__)
finally:
    s.close()
"""


def test_os_process_checkpoint_write_race_one_winner(path):
    gen = _claim(path)
    go_file = path + ".go"

    def run(seq, generation, tag):
        result_file = path + "." + tag
        subprocess.run(
            [sys.executable, "-c", _WRITE_CHECKPOINT_WORKER, path, str(seq), go_file, result_file, str(generation)],
            cwd=ROOT,
            check=True,
        )
        with open(result_file) as fh:
            return fh.read().strip()

    # valid-generation worker and stale-generation worker race on one job.
    import threading
    results = {}

    def worker(seq, generation, tag):
        results[tag] = run(seq, generation, tag)

    t1 = threading.Thread(target=worker, args=(1, gen, "valid"))
    t2 = threading.Thread(target=worker, args=(2, gen + 999, "stale"))
    t1.start()
    t2.start()
    open(go_file, "w").close()  # release both
    t1.join()
    t2.join()

    assert results["valid"] == "ok"
    assert results["stale"] == "LeaseExpired"
    assert _seqs(path) == {1}  # only the valid worker's checkpoint persisted


def test_os_process_first_open_race_on_checkpoint_store(path):
    go_file = path + ".go"
    worker = r"""
import os, sys, time
go_file = sys.argv[2]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.checkpoint_store import SqliteCheckpointStore
s = SqliteCheckpointStore(sys.argv[1])
print(s.schema_version)
s.close()
"""
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", worker, path, go_file],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    open(go_file, "w").close()
    outs = [p.communicate(timeout=30)[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert set(outs) == {"4"}  # both migrated to the current schema


# --------------------------------------------------------------------------
# Edge refusals (error branches)
# --------------------------------------------------------------------------


def test_write_checkpoint_rejects_non_checkpoint(path):
    _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(TypeError):
            store.write_checkpoint("not-a-checkpoint")
    finally:
        store.close()


def test_record_op_outcome_missing_op_raises(path):
    _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(OpNotFound):
            store.record_op_outcome("nope", OpAction.MARK_FAILED, 1)
    finally:
        store.close()


def test_record_op_before_rejects_bad_attempt(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        with pytest.raises(ValueError):
            store.record_op_before("job-1", step_id="x", op_kind="git_push", generation=gen, attempt=0)
    finally:
        store.close()


def test_resolve_op_rejects_non_resolution_kind(path):
    gen = _claim(path)
    store = SqliteCheckpointStore(path)
    try:
        key = _uncertain_op(path, store, gen)
        with pytest.raises(TypeError):
            store.resolve_op(key, OPERATOR, "replay", "nope")
    finally:
        store.close()