"""Tests for the model registry: configuration, validation, and health.

The registry is the one layer that knows a model id exists. Agents must not,
and this file protects that by asserting on configuration *and* on health
semantics, because a registry that loads a file is not the same as a registry
that can say which models are usable.

The three states a slot can be in are deliberately distinct and are asserted
separately here:

    CONFIGURED   present and enabled, with nothing saying it is unusable
    DISABLED     present, ``enabled = false``
    UNAVAILABLE  enabled, but the injected availability source says no
    UNROUTABLE   enabled, but its provider has no gateway implementation

``CONFIGURED`` is not ``AVAILABLE``. Health is computed from usability, so a
tier whose only models are disabled or unroutable is RED even though the file
parsed without complaint.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from forge.models.providers import ProviderCatalog, ProviderSpec
from forge.models.registry import (
    Availability,
    SlotState,
    load_registry,
    parse_registry,
)
from forge.models.tiers import CapabilityTier, Health, RegistryConfigError


ALL_TIERS = """
[tiers.general_light]
primary = { model = "alpha-light", provider = "openrouter" }

[tiers.coding_standard]
primary = { model = "alpha-code", provider = "openrouter" }
fallbacks = [{ model = "beta-code", provider = "local" }]

[tiers.reasoning_heavy]
primary = { model = "alpha-reason", provider = "openrouter" }
"""

CODING_ROUTING = """
[routing]
default_tier = "coding_standard"

[routing.tasks.coding]
standard = "coding_standard"
hard = "reasoning_heavy"
"""


def _toml(tiers: str = ALL_TIERS, routing: str | None = None) -> str:
    """A document.

    ``routing`` defaults to a table naming the first tier ``tiers`` declares, so
    a test that narrows the tiers does not also have to narrow the routing to
    keep the document internally consistent.
    """
    if routing is None:
        first = tiers.split("[tiers.", 1)[1].split("]", 1)[0].strip()
        routing = f'\n[routing]\ndefault_tier = "{first}"\n'
    return f"{tiers}\n{routing}\n"


def _registry(toml: str = _toml(), **kwargs):
    return parse_registry(toml, **kwargs)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_a_registry_exposes_exactly_the_three_capability_tiers() -> None:
    registry = _registry()

    assert registry.tier_names() == (
        CapabilityTier.GENERAL_LIGHT,
        CapabilityTier.CODING_STANDARD,
        CapabilityTier.REASONING_HEAVY,
    )


def test_a_tier_keeps_its_primary_and_its_fallbacks_in_order() -> None:
    registry = _registry()

    tier = registry.tier(CapabilityTier.CODING_STANDARD)

    assert tier is not None
    assert tier.primary.model == "alpha-code"
    assert [slot.model for slot in tier.fallbacks] == ["beta-code"]


def test_a_tier_may_declare_several_fallbacks() -> None:
    registry = _registry(
        _toml(
            tiers="""
