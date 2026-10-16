"""The model registry: one configuration layer, and where model ids live.

**Every model identifier in Forge is named here and nowhere else.** An agent
asks for a capability; this layer answers with a model id. That is the whole
decoupling contract, and it is why ``alpha-reason`` can disappear tomorrow and
be replaced in a file.

Configuration is TOML, read with stdlib ``tomllib`` -- no new dependency, in a
package that declares zero runtime dependencies on the grounds that the
security layer should not pull in a dependency tree. Resolution order is
``$FORGE_MODELS_CONFIG``, then ``./models.toml``, then the packaged default, so
a fresh clone has a working registry and an operator can override any level
without editing the package.

Three ideas are kept apart on purpose, because collapsing them is what makes a
health indicator lie:

``configured``
    The slot exists in the file. Says nothing about usability.

``enabled``
    ``enabled = true``. An operator's decision, and the only thing a runtime
    probe must never be able to override.

``available``
    An injected :class:`Availability` source said yes. Absent by default, and
    *explicitly* absent: :attr:`Availability.verified` is False until a source
    is supplied, so nothing here can claim to have checked a provider it did not
    check. ``forge models status`` reports that distinction rather than smoothing
    it over.

Health follows usability, not configuration. A tier whose primary is disabled,
unavailable, or on a provider with no gateway is not GREEN, and
``[tier.general_light]`` is refused rather than ignored so a typo cannot quietly
produce an empty registry that looks healthy.

**No routing happens here.** Selecting a slot is ``forge.models.router``'s job,
and health is computed here because a status view and a routing decision must
never disagree about whether a tier is usable. Agent defaults are configuration
only; the router applies their precedence before ordinary tier routing.

**No probing happens here either.** ``forge.models.verify`` imports this
module; this module does not import that one, does not import ``urllib``, and
has no way to make a network call even if a future edit wanted to. A probe
reaches health through :meth:`Availability` and nothing else.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .providers import ProviderCatalog, ProviderSpec
from .tiers import CapabilityTier, Health, RegistryConfigError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .base import VerificationResult

__all__ = [
    "SlotState",
    "ModelSlot",
    "TierConfig",
    "AgentDefault",
    "RoutingTable",
    "Availability",
    "ModelRegistry",
    "parse_registry",
    "load_registry",
    "default_config_path",
    "MODELS_CONFIG_ENV",
    "LOCAL_CONFIG_NAME",
]

#: Overrides every other location.
MODELS_CONFIG_ENV = "FORGE_MODELS_CONFIG"

#: Looked for in the current directory before the packaged default.
LOCAL_CONFIG_NAME = "models.toml"

#: Top-level tables a configuration file may contain. Anything else is a typo
#: until proven otherwise, and a silently ignored typo is how a working registry
#: becomes a no-op that still reports GREEN.
_ALLOWED_TABLES = ("providers", "tiers", "routing", "agents")

_DEFAULT_DIFFICULTY = "standard"


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


class SlotState(str, Enum):
    """What is known about one slot.

    The three facts -- present, enabled, available -- are separate columns in
    this enum rather than a bitmask, because each is reported to the operator
    with a different reason attached.
    """

    #: Present and enabled, with no availability source saying otherwise. This
    #: is *not* a claim that the model answered.
    CONFIGURED = "configured"
    #: Present, ``enabled = false``.
    DISABLED = "disabled"
    #: Enabled, but the availability source said no.
    UNAVAILABLE = "unavailable"
    #: Enabled and available, but the provider has no gateway implementation, so
    #: no request could be made.
    UNROUTABLE = "unroutable"

    def __str__(self) -> str:
        return self.value


class Availability:
    """An injected source of "is this model usable right now".

    Four forms, all offline:

    *   ``None`` -- nothing is known. Every enabled slot is treated as usable,
        and :attr:`verified` is False.
    *   a mapping keyed ``"provider/model"`` or a bare model id.
    *   a callable taking a :class:`ModelSlot` and returning a bool.
    *   a mapping of ``"provider/model"`` to a
        :class:`~forge.models.base.VerificationResult` -- what
        ``forge models status --verify`` produces. The result's ``usable`` is
        the availability, and its ``reason`` is kept alongside so a status view
        can report *why* without re-deriving it.

    The callable form exists so a health probe drops into the same seam
    with no signature change. **No form performs network I/O**, and none of them
    may: a probe belongs in ``forge.models.verify``, which imports this module.

    A model absent from a mapping is available. Absence means "not known to be
    broken"; a mapping that must enumerate every model would make adding a
    fallback a two-place edit and would mark a newly configured model unusable
    by default, which is the dangerous direction to fail in.

    The ``VerificationResult`` form is the one place a reason travels, and it is
    still just data: this module imports the *value type* from ``base``, never a
    verifier, so the decision path gains no ability to reach a provider.
    """

    def __init__(
        self,
        source: (
            Mapping[str, bool]
            | Mapping[str, "VerificationResult"]
            | Callable[["ModelSlot"], bool]
            | None
        ) = None,
        *,
        verified: bool | None = None,
        reasons: Mapping[str, str] | None = None,
        codes: Mapping[str, str] | None = None,
    ) -> None:
        #: Optional per-slot reason text, keyed the same way the source is.
        #: Populated by a probe so a status consumer can explain a failure
        #: without a second lookup table of its own.
        self.reasons: Mapping[str, str] = dict(reasons or {})
        #: Optional per-slot :class:`~forge.models.base.VerificationCode`
        #: value, keyed identically. The machine-readable companion to
        #: :attr:`reasons`: a consumer that must tell an auth failure from a
        #: timeout should not have to pattern-match on prose.
        self.codes: Mapping[str, str] = dict(codes or {})
        self.source = source
        #: Whether anything actually checked availability.

        # An explicit ``verified`` is honoured even with no source, because
        # :meth:`all_available` *is* a statement: "treat every enabled slot as
        # usable, and know that is what you did." Otherwise verified is False --
        # a missing source is not a check, and reporting otherwise is how a
        # health indicator ends up confidently green about a provider nobody
        # contacted.
        self.verified = (
            verified if verified is not None else source is not None
        )

    @classmethod
    def coerce(
        cls, value: "Availability | Mapping[str, bool] | Mapping[str, VerificationResult] | Callable[[ModelSlot], bool] | None"
    ) -> "Availability":
        """Accept the raw forms at the API boundary.

        A caller passing a dict or a lambda should not have to know there is an
        :class:`Availability` class at all; the shape they care about is "where
        availability comes from".
        """
        if isinstance(value, cls):
            return value
        return cls(value)  # type: ignore[arg-type] - narrowed above

    @classmethod
    def none(cls) -> "Availability":
        """No source. Usable-by-default, explicitly unverified."""
        return cls(source=None, verified=False)

    @classmethod
    def all_available(cls) -> "Availability":
        """Everything is available. For tests and for offline operation."""
        return cls(source=None, verified=True)

    @classmethod
    def unavailable(cls, *models: str, available: Sequence[str] = ()) -> "Availability":
        """A mapping source naming models as unavailable.

        Excludes ``models`` from availability. ``available`` is accepted for
        symmetry with a mapping literal but does not add information -- absent
        means available -- so it is deliberately not part of the mapping.
        """
        return cls(source={f"*{name}": False for name in models})

    @classmethod
    def verified_results(
        cls, results: Mapping[str, "VerificationResult"]
    ) -> "Availability":
        """Build availability from probe results, keyed ``"provider/model"``.

        ``verified`` is **True unconditionally**, and that is the point worth
        being careful about: it means "Forge checked and here is the answer",
        not "everything is fine". A probe that found every model broken produces
        ``verified=True`` availability in which every slot is unavailable --
        which is the honest report. Setting it only on a clean sweep would make
        a total outage indistinguishable from never having looked.
        """
        from .base import VerificationResult as _Result

        usable: dict[str, bool] = {}
        reasons: dict[str, str] = {}
        codes: dict[str, str] = {}
        for key, result in results.items():
            if not isinstance(result, _Result):  # pragma: no cover - defensive
                continue
            usable[key] = result.usable
            reasons[key] = result.reason
            codes[key] = result.code.value

        return cls(source=usable, verified=True, reasons=reasons, codes=codes)

    def _lookup(self, slot: "ModelSlot"):
        """The source's entry for ``slot``, or ``None`` if it names none."""
        if self.source is None or callable(self.source):
            return None
        for key in (f"{slot.provider}/{slot.model}", f"*{slot.model}", slot.model):
            if key in self.source:
                return self.source[key]
        return None

    def available(self, slot: "ModelSlot") -> bool:
        """Whether ``slot`` is reported available by this source."""
        if self.source is None:
            return True
        if callable(self.source):
            return bool(self.source(slot))
        found = self._lookup(slot)
        if found is None:
            return True
        # A VerificationResult is truthy as an object, so availability has to be
        # read off it explicitly. Checking the type rather than duck-typing on
        # ``.usable`` means a plain bool mapping keeps working unchanged.
        return bool(getattr(found, "usable", found))

    def code_for(self, slot: "ModelSlot") -> str:
        """The :class:`~forge.models.base.VerificationCode` value for ``slot``.

        ``""`` when no source named this slot. Present as a machine-readable
        field alongside the human-readable :meth:`reason_for`, because a status
        consumer that has to match on prose to tell an auth failure from a
        timeout is a consumer that will eventually match it wrongly. Keeping both
        means the renderer writes the sentence and a UI binds the code.
        """
        for key in (f"{slot.provider}/{slot.model}", f"*{slot.model}", slot.model):
            if key in self.codes:
                return self.codes[key]
        return ""

    def reason_for(self, slot: "ModelSlot") -> str:
        """Why ``slot`` is available or not, or ``""`` when nothing is known.

        Distinct from availability on purpose. ``available(slot) is False`` says
        the model cannot serve; this says whether that was a rejected key, a
        timeout, or a model the provider no longer lists -- three different
        things for an operator to go and fix.
        """
        for key in (f"{slot.provider}/{slot.model}", f"*{slot.model}", slot.model):
            if key in self.reasons:
                return self.reasons[key]
        return ""


