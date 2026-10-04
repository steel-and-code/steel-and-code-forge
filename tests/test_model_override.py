"""Manual model override: the invocation-level decision, pinned.

Stage 2 made `--model` an explicitly documented feature rather than an
incidental escape hatch. Almost nothing in the code changed -- which is the
point, and is why this file is mostly *characterisation*: the override path
already existed, already short-circuited the registry, and already worked for
both agents. These tests exist so that the settled semantics cannot drift
quietly, and so the reasoning behind them is written down somewhere executable.

**The settled contract, in one sentence: an override is reported on, never
validated against.** `docs/model-registry.md` decides that a pinned model must
keep working when the configuration file is broken -- which is exactly when an
operator reaches for it -- so the override short-circuits before the registry is
read and the registry cannot fail the run. Stage 2's ruling was to keep that
and *document* it rather than change it, adding a non-blocking stderr advisory
where the registry happens to be readable.

What is therefore pinned here:

* precedence, exactly: CLI > environment > routing;
* the registry is not consulted to *decide* anything for an override;
* an advisory is emitted for an unknown/disabled/unusable selection, and never
  blocks;
* an unreadable registry produces silence, not a failure;
* both agents share one override surface, independent of identity;
* ``AgentProfile.model`` stays ``None``, so no override can persist anywhere.

No test contacts a provider. Resolution is exercised through the real registry
and router with an injected configuration; the gateway is never called.
"""

from __future__ import annotations

import importlib
import io
from pathlib import Path

import pytest

from forge.agents.byte import BYTE
from forge.agents.neighbor import NEIGHBOR
from forge.agents.profile import AgentProfile
from forge.cli.main import (
    BYTE_TASK_DIFFICULTY,
    BYTE_TASK_KIND,
    DEFAULT_MODEL_ENV,
    MODEL_PROVIDER_ENV,
    NEIGHBOR_TASK_DIFFICULTY,
    NEIGHBOR_TASK_KIND,
    OVERRIDE_WARNING_PREFIX,
    build_parser,
    main,
)
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
    """A registry where the two agents' tasks resolve to different models.

    Distinct model ids per tier are load-bearing: without them a test could not
    tell "routed to the coding tier" from "routed to the light tier", and the
    precedence assertions below would pass for the wrong reason.
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
        'fallbacks = [\n'
        '  { model = "vendor/coding-fallback", provider = "openrouter" },\n'
        "]\n"
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


def _env(config: Path | None, **extra: str) -> dict[str, str]:
    base = {"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    if config is not None:
        base[MODELS_CONFIG_ENV] = str(config)
    return {**base, **extra}


def _broken_registry(tmp_path: Path) -> Path:
    """A file that will not parse.

    The escape hatch's reason for existing. An override paired with this must
    still resolve, which is the single most important assertion in this file.
    """
    path = tmp_path / "broken.toml"
    path.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[tiers.general_light]\n"
        'primary = { model = "vendor/light", provider = "openrouter" }\n'
        "this line is not valid toml at all\n",
        encoding="utf-8",
    )
    return path


def _disabled_registry(tmp_path: Path) -> Path:
    """A registry whose only ``vendor/off`` slot is switched off."""
    path = tmp_path / "disabled.toml"
    path.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[providers.local]\n"
        'credential_env = ""\n'
        "\n[tiers.general_light]\n"
        'primary = { model = "vendor/light", provider = "openrouter" }\n'
        "\n[tiers.coding_standard]\n"
        'primary = { model = "vendor/off", provider = "openrouter", '
        "enabled = false }\n"
        'fallbacks = [\n'
        '  { model = "vendor/coding", provider = "openrouter" },\n'
        "]\n"
        "\n[routing]\n"
        'default_tier = "general_light"\n'
        "\n[routing.tasks.coding]\n"
        'standard = "coding_standard"\n'
        "\n[routing.tasks.conversation]\n"
        'standard = "general_light"\n',
        encoding="utf-8",
    )
    return path


def _all_disabled_registry(tmp_path: Path) -> Path:
    """A readable registry whose coding tier has nothing usable in it.

    Distinct from ``_disabled_registry``, which keeps one enabled fallback: that
    one is for exercising the *advisory* about a switched-off slot, and it
    routes perfectly well. This one is for the routed failure path, so the
    primary is off **and** there are no fallbacks.
    """
    path = tmp_path / "all-off.toml"
    path.write_text(
        "[providers.openrouter]\n"
        'credential_env = "FORGE_OPENROUTER_API_KEY"\n'
        "\n[providers.local]\n"
        'credential_env = ""\n'
        "\n[tiers.general_light]\n"
        'primary = { model = "vendor/light", provider = "openrouter" }\n'
        "\n[tiers.coding_standard]\n"
        'primary = { model = "vendor/off", provider = "openrouter", '
        "enabled = false }\n"
        "\n[routing]\n"
        'default_tier = "general_light"\n'
        "\n[routing.tasks.coding]\n"
        'standard = "coding_standard"\n'
        "\n[routing.tasks.conversation]\n"
        'standard = "general_light"\n',
        encoding="utf-8",
    )
    return path


def _resolve(argv: list[str], environ: dict[str, str], **kw):
    args = build_parser().parse_args(argv)
    warn = io.StringIO()
    resolved = _module("forge.cli.main")._resolve_default_model(
        args, environ, warn=warn, **kw
    )
    return resolved, warn.getvalue()


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


def test_cli_beats_environment_beats_routing(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The whole precedence chain, asserted in one place.

    Every rung is given a *different* model id, so the assertion is about which
    source won rather than about what any single source resolves to.
    """
    config = _registry(tmp_path)
    env = _env(config, **{DEFAULT_MODEL_ENV: "vendor/from-env"})

    # 1. CLI wins over the environment.
    cli, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/from-cli", "t"],
        env,
    )
    assert cli.model_id == "vendor/from-cli"
    assert cli.routed is False

    # 2. Environment wins over routing.
    from_env, _ = _resolve(["byte", "--workspace", str(workspace_dir), "t"], env)
    assert from_env.model_id == "vendor/from-env"
    assert from_env.routed is False

    # 3. Routing is the default, and only applies when neither is set.
    routed, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"], _env(config)
    )
    assert routed.model_id == "vendor/coding"
    assert routed.routed is True


