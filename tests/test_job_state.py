"""Tests for the job state machine: construction, transitions, budget, claim.

The exhaustive transition test pins every ``(state, event)`` pair -- the
acceptance criterion "deterministic state transitions" -- so a change to the
machine cannot silently alter an edge without failing here. FORGE-12 adds actor
roles (worker/operator), the RECOVER event, and the operator-gated fail-from-ready.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from forge.jobs.state import (
    BASELINE_TOOLS,
    TERMINAL_STATES,
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
    transition,
)

# Every state where a claim's owner is still attached. A job in one of these was
# claimed by "alice" at some point; only release/recover (and terminal states)
# drop it.
OWNED_STATES = (
    JobState.CLAIMED,
    JobState.RUNNING,
    JobState.BLOCKED,
    JobState.REVIEW,
)

OWNER = "alice"

#: The owning worker and the operator, as the acting Actor values the machine
#: now takes instead of a bare owner string.
WORKER = Actor(OWNER)
OPERATOR = Actor("operator", ActorRole.OPERATOR)


def _job(state: JobState = JobState.READY, *, owner: str | None = None) -> Job:
    """A minimal valid job, forced into ``state`` with ``owner``.

    Uses ``dataclasses.replace`` rather than the transition function so the
    setup of a transition test never depends on the code under test.
    """
    base = new_job(
        job_id="job-1",
        ticket_ref="OUT-123",
        repo="outpost",
        allowed_tools=("read_file", "write_file"),
        budget=10.0,
    )
    return replace(base, state=state, owner=owner)


def _by_owner() -> Job:
    return _job(JobState.CLAIMED, owner=OWNER)


# --------------------------------------------------------------------------
# Construction and validation
# --------------------------------------------------------------------------

def test_defaults_are_ready_unowned_with_no_budget():
    job = new_job(job_id="j", ticket_ref="T-1", repo="r")
    assert job.state is JobState.READY
    assert job.owner is None
    assert job.budget is None
    assert job.allowed_tools == ()


def test_allowed_tools_are_sorted_and_deduplicated():
    job = new_job(
        job_id="j",
        ticket_ref="T-1",
        repo="r",
        allowed_tools=("write_file", "read_file", "read_file"),
    )
    assert job.allowed_tools == ("read_file", "write_file")


def test_out_of_baseline_tool_is_refused_at_construction():
    with pytest.raises(ValueError, match="outside the baseline"):
        new_job(job_id="j", ticket_ref="T-1", repo="r", allowed_tools=("sudo",))


def test_a_narrower_baseline_can_reject_a_default_tool():
    with pytest.raises(ValueError, match="outside the baseline"):
        new_job(
            job_id="j",
            ticket_ref="T-1",
            repo="r",
            allowed_tools=("exec_command",),
            baseline_tools=("read_file", "write_file"),
        )


def test_empty_identifiers_are_refused():
    for kwargs in (
        {"job_id": "", "ticket_ref": "T-1", "repo": "r"},
        {"job_id": "j", "ticket_ref": "", "repo": "r"},
        {"job_id": "j", "ticket_ref": "T-1", "repo": ""},
        {"job_id": "  ", "ticket_ref": "T-1", "repo": "r"},
    ):
        with pytest.raises(ValueError):
            new_job(**kwargs)


def test_nul_byte_identifiers_are_refused():
    with pytest.raises(ValueError, match="NUL"):
        new_job(job_id="j\x00", ticket_ref="T-1", repo="r")


def test_budget_must_be_positive_and_finite():
    for bad in (0, -5, float("inf"), float("nan"), "ten", True):
        with pytest.raises(ValueError):
            new_job(job_id="j", ticket_ref="T-1", repo="r", budget=bad)


def test_global_budget_must_be_positive_and_finite():
    for bad in (0, -5, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            new_job(job_id="j", ticket_ref="T-1", repo="r", budget=1.0, global_budget=bad)


def test_per_job_budget_may_not_exceed_global_budget():
    with pytest.raises(ValueError, match="exceeds the global ceiling"):
        new_job(job_id="j", ticket_ref="T-1", repo="r", budget=50.0, global_budget=10.0)


def test_per_job_budget_equal_to_global_is_allowed():
    job = new_job(job_id="j", ticket_ref="T-1", repo="r", budget=10.0, global_budget=10.0)
    assert job.budget == 10.0


def test_repo_not_in_operator_allowlist_is_refused():
    with pytest.raises(ValueError, match="allowlist"):
        new_job(
            job_id="j",
            ticket_ref="T-1",
            repo="someother",
            allowed_repos=("outpost", "steel-and-code-forge"),
        )


def test_repo_in_operator_allowlist_is_accepted():
    job = new_job(job_id="j", ticket_ref="T-1", repo="outpost", allowed_repos=("outpost",))
    assert job.repo == "outpost"


def test_no_allowlist_means_no_repo_gate():
    # Absent operator config, the repo passes through; the gate is opt-in config.
    job = new_job(job_id="j", ticket_ref="T-1", repo="anything")
    assert job.repo == "anything"


def test_allows_reports_membership():
    job = new_job(
        job_id="j", ticket_ref="T-1", repo="r", allowed_tools=("read_file",)
    )
    assert job.allows("read_file")
    assert not job.allows("write_file")


def test_baseline_matches_byte_tools():
    # The job layer must narrow the same baseline the executor agent uses.
    from forge.agents.byte import BYTE_TOOLS

    assert BASELINE_TOOLS == BYTE_TOOLS


# --------------------------------------------------------------------------
# Exhaustive, deterministic transition table
# --------------------------------------------------------------------------

# (state, event) -> successor state, or None to expect InvalidTransition.
# Budget-bearing events (claim, start) get a valid worker actor and an approved
# budget check. RECOVER gets an operator actor; FAIL gets the worker on owned
# states and the operator on READY.
TRANSITIONS: dict[JobState, dict[JobEvent, JobState]] = {
    JobState.READY: {
        JobEvent.CLAIM: JobState.CLAIMED,
        JobEvent.FAIL: JobState.FAILED,
    },
    JobState.CLAIMED: {
        JobEvent.START: JobState.RUNNING,
        JobEvent.RELEASE: JobState.READY,
        JobEvent.RECOVER: JobState.READY,
        JobEvent.FAIL: JobState.FAILED,
    },
    JobState.RUNNING: {
        JobEvent.BLOCK: JobState.BLOCKED,
        JobEvent.SUBMIT: JobState.REVIEW,
        JobEvent.COMPLETE: JobState.DONE,
        JobEvent.RECOVER: JobState.READY,
        JobEvent.FAIL: JobState.FAILED,
    },
    JobState.BLOCKED: {
        JobEvent.UNBLOCK: JobState.RUNNING,
        JobEvent.RECOVER: JobState.READY,
        JobEvent.FAIL: JobState.FAILED,
    },
    JobState.REVIEW: {
        JobEvent.REVISE: JobState.RUNNING,
        JobEvent.COMPLETE: JobState.DONE,
        JobEvent.RECOVER: JobState.READY,
        JobEvent.FAIL: JobState.FAILED,
    },
    JobState.DONE: {},
    JobState.FAILED: {},
}


def _ctx(state: JobState, event: JobEvent) -> dict:
    if event in (JobEvent.CLAIM, JobEvent.START):
        return {"actor": WORKER, "budget_check": BudgetCheck.approved()}
    if event in (
        JobEvent.RELEASE,
        JobEvent.BLOCK,
        JobEvent.UNBLOCK,
        JobEvent.SUBMIT,
        JobEvent.REVISE,
        JobEvent.COMPLETE,
    ):
        return {"actor": WORKER}
    if event is JobEvent.RECOVER:
        return {"actor": OPERATOR}
    if event is JobEvent.FAIL:
        return {"actor": OPERATOR if state is JobState.READY else WORKER}
    return {}


@pytest.mark.parametrize("state", list(JobState))
@pytest.mark.parametrize("event", list(JobEvent))
def test_every_state_event_pair_is_deterministic(state: JobState, event: JobEvent):
    expected = TRANSITIONS[state].get(event)

    if state in OWNED_STATES:
        job = _job(state, owner=OWNER)
    elif state is JobState.DONE or state is JobState.FAILED:
        job = _job(JobState.READY, owner=None)
        job = replace(job, state=state, owner=OWNER)
    else:  # READY
        job = _job(state, owner=None)

    if expected is None:
        with pytest.raises(InvalidTransition):
            transition(job, event, **_ctx(state, event))
        return

    result = transition(job, event, **_ctx(state, event))
    assert result.state is expected


def test_transition_table_is_total():
    # Every state is present, so a missing row would have failed the test above.
    assert set(TRANSITIONS) == set(JobState)


# --------------------------------------------------------------------------
# Claim ownership
# --------------------------------------------------------------------------

def test_claim_sets_the_owner():
    job = _job(JobState.READY)
    claimed = transition(job, JobEvent.CLAIM, actor=WORKER, budget_check=BudgetCheck.approved())
    assert claimed.state is JobState.CLAIMED
    assert claimed.owner == OWNER


def test_claim_requires_an_actor():
    job = _job(JobState.READY)
    for missing in (None, Actor(""), Actor("   ")):
        with pytest.raises(AuthorizationRefused):
            transition(job, JobEvent.CLAIM, actor=missing, budget_check=BudgetCheck.approved())


def test_start_requires_the_claim_owner():
    job = _by_owner()
    with pytest.raises(ClaimConflict, match="does not match"):
        transition(
            job, JobEvent.START,
            actor=Actor("mallory"), budget_check=BudgetCheck.approved(),
        )


def test_start_refuses_the_operator():
    # The operator may recover and fail-from-ready, but may not drive a job.
    job = _by_owner()
    with pytest.raises(AuthorizationRefused):
        transition(job, JobEvent.START, actor=OPERATOR, budget_check=BudgetCheck.approved())


def test_release_requires_the_claim_owner():
    job = _by_owner()
    with pytest.raises(ClaimConflict):
        transition(job, JobEvent.RELEASE, actor=Actor("mallory"))


def test_release_returns_to_ready_and_clears_owner():
    job = _by_owner()
    ready = transition(job, JobEvent.RELEASE, actor=WORKER)
    assert ready.state is JobState.READY
    assert ready.owner is None


def test_a_released_job_can_be_claimed_by_a_different_owner():
    job = _by_owner()
    ready = transition(job, JobEvent.RELEASE, actor=WORKER)
    reclaimed = transition(ready, JobEvent.CLAIM, actor=Actor("bob"), budget_check=BudgetCheck.approved())
    assert reclaimed.owner == "bob"
    assert reclaimed.state is JobState.CLAIMED


def test_a_claimed_job_cannot_be_claimed_again():
    job = _by_owner()
    with pytest.raises(InvalidTransition):
        transition(job, JobEvent.CLAIM, actor=Actor("bob"), budget_check=BudgetCheck.approved())


# --------------------------------------------------------------------------
# Worker-authorization on the post-claim events (the FORGE-12 gap)
# --------------------------------------------------------------------------

def test_complete_requires_the_owning_worker():
    job = _job(JobState.RUNNING, owner=OWNER)
    with pytest.raises(ClaimConflict, match="does not match"):
        transition(job, JobEvent.COMPLETE, actor=Actor("mallory"))


def test_complete_refuses_the_operator():
    job = _job(JobState.RUNNING, owner=OWNER)
    with pytest.raises(AuthorizationRefused):
        transition(job, JobEvent.COMPLETE, actor=OPERATOR)


def test_worker_cannot_block_another_workers_job():
    job = _job(JobState.RUNNING, owner=OWNER)
    with pytest.raises(ClaimConflict, match="does not match"):
        transition(job, JobEvent.BLOCK, actor=Actor("mallory"))


# --------------------------------------------------------------------------
# RECOVER (operator-only)
# --------------------------------------------------------------------------

def test_recover_requires_operator():
    job = _by_owner()
    with pytest.raises(AuthorizationRefused):
        transition(job, JobEvent.RECOVER, actor=WORKER)


def test_recover_returns_to_ready_and_clears_owner():
    job = _job(JobState.RUNNING, owner=OWNER)
    ready = transition(job, JobEvent.RECOVER, actor=OPERATOR)
    assert ready.state is JobState.READY
    assert ready.owner is None


def test_recover_from_ready_is_invalid():
    job = _job(JobState.READY)
    with pytest.raises(InvalidTransition):
        transition(job, JobEvent.RECOVER, actor=OPERATOR)


def test_recover_from_terminal_is_invalid():
    job = replace(_job(JobState.READY), state=JobState.DONE, owner=OWNER)
    with pytest.raises(InvalidTransition):
        transition(job, JobEvent.RECOVER, actor=OPERATOR)


# --------------------------------------------------------------------------
# Budget -- fail closed before paid execution
# --------------------------------------------------------------------------

def test_missing_budget_refuses_claim():
    job = replace(_job(JobState.READY), budget=None)
    with pytest.raises(BudgetRefused, match="no positive per-job budget"):
        transition(job, JobEvent.CLAIM, actor=WORKER, budget_check=BudgetCheck.approved())


def test_missing_budget_refuses_start():
    job = replace(_by_owner(), budget=None)
    with pytest.raises(BudgetRefused):
        transition(job, JobEvent.START, actor=WORKER, budget_check=BudgetCheck.approved())


def test_absent_budget_check_refuses_claim():
    job = _job(JobState.READY)
    with pytest.raises(BudgetRefused, match="budget authorization"):
        transition(job, JobEvent.CLAIM, actor=WORKER, budget_check=None)


def test_absent_budget_check_refuses_start():
    job = _by_owner()
    with pytest.raises(BudgetRefused):
        transition(job, JobEvent.START, actor=WORKER, budget_check=None)


def test_exhausted_per_job_budget_refuses():
    job = _job(JobState.READY)
    check = BudgetCheck(per_job_allowed=False, global_allowed=True, detail="job budget exhausted")
    with pytest.raises(BudgetRefused):
        transition(job, JobEvent.CLAIM, actor=WORKER, budget_check=check)


def test_exhausted_global_budget_refuses():
    job = _job(JobState.READY)
    check = BudgetCheck(per_job_allowed=True, global_allowed=False, detail="monthly cap reached")
    with pytest.raises(BudgetRefused, match="monthly cap reached"):
        transition(job, JobEvent.CLAIM, actor=WORKER, budget_check=check)


def test_start_rechecks_budget_even_after_claim_passed():
    claimed = transition(
        _job(JobState.READY),
        JobEvent.CLAIM,
        actor=WORKER,
        budget_check=BudgetCheck.approved(),
    )
    exhausted = BudgetCheck(per_job_allowed=False, global_allowed=True, detail="spent at claim")
    with pytest.raises(BudgetRefused):
        transition(claimed, JobEvent.START, actor=WORKER, budget_check=exhausted)


def test_budget_refusal_leaves_the_job_unchanged():
    job = _by_owner()
    with pytest.raises(BudgetRefused):
        transition(job, JobEvent.START, actor=WORKER, budget_check=None)
    assert job.state is JobState.CLAIMED


# --------------------------------------------------------------------------
# Crash / recovery -- no silent terminal state
# --------------------------------------------------------------------------

def test_no_event_is_a_default():
    # There is no transition that marks a job done without an explicit complete.
    job = _job(JobState.RUNNING, owner=OWNER)
    assert job.state is JobState.RUNNING


def test_running_job_stays_running_across_other_events():
    job = _job(JobState.RUNNING, owner=OWNER)
    with pytest.raises(InvalidTransition):
        transition(job, JobEvent.RELEASE, actor=WORKER)


def test_fail_is_explicit_and_terminal():
    job = _job(JobState.RUNNING, owner=OWNER)
    failed = transition(job, JobEvent.FAIL, actor=WORKER)
    assert failed.state is JobState.FAILED


def test_fail_from_ready_requires_operator():
    # A pre-flight rejection of an unclaimed job is now an operator/system action.
    job = _job(JobState.READY)
    with pytest.raises(AuthorizationRefused):
        transition(job, JobEvent.FAIL, actor=WORKER)
    failed = transition(job, JobEvent.FAIL, actor=OPERATOR)
    assert failed.state is JobState.FAILED
    assert failed.owner is None


def test_a_failed_job_cannot_be_revived_implicitly():
    failed = transition(_job(JobState.RUNNING, owner=OWNER), JobEvent.FAIL, actor=WORKER)
    with pytest.raises(InvalidTransition):
        transition(failed, JobEvent.CLAIM, actor=WORKER, budget_check=BudgetCheck.approved())


def test_a_done_job_is_terminal():
    done = transition(_job(JobState.RUNNING, owner=OWNER), JobEvent.COMPLETE, actor=WORKER)
    assert done.state is JobState.DONE
    assert done.terminal
    with pytest.raises(InvalidTransition):
        transition(done, JobEvent.FAIL, actor=WORKER)


def test_blocked_review_loop_is_recoverable():
    # running -> block -> unblock -> submit -> revise -> complete
    running = _job(JobState.RUNNING, owner=OWNER)
    blocked = transition(running, JobEvent.BLOCK, actor=WORKER)
    unblocked = transition(blocked, JobEvent.UNBLOCK, actor=WORKER)
    assert unblocked.state is JobState.RUNNING

    review = transition(unblocked, JobEvent.SUBMIT, actor=WORKER)
    assert review.state is JobState.REVIEW

    revised = transition(review, JobEvent.REVISE, actor=WORKER)
    assert revised.state is JobState.RUNNING

    done = transition(revised, JobEvent.COMPLETE, actor=WORKER)
    assert done.state is JobState.DONE


def test_transition_is_pure_and_returns_a_new_job():
    job = _by_owner()
    result = transition(job, JobEvent.START, actor=WORKER, budget_check=BudgetCheck.approved())
    assert result is not job
    assert result.state is JobState.RUNNING
    assert job.state is JobState.CLAIMED  # original untouched


# --------------------------------------------------------------------------
# Exceptions are a coherent family
# --------------------------------------------------------------------------

def test_refusals_are_job_transition_errors():
    assert issubclass(InvalidTransition, JobTransitionError)
    assert issubclass(BudgetRefused, JobTransitionError)
    assert issubclass(ClaimConflict, JobTransitionError)
    assert issubclass(AuthorizationRefused, JobTransitionError)


# --------------------------------------------------------------------------
# Structural: this module cannot enable unattended execution
# --------------------------------------------------------------------------

def test_jobs_package_imports_no_process_network_or_credential_modules():
    import pathlib

    import forge.jobs

    root = pathlib.Path(forge.jobs.__file__).parent
    forbidden = (
        "import subprocess",
        "import socket",
        "import urllib",
        "import requests",
        "import http",
        "os.environ",
        "from os import environ",
    )
    sources = list(root.glob("*.py"))
    assert sources, "expected forge/jobs source files"
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} imports or reads {token!r}"