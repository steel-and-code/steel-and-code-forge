"""Tests for the durable SQLite-backed job store (FORGE-11).

Covers the six requirements: persistence of every field, schema versioning and
migrations, atomic claiming and conflict detection, restart-safe recovery, the
fail-closed ownership/budget refusals, and the transactional pairing of a state
change with its history record.

Concurrency is exercised with two independent ``SqliteJobStore`` connections to
one file (the faithful model of two processes), synchronised with a barrier so
the claims genuinely race rather than merely run in sequence.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

from forge.jobs.idempotency import DedupeStore
from forge.jobs.store import (
    SCHEMA_VERSION,
    ConcurrencyConflict,
    JobAlreadyExists,
    JobNotFound,
    JobRecord,
    JobStoreError,
    MigrationError,
    SqliteDedupeStore,
    SqliteJobStore,
)
from forge.jobs.state import (
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

OWNER = "alice"
OTHER = "bob"

#: Repo root, used to make ``forge`` importable in the subprocess workers.
ROOT = str(pathlib.Path(__file__).resolve().parent.parent)

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
from forge.jobs.state import BudgetCheck
s = SqliteJobStore(sys.argv[1])
try:
    s.claim("job-1", sys.argv[2], budget_check=BudgetCheck.approved())
    open(result_file, "w").write("ok:" + sys.argv[2])
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
            "SELECT event, from_state, to_state, owner, outcome "
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
    event, from_state, to_state, owner, outcome = rows[0]
    assert event == "register"
    assert from_state is None
    assert to_state == "ready"
    assert owner is None
    assert outcome == "ok"


# --------------------------------------------------------------------------
# Claiming: atomic, owner-setting, fail-closed budget
# --------------------------------------------------------------------------


def test_claim_sets_owner_and_persists(store):
    store.save(_job())
    claimed = store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    assert claimed.state is JobState.CLAIMED
    assert claimed.owner == OWNER
    assert store.load("job-1") == claimed


def test_claim_without_budget_check_fails_closed(store):
    store.save(_job())
    with pytest.raises(BudgetRefused):
        store.claim("job-1", OWNER)
    # Nothing changed: still ready and unowned.
    job = store.load("job-1")
    assert job.state is JobState.READY
    assert job.owner is None


def test_claim_with_exhausted_budget_fails_closed_and_leaves_row_unchanged(store):
    store.save(_job())
    before = store.get("job-1")
    check = BudgetCheck(False, True, detail="job budget exhausted")
    with pytest.raises(BudgetRefused):
        store.claim("job-1", OWNER, budget_check=check)
    after = store.get("job-1")
    assert after.job.state is JobState.READY
    assert after.job.owner is None
    assert after.updated_at == before.updated_at  # no partial state write


def test_claim_on_missing_job_raises(store):
    with pytest.raises(JobNotFound):
        store.claim("absent", OWNER, budget_check=BudgetCheck.approved())


def test_second_claim_of_an_owned_job_is_refused(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    with pytest.raises(JobTransitionError):
        store.claim("job-1", OTHER, budget_check=BudgetCheck.approved())


def test_claim_requires_a_job_event(store):
    with pytest.raises(TypeError):
        store.transition("job-1", "claim")


# --------------------------------------------------------------------------
# Ownership: start/release are gated on the claim holder
# --------------------------------------------------------------------------


def test_start_with_wrong_owner_is_refused(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict, match="does not match"):
        store.transition(
            "job-1", JobEvent.START,
            owner=OTHER, budget_check=BudgetCheck.approved(),
        )
    assert store.load("job-1").state is JobState.CLAIMED


def test_release_requires_the_claim_owner(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict):
        store.transition("job-1", JobEvent.RELEASE, owner=OTHER)


def test_full_lifecycle_through_the_store(store):
    store.save(_job())
    claimed = store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    running = store.transition(
        "job-1", JobEvent.START,
        owner=OWNER, budget_check=BudgetCheck.approved(),
    )
    assert running.state is JobState.RUNNING
    done = store.transition("job-1", JobEvent.COMPLETE)
    assert done.state is JobState.DONE
    assert store.load("job-1").state is JobState.DONE


# --------------------------------------------------------------------------
# Transactional state + history pairing
# --------------------------------------------------------------------------


def test_transitions_append_one_history_row_each(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    store.transition(
        "job-1", JobEvent.START, owner=OWNER, budget_check=BudgetCheck.approved()
    )
    events = [row["event"] for row in _history(store, "job-1")]
    assert events == ["register", "claim", "start"]


def test_successful_transition_records_from_to_and_outcome(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    rows = _history(store, "job-1")
    claim = rows[1]
    assert claim["from_state"] == "ready"
    assert claim["to_state"] == "claimed"
    assert claim["owner"] == OWNER
    assert claim["outcome"] == "ok"


def test_refused_transition_is_audited_with_unchanged_states(store):
    store.save(_job())
    store.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
    with pytest.raises(ClaimConflict):
        store.transition(
            "job-1", JobEvent.START,
            owner=OTHER, budget_check=BudgetCheck.approved(),
        )
    rows = _history(store, "job-1")
    refusal = rows[-1]
    assert refusal["event"] == "start"
    assert refusal["from_state"] == "claimed"
    assert refusal["to_state"] == "claimed"  # unchanged
    assert refusal["owner"] == OTHER  # the actor who was refused
    assert refusal["outcome"] == "ClaimConflict"
    # The stored job did not move.
    assert store.load("job-1").state is JobState.CLAIMED


def test_refused_claim_does_not_leak_a_history_success(store):
    store.save(_job())
    with pytest.raises(BudgetRefused):
        store.transition("job-1", JobEvent.CLAIM, owner=OWNER, budget_check=None)
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

    def claimer(name: str) -> None:
        s = SqliteJobStore(path)
        try:
            barrier.wait()
            s.claim("job-1", name, budget_check=BudgetCheck.approved())
            results[name] = "ok"
        except JobTransitionError as exc:
            results[name] = type(exc).__name__
        finally:
            s.close()

    threads = [
        threading.Thread(target=claimer, args=(OWNER,)),
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
    assert stored.owner in (OWNER, OTHER)
    assert results[stored.owner] == "ok"


# --------------------------------------------------------------------------
# Restart-safe persistence and recovery
# --------------------------------------------------------------------------


def test_state_survives_close_and_reopen(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
        s.transition(
            "job-1", JobEvent.START,
            owner=OWNER, budget_check=BudgetCheck.approved(),
        )

    with SqliteJobStore(path) as s:
        assert s.load("job-1").state is JobState.RUNNING
        assert s.load("job-1").owner == OWNER


def test_reopen_does_not_silently_complete_a_job(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", OWNER, budget_check=BudgetCheck.approved())

    with SqliteJobStore(path) as s:
        job = s.load("job-1")
        assert job.state is JobState.CLAIMED  # not done, not failed
        assert not job.terminal


def test_crash_recovery_release_then_reclaim(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
        # "crash" -- the claim was taken but never started.

    with SqliteJobStore(path) as s:
        ready = s.transition("job-1", JobEvent.RELEASE, owner=OWNER)
        assert ready.state is JobState.READY
        assert ready.owner is None
        reclaimed = s.claim("job-1", OTHER, budget_check=BudgetCheck.approved())
        assert reclaimed.owner == OTHER
        assert reclaimed.state is JobState.CLAIMED


def test_history_survives_restart(path):
    with SqliteJobStore(path) as s:
        s.save(_job())
        s.claim("job-1", OWNER, budget_check=BudgetCheck.approved())
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
    for exc in (JobNotFound, JobAlreadyExists, ConcurrencyConflict, MigrationError):
        assert issubclass(exc, JobStoreError)
    # Transition refusals stay in the value-level family, untouched.
    assert issubclass(ClaimConflict, JobTransitionError)
    assert issubclass(BudgetRefused, JobTransitionError)
    assert issubclass(InvalidTransition, JobTransitionError)


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
# Cross-process: simultaneous init and concurrent claims (OS processes)
# --------------------------------------------------------------------------


def test_simultaneous_first_open_initializes_once(path, tmp_path):
    # Six OS processes open a brand-new database at the same time. Exactly one
    # migration must win; the rest must observe the already-applied schema and
    # open cleanly -- none may raise on a duplicate table.
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
    owners = (OWNER, OTHER)
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


# --------------------------------------------------------------------------
# Reject a schema newer than this implementation supports
# --------------------------------------------------------------------------


def test_newer_schema_version_is_rejected(path):
    with SqliteJobStore(path) as s:
        assert s.schema_version == SCHEMA_VERSION

    # Simulate a database produced by a future Forge.
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="newer"):
        SqliteJobStore(path)