[tiers.coding_standard]
primary = { model = "a", provider = "openrouter" }
fallbacks = [
    { model = "b", provider = "local" },
    { model = "c", provider = "openai" },
]
"""
        )
    )

    tier = registry.tier(CapabilityTier.CODING_STANDARD)

    assert tier is not None
    assert [slot.model for slot in tier.fallbacks] == ["b", "c"]


def test_a_tier_with_no_fallbacks_is_valid() -> None:
    """One or more fallbacks is supported; zero is a real choice, not an error."""
    registry = _registry()

    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)

    assert tier is not None
    assert tier.fallbacks == ()


# --------------------------------------------------------------------------
# Invalid configuration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document,fragment",
    [
        ("[tiers.general_light]\nprimary = 'not-a-table'\n", "primary"),
        (
            '[tiers.general_light]\nprimary = { provider = "openrouter" }\n',
            "model",
        ),
        (
            '[tiers.general_light]\nprimary = { model = "a", provider = "x" }\n',
            "provider",
        ),
        (
            "[tiers.general_light]\nprimary = { model = '', provider = 'local' }\n",
            "model",
        ),
        (
            '[tiers.general_light]\nprimary = { model = "a", provider = "local", '
            'enabled = "yes" }\n',
            "enabled",
        ),
        ("[tiers.general_light]\n", "primary"),
        (
            "[tiers.general_light]\nprimary = { model = 'a', provider = 'local' }\n"
            'fallbacks = { model = "b", provider = "local" }\n',
            "fallbacks",
        ),
    ],
)
def test_malformed_tier_configuration_is_refused(document: str, fragment: str) -> None:
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(document)

    assert fragment in str(caught.value)


def test_an_unknown_capability_tier_name_is_refused() -> None:
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(
            '[tiers.general_light]\nprimary = { model = "a", provider = "local" }\n'
            '[tiers.general-ligt]\nprimary = { model = "b", provider = "local" }\n'
        )

    assert "general-ligt" in str(caught.value)


def test_a_misspelled_top_level_table_is_refused() -> None:
    """A typo must not silently load an empty registry.

    An unrecognised table is far more likely to be a mistake than an extension,
    and silently ignoring it is how a working configuration becomes a no-op.
    """
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(
            '[providers.local]\n'
            '[tier.general_light]\nprimary = { model = "a", provider = "local" }\n'
        )

    assert "tier" in str(caught.value)


def test_an_unknown_provider_named_by_a_slot_is_refused() -> None:
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(
            '[tiers.general_light]\nprimary = { model = "a", provider = "acme" }\n'
        )

    assert "acme" in str(caught.value)


def test_a_fallback_repeating_the_primary_is_refused() -> None:
    """The same model twice makes the fallback list lie about what it covers."""
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(
            "[tiers.general_light]\n"
            'primary = { model = "a", provider = "local" }\n'
            'fallbacks = [{ model = "a", provider = "local" }]\n'
        )

    assert "a" in str(caught.value)


def test_a_routing_entry_naming_an_unknown_tier_is_refused() -> None:
    with pytest.raises(RegistryConfigError) as caught:
        parse_registry(
            _toml(
                routing="""
[routing]
default_tier = "general_light"

[routing.tasks.coding]
standard = "coding_turbo"
"""
            )
        )

    assert "coding_turbo" in str(caught.value)


def test_broken_toml_is_refused_without_a_traceback() -> None:
    with pytest.raises(RegistryConfigError):
        parse_registry("[tiers.general_light\nprimary = ")


def test_a_missing_file_is_refused_without_a_traceback(tmp_path) -> None:
    with pytest.raises(RegistryConfigError) as caught:
        load_registry(tmp_path / "absent.toml")

    assert "absent.toml" in str(caught.value)


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def test_the_catalog_implements_openrouter_and_local() -> None:
    catalog = ProviderCatalog.default()

    assert catalog.gateway_for("openrouter") is not None
    assert catalog.gateway_for("local") is not None


def test_the_catalog_declares_openai_and_anthropic_without_a_gateway() -> None:
    """Declared, so configuration validates and status can show them; not wired."""
    catalog = ProviderCatalog.default()

    for name in ("openai", "anthropic"):
        spec = catalog.spec(name)
        assert spec is not None
        assert spec.gateway_factory is None
        assert spec.credential_env


def test_an_unknown_provider_has_no_spec() -> None:
    assert ProviderCatalog.default().spec("acme") is None


def test_a_provider_may_be_declared_inline() -> None:
    """A local endpoint Forge has never heard of is configuration, not a code change."""
    catalog = ProviderCatalog.default()

    registry = parse_registry(
        _toml(
            tiers='[providers.house]\ncredential_env = "FORGE_HOUSE_KEY"\n'
            '[tiers.general_light]\n'
            'primary = { model = "house-1", provider = "house" }\n',
            routing=LIGHT_ROUTING,
        ),
        catalog=catalog,
    )

    slot = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert slot is not None
    assert slot.primary.provider == "house"


def test_a_declared_provider_still_has_no_gateway() -> None:
    catalog = ProviderCatalog.default()

    registry = parse_registry(
        _toml(
            tiers='[providers.house]\ncredential_env = "FORGE_HOUSE_KEY"\n'
            '[tiers.general_light]\n'
            'primary = { model = "house-1", provider = "house" }\n',
            routing=LIGHT_ROUTING,
        ),
        catalog=catalog,
    )

    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None
    assert catalog.gateway_for("house") is None


# --------------------------------------------------------------------------
# Slot state: configured, enabled, available
# --------------------------------------------------------------------------


def test_an_enabled_slot_is_configured() -> None:
    registry = _registry()
    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    assert registry.slot_state(tier.primary) is SlotState.CONFIGURED


def test_a_disabled_slot_is_reported_as_disabled_not_configured() -> None:
    """Disabled is a different fact from present. A UI showing both needs both."""
    registry = _registry(
        _toml(
            tiers="""
