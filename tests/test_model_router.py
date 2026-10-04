"""Tests for the router: which model serves a request, and why.

Routing is where the decoupling is either real or notional. These tests hold two
things together:

*   **Behaviour** -- primary chosen when usable, fallbacks in declaration order,
    a structured failure when nothing is. Every decision states a reason, so a
    run served by a fallback can be explained afterwards.
*   **The decoupling claim** -- that swapping every model id in the registry
    changes what Forge does without a line of agent code changing. The test for
    that is structural: it re-parses a registry naming different models and
    asserts the *same* request routes, with the agent untouched. If routing ever
    grew a hard-coded id, this is where it fails.

No test here makes a network call. Availability is injected, and the router
never constructs a gateway -- deciding is not calling.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from forge.models.registry import (
    Availability,
    ModelRegistry,
    load_registry,
    parse_registry,
)
from forge.models.router import ModelRouter, RouteRequest
from forge.models.tiers import CapabilityTier, Health, SelectionKind


ALL_TIERS: tuple[CapabilityTier, ...] = tuple(CapabilityTier)


def _doc(
    primary: str = 'primary = { model = "alpha", provider = "openrouter" }',
    fallbacks: str = (
        'fallbacks = [\n'
        '  { model = "beta", provider = "local" },\n'
        '  { model = "gamma", provider = "openrouter" },\n'
        "]"
    ),
    tiers: tuple[CapabilityTier, ...] = ALL_TIERS,
) -> str:
    """A document with every tier configured identically, for easy swapping."""
    bodies = []
    for tier in tiers:
        bodies.append(
            f"[tiers.{tier.value}]\n{primary}\n{fallbacks}\n"
        )
    joined = "\n".join(bodies)
    return (
        joined
        + "\n[routing]\n"
        + f'default_tier = "{tiers[0].value}"\n'
        + "\n[routing.tasks.coding]\n"
        + 'light = "general_light"\n'
        + 'standard = "coding_standard"\n'
        + 'hard = "reasoning_heavy"\n'
    )


def _router(
    document: str | None = None,
    availability: Availability | object | None = Availability.all_available(),
) -> ModelRouter:
    registry = parse_registry(
        document if document is not None else _doc(), availability=availability
    )
    return ModelRouter(registry)


def _request(task: str = "coding", difficulty: str = "standard") -> RouteRequest:
    return RouteRequest(agent="byte", task=task, difficulty=difficulty)


# --------------------------------------------------------------------------
# Tier selection
# --------------------------------------------------------------------------


def test_a_routing_entry_chooses_the_tier() -> None:
    decision = _router().route(_request("coding", "hard"))

    assert decision.tier == CapabilityTier.REASONING_HEAVY


def test_difficulty_selects_a_different_tier_for_the_same_task() -> None:
    router = _router()

    light = router.route(_request("coding", "light"))
    hard = router.route(_request("coding", "hard"))

    assert light.tier != hard.tier


def test_an_unlisted_task_routes_to_the_default_tier() -> None:
    """Work nobody anticipated still routes; refusing would make completeness
    of a config file load-bearing for the whole system."""
    decision = _router().route(_request("archaeology", "standard"))

    assert decision.tier == CapabilityTier.GENERAL_LIGHT


def test_an_unlisted_difficulty_falls_back_to_standard_for_that_task() -> None:
    decision = _router().route(_request("coding", "impossible"))

    assert decision.tier == CapabilityTier.CODING_STANDARD


def test_the_decision_names_the_routing_rule_that_matched() -> None:
    decision = _router().route(_request("coding", "hard"))

    assert decision.tier_reason == "routing.tasks.coding.hard"


# --------------------------------------------------------------------------
# Primary selection
# --------------------------------------------------------------------------


def test_a_usable_primary_is_selected() -> None:
    decision = _router().route(_request())

    assert decision.model == "alpha"
    assert decision.provider == "openrouter"
    assert decision.selection is SelectionKind.PRIMARY
    assert decision.tier == CapabilityTier.CODING_STANDARD


def test_a_primary_decision_says_the_primary_was_available() -> None:
    decision = _router().route(_request())

    assert "primary" in decision.reason
    assert "available" in decision.reason


def test_a_healthy_tier_reports_green_on_its_decision() -> None:
    decision = _router().route(_request())

    assert decision.health is Health.GREEN


# --------------------------------------------------------------------------
# Fallback selection
# --------------------------------------------------------------------------


def test_an_unavailable_primary_selects_the_first_fallback() -> None:
    decision = _router(availability=Availability.unavailable("alpha")).route(
        _request()
    )

    assert decision.model == "beta"
    assert decision.provider == "local"
    assert decision.selection is SelectionKind.FALLBACK


def test_the_first_fallback_is_preferred_over_the_second() -> None:
    """Declaration order is the operator's stated preference."""
    decision = _router(
        availability=Availability.unavailable("alpha", "beta")
    ).route(_request())

    assert decision.model == "gamma"


