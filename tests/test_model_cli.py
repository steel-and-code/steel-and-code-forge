"""Tests for the ``forge models`` and ``forge route`` commands.

These are inspection commands. ``forge route`` decides which model *would* serve
a request and reports why; it builds no gateway, opens no socket, and reads no
credential. Every test here runs with an empty environment, which is what makes
that claim checkable rather than aspirational.

The output contract under test is the one an operator reads when something is
wrong: which tier, which model, which provider, primary or fallback, and why.
A routing report that omits the reason is not actionable, so its presence is
asserted rather than assumed.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import pathlib
import re
from pathlib import Path

import pytest

from forge.cli.main import main
from forge.cli.render import (
    EXIT_MODEL_UNAVAILABLE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
)
from forge.models.registry import MODELS_CONFIG_ENV

ANSI = re.compile(r"\x1b\[[0-9;?\w]*[A-Za-z]")


def _module(dotted: str):
    return importlib.import_module(dotted)


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json(self) -> dict:
        """The last JSON object printed.

        ``forge route`` emits one; ``forge models status --json`` emits one per
        tier and those tests pick by tier explicitly.
        """
        return json.loads(self.out.strip().splitlines()[-1])

    def json_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.out.splitlines() if line.strip()]


def run(argv: list[str], environ: dict[str, str] | None = None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err, environ=environ if environ is not None else {})
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture()
def config(tmp_path: Path) -> Path:
    """A registry with one routable primary per tier and real fallbacks."""
    path = tmp_path / "models.toml"
    path.write_text(
        """
[providers.openrouter]
credential_env = "FORGE_OPENROUTER_API_KEY"

[providers.local]
credential_env = ""

[tiers.general_light]
primary = { model = "vendor/alpha", provider = "openrouter" }
fallbacks = [{ model = "local-beta", provider = "local" }]

[tiers.coding_standard]
primary = { model = "vendor/coder", provider = "openrouter" }
fallbacks = [{ model = "local-coder", provider = "local" }]

[tiers.reasoning_heavy]
primary = { model = "vendor/thinker", provider = "openrouter" }
fallbacks = [{ model = "local-thinker", provider = "local" }]

[routing]
default_tier = "general_light"

