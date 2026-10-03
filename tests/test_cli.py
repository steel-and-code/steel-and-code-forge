"""Tests for the Forge terminal entry point.

Two things are under test here, and the second matters more than the first.

The first is behaviour: parsing, exit codes, and the six rendered states. The
channel is injected for every approval case, so no test needs a TTY.

The second is architecture. The CLI claims to be a thin adapter, and a claim
like that decays silently unless something checks it. The guards at the bottom
of this file assert that the CLI holds no policy of its own, builds no approval
requests, cannot execute, and reports the rule the stack actually decided rather
than a rule it invented. Those are the tests most likely to catch a future
regression that every other test here would happily accept.
"""

from __future__ import annotations

import importlib
import io
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from forge.cli import render
from forge.cli.main import DEFAULT_AGENT, WORKSPACE_ENV, build_parser, main
from forge.tools.approval import ScriptedApprovalChannel

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _module(dotted: str) -> ModuleType:
    """Import a module by name, bypassing package attribute re-exports.

    ``forge.cli.__init__`` re-exports the ``main`` *function*, so a plain
    ``import forge.cli.main as m`` binds the function and ``m.__file__`` fails.
    The architecture guards below read source text, so they need the module.
    """
    return importlib.import_module(dotted)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Path:
    """A workspace with one clean file, one name-guess, one structural secret."""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "plain.txt").write_text("ordinary\n")
    (root / "secrets.json").write_text('{"api_key": "AKIAEXAMPLE-not-real"}\n')
    (root / ".env").write_text("TOKEN=not-a-real-value\n")
    (root / "key.pem").write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nnotarealkey\n"
    )
    return root


class Run:
    """The result of one CLI invocation."""

    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json(self) -> dict[str, object]:
        parsed = json.loads(self.out)
        assert isinstance(parsed, dict), parsed
        return parsed


def run(
    argv: list[str],
    *,
    responses: list[str | None] | None = None,
    env: dict[str, str] | None = None,
) -> Run:
    """Invoke the CLI with an injected channel. No TTY involved."""
    out, err = io.StringIO(), io.StringIO()
    channel = (
        ScriptedApprovalChannel(responses or []) if responses is not None else None
    )
    code = main(argv, channel=channel, out=out, err=err, environ=env or {})
    return Run(code, out.getvalue(), err.getvalue())


def cmd(workspace_dir: Path, *args: str, **flags: str) -> list[str]:
    """Build a `check command` argv.

    Flags are placed before the `--`, which is the point: Forge's options there,
    the command's own arguments after it. Store-true flags take the literal
    string "true" as their only value.
    """
    argv = ["check", "command", "--workspace", str(workspace_dir)]
    for name, value in flags.items():
        flag = "--" + name.replace("_", "-")
        argv.append(flag if value == "true" else f"{flag}={value}")
    argv.append("--")
    argv.extend(args)
    return argv


class RecordingChannel:
    """A channel that records prompts and writes them out, like the real one.

    `ScriptedApprovalChannel` answers without printing, which is right for
    decision tests and wrong for tests that assert the operator sees the prompt.
    This one does both, so prompt content can be asserted without a TTY.
    """

    def __init__(self, responses: list[str | None], out: io.StringIO) -> None:
        self._responses = list(responses)
        self._out = out
        self.prompts: list[str] = []
        self.requests: list[object] = []

    @property
    def available(self) -> bool:
        return True

    def present(self, request, prompt: str) -> str | None:  # type: ignore[no-untyped-def]
        self.prompts.append(prompt)
        self.requests.append(request)
        print(prompt, file=self._out)
        if not self._responses:
            return None
        return self._responses.pop(0)


def run_recording(argv: list[str], responses: list[str | None]) -> Run:
    """Invoke with a channel that prints the prompt it is given."""
    out, err = io.StringIO(), io.StringIO()
    channel = RecordingChannel(responses, out)
    code = main(argv, channel=channel, out=out, err=err, environ={})
    return Run(code, out.getvalue(), err.getvalue())


