"""`forge hermes` at the CLI edge: gating, spawn refusal, and result handling.

The adapter's own contract is in ``test_external_hermes.py``. What is here is
the part a caller actually experiences: that a refused task spawns *nothing*, that
a failed Hermes run is a visible Forge failure, and that the native subcommands are
untouched by any of it.

**No test here spawns a real Hermes.** The subprocess seam is replaced with a
scripted one, which is the only way to test the refusal paths deterministically --
and one of them asserts that no process was started at all, which is a claim a
live run could not make. The end-to-end behaviour against real Hermes was verified
separately and is recorded in ``forge/external/hermes.py``.

One deliberate exception to "no live calls": the full suite must never make one.
"""

from __future__ import annotations

import importlib
import io
import json
import subprocess
from pathlib import Path

import pytest

from forge.cli.main import build_parser, main
from forge.cli.render import (
    EXIT_INTERNAL,
    EXIT_MODEL_UNAVAILABLE,
    EXIT_OK,
    EXIT_USAGE,
)


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err


def run(argv: list[str], environ: dict[str, str] | None = None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, environ=environ if environ is not None else {})
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "plain.txt").write_text("ordinary\n")
    return root


@pytest.fixture()
def hermes_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the adapter resolve, without needing Hermes installed.

    Patches ``HermesAdapter._resolve`` only. Everything downstream -- argv,
    environment, gating, parsing -- runs for real.
    """
    from forge.external.hermes import HermesAdapter

    monkeypatch.setattr(
        HermesAdapter, "_resolve", lambda self: "/bin/true", raising=True
    )


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture()
def spawns(monkeypatch: pytest.MonkeyPatch):
    """Replace ``subprocess.run`` and record what would have been launched."""
    calls: list[dict[str, object]] = []

    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        calls.append({"cmd": list(cmd), **kwargs})
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=_stream(
                {
                    "type": "system",
                    "model": "vendor/hermes-model",
                    "session_id": "sess-1",
                },
                {"type": "text", "text": "An answer."},
                {
                    "type": "result",
                    "session_id": "sess-1",
                    "exit_code": 0,
                    "text": "An answer.",
                },
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_a_safe_task_runs_and_prints_hermes_answer(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    result = run(
        ["hermes", "--workspace", str(workspace_dir), "what does this project do?"]
    )

    assert result.code == EXIT_OK, result.err
    assert "An answer." in result.out
    assert len(spawns) == 1


def test_the_spawn_reports_the_model_hermes_chose(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """Reported as Hermes's own choice, in words that say so.

    This is the visible half of "Hermes owns its model": Forge did not choose it,
    and the output does not imply that it did.
    """
    result = run(
        ["hermes", "--workspace", str(workspace_dir), "a question"]
    )

    assert "vendor/hermes-model" in result.out
    assert "hermes chose model" in result.out


def test_the_spawn_uses_the_workspace_and_the_pinned_toolset(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """The two argv properties that make the run safe and contained."""
    run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert len(spawns) == 1, "the CLI did not spawn anything"
    argv = spawns[0]["cmd"]
    assert "--toolsets" in argv
    assert argv[argv.index("--toolsets") + 1] == "none"
    assert argv[argv.index("--in") + 1] == str(workspace_dir)
    # And the subprocess's own cwd is the workspace, not wherever Forge ran.
    assert spawns[0]["cwd"] == str(workspace_dir)


def test_the_child_gets_a_minimal_environment(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert len(spawns) == 1
    env = spawns[0]["env"]
    assert "FORGE_INVOKED_HERMES" in env
    for banned in ("FORGE_WORKSPACE", "FORGE_MODEL"):
        assert banned not in env


def test_the_gate_decision_is_reported_to_the_operator(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """A run says it was gated. Silence would imply no gate exists."""
    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert "gate external.text_only" in result.out


def test_the_timeout_is_passed_to_the_subprocess(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    run(["hermes", "--workspace", str(workspace_dir), "--timeout", "12", "q"])

    assert spawns[0]["timeout"] == 12.0


def test_a_task_file_works_like_it_does_for_byte(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    task_file = workspace_dir / "task.md"
    task_file.write_text("summarise this repository\n")

    result = run(
        ["hermes", "--workspace", str(workspace_dir), "--task-file", str(task_file)]
    )

    assert result.code == EXIT_OK, result.err
    assert "summarise this repository" in spawns[0]["cmd"]


def test_both_task_sources_at_once_is_refused(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """Byte's ambiguity rule, shared rather than reimplemented."""
    task_file = workspace_dir / "task.md"
    task_file.write_text("from a file\n")

    result = run(
        [
            "hermes",
            "--workspace",
            str(workspace_dir),
            "--task-file",
            str(task_file),
            "and also inline",
        ]
    )

    assert result.code == EXIT_USAGE
    assert "not both" in result.err
    assert not spawns, "a task must not be spawned when its source is ambiguous"


