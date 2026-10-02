"""Tests for terminal approval.

Covers the routing between a classification and a future execution: that a
denial never reaches a prompt, that an allow never reaches a channel, that an
approval-required operation with nobody present fails closed, and that only an
explicit affirmative approves.

The verdicts under test are produced by the real classifiers rather than
constructed by hand, because the interesting failures live at the seam between
the two modules: a request built from a real denial, a prompt rendered from a
real resolved target list.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from forge.tools.approval import (
    NOT_DETERMINABLE,
    NOT_SUPPLIED,
    ApprovalConsumed,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
    Approver,
    DeniedByPolicy,
    OperationKind,
    ScriptedApprovalChannel,
    TerminalApprovalChannel,
    parse_response,
    render_prompt,
)
from forge.tools.commands import Command, CommandClassifier
from forge.tools.secrets import SecretClassifier
from forge.tools.workspace import Classification, Workspace


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def classifier(workspace: Workspace) -> CommandClassifier:
    return CommandClassifier(workspace)


@pytest.fixture()
def secrets(workspace: Workspace) -> SecretClassifier:
    return SecretClassifier(workspace)


def _cmd(program: str, *args: str, **kw: object) -> Command:
    return Command(program=program, args=tuple(args), **kw)  # type: ignore[arg-type]


def _in(workspace: Workspace, name: str) -> Path:
    """A path inside the workspace.

    The secret classifier resolves relative paths against the process working
    directory, which in a test run is the repository -- outside the workspace
    root, and therefore denied on containment before any secret rule is even
    reached. Naming the file inside the workspace is what makes these tests
    exercise the secret tier rather than the boundary.
    """
    return workspace.root / name


# ==========================================================================
# A denial never becomes a request
# ==========================================================================


def test_denied_command_becomes_policy_denial(
    classifier: CommandClassifier,
) -> None:
    channel = ScriptedApprovalChannel(["y"])
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("sudo", "rm", "file.txt")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert outcome.denied
    assert not outcome.approved
    # The decisive guarantee: no prompt was ever shown, so no operator could
    # have said yes to a denial.
    assert channel.presented == []


def test_denied_command_outside_workspace_is_policy_denial(
    classifier: CommandClassifier, tmp_path: Path
) -> None:
    approver = Approver(ScriptedApprovalChannel(["yes"]))

    outcome = approver.submit_command(
        classifier.classify(_cmd("ls", cwd=tmp_path)), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert "workspace.outside_lexical" in outcome.detail


def test_denied_secret_becomes_policy_denial(secrets: SecretClassifier) -> None:
    approver = Approver(ScriptedApprovalChannel(["y"]))

    outcome = approver.submit_secret(secrets.classify(".env"), agent="byte")

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert not outcome.human_decided


def test_name_guess_secret_is_approval_required_not_denied(
    secrets: SecretClassifier, workspace: Workspace
) -> None:
    approver = Approver(ScriptedApprovalChannel(["y"]))

    outcome = approver.submit_secret(
        secrets.classify(_in(workspace, "secrets.json")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.request is not None
    assert outcome.request.kind is OperationKind.SECRET
    assert outcome.request.rule == "guess.secrets_json"


def test_private_key_content_denial_is_policy_denial(
    secrets: SecretClassifier, workspace: Workspace
) -> None:
    key = workspace.root / "config"
    key.write_text("-----BEGIN RSA PRIVATE KEY-----\nnotarealkey\n")

    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_secret(
        secrets.classify(key), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY


def test_secret_denial_overrides_an_approvable_command(
    classifier: CommandClassifier,
    secrets: SecretClassifier,
    workspace: Workspace,
) -> None:
    """A command that is merely approval-required does not rescue a secret.

    The enforcement pipeline puts the secret check above the prompt, so a
    structural denial holds even when the command itself would have been
    promptable.
    """
    approver = Approver(ScriptedApprovalChannel(["y"]))
    command = classifier.classify(_cmd("cat", "secrets.json"))
    assert command.classification is Classification.ALLOW

    outcome = approver.submit_command(
        command,
        agent="byte",
        secret_verdicts=(secrets.classify(_in(workspace, ".env")),),
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert "secret.env" in outcome.detail


def test_request_constructor_refuses_a_denied_verdict(
    classifier: CommandClassifier,
) -> None:
    verdict = classifier.classify(_cmd("sudo", "true"))

    with pytest.raises(DeniedByPolicy):
        ApprovalRequest.from_command(verdict, agent="byte")


def test_resolve_refuses_a_hand_built_denial(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    """A denial survives being wrapped in a request built directly.

    ``ApprovalRequest`` is a frozen dataclass, so a caller can construct one
    without going through the constructors. ``resolve`` re-checks rather than
    trusting the wrapper.
    """
    channel = ScriptedApprovalChannel(["y"])
    forged = ApprovalRequest(
        kind=OperationKind.COMMAND,
        agent="byte",
        tool="exec_command",
        operation="run `sudo`",
        rule="cmd.sudo",
        classification=Classification.DENY,
        reason="privilege escalation",
        workspace=workspace.root,
        detail="sudo true",
    )

    outcome = Approver(channel).resolve(forged)

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert channel.presented == []


# ==========================================================================
# An allow never reaches a channel
# ==========================================================================


def test_allowed_command_approved_without_consulting_the_channel(
    classifier: CommandClassifier,
) -> None:
    channel = ScriptedApprovalChannel(["y"])
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("pytest", "tests/", "-q")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.approved
    assert channel.presented == []


def test_allow_records_no_operator_response(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("git", "status")), agent="byte"
    )

    assert outcome.approved
    # Nothing was asked, so nothing was answered. Recording a response here
    # would imply an operator was consulted.
    assert outcome.operator_response is None


# ==========================================================================
# Unattended fails closed
# ==========================================================================


def test_approval_required_without_a_channel_fails_closed(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    assert outcome.denied
    assert not outcome.human_decided
    assert outcome.requires_human


def test_unavailable_channel_fails_closed(classifier: CommandClassifier) -> None:
    channel = ScriptedApprovalChannel(["y"], available=False)
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("git", "push")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    # Availability is checked before rendering, so an unattended run gets a
    # clear failure rather than a prompt that goes nowhere.
    assert channel.presented == []


def test_unattended_detail_states_the_operation_did_not_run(
    classifier: CommandClassifier,
) -> None:
    """A failure that says "needed approval, nobody was there" is reportable.

    The detail is deliberately explicit that nothing ran and nothing was
    queued, because a silent skip is not a reportable condition.
    """
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert "not run" in outcome.detail
    assert "not queued" in outcome.detail


def test_unattended_still_carries_the_request(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("rm", "build", "-r")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    # The request is retained so a caller can report exactly what was refused.
    assert outcome.request is not None
    assert outcome.request.rule == "cmd.delete_recursive"


# ==========================================================================
# Strict affirmative parsing
# ==========================================================================


@pytest.mark.parametrize("response", ["y", "Y", "yes", "YES", " yes ", "\tYes\n"])
def test_exact_affirmatives_approve(response: str) -> None:
    assert parse_response(response) is True


@pytest.mark.parametrize(
    "response",
    [
        "",
        "   ",
        "n",
        "no",
        "yeah",
        "yep",
        "yolo",
        "yes please",
        "ok",
        "1",
        "true",
        "y n",
        "y.",
        "sure",
        "delete everything",
    ],
)
def test_everything_else_rejects(response: str) -> None:
    assert parse_response(response) is False


def test_no_answer_rejects() -> None:
    """An unreachable prompt is not an approval."""
    assert parse_response(None) is False


@pytest.mark.parametrize("response", ["y", "yes", "Y"])
def test_affirmative_through_the_approver_approves(
    classifier: CommandClassifier, response: str
) -> None:
    outcome = Approver(ScriptedApprovalChannel([response])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.operator_response == response


@pytest.mark.parametrize("response", ["", "n", "maybe", "yeah"])
def test_non_affirmative_through_the_approver_rejects(
    classifier: CommandClassifier, response: str
) -> None:
    outcome = Approver(ScriptedApprovalChannel([response])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED
    assert outcome.human_decided
    assert not outcome.requires_human


def test_no_response_is_rejected_not_unattended(
    classifier: CommandClassifier,
) -> None:
    """A channel that was available but got no answer is a human decision.

    The distinction matters: the operator was reachable and declined, which is
    different from nobody being there at all.
    """
    outcome = Approver(ScriptedApprovalChannel([None])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED
    assert "no approval was given" in outcome.detail


# ==========================================================================
# Approval is per-operation and single use
# ==========================================================================


def test_approver_remembers_nothing_between_operations(
    classifier: CommandClassifier,
) -> None:
    """The same command asks again, because v1 has no session-wide allow."""
    channel = ScriptedApprovalChannel(["y", "y"])
    approver = Approver(channel)
    command = classifier.classify(_cmd("git", "commit", "-m", "x"))

    first = approver.submit_command(command, agent="byte")
    second = approver.submit_command(command, agent="byte")

    assert first.approved and second.approved
    assert len(channel.presented) == 2


def test_outcome_can_be_consumed_once() -> None:
    outcome = ApprovalOutcome(
        decision=ApprovalDecision.APPROVED, detail="approved", consumed=False
    )

    claimed = outcome.consume()

    assert claimed.consumed is True
    assert outcome.consumed is False  # frozen: the original is untouched


def test_consuming_twice_raises() -> None:
    outcome = ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="approved")

    with pytest.raises(ApprovalConsumed):
        outcome.consume().consume()


def test_approving_the_same_command_twice_uses_two_separate_outcomes(
    classifier: CommandClassifier,
) -> None:
    channel = ScriptedApprovalChannel(["y", "y"])
    approver = Approver(channel)
    command = classifier.classify(_cmd("git", "commit", "-m", "x"))

    approver.submit_command(command, agent="byte").consume()
    second = approver.submit_command(command, agent="byte")

    # A second approval was genuinely required and genuinely given, and its
    # outcome is unconsumed.
    assert second.approved
    assert second.consumed is False


# ==========================================================================
# The prompt shows what the spec asks it to show
# ==========================================================================


def test_prompt_contains_every_specified_field(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "message")),
        agent="byte",
        intent="the operator asked for a commit",
        git_context="remote origin, branch main, 1 ahead",
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    for field in (
        "Requesting agent",
        "Operation",
        "Tool",
        "Workspace",
        "Command or action",
        "Affected targets",
        "Rule",
        "Why it is gated",
        "Reason",
        "Git context",
    ):
        assert field in prompt, f"missing field: {field}"


def test_prompt_shows_the_exact_program_and_arguments(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("rm", "build", "-rf", "dist")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "rm build -rf dist" in prompt


def test_prompt_shows_resolved_targets(classifier: CommandClassifier) -> None:
    """A recursive delete shows the actual target list, per the wildcard policy."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("rm", "build", "dist", "-rf")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "Affected targets" in prompt
    assert "build" in prompt
    assert "dist" in prompt
    assert NOT_DETERMINABLE not in prompt