# ==========================================================================
# Argument parsing
# ==========================================================================


def test_parser_exposes_check_with_two_subjects() -> None:
    parser = build_parser()
    args = parser.parse_args(["check", "command", "--workspace", "/tmp", "--", "ls"])

    assert args.subcommand == "check"
    assert args.leaf == "command"
    assert args.workspace == "/tmp"


def test_shared_options_work_after_the_leaf_subcommand(workspace_dir: Path) -> None:
    """The canonical placement is the only placement.

    Options are attached to the leaf parsers through a parent, so
    `check command --workspace` works without the global-parser ordering
    requirement that would make the documented form merely conventional.
    """
    argv = [
        "check",
        "command",
        "--workspace",
        str(workspace_dir),
        "--agent",
        "someone",
        "--json",
        "--classify-only",
        "--",
        "ls",
    ]
    args = build_parser().parse_args(argv)

    assert args.workspace == str(workspace_dir)
    assert args.agent == "someone"
    assert args.as_json is True
    assert args.classify_only is True


def test_program_and_arguments_are_captured_after_the_separator(
    workspace_dir: Path,
) -> None:
    args = build_parser().parse_args(
        ["check", "command", "--workspace", str(workspace_dir), "--", "rm", "-rf", "build"]
    )

    assert args.argv == ["--", "rm", "-rf", "build"]


def test_command_flags_are_not_parsed_as_forge_flags(
    workspace_dir: Path,
) -> None:
    """`-q` belongs to pytest, not to Forge.

    Without the -- separator this is the bug the separator exists to prevent.
    `--classify-only` is used so the test measures parsing rather than the
    approval step: pytest is now approval-required as repository-code
    execution, so an interactive run would fail closed with exit 1 and the exit
    code would say nothing about argument parsing.
    """
    result = run(
        [
            "check",
            "command",
            "--workspace",
            str(workspace_dir),
            "--json",
            "--classify-only",
            "--",
            "pytest",
            "tests/",
            "-q",
        ]
    )

    assert result.code == 3
    assert result.json()["command"] == "pytest tests/ -q"


def test_check_path_accepts_multiple_paths(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "plain.txt",
            ".env",
        ]
    )

    assert result.code == render.EXIT_REFUSED
    assert ".env" in result.out


def test_missing_separator_is_a_usage_error(workspace_dir: Path) -> None:
    result = run(["check", "command", "--workspace", str(workspace_dir), "pytest", "-q"])

    assert result.code == render.EXIT_USAGE
    assert "--" in result.err


def test_no_command_after_separator_is_a_usage_error(workspace_dir: Path) -> None:
    result = run(["check", "command", "--workspace", str(workspace_dir), "--"])

    assert result.code == render.EXIT_USAGE
    assert "no command given" in result.err


def test_missing_workspace_is_a_usage_error(tmp_path: Path) -> None:
    result = run(["check", "command", "--", "ls"], env={})

    assert result.code == render.EXIT_USAGE
    assert WORKSPACE_ENV in result.err


def test_workspace_comes_from_the_environment(tmp_path: Path) -> None:
    root = tmp_path / "envws"
    root.mkdir()
    (root / "f.txt").write_text("x\n")

    result = run(
        ["check", "path", "--classify-only", "f.txt"],
        env={WORKSPACE_ENV: str(root)},
    )

    assert result.code == 0
    assert "ALLOW" in result.out


def test_explicit_workspace_beats_the_environment(tmp_path: Path) -> None:
    root = tmp_path / "flagws"
    root.mkdir()
    (root / "f.txt").write_text("x\n")

    result = run(
        ["check", "path", "--workspace", str(root), "--classify-only", "f.txt"],
        env={WORKSPACE_ENV: str(tmp_path / "ignored")},
    )

    assert result.code == 0
    assert "ALLOW" in result.out


def test_unknown_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        run(["nonsense"])

    assert excinfo.value.code == render.EXIT_USAGE


def test_bare_invocation_prints_status_and_exits_zero() -> None:
    result = run([])

    assert result.code == render.EXIT_OK
    assert "current state" in result.out.lower()


