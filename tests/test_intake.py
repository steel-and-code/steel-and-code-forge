"""Tests for the durable Jira intake store (FORGE-13).

Covers the two-operation split -- delivery receipt (dedupe/audit, never creates
work) and authorized job admission (idempotent per request, one open job per
ticket, one job per work cycle) -- plus conflict detection, reordered-input
fingerprinting, denied-admission auditing, crash/restart recovery, and
cross-process races.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys
import threading

import pytest

from forge.jobs.intake import AdmissionOutcome, SqliteIntakeStore, job_id_for
from forge.jobs.state import Actor, ActorRole, AuthorizationRefused, JobEvent
from forge.jobs.store import SqliteJobStore

WORKER = Actor("alice")
OPERATOR = Actor("operator", ActorRole.OPERATOR)

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)


class _SteppedClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "jobs.db")


@pytest.fixture
def store(path):
    s = SqliteIntakeStore(path)
    yield s
    s.close()


def _admit_kwargs(**overrides):
    kwargs = dict(
        repo="outpost",
        allowed_tools=("read_file", "write_file"),
        budget=10.0,
        allowed_repos=("outpost",),
    )
    kwargs.update(overrides)
    return kwargs


def _delivery(store, issue_key="OUT-1", *, delivery_id=None, arguments=None):
    if arguments is None:
        arguments = {"summary": "do a thing"}
    return store.record_delivery(
        delivery_id=delivery_id,
        issue_key=issue_key,
        event_type="issue_created",
        arguments=arguments,
    )


# --------------------------------------------------------------------------
# Delivery receipt: dedupe by content, audit every attempt
# --------------------------------------------------------------------------

def test_delivery_accepts_once_then_duplicate(store):
    first = _delivery(store, arguments={"n": 1})
    assert first.outcome.value == "accepted"
    assert first.content_key is not None
    assert first.job_id is None  # no job exists yet

    second = _delivery(store, arguments={"n": 1})
    assert second.outcome.value == "duplicate"
    assert second.content_key == first.content_key


def test_same_delivery_id_repeated_is_duplicate_and_both_audited(store):
    _delivery(store, delivery_id="d1", arguments={"n": 1})
    _delivery(store, delivery_id="d1", arguments={"n": 1})

    rows = store._conn.execute(
        "SELECT delivery_id, outcome FROM intake_deliveries ORDER BY id"
    ).fetchall()
    assert [r["outcome"] for r in rows] == ["accepted", "duplicate"]
    assert all(r["delivery_id"] == "d1" for r in rows)


def test_same_delivery_id_different_content_is_conflict(store):
    assert _delivery(store, delivery_id="d1", arguments={"n": 1}).outcome.value == "accepted"
    result = _delivery(store, delivery_id="d1", arguments={"n": 2})
    assert result.outcome.value == "conflict"
    assert result.reason is not None


def test_new_delivery_id_with_seen_content_binds_then_conflicts(store):
    _delivery(store, delivery_id="d1", arguments={"n": 1})  # accepted

    # Different delivery id, same content: duplicate, but the new id must bind.
    result = _delivery(store, delivery_id="d2", arguments={"n": 1})
    assert result.outcome.value == "duplicate"
    bound = store._conn.execute(
        "SELECT content_key FROM delivery_ids WHERE delivery_id = 'd2'"
    ).fetchone()
    assert bound is not None  # invariant 2: the association was persisted

    # Now a conflicting replay of d2 is detectable.
    conflict = _delivery(store, delivery_id="d2", arguments={"n": 2})
    assert conflict.outcome.value == "conflict"


def test_no_delivery_id_falls_back_to_content_dedupe(store):
    assert _delivery(store, arguments={"n": 1}).outcome.value == "accepted"
    assert _delivery(store, arguments={"n": 1}).outcome.value == "duplicate"
    assert _delivery(store, arguments={"n": 2}).outcome.value == "accepted"


def test_uncanonicalizable_payload_is_invalid_with_null_content_key(store):
    result = _delivery(store, arguments={"payload": {1, 2, 3}})  # a set: not JSON
    assert result.outcome.value == "invalid"
    assert result.content_key is None
    assert result.reason is not None

    row = store._conn.execute(
        "SELECT content_key, outcome, reason FROM intake_deliveries"
    ).fetchone()
    assert row["content_key"] is None  # invariant 3
    assert row["outcome"] == "invalid"
    # Nothing else was written.
    assert store._conn.execute("SELECT COUNT(*) AS c FROM intake_events").fetchone()["c"] == 0
    assert store._conn.execute("SELECT COUNT(*) AS c FROM delivery_ids").fetchone()["c"] == 0
    assert store._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 0


def test_delivery_does_not_create_a_job(store):
    _delivery(store, arguments={"n": 1})
    assert store._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 0


# --------------------------------------------------------------------------
# Admission: idempotent, one job per cycle, denied requests audited
# --------------------------------------------------------------------------

def test_admit_creates_then_duplicates(store):
    first = store.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
    assert first.outcome is AdmissionOutcome.CREATED
    assert first.job_id == job_id_for("OUT-1", 1)
    assert first.cycle == 1

    again = store.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
    assert again.outcome is AdmissionOutcome.DUPLICATE
    assert again.job_id == first.job_id
    assert again.cycle == 1


def test_admission_request_id_reused_with_changed_inputs_is_conflict(store):
    store.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
    changed = _admit_kwargs(allowed_tools=("read_file", "write_file", "exec_command"))
    result = store.admit_job("req-1", "OUT-1", OPERATOR, **changed)
    assert result.outcome is AdmissionOutcome.CONFLICT
    denial = store._conn.execute(
        "SELECT reason FROM admission_denials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert denial["reason"] == "admission_request_id reused with changed inputs"
    assert store._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 1


def test_equivalent_reordered_inputs_are_duplicate_not_conflict(store):
    a = _admit_kwargs(allowed_tools=("read_file", "write_file"))
    b = _admit_kwargs(allowed_tools=("write_file", "read_file"))
    assert store.admit_job("req-1", "OUT-1", OPERATOR, **a).outcome is AdmissionOutcome.CREATED
    assert store.admit_job("req-1", "OUT-1", OPERATOR, **b).outcome is AdmissionOutcome.DUPLICATE


def test_generator_inputs_are_materialized_once(store):
    result = store.admit_job(
        "req-1", "OUT-1", OPERATOR,
        repo="outpost",
        allowed_tools=(x for x in ("read_file", "write_file")),
        budget=10.0,
        allowed_repos=(r for r in ("outpost",)),
    )
    assert result.outcome is AdmissionOutcome.CREATED
    # The generator was not exhausted by the fingerprint: the full set survives.
    row = store._conn.execute(
        "SELECT allowed_tools FROM jobs WHERE id = 'jira:OUT-1:1'"
    ).fetchone()
    assert json.loads(row["allowed_tools"]) == ["read_file", "write_file"]


def test_generator_inputs_reordered_equivalent_are_duplicate(store):
    a = store.admit_job(
        "req-1", "OUT-1", OPERATOR,
        repo="outpost",
        allowed_tools=(x for x in ("read_file", "write_file")),
        budget=10.0,
        allowed_repos=(r for r in ("outpost",)),
    )
    assert a.outcome is AdmissionOutcome.CREATED
    b = store.admit_job(
        "req-1", "OUT-1", OPERATOR,
        repo="outpost",
        allowed_tools=(x for x in ("write_file", "read_file")),
        budget=10.0,
        allowed_repos=(r for r in ("outpost",)),
    )
    assert b.outcome is AdmissionOutcome.DUPLICATE


def test_distinct_request_while_open_is_open_exists_not_bound(store):
    store.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
    result = store.admit_job("req-2", "OUT-1", OPERATOR, **_admit_kwargs())
    assert result.outcome is AdmissionOutcome.OPEN_EXISTS
    assert result.job_id == job_id_for("OUT-1", 1)  # reference for visibility
    # The new request was NOT bound to the open job.
    assert store._conn.execute(
        "SELECT 1 FROM admissions WHERE admission_request_id = 'req-2'"
    ).fetchone() is None
    assert store._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 1


def test_non_operator_admission_is_unauthorized_and_audited(store):
    with pytest.raises(AuthorizationRefused):
        store.admit_job("req-1", "OUT-1", WORKER, **_admit_kwargs())
    denial = store._conn.execute(
        "SELECT actor_identity, actor_role, reason, admission_request_id "
        "FROM admission_denials"
    ).fetchone()
    assert denial["actor_identity"] == "alice"
    assert denial["actor_role"] == "worker"
    assert denial["reason"] == "unauthorized admission"
    assert denial["admission_request_id"] == "req-1"
    assert store._conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"] == 0


def test_terminal_job_then_same_request_duplicates_then_new_cycle_creates(path):
    clock = _SteppedClock()
    with SqliteIntakeStore(path, clock=clock) as intake:
        intake.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())

        # Drive the admitted job to a terminal state through the real path.
        with SqliteJobStore(path) as jobs:
            jobs.transition("jira:OUT-1:1", JobEvent.FAIL, actor=OPERATOR)

        # Same request still resolves to the ORIGINAL job, even though terminal.
        again = intake.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
        assert again.outcome is AdmissionOutcome.DUPLICATE
        assert again.job_id == "jira:OUT-1:1"

        # A distinct authorized request mints the next cycle.
        fresh = intake.admit_job("req-2", "OUT-1", OPERATOR, **_admit_kwargs())
        assert fresh.outcome is AdmissionOutcome.CREATED
        assert fresh.cycle == 2
        assert fresh.job_id == "jira:OUT-1:2"


# --------------------------------------------------------------------------
# Delivery -> open job resolution
# --------------------------------------------------------------------------

def test_delivery_resolves_to_the_open_job(store):
    store.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())
    result = _delivery(store, arguments={"n": 1})
    assert result.outcome.value == "accepted"
    assert result.job_id == "jira:OUT-1:1"


# --------------------------------------------------------------------------
# Concurrency across connections
# --------------------------------------------------------------------------

def test_concurrent_deliveries_yield_one_accepted(path):
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def deliver(name: str) -> None:
        s = SqliteIntakeStore(path)
        try:
            barrier.wait()
            r = _delivery(s, arguments={"n": 1})
            results[name] = r.outcome.value
        finally:
            s.close()

    threads = [threading.Thread(target=deliver, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["accepted", "duplicate"], results


def test_concurrent_admissions_yield_one_created(path):
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def admit(name: str) -> None:
        s = SqliteIntakeStore(path)
        try:
            barrier.wait()
            r = s.admit_job(f"req-{name}", "OUT-1", OPERATOR, **_admit_kwargs())
            results[name] = r.outcome.value
        finally:
            s.close()

    threads = [threading.Thread(target=admit, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == ["created", "open_exists"], results


# --------------------------------------------------------------------------
# Restart / cross-process durability
# --------------------------------------------------------------------------

def test_dedupe_survives_reopen(path):
    with SqliteIntakeStore(path) as s:
        _delivery(s, arguments={"n": 1})
        s.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())

    with SqliteIntakeStore(path) as s:
        assert _delivery(s, arguments={"n": 1}).outcome.value == "duplicate"
        assert (
            s.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs()).outcome
            is AdmissionOutcome.DUPLICATE
        )


_REDELIVER_WORKER = r"""
import os, sys, time
go_file = sys.argv[1]
while not os.path.exists(go_file):
    time.sleep(0.001)
