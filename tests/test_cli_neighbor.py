"""`forge neighbor`: selecting a second agent from the terminal.

Byte's CLI tests are in ``test_cli.py`` and its routing tests in
``test_model_default.py``; neither is repeated here. What is tested is what
only becomes true once there is more than one agent:

* the subcommand exists, parses, and takes the same options as `forge byte`;
* it routes by *its own* task kind rather than inheriting Byte's;
* ``--model`` still short-circuits routing for Neighbor exactly as for Byte;
* the approval prompt names Neighbor, not Byte;
* `forge byte` is unchanged.

That last one matters most. ``_run_byte`` was refactored into a shared
``_run_agent``, so "Byte behaves as before" is a claim this change could have
broken quietly, and it is checked here rather than left to inspection.

No test contacts a provider. Model resolution is exercised through the real
registry and router with an injected configuration; no gateway is ever called.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest

from forge.cli.main import (
    BYTE_TASK_KIND,
    DEFAULT_MODEL_ENV,
    MODEL_PROVIDER_ENV,
    NEIGHBOR_TASK_DIFFICULTY,
    NEIGHBOR_TASK_KIND,
    build_parser,
    main,
)
from forge.cli import render
from forge.models.registry import MODELS_CONFIG_ENV
from forge.models.scripted import ScriptedGateway, ScriptedTurn


def _module(dotted: str):
    return importlib.import_module(dotted)


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


def _registry(tmp_path: Path) -> Path:
    """A registry whose ``conversation`` and ``coding`` tasks route apart.

    The two task kinds point at tiers backed by *different* model ids, which is
    what makes "Neighbor routed by its own kind" observable. If both tasks
    resolved to the same model this file could not tell the two agents apart at
    all -- which is exactly the mistake a same-tier fixture would hide.
    """
    path = tmp_path / "models.toml"
    path.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[providers.local]\n"
        'credential_env = ""\n'
        "\n[tiers.general_light]\n"
        'primary = { model = "vendor/light", provider = "openrouter" }\n'
        "\n[tiers.coding_standard]\n"
        'primary = { model = "vendor/coding", provider = "openrouter" }\n'
        "\n[tiers.reasoning_heavy]\n"
        'primary = { model = "vendor/reasoning", provider = "openrouter" }\n'
        "\n[routing]\n"
        'default_tier = "general_light"\n'
        "\n[routing.tasks.coding]\n"
        'light = "general_light"\n'
        'standard = "coding_standard"\n'
        'hard = "reasoning_heavy"\n'
        "\n[routing.tasks.conversation]\n"
        'light = "general_light"\n'
        'standard = "general_light"\n'
        'hard = "coding_standard"\n',
        encoding="utf-8",
    )
    return path


def _env(config: Path, **extra: str) -> dict[str, str]:
    return {
        MODELS_CONFIG_ENV: str(config),
        "FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»",
        **extra,
    }


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_the_neighbor_subcommand_exists() -> None:
    args = build_parser().parse_args(
        ["neighbor", "--workspace", "/tmp", "explain this module"]
    )

    assert args.subcommand == "neighbor"
    assert args.task == "explain this module"
    assert args.workspace == "/tmp"


def test_neighbor_takes_the_same_options_as_byte() -> None:
    """One verb per agent, one option set -- deliberately not per-agent.

    A divergent option set would be Forge inventing a per-agent permission
    surface, which the contract forbids. Compared field-by-field so a new flag
    added to one and not the other fails here.
    """
    parser = build_parser()
    byte_args = parser.parse_args(
        ["byte", "--workspace", "/tmp", "--max-turns", "5", "task"]
    )
    neighbor_args = parser.parse_args(
        ["neighbor", "--workspace", "/tmp", "--max-turns", "5", "task"]
    )

    shared = {"workspace", "task_file", "model", "max_turns", "unattended", "task"}
    for field in shared:
        assert getattr(neighbor_args, field) == getattr(byte_args, field), field


def test_the_status_banner_lists_both_agents() -> None:
    result = run([])

    assert "forge byte" in result.out
    assert "forge neighbor" in result.out


def test_neighbor_requires_a_workspace() -> None:
    """Same explicit-root rule as Byte. No inference from the cwd."""
    result = run(["neighbor", "a task"])

    assert result.code != 0
    assert "--workspace" in result.err


def test_neighbor_reports_a_missing_task_rather_than_running() -> None:
    result = run(["neighbor", "--workspace", "/tmp"])

    assert result.code != 0
    assert "no task given" in result.err.lower() or "task" in result.err.lower()


def test_neighbor_refuses_both_task_sources_at_once(workspace_dir: Path) -> None:
    """The ambiguity rule is shared with Byte, not re-implemented."""
    task_file = workspace_dir / "task.md"
    task_file.write_text("do the thing\n")

    result = run(
        [
            "neighbor",
            "--workspace",
            str(workspace_dir),
            "--task-file",
            str(task_file),
            "and also this",
        ]
    )

    assert result.code != 0
    assert "not both" in result.err


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_neighbor_routes_by_its_own_task_kind(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The load-bearing check: Neighbor does not inherit Byte's routing.

    ``conversation``/``standard`` resolves to ``general_light`` while Byte's
    ``coding``/``standard`` resolves to ``coding_standard``, and the fixture
    gives those tiers different models -- so a regression that routed every
    agent as Byte would produce a visibly wrong model id rather than a
    coincidentally identical one.
    """
    config = _registry(tmp_path)
    parser = build_parser()
    module = _module("forge.cli.main")

    byte_args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])
    neighbor_args = parser.parse_args(
        ["neighbor", "--workspace", str(workspace_dir), "t"]
    )

    from forge.agents.byte import BYTE
    from forge.agents.neighbor import NEIGHBOR

    byte_resolved = module._resolve_default_model(byte_args, _env(config))
    neighbor_resolved = module._resolve_default_model(
        neighbor_args,
        _env(config),
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
    )

    assert byte_resolved.model_id == "vendor/coding"
    assert byte_resolved.tier == "coding_standard"
    assert neighbor_resolved.model_id == "vendor/light"
    assert neighbor_resolved.tier == "general_light"
    assert neighbor_resolved.routed is True
    # And the decision records whose request it was, so an audit can tell them
    # apart even though the routing logic treats them identically.
    assert byte_resolved.model_id != neighbor_resolved.model_id
    assert BYTE.name != NEIGHBOR.name