def test_status_lists_the_two_entry_points() -> None:
    """The banner must distinguish what classifies from what executes.

    `forge check` still executes nothing and `forge byte` does execute, so a
    reader who saw one and assumed the other would be wrong about the one they
    did not use.
    """
    result = run([])

    assert "forge check" in result.out
    assert "forge byte" in result.out
    assert "executes nothing" in result.out
    assert "does execute" in result.out


# ==========================================================================
# forge byte
# ==========================================================================


def test_byte_requires_an_explicit_workspace(workspace_dir: Path) -> None:
    """No workspace, no run. Not inferred from the current directory."""
    result = run(["byte", "do something"], env={})

    assert result.code == render.EXIT_USAGE
    assert "workspace" in result.err


def test_byte_requires_a_task(workspace_dir: Path) -> None:
    result = run(
        ["byte", "--workspace", str(workspace_dir)],
        env={"FORGE_OPENROUTER_API_KEY": "x"},
    )

    assert result.code == render.EXIT_USAGE
    assert "no task given" in result.err


def test_byte_rejects_task_from_two_sources(workspace_dir: Path, tmp_path: Path) -> None:
    """Ambiguous input is refused rather than silently preferring one source."""
    task_file = tmp_path / "task.md"
    task_file.write_text("from a file\n")

    result = run(
        [
            "byte",
            "--workspace",
            str(workspace_dir),
            "--task-file",
            str(task_file),
            "from argv",
        ],
        env={"FORGE_OPENROUTER_API_KEY": "x"},
    )

    assert result.code == render.EXIT_USAGE
    assert "not both" in result.err


def test_byte_reads_a_task_from_a_file(workspace_dir: Path, tmp_path: Path) -> None:
    """`--task-file` is accepted; the run then fails at the provider, not here."""
    task_file = tmp_path / "task.md"
    task_file.write_text("summarise the repository\n")

    result = run(
        [
            "byte",
            "--workspace",
            str(workspace_dir),
            "--task-file",
            str(task_file),
        ],
        env={},
    )

    # No key, so the gateway refuses before any request. That the failure is the
    # credential and not the task file is what this asserts.
    assert "FORGE_OPENROUTER_API_KEY" in result.err


def test_byte_missing_task_file_is_reported(workspace_dir: Path) -> None:
    result = run(
        [
            "byte",
            "--workspace",
            str(workspace_dir),
            "--task-file",
            str(workspace_dir / "does-not-exist.md"),
        ],
        env={"FORGE_OPENROUTER_API_KEY": "x"},
    )

    assert result.code == render.EXIT_USAGE
    assert "could not be read" in result.err


def test_byte_missing_key_fails_before_any_request(workspace_dir: Path) -> None:
    """A missing provider key is a visible startup failure, not a silent one.

    No network call is attempted, which is what makes this safe to assert.
    """
    result = run(["byte", "--workspace", str(workspace_dir), "hello"], env={})

    assert result.code == render.EXIT_USAGE
    assert "FORGE_OPENROUTER_API_KEY" in result.err


def test_byte_missing_key_error_does_not_echo_other_secrets(workspace_dir: Path) -> None:
    """The failure names the variable and no value from the environment."""
    result = run(
        ["byte", "--workspace", str(workspace_dir), "hello"],
        env={
            "FORGE_OPENROUTER_API_KEY": "   ",
            "AWS_SECRET_ACCESS_KEY": "aws-SECRET-999",
        },
    )

    assert "SECRET-999" not in result.err
    assert "SECRET-999" not in result.out


def test_byte_does_not_print_the_key(workspace_dir: Path) -> None:
    """Even on a failure path, the credential never reaches a stream."""
    key = "sk-or-v1-CLI-SHOULD-NOT-PRINT"
    result = run(
        ["byte", "--workspace", str(workspace_dir), "hello"],
        env={"FORGE_OPENROUTER_API_KEY": key},
    )

    assert key not in result.out
    assert key not in result.err


# ==========================================================================
# Classification outcomes
# ==========================================================================