from forge.jobs.intake import SqliteIntakeStore
from forge.jobs.state import Actor, ActorRole
s = SqliteIntakeStore(sys.argv[2])
r = s.record_delivery(issue_key="OUT-1", event_type="issue_created", arguments={"n": 1})
a = s.admit_job("req-1", "OUT-1", Actor("operator", ActorRole.OPERATOR),
                repo="outpost", allowed_tools=("read_file", "write_file"),
                budget=10.0, allowed_repos=("outpost",))
print(r.outcome.value, a.outcome.value)
s.close()
"""


def test_dedupe_across_process_restart(path, tmp_path):
    with SqliteIntakeStore(path) as s:
        _delivery(s, arguments={"n": 1})
        s.admit_job("req-1", "OUT-1", OPERATOR, **_admit_kwargs())

    go_file = str(tmp_path / "go")
    p = subprocess.Popen(
        [sys.executable, "-c", _REDELIVER_WORKER, go_file, path],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    open(go_file, "w").close()
    out, err = p.communicate(timeout=30)
    assert p.returncode == 0, err.decode()
    assert out.decode().strip() == "duplicate duplicate"


# --------------------------------------------------------------------------
# Schema invariants
# --------------------------------------------------------------------------

def test_ticket_jobs_has_no_state_column(path):
    with SqliteIntakeStore(path) as s:
        cols = {r[1] for r in s._conn.execute("PRAGMA table_info(ticket_jobs)")}
    assert "state" not in cols


def test_intake_imports_no_process_network_or_credential_modules():
    import forge.jobs.intake

    text = pathlib.Path(forge.jobs.intake.__file__).read_text(encoding="utf-8")
    for token in (
        "import subprocess",
        "import socket",
        "import urllib",
        "import requests",
        "import http",
        "os.environ",
        "from os import environ",
    ):
        assert token not in text, f"intake.py imports or reads {token!r}"


def test_schema_version_is_current(path):
    with SqliteIntakeStore(path) as s:
        from forge.jobs.store import SCHEMA_VERSION

        assert s.schema_version == SCHEMA_VERSION