# --------------------------------------------------------------------------
# Slots and tiers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSlot:
    """One model in one tier.

    Attributes:
        model: The provider-specific model identifier. The only place in Forge
            where such a string is written down.
        provider: Which provider serves it. Per-slot rather than per-tier, so a
            tier's fallback can be a local model while its primary is remote.
        enabled: The operator's on/off switch. Nothing can set this at run time.
    """

    model: str
    provider: str
    enabled: bool = True

    @property
    def key(self) -> str:
        """``"provider/model"`` -- a stable identity for status and routing."""
        return f"{self.provider}/{self.model}"

    def state(self, catalog: ProviderCatalog, availability: Availability) -> SlotState:
        """What is known about this slot.

        Order matters and encodes the operator's precedence: *disabled* is
        checked before *unavailable*, because "you switched this off" is the more
        useful answer than "this is also currently unreachable". And a disabled
        slot is never UNROUTABLE, because there is no point reporting that a
        model nobody may use has no gateway.
        """
        if not self.enabled:
            return SlotState.DISABLED
        if not availability.available(self):
            return SlotState.UNAVAILABLE
        if catalog.gateway_for(self.provider) is None:
            return SlotState.UNROUTABLE
        return SlotState.CONFIGURED

    def usable(self, catalog: ProviderCatalog, availability: Availability) -> bool:
        """Whether this slot could serve a request right now.

        The single definition of usability. Health, routing, and the status view
        all call it, so they cannot disagree.
        """
        return self.state(catalog, availability) is SlotState.CONFIGURED