def test_an_empty_environment_override_is_not_an_override(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """``$FORGE_MODEL=""`` must fall through to routing.

    A blank variable exported by a shell profile is common, and treating it as
    a selection would resolve the run to an empty model id.
    """
    config = _registry(tmp_path)
    resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"],
        _env(config, **{DEFAULT_MODEL_ENV: "   "}),
    )

    assert resolved.model_id == "vendor/coding"
    assert resolved.routed is True


def test_an_override_carries_its_reason(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The operator is told which source won.

    `ResolvedModel.reason` is what an operator reads when a run used a model
    they did not expect, so "why this model" has to survive the override path
    rather than being filled in only for routed decisions.
    """
    config = _registry(tmp_path)

    from_cli, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/x", "t"],
        _env(config),
    )
    from_env, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"],
        _env(config, **{DEFAULT_MODEL_ENV: "vendor/y"}),
    )
    routed, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"], _env(config)
    )

    assert "command line" in from_cli.reason
    assert DEFAULT_MODEL_ENV in from_env.reason
    assert routed.reason and routed.reason != from_cli.reason


# --------------------------------------------------------------------------
# The registry is not consulted to decide
# --------------------------------------------------------------------------


def test_an_override_survives_a_broken_registry(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The escape hatch, and the reason the contract is what it is.

    If this ever fails, the override has become coupled to the configuration it
    exists to bypass -- and the operator who most needs the pin is the one who
    can no longer use it.
    """
    resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"],
        _env(_broken_registry(tmp_path)),
    )

    assert resolved.model_id == "vendor/pinned"
    assert resolved.routed is False


def test_an_override_survives_a_missing_registry_entirely(
    workspace_dir: Path,
) -> None:
    """No configured file at all is the same escape, not a new failure."""
    resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"],
        _env(None),
    )

    assert resolved.model_id == "vendor/pinned"


