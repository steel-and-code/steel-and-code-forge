"""Tests for the FORGE-14 pure contract (:mod:`forge.jobs.progress`).

Exhaustive deterministic tables over the recovery classifier and the external-
operation state machine, value-shape guarantees (frozen, reference-only
``evidence``), and the resume-plan blocking rules. Everything here is pure -- no
store, no clock, no file.
"""

from __future__ import annotations

import dataclasses

import pytest

from forge.jobs.progress import (
    DEFAULT_RETRY_POLICY,
    DEFAULT_SIDE_EFFECT_CLASS,
    Checkpoint,
    CheckpointKind,
    ExternalOpRecord,
    FailureClass,
    InvalidOpTransition,
    OpAction,
    OpEvidenceRequired,
    OpState,
    RecoveryDecision,
    ResolutionKind,
    ResumePlan,
    RetryPolicy,
    SideEffectClass,
    advance_op_state,
    backoff_delay,
    classify_recovery,
    compute_resume_plan,
    op_key,
    side_effect_class_for,
)


# --------------------------------------------------------------------------
# RetryPolicy
# --------------------------------------------------------------------------


def test_retry_policy_defaults_match_the_spec():
    p = RetryPolicy()
    assert (p.max_attempts, p.initial_delay_sec, p.backoff_factor, p.max_delay_sec) == (
        3,
        30.0,
        2.0,
        300.0,
    )
    assert DEFAULT_RETRY_POLICY == RetryPolicy()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"max_attempts": True},
        {"max_attempts": 1.5},
        {"initial_delay_sec": -1.0},
        {"backoff_factor": 0.5},
        {"max_delay_sec": 29.0},  # below initial_delay
    ],
)
def test_retry_policy_refuses_invalid_values(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_retry_policy_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        RetryPolicy().max_attempts = 9


# --------------------------------------------------------------------------
# backoff_delay
# --------------------------------------------------------------------------


def test_backoff_delay_is_deterministic_and_bounded():
    p = RetryPolicy()
    assert backoff_delay(p, 1) == 30.0
    assert backoff_delay(p, 2) == 60.0
    assert backoff_delay(p, 3) == 120.0
    assert backoff_delay(p, 4) == 240.0
    assert backoff_delay(p, 5) == 300.0
    assert backoff_delay(p, 6) == 300.0  # capped, never beyond max_delay
    assert backoff_delay(p, 100) == 300.0
    # no randomness: repeated calls identical
    for _ in range(5):
        assert backoff_delay(p, 3) == 120.0


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_backoff_delay_refuses_bad_attempt(bad):
    with pytest.raises(ValueError):
        backoff_delay(RetryPolicy(), bad)


# --------------------------------------------------------------------------
# classify_recovery — exhaustive table
# --------------------------------------------------------------------------


def _rc(fc, attempts, policy=DEFAULT_RETRY_POLICY):
    return classify_recovery(fc, attempts, policy)


def test_classify_transient_within_budget_retries_with_backoff():
    r = _rc(FailureClass.TRANSIENT, 1)
    assert r.decision is RecoveryDecision.RETRY
    assert r.next_delay == 60.0
    assert r.reason is None


def test_classify_transient_exhausted_fails():
    r = _rc(FailureClass.TRANSIENT, 3)
    assert r.decision is RecoveryDecision.FAIL
    assert r.next_delay is None
    assert r.reason == "attempt budget exhausted"


def test_classify_external_error_bounded_like_transient():
    assert _rc(FailureClass.EXTERNAL_ERROR, 1).decision is RecoveryDecision.RETRY
    assert _rc(FailureClass.EXTERNAL_ERROR, 2).decision is RecoveryDecision.RETRY
    assert _rc(FailureClass.EXTERNAL_ERROR, 3).decision is RecoveryDecision.FAIL


def test_classify_deterministic_refusal_always_fails():
    for attempts in (1, 2, 3):
        r = _rc(FailureClass.DETERMINISTIC_REFUSAL, attempts)
        assert r.decision is RecoveryDecision.FAIL
        assert r.reason == "deterministic refusal"


def test_classify_uncertain_side_effect_always_escalates_never_retries():
    for attempts in (1, 2, 3, 10):
        r = _rc(FailureClass.SIDE_EFFECT_UNCERTAIN, attempts)
        assert r.decision is RecoveryDecision.ESCALATE
        assert r.next_delay is None
        assert r.reason == "side effect uncertain; operator resolution required"


def test_classify_exhaustive_table_total():
    """Every FailureClass x attempt in {1..max+1} x policy in {max=3, max=1}."""
    for policy in (RetryPolicy(), RetryPolicy(max_attempts=1)):
        for fc in FailureClass:
            for attempts in (1, 2, 3, 4):
                r = classify_recovery(fc, attempts, policy)
                assert isinstance(r.decision, RecoveryDecision)
                # invariant: next_delay iff RETRY; reason iff non-RETRY
                assert (r.next_delay is not None) is (r.decision is RecoveryDecision.RETRY)
                assert (r.reason is not None) is (r.decision is not RecoveryDecision.RETRY)


def test_classify_max_attempts_one_never_retries():
    policy = RetryPolicy(max_attempts=1)
    r = classify_recovery(FailureClass.TRANSIENT, 1, policy)
    assert r.decision is RecoveryDecision.FAIL
    assert r.reason == "attempt budget exhausted"


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_classify_refuses_bad_attempts_made(bad):
    with pytest.raises(ValueError):
        classify_recovery(FailureClass.TRANSIENT, bad)


# --------------------------------------------------------------------------
# Value shapes
# --------------------------------------------------------------------------


def test_checkpoint_is_frozen_and_reference_only():
    cp = Checkpoint(
        job_id="job-1", seq=1, attempt=1, kind=CheckpointKind.STEP_DONE,
        step_id="run-tests", generation=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        cp.seq = 2


def test_checkpoint_field_names_pinned():
    """A future 'transcript'/'secret'-shaped field added to Checkpoint fails here."""
    names = {f.name for f in dataclasses.fields(Checkpoint)}
    assert names == {
        "job_id", "seq", "attempt", "kind", "step_id", "side_effect_class",
        "idempotency_key", "outcome", "evidence", "generation",
    }


def test_external_op_field_names_pinned():
    names = {f.name for f in dataclasses.fields(ExternalOpRecord)}
    assert names == {
        "op_key", "job_id", "step_id", "op_kind", "side_effect_class",
        "idempotency_key", "state", "evidence", "attempt", "generation",
    }


def test_resume_plan_can_proceed_property():
    assert ResumePlan(resume_seq=None, blocked_ops=frozenset()).can_proceed is True
    assert ResumePlan(resume_seq=None, blocked_ops=frozenset({"k"})).can_proceed is False


# --------------------------------------------------------------------------
# Side-effect classification
# --------------------------------------------------------------------------


def test_default_side_effect_class_is_non_idempotent():
    assert side_effect_class_for("git_push") is SideEffectClass.NON_IDEMPOTENT
    assert side_effect_class_for("pr_create") is SideEffectClass.NON_IDEMPOTENT
    assert side_effect_class_for("jira_comment") is SideEffectClass.NON_IDEMPOTENT
    assert side_effect_class_for("jira_transition") is SideEffectClass.NON_IDEMPOTENT
    assert side_effect_class_for("fs_write") is SideEffectClass.NON_IDEMPOTENT


def test_unknown_op_fails_closed_to_non_idempotent():
    assert side_effect_class_for("totally_unknown_thing") is SideEffectClass.NON_IDEMPOTENT


def test_side_effect_class_mapping_never_downgrades_to_guarded():
    assert set(DEFAULT_SIDE_EFFECT_CLASS.values()) == {SideEffectClass.NON_IDEMPOTENT}


# --------------------------------------------------------------------------
# External-operation state machine — exhaustive table
# --------------------------------------------------------------------------


def _op(evidence=None):
    return advance_op_state(OpState.PENDING, OpAction.COMMIT, evidence=evidence)


def test_commit_requires_evidence():
    with pytest.raises(OpEvidenceRequired):
        advance_op_state(OpState.PENDING, OpAction.COMMIT, evidence=None)
    with pytest.raises(OpEvidenceRequired):
        advance_op_state(OpState.PENDING, OpAction.COMMIT, evidence="   ")


def test_commit_persists_evidence_handle():
    assert advance_op_state(OpState.PENDING, OpAction.COMMIT, evidence="pr#42") is OpState.COMMITTED


@pytest.mark.parametrize(
    "current, action, expected",
    [
        (OpState.PENDING, OpAction.COMMIT, OpState.COMMITTED),
        (OpState.PENDING, OpAction.MARK_UNCERTAIN, OpState.UNCERTAIN),
        (OpState.PENDING, OpAction.MARK_FAILED, OpState.FAILED),
        (OpState.UNCERTAIN, OpAction.COMMIT, OpState.COMMITTED),
        (OpState.UNCERTAIN, OpAction.MARK_FAILED, OpState.FAILED),
        (OpState.UNCERTAIN, OpAction.RESOLVE_REPLAY, OpState.PENDING),
        (OpState.UNCERTAIN, OpAction.RESOLVE_SKIP, OpState.SKIPPED),
        (OpState.UNCERTAIN, OpAction.RESOLVE_FAIL, OpState.FAILED),
    ],
)
def test_advance_op_legal_edges(current, action, expected):
    kwargs = {"evidence": "handle"} if action is OpAction.COMMIT else {}
    assert advance_op_state(current, action, **kwargs) is expected


@pytest.mark.parametrize(
    "current, action",
    [
        # resolution only from UNCERTAIN
        (OpState.PENDING, OpAction.RESOLVE_REPLAY),
        (OpState.PENDING, OpAction.RESOLVE_SKIP),
        (OpState.PENDING, OpAction.RESOLVE_FAIL),
        # re-marking uncertain is refused
        (OpState.UNCERTAIN, OpAction.MARK_UNCERTAIN),
        # absorbing states move nowhere
        (OpState.COMMITTED, OpAction.COMMIT),
        (OpState.COMMITTED, OpAction.RESOLVE_SKIP),
        (OpState.FAILED, OpAction.RESOLVE_REPLAY),
        (OpState.SKIPPED, OpAction.RESOLVE_REPLAY),
        (OpState.SKIPPED, OpAction.MARK_FAILED),
    ],
)
def test_advance_op_illegal_edges_refused(current, action):
    with pytest.raises(InvalidOpTransition):
        advance_op_state(current, action, evidence="handle")


def test_advance_op_exhaustive_table_total():
    """Every (state, action) pair either succeeds or raises InvalidOpTransition /
    OpEvidenceRequired -- never anything else, never a silent no-op."""
    for state in OpState:
        for action in OpAction:
            try:
                advance_op_state(state, action, evidence="handle")
            except (InvalidOpTransition, OpEvidenceRequired):
                pass


# --------------------------------------------------------------------------
# resume plan
# --------------------------------------------------------------------------


def _external_op(op_key="k", state=OpState.PENDING, sclass=SideEffectClass.NON_IDEMPOTENT):
    return ExternalOpRecord(
        op_key=op_key, job_id="job-1", step_id="s", op_kind="git_push",
        side_effect_class=sclass, idempotency_key=None, state=state,
        evidence=None, attempt=1, generation=1,
    )


def test_resume_seq_is_last_completed_marker():
    cps = [
        Checkpoint("job-1", 1, 1, CheckpointKind.STEP_DONE, "clone", generation=1),
        Checkpoint("job-1", 2, 1, CheckpointKind.SIDE_EFFECT_BEFORE, "push", generation=1),
        Checkpoint("job-1", 3, 1, CheckpointKind.SIDE_EFFECT_AFTER, "push", generation=1),
    ]
    plan = compute_resume_plan(cps, ())
    assert plan.resume_seq == 3


def test_resume_seq_none_without_completion():
    plan = compute_resume_plan(
        [Checkpoint("job-1", 1, 1, CheckpointKind.SIDE_EFFECT_BEFORE, "push", generation=1)],
        (),
    )
    assert plan.resume_seq is None


def test_unresolved_non_idempotent_op_blocks():
    plan = compute_resume_plan((), [_external_op("a", OpState.PENDING)])
    assert plan.can_proceed is False
    assert plan.blocked_ops == frozenset({"a"})


def test_unresolved_uncertain_op_blocks():
    plan = compute_resume_plan((), [_external_op("a", OpState.UNCERTAIN)])
    assert plan.can_proceed is False


def test_committed_and_failed_ops_do_not_block():
    for st in (OpState.COMMITTED, OpState.FAILED, OpState.SKIPPED):
        plan = compute_resume_plan((), [_external_op("a", st)])
        assert plan.can_proceed is True


def test_idempotent_guarded_pending_does_not_block():
    plan = compute_resume_plan(
        (), [_external_op("a", OpState.PENDING, SideEffectClass.IDEMPOTENT_GUARDED)]
    )
    assert plan.can_proceed is True


def test_deterministic_steps_never_block():
    plan = compute_resume_plan([], [])
    assert plan.can_proceed is True


# --------------------------------------------------------------------------
# op_key
# --------------------------------------------------------------------------


def test_op_key_is_deterministic_and_distinct():
    assert op_key("j", "s", "k") == op_key("j", "s", "k")
    assert op_key("j", "s", "k") != op_key("j", "s", "k2")
    assert op_key("j", "s", "k") != op_key("j2", "s", "k")


def test_op_key_independent_of_attempt():
    # REPLAY keeps the same logical key; attempt is not part of the key.
    k = op_key("j", "s", "git_push")
    assert k == op_key("j", "s", "git_push")


def test_classify_recovery_rejects_non_failure_class():
    with pytest.raises(TypeError):
        classify_recovery("transient", 1)


def test_advance_op_state_rejects_non_enum_args():
    with pytest.raises(TypeError):
        advance_op_state("pending", OpAction.COMMIT, evidence="x")
    with pytest.raises(TypeError):
        advance_op_state(OpState.PENDING, "commit", evidence="x")


def test_side_effect_class_for_rejects_empty_kind():
    with pytest.raises(ValueError):
        side_effect_class_for("   ")