@dataclass(frozen=True)
class TierConfig:
    """A capability tier's declared models.

    ``primary`` is required, including when it is disabled: a tier with no
    primary is a configuration mistake rather than an empty-but-valid tier,
    because "which model is first" is the question this layer exists to answer.
    """

    tier: str
    primary: ModelSlot
    fallbacks: tuple[ModelSlot, ...] = ()

    @property
    def slots(self) -> tuple[ModelSlot, ...]:
        """Every slot in declaration order: primary, then fallbacks."""
        return (self.primary, *self.fallbacks)

    def health(
        self, catalog: ProviderCatalog, availability: Availability
    ) -> Health:
        """This tier's health, computed from usability.

        YELLOW rather than RED when a fallback is usable, because traffic still
        flows. RED when nothing is, and never a substitution across tiers: serving
        ``general_light`` traffic from ``coding_standard`` would answer a
        different question than the one asked.
        """
        if self.primary.usable(catalog, availability):
            return Health.GREEN
        if any(slot.usable(catalog, availability) for slot in self.fallbacks):
            return Health.YELLOW
        return Health.RED


@dataclass(frozen=True)
class AgentDefault:
    """An optional agent-specific model slot, configured in the registry."""

    model: str
    provider: str
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class RoutingTable:
    """Which tier serves which kind of work.

    Configured, not coded, so difficulty becomes an operator decision. Lookup is
    most-specific-first:

        routing.tasks.<task>.<difficulty>
        routing.tasks.<task>.default
        routing.default_tier

    A task or difficulty not named resolves to ``default_tier`` rather than
    failing. An agent handed work nobody anticipated should still run; refusing
    to route would make the fallback behaviour of the whole system depend on the
    completeness of a config file.
    """

    default_tier: str
    tasks: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def tier_for(self, task: str | None, difficulty: str | None) -> str:
        """The tier serving ``task`` at ``difficulty``."""
        if task:
            by_difficulty = self.tasks.get(task.strip().lower())
            if by_difficulty:
                chosen = (
                    by_difficulty.get((difficulty or "").strip().lower())
                    or by_difficulty.get(_DEFAULT_DIFFICULTY)
                )
                if chosen:
                    return chosen
        return self.default_tier

    def describe(self, task: str | None, difficulty: str | None) -> str:
        """Why :meth:`tier_for` chose what it chose.

        Returns a sentence fragment naming the rule that matched, so ``forge
        route`` can state a reason rather than implying the tier was obvious.
        """
        key_task = (task or "").strip().lower()
        key_difficulty = (difficulty or "").strip().lower()

        by_difficulty = self.tasks.get(key_task, {}) if key_task else {}
        if key_task and key_difficulty in by_difficulty:
            return f"routing.tasks.{key_task}.{key_difficulty}"
        if key_task and _DEFAULT_DIFFICULTY in by_difficulty:
            return f"routing.tasks.{key_task}.{_DEFAULT_DIFFICULTY}"
        if by_difficulty:
            return f"routing.tasks.{key_task} (no entry for difficulty {key_difficulty or 'unset'})"
        if key_task:
            return f"no routing entry for task {key_task!r}"
        return "routing.default_tier"


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRegistry:
    """Configured tiers, plus the catalog and availability they resolve against.

    Immutable and cheap to copy. Constructed by :func:`parse_registry` or
    :func:`load_registry`, and holding no gateway: this layer answers "which
    model", never "how do I call it".
    """

    tiers: Mapping[str, TierConfig]
    routing: RoutingTable
    agent_defaults: Mapping[str, AgentDefault]
    catalog: ProviderCatalog
    availability: Availability
    source: str = ""

    def tier_names(self) -> tuple[str, ...]:
        """Tier names in declaration order."""
        return tuple(self.tiers)

    def tier(self, name: str | None) -> TierConfig | None:
        """One tier by name, or ``None`` if this registry does not carry it."""
        if not name:
            return None
        return self.tiers.get(str(name).strip().lower())

    def slot_state(self, slot: ModelSlot) -> SlotState:
        """What is known about ``slot`` under this registry's availability.

        Lives here rather than on :class:`ModelSlot` because a slot's state is
        not a property of the slot: it is the slot read against a catalog and an
        availability source. Putting the method on the slot would mean
        broadcasting those two to every caller and inviting them to answer with
        different results.
        """
        return slot.state(self.catalog, self.availability)

    def slot_usable(self, slot: ModelSlot) -> bool:
        """Whether ``slot`` could serve a request right now."""
        return slot.usable(self.catalog, self.availability)

    def health(self, name: str | None) -> Health:
        """A tier's health, or RED for a tier this registry does not carry.

        RED rather than a sentinel: an absent tier cannot serve traffic, and a
        status view that raised on a missing tier would show a crash where the
        truth is simply "this capability is not configured".
        """
        tier = self.tier(name)
        if tier is None:
            return Health.RED
        return tier.health(self.catalog, self.availability)

    def resolve_tier(self, task: str | None, difficulty: str | None) -> RoutingTable:
        """The routing table consulted for a request."""
        return self.routing

    def with_availability(
        self,
        availability: (
            Availability | Mapping[str, bool] | Callable[[ModelSlot], bool]
        ),
    ) -> "ModelRegistry":
        """A copy of this registry using a different availability source.

        The seam a health probe replaces. The registry is immutable so this
        returns a new value rather than mutating the one a caller is holding --
        two consumers can then disagree about availability without corrupting
        each other.

        Per-slot reasons travel with the source: a copy that kept availability
        but dropped the reasons would make ``forge models status --verify`` show
        a RED tier with nothing to explain it. ``Availability.coerce`` returns
        the same object when handed an ``Availability``, so passing an existing
        instance through preserves them with no special case.
        """
        return ModelRegistry(
            tiers=self.tiers,
            routing=self.routing,
            agent_defaults=self.agent_defaults,
            catalog=self.catalog,
            availability=Availability.coerce(availability),
            source=self.source,
        )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RegistryConfigError(
            f"{where} must be a table of settings, not {type(value).__name__}"
        )
    return value


