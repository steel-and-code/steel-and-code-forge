"""Capability tiers, health states, and the configuration error type.

**This module holds no routing and no configuration.** It owns three small
vocabularies that the registry and the router both need to agree on:

*   :class:`CapabilityTier` -- what a model is *for*, not what it is called.
*   :class:`Health` -- whether a tier can currently serve traffic.
*   :class:`SelectionKind` -- whether a decision took the primary or a fallback.

The distinction between a tier and a model is the whole point. ``alpha-reason``
disappearing tomorrow is a configuration edit; ``reasoning_heavy`` ceasing to
exist would be a code change, and is exactly what this design forbids.

Health is deliberately three-valued rather than a boolean. An operator needs to
tell "serving normally" from "degraded but alive" from "cannot serve", and a
boolean cannot distinguish the first two, which are the two that matter most
when something has broken.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "CapabilityTier",
    "Health",
    "SelectionKind",
    "RegistryConfigError",
]


class RegistryConfigError(ValueError):
    """The model configuration is not usable.

    Raised at load time, never during a run, so a broken registry cannot be
    discovered halfway through a task. The message names the offending table and
    the rule it broke, because a registry that loads without complaint but
    silently omits a tier is worse than one that refuses.

    Never carries file *contents* -- only the table path and a rule -- so an
    error message is safe to print in a terminal, log, or UI panel. A model
    configuration file has no credentials in it, but the habit is the same one
    the gateway layer keeps.
    """


class CapabilityTier(str, Enum):
    """What a model is asked to do.

    A ``str`` enum so a value read straight out of TOML compares equal to the
    member without unwrapping, which keeps the registry free of conversion
    noise and lets JSON consumers read ``"general_light"`` directly.
    """

    GENERAL_LIGHT = "general_light"
    CODING_STANDARD = "coding_standard"
    REASONING_HEAVY = "reasoning_heavy"

    def __str__(self) -> str:
        return self.value


class Health(str, Enum):
    """Whether a capability tier can currently serve traffic.

    ``GREEN``
        The primary model is usable.

    ``YELLOW``
        The primary is not usable, but at least one fallback is. Traffic still
        flows; quality or cost may have changed, which is why this is not green.

    ``RED``
        Nothing in the tier is usable. No traffic will be served, and no
        substitution happens -- Forge reports the failure rather than quietly
        serving a different capability than the one asked for.
    """

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"

    def __str__(self) -> str:
        return self.value


class SelectionKind(str, Enum):
    """Which kind of slot a routing decision landed on.

    ``PRIMARY``
        The tier's declared primary. The expected case.

    ``FALLBACK``
        A declared fallback, chosen in declaration order. The distinction from
        PRIMARY is recorded because a run served by a fallback is a run whose
        cost and behaviour may differ from the one the operator configured.

    ``NONE``
        No slot in the tier was usable. Carries a reason, never an exception.
    """

    PRIMARY = "primary"
    FALLBACK = "fallback"
    NONE = "none"

    def __str__(self) -> str:
        return self.value