def test_allowed_command_reports_allow(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "ls", "-la"))

    assert result.code == render.EXIT_OK
    assert result.out.startswith("ALLOW")


def test_allowed_command_never_shows_a_prompt(workspace_dir: Path) -> None:
    """An allow must not need a human. A prompt on every command is a prompt
    nobody reads, which is the reasoning the v1 permission model uses."""
    result = run(cmd(workspace_dir, "ls", "-la"), responses=["y"])

    assert result.code == render.EXIT_OK
    assert "APPROVAL REQUIRED" not in result.out


def test_classify_only_reports_approval_required_and_exits_three(
    workspace_dir: Path,
) -> None:
    result = run(cmd(workspace_dir, "rm", "-rf", "build", classify_only="true"))

    assert result.code == render.EXIT_NEEDS_APPROVAL
    assert result.out.startswith("APPROVAL REQUIRED")
    assert "cmd.delete_recursive" in result.out


def test_classify_only_on_allow_stays_exit_zero(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "ls", classify_only="true"))

    assert result.code == render.EXIT_OK
    assert result.out.startswith("ALLOW")


def test_classify_only_shows_resolved_targets(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "rm", "-rf", "build", classify_only="true"))

    assert str(workspace_dir / "build") in result.out


def test_classify_only_states_targets_when_not_determinable(
    workspace_dir: Path,
) -> None:
    result = run(cmd(workspace_dir, "git", "push", classify_only="true"))

    assert "not determinable" in result.out


def test_flag_order_does_not_change_classification(workspace_dir: Path) -> None:
    """`rm build -rf` must classify identically to `rm -rf build`.

    The classifier extracts flags from every position; this asserts the CLI
    preserves argv order faithfully enough for that to hold.
    """
    first = run(cmd(workspace_dir, "rm", "-rf", "build", classify_only="true", json="true"))
    second = run(cmd(workspace_dir, "rm", "build", "-rf", classify_only="true", json="true"))

    assert first.json()["rule"] == second.json()["rule"]
    assert first.json()["state"] == second.json()["state"]


# ==========================================================================
# Approval outcomes
# ==========================================================================


def test_operator_approval_reports_approved(workspace_dir: Path) -> None:
    result = run(
        cmd(workspace_dir, "git", "commit", "-m", "x"), responses=["y"]
    )

    assert result.code == render.EXIT_OK
    assert result.out.startswith("APPROVED")


def test_approved_output_says_execution_is_not_implemented(
    workspace_dir: Path,
) -> None:
    """The sentence that stops an operator believing their `rm -rf` ran.

    Approving something that then silently did nothing would be worse than no
    approval at all, so this assertion is load-bearing rather than cosmetic.
    """
    result = run(
        cmd(workspace_dir, "rm", "-rf", "build"), responses=["yes"]
    )

    assert result.code == render.EXIT_OK
    assert "not implemented" in result.out


def test_approved_output_states_nothing_ran(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "git", "commit", "-m", "x"), responses=["y"])

    assert "nothing ran" in result.out


def test_operator_rejection_reports_rejected(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "git", "commit", "-m", "x"), responses=["n"])

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("REJECTED")


@pytest.mark.parametrize("response", ["", "maybe", "yeah", "ok", None])
def test_non_affirmative_responses_all_reject(
    workspace_dir: Path, response: str | None
) -> None:
    result = run(
        cmd(workspace_dir, "git", "commit", "-m", "x"), responses=[response]
    )

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("REJECTED")


def test_interactive_approval_shows_the_real_prompt(workspace_dir: Path) -> None:
    """The operator sees the approval layer's own prompt, not a CLI paraphrase."""
    result = run_recording(cmd(workspace_dir, "git", "commit", "-m", "x"), ["y"])

    assert "APPROVAL REQUIRED" in result.out
    assert "Requesting agent" in result.out
    assert "Affected targets" in result.out