def _parse_slot(raw: Any, where: str, catalog: ProviderCatalog) -> ModelSlot:
    """One ``primary`` or ``fallbacks`` entry."""
    table = _require_mapping(raw, where)

    unknown = set(table) - {"model", "provider", "enabled"}
    if unknown:
        raise RegistryConfigError(
            f"{where} has unrecognised keys: {', '.join(sorted(unknown))}. "
            "Expected model, provider, enabled."
        )

    model = table.get("model")
    if not isinstance(model, str) or not model.strip():
        raise RegistryConfigError(
            f"{where} must name a model with a non-empty `model` string"
        )

    provider = table.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise RegistryConfigError(
            f"{where} ({model}) must name a provider with a non-empty `provider` "
            "string"
        )

    provider_key = provider.strip().lower()
    if catalog.spec(provider_key) is None:
        known = ", ".join(catalog.names())
        raise RegistryConfigError(
            f"{where} names provider {provider_key!r}, which is not declared. "
            f"Declare it under [providers.{provider_key}] or use one of: {known}"
        )

    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegistryConfigError(
            f"{where} ({model}) has `enabled = {enabled!r}`; enabled must be "
            "true or false"
        )

    return ModelSlot(
        model=model.strip(), provider=provider_key, enabled=enabled
    )


