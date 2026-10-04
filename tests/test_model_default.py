"""Tests for `forge byte`'s default model resolution.

Before this change the default was a string constant in the CLI
(``DEFAULT_MODEL = "anthropic/claude-sonnet-4"``), which meant replacing it was a
code change. Now the default comes from the registry, so it is configuration.

What must not move:

*   ``--model`` still wins over everything.
*   ``$FORGE_MODEL`` still wins over the registry default.
*   An explicit model still uses the provider gateway chosen by
    ``$FORGE_MODEL_PROVIDER``, exactly as before.
*   Nothing about approvals, tool dispatch, or the tool loop changes. Only
    *where the default model id comes from*.

What is new:

*   With neither override set, Byte's model is chosen by routing.
*   The default provider follows the *selected slot*, so a registry routing to a
    local model gets the local gateway rather than whatever
    ``$FORGE_MODEL_PROVIDER`` says.

No test here starts a live provider. The gateway is inspected, not called, and
the routing decisions are read back off the constructed objects.
"""

from __future__ import annotations

import ast
import importlib
import io
from pathlib import Path

import pytest

from forge.cli.main import DEFAULT_MODEL_ENV, MODEL_PROVIDER_ENV, main
from forge.models.registry import MODELS_CONFIG_ENV


def _module(dotted: str):
    return importlib.import_module(dotted)


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json(self) -> dict:
        import json

        return json.loads(self.out.strip().splitlines()[-1])


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


def _registry(
    tmp_path: Path,
    *,
    primary: str = 'primary = { model = "vendor/primary", provider = "openrouter" }',
    fallbacks: str = (
        'fallbacks = [\n'
        '  { model = "vendor/fallback-one", provider = "openrouter" },\n'
        "]\n"
    ),
) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[providers.local]\n"
        'credential_env = ""\n'
        "\n[tiers.general_light]\n"
        f"{primary}\n"
        "\n[tiers.coding_standard]\n"
        f"{primary}\n"
        f"{fallbacks}"
        "\n[tiers.reasoning_heavy]\n"
        f"{primary}\n"
        "\n[routing]\n"
        'default_tier = "general_light"\n'
        "\n[routing.tasks.coding]\n"
        'light = "general_light"\n'
        'standard = "coding_standard"\n'
        'hard = "reasoning_heavy"\n',
        encoding="utf-8",
    )
    return path


def _env(config: Path, **extra: str) -> dict[str, str]:
    return {
        MODELS_CONFIG_ENV: str(config),
        "FORGE_OPENROUTER_API_KEY": "sk-not-a-real-key-for-tests",
        **extra,
    }


# --------------------------------------------------------------------------
# The default now comes from configuration
# --------------------------------------------------------------------------


def test_byte_uses_the_routed_primary_when_no_override_is_set(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The point of the change: the default model id is no longer a constant."""
    config = _registry(tmp_path)

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "task"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.model_id == "vendor/primary"


def test_byte_routes_a_coding_task_to_the_coding_tier(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(
        tmp_path,
        primary='primary = { model = "tier-specific", provider = "openrouter" }',
    )
    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "fix the tests"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.tier == "coding_standard"


def test_the_default_model_comes_from_the_registry_not_a_constant() -> None:
    """Structural: the old constant is gone from the CLI.

    Asserted by scanning rather than by importing, because the failure mode is a
    constant that *looks* unused but is still consulted as a last resort.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()

    assert "DEFAULT_MODEL =" not in source


def test_no_model_identifier_literal_survives_in_the_cli() -> None:
    """Every model id lives in the registry. None in the CLI."""
    from tests.test_model_router import _known_ids

    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)
    known = _known_ids()

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(known_id in node.value for known_id in known)
    ]

    assert offenders == []


# --------------------------------------------------------------------------
# Override precedence
# --------------------------------------------------------------------------


def test_the_model_flag_beats_the_registry(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """An operator pinning a model for one run must always win."""
    config = _registry(tmp_path)
    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{DEFAULT_MODEL_ENV: "from-env"})
    )

    assert resolved.model_id == "vendor/pinned"


def test_the_environment_beats_the_registry(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(tmp_path)
    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{DEFAULT_MODEL_ENV: "from-env"})
    )

    assert resolved.model_id == "from-env"


