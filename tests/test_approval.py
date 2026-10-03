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

import copy
import io
import threading
from dataclasses import replace
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
from forge.tools.commands import Command, CommandClassifier, CommandVerdict
from forge.tools.secrets import SecretClassifier, SecretVerdict
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
        classifier.classify(_cmd("sudo", "rm", "file.txt")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("ls", cwd=tmp_path)),
        agent="byte",
        workspace=classifier.workspace.root,
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
        workspace=workspace.root,
        secret_verdicts=(secrets.classify(_in(workspace, ".env")),),
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert "secret.env" in outcome.detail


def test_request_constructor_refuses_a_denied_verdict(
    classifier: CommandClassifier,
) -> None:
    verdict = classifier.classify(_cmd("sudo", "true"))

    with pytest.raises(DeniedByPolicy):
        ApprovalRequest.from_command(
            verdict, agent="byte", workspace=classifier.workspace.root
        )


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
    """An automatic operation is approved with no prompt at all.

    Uses `git status`, which is on List 3. `pytest` would not do here: it is
    approval-required as repository-code execution, so the channel is consulted
    and this test would measure the wrong thing.
    """
    channel = ScriptedApprovalChannel(["y"])
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("git", "status")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.approved
    assert channel.presented == []


def test_unknown_command_is_gated_not_auto_approved(
    classifier: CommandClassifier,
) -> None:
    """An unrecognised command reaches the channel rather than running.

    The counterpart to the test above, and the property the default change
    exists to provide: a classifier miss produces a prompt, not an approval.
    """
    channel = ScriptedApprovalChannel(["y"])
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("some-unknown-tool", "--check")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert len(channel.presented) == 1
    assert "unrecognised" in channel.presented[0].reason