def _parse_tier(name: str, raw: Any, catalog: ProviderCatalog) -> TierConfig:
    where = f"tiers.{name}"
    table = _require_mapping(raw, where)

    unknown = set(table) - {"primary", "fallbacks", "description"}
    if unknown:
        raise RegistryConfigError(
            f"{where} has unrecognised keys: {', '.join(sorted(unknown))}. "
            "Expected primary, fallbacks."
        )

    if "primary" not in table:
        raise RegistryConfigError(
            f"{where} must declare a `primary` model, even if it is disabled"
        )
    primary = _parse_slot(table["primary"], f"{where}.primary", catalog)

    raw_fallbacks = table.get("fallbacks", [])
    if isinstance(raw_fallbacks, dict) or not isinstance(raw_fallbacks, list):
        raise RegistryConfigError(
            f"{where}.fallbacks must be an array of tables, not "
            f"{type(raw_fallbacks).__name__}"
        )

    fallbacks: list[ModelSlot] = []
    for index, entry in enumerate(raw_fallbacks):
        slot = _parse_slot(entry, f"{where}.fallbacks[{index}]", catalog)
        if slot.key == primary.key:
            raise RegistryConfigError(
                f"{where}.fallbacks[{index}] repeats the primary model "
                f"{slot.key!r}; the fallback list would claim coverage it does "
                "not add"
            )
        fallbacks.append(slot)

    return TierConfig(
        tier=name, primary=primary, fallbacks=tuple(fallbacks)
    )


def _parse_agent_defaults(raw: Any, catalog: ProviderCatalog) -> dict[str, AgentDefault]:
    table = _require_mapping(raw, "agents")
    defaults: dict[str, AgentDefault] = {}
    for agent, raw_default in table.items():
        name = str(agent).strip().lower()
        where = f"agents.{name}"
        slot = _parse_slot(raw_default, where, catalog)
        defaults[name] = AgentDefault(slot.model, slot.provider, slot.enabled)
    return defaults