def test_the_task_kinds_are_byte_and_conversation() -> None:
    """Pinned so a rename cannot silently re-route an agent."""
    assert BYTE_TASK_KIND == "coding"
    assert NEIGHBOR_TASK_KIND == "conversation"
    assert NEIGHBOR_TASK_DIFFICULTY == "standard"


def test_the_conversation_task_kind_exists_in_the_packaged_config() -> None:
    """Reuse, not a new kind.

    An unlisted task kind still routes -- it falls through to ``default_tier``
    -- so a missing entry would be invisible in a passing test and would mean
    Neighbor's model is chosen by accident rather than by configuration.
    """
    import tomllib

    from forge.models.registry import LOCAL_CONFIG_NAME

    packaged = Path(__file__).resolve().parent.parent / "forge" / "models" / LOCAL_CONFIG_NAME
    data = tomllib.loads(packaged.read_text(encoding="utf-8"))

    tasks = data["routing"]["tasks"]
    assert NEIGHBOR_TASK_KIND in tasks, f"{NEIGHBOR_TASK_KIND!r} is not configured"
    assert BYTE_TASK_KIND in tasks
    # And the difficulty resolves, rather than falling back to `standard` by
    # accident and happening to match.
    assert NEIGHBOR_TASK_DIFFICULTY in tasks[NEIGHBOR_TASK_KIND]
