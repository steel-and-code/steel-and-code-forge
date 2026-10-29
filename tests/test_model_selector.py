"""The Workbench model selector: the (provider, model) binding, and its catalog.

The single property this file exists to protect is the one the issue is
*about*: a model id and its provider are one binding, and the gateway is derived
from the provider -- never chosen beside it. A ``local`` selection must yield
the local gateway and a local model id, so a local model can never be handed to
the paid OpenRouter gateway, and no unavailable provider is ever silently
substituted for another.

Nothing here reaches a network: listings are stubbed at the verifier boundary,
and the gateway is only *constructed* (never called), so the type assertions are
about wiring, not provider reachability.
"""

from __future__ import annotations

import urllib.error

import pytest

from forge.models.base import ModelListing
from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier
from forge.models.openrouter import OpenRouterGateway, OpenRouterVerifier
from forge.models.providers import build_gateway
from forge.ui.selector import PROVIDERS, ModelSelection, ModelSelector


# --------------------------------------------------------------------------
# build_gateway: one provider -> gateway mapping
# --------------------------------------------------------------------------


def test_build_gateway_maps_local_to_the_local_gateway() -> None:
    gateway = build_gateway("local", {})
    assert isinstance(gateway, LocalLlamaGateway)


def test_build_gateway_maps_openrouter_to_the_openrouter_gateway() -> None:
    environ = {"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    gateway = build_gateway("openrouter", environ)
    assert isinstance(gateway, OpenRouterGateway)


def test_build_gateway_refuses_an_unknown_provider() -> None:
    from forge.models.base import GatewayError

    with pytest.raises(GatewayError):
        build_gateway("bogus", {})


def test_build_gateway_without_a_key_does_not_fall_back_to_local() -> None:
    """A missing OpenRouter credential is a refusal, never a local substitute."""
    from forge.models.openrouter import ProviderCredentialMissing

    with pytest.raises(ProviderCredentialMissing):
        build_gateway("openrouter", {})


# --------------------------------------------------------------------------
# ModelSelection / ModelSelector: the binding
# --------------------------------------------------------------------------


def test_the_selection_is_one_value_not_two_fields() -> None:
    """Provider and model travel together; there is no way to set them apart."""
    selection = ModelSelection("local", "qwen3.5:27b-q4_K_M")
    assert selection.provider == "local"
    assert selection.model == "qwen3.5:27b-q4_K_M"


def test_resolve_derives_the_gateway_from_the_provider() -> None:
    """The requirement: a local model can never be handed to OpenRouter."""
    selector = ModelSelector({}, ModelSelection("local", "qwen3.5:27b-q4_K_M"))

    gateway, model = selector.resolve()

    assert isinstance(gateway, LocalLlamaGateway)
    assert model == "qwen3.5:27b-q4_K_M"


def test_an_openrouter_selection_yields_the_openrouter_gateway() -> None:
    environ = {"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    selector = ModelSelector(environ, ModelSelection("openrouter", "vendor/coder"))

    gateway, model = selector.resolve()

    assert isinstance(gateway, OpenRouterGateway)
    assert model == "vendor/coder"


def test_a_local_selection_never_yields_an_openrouter_gateway() -> None:
    """Even with an OpenRouter key present, ``local`` stays local.

    This is the negative form of the binding test: the presence of a paid
    provider's credential must not leak into a local selection.
    """
    environ = {"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"}
    selector = ModelSelector(environ, ModelSelection("local", "byte-coder"))

    gateway, _ = selector.resolve()

    assert isinstance(gateway, LocalLlamaGateway)


def test_set_selection_refuses_an_unknown_provider() -> None:
    selector = ModelSelector({}, ModelSelection("openrouter", "vendor/coder"))

    with pytest.raises(ValueError):
        selector.set_selection(ModelSelection("anthropic", "claude"))


def test_set_selection_refuses_an_empty_model() -> None:
    selector = ModelSelector({}, ModelSelection("openrouter", "vendor/coder"))

    with pytest.raises(ValueError):
        selector.set_selection(ModelSelection("local", "   "))


def test_set_selection_replaces_the_binding_atomically() -> None:
    selector = ModelSelector({}, ModelSelection("openrouter", "vendor/coder"))

    selector.set_selection(ModelSelection("local", "qwen3.5:27b-q4_K_M"))

    assert selector.selection == ModelSelection("local", "qwen3.5:27b-q4_K_M")


def test_a_constructed_selector_refuses_an_unknown_initial_provider() -> None:
    with pytest.raises(ValueError):
        ModelSelector({}, ModelSelection("anthropic", "claude"))


def test_the_provider_vocabulary_is_fixed_and_lists_local_first() -> None:
    assert PROVIDERS == ("local", "openrouter")
    assert PROVIDERS[0] == "local"


# --------------------------------------------------------------------------
# The catalog: listing, and offline behaviour
# --------------------------------------------------------------------------


def test_the_catalog_starts_empty_and_a_read_never_probes(monkeypatch) -> None:
    """``snapshot`` is a read of cached state; it never contacts a provider."""
    from forge.models import verify as verify_module

    probed: list[str] = []

    def fake_listing(provider, *, environ, timeout=None):
        probed.append(provider)
        return ModelListing(provider=provider, models=(), available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", fake_listing)

    selector = ModelSelector({}, ModelSelection("openrouter", "vendor/coder"))

    snapshot = selector.snapshot()

    assert snapshot["selected"] == {"provider": "openrouter", "model": "vendor/coder"}
    assert snapshot["available"] == {}
    assert probed == [], "snapshot must not probe"


def test_refresh_catalog_lists_each_provider_once(monkeypatch) -> None:
    from forge.models import verify as verify_module

    probed: list[str] = []
    counts = {"local": ("qwen3.5:27b-q4_K_M",), "openrouter": ("vendor/coder",)}

    def fake_listing(provider, *, environ, timeout=None):
        probed.append(provider)
        return ModelListing(provider=provider, models=counts[provider], available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", fake_listing)

    selector = ModelSelector({}, ModelSelection("openrouter", "vendor/coder"))
    selector.refresh_catalog()

    snapshot = selector.snapshot()
    assert snapshot["available"]["local"]["models"] == ["qwen3.5:27b-q4_K_M"]
    assert snapshot["available"]["openrouter"]["models"] == ["vendor/coder"]
    assert sorted(probed) == ["local", "openrouter"]


def test_an_unavailable_provider_is_reported_not_substituted(monkeypatch) -> None:
    """Requirement 6: local down is an error, and the selection does not move."""
    from forge.models import verify as verify_module

    def fake_listing(provider, *, environ, timeout=None):
        if provider == "local":
            return ModelListing(provider="local", available=False, error="ConnectionRefusedError")
        return ModelListing(provider="openrouter", models=("vendor/coder",), available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", fake_listing)

    selector = ModelSelector({}, ModelSelection("local", "qwen3.5:27b-q4_K_M"))
    selector.refresh_catalog()

    snapshot = selector.snapshot()

    # The unavailable local provider is reported with a reason ...
    assert snapshot["available"]["local"]["available"] is False
    assert snapshot["available"]["local"]["error"] == "ConnectionRefusedError"
    # ... and the selection is unchanged: no silent fallback to OpenRouter.
    assert snapshot["selected"]["provider"] == "local"
    # And a run would still go through the *local* gateway, which will fail
    # rather than silently reach the paid host.
    gateway, _ = selector.resolve()
    assert isinstance(gateway, LocalLlamaGateway)


def test_a_catalog_read_is_cached_after_refresh(monkeypatch) -> None:
    from forge.models import verify as verify_module

    calls = 0

    def fake_listing(provider, *, environ, timeout=None):
        nonlocal calls
        calls += 1
        return ModelListing(provider=provider, models=("m",), available=True)

    monkeypatch.setattr(verify_module, "list_provider_models", fake_listing)

    selector = ModelSelector({}, ModelSelection("openrouter", "m"))
    selector.refresh_catalog()
    for _ in range(3):
        selector.snapshot()

    assert calls == 2, "snapshot must read the cache, not re-probe"


# --------------------------------------------------------------------------
# The verifier listing methods (reused by the catalog seam)
# --------------------------------------------------------------------------


def test_local_list_models_returns_the_parsed_ids(monkeypatch) -> None:
    verifier = LocalLlamaVerifier(endpoint="http://127.0.0.1:9/v1/models")
    monkeypatch.setattr(
        verifier,
        "_get",
        lambda url, timeout: (200, '{"data": [{"id": "b"}, {"id": "a"}]}'),
    )

    listing = verifier.list_models()

    assert listing.available is True
    assert listing.models == ("a", "b")


def test_local_list_models_reports_an_unreachable_endpoint(monkeypatch) -> None:
    verifier = LocalLlamaVerifier(endpoint="http://127.0.0.1:9/v1/models")

    def down(url, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(verifier, "_get", down)

    listing = verifier.list_models()

    assert listing.available is False
    assert listing.error


def test_openrouter_list_models_without_a_key_is_credential_missing() -> None:
    verifier = OpenRouterVerifier(environ={})

    listing = verifier.list_models()

    assert listing.available is False
    assert listing.error == "credential_missing"


# --------------------------------------------------------------------------
# selectable_runtime_factory: resolution is per run, from the selector
# --------------------------------------------------------------------------


def test_the_runtime_resolves_the_current_selection_once_per_run(tmp_path) -> None:
    """Requirements 4 and 5, end to end at the runtime boundary.

    The factory samples the selector when the run starts, so a later change
    applies to the *next* run and never one already built -- and the gateway is
    always derived from the provider, never chosen beside it.
    """
    from forge.agents.byte import BYTE
    from forge.tools.workspace import Workspace
    from forge.ui.runner import selectable_runtime_factory

    class _Channel:
        pass

    selector = ModelSelector(
        {"FORGE_OPENROUTER_API_KEY": "«redacted:sk-…»"},
        ModelSelection("local", "qwen3.5:27b-q4_K_M"),
    )
    factory = selectable_runtime_factory(
        Workspace(tmp_path), profile=BYTE, selector=selector
    )

    first = factory(None, _Channel(), None)

    assert isinstance(first.gateway, LocalLlamaGateway)
    assert first.model_id == "qwen3.5:27b-q4_K_M"

    # Change the binding; the NEXT run carries it, the first does not.
    selector.set_selection(ModelSelection("openrouter", "vendor/coder"))

    second = factory(None, _Channel(), None)

    assert second is not first
    assert isinstance(second.gateway, OpenRouterGateway)
    assert second.model_id == "vendor/coder"
    # The first run's runtime is untouched by the later change.
    assert first.model_id == "qwen3.5:27b-q4_K_M"
    assert isinstance(first.gateway, LocalLlamaGateway)