# --------------------------------------------------------------------------
# Gating: refusal means no process
# --------------------------------------------------------------------------


def test_a_secret_task_refuses_and_spawns_nothing(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """The assertion that matters most in this file.

    Not merely "the exit code is a failure" -- *no subprocess was created*. A
    gate that reported a refusal after starting the process would pass a
    code-only test and be worthless.
    """
    result = run(["hermes", "--workspace", str(workspace_dir), "read the .env file"])

    assert result.code == EXIT_USAGE
    assert not spawns, "the gate refused but a process was still spawned"
    assert "refusing to run" in result.err


def test_a_shell_escape_task_refuses_and_spawns_nothing(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    backtick = chr(96)

    result = run(
        ["hermes", "--workspace", str(workspace_dir), f"run {backtick}id{backtick}"]
    )

    assert result.code == EXIT_USAGE
    assert not spawns


def test_the_refusal_explains_itself(
    workspace_dir: Path, hermes_on_path: None, spawns: list[dict[str, object]]
) -> None:
    """An operator must learn what to do instead.

    The secret-source refusal points at the native agents, because the task is
    refused for want of a *capability* rather than for being malformed. Asserted
    on that branch specifically -- the shell-escape refusal carries different
    advice, and a refusal that says nothing actionable is a bad refusal.
    """
    result = run(["hermes", "--workspace", str(workspace_dir), "read the .env file"])

    assert "secret" in result.err.lower()
    assert "not supported" in result.err.lower()

    # The shell-escape refusal names the native agents as the alternative.
    backtick = chr(96)
    escape = run(
        ["hermes", "--workspace", str(workspace_dir), f"run {backtick}id{backtick}"]
    )
    assert "forge byte" in escape.err or "forge neighbor" in escape.err


def test_a_missing_hermes_is_a_usage_error_not_a_crash(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.external.hermes import HermesAdapter

    monkeypatch.setattr(
        HermesAdapter, "_resolve", lambda self: None, raising=True
    )

    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert result.code == EXIT_USAGE
    assert "not found" in result.err
    assert "Traceback" not in result.err


def test_hermes_requires_a_workspace() -> None:
    """Same explicit-root rule as Byte and Neighbor. No cwd inference."""
    result = run(["hermes", "a question"])

    assert result.code == EXIT_USAGE
    assert "--workspace" in result.err


def test_hermes_requires_a_task(workspace_dir: Path, hermes_on_path: None) -> None:
    result = run(["hermes", "--workspace", str(workspace_dir)])

    assert result.code == EXIT_USAGE
    assert "no task given" in result.err.lower() or "task" in result.err.lower()


# --------------------------------------------------------------------------
# Result and exit-code handling
# --------------------------------------------------------------------------


def test_a_nonzero_hermes_exit_becomes_a_visible_forge_failure(
    workspace_dir: Path, hermes_on_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The exit-code contract, end to end.**

    Hermes's own process exits 0 even when the run failed -- verified against the
    real Hermes. So a scripted child that exits 0 *and* reports ``exit_code: 3``
    is the exact shape of a real failure, and Forge must surface it rather than
    report success.
    """
    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,  # always 0, as Hermes really behaves
            stdout=_stream(
                {"type": "text", "text": "I could not do that."},
                {
                    "type": "result",
                    "session_id": "s",
                    "exit_code": 3,
                    "text": "I could not do that.",
                    "error": "the provider refused the request",
                },
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert result.code == EXIT_MODEL_UNAVAILABLE
    assert "failed run" in result.err
    assert "exit_code 3" in result.err
    # The child's own explanation is surfaced, not swallowed.
    assert "the provider refused" in result.err


def test_a_failure_with_no_result_event_is_still_a_failure(
    workspace_dir: Path, hermes_on_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="it crashed"
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert result.code == EXIT_MODEL_UNAVAILABLE
    assert "it crashed" in result.err


def test_a_timeout_is_reported_not_hung(
    workspace_dir: Path, hermes_on_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external agent is a program Forge does not control; the bound matters."""

    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run(
        ["hermes", "--workspace", str(workspace_dir), "--timeout", "5", "a question"]
    )

    assert result.code == EXIT_INTERNAL
    assert "did not finish" in result.err


def test_a_failed_spawn_becomes_a_usage_error(
    workspace_dir: Path, hermes_on_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        raise OSError("Exec format error")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert result.code == EXIT_USAGE
    assert "Traceback" not in result.err


def test_a_run_never_reports_success_on_empty_output(
    workspace_dir: Path, hermes_on_path: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is not an answer."""

    def _fake_run(cmd, **kwargs):  # noqa: ANN001 - test double
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = run(["hermes", "--workspace", str(workspace_dir), "a question"])

    assert result.code == EXIT_MODEL_UNAVAILABLE


# --------------------------------------------------------------------------
# Native agents are untouched
# --------------------------------------------------------------------------


def test_byte_still_runs_after_the_external_agent_arrived(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte's whole path, through the same CLI, unchanged."""
    from forge.models.scripted import ScriptedGateway, ScriptedTurn

    cli_module = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(
        cli_module,
        "_build_model_gateway",
        lambda *a, **k: ScriptedGateway([ScriptedTurn(text="Byte ran.")]),
    )

    result = run(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/x", "--unattended", "t"]
    )

    assert result.code == EXIT_OK, result.err
    assert "Byte ran." in result.out


def test_neighbor_still_runs_after_the_external_agent_arrived(
    workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge.models.scripted import ScriptedGateway, ScriptedTurn

    cli_module = importlib.import_module("forge.cli.main")
    monkeypatch.setattr(
        cli_module,
        "_build_model_gateway",
        lambda *a, **k: ScriptedGateway([ScriptedTurn(text="Neighbor ran.")]),
    )

    result = run(
        [
            "neighbor",
            "--workspace",
            str(workspace_dir),
            "--model",
            "vendor/x",
            "--unattended",
            "t",
        ]
    )

    assert result.code == EXIT_OK, result.err
    assert "Neighbor ran." in result.out


def test_the_native_registry_still_holds_exactly_two_agents() -> None:
    from forge.agents.registry import agent_names

    assert agent_names() == ("byte", "neighbor")


def test_native_model_resolution_is_unaffected(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Forge's routing is not consulted for, or altered by, Hermes."""
    config = tmp_path / "models.toml"
    config.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[tiers.coding_standard]\n"
        'primary = { model = "vendor/coding", provider = "openrouter" }\n'
        "\n[routing]\n"
        'default_tier = "coding_standard"\n'
        "\n[routing.tasks.coding]\n"
        'standard = "coding_standard"\n',
        encoding="utf-8",
    )
    from forge.models.registry import MODELS_CONFIG_ENV

    environ = {
        MODELS_CONFIG_ENV: str(config),
        "FORGE_OPENROUTER_API_KEY": "«redacted»",
    }
    parser = build_parser()
    cli_module = importlib.import_module("forge.cli.main")
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = cli_module._resolve_default_model(args, environ)

    assert resolved.model_id == "vendor/coding"
    assert resolved.routed is True