[tiers.general_light]
primary = { model = "a", provider = "local", enabled = false }
"""
        )
    )
    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    assert tier.primary.enabled is False
    assert registry.slot_state(tier.primary) is SlotState.DISABLED


def test_an_available_slot_is_usable() -> None:
    registry = _registry(availability=Availability.all_available())
    tier = registry.tier(CapabilityTier.CODING_STANDARD)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is True


def test_an_unavailable_slot_is_not_usable_even_though_it_is_configured() -> None:
    registry = _registry(
        availability=Availability.unavailable("alpha-code")
    )
    tier = registry.tier(CapabilityTier.CODING_STANDARD)
    assert tier is not None

    assert tier.primary.enabled is True
    assert registry.slot_state(tier.primary) is SlotState.UNAVAILABLE
    assert registry.slot_usable(tier.primary) is False


def test_a_disabled_slot_is_not_usable_even_when_reported_available() -> None:
    """Availability cannot re-enable a model an operator switched off."""
    registry = _registry(
        _toml(
            tiers="""
[tiers.general_light]
primary = { model = "a", provider = "local", enabled = false }
"""
        ),
        availability=Availability.all_available(),
    )
    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is False


def test_availability_may_be_keyed_by_provider_and_model() -> None:
    """The same model id under two providers is two different models."""
    registry = _registry(
        _toml(
            tiers="""