def test_prompt_names_the_cli_requester_not_byte(workspace_dir: Path) -> None:
    """An operator-invoked check reports `forge-cli`, not `byte`.

    Still correct now that a Byte runtime exists: this is `forge check`, run by
    the operator, so attributing the request to Byte would be a fabrication an
    operator could act on. `forge byte` reports `byte` because there the
    attribution is true.
    """
    result = run_recording(cmd(workspace_dir, "git", "commit", "-m", "x"), ["y"])

    assert DEFAULT_AGENT in result.out
    assert "from byte?" not in result.out


def test_agent_flag_overrides_the_requester(workspace_dir: Path) -> None:
    result = run_recording(
        cmd(workspace_dir, "git", "commit", "-m", "x", agent="someone"), ["y"]
    )

    assert "someone" in result.out


def test_prompt_states_that_no_reason_was_supplied(workspace_dir: Path) -> None:
    """The CLI has no agent-supplied reason and must not invent one."""
    result = run_recording(cmd(workspace_dir, "git", "commit", "-m", "x"), ["y"])

    assert "not supplied" in result.out


def test_unattended_fails_closed(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "git", "push", unattended="true"))

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED UNATTENDED")


def test_unattended_shows_no_prompt(workspace_dir: Path) -> None:
    result = run(
        cmd(workspace_dir, "git", "push", unattended="true"), responses=["y"]
    )

    assert "Requesting agent" not in result.out


def test_unattended_says_the_operation_did_not_run(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "git", "push", unattended="true"))

    assert "not run" in result.out


def test_unattended_on_an_allowed_command_still_succeeds(workspace_dir: Path) -> None:
    """Unattended fails closed for gated work only; routine work needs no human."""
    result = run(cmd(workspace_dir, "ls", "-la", unattended="true"))

    assert result.code == render.EXIT_OK
    assert result.out.startswith("ALLOW")


# ==========================================================================
# Policy denial
# ==========================================================================


def test_denied_command_reports_denied_by_policy(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "sudo", "rm", "build"))

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED BY POLICY")


def test_policy_denial_shows_no_prompt_even_with_an_answer_ready(
    workspace_dir: Path,
) -> None:
    """A denial must not be reachable by an operator saying yes."""
    result = run(cmd(workspace_dir, "sudo", "rm", "build"), responses=["y"])

    assert result.out.startswith("DENIED BY POLICY")
    assert "APPROVAL REQUIRED" not in result.out


def test_policy_denial_names_the_rule_that_denied(workspace_dir: Path) -> None:
    """A denial that says which rule fired is fixable; one that says 'error' is not."""
    result = run(cmd(workspace_dir, "sudo", "rm", "build"))

    assert "cmd.sudo" in result.out