def test_the_flag_beats_the_environment(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Unchanged precedence: flag, then environment, then configuration."""
    config = _registry(tmp_path)
    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{DEFAULT_MODEL_ENV: "from-env"})
    )

    assert resolved.model_id == "vendor/pinned"


def test_an_explicit_model_does_not_consult_the_registry_at_all(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A pinned model must not fail because a registry file is broken.

    The flag is a complete override. Making it depend on configuration being
    valid would break the one escape hatch an operator has when the registry is
    wrong.
    """
    broken = tmp_path / "broken.toml"
    broken.write_text("[tier.general_light\n", encoding="utf-8")

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args,
        {
            MODELS_CONFIG_ENV: str(broken),
            "FORGE_OPENROUTER_API_KEY": "k",
        },
    )

    assert resolved.model_id == "vendor/pinned"


def test_an_explicit_model_keeps_the_environment_chosen_provider(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Pre-existing behaviour, deliberately unchanged.

    An explicit ``--model`` has always used the provider from
    ``$FORGE_MODEL_PROVIDER``. This change does not quietly reinterpret that.
    """
    config = _registry(tmp_path)
    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "local-model", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{MODEL_PROVIDER_ENV: "local"})
    )

    assert resolved.provider == "local"


# --------------------------------------------------------------------------
# The registry's own fallback behaviour applies to the default
# --------------------------------------------------------------------------


def test_a_disabled_default_falls_back_for_a_byte_run(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Registry fallback must reach the runtime, not just the report.

    Otherwise `forge models status` would say YELLOW while Byte kept trying a
    model an operator switched off.
    """
    config = _registry(
        tmp_path,
        primary=(
            'primary = { model = "vendor/primary", provider = "openrouter", '
            "enabled = false }"
        ),
    )

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.model_id == "vendor/fallback-one"


def test_the_second_fallback_is_used_when_the_first_is_unavailable(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(
        tmp_path,
        primary=(
            'primary = { model = "vendor/primary", provider = "openrouter", '
            "enabled = false }"
        ),
        fallbacks=(
            "fallbacks = [\n"
            '  { model = "vendor/fallback-one", provider = "openrouter", '
            "enabled = false },\n"
            '  { model = "vendor/fallback-two", provider = "openrouter" },\n'
            "]\n"
        ),
    )

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.model_id == "vendor/fallback-two"


def test_a_tier_with_nothing_usable_is_an_error_not_a_silent_default(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """No usable model must fail visibly.

    Falling back to a hard-coded id here would defeat the entire change: the
    operator would see a RED tier and a run on a model nobody configured.
    """
    config = _registry(
        tmp_path,
        primary=(
            'primary = { model = "vendor/primary", provider = "openrouter", '
            "enabled = false }"
        ),
        fallbacks="fallbacks = []\n",
    )

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    with pytest.raises(Exception) as caught:
        _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert "no usable model" in str(caught.value).lower()


def test_a_broken_registry_is_a_usage_error_not_a_silent_default(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A malformed registry fails the run rather than guessing a model."""
    broken = tmp_path / "broken.toml"
    broken.write_text("[tier.general_light\n", encoding="utf-8")

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    with pytest.raises(Exception) as caught:
        _module("forge.cli.main")._resolve_default_model(
            args, {MODELS_CONFIG_ENV: str(broken), "FORGE_OPENROUTER_API_KEY": "k"}
        )

    assert "toml" in str(caught.value).lower()


def test_byte_reports_a_broken_registry_without_a_traceback(
    tmp_path: Path, workspace_dir: Path
) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[tier.general_light\n", encoding="utf-8")

    result = run(
        ["byte", "--workspace", str(workspace_dir), "t"],
        environ={MODELS_CONFIG_ENV: str(broken), "FORGE_OPENROUTER_API_KEY": "k"},
    )

    assert result.code == 2
    assert "Traceback" not in result.err


def test_byte_reports_a_red_tier_without_a_traceback(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(
        tmp_path,
        primary=(
            'primary = { model = "vendor/primary", provider = "openrouter", '
            "enabled = false }"
        ),
        fallbacks="fallbacks = []\n",
    )

    result = run(
        ["byte", "--workspace", str(workspace_dir), "t"], environ=_env(config)
    )

    assert result.code == 2
    assert "Traceback" not in result.err
    assert "no usable model" in result.err.lower()


# --------------------------------------------------------------------------
# The default provider follows the selected slot
# --------------------------------------------------------------------------


def test_a_local_default_selects_the_local_gateway(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A registry routing to a local model must not get the remote gateway.

    This is the whole reason the provider travels with the model: a run pointed
    at a local model over OpenRouter would fail at request time, inside a
    provider, for a reason that has nothing to do with the model.
    """
    config = _registry(
        tmp_path,
        primary='primary = { model = "qwen2.5-coder:14b", provider = "local" }',
        fallbacks="fallbacks = []\n",
    )

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.provider == "local"


def test_the_default_provider_ignores_the_provider_environment_variable(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """$FORGE_MODEL_PROVIDER configures the *gateway*, not the routing table.

    With no explicit model, configuration decides both. Silently letting the
    environment override the provider would produce a mismatched pair.
    """
    config = _registry(
        tmp_path,
        primary='primary = { model = "qwen2.5-coder:14b", provider = "local" }',
        fallbacks="fallbacks = []\n",
    )

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{MODEL_PROVIDER_ENV: "openrouter"})
    )

    assert resolved.provider == "local"


def test_a_routed_default_reports_where_the_model_came_from(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The operator can see which tier and slot Byte picked.

    Without this, a run on an unexpected model would be inexplicable after the
    fact, which is the failure the registry was built to prevent.
    """
    config = _registry(tmp_path)

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.tier == "coding_standard"
    assert resolved.selection == "primary"
    assert resolved.reason


# --------------------------------------------------------------------------
# Nothing else moved
# --------------------------------------------------------------------------


def test_the_model_flag_help_no_longer_promises_a_hard_coded_default() -> None:
    """Help text must not name a model that is no longer the default."""
    parser = _module("forge.cli.main").build_parser()
    text = parser.format_help()

    assert "anthropic/claude-sonnet-4" not in text


def test_byte_still_exposes_a_model_flag() -> None:
    parser = _module("forge.cli.main").build_parser()

    args = parser.parse_args(
        ["byte", "--workspace", ".", "--model", "vendor/x", "task"]
    )

    assert args.model == "vendor/x"


def test_byte_still_requires_a_workspace(workspace_dir: Path) -> None:
    """Unrelated to models, and deliberately unchanged."""
    result = run(["byte", "t"], environ={"FORGE_OPENROUTER_API_KEY": "k"})

    assert result.code == 2
    assert "workspace" in result.err.lower()


def test_the_agent_profile_still_declares_no_model() -> None:
    """Byte has no fixed model, by settled decision. Still true."""
    from forge.agents.byte import BYTE

    assert BYTE.model is None


def test_no_live_health_probe_was_introduced() -> None:
    """Availability stays injected. Nothing here may probe a provider."""
    source = Path(str(_module("forge.cli.main").__file__)).read_text()

    for banned in ("urllib", "socket", "http.client", "requests", "httpx"):
        assert banned not in source, banned


def test_the_runtime_still_receives_a_plain_model_string(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The registry is resolved once, at the CLI edge.

    `AgentRuntime` takes a `model_id: str` and that has not changed. Threading a
    decision object through the runtime would be execution integration, which is
    a separate step.
    """
    config = _registry(tmp_path)

    parser = _module("forge.cli.main").build_parser()
    args = parser.parse_args(["byte", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert isinstance(resolved.model_id, str)

    import inspect

    from forge.agents.runtime import AgentRuntime

    annotation = inspect.signature(AgentRuntime.__init__).parameters["model_id"]
    assert annotation.annotation == "str"


def test_the_workbench_resolves_its_model_the_same_way() -> None:
    """Not an extra feature: removing DEFAULT_MODEL forced it.

    The workbench used to fall back on that constant. With the constant gone it
    would raise NameError, and putting the constant back would reinstate the
    hard-coded model id this change exists to delete. It is a browser surface
    onto the same Byte run, so it resolves through the same resolver -- it must
    not be able to start a run on a model `forge byte` would refuse.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)

    workbench = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_run_workbench"
    )

    assert "_resolve_default_model" in ast.dump(workbench)


def test_no_reference_to_the_removed_constant_remains() -> None:
    """Every remaining reference would be a NameError waiting to fire.

    Found by deleting a constant: two call sites used it, only one of which I was
    changing. This asserts the constant is gone *and* unreferenced, because the
    first check alone passes while the second is still broken.
    """
    source = Path(str(_module("forge.cli.main").__file__)).read_text()
    tree = ast.parse(source)

    referenced = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "DEFAULT_MODEL"
    }

    assert referenced == set()
    assert "DEFAULT_MODEL =" not in source