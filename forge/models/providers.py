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

__all__ = [
    "ProviderSpec",
    "ProviderCatalog",
    "GatewayFactory",
    "VerifierFactory",
]


#: Builds a gateway. Takes no arguments here; credential lookup happens inside
#: the gateway, which is the layer that owns credentials.
GatewayFactory = Callable[[], object]

#: Builds a verifier. Takes an injected credential mapping -- a verifier reads a
#: credential to present it, so it is handed one explicitly rather than reading
#: ``os.environ``. The verifier factory is the *credential-touching* layer for
#: health, and saying so in the type is what keeps ``forge models status``
#: (without ``--verify``) from having any reason to import one.
VerifierFactory = Callable[[Mapping[str, str]], object]


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
        verifier_factory: Builds this provider's
            :class:`~forge.models.base.ModelVerifier`, or ``None`` when
            verification is not implemented.

            Deliberately a **separate field** rather than a second method on the
            gateway. The gateway Protocol documents defending its one-method
            shape, and verification is not a completion: it must never grow a
            retry loop, a tool path, or the ability to generate a token, all of
            which a shared interface would make someone else's problem to
            enforce. A provider with a gateway but no verifier is *declared but
            unchecked*, which :meth:`ProviderCatalog.verifier_for` reports as
            ``None`` rather than guessing at.
    """

    name: str
    credential_env: str | None = None
    gateway_factory: GatewayFactory | None = None
    verifier_factory: VerifierFactory | None = None

    @property
    def routable(self) -> bool:
        """Whether a gateway exists for this provider.

        ``True`` means Forge *can* build one. It says nothing about whether the
        provider is reachable, which is what
        :class:`~forge.models.registry.Availability` is for.
        """
        return self.gateway_factory is not None

    @property
    def verifiable(self) -> bool:
        """Whether Forge can actively check this provider.

        Distinct from :attr:`routable`: Forge may be able to send a request to a
        provider it cannot check (an unimplemented verifier), and treating the
        two as the same fact is how "we could have asked but did not" turns into
        a confident green.
        """
        return self.verifier_factory is not None


def _openrouter_factory() -> object:
    """Build an OpenRouter gateway.

    Imported lazily and by name. A module-level ``from .openrouter import ...``
    would make importing the catalog import a provider implementation, which
    would drag the credential-owning module into every ``forge route`` call for
    no reason.
    """
    from .openrouter import OpenRouterGateway

    return OpenRouterGateway


def _openrouter_verifier_factory(environ: Mapping[str, str]) -> object:
    """Build an OpenRouter verifier, handed the credential mapping.

    Imported by name for the same reason as the gateway factory above.
    """
    from .openrouter import OpenRouterVerifier

    return OpenRouterVerifier(environ=environ)


def _local_factory() -> object:
    from .local import LocalLlamaGateway

    return LocalLlamaGateway


def _local_verifier_factory(environ: Mapping[str, str]) -> object:
    """Build a local verifier.

    ``environ`` is handed to the verifier so it can read endpoint overrides.
    """
    from .local import LocalLlamaVerifier

    return LocalLlamaVerifier(environ=environ)


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
                    verifier_factory=_openrouter_verifier_factory,
                ),
                "local": ProviderSpec(
                    name="local",
                    credential_env=None,
                    gateway_factory=_local_factory,
                    verifier_factory=_local_verifier_factory,
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

    def verifier_for(self, name: str) -> VerifierFactory | None:
        """The verifier factory for ``name``, or ``None`` if there is none.

        ``None`` covers both an unknown provider and one that is routable but
        not verifiable. The distinction between "cannot be checked" and "checked
        and failed" belongs to whoever asked -- a probe reports the first as
        :class:`~forge.models.base.VerificationCode.UNIMPLEMENTED` and the
        second with whatever the provider actually said, and this method is the
        only thing that distinguishes them beforehand.
        """
        spec = self.spec(name)
        return spec.verifier_factory if spec is not None else None

    def verifiable_providers(self) -> tuple[str, ...]:
        """Names of providers Forge can actively check.

        Named here rather than in a probe module so "which providers can Forge
        verify" is answered by the same table that answers "which providers does
        Forge know".
        """
        return tuple(
            name for name, spec in self._specs.items() if spec.verifier_factory
        )