def test_out_of_workspace_path_is_denied(tmp_path: Path, workspace_dir: Path) -> None:
    """Containment runs before command classification, per the pipeline order."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n")

    result = run(cmd(workspace_dir, "cat", str(outside)))

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED BY POLICY")


# ==========================================================================
# Secret classification
# ==========================================================================


def test_structural_secret_is_denied_by_policy(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            ".env",
        ]
    )

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED BY POLICY")
    assert "secret.env" in result.out


def test_name_guess_secret_requires_approval(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "secrets.json",
        ]
    )

    assert result.code == render.EXIT_NEEDS_APPROVAL
    assert result.out.startswith("APPROVAL REQUIRED")
    assert "guess.secrets_json" in result.out


def test_clean_file_is_allowed(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "plain.txt",
        ]
    )

    assert result.code == render.EXIT_OK
    assert result.out.startswith("ALLOW")


def test_secret_denial_outranks_the_command_classification(workspace_dir: Path) -> None:
    """`cat .env` is an ordinary command reading a structural secret.

    The secret check sits above the prompt in the pipeline, so the denial wins
    even though nothing about `cat` is dangerous.
    """
    result = run(cmd(workspace_dir, "cat", ".env"))

    assert result.out.startswith("DENIED BY POLICY")
    assert "secret.env" in result.out


def test_name_guess_secret_escalates_an_otherwise_allowed_command(
    workspace_dir: Path,
) -> None:
    """`cat secrets.json` needs approval even though the command is ordinary.

    Integration coverage for the approval-layer rule that a supplied
    approval-required secret verdict gates the whole operation. Before that rule
    this reported ALLOW and read the file with nobody asked.
    """
    result = run(cmd(workspace_dir, "cat", "secrets.json", classify_only="true"))

    assert result.code == render.EXIT_NEEDS_APPROVAL
    assert result.out.startswith("APPROVAL REQUIRED")
    assert "guess.secrets_json" in result.out


def test_name_guess_secret_prompts_interactively(workspace_dir: Path) -> None:
    result = run_recording(cmd(workspace_dir, "cat", "secrets.json"), ["y"])

    assert "APPROVAL REQUIRED" in result.out
    assert "Requesting agent" in result.out
    # Exact footer, as a whole line. Not endswith: the CLI prints the
    # no-execution note after the prompt, so the prompt is not last.
    assert (
        "Approve request from forge-cli? "
        "Type 'y'/'yes' to approve or 'n'/'no' to reject:"
    ) in result.out.splitlines()
    assert "Type 'y' or 'yes'" not in result.out


def test_name_guess_secret_can_be_rejected(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "cat", "secrets.json"), responses=["n"])

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("REJECTED")


def test_name_guess_secret_fails_closed_when_unattended(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "cat", "secrets.json", unattended="true"))

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED UNATTENDED")


def test_escalated_command_shows_one_prompt_not_one_per_target(
    workspace_dir: Path,
) -> None:
    """One command, one decision."""
    extra = workspace_dir / "credentials.yaml"
    extra.write_text("name: value\n")

    channel = ScriptedApprovalChannel(["y"])
    out, err = io.StringIO(), io.StringIO()
    code = main(
        cmd(workspace_dir, "cat", "secrets.json", "credentials.yaml"),
        channel=channel,
        out=out,
        err=err,
        environ={},
    )

    assert len(channel.presented) == 1
    assert code == render.EXIT_OK


def test_cli_does_not_implement_the_ranking_itself() -> None:
    """The rule belongs to the approval layer so every caller inherits it.

    If the CLI started ranking verdicts, the next caller would have to
    reimplement it, and one that forgot would reopen the hole.
    """
    cli_main = _module("forge.cli.main")
    source = Path(str(cli_main.__file__)).read_text()

    assert "strictest(" not in source
    assert "DENIED_BY_POLICY" not in source


def test_secret_denied_command_shows_no_prompt(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "cat", ".env"), responses=["y"])

    assert "APPROVAL REQUIRED" not in result.out


def test_private_key_content_is_denied(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "key.pem",
        ]
    )

    assert result.code == render.EXIT_REFUSED
    assert result.out.startswith("DENIED BY POLICY")


def test_relative_path_resolves_against_the_workspace(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A typed relative path means the workspace, not the shell's directory.

    Over SSH the shell directory is often somewhere unrelated. Classifying a
    different file than the operator named would be a silent wrong answer.
    """
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secrets.json").write_text("{}")

    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "secrets.json",
        ]
    )

    assert "guess.secrets_json" in result.out
    assert str(workspace_dir) in result.out


def test_multiple_paths_report_the_strictest_outcome(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "plain.txt",
            ".env",
        ]
    )

    assert result.code == render.EXIT_REFUSED
    assert result.out.count("DENIED BY POLICY") == 1


# ==========================================================================
# Exit-code contract
# ==========================================================================


def test_exit_codes_are_distinct_and_stable() -> None:
    assert render.EXIT_OK == 0
    assert render.EXIT_REFUSED == 1
    assert render.EXIT_USAGE == 2
    assert render.EXIT_NEEDS_APPROVAL == 3
    assert render.EXIT_INTERNAL == 4