def test_an_unusable_tier_names_neighbor_not_byte(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The error must name the agent whose routing failed.

    An operator reading "no usable model for Byte's default routing" while
    running `forge neighbor` has been told the wrong thing about their own
    command.

    The tier is made unusable with ``enabled = false`` -- the operator's own
    switch, which nothing overrides. Building the brokenness that way rather
    than by omitting a credential keeps the test about *routing* rather than
    about a missing key.
    """
    broken = tmp_path / "broken.toml"
    broken.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[providers.local]\n"
        'credential_env = ""\n'
        "\n[tiers.general_light]\n"
        'primary = { model = "vendor/light", provider = "openrouter", '
        'enabled = false }\n'
        "\n[tiers.coding_standard]\n"
        'primary = { model = "vendor/coding", provider = "openrouter" }\n'
        "\n[routing]\n"
        'default_tier = "general_light"\n'
        "\n[routing.tasks.coding]\n"
        'standard = "coding_standard"\n'
        "\n[routing.tasks.conversation]\n"
        'standard = "general_light"\n',
        encoding="utf-8",
    )
    parser = build_parser()
    args = parser.parse_args(["neighbor", "--workspace", str(workspace_dir), "t"])

    from forge.agents.neighbor import NEIGHBOR

    with pytest.raises(Exception) as caught:
        _module("forge.cli.main")._resolve_default_model(
            args,
            _env(broken),
            profile=NEIGHBOR,
            task_kind=NEIGHBOR_TASK_KIND,
            difficulty=NEIGHBOR_TASK_DIFFICULTY,
        )

    message = str(caught.value)
    assert "no usable model" in message.lower()
    assert "neighbor" in message.lower()
    assert NEIGHBOR_TASK_KIND in message

def test_byte_still_routes_to_the_coding_tier(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The pre-existing assertion, re-run after the refactor.

    ``_run_byte`` now delegates to ``_run_agent`` and ``_resolve_default_model``
    gained keyword parameters. This is the test that says the delegation did
    not change what Byte resolves to.
    """
    config = _registry(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.model_id == "vendor/coding"
    assert resolved.tier == "coding_standard"


def test_the_resolver_defaults_to_byte_when_no_profile_is_passed(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Every pre-existing caller omits the new keywords and must get Byte.

    The workbench is one such caller: it is a browser surface onto a Byte run
    and must not be able to start a run on a model `forge byte` would refuse.
    Asserted through the default rather than through the workbench so a change
    to the default is caught here.
    """
    config = _registry(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    without = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    from forge.agents.byte import BYTE

    explicit = _module("forge.cli.main")._resolve_default_model(
        args,
        _env(config),
        profile=BYTE,
        task_kind=BYTE_TASK_KIND,
        difficulty="standard",
    )

    assert without.model_id == explicit.model_id
    assert without.tier == explicit.tier


def test_byte_options_are_unchanged(workspace_dir: Path) -> None:
    """`forge byte` still parses exactly as it did."""
    args = build_parser().parse_args(
        [
            "byte",
            "--workspace",
            str(workspace_dir),
            "--max-turns",
            "7",
            "--unattended",
            "fix the tests",
        ]
    )

    assert args.subcommand == "byte"
    assert args.max_turns == 7
    assert args.unattended is True
    assert args.task == "fix the tests"


# --------------------------------------------------------------------------
# End to end, offline
# --------------------------------------------------------------------------


def _with_scripted_gateway(
    monkeypatch: pytest.MonkeyPatch, *turns: ScriptedTurn
) -> None:
    """Swap the CLI's gateway builder for the offline one.

    `_build_model_gateway` is the single seam between the CLI and a provider, so
    patching it exercises the whole real path -- parsing, model resolution, the
    dispatcher, the runtime, and rendering -- with no network, no key, and no
    cost. Preferred to patching deeper layers precisely because it does not
    skip anything this test is meant to cover.
    """
    module = _module("forge.cli.main")
    gateway = ScriptedGateway(list(turns))
    monkeypatch.setattr(module, "_build_model_gateway", lambda *a, **k: gateway)


def test_a_neighbor_run_completes_end_to_end(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`forge neighbor` runs the agent loop and prints the model's own words."""
    _with_scripted_gateway(
        monkeypatch,
        ScriptedTurn(
            tool_calls=(
                (
                    "finish",
                    {
                        "status": "done",
                        "summary": "It builds and tests Forge.",
                        "validation": "ran the build and tests",
                        "limitations": "none",
                    },
                ),
            )
        ),
    )

    result = run(
        ["neighbor", "--workspace", str(workspace_dir), "--unattended", "what is this?"],
        environ=_env(_registry(tmp_path)),
    )

    assert result.code == 0, result.err
    assert "It builds and tests Forge." in result.out
    # The model's own declaration, not a synthesised summary.
    assert "agent: 1 model turn(s)" in result.out


def test_a_byte_run_is_unchanged_end_to_end(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same path for Byte, after the refactor into ``_run_agent``.

    Byte's run must still reach the scripted gateway, still print the model's
    text, and still report one turn. Anything the shared runner changed for
    Byte would surface here.
    """
    _with_scripted_gateway(
        monkeypatch,
        ScriptedTurn(
            tool_calls=(
                (
                    "finish",
                    {
                        "status": "done",
                        "summary": "Done.",
                        "validation": "ran the suite",
                        "limitations": "none",
                    },
                ),
            )
        ),
    )

    result = run(
        ["byte", "--workspace", str(workspace_dir), "--unattended", "fix it"],
        environ=_env(_registry(tmp_path)),
    )

    assert result.code == 0, result.err
    assert "Done." in result.out
    assert "agent: 1 model turn(s)" in result.out


def test_the_approval_prompt_names_the_agent_that_actually_ran(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requester's identity reaches the operator, and it is Neighbor's.

    ``agent_name`` on the dispatcher is what an approval prompt reports, so a
    shared runner that hard-coded Byte's name would produce a prompt telling the
    operator that Byte wants to run a command Neighbor asked for. Driven with a
    real approval-gated tool call and a recording channel, so the name is read
    off the actual prompt text rather than off the profile.
    """
    _with_scripted_gateway(
        monkeypatch,
        ScriptedTurn(
            tool_calls=(
                # `git commit` is approval-required under the Forge baseline.
                # The exec schema takes `program` plus `args`, not a single
                # `argv` -- an `argv` key is an invalid_arguments error and
                # would exercise the wrong path entirely.
                (
                    "exec_command",
                    {"program": "git", "args": ["commit", "-m", "x"]},
                ),
            )
        ),
        ScriptedTurn(
            tool_calls=(
                (
                    "finish",
                    {
                        "status": "done",
                        "summary": "I stopped.",
                        "validation": "n/a",
                        "limitations": "commit was denied",
                    },
                ),
            )
        ),
    )

    class _RecordingChannel:
        """A channel that records every prompt and denies each one.

        Denying keeps the run finite; the prompt is captured on the way through
        either way.
        """

        def __init__(self) -> None:
            self.prompts: list[str] = []
            self.requests: list[object] = []

        @property
        def available(self) -> bool:
            return True

        def present(self, request: object, prompt: str) -> str | None:
            self.prompts.append(prompt)
            self.requests.append(request)
            return "n"

    channel = _RecordingChannel()
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["neighbor", "--workspace", str(workspace_dir), "commit something"],
        channel=channel,  # type: ignore[arg-type]
        out=out,
        err=err,
        environ=_env(_registry(tmp_path)),
    )

    assert code == 0, err.getvalue()
    assert channel.prompts, "no approval prompt was presented"
    # The request's own `agent` field, not a substring search over the rendered
    # prompt: the prompt includes the workspace path, and a temp directory can
    # contain any word at all -- a text search here would pass or fail for
    # reasons that have nothing to do with the agent's identity.
    assert channel.requests, "no approval request reached the channel"
    for request in channel.requests:
        assert getattr(request, "agent", None) == "neighbor"


def test_an_unattended_neighbor_run_fails_closed_on_an_approval(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No human, no approval. The unattended guarantee covers Neighbor too.

    This is the property that makes an unattended Forge run safe to schedule, and
    it is enforced by the same shared stack Byte uses -- so it is checked here
    rather than assumed to transfer.
    """
    _with_scripted_gateway(
        monkeypatch,
        ScriptedTurn(
            tool_calls=(
                (
                    "exec_command",
                    {"program": "git", "args": ["commit", "-m", "x"]},
                ),
            )
        ),
        ScriptedTurn(
            tool_calls=(
                (
                    "finish",
                    {
                        "status": "blocked",
                        "summary": "I stopped.",
                        "limitations": "the commit was refused unattended",
                    },
                ),
            )
        ),
    )

    result = run(
        ["neighbor", "--workspace", str(workspace_dir), "--unattended", "commit"],
        environ=_env(_registry(tmp_path)),
    )

    # A refused command leaves the task blocked, which is a distinct, non-zero
    # exit -- not a success, and not an internal failure either.
    assert result.code == render.EXIT_INCOMPLETE, result.err
    # The run continued and the model saw a refusal it could read, rather than
    # the command having silently executed.
    assert "I stopped." in result.out
    assert "permission refusals:" in result.out
    assert "no interactive approval channel" in result.out.lower()
    assert "was not run" in result.out.lower()