def _parse_routing(raw: Any) -> RoutingTable:
    table = _require_mapping(raw, "routing")

    unknown = set(table) - {"default_tier", "tasks"}
    if unknown:
        raise RegistryConfigError(
            f"routing has unrecognised keys: {', '.join(sorted(unknown))}. "
            "Expected default_tier, tasks."
        )

    default_tier = table.get("default_tier")
    if not isinstance(default_tier, str) or not default_tier.strip():
        raise RegistryConfigError(
            "routing.default_tier must name the tier used when no task matches"
        )

    raw_tasks = table.get("tasks", {})
    tasks_raw = _require_mapping(raw_tasks, "routing.tasks")

    tasks: dict[str, dict[str, str]] = {}
    for task, raw_by_difficulty in tasks_raw.items():
        key = str(task).strip().lower()
        by_difficulty = _require_mapping(raw_by_difficulty, f"routing.tasks.{key}")
        entries: dict[str, str] = {}
        for difficulty, tier in by_difficulty.items():
            if not isinstance(tier, str) or not tier.strip():
                raise RegistryConfigError(
                    f"routing.tasks.{key}.{difficulty} must name a tier"
                )
            entries[str(difficulty).strip().lower()] = tier.strip()
        tasks[key] = entries

    return RoutingTable(default_tier=default_tier.strip(), tasks=tasks)


def parse_registry(
    document: str,
    *,
    catalog: ProviderCatalog | None = None,
    availability: (
        Availability | Mapping[str, bool] | Callable[[ModelSlot], bool] | None
    ) = None,
    credentials: Mapping[str, str] | None = None,
    source: str = "<string>",
    extra_providers: Sequence[ProviderSpec] = (),
) -> ModelRegistry:
    """Build a registry from TOML text.

    Args:
        document: The TOML.
        catalog: Providers Forge knows. Defaults to
            :meth:`ProviderCatalog.default`.
        availability: Injected usability source. ``None`` means
            :meth:`Availability.none` -- usable-by-default and explicitly
            unverified, so nothing here can report a provider as checked when it
            was not.
        credentials: Optional mapping. Accepted so a caller can be handed the
            same injected environment the gateways use, and **not used for
            health**: a missing key is configuration, not evidence a model is
            unreachable. It affects nothing here, which is the point -- the
            credential-owning layer is the gateway, and the registry never reads
            one.
        source: Where the text came from, for error messages.
        extra_providers: Providers declared inline for this registry only.

    Raises:
        RegistryConfigError: the document is not usable. Never a bare
            ``tomllib.TOMLDecodeError``.
    """
    try:
        document_map = tomllib.loads(document)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryConfigError(f"{source} is not valid TOML: {exc}") from None

    unknown = set(document_map) - set(_ALLOWED_TABLES)
    if unknown:
        raise RegistryConfigError(
            f"{source} has unrecognised top-level tables: "
            f"{', '.join(sorted(unknown))}. Expected: "
            f"{', '.join(_ALLOWED_TABLES)}."
        )

    base = catalog or ProviderCatalog.default()
    for spec in extra_providers:
        base = base.with_spec(spec)

    for provider_name, raw_spec in _require_mapping(
        document_map.get("providers", {}), "providers"
    ).items():
        key = str(provider_name).strip().lower()
        spec_table = _require_mapping(raw_spec, f"providers.{key}")
        unknown_keys = set(spec_table) - {"credential_env"}
        if unknown_keys:
            raise RegistryConfigError(
                f"providers.{key} has unrecognised keys: "
                f"{', '.join(sorted(unknown_keys))}. Expected credential_env."
            )
        # An empty string is how a credential-free provider is written down.
        # `credential_env = ""` means "none needed", the same as omitting the
        # key, and a local endpoint genuinely has no key.
        raw_env = spec_table.get("credential_env")
        if raw_env is not None and not isinstance(raw_env, str):
            raise RegistryConfigError(
                f"providers.{key}.credential_env must be a variable name, or "
                'an empty string for a provider that needs no credential'
            )
        credential_env = raw_env.strip() if isinstance(raw_env, str) else None

        existing = base.spec(key)
        base = base.with_spec(
            ProviderSpec(
                name=key,
                credential_env=credential_env or None,
                gateway_factory=existing.gateway_factory if existing else None,
                # Carried across for the same reason as the gateway above: a
                # redeclaration in configuration names a credential, and
                # silently dropping the implementation would turn a provider
                # Forge *can* check into one it reports as unimplemented -- the
                # exact failure `--verify` exists to remove.
                verifier_factory=(
                    existing.verifier_factory if existing else None
                ),
            )
        )

    raw_tiers = _require_mapping(document_map.get("tiers", {}), "tiers")
    tiers: dict[str, TierConfig] = {}
    for tier_name, raw_tier in raw_tiers.items():
        key = str(tier_name).strip().lower()
        try:
            CapabilityTier(key)
        except ValueError:
            known = ", ".join(t.value for t in CapabilityTier)
            raise RegistryConfigError(
                f"tiers.{tier_name} is not a known capability tier. "
                f"Expected one of: {known}"
            ) from None
        tiers[key] = _parse_tier(key, raw_tier, base)

    routing = _parse_routing(document_map.get("routing", {"default_tier": ""}))
    agent_defaults = _parse_agent_defaults(document_map.get("agents", {}), base)

    for task, by_difficulty in routing.tasks.items():
        for difficulty, tier_name in by_difficulty.items():
            if tier_name not in tiers:
                known = ", ".join(sorted(tiers)) or "none declared"
                raise RegistryConfigError(
                    f"routing.tasks.{task}.{difficulty} names tier {tier_name!r}, "
                    f"which is not configured. Configured tiers: {known}"
                )

    if routing.default_tier not in tiers:
        known = ", ".join(sorted(tiers)) or "none declared"
        raise RegistryConfigError(
            f"routing.default_tier names {routing.default_tier!r}, which is not "
            f"configured. Configured tiers: {known}"
        )

    del credentials  # read for signature clarity; deliberately not consulted

    return ModelRegistry(
        tiers=tiers,
        routing=routing,
        agent_defaults=agent_defaults,
        catalog=base,
        availability=Availability.coerce(availability),
        source=source,
    )