def test_a_fallback_decision_names_the_primary_that_failed() -> None:
    decision = _router(availability=Availability.unavailable("alpha")).route(
        _request()
    )

    assert "alpha" in decision.reason


def test_a_fallback_decision_reports_which_fallback_was_taken() -> None:
    """Position matters for cost and behaviour, so it is reported, not implied."""
    decision = _router(
        availability=Availability.unavailable("alpha", "beta")
    ).route(_request())

    assert "2" in decision.reason


def test_a_degraded_tier_reports_yellow_on_its_decision() -> None:
    decision = _router(availability=Availability.unavailable("alpha")).route(
        _request()
    )

    assert decision.health is Health.YELLOW


def test_a_disabled_primary_falls_through_to_a_fallback() -> None:
    """An operator switching a model off must not break the tier that used it."""
    document = _doc(
        primary='primary = { model = "alpha", provider = "openrouter", '
        "enabled = false }"
    )

    decision = _router(document).route(_request())

    assert decision.model == "beta"
    assert decision.selection is SelectionKind.FALLBACK


def test_a_disabled_primary_decision_says_it_was_disabled() -> None:
    """Disabled and unavailable are different facts and get different reasons."""
    document = _doc(
        primary='primary = { model = "alpha", provider = "openrouter", '
        "enabled = false }"
    )

    decision = _router(document).route(_request())

    assert "disabled" in decision.reason


def test_a_disabled_fallback_is_skipped() -> None:
    document = _doc(
        fallbacks=(
            'fallbacks = [\n'
            '  { model = "beta", provider = "local", enabled = false },\n'
            '  { model = "gamma", provider = "openrouter" },\n'
            "]"
        )
    )

    decision = _router(document, availability=Availability.unavailable("alpha")).route(
        _request()
    )

    assert decision.model == "gamma"


# --------------------------------------------------------------------------
# Nothing usable
# --------------------------------------------------------------------------


def test_an_unavailable_primary_and_fallback_produces_no_model() -> None:
    decision = _router(
        availability=Availability.unavailable("alpha", "beta", "gamma")
    ).route(_request())

    assert decision.selection is SelectionKind.NONE
    assert decision.model == ""
    assert decision.provider == ""


def test_a_tier_with_nothing_usable_reports_red() -> None:
    decision = _router(
        availability=Availability.unavailable("alpha", "beta", "gamma")
    ).route(_request())

    assert decision.health is Health.RED


def test_a_routing_failure_still_reports_the_tier_it_tried() -> None:
    """The operator needs to know *which* capability failed, not just that one did."""
    decision = _router(
        availability=Availability.unavailable("alpha", "beta", "gamma")
    ).route(_request())

    assert decision.tier == CapabilityTier.CODING_STANDARD


def test_a_routing_failure_explains_why_in_specific_terms() -> None:
    decision = _router(
        availability=Availability.unavailable("alpha", "beta", "gamma")
    ).route(_request())

    assert "alpha" in decision.reason
    assert "no usable model" in decision.reason


def test_a_routing_failure_never_raises() -> None:
    """A structured failure is a value. A run with nothing to run must say so,
    not throw out of a decision an operator is trying to read."""
    router = _router(
        availability=Availability.unavailable("alpha", "beta", "gamma")
    )

    assert router.route(_request()).selection is SelectionKind.NONE