[tiers.general_light]
primary = { model = "a", provider = "local" }
fallbacks = [{ model = "a", provider = "openrouter" }]
"""
        ),
        availability={"local/a": False},
    )
    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is False
    assert registry.slot_usable(tier.fallbacks[0]) is True


def test_availability_may_be_a_callable() -> None:
    """The seam a future probe plugs into, proven callable today with no network."""
    registry = _registry(availability=lambda slot: slot.provider == "openrouter")
    tier = registry.tier(CapabilityTier.CODING_STANDARD)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is True
    assert registry.slot_usable(tier.fallbacks[0]) is False


def test_a_missing_availability_source_leaves_health_unverified() -> None:
    """No probe means the registry must not claim to have checked anything.

    Health is still computed, but ``verified`` is False, so a future UI indicator
    can show "assumed" rather than a confident green it cannot support.
    """
    registry = _registry()

    assert registry.availability.verified is False


def test_an_injected_availability_source_makes_health_verified() -> None:
    registry = _registry(availability=Availability.all_available())

    assert registry.availability.verified is True


def test_a_model_absent_from_an_availability_mapping_is_available() -> None:
    """Absence means "not known to be broken", which is not the same as broken.

    Demanding that a mapping enumerate every model would make adding a fallback
    a two-place edit and would mark a newly configured model unusable by default
    -- failing in the dangerous direction. So absence counts as available.
    """
    registry = _registry(availability=Availability.unavailable("alpha-code"))

    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is True


def test_absence_from_a_mapping_does_not_become_verified_healthy() -> None:
    """The rule that must survive a future probe.

    A model nobody mentioned in the availability mapping is usable for routing,
    and that must never be reported as *verified*. If a probe lands here and
    treats "no entry" as "confirmed working", a GREEN tier would start meaning
    "Forge checked and it worked" for a model it never contacted.
    """
    registry = _registry(availability=Availability.unavailable("alpha-code"))
    tier = registry.tier(CapabilityTier.GENERAL_LIGHT)
    assert tier is not None

    # Usable for routing...
    assert registry.slot_usable(tier.primary) is True
    # ...but the mapping said nothing about *this* model. Its usability came
    # from the absence of an entry, which is the whole risk: a source that
    # answers "available" by having never heard of a model must not be
    # mistaken for one that checked it.
    assert registry.availability.available(tier.primary) is True
    mapping = registry.availability.source
    assert isinstance(mapping, Mapping)
    assert f"{tier.primary.provider}/{tier.primary.model}" not in mapping
    assert f"*{tier.primary.model}" not in mapping


def test_a_none_source_is_unverified_even_though_everything_routes() -> None:
    """Every model usable, and nothing verified. Both halves, together."""
    registry = _registry(availability=Availability.none())
    tier = registry.tier(CapabilityTier.CODING_STANDARD)
    assert tier is not None

    assert registry.slot_usable(tier.primary) is True
    assert registry.availability.verified is False


def test_the_verified_flag_survives_rebuilding_the_registry() -> None:
    """`with_availability` must not quietly upgrade an unverified registry.

    The obvious future bug: a caller who attaches a source, reads health, then
    reattaches the default, and the cached "verified" survives the second call.
    """
    registry = _registry(availability=Availability.none())

    rebuilt = registry.with_availability(Availability.none())

    assert rebuilt.availability.verified is False
    assert rebuilt.health(CapabilityTier.GENERAL_LIGHT) is Health.GREEN


def test_a_missing_credential_does_not_make_health_verified() -> None:
    """Credentials are configuration, not availability."""
    registry = _registry(credentials={"FORGE_OPENROUTER_API_KEY": "k"})

    assert registry.availability.verified is False


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_a_healthy_primary_is_green() -> None:
    registry = _registry(availability=Availability.all_available())

    assert registry.health(CapabilityTier.GENERAL_LIGHT) is Health.GREEN


def test_an_unavailable_primary_with_a_usable_fallback_is_yellow() -> None:
    registry = _registry(availability=Availability.unavailable("alpha-code"))

    assert registry.health(CapabilityTier.CODING_STANDARD) is Health.YELLOW


def test_a_disabled_primary_with_a_usable_fallback_is_yellow() -> None:
    registry = _registry(
        _toml(
            tiers="""
[tiers.coding_standard]
primary = { model = "a", provider = "local", enabled = false }
fallbacks = [{ model = "b", provider = "local" }]
"""
        ),
        availability=Availability.all_available(),
    )

    assert registry.health(CapabilityTier.CODING_STANDARD) is Health.YELLOW


def test_a_tier_with_no_usable_model_is_red() -> None:
    """Every model present but every one of them unusable is not a healthy tier."""
    registry = _registry(
        _toml(
            tiers="""
[tiers.general_light]
primary = { model = "a", provider = "local", enabled = false }
fallbacks = [{ model = "b", provider = "local", enabled = false }]
"""
        )
    )

    assert registry.health(CapabilityTier.GENERAL_LIGHT) is Health.RED


def test_a_tier_routing_only_to_an_unimplemented_provider_is_red() -> None:
    """Declared is not usable: openai is a known name with no gateway behind it."""
    registry = _registry(
        _toml(
            tiers="""
[tiers.general_light]
primary = { model = "gpt-x", provider = "openai" }
"""
        ),
        availability=Availability.all_available(),
    )

    assert registry.health(CapabilityTier.GENERAL_LIGHT) is Health.RED


ONLY_LIGHT = """
[tiers.general_light]
primary = { model = "a", provider = "openrouter" }
"""

LIGHT_ROUTING = """
[routing]
default_tier = "general_light"
"""


def test_health_of_a_tier_the_registry_does_not_carry_is_red() -> None:
    registry = _registry(_toml(tiers=ONLY_LIGHT, routing=LIGHT_ROUTING))

    assert registry.health("coding_standard") is Health.RED


def test_health_never_reports_green_for_an_absent_tier() -> None:
    """Defence in depth against a status view that assumes presence means health."""
    registry = _registry(_toml(tiers=ONLY_LIGHT, routing=LIGHT_ROUTING))

    assert registry.health("reasoning_heavy") is Health.RED