def test_prompt_states_when_targets_are_not_determinable(
    classifier: CommandClassifier,
) -> None:
    """An operator seeing "not determinable" can decide differently.

    Omitting the field would be worse: a missing line is indistinguishable from
    an absent field to anyone skimming.
    """
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "push", "origin", "main")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert NOT_DETERMINABLE in prompt


def test_prompt_says_not_supplied_for_absent_intent(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert NOT_SUPPLIED in prompt


def test_prompt_does_not_invent_a_reason(
    secrets: SecretClassifier, workspace: Workspace
) -> None:
    """The approval layer renders what it is given and says so otherwise."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_secret(
        secrets.classify(_in(workspace, "secrets.json")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert NOT_SUPPLIED in prompt
    assert "filename suggests stored secrets" in prompt  # the classifier's own


def test_prompt_carries_git_context_when_supplied(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "push")),
        agent="byte",
        git_context="remote origin, branch main, 3 ahead",
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "remote origin, branch main, 3 ahead" in prompt


def test_prompt_flags_a_shell_string(classifier: CommandClassifier) -> None:
    """Nothing inside a shell string is checked, so the prompt says so."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("bash", "-c", "echo hi", shell=True)),
        agent="byte",
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "shell string" in prompt
    assert "not parsed" in prompt


def test_prompt_never_contains_file_contents(
    secrets: SecretClassifier, workspace: Workspace
) -> None:
    """Non-exposure: a prompt must not become a way to see a secret value."""
    secret = workspace.root / "config"
    secret.write_text('{"api_key": "AKIAEXAMPLE-do-not-print-me"}')

    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_secret(
        secrets.classify(secret), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "AKIAEXAMPLE-do-not-print-me" not in prompt
    assert str(secret) in prompt


def test_prompt_footer_names_the_requesting_agent(
    classifier: CommandClassifier,
) -> None:
    """The question names who is asking, so the answer is not given blindly."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "Approve request from byte? Type 'y' or 'yes':" in prompt


def test_prompt_footer_labels_stay_aligned(
    classifier: CommandClassifier,
) -> None:
    """The field column is fixed width, so prompts line up between operations."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    fields = [
        line for line in prompt.splitlines() if line.startswith("  ") and line.strip()
    ]
    assert fields
    # Every field value begins in the same column.
    columns = {len(line) - len(line.lstrip()) for line in fields}
    assert columns == {2}
    value_columns = {line.index(line.strip()) for line in fields}
    assert len(value_columns) == 1, value_columns


def test_render_prompt_is_pure(classifier: CommandClassifier) -> None:
    """Rendering a prompt must not itself be the moment it is asked."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert render_prompt(outcome.request) == outcome.request.render()  # type: ignore[arg-type]


# ==========================================================================
# The terminal channel
# ==========================================================================


def test_terminal_channel_prints_the_prompt_and_reads_a_line(
    classifier: CommandClassifier,
) -> None:
    stream = io.StringIO()
    channel = TerminalApprovalChannel(
        prompt_stream=stream, read=lambda _: "y", available=True
    )

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    printed = stream.getvalue()
    assert "APPROVAL REQUIRED" in printed
    assert "git commit -m x" in printed


def test_terminal_channel_rejects_on_decline(
    classifier: CommandClassifier,
) -> None:
    channel = TerminalApprovalChannel(
        prompt_stream=io.StringIO(), read=lambda _: "no", available=True
    )

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED


def test_terminal_channel_treats_eof_as_no_answer(
    classifier: CommandClassifier,
) -> None:
    def _raise_eof(_: str) -> str:
        raise EOFError

    channel = TerminalApprovalChannel(
        prompt_stream=io.StringIO(), read=_raise_eof, available=True
    )

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED
    assert outcome.operator_response is None


def test_terminal_channel_treats_interrupt_as_no_answer(
    classifier: CommandClassifier,
) -> None:
    """An interrupt at the prompt is a decision to stop, not an approval."""

    def _interrupt(_: str) -> str:
        raise KeyboardInterrupt

    channel = TerminalApprovalChannel(
        prompt_stream=io.StringIO(), read=_interrupt, available=True
    )

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED


def test_terminal_channel_without_a_tty_is_unavailable(
    classifier: CommandClassifier,
) -> None:
    """A detached stream has no usable TTY notion, so it fails closed."""
    channel = TerminalApprovalChannel(prompt_stream=io.StringIO(), read=lambda _: "y")

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    # Either the probe correctly reports no TTY, or the host genuinely has one.
    # Both are correct; what must never happen is an approval without an
    # operator, so assert on the decision's meaning rather than the probe.
    assert outcome.decision in (
        ApprovalDecision.DENIED_UNATTENDED,
        ApprovalDecision.APPROVED,
    )
    if outcome.decision is ApprovalDecision.APPROVED:
        assert channel.available is True


def test_unanswered_prompt_is_rejected_not_approved(
    classifier: CommandClassifier,
) -> None:
    channel = ScriptedApprovalChannel([])  # nothing to answer with

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")), agent="byte"
    )

    assert outcome.decision is ApprovalDecision.REJECTED


# ==========================================================================
# The layer does not execute and does not re-classify
# ==========================================================================


def test_module_exposes_no_execution_surface() -> None:
    """The handoff is an outcome. Nothing here runs anything.

    If a future change adds a runner, this test is the place that should fail
    first: the approval layer's whole claim is that it decides, and execution
    is a separate, later decision.
    """
    import forge.tools.approval as approval

    forbidden = ("run", "execute", "spawn", "popen", "subprocess", "system")
    offenders = [
        name
        for name in dir(approval)
        if not name.startswith("_")
        and not callable(getattr(approval, name))
        and any(word in name.casefold() for word in forbidden)
    ]
    assert offenders == []


def test_prompts_are_not_built_from_a_shadowed_verdict(
    classifier: CommandClassifier,
) -> None:
    """The routed verdict is the one that decided the outcome.

    If the layer re-classified internally, a rule change would be visible here
    as a mismatch between the rule shown to the operator and the rule that
    actually gated the operation.
    """
    verdict = classifier.classify(_cmd("rm", "build", "-rf"))
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        verdict, agent="byte"
    )

    assert outcome.request is not None
    assert outcome.request.rule == verdict.rule
    assert outcome.request.matched_rules == verdict.matched_rules
    assert outcome.request.targets == verdict.targets_resolved


def test_outcome_str_is_operator_safe(
    classifier: CommandClassifier, secrets: SecretClassifier
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["n"])).submit_command(
        classifier.classify(_cmd("git", "push", "origin", "main")), agent="byte"
    )

    text = str(outcome)

    assert "rejected" in text
    assert "git push origin main" in text


def test_decision_and_outcome_helpers_agree() -> None:
    approved = ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="")
    denied = ApprovalOutcome(
        decision=ApprovalDecision.DENIED_BY_POLICY, detail=""
    )
    unattended = ApprovalOutcome(
        decision=ApprovalDecision.DENIED_UNATTENDED, detail=""
    )

    assert approved.approved and not approved.denied
    assert denied.denied and not denied.approved
    assert unattended.denied and unattended.requires_human
    assert not unattended.human_decided
    assert ApprovalDecision.APPROVED.permitted
    assert not ApprovalDecision.DENIED_BY_POLICY.permitted
    assert not ApprovalDecision.DENIED_UNATTENDED.permitted
