"""Tests for the durable SQLite-backed job store (FORGE-11, FORGE-12).

FORGE-11 covered persistence, schema migrations, atomic claiming, restart
recovery, and the transactional history pairing. FORGE-12 adds durable leases,
lease-generation fencing, the operator recovery path, and the actor/role audit
split.

Concurrency is exercised with independent ``SqliteJobStore`` connections to one
file (the faithful model of two processes), synchronised with a barrier or a
``go`` marker file so the operations genuinely race. Lease timing is driven by
an injected ``_SteppedClock`` so no test sleeps.
"""

from __future__ import annotations

import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

from forge.jobs.idempotency import DedupeStore
from forge.jobs.store import (
    SCHEMA_VERSION,
    DEFAULT_LEASE_TTL,
    MIGRATIONS,
    ConcurrencyConflict,
    JobAlreadyExists,
    JobNotFound,
    JobRecord,
    JobStoreError,
    LeaseExpired,
    MigrationError,
    SqliteDedupeStore,
    SqliteJobStore,
)
from forge.jobs.state import (
    Actor,
    ActorRole,
    AuthorizationRefused,
    BudgetCheck,
    BudgetRefused,
    ClaimConflict,
    InvalidTransition,
    Job,
    JobEvent,
    JobState,
    JobTransitionError,
    new_job,
)

WORKER = Actor("alice")
OTHER = Actor("bob")
OPERATOR = Actor("operator", ActorRole.OPERATOR)

#: Repo root, used to make ``forge`` importable in the subprocess workers.
ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


class _SteppedClock:
    """A deterministic, manually advanced timestamp source for lease tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


# Subprocess workers. Each polls for a ``go`` marker file so the processes
# genuinely race on the database rather than merely running in sequence. The
# workers accept ``path`` as argv[1]; the rest of argv is worker-specific.

_FIRST_OPEN_WORKER = r"""
import os, sys, time
go_file = sys.argv[2]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.store import SqliteJobStore, SCHEMA_VERSION
s = SqliteJobStore(sys.argv[1])
print(s.schema_version)
s.close()
"""

_CLAIM_WORKER = r"""
import os, sys, time
go_file, result_file = sys.argv[3], sys.argv[4]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.store import SqliteJobStore
from forge.jobs.state import Actor, BudgetCheck
s = SqliteJobStore(sys.argv[1])
try:
    s.claim("job-1", Actor(sys.argv[2]), budget_check=BudgetCheck.approved())
    open(result_file, "w").write("ok:" + sys.argv[2])
except Exception as exc:
    open(result_file, "w").write(type(exc).__name__)
finally:
    s.close()
"""

_RENEW_WORKER = r"""
import os, sys, time
go_file, result_file = sys.argv[2], sys.argv[4]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.store import SqliteJobStore
from forge.jobs.state import Actor
s = SqliteJobStore(sys.argv[1])
try:
    s.renew("job-1", Actor("alice"), int(sys.argv[3]))
    open(result_file, "w").write("ok")
except Exception as exc:
    open(result_file, "w").write(type(exc).__name__)
finally:
    s.close()
"""

_RECOVER_WORKER = r"""
import os, sys, time
go_file, result_file = sys.argv[2], sys.argv[3]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.store import SqliteJobStore
from forge.jobs.state import Actor, ActorRole
s = SqliteJobStore(sys.argv[1])
try:
    s.recover("job-1", Actor("operator", ActorRole.OPERATOR), "race test", force=True)
    open(result_file, "w").write("ok")
except Exception as exc:
    open(result_file, "w").write(type(exc).__name__)
finally:
    s.close()
