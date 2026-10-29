"""The Workbench model selector: one (provider, model) binding, changed between runs.

This module is the Workbench's single source of truth for *which model* the next
run will use, and it exists to settle one fact that the CLI's ``--model`` override
does not: a model id and its provider are two facts that must travel together,
or a local model id ends up POSTed to a paid provider. So the selector holds a
provider and a model as one :class:`ModelSelection` value, never as two
independently-mutable fields that could drift apart.

Three properties, each a decision:

**The gateway is derived from the provider, never chosen beside it.**
:meth:`ModelSelector.resolve` builds the gateway from :data:`selection.provider`
through :func:`forge.models.providers.build_gateway` -- the one place the
provider -> gateway mapping lives. There is no code path that takes a ``local``
provider and yields an OpenRouter gateway, so a local model cannot be routed to
OpenRouter, and no fallback ever substitutes a paid provider for an unavailable
local one: the gateway is what the selection says, or the run fails.

**A change applies to the next run, not the current one.** The selection is read
once, at run start, by the runtime factory. While a run is in flight the server
refuses selection changes (``409``), so a running task keeps the model it started
on and the new selection takes effect on the following run.

**The catalog is a listing, not a decision.** :meth:`refresh_catalog` asks each
provider for its available models through the existing verifier seam (the same
``/v1/models`` endpoints ``forge models status --verify`` reads). An unavailable
provider is a reported ``available=False`` with a reason -- surfaced to the
operator -- never a silent substitution and never an exception. The catalog is
changed only by an explicit refresh, never by a read.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass

#: The providers the selector offers, in display order. "local" is the Space
#: Monkey Ollama tunnel / llama.cpp gateway; "openrouter" is the paid host.
PROVIDERS = ("local", "openrouter")


@dataclass(frozen=True)
class ModelSelection:
    """One provider/model pair, bound together so they cannot drift apart.

    Attributes:
        provider: ``"local"`` or ``"openrouter"``.
        model: The provider-specific model identifier (an Ollama tag like
            ``qwen3.5:27b-q4_K_M`` for local, a ``vendor/name`` id for
            OpenRouter).
    """

    provider: str
    model: str


class ModelSelector:
    """The current model binding, the gateways to serve it, and the per-provider
    model catalog.

    Thread-safe: the selection, the per-provider gateways, and the catalog
    snapshot are each replaced under one lock rather than mutated in place, so a
    read during a change sees either the old value or the new one.

    Args:
        environ: The credential/configuration mapping. Held for constructing
            gateways (via :func:`forge.models.providers.build_gateway`) and
            listing models (via :func:`forge.models.verify.list_provider_models`).
            Injected, never read from ``os.environ``, and never published.
        initial: The selection the workbench starts on. Typically the CLI's
            resolved (provider, model) pair.

    Raises:
        ValueError: ``initial`` names a provider the selector does not offer, or
            carries an empty model id.
    """

    def __init__(self, environ: Mapping[str, str], initial: ModelSelection) -> None:
        self._environ = environ
        self._lock = threading.Lock()
        self._selection = self._validate(initial)
        self._gateways: dict[str, object] = {}
        self._catalog: dict[str, object] = {}

    @staticmethod
    def _validate(selection: ModelSelection) -> ModelSelection:
        """Refuse a selection that names an unknown provider or an empty model.

        Validating here -- rather than in the server route alone -- means every
        construction site gets the same rule, and a provider Forge has no
        gateway for can never become the running selection.
        """
        if selection.provider not in PROVIDERS:
            raise ValueError(
                f"unknown model provider {selection.provider!r}; "
                f"expected {' or '.join(PROVIDERS)}"
            )
        if not selection.model.strip():
            raise ValueError("a model id is required")
        return ModelSelection(selection.provider, selection.model.strip())

    # -- selection -------------------------------------------------------

    @property
    def selection(self) -> ModelSelection:
        """The current binding, as one value."""
        with self._lock:
            return self._selection

    def set_selection(self, selection: ModelSelection) -> None:
        """Replace the current binding. Applies to the next run, not the current.

        The caller (the server route) guarantees no run is in flight, so nothing
        here is asked to reconcile with a running task. Raises for a selection
        :meth:`_validate` would refuse.
        """
        with self._lock:
            self._selection = self._validate(selection)

    # -- resolution ------------------------------------------------------

    def resolve(self):
        """``(gateway, model_id)`` for the current selection.

        The gateway is built from the selection's provider, lazily and cached
        per provider, so the binding (requirement: a local model cannot reach
        OpenRouter) is enforced exactly here. ``build_gateway`` raises for an
        unknown provider -- unreachable here because ``_validate`` forbade it --
        and the OpenRouter gateway raises ``ProviderCredentialMissing`` at
        construction when no key is set, which becomes a run failure rather
        than a fallback.
        """
        selection = self.selection
        gateway = self._gateway_for(selection.provider)
        return gateway, selection.model

    def _gateway_for(self, provider: str):
        with self._lock:
            gateway = self._gateways.get(provider)
            if gateway is None:
                from ..models.providers import build_gateway

                gateway = build_gateway(provider, self._environ)
                self._gateways[provider] = gateway
            return gateway

    # -- catalog ---------------------------------------------------------

    @property
    def catalog(self) -> dict[str, object]:
        """The last refreshed per-provider listing, keyed by provider name.

        Read-only under the lock. An empty mapping means ``refresh_catalog`` has
        not run yet (or failed to record anything); a read never probes.
        """
        with self._lock:
            return dict(self._catalog)

    def refresh_catalog(self) -> dict[str, object]:
        """Ask each provider for its available models and replace the catalog.

        Synchronous and bounded by the verifier's own timeout, one attempt per
        provider. Never raises for a provider-side failure: an unreachable
        provider is recorded as ``available=False`` with a reason. The caller
        runs this off the event loop for the same reason ``ModelHealth.verify``
        is run off it.
        """
        # Imported here, by name, so the listing path (which opens a socket)
        # is not imported by every module that merely imports the selector.
        from ..models.verify import list_provider_models

        snapshot: dict[str, object] = {}
        for provider in PROVIDERS:
            listing = list_provider_models(provider, environ=self._environ)
            snapshot[provider] = {
                "available": listing.available,
                "models": list(listing.models),
                "error": listing.error,
            }
        with self._lock:
            self._catalog = snapshot
        return dict(snapshot)

    # -- API shape -------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """The selector's whole state, as the HTTP surface publishes it."""
        selection = self.selection
        with self._lock:
            catalog = dict(self._catalog)
        return {
            "selected": {"provider": selection.provider, "model": selection.model},
            "providers": list(PROVIDERS),
            "available": catalog,
        }