def test_all_three_refusals_share_one_exit_code() -> None:
    """Rejected, denied by policy, and denied unattended all exit 1.

    They are different events and the rendered state distinguishes them, but the
    exit-code contract is deliberately not expanded per failure mode.
    """
    assert (
        render.EXIT_FOR_STATE[render.STATE_REJECTED]
        == render.EXIT_FOR_STATE[render.STATE_DENIED_BY_POLICY]
        == render.EXIT_FOR_STATE[render.STATE_DENIED_UNATTENDED]
        == render.EXIT_REFUSED
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        (render.STATE_ALLOW, render.EXIT_OK),
        (render.STATE_APPROVAL_REQUIRED, render.EXIT_NEEDS_APPROVAL),
        (render.STATE_APPROVED, render.EXIT_OK),
        (render.STATE_REJECTED, render.EXIT_REFUSED),
        (render.STATE_DENIED_BY_POLICY, render.EXIT_REFUSED),
        (render.STATE_DENIED_UNATTENDED, render.EXIT_REFUSED),
    ],
)
def test_every_state_maps_to_an_exit_code(state: str, expected: int) -> None:
    assert render.EXIT_FOR_STATE[state] == expected


# ==========================================================================
# Rendering
# ==========================================================================


def test_output_contains_no_ansi_escape_sequences(workspace_dir: Path) -> None:
    """Forge is expected to run over SSH on a VPS operator's terminal.

    A colour code that renders as literal escape text on some hosts is worse
    than no colour.
    """
    results = [
        run(cmd(workspace_dir, "ls")),
        run(cmd(workspace_dir, "rm", "-rf", "build", classify_only="true")),
        run(cmd(workspace_dir, "sudo", "rm", "x")),
        run(cmd(workspace_dir, "git", "push", unattended="true")),
    ]

    for result in results:
        assert not ANSI.search(result.out)


def test_states_are_the_first_line(workspace_dir: Path) -> None:
    """State first, details beneath: scannable when comparing two runs."""
    results = [
        run(cmd(workspace_dir, "ls")),
        run(cmd(workspace_dir, "rm", "-rf", "build", classify_only="true")),
        run(cmd(workspace_dir, "sudo", "rm", "x")),
        run(cmd(workspace_dir, "git", "push", unattended="true")),
    ]

    for result in results:
        assert result.out.splitlines()[0] in render.EXIT_FOR_STATE


def test_each_state_maps_from_exactly_one_decision() -> None:
    """No ambiguity in the decision-to-label mapping."""
    from forge.tools.approval import ApprovalDecision, ApprovalOutcome

    expected = {
        ApprovalDecision.REJECTED: render.STATE_REJECTED,
        ApprovalDecision.DENIED_BY_POLICY: render.STATE_DENIED_BY_POLICY,
        ApprovalDecision.DENIED_UNATTENDED: render.STATE_DENIED_UNATTENDED,
    }
    for decision, state in expected.items():
        outcome = ApprovalOutcome(decision=decision, detail="")
        assert render.state_for_outcome(outcome) == state


def test_operator_approved_is_distinct_from_allow() -> None:
    """An automatic approval and an operator's yes are different events."""
    from forge.tools.approval import ApprovalDecision, ApprovalOutcome

    automatic = ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="")
    operator = ApprovalOutcome(
        decision=ApprovalDecision.APPROVED, detail="", operator_response="y"
    )

    assert render.state_for_outcome(automatic) == render.STATE_ALLOW
    assert render.state_for_outcome(operator) == render.STATE_APPROVED


# ==========================================================================
# JSON output
# ==========================================================================


def test_json_is_one_parseable_object(workspace_dir: Path) -> None:
    result = run(cmd(workspace_dir, "ls", json="true"))

    body = result.json()

    assert isinstance(body, dict)
    assert body["state"] == "ALLOW"
    assert body["exit_code"] == 0


def test_json_state_tokens_are_underscored(workspace_dir: Path) -> None:
    """Human labels have spaces; JSON tokens do not."""
    result = run(cmd(workspace_dir, "sudo", "rm", "x", json="true"))

    assert result.json()["state"] == "DENIED_BY_POLICY"


def test_json_reports_the_stack_rule_not_a_cli_invention(
    workspace_dir: Path,
) -> None:
    result = run(cmd(workspace_dir, "sudo", "rm", "x", json="true"))

    assert result.json()["rule"] == "cmd.sudo"