def test_routing_still_fails_loudly_without_a_usable_configuration(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The contrast: a *routed* run has no escape hatch and must say so.

    The asymmetry is the contract. An override bypasses the registry because it
    is an explicit human decision; a routed run has no such decision behind it,
    so a broken configuration has to surface rather than resolve to something
    arbitrary.

    A malformed file raises a ``ModelResolutionError`` naming the file and the
    parse position, which is a better message than a generic "no usable model"
    and is what actually happens. Asserted on the exception type and the
    filename, not on the wording, so a clearer message later does not break it.
    """
    broken = _broken_registry(tmp_path)

    with pytest.raises(Exception) as caught:
        _resolve(["byte", "--workspace", str(workspace_dir), "t"], _env(broken))

    message = str(caught.value)
    assert broken.name in message
    assert "not valid" in message.lower()


def test_a_valid_registry_with_no_usable_tier_says_no_usable_model(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The other routed failure: a readable config whose tier cannot serve.

    Distinct from the malformed case and worth its own assertion, because the
    two produce different messages and an operator needs to tell "my file is
    wrong" from "your configuration has nothing usable in it".
    """
    # `coding`/`standard` routes to coding_standard, whose only slot is
    # switched off, so the tier has nothing usable.
    disabled = _all_disabled_registry(tmp_path)

    with pytest.raises(Exception) as caught:
        _resolve(
            ["byte", "--workspace", str(workspace_dir), "t"],
            _env(disabled),
            profile=BYTE,
            task_kind=BYTE_TASK_KIND,
            difficulty=BYTE_TASK_DIFFICULTY,
        )

    message = str(caught.value).lower()
    assert "no usable model" in message
    # And the message names the agent, so an operator is not told about Byte
    # while running something else.
    assert "byte" in message


def test_an_override_is_not_rewritten_to_a_fallback(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """No silent fallback for an explicit selection.

    The registry offers ``vendor/coding-fallback`` for the coding tier. If an
    override ever resolved to it, the operator would be told they ran one model
    while another served the request -- and for an override there is no tier
    being consulted that could legitimately substitute anything.
    """
    config = _registry(tmp_path)

    resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/nonexistent", "t"],
        _env(config),
    )

    assert resolved.model_id == "vendor/nonexistent"
    assert "fallback" not in resolved.model_id
    assert resolved.tier == ""
    assert resolved.selection == ""


# --------------------------------------------------------------------------
# The advisory: reported on, never blocking
# --------------------------------------------------------------------------


def test_an_unknown_override_is_reported_but_still_resolves(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The typo case. The most valuable warning, and the least blocking one."""
    config = _registry(tmp_path)

    resolved, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/typo", "t"],
        _env(config),
    )

    assert resolved.model_id == "vendor/typo"
    assert OVERRIDE_WARNING_PREFIX in warning
    assert "not configured" in warning


def test_a_disabled_override_is_reported_but_still_resolves(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """`enabled = false` is the operator's own switch, so it is worth naming.

    It is reported and not enforced, which looks contradictory and is not: the
    switch governs *routing*, and the override never routes. The advisory exists
    so an operator who pinned a switched-off model finds out before the
    provider does.
    """
    config = _disabled_registry(tmp_path)

    resolved, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/off", "t"],
        _env(config),
    )

    assert resolved.model_id == "vendor/off"
    assert "disabled" in warning


def test_a_configured_and_enabled_override_warns_about_nothing(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Silence is the success case, and is asserted as one."""
    config = _registry(tmp_path)

    _, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/coding", "t"],
        _env(config),
    )

    assert warning == ""


def test_either_identifier_form_is_recognised(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """``vendor/coding`` and ``openrouter/vendor/coding`` both resolve quietly.

    An operator pastes either. Warning about a model that *is* configured,
    because they wrote the bare id, would train them to ignore the warning.
    """
    config = _registry(tmp_path)

    _, bare = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/coding", "t"],
        _env(config),
    )
    _, qualified = _resolve(
        [
            "byte",
            "--workspace",
            str(workspace_dir),
            "--model",
            "openrouter/vendor/coding",
            "t",
        ],
        _env(config),
    )

    assert bare == ""
    assert qualified == ""


def test_a_broken_registry_produces_no_warning_at_all(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Silence, not a complaint about the file being bypassed.

    The advisory is best-effort by construction. A registry that will not parse
    yields ``None``, so an operator using the escape hatch is not also handed a
    warning about the file they are deliberately ignoring.
    """
    _, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"],
        _env(_broken_registry(tmp_path)),
    )

    assert warning == ""


def test_the_environment_override_is_advised_too(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Both override sources report, not just the flag.

    ``$FORGE_MODEL`` shadowing the registry silently is called out in
    ``docs/model-registry.md`` as a real trap, and an advisory is the cheapest
    way to make it less silent without changing what it does.
    """
    config = _registry(tmp_path)

    resolved, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"],
        _env(config, **{DEFAULT_MODEL_ENV: "vendor/typo"}),
    )

    assert resolved.model_id == "vendor/typo"
    assert "not configured" in warning


def test_a_routed_run_is_never_advised(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """No override, no advisory.

    Routing already reports its own outcome through ``tier``/``selection``, and
    a warning on the default path would be noise about a decision nobody made by
    hand.
    """
    _, warning = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"], _env(_registry(tmp_path))
    )

    assert warning == ""


def test_no_warning_stream_means_no_warning(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The resolver stays silent for every pre-existing caller.

    This is what let the advisory be added without touching a single existing
    call site, and it is asserted so a future default cannot start printing to
    stdout from inside a resolver.
    """
    args = build_parser().parse_args(
        ["byte", "--workspace", str(tmp_path), "--model", "vendor/typo", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(_registry(tmp_path))
    )

    assert resolved.model_id == "vendor/typo"


# --------------------------------------------------------------------------
# Both agents, one surface
# --------------------------------------------------------------------------


def test_both_agents_share_one_override_surface(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Identity and manual selection are independent.

    The same pinned model, the same precedence, the same advisory -- for an
    agent whose routing default is a different tier entirely. That is the claim
    that a task kind and an override are separate axes.
    """
    config = _registry(tmp_path)

    byte_args = build_parser().parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )
    neighbor_args = build_parser().parse_args(
        ["neighbor", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )
    module = _module("forge.cli.main")

    byte_resolved = module._resolve_default_model(byte_args, _env(config))
    neighbor_resolved = module._resolve_default_model(
        neighbor_args,
        _env(config),
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
    )

    assert byte_resolved.model_id == neighbor_resolved.model_id == "vendor/pinned"
    assert byte_resolved.routed is neighbor_resolved.routed is False


def test_the_override_surfaces_identically_on_both_subcommands(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """`forge byte --model` and `forge neighbor --model` parse identically."""
    parser = build_parser()
    byte_args = parser.parse_args(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/x", "t"]
    )
    neighbor_args = parser.parse_args(
        ["neighbor", "--workspace", str(workspace_dir), "--model", "vendor/x", "t"]
    )

    assert byte_args.model == neighbor_args.model == "vendor/x"


def test_the_provider_env_still_governs_an_override(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Pre-existing behaviour, pinned on both agents.

    An override's provider comes from ``$FORGE_MODEL_PROVIDER``; only a routed
    default takes its provider from the selected slot. Mixing them would address
    a model to the wrong endpoint.
    """
    config = _registry(tmp_path)
    env = _env(config, **{MODEL_PROVIDER_ENV: "local"})

    byte_resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "--model", "vendor/x", "t"], env
    )
    neighbor_resolved, _ = _resolve(
        ["neighbor", "--workspace", str(workspace_dir), "--model", "vendor/x", "t"],
        env,
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
    )

    assert byte_resolved.provider == "local"
    assert neighbor_resolved.provider == "local"


# --------------------------------------------------------------------------
# Defaults must not drift
# --------------------------------------------------------------------------


def test_byte_default_route_is_unchanged(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(tmp_path)
    resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"], _env(config)
    )

    assert resolved.model_id == "vendor/coding"
    assert resolved.tier == "coding_standard"
    assert BYTE_TASK_KIND == "coding"
    assert BYTE_TASK_DIFFICULTY == "standard"


def test_neighbor_default_route_is_unchanged(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(tmp_path)
    resolved, _ = _resolve(
        ["neighbor", "--workspace", str(workspace_dir), "t"],
        _env(config),
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
    )

    assert resolved.model_id == "vendor/light"
    assert resolved.tier == "general_light"
    assert NEIGHBOR_TASK_KIND == "conversation"
    assert NEIGHBOR_TASK_DIFFICULTY == "standard"


def test_the_two_agents_still_route_apart(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Adding an override must not collapse the agents onto one model."""
    config = _registry(tmp_path)

    byte_resolved, _ = _resolve(
        ["byte", "--workspace", str(workspace_dir), "t"], _env(config)
    )
    neighbor_resolved, _ = _resolve(
        ["neighbor", "--workspace", str(workspace_dir), "t"],
        _env(config),
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
    )

    assert byte_resolved.tier != neighbor_resolved.tier
    assert byte_resolved.model_id != neighbor_resolved.model_id


# --------------------------------------------------------------------------
# Nothing persists
# --------------------------------------------------------------------------


def test_no_override_field_exists_on_the_profile() -> None:
    """The structural guarantee that an override cannot persist.

    Checked over the dataclass fields rather than by instantiating: a field
    that merely defaults to ``None`` would pass a value check and fail this one.
    """
    fields = frozenset(AgentProfile.__dataclass_fields__)

    assert fields == {
        "name",
        "instructions",
        "allowed_tools",
        "model",
        "max_tool_turns",
        "max_output_tokens",
        "metadata",
    }
    for forbidden in ("override", "manual_model", "pinned_model", "preferred_model"):
        assert forbidden not in fields


def test_both_agents_still_have_no_fixed_model() -> None:
    """Stage 1's decision, re-asserted because Stage 2 touched the CLI.

    An override is a per-run decision; if either profile had acquired a model,
    the two mechanisms would be competing and the routing layer would no longer
    be the single source of the default.
    """
    assert BYTE.model is None
    assert NEIGHBOR.model is None


def test_a_full_run_with_an_override_writes_nothing(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a pinned model runs, and leaves no state behind.

    "Invocation-level" is only true if nothing is written, and a persistent
    per-agent preference is the specific thing this stage was told not to add --
    so the whole real path is run and the workspace tree compared across it. The
    gateway is swapped for the offline one at the single seam the CLI uses, so
    this costs no network, no key, and no money.
    """
    module = _module("forge.cli.main")
    monkeypatch.setattr(
        module,
        "_build_model_gateway",
        lambda *a, **k: ScriptedGateway(
            [ScriptedTurn(text="Ran on the pinned model.")]
        ),
    )

    before = {p for p in workspace_dir.rglob("*")}
    result = run(
        [
            "neighbor",
            "--workspace",
            str(workspace_dir),
            "--model",
            "vendor/pinned",
            "--unattended",
            "do the thing",
        ],
        environ=_env(_registry(tmp_path)),
    )

    assert result.code == 0, result.err
    assert "Ran on the pinned model." in result.out
    assert {p for p in workspace_dir.rglob("*")} == before
    assert not (workspace_dir / ".forge").exists()


# --------------------------------------------------------------------------
# Moved here from tests/test_cli_neighbor.py
# --------------------------------------------------------------------------
#
# These three began life in the Neighbor CLI tests because Neighbor was the
# agent that made them interesting -- it routes to a different tier than Byte,
# so "the override does not depend on the agent" was worth asserting. That is a
# property of the override, not of Neighbor, so they belong with the override
# contract rather than in a file about a second agent's CLI surface.
#
# They are the same tests, unchanged. Moving them is bookkeeping; the assertions
# are identical.


def test_model_override_still_short_circuits_routing_for_neighbor(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """The escape hatch must work for the new agent too.

    It is the path that keeps a run possible when the configuration is broken,
    so an agent added without it would be strictly less operable than Byte.
    """
    config = _registry(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        ["neighbor", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(args, _env(config))

    assert resolved.model_id == "vendor/pinned"
    assert resolved.routed is False





def test_the_environment_override_still_wins_for_neighbor(
    tmp_path: Path, workspace_dir: Path
) -> None:
    config = _registry(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["neighbor", "--workspace", str(workspace_dir), "t"])

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{DEFAULT_MODEL_ENV: "vendor/from-env"})
    )

    assert resolved.model_id == "vendor/from-env"
    assert resolved.routed is False





def test_the_provider_env_is_still_honoured_for_an_override(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """Pre-existing behaviour, checked on the new agent's code path.

    An override takes its provider from ``$FORGE_MODEL_PROVIDER``, not from the
    selected slot -- the rule that predates routing and must not have shifted.
    """
    config = _registry(tmp_path)
    parser = build_parser()
    args = parser.parse_args(
        ["neighbor", "--workspace", str(workspace_dir), "--model", "vendor/pinned", "t"]
    )

    resolved = _module("forge.cli.main")._resolve_default_model(
        args, _env(config, **{MODEL_PROVIDER_ENV: "local"})
    )

    assert resolved.provider == "local"
    assert resolved.routed is False


# --------------------------------------------------------------------------
# Byte is unchanged
# --------------------------------------------------------------------------