def test_an_unknown_tier_routes_to_nothing_rather_than_raising() -> None:
    """A request naming a tier no registry carries must fail visibly, not
    silently substitute the default."""
    router = _router()

    decision = router.route(RouteRequest(agent="byte", tier="nope"))

    assert decision.selection is SelectionKind.NONE
    assert decision.health is Health.RED
    assert "nope" in decision.reason


def test_routing_to_an_unimplemented_provider_names_the_provider() -> None:
    """openai is declared and has no gateway. The failure says which provider and
    why, rather than surfacing an import error inside a run."""
    document = _doc(
        primary='primary = { model = "gpt-x", provider = "openai" }',
        fallbacks="fallbacks = []",
        tiers=(CapabilityTier.CODING_STANDARD,),
    ).replace(
        'light = "general_light"\nstandard = "coding_standard"\n'
        'hard = "reasoning_heavy"',
        'light = "coding_standard"\nstandard = "coding_standard"\n'
        'hard = "coding_standard"',
    )
    router = _router(document, availability=Availability.all_available())

    decision = router.route(RouteRequest(agent="byte", tier="coding_standard"))

    assert decision.selection is SelectionKind.NONE
    assert "openai" in decision.reason


# --------------------------------------------------------------------------
# Explicit selection
# --------------------------------------------------------------------------


def test_a_request_may_name_a_tier_directly() -> None:
    """An operator overriding the table is a legitimate thing to do."""
    decision = _router().route(RouteRequest(agent="byte", tier="reasoning_heavy"))

    assert decision.tier == CapabilityTier.REASONING_HEAVY


def test_an_explicit_tier_wins_over_the_routing_table() -> None:
    decision = _router().route(
        RouteRequest(agent="byte", task="coding", difficulty="light",
                     tier="reasoning_heavy")
    )

    assert decision.tier == CapabilityTier.REASONING_HEAVY


# --------------------------------------------------------------------------
# Agent / model decoupling
# --------------------------------------------------------------------------


def test_no_agent_module_names_a_model_identifier() -> None:
    """The decoupling claim, asserted structurally.

    If a model id ever appears in an agent, the registry is no longer the only
    place models are named, and replacing one becomes a code change. Scanned
    rather than reviewed because this is exactly the kind of regression that
    looks harmless in review.
    """
    package = pathlib.Path("forge/agents")
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                # A provider-qualified id, or anything with a model-shaped
                # slash. Compared against the shipped default's own vocabulary
                # so this test does not have to hard-code model ids itself.
                if (
                    "/" in text
                    and not text.startswith(("forge/", "http", "/", "./"))
                    and not text.endswith(("/",))
                    and " " not in text
                    and any(known in text for known in _known_ids())
                ):
                    offenders.append(f"{path.name}:{node.lineno}: {text!r}")

    assert offenders == []


def _known_ids() -> tuple[str, ...]:
    """Fragments of model ids as they appear in the shipped configuration.

    Read from the package rather than pasted, so the guard keeps working when
    the default registry changes and never itself becomes a hard-coded list.
    """
    from forge.models.registry import default_config_path

    text = default_config_path().read_text(encoding="utf-8")
    found: set[str] = set()
    for line in text.splitlines():
        if "model =" in line:
            value = line.split("model =", 1)[1]
            value = value.strip().strip(",").strip('"').strip("'")
            if value:
                found.add(value.split("/")[-1])
    return tuple(sorted(found))


def test_swapping_every_model_changes_routing_without_touching_agent_code() -> None:
    """The scenario this layer exists for.

    `alpha` disappears and `omega` takes over. The request is identical, the
    agent is identical, the routing table is identical, and only configuration
    changed.
    """
    before = _router().route(_request())
    after = _router(
        _doc(
            primary='primary = { model = "vendor-x/omega", provider = "openrouter" }',
            fallbacks='fallbacks = [{ model = "local-omega", provider = "local" }]',
        )
    ).route(_request())

    assert before.model == "alpha"
    assert after.model == "vendor-x/omega"
    assert before.tier == after.tier