def test_json_includes_the_requester_response(workspace_dir: Path) -> None:
    result = run(
        cmd(workspace_dir, "git", "commit", "-m", "x", json="true"),
        responses=["y"],
    )

    body = result.json()

    assert body["state"] == "APPROVED"
    assert body["requester_response"] == "y"


def test_json_distinguishes_the_three_refusals(workspace_dir: Path) -> None:
    rejected = run(
        cmd(workspace_dir, "git", "commit", "-m", "x", json="true"), responses=["n"]
    ).json()
    policy = run(cmd(workspace_dir, "sudo", "rm", "x", json="true")).json()
    unattended = run(
        cmd(workspace_dir, "git", "push", "--unattended", json="true")
    ).json()

    assert rejected["state"] == "REJECTED"
    assert policy["state"] == "DENIED_BY_POLICY"
    assert unattended["state"] == "DENIED_UNATTENDED"


def test_json_check_path_emits_one_object_per_path(workspace_dir: Path) -> None:
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "--json",
            "plain.txt",
            ".env",
        ]
    )

    lines = [line for line in result.out.splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_json_never_contains_a_credential_value(workspace_dir: Path) -> None:
    """Non-exposure. The JSON body is a surface like any other."""
    result = run(
        [
            "check",
            "path",
            "--workspace",
            str(workspace_dir),
            "--classify-only",
            "--json",
            "secrets.json",
        ]
    )

    assert "AKIAEXAMPLE-not-real" not in result.out


# ==========================================================================
# Architecture guards
# ==========================================================================


def test_cli_imports_no_execution_or_network_modules() -> None:
    """The CLI cannot run anything.

    If a future change adds a runner, a client, or a socket import here, this is
    where it should fail first.
    """
    cli_main = _module("forge.cli.main")
    cli_render = _module("forge.cli.render")

    source = Path(cli_main.__file__).read_text() + Path(cli_render.__file__).read_text()

    for banned in (
        "import subprocess",
        "import socket",
        "import urllib",
        "import http",
        "import requests",
        "import shutil",
        "os.system",
        "os.exec",
        "os.spawn",
        "pty.spawn",
    ):
        assert banned not in source, banned


def test_cli_never_constructs_an_approval_request() -> None:
    """Requests are built by the approval layer from a verdict.

    Constructing one here would route around the guarantee that a denial cannot
    become a prompt.
    """
    cli_main = _module("forge.cli.main")

    source = Path(cli_main.__file__).read_text()

    assert "ApprovalRequest(" not in source
    assert "from ..tools.approval import" in source


def test_cli_goes_through_the_approver_not_the_verdict() -> None:
    """The CLI submits verdicts; it does not decide from them."""
    cli_main = _module("forge.cli.main")

    source = Path(cli_main.__file__).read_text()

    assert "submit_command" in source
    assert "submit_secret" in source


def test_cli_defines_no_classification_rules(workspace_dir: Path) -> None:
    """The CLI holds no rules: it cannot answer for a command it was not shown.

    A regression test against the specific commands the tests happen to use. If
    someone adds a special case for one of these, the CLI has started deciding.
    """
    result = run(cmd(workspace_dir, "sudo", "rm", "x"))

    # The reason must come from the classifier, not from the CLI. A CLI-side
    # rule would produce a reason string that does not name a classifier rule.
    assert "cmd.sudo" in result.out


def test_render_module_is_pure(workspace_dir: Path) -> None:
    """render.py formats; it must not reach back into the stack to decide."""
    cli_render = _module("forge.cli.render")

    source = Path(cli_render.__file__).read_text()

    for banned in ("import subprocess", "Workspace(", "CommandClassifier(", "SecretClassifier("):
        assert banned not in source, banned


def test_requester_default_is_the_cli_not_an_agent() -> None:
    """A CLI that claims to be Byte would be reporting a request nobody made."""
    assert DEFAULT_AGENT == "forge-cli"