[routing.tasks.coding]
light = "general_light"
standard = "coding_standard"
hard = "reasoning_heavy"
""",
        encoding="utf-8",
    )
    return path


def _env(config: Path, **extra: str) -> dict[str, str]:
    return {MODELS_CONFIG_ENV: str(config), **extra}


# --------------------------------------------------------------------------
# forge route
# --------------------------------------------------------------------------


def test_route_names_the_tier_model_and_provider(config: Path) -> None:
    result = run(["route", "--task", "coding", "--difficulty", "standard"],
                 environ=_env(config))

    assert result.code == EXIT_OK
    assert "coding_standard" in result.out
    assert "vendor/coder" in result.out
    assert "openrouter" in result.out


def test_route_reports_that_the_primary_was_selected(config: Path) -> None:
    result = run(["route", "--task", "coding"], environ=_env(config))

    assert "primary" in result.out


def test_route_states_the_reason_for_the_selection(config: Path) -> None:
    """A selection without a reason is not actionable when something breaks."""
    result = run(["route", "--task", "coding"], environ=_env(config))

    assert "reason" in result.out


def test_route_accepts_an_agent_name(config: Path) -> None:
    result = run(
        ["route", "--agent", "byte", "--task", "coding"], environ=_env(config)
    )

    assert "byte" in result.out


def test_route_defaults_an_unknown_task_to_the_default_tier(config: Path) -> None:
    result = run(["route", "--task", "archaeology"], environ=_env(config))

    assert "general_light" in result.out


def test_route_emits_one_json_object(config: Path) -> None:
    result = run(
        ["route", "--task", "coding", "--json"], environ=_env(config)
    )

    body = result.json()

    assert body["tier"] == "coding_standard"
    assert body["model"] == "vendor/coder"
    assert body["provider"] == "openrouter"
    assert body["selection"] == "primary"
    assert body["health"] == "GREEN"


def test_route_json_states_the_reason(config: Path) -> None:
    body = run(["route", "--task", "coding", "--json"], environ=_env(config)).json()

    assert body["reason"]


def test_route_exits_nonzero_when_no_model_can_serve(tmp_path: Path) -> None:
    """A caller scripting Forge must be able to branch on this."""
    path = tmp_path / "models.toml"
    path.write_text(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["route", "--task", "anything"], environ=_env(path))

    assert result.code == EXIT_MODEL_UNAVAILABLE


def test_route_reports_red_when_nothing_is_usable(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["route", "--task", "anything", "--json"], environ=_env(path))

    assert result.json()["health"] == "RED"


def test_route_reports_why_nothing_is_usable(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["route", "--task", "anything", "--json"], environ=_env(path))

    assert "disabled" in result.json()["reason"]


def test_route_names_the_provider_when_it_has_no_gateway(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.anthropic]\ncredential_env = "FORGE_ANTHROPIC_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "claude-x", provider = "anthropic" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["route", "--task", "anything", "--json"], environ=_env(path))

    assert "anthropic" in result.json()["reason"]


def test_route_does_not_crash_on_an_unimplemented_provider(tmp_path: Path) -> None:
    """It reports. It does not raise an import error out of an inspection command."""
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.anthropic]\ncredential_env = "FORGE_ANTHROPIC_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "claude-x", provider = "anthropic" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["route", "--task", "anything"], environ=_env(path))

    assert result.code == EXIT_MODEL_UNAVAILABLE
    assert "Traceback" not in result.err


def test_route_reports_a_broken_configuration_as_a_usage_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.toml"
    path.write_text("[tier.general_light\n", encoding="utf-8")

    result = run(["route", "--task", "coding"], environ=_env(path))

    assert result.code == EXIT_USAGE


def test_route_explains_a_broken_configuration(tmp_path: Path) -> None:
    path = tmp_path / "models.toml"
    path.write_text("[tier.general_light\n", encoding="utf-8")

    result = run(["route", "--task", "coding"], environ=_env(path))

    assert "forge:" in result.err
    assert "Traceback" not in result.err


def test_route_reports_a_missing_configuration_file(tmp_path: Path) -> None:
    result = run(
        ["route", "--task", "coding"],
        environ={MODELS_CONFIG_ENV: str(tmp_path / "absent.toml")},
    )

    assert result.code == EXIT_USAGE
    assert "absent.toml" in result.err


# --------------------------------------------------------------------------
# forge models status
# --------------------------------------------------------------------------


def test_models_status_lists_every_capability_tier(config: Path) -> None:
    result = run(["models", "status"], environ=_env(config))

    for tier in ("general_light", "coding_standard", "reasoning_heavy"):
        assert tier in result.out


def test_models_status_shows_a_health_value_per_tier(config: Path) -> None:
    """The future UI indicator's data, in the terminal first."""
    result = run(["models", "status"], environ=_env(config))

    assert result.out.count("GREEN") == 3


def test_models_status_reports_the_selected_model_per_tier(config: Path) -> None:
    result = run(["models", "status"], environ=_env(config))

    assert "vendor/alpha" in result.out


def test_models_status_shows_the_fallbacks_behind_each_tier(config: Path) -> None:
    """When the primary goes down, what stands behind it should be visible now.

    Reporting fallbacks only when a tier is already degraded would mean the
    operator has to be told the hard way, at the moment it matters least.
    """
    result = run(["models", "status"], environ=_env(config))

    assert "local-beta" in result.out
    assert "local-coder" in result.out


def test_models_status_marks_a_disabled_fallback_as_disabled(tmp_path: Path) -> None:
    """A disabled fallback is not coverage, and should not look like coverage."""
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter" }\n'
        'fallbacks = [{ model = "y", provider = "local", enabled = false }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["models", "status"], environ=_env(path))

    assert "(disabled)" in result.out


def test_models_status_shows_red_for_a_tier_with_nothing_usable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["models", "status"], environ=_env(path))

    assert "RED" in result.out