def test_a_replaced_model_does_not_change_the_tier_chosen() -> None:
    """Tiers are the stable contract; the model behind one is not."""
    before = _router().route(_request("coding", "hard"))
    after = _router(
        _doc(primary='primary = { model = "vendor-x/omega", provider = "openrouter" }')
    ).route(_request("coding", "hard"))

    assert before.tier is after.tier is CapabilityTier.REASONING_HEAVY


def test_the_agent_is_reported_on_the_decision() -> None:
    """Recorded so a routing audit says whose request this was."""
    decision = _router().route(_request())

    assert decision.agent == "byte"


def test_the_router_holds_no_gateway() -> None:
    """Deciding is not calling.

    A router that could build a gateway would be able to make a network call,
    and ``forge route`` -- an inspection command -- would stop being safe to
    run offline.
    """
    source = pathlib.Path("forge/models/router.py").read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    for module in ("openrouter", "local", "scripted", "urllib", "socket", "http"):
        assert not any(module in name for name in imported), module


def test_the_router_imports_nothing_from_the_safety_stack() -> None:
    """The router has no opinion about permissions and cannot acquire one."""
    source = pathlib.Path("forge/models/router.py").read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "tools" not in node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "tools" not in alias.name


# --------------------------------------------------------------------------
# Health reporting across the whole registry
# --------------------------------------------------------------------------


def test_the_router_reports_health_for_every_configured_tier() -> None:
    """The data a future UI health indicator needs, per tier."""
    router = _router(availability=Availability.unavailable("alpha"))

    health = router.health_by_tier()

    assert set(health) == {tier.value for tier in CapabilityTier}
    assert health["general_light"] is Health.YELLOW


def test_health_by_tier_agrees_with_the_registry() -> None:
    """One definition of health. A status view and a routing decision that
    disagreed would make both untrustworthy."""
    registry = parse_registry(_doc(), availability=Availability.unavailable("alpha"))
    router = ModelRouter(registry)

    for name, value in router.health_by_tier().items():
        assert value is registry.health(name)


def test_the_packaged_default_registry_loads() -> None:
    """A fresh install has a working registry with no configuration written."""
    registry = load_registry()

    assert set(t.value for t in CapabilityTier) <= set(registry.tier_names())


def test_the_packaged_default_does_not_claim_verified_health() -> None:
    """Loading a configuration is not checking a provider.

    The packaged default names models nobody has contacted, so it must report
    health it cannot vouch for as unverified rather than GREEN.
    """
    registry = load_registry()

    assert registry.availability.verified is False


def test_the_packaged_default_does_not_report_green_merely_because_it_loaded() -> None:
    """The correction to a tempting shortcut.

    An unimplemented provider is not usable, so a tier whose only models sit on
    one must be RED even though the file parsed cleanly.
    """
    from forge.models.registry import parse_registry as parse

    document = (
        '[providers.openai]\ncredential_env = "FORGE_OPENAI_API_KEY"\n'
        '[tiers.general_light]\n'
        'primary = { model = "gpt-x", provider = "openai" }\n'
        "\n[routing]\ndefault_tier = \"general_light\"\n"
    )
    registry = parse(document, availability=Availability.all_available())

    assert registry.health(CapabilityTier.GENERAL_LIGHT) is Health.RED


# --------------------------------------------------------------------------
# Registry independence
# --------------------------------------------------------------------------


def test_the_router_accepts_a_registry_built_by_hand() -> None:
    """No file required: the router takes a registry, not a path.

    Keeps the router testable without the filesystem and lets a caller compose
    a registry from several sources.
    """
    registry: ModelRegistry = parse_registry(
        _doc(), availability=Availability.all_available()
    )

    assert ModelRouter(registry).route(_request()).model == "alpha"


@pytest.mark.parametrize("difficulty", ["light", "standard", "hard"])
def test_every_configured_difficulty_routes(difficulty: str) -> None:
    assert _router().route(_request("coding", difficulty)).selection in (
        SelectionKind.PRIMARY,
        SelectionKind.FALLBACK,
    )