"""


def _job(job_id: str = "job-1", *, budget: float | None = 10.0) -> Job:
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


@pytest.fixture
def store(path):
    s = SqliteJobStore(path)
    yield s
    s.close()


def _history(store, job_id):
    return list(
        store._conn.execute(
            "SELECT event, from_state, to_state, owner, outcome, "
            "actor_identity, actor_role, reason "
            "FROM job_history WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
    )


# --------------------------------------------------------------------------
# Persistence of the value and every field
# --------------------------------------------------------------------------

def test_save_and_load_roundtrips_the_job(store):
    job = _job()
    assert store.save(job) is job
    assert store.load("job-1") == job


def test_save_persists_every_field(store):
    job = _job(budget=7.5)
    store.save(job)
    loaded = store.load("job-1")
    assert loaded.ticket_ref == "OUT-123"
    assert loaded.repo == "outpost"
    assert loaded.state is JobState.READY
    assert loaded.allowed_tools == ("read_file", "write_file")
    assert loaded.budget == 7.5
    assert loaded.owner is None


def test_unset_budget_persists_as_none(store):
    job = _job(budget=None)
    store.save(job)
    assert store.load("job-1").budget is None


def test_load_missing_returns_none(store):
    assert store.load("absent") is None


def test_get_returns_record_with_metadata(store):
    store.save(_job())
    record = store.get("job-1")
    assert isinstance(record, JobRecord)
    assert record.job == _job()
    assert record.version == 0
    assert record.created_at == record.updated_at  # untouched since register
    # Fresh job: no lease, generation zero.
    assert record.lease_expires_at is None
    assert record.lease_generation == 0


def test_get_missing_returns_none(store):
    assert store.get("absent") is None


def test_save_duplicate_id_is_refused(store):
    store.save(_job())
    with pytest.raises(JobAlreadyExists):
        store.save(_job("job-1"))


def test_save_rejects_non_job(store):
    with pytest.raises(TypeError):
        store.save("not a job")


# --------------------------------------------------------------------------
# Registration and history
# --------------------------------------------------------------------------

def test_save_records_a_register_history_row(store):
    store.save(_job())
    rows = _history(store, "job-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "register"
    assert row["from_state"] is None
    assert row["to_state"] == "ready"
    assert row["owner"] is None
    assert row["outcome"] == "ok"
    assert row["actor_identity"] is None
    assert row["actor_role"] is None


# --------------------------------------------------------------------------
# Claiming: atomic, owner-setting, fail-closed budget, lease minting
# --------------------------------------------------------------------------

def test_claim_sets_owner_and_persists(store):
    store.save(_job())
    rec = store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    assert rec.job.state is JobState.CLAIMED
    assert rec.job.owner == WORKER.identity
    assert store.load("job-1") == rec.job


def test_claim_mints_a_lease_and_generation(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        assert rec.lease_generation == 1
        assert rec.lease_expires_at == clock.now + 100.0


def test_claim_without_budget_check_fails_closed(store):
    store.save(_job())
    with pytest.raises(BudgetRefused):
        store.claim("job-1", WORKER)
    job = store.load("job-1")
    assert job.state is JobState.READY
    assert job.owner is None


def test_claim_with_exhausted_budget_fails_closed_and_leaves_row_unchanged(store):
    store.save(_job())
    before = store.get("job-1")
    check = BudgetCheck(False, True, detail="job budget exhausted")
    with pytest.raises(BudgetRefused):
        store.claim("job-1", WORKER, budget_check=check)
    after = store.get("job-1")
    assert after.job.state is JobState.READY
    assert after.job.owner is None
    assert after.updated_at == before.updated_at


def test_claim_on_missing_job_raises(store):
    with pytest.raises(JobNotFound):
        store.claim("absent", WORKER, budget_check=BudgetCheck.approved())


def test_claim_requires_an_actor(store):
    store.save(_job())
    with pytest.raises(TypeError):
        store.claim("job-1", "alice", budget_check=BudgetCheck.approved())


def test_second_claim_of_an_owned_job_is_refused(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(JobTransitionError):
        store.claim("job-1", OTHER, budget_check=BudgetCheck.approved())


def test_transition_requires_a_job_event(store):
    with pytest.raises(TypeError):
        store.transition("job-1", "claim")


def test_transition_rejects_claim_and_recover(store):
    store.save(_job())
    with pytest.raises(ValueError, match="claim"):
        store.transition("job-1", JobEvent.CLAIM, actor=WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(ValueError, match="recover"):
        store.transition("job-1", JobEvent.RECOVER, actor=OPERATOR)


# --------------------------------------------------------------------------
# Ownership: worker mutations are gated on the claim holder
# --------------------------------------------------------------------------

def test_start_with_wrong_owner_is_refused(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict, match="does not match"):
        store.transition(
            "job-1", JobEvent.START,
            actor=OTHER, budget_check=BudgetCheck.approved(),
        )
    assert store.load("job-1").state is JobState.CLAIMED


def test_release_requires_the_claim_owner(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict):
        store.transition("job-1", JobEvent.RELEASE, actor=OTHER)


def test_full_lifecycle_through_the_store(store):
    store.save(_job())
    rec = store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    running = store.transition(
        "job-1", JobEvent.START,
        actor=WORKER, budget_check=BudgetCheck.approved(),
        generation=rec.lease_generation,
    )
    assert running.state is JobState.RUNNING
    done = store.transition(
        "job-1", JobEvent.COMPLETE,
        actor=WORKER, generation=rec.lease_generation,
    )
    assert done.state is JobState.DONE
    assert store.load("job-1").state is JobState.DONE


# --------------------------------------------------------------------------
# Transactional state + history pairing (with the actor/owner split)
# --------------------------------------------------------------------------

def test_transitions_append_one_history_row_each(store):
    store.save(_job())
    rec = store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    store.transition(
        "job-1", JobEvent.START,
        actor=WORKER, budget_check=BudgetCheck.approved(),
        generation=rec.lease_generation,
    )
    events = [row["event"] for row in _history(store, "job-1")]
    assert events == ["register", "claim", "start"]


def test_successful_transition_records_from_to_and_actor(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    rows = _history(store, "job-1")
    claim = rows[1]
    assert claim["from_state"] == "ready"
    assert claim["to_state"] == "claimed"
    assert claim["owner"] == WORKER.identity  # job owner snapshot
    assert claim["actor_identity"] == WORKER.identity
    assert claim["actor_role"] == "worker"
    assert claim["outcome"] == "ok"


def test_refused_transition_is_audited_separating_actor_from_owner(store):
    store.save(_job())
    rec = store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict):
        store.transition(
            "job-1", JobEvent.START,
            actor=OTHER, budget_check=BudgetCheck.approved(),
            generation=rec.lease_generation,
        )
    rows = _history(store, "job-1")
    refusal = rows[-1]
    assert refusal["event"] == "start"
    assert refusal["from_state"] == "claimed"
    assert refusal["to_state"] == "claimed"  # unchanged
    assert refusal["owner"] == WORKER.identity  # job owner, unchanged
    assert refusal["actor_identity"] == OTHER.identity  # the refused actor
    assert refusal["actor_role"] == "worker"
    assert refusal["outcome"] == "ClaimConflict"
    assert store.load("job-1").state is JobState.CLAIMED


def test_refused_claim_does_not_leak_a_history_success(store):
    store.save(_job())
    with pytest.raises(BudgetRefused):
        store.claim("job-1", WORKER, budget_check=None)
    rows = _history(store, "job-1")
    assert len(rows) == 2  # register + the refused claim, nothing else
    assert rows[-1]["outcome"] == "BudgetRefused"


# --------------------------------------------------------------------------
# Concurrent claims: exactly one winner across connections
# --------------------------------------------------------------------------

def test_concurrent_claims_yield_exactly_one_owner(path):
    starter = SqliteJobStore(path)
    starter.save(_job())
    starter.close()

    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def claimer(actor: Actor) -> None:
        s = SqliteJobStore(path)
        try:
            barrier.wait()
            s.claim("job-1", actor, budget_check=BudgetCheck.approved())
            results[actor.identity] = "ok"
        except JobTransitionError as exc:
            results[actor.identity] = type(exc).__name__
        finally:
            s.close()

    threads = [
        threading.Thread(target=claimer, args=(WORKER,)),
        threading.Thread(target=claimer, args=(OTHER,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["InvalidTransition", "ok"], results

    reader = SqliteJobStore(path)
    try:
        stored = reader.load("job-1")
    finally:
        reader.close()
    assert stored.state is JobState.CLAIMED
    assert stored.owner in (WORKER.identity, OTHER.identity)
    assert results[stored.owner] == "ok"


# --------------------------------------------------------------------------
# Leases: renewal, expiry, and generation fencing (stepped clock)
# --------------------------------------------------------------------------

def test_renew_extends_lease(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        clock.advance(50.0)
        new_expiry = s.renew("job-1", WORKER, rec.lease_generation)
        assert new_expiry == clock.now + 100.0
        assert s.get("job-1").lease_expires_at == new_expiry


def test_renew_after_expiry_is_refused(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        clock.advance(200.0)  # lease lapsed
        with pytest.raises(LeaseExpired):
            s.renew("job-1", WORKER, rec.lease_generation)


def test_worker_mutation_after_expiry_is_refused(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        clock.advance(200.0)
        with pytest.raises(LeaseExpired):
            s.transition(
                "job-1", JobEvent.START,
                actor=WORKER, budget_check=BudgetCheck.approved(),
                generation=rec.lease_generation,
            )
        # The job did not move.
        assert s.load("job-1").state is JobState.CLAIMED


def test_renew_requires_matching_generation(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        with pytest.raises(LeaseExpired):
            s.renew("job-1", WORKER, rec.lease_generation + 1)


def test_renew_refuses_non_owner(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        with pytest.raises(ClaimConflict):
            s.renew("job-1", OTHER, rec.lease_generation)


def test_same_identity_reclaim_fences_the_old_generation(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec1 = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        g1 = rec1.lease_generation

        clock.advance(200.0)  # worker's lease lapses
        s.recover("job-1", OPERATOR, "worker abandoned the job")
        assert s.load("job-1").state is JobState.READY

        # Same worker re-claims; the fresh generation supersedes the old token.
        rec2 = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        assert rec2.lease_generation == g1 + 2  # recover +1, claim +1

        # The old generation is fenced on both renew and mutation.
        with pytest.raises(LeaseExpired):
            s.renew("job-1", WORKER, g1)
        with pytest.raises(LeaseExpired):
            s.transition(
                "job-1", JobEvent.START,
                actor=WORKER, budget_check=BudgetCheck.approved(),
                generation=g1,
            )
        # The fresh generation works.
        running = s.transition(
            "job-1", JobEvent.START,
            actor=WORKER, budget_check=BudgetCheck.approved(),
            generation=rec2.lease_generation,
        )
        assert running.state is JobState.RUNNING


# --------------------------------------------------------------------------
# Operator recovery
# --------------------------------------------------------------------------

def test_recover_clears_owner_lease_and_bumps_generation(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        clock.advance(200.0)  # expired, so no force needed
        ready = s.recover("job-1", OPERATOR, "abandoned")
        assert ready.state is JobState.READY
        assert ready.owner is None
        rec_after = s.get("job-1")
        assert rec_after.lease_expires_at is None
        assert rec_after.lease_generation == rec.lease_generation + 1


def test_recover_of_live_lease_requires_force(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        # Lease still live: recovery demands explicit operator authorization.
        with pytest.raises(AuthorizationRefused, match="force"):
            s.recover("job-1", OPERATOR, "premature")
        ready = s.recover("job-1", OPERATOR, "overriding a healthy lease", force=True)
        assert ready.state is JobState.READY


def test_recover_requires_operator_role(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with pytest.raises(AuthorizationRefused):
        store.recover("job-1", WORKER, "not an operator")


def test_recover_requires_a_reason(store):
    store.save(_job())
    store.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="reason"):
            store.recover("job-1", OPERATOR, bad)


def test_recover_writes_actor_and_reason_to_history(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        clock.advance(200.0)
        s.recover("job-1", OPERATOR, "worker stalled, no heartbeat", )
        rows = _history(s, "job-1")
        recover = rows[-1]
        assert recover["event"] == "recover"
        assert recover["actor_identity"] == "operator"
        assert recover["actor_role"] == "operator"
        assert recover["reason"] == "worker stalled, no heartbeat"
        assert recover["owner"] is None  # owner cleared


def test_fail_from_ready_requires_operator(store):
    store.save(_job())
    with pytest.raises(AuthorizationRefused):
        store.transition("job-1", JobEvent.FAIL, actor=WORKER)
    failed = store.transition("job-1", JobEvent.FAIL, actor=OPERATOR)
    assert failed.state is JobState.FAILED


# --------------------------------------------------------------------------
# Lease clearing: leaving an owned state drops the lease atomically
# --------------------------------------------------------------------------

def test_release_clears_lease(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        assert s.get("job-1").lease_expires_at is not None

        s.transition("job-1", JobEvent.RELEASE, actor=WORKER, generation=rec.lease_generation)

        after = s.get("job-1")
        assert after.job.state is JobState.READY
        assert after.job.owner is None
        assert after.lease_expires_at is None  # lease cleared
        assert after.lease_generation == rec.lease_generation  # fence lineage kept


def test_complete_clears_lease(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        s.transition(
            "job-1", JobEvent.START,
            actor=WORKER, budget_check=BudgetCheck.approved(),
            generation=rec.lease_generation,
        )
        assert s.get("job-1").lease_expires_at is not None  # still leased while running

        s.transition("job-1", JobEvent.COMPLETE, actor=WORKER, generation=rec.lease_generation)

        after = s.get("job-1")
        assert after.job.state is JobState.DONE
        assert after.lease_expires_at is None  # terminal -> unleased


def test_fail_clears_lease(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        assert s.get("job-1").lease_expires_at is not None

        s.transition("job-1", JobEvent.FAIL, actor=WORKER, generation=rec.lease_generation)

        after = s.get("job-1")
        assert after.job.state is JobState.FAILED
        assert after.lease_expires_at is None  # terminal -> unleased


def test_start_keeps_the_lease(path):
    clock = _SteppedClock()
    with SqliteJobStore(path, clock=clock, lease_ttl=100.0) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        original_expiry = rec.lease_expires_at
        s.transition(
            "job-1", JobEvent.START,
            actor=WORKER, budget_check=BudgetCheck.approved(),
            generation=rec.lease_generation,
        )
        # A still-owned transition must not disturb the lease.
        assert s.get("job-1").lease_expires_at == original_expiry


# --------------------------------------------------------------------------
# Restart-safe persistence and recovery
# --------------------------------------------------------------------------

def test_state_survives_close_and_reopen(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        s.transition(
            "job-1", JobEvent.START,
            actor=WORKER, budget_check=BudgetCheck.approved(),
            generation=rec.lease_generation,
        )

    with SqliteJobStore(path) as s:
        assert s.load("job-1").state is JobState.RUNNING
        assert s.load("job-1").owner == WORKER.identity


def test_reopen_does_not_silently_complete_a_job(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())

    with SqliteJobStore(path) as s:
        job = s.load("job-1")
        assert job.state is JobState.CLAIMED  # not done, not failed
        assert not job.terminal


def test_crash_recovery_release_then_reclaim(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        g1 = rec.lease_generation
        # "crash" -- the claim was taken but never started.

    with SqliteJobStore(path) as s:
        ready = s.transition("job-1", JobEvent.RELEASE, actor=WORKER, generation=g1)
        assert ready.state is JobState.READY
        assert ready.owner is None
        reclaimed = s.claim("job-1", OTHER, budget_check=BudgetCheck.approved())
        assert reclaimed.job.owner == OTHER.identity
        assert reclaimed.job.state is JobState.CLAIMED


def test_history_survives_restart(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    with SqliteJobStore(path) as s:
        assert [r["event"] for r in _history(s, "job-1")] == ["register", "claim"]


# --------------------------------------------------------------------------
# Schema versioning and migrations
# --------------------------------------------------------------------------

def test_schema_version_is_current_after_open(path):
    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION


def test_migrations_are_idempotent_across_reopens(path):
    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION
    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION
    with SqliteDedupeStore(path) as s:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_both_stores_apply_the_same_schema(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
    with SqliteDedupeStore(path) as dedupe:
        assert dedupe.record("k", "done") is True
    with SqliteJobStore(path) as s:
        assert s.load("job-1") is not None


def test_v1_database_upgrades_additively_and_owned_rows_are_expired(tmp_path):
    # Build a v1 database by applying only the first migration, with an owned
    # row that predates leases (FORGE-12's R3 case).
    db = str(tmp_path / "v1.db")
    conn = sqlite3.connect(db)
    for statement in MIGRATIONS[0][1]:
        conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {MIGRATIONS[0][0]}")
    conn.execute(
        "INSERT INTO jobs (id, ticket_ref, repo, state, allowed_tools, budget, "
        "owner, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("job-1", "OUT-123", "outpost", "claimed", "[]", 10.0, "alice", 0.0, 0.0, 0),
    )
    conn.commit()
    conn.close()

    with SqliteJobStore(db) as s:
        assert s.schema_version == SCHEMA_VERSION
        rec = s.get("job-1")
        assert rec.lease_expires_at is None  # legacy row has no lease
        assert rec.lease_generation == 0

        # Fail closed: the owned-but-unleased row cannot be driven by a worker.
        with pytest.raises(LeaseExpired):
            s.transition(
                "job-1", JobEvent.START,
                actor=WORKER, budget_check=BudgetCheck.approved(),
                generation=0,
            )
        # The operator can recover it.
        ready = s.recover("job-1", OPERATOR, "legacy v1 row recovery")
        assert ready.state is JobState.READY


# --------------------------------------------------------------------------
# Durable deduplication
# --------------------------------------------------------------------------

def test_dedupe_records_durably(path):
    with SqliteDedupeStore(path) as d:
        assert d.seen("k") is None
        assert d.record("k", "done") is True
        assert d.seen("k") == "done"
        assert d.record("k", "done") is False


def test_dedupe_survives_reopen(path):
    with SqliteDedupeStore(path) as d:
        d.record("k", "done")
    with SqliteDedupeStore(path) as d:
        assert d.seen("k") == "done"
        assert d.record("k", "done") is False


def test_dedupe_is_an_implementation_of_the_interface():
    assert issubclass(SqliteDedupeStore, DedupeStore)


# --------------------------------------------------------------------------
# Exceptions are a coherent family
# --------------------------------------------------------------------------

def test_store_errors_are_a_coherent_family():
    for exc in (JobNotFound, JobAlreadyExists, ConcurrencyConflict, MigrationError, LeaseExpired):
        assert issubclass(exc, JobStoreError)
    # Transition refusals stay in the value-level family, untouched.
    assert issubclass(ClaimConflict, JobTransitionError)
    assert issubclass(BudgetRefused, JobTransitionError)
    assert issubclass(InvalidTransition, JobTransitionError)
    assert issubclass(AuthorizationRefused, JobTransitionError)


# --------------------------------------------------------------------------
# Structural: the durable layer enables no unattended execution
# --------------------------------------------------------------------------

def test_store_imports_no_process_network_or_credential_modules():
    import pathlib

    import forge.jobs.store

    text = pathlib.Path(forge.jobs.store.__file__).read_text(encoding="utf-8")
    forbidden = (
        "import subprocess",
        "import socket",
        "import urllib",
        "import requests",
        "import http",
        "os.environ",
        "from os import environ",
    )
    for token in forbidden:
        assert token not in text, f"store.py imports or reads {token!r}"


# --------------------------------------------------------------------------
# Cross-process: simultaneous init, concurrent claims, recovery fencing
# --------------------------------------------------------------------------

def test_simultaneous_first_open_initializes_once(path, tmp_path):
    go_file = str(tmp_path / "go")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _FIRST_OPEN_WORKER, path, go_file],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(6)
    ]
    open(go_file, "w").close()
    for p in procs:
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err.decode()
        assert out.decode().strip() == str(SCHEMA_VERSION)

    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION


def test_concurrent_claims_across_os_processes(path, tmp_path):
    starter = SqliteJobStore(path)
    starter.save(_job())
    starter.close()

    go_file = str(tmp_path / "go")
    owners = (WORKER.identity, OTHER.identity)
    result_files = [str(tmp_path / f"result-{i}") for i in range(len(owners))]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CLAIM_WORKER, path, owner, go_file, rf],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for owner, rf in zip(owners, result_files)
    ]
    open(go_file, "w").close()
    for p in procs:
        _, err = p.communicate(timeout=30)
        assert p.returncode == 0, err.decode()

    results = [pathlib.Path(rf).read_text() for rf in result_files]
    winners = [r for r in results if r.startswith("ok:")]
    refusals = [r for r in results if not r.startswith("ok:")]
    assert len(winners) == 1, results
    assert refusals == ["InvalidTransition"], results

    winner = winners[0].split(":", 1)[1]
    with SqliteJobStore(path) as s:
        job = s.load("job-1")
        assert job.state is JobState.CLAIMED
        assert job.owner == winner


def test_recovery_fences_generation_across_processes(path, tmp_path):
    # The critical invariant, not a naive "exactly one wins" race: regardless of
    # how a concurrent renew and recovery interleave (the SQLite writer lock
    # serialises them), the recovery atomically bumps the generation and a
    # stale-generation renew is refused afterward.
    with SqliteJobStore(path) as s:
        s.save(_job())
        rec = s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
    g1 = rec.lease_generation

    go_file = str(tmp_path / "go")
    renew_file = str(tmp_path / "renew.txt")
    recover_file = str(tmp_path / "recover.txt")
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RENEW_WORKER, path, go_file, str(g1), renew_file],
            cwd=ROOT,
        ),
        subprocess.Popen(
            [sys.executable, "-c", _RECOVER_WORKER, path, go_file, recover_file],
            cwd=ROOT,
        ),
    ]
    open(go_file, "w").close()
    for p in procs:
        p.wait(timeout=30)

    # Recovery must have succeeded and atomically bumped the generation.
    assert pathlib.Path(recover_file).read_text() == "ok"
    # The renew may legitimately succeed (ordered before recovery) or be refused
    # (ordered after); neither ordering is wrong, so we assert only the fence.
    assert pathlib.Path(renew_file).read_text() in ("ok", "LeaseExpired", "ClaimConflict")

    with SqliteJobStore(path) as s:
        assert s.load("job-1").state is JobState.READY
        assert s.get("job-1").lease_generation == g1 + 1
        # Re-claim with the same identity mints a fresh generation; the old
        # token presented by the pre-recovery worker is now fenced.
        s.claim("job-1", WORKER, budget_check=BudgetCheck.approved())
        with pytest.raises(LeaseExpired):
            s.renew("job-1", WORKER, g1)


# --------------------------------------------------------------------------
# Reject a schema newer than this implementation supports
# --------------------------------------------------------------------------

def test_newer_schema_version_is_rejected(path):
    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION

    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="newer"):
        SqliteJobStore(path)