def test_models_status_shows_yellow_when_a_fallback_is_serving(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        'fallbacks = [{ model = "y", provider = "local" }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    result = run(["models", "status"], environ=_env(path))

    assert "YELLOW" in result.out


def test_models_status_names_the_source_of_the_configuration(
    config: Path,
) -> None:
    """An operator needs to know which file they are looking at."""
    result = run(["models", "status"], environ=_env(config))

    assert str(config) in result.out


def test_models_status_reports_a_missing_configuration(tmp_path: Path) -> None:
    result = run(
        ["models", "status"],
        environ={MODELS_CONFIG_ENV: str(tmp_path / "absent.toml")},
    )

    assert result.code == EXIT_USAGE


def test_models_status_emits_one_json_object_per_tier(config: Path) -> None:
    bodies = run(["models", "status", "--json"], environ=_env(config)).json_lines()

    assert len(bodies) == 3
    assert {body["tier"] for body in bodies} == {
        "general_light",
        "coding_standard",
        "reasoning_heavy",
    }


def test_models_status_json_carries_the_health_key(config: Path) -> None:
    """The key a UI indicator will read. Named once, here, before it exists."""
    result = run(["models", "status", "--json"], environ=_env(config))

    for body in result.json_lines():
        assert body["health"] in ("GREEN", "YELLOW", "RED")


def test_models_status_json_reports_each_slot_with_its_state(
    tmp_path: Path,
) -> None:
    """Configured, enabled, and available are separate facts and must stay so.

    A future indicator that only receives a boolean has to re-derive these, and
    would eventually re-derive them wrongly.
    """
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "openrouter", enabled = false }\n'
        'fallbacks = [{ model = "y", provider = "local" }]\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    by_tier = {
        body["tier"]: body
        for body in run(
            ["models", "status", "--json"], environ=_env(path)
        ).json_lines()
    }
    slots = {slot["model"]: slot for slot in by_tier["general_light"]["slots"]}

    assert slots["x"]["enabled"] is False
    assert slots["x"]["state"] == "disabled"
    assert slots["y"]["enabled"] is True


def test_models_status_names_the_availability_source(config: Path) -> None:
    """Which file answered, so an operator is never guessing what they are reading."""
    result = run(["models", "status"], environ=_env(config))

    assert str(config) in result.out


def test_models_status_says_health_was_not_verified(config: Path) -> None:
    """Loading configuration is not checking a provider.

    With no availability source, Forge has contacted nobody, and a status view
    that reads GREEN as "confirmed working" would be lying.
    """
    result = run(["models", "status"], environ=_env(config))

    assert "unverified" in result.out.lower()


def test_models_status_reports_a_red_tier_for_every_known_tier_name(
    tmp_path: Path,
) -> None:
    """A tier the registry does not carry is RED, not absent.

    An indicator iterating the known tier names must get a value for each one.
    """
    path = tmp_path / "models.toml"
    path.write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "x", provider = "local" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )

    by_tier = {
        body["tier"]: body
        for body in run(
            ["models", "status", "--json"], environ=_env(path)
        ).json_lines()
    }

    assert by_tier["coding_standard"]["health"] == "RED"
    assert by_tier["reasoning_heavy"]["health"] == "RED"


# --------------------------------------------------------------------------
# Both commands
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["route", "--task", "coding"], ["models", "status"]],
)
def test_neither_command_reads_a_credential(argv: list[str], config: Path) -> None:
    """The value must not appear even when the environment has one."""
    secret = "sk-not-a-real-key-abcdefghijklmnop"
    result = run(argv, environ=_env(config, FORGE_OPENROUTER_API_KEY=secret))

    assert secret not in result.out
    assert secret not in result.err


@pytest.mark.parametrize(
    "argv", [["route", "--task", "coding"], ["models", "status"]]
)
def test_neither_command_emits_ansi(argv: list[str], config: Path) -> None:
    """Same reason as every other Forge surface: SSH terminals."""
    result = run(argv, environ=_env(config))

    assert not ANSI.search(result.out)


def test_route_and_status_work_with_no_environment_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both run offline, with no key and no config file, from the packaged default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MODELS_CONFIG_ENV, raising=False)

    assert run(["route", "--task", "coding"]).code == EXIT_OK
    assert run(["models", "status"]).code == EXIT_OK


