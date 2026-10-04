"""The provider catalog: names Forge knows, and which of them have a gateway.

A provider is a **name and a credential**, not a class. :class:`ProviderSpec`
holds neither an SDK type nor a client, and the catalog maps names to gateway
*factories* by string. That is the whole decoupling mechanism: nothing in
``forge.models.registry`` or ``forge.models.router`` imports a gateway, and a
provider Forge has never heard of can be declared in configuration and used as
long as an operator supplies a gateway for it.

Two of the four required providers have gateways today (``openrouter``,
``local``) and two are declared without one (``openai``, ``anthropic``).
That asymmetry is intentional rather than an oversight:

*   Declaring them means ``forge models status`` can *show* that a slot points at
    Anthropic, so an operator can see the configuration is honest about what it
    intends rather than having it silently dropped.
*   Leaving them without a gateway means routing to one produces a structured
    failure naming the provider, never an import error deep inside a run.

An unimplemented provider is therefore **declared but unroutable**, and the
registry treats an unroutable slot as unusable. That is what keeps a tier from
reporting GREEN because a well-formed configuration file happened to parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

__all__ = ["ProviderSpec", "ProviderCatalog", "GatewayFactory"]


#: Builds a gateway. Takes no arguments here; credential lookup happens inside
#: the gateway, which is the layer that owns credentials.
GatewayFactory = Callable[[], object]


@dataclass(frozen=True)
class ProviderSpec:
    """One provider, by name.

    Attributes:
        name: The identifier used in configuration. Lowercase, and matched
            case-insensitively so ``OpenRouter`` and ``openrouter`` are the same
            provider rather than a typo that reads as a missing gateway.
        credential_env: The environment variable holding this provider's key, or
            ``None`` for a provider that needs no credential (a local endpoint).
            The *name* only. The catalog never reads a credential, never holds
            one, and cannot leak one -- it does not import ``os`` to do so.
        gateway_factory: Builds the gateway, or ``None`` when no implementation
            is registered yet.
    """

    name: str
    credential_env: str | None = None
    gateway_factory: GatewayFactory | None = None

    @property
    def routable(self) -> bool:
        """Whether a gateway exists for this provider.

        ``True`` means Forge *can* build one. It says nothing about whether the
        provider is reachable, which is what
        :class:`~forge.models.registry.Availability` is for.
        """
        return self.gateway_factory is not None


def _openrouter_factory() -> object:
    """Build an OpenRouter gateway.

    Imported lazily and by name. A module-level ``from .openrouter import ...``
    would make importing the catalog import a provider implementation, which
    would drag the credential-owning module into every ``forge route`` call for
    no reason.
    """
    from .openrouter import OpenRouterGateway

    return OpenRouterGateway


def _local_factory() -> object:
    from .local import LocalLlamaGateway

    return LocalLlamaGateway


class ProviderCatalog:
    """The providers Forge knows about.

    Built once from a mapping, so a test can supply a two-provider catalog and
    exercise every path without the real four being involved.
    """

    def __init__(self, specs: Mapping[str, ProviderSpec] | None = None) -> None:
        self._specs: dict[str, ProviderSpec] = {
            name.lower(): spec for name, spec in (specs or {}).items()
        }

    @classmethod
    def default(cls) -> "ProviderCatalog":
        """The catalog a fresh install runs with.

        Four providers, two of them routable. This is the only place in Forge
        where a provider name is written down, which is what makes "swap
        OpenRouter for something else" a configuration change rather than an
        edit across the codebase.
        """
        return cls(
            {
                "openrouter": ProviderSpec(
                    name="openrouter",
                    credential_env="FORGE_OPENROUTER_API_KEY",
                    gateway_factory=_openrouter_factory,
                ),
                "local": ProviderSpec(
                    name="local",
                    credential_env=None,
                    gateway_factory=_local_factory,
                ),
                # Declared, not implemented. Configuration naming these parses
                # and validates; routing to them reports why it cannot.
                "openai": ProviderSpec(
                    name="openai",
                    credential_env="FORGE_OPENAI_API_KEY",
                    gateway_factory=None,
                ),
                "anthropic": ProviderSpec(
                    name="anthropic",
                    credential_env="FORGE_ANTHROPIC_API_KEY",
                    gateway_factory=None,
                ),
            }
        )

    def with_spec(self, spec: ProviderSpec) -> "ProviderCatalog":
        """A copy of this catalog plus one provider.

        Returns a new catalog rather than mutating, so a registry built from an
        inline declaration cannot alter the catalog another caller is using.
        """
        merged = dict(self._specs)
        merged[spec.name.lower()] = spec
        return ProviderCatalog(merged)

    def spec(self, name: str) -> ProviderSpec | None:
        """The spec for ``name``, case-insensitively, or ``None``."""
        return self._specs.get(name.strip().lower())

    def names(self) -> tuple[str, ...]:
        """Every provider name, in declaration order."""
        return tuple(self._specs)

    def gateway_for(self, name: str) -> GatewayFactory | None:
        """The factory for ``name``, or ``None`` if unknown or unimplemented."""
        spec = self.spec(name)
        return spec.gateway_factory if spec is not None else None