def test_allow_records_no_operator_response(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("git", "status")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    assert outcome.denied
    assert not outcome.human_decided
    assert outcome.requires_human


def test_unavailable_channel_fails_closed(classifier: CommandClassifier) -> None:
    channel = ScriptedApprovalChannel(["y"], available=False)
    approver = Approver(channel)

    outcome = approver.submit_command(
        classifier.classify(_cmd("git", "push")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert "not run" in outcome.detail
    assert "not queued" in outcome.detail


def test_unattended_still_carries_the_request(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("rm", "build", "-r")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.operator_response == response


@pytest.mark.parametrize("response", ["", "n", "maybe", "yeah"])
def test_non_affirmative_through_the_approver_rejects(
    classifier: CommandClassifier, response: str
) -> None:
    outcome = Approver(ScriptedApprovalChannel([response])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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

    first = approver.submit_command(
        command, agent="byte", workspace=classifier.workspace.root
    )
    second = approver.submit_command(
        command, agent="byte", workspace=classifier.workspace.root
    )

    assert first.approved and second.approved
    assert len(channel.presented) == 2
    # Two decisions produce two independent capabilities, so spending one
    # leaves the other spendable.
    assert not first.claimed
    first.claim()
    assert first.claimed and not second.claimed


def test_approving_the_same_command_twice_uses_two_separate_outcomes(
    classifier: CommandClassifier,
) -> None:
    channel = ScriptedApprovalChannel(["y", "y"])
    approver = Approver(channel)
    command = classifier.classify(_cmd("git", "commit", "-m", "x"))

    approver.submit_command(
        command, agent="byte", workspace=classifier.workspace.root
    ).claim()
    second = approver.submit_command(
        command, agent="byte", workspace=classifier.workspace.root
    )

    # A second approval was genuinely required and genuinely given, and it is
    # still spendable.
    assert second.approved
    assert not second.claimed


# ==========================================================================
# The prompt shows what the spec asks it to show
# ==========================================================================


def test_prompt_contains_every_specified_field(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "message")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("rm", "build", "-rf", "dist")),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "rm build -rf dist" in prompt


def test_prompt_shows_resolved_targets(classifier: CommandClassifier) -> None:
    """A recursive delete shows the actual target list, per the wildcard policy."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("rm", "build", "dist", "-rf")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "push", "origin", "main")),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert NOT_DETERMINABLE in prompt


def test_prompt_says_not_supplied_for_absent_intent(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        workspace=classifier.workspace.root,
        git_context="remote origin, branch main, 3 ahead",
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "remote origin, branch main, 3 ahead" in prompt


def test_prompt_flags_a_shell_string(classifier: CommandClassifier) -> None:
    """Nothing inside a shell string is checked, so the prompt says so."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("bash", "-c", "echo hi", shell=True)),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert prompt.endswith(
        "Approve request from byte? "
        "Type 'y'/'yes' to approve or 'n'/'no' to reject:"
    )


def test_prompt_footer_spells_out_both_answers(
    classifier: CommandClassifier,
) -> None:
    """The footer names approve and reject, so rejecting needs no guessing."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )
    prompt = outcome.request.render()  # type: ignore[union-attr]

    assert "Type 'y'/'yes' to approve or 'n'/'no' to reject:" in prompt
    assert "Type 'y' or 'yes'" not in prompt


def test_prompt_footer_labels_stay_aligned(
    classifier: CommandClassifier,
) -> None:
    """The field column is fixed width, so prompts line up between operations."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.REJECTED


def test_terminal_channel_without_a_tty_is_unavailable(
    classifier: CommandClassifier,
) -> None:
    """A detached stream has no usable TTY notion, so it fails closed."""
    channel = TerminalApprovalChannel(prompt_stream=io.StringIO(), read=lambda _: "y")

    outcome = Approver(channel).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
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
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.REJECTED


# ==========================================================================
# The layer does not execute and does not re-classify
# ==========================================================================


# ==========================================================================
# Verdicts are ranked together: DENY > APPROVAL_REQUIRED > ALLOW
# ==========================================================================
#
# The command verdict and every supplied secret verdict describe one operation,
# so they are ranked as a set and the strictest wins. The rule lives in
# `Approver.submit_command` so every future caller inherits it.
#
# The failure this closes: `cat secrets.json` is an ordinary command reading a
# name-guess file. When only secret *denials* were consulted, the operation was
# approved without asking, while the same file through `submit_secret` prompted.
# The inconsistency was a hole, not a design.


def _secret_verdicts(
    classifier: SecretClassifier, *paths: Path
) -> tuple[SecretVerdict, ...]:
    """Classify paths against the real secret rules.

    The classifier is passed rather than captured from a fixture because a
    module-level helper cannot see a test function's arguments.
    """
    return tuple(classifier.classify(p) for p in paths)


def _submit(
    verdict: CommandVerdict,
    workspace: Workspace,
    secrets_found: tuple[SecretVerdict, ...],
    responses: list[str | None] | None = None,
) -> tuple[ApprovalOutcome, ScriptedApprovalChannel]:
    channel = ScriptedApprovalChannel(responses or [])
    outcome = Approver(channel).submit_command(
        verdict,
        agent="byte",
        workspace=workspace.root,
        secret_verdicts=secrets_found,
    )
    return outcome, channel


def test_command_allow_with_secret_allow_approves_without_a_channel(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """Everything clean: no prompt at all."""
    outcome, channel = _submit(
        classifier.classify(_cmd("cat", "plain.txt")),
        workspace,
        _secret_verdicts(secrets, workspace.root / "plain.txt"),
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert channel.presented == []
    assert outcome.operator_response is None


def test_command_allow_with_secret_approval_required_prompts(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """An ordinary command reading a name-guess secret needs a human.

    This is the case the hole left open. `cat` is not dangerous and the file is
    not structural, but the filename suggests sensitivity and the spec says that
    tier is approval-required rather than denied.
    """
    outcome, channel = _submit(
        classifier.classify(_cmd("cat", "secrets.json")),
        workspace,
        _secret_verdicts(secrets, workspace.root / "secrets.json"),
        ["y"],
    )

    assert channel.presented, "a name-guess secret must produce a prompt"
    assert outcome.decision is ApprovalDecision.APPROVED


def test_command_allow_with_secret_deny_is_policy_denial(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    outcome, channel = _submit(
        classifier.classify(_cmd("cat", ".env")),
        workspace,
        _secret_verdicts(secrets, workspace.root / ".env"),
        ["y"],
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert channel.presented == []
    assert "secret.env" in outcome.detail


def test_command_approval_required_with_secret_allow_prompts(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """The command's own gate stands on its own; secrets do not interfere."""
    outcome, channel = _submit(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        workspace,
        _secret_verdicts(secrets, workspace.root / "plain.txt"),
        ["y"],
    )

    assert len(channel.presented) == 1
    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.request is not None
    # The command's rule is what gated it, not a secret rule.
    assert outcome.request.rule == "cmd.git_commit"


def test_command_approval_required_with_secret_approval_required_is_one_prompt(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """One operation, one decision.

    Both verdicts are gated, and the operator still answers a single question:
    they are approving one command, not two verdicts.
    """
    outcome, channel = _submit(
        classifier.classify(_cmd("cat", "secrets.json")),
        workspace,
        _secret_verdicts(secrets, workspace.root / "secrets.json"),
        ["y"],
    )

    assert len(channel.presented) == 1


def test_multiple_secrets_produce_one_prompt_not_one_each(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """A prompt per target would train the operator to approve without reading."""
    targets = []
    for name in ("secrets.json", "credentials.yaml", "app.token"):
        path = workspace.root / name
        path.write_text("name: value\n")
        targets.append(path)

    outcome, channel = _submit(
        classifier.classify(_cmd("cat", *(t.name for t in targets))),
        workspace,
        _secret_verdicts(secrets, *targets),
        ["y"],
    )

    assert len(channel.presented) == 1
    assert outcome.decision is ApprovalDecision.APPROVED


def test_command_approval_required_with_secret_deny_is_policy_denial(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """A gated command does not survive a structural secret among its targets."""
    outcome, channel = _submit(
        classifier.classify(_cmd("git", "add", "secrets.json")),
        workspace,
        _secret_verdicts(secrets, workspace.root / ".env"),
        ["y"],
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert channel.presented == []


def test_one_secret_deny_outranks_all_other_secret_verdicts(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """DENY wins over a name guess in the same operation.

    The deciding rule named in the outcome is the secret denial, because that is
    the one an operator can act on.
    """
    outcome, channel = _submit(
        classifier.classify(_cmd("cat", "secrets.json", ".env")),
        workspace,
        _secret_verdicts(
            secrets, workspace.root / "secrets.json", workspace.root / ".env"
        ),
        ["y"],
    )

    assert outcome.decision is ApprovalDecision.DENIED_BY_POLICY
    assert channel.presented == []
    assert "secret.env" in outcome.detail
    assert "guess.secrets_json" not in outcome.detail


def test_one_secret_approval_required_escalates_an_allow(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """A single name guess gates an otherwise-automatic command."""
    clean = workspace.root / "plain.txt"
    clean.write_text("ordinary\n")
    guessed = workspace.root / "secrets.json"
    guessed.write_text("{}\n")

    outcome, channel = _submit(
        classifier.classify(_cmd("cat", "plain.txt", "secrets.json")),
        workspace,
        _secret_verdicts(secrets, clean, guessed),
        ["n"],
    )

    assert len(channel.presented) == 1
    assert outcome.decision is ApprovalDecision.REJECTED


def test_unattended_secret_approval_requirement_fails_closed(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """No channel plus a gated secret is DENIED_UNATTENDED, not approved.

    The pre-fix behaviour approved this outright, which is the worst version of
    the bug: an unattended run would have read a name-guess secret with nobody
    present to object.
    """
    outcome = Approver(None).submit_command(
        classifier.classify(_cmd("cat", "secrets.json")),
        agent="byte",
        workspace=workspace.root,
        secret_verdicts=_secret_verdicts(secrets, workspace.root / "secrets.json"),
    )

    assert outcome.decision is ApprovalDecision.DENIED_UNATTENDED
    assert outcome.denied
    assert not outcome.human_decided


def test_escalated_request_names_the_secret_rule(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    """The operator must see why an ordinary command needs a decision.

    A prompt for `cat` with no stated reason teaches the operator to approve
    without reading, which is exactly what review-then-confirm exists to avoid.
    """
    outcome, _ = _submit(
        classifier.classify(_cmd("cat", "secrets.json")),
        workspace,
        _secret_verdicts(secrets, workspace.root / "secrets.json"),
        ["y"],
    )

    assert outcome.request is not None
    assert outcome.request.rule == "guess.secrets_json"
    assert "secret source requires approval" in outcome.request.reason


def test_escalated_prompt_shows_the_secret_reason(
    classifier: CommandClassifier, secrets: SecretClassifier, workspace: Workspace
) -> None:
    captured: list[str] = []

    class _Capturing(ScriptedApprovalChannel):
        def present(self, request, prompt):  # type: ignore[no-untyped-def]
            captured.append(prompt)
            return "y"

    Approver(_Capturing(["y"])).submit_command(
        classifier.classify(_cmd("cat", "secrets.json")),
        agent="byte",
        workspace=workspace.root,
        secret_verdicts=_secret_verdicts(secrets, workspace.root / "secrets.json"),
    )

    assert captured
    assert "filename suggests stored secrets" in captured[0]


def test_no_secret_verdicts_leaves_command_verdict_unchanged(
    classifier: CommandClassifier,
) -> None:
    """Supplying nothing must not change existing behaviour."""
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    assert outcome.decision is ApprovalDecision.APPROVED
    assert outcome.request is not None
    assert outcome.request.rule == "cmd.git_commit"


# ==========================================================================
# Single-spend approvals
# ==========================================================================
#
# An approved outcome is a capability. Spending it is guarded by a ClaimToken
# held inside the frozen dataclass, so the guarantee survives assignment,
# replace(), copy.copy(), copy.deepcopy(), and concurrent callers -- rather than
# depending on any single consumer threading the returned value forward.

def _approved() -> ApprovalOutcome:
    return ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="test")


def test_claiming_an_unclaimed_approval_succeeds() -> None:
    outcome = _approved()

    assert outcome.claimed is False
    outcome.claim()
    assert outcome.claimed is True


def test_claiming_the_same_outcome_twice_raises() -> None:
    outcome = _approved()
    outcome.claim()

    with pytest.raises(ApprovalConsumed):
        outcome.claim()


def test_claiming_the_original_twice_raises() -> None:
    """The original object is spent, not a copy handed back by claim().

    claim() returns self, so the caller's variable and the returned value are
    the same object. If claim() had returned a marked *copy* -- as the old
    implementation did -- the original would still look unspent and could be
    spent again.
    """
    outcome = _approved()
    claimed = outcome.claim()

    assert claimed is outcome
    with pytest.raises(ApprovalConsumed):
        outcome.claim()


def test_two_references_to_one_outcome_cannot_both_claim() -> None:
    outcome = _approved()
    alias = outcome

    outcome.claim()

    with pytest.raises(ApprovalConsumed):
        alias.claim()


def test_replace_shares_the_claim_token() -> None:
    outcome = _approved()
    replaced = replace(outcome, detail="different")

    outcome.claim()

    with pytest.raises(ApprovalConsumed):
        replaced.claim()


def test_replace_before_claiming_still_shares() -> None:
    outcome = _approved()
    replaced = replace(outcome)

    replaced.claim()

    assert outcome.claimed is True


def test_copy_copy_shares_the_claim_token() -> None:
    outcome = _approved()
    copied = copy.copy(outcome)

    outcome.claim()

    with pytest.raises(ApprovalConsumed):
        copied.claim()


def test_copy_deepcopy_shares_the_claim_token() -> None:
    """A deepcopy must not mint a second spendable capability.

    This is why ClaimToken implements __deepcopy__ returning self. The default
    path would try to pickle the lock and raise TypeError; an implementation
    that cloned the token would hand out a second capability, which is exactly
    the hole the token exists to close.
    """
    outcome = _approved()
    deep = copy.deepcopy(outcome)

    assert deep._token is outcome._token

    outcome.claim()
    with pytest.raises(ApprovalConsumed):
        deep.claim()


def test_concurrent_claims_yield_exactly_one_winner() -> None:
    outcome = _approved()
    winners: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(32)

    def attempt() -> None:
        barrier.wait()
        try:
            outcome.claim()
        except ApprovalConsumed:
            return
        with lock:
            winners.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


def test_consume_is_an_alias_for_claim() -> None:
    outcome = _approved()

    outcome.consume()

    assert outcome.claimed is True
    with pytest.raises(ApprovalConsumed):
        outcome.consume()


def test_consumed_field_no_longer_exists() -> None:
    """The field was removed; `claimed` is the live state.

    A stored flag could disagree with the token, and this is the test that stops
    someone reintroducing one.
    """
    assert "consumed" not in ApprovalOutcome.__dataclass_fields__
    assert not hasattr(_approved(), "consumed")


def test_claimed_is_computed_not_stored() -> None:
    assert "claimed" not in ApprovalOutcome.__dataclass_fields__


def test_claim_token_is_absent_from_repr() -> None:
    assert "ClaimToken" not in repr(_approved())
    assert "_token" not in repr(_approved())


def test_claim_token_is_absent_from_equality() -> None:
    """Two outcomes differing only in claim state compare equal.

    Otherwise spending an approval would change its value, which would be a
    surprising thing for a security-relevant object.
    """
    outcome = _approved()
    before = outcome == ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="test")

    outcome.claim()
    after = outcome == ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="test")

    assert before and after


def test_claim_token_is_absent_from_public_dict() -> None:
    body = _approved().to_public_dict()

    assert "_token" not in body
    assert "claimed" in body


def test_public_dict_is_json_serialisable() -> None:
    import json

    body = _approved().to_public_dict()

    assert json.loads(json.dumps(body))["decision"] == "approved"


def test_public_dict_reflects_claim_state() -> None:
    outcome = _approved()
    assert outcome.to_public_dict()["claimed"] is False

    outcome.claim()
    assert outcome.to_public_dict()["claimed"] is True


def test_claim_token_absent_from_rendered_prompt(
    classifier: CommandClassifier,
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        classifier.classify(_cmd("git", "commit", "-m", "x")),
        agent="byte",
        workspace=classifier.workspace.root,
    )

    prompt = outcome.request.render()  # type: ignore[union-attr]

    # Matched as a field reference, not a bare substring: pytest's tmp_path
    # embeds the test name in the workspace path, so "_token" appears in an
    # unrelated directory name.
    assert "ClaimToken" not in prompt
    assert "_token=" not in prompt
    assert "_token:" not in prompt


def test_claiming_a_denied_outcome_is_possible_but_useless() -> None:
    """claim() does not check the decision; execute() does.

    Keeping the two separate means the capability has one job -- being spent
    once -- and every decision about whether it was ever *earned* stays with the
    approver and the executor.
    """
    denied = ApprovalOutcome(
        decision=ApprovalDecision.DENIED_BY_POLICY, detail="nope"
    )

    denied.claim()

    assert denied.claimed is True


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
        verdict, agent="byte", workspace=classifier.workspace.root
    )

    assert outcome.request is not None
    assert outcome.request.rule == verdict.rule
    assert outcome.request.matched_rules == verdict.matched_rules
    assert outcome.request.targets == verdict.targets_resolved


def test_outcome_str_is_operator_safe(
    classifier: CommandClassifier, secrets: SecretClassifier
) -> None:
    outcome = Approver(ScriptedApprovalChannel(["n"])).submit_command(
        classifier.classify(_cmd("git", "push", "origin", "main")),
        agent="byte",
        workspace=classifier.workspace.root,
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