def test_a_config_file_in_the_working_directory_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution order, second step: ./models.toml."""
    (tmp_path / "models.toml").write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "from-cwd", provider = "local" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MODELS_CONFIG_ENV, raising=False)

    result = run(["route", "--task", "anything", "--json"], environ={})

    assert result.json()["model"] == "from-cwd"


def test_the_config_file_in_the_working_directory_overrides_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution order, last step: the packaged default is only a fallback."""
    (tmp_path / "models.toml").write_text(
        '[providers.local]\ncredential_env = ""\n'
        '[tiers.general_light]\n'
        'primary = { model = "local-wins", provider = "local" }\n'
        '\n[routing]\ndefault_tier = "general_light"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(MODELS_CONFIG_ENV, raising=False)

    by_tier = {
        body["tier"]: body
        for body in run(["models", "status", "--json"], environ={}).json_lines()
    }

    assert by_tier["general_light"]["model"] == "local-wins"


# --------------------------------------------------------------------------
# Existing behaviour is preserved
# --------------------------------------------------------------------------


def test_the_help_text_documents_both_commands() -> None:
    """Read the parser's own formatter output, not ``main``.

    ``main`` re-raises the SystemExit argparse uses for ``--help`` -- a
    deliberate contract, since a CLI that turns ``--help`` into a return code
    breaks shell completion. So the help text is captured from the parser.
    """
    parser = _module("forge.cli.main").build_parser()
    text = parser.format_help()

    assert "route" in text
    assert "models" in text


def test_route_shows_its_own_usage_on_a_missing_task() -> None:
    result = run(["route"])

    assert result.code == EXIT_USAGE
    assert "forge route" in result.err


def test_byte_still_accepts_an_explicit_model_flag() -> None:
    """The pre-existing flag must keep working.

    This change adds a registry; it does not remove the existing way to pin a
    model for one run.
    """
    parser = _module("forge.cli.main").build_parser()

    args = parser.parse_args(
        ["byte", "--workspace", ".", "--model", "vendor/x", "task"]
    )

    assert args.model == "vendor/x"


def test_route_requires_no_workspace(config: Path) -> None:
    """Routing is not an operation on a workspace and must not require one.

    Workspace roots are an explicit safety decision; bolting that requirement
    onto an inspection command would make the two unrelated concerns look
    related.
    """
    assert run(["route", "--task", "coding"], environ=_env(config)).code == EXIT_OK


# --------------------------------------------------------------------------
# Architecture guards
# --------------------------------------------------------------------------


def test_the_cli_still_names_no_model_identifier() -> None:
    """The registry is the only place a model id is written down.

    Scanned rather than reviewed: a model id appearing in the CLI would be a
    second source of truth, and the drift would only show up when a model was
    replaced in one place and not the other.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)

    from tests.test_model_router import _known_ids

    known = _known_ids()
    offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "/" in text and any(known_id in text for known_id in known):
                offenders.append(repr(text))

    assert offenders == []


def test_the_routing_path_imports_no_gateway() -> None:
    """`forge route` decides; it does not call.

    Scoped to the routing and status handlers rather than the whole CLI: `forge
    byte` legitimately builds a gateway, and a guard that failed there would be
    telling us to remove execution rather than protect an inspection command.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)

    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_run_route", "_run_models_status", "_build_router")
    ]
    assert handlers, "routing handlers not found; this guard has gone stale"

    for handler in handlers:
        for node in ast.walk(handler):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert "openrouter" not in name, f"{handler.name}: {name}"
                assert "models.local" not in name, f"{handler.name}: {name}"
                assert "urllib" not in name, f"{handler.name}: {name}"


def test_route_reads_no_credential() -> None:
    """The routing handlers never name a credential variable.

    Building a gateway requires a key, so a routing path that imported one would
    fail for an operator who simply wanted to inspect configuration.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in ("_run_route", "_run_models_status", "_build_router")
        ):
            body = ast.dump(node)
            assert "API_KEY" not in body, node.name