def default_config_path() -> Path:
    """The packaged ``models.toml``, resolved through ``importlib.resources``.

    Shipped as package data. Without that entry in ``pyproject.toml`` an
    editable install works and a real wheel silently ships a registry with no
    default at all -- the same failure the Workbench stylesheet already had.
    """
    from importlib.resources import files

    return Path(str(files("forge.models") / "models.toml"))


def load_registry(
    path: str | os.PathLike[str] | None = None,
    *,
    catalog: ProviderCatalog | None = None,
    availability: (
        Availability | Mapping[str, bool] | Callable[[ModelSlot], bool] | None
    ) = None,
    environ: Mapping[str, str] | None = None,
) -> ModelRegistry:
    """Build a registry from a TOML file.

    Args:
        path: The file. ``None`` means resolution order:
            ``$FORGE_MODELS_CONFIG``, then ``./models.toml``, then the packaged
            default. An empty string is a config error rather than "unset",
            because an operator who exports the variable to nothing has made a
            mistake worth reporting.
        environ: Read for the config-path variable only. Passed in rather than
            read from ``os.environ`` so a test never mutates the process
            environment.

    Raises:
        RegistryConfigError: the file is missing, unreadable, or not usable.
    """
    env = os.environ if environ is None else environ

    if isinstance(path, str) and not path.strip():
        raise RegistryConfigError(
            f"{MODELS_CONFIG_ENV} is set but empty; unset it to fall back to "
            f"./{LOCAL_CONFIG_NAME}, or give it a path"
        )

    resolved = _resolve_config_path(path, env)
    source = str(resolved)

    try:
        document = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryConfigError(
            f"model configuration could not be read from {source}: "
            f"{exc.__class__.__name__}"
        ) from None
    except UnicodeError:
        raise RegistryConfigError(
            f"model configuration at {source} is not valid UTF-8"
        ) from None

    return parse_registry(
        document,
        catalog=catalog,
        availability=availability,
        source=source,
    )


def _resolve_config_path(
    path: str | os.PathLike[str] | None, env: Mapping[str, str]
) -> Path:
    """Pick the configuration file, in the order documented above."""
    if path is not None:
        return Path(path)

    from_env = env.get(MODELS_CONFIG_ENV, "").strip()
    if from_env:
        return Path(from_env)

    local = Path.cwd() / LOCAL_CONFIG_NAME
    if local.is_file():
        return local

    packaged = default_config_path()
    if packaged.is_file():
        return packaged

    raise RegistryConfigError(
        f"no model configuration found: set {MODELS_CONFIG_ENV}, create "
        f"./{LOCAL_CONFIG_NAME}, or install the packaged default"
    )