"""The router: a request, a capability tier, and one model chosen with a reason.

A :class:`RouteRequest` names *what kind of work* this is. A :class:`RouteDecision`
names what Forge will do about it. Neither mentions a model until the last step,
and the model that appears is whatever configuration said -- which is the entire
decoupling contract in one sentence.

Three properties this module exists to hold:

**The agent never chooses a model.** :class:`RouteRequest` carries an agent name
and a task kind, never a model id. An agent that could name a model would
reintroduce exactly the coupling the registry removes, so ``tier`` may be given
explicitly (an operator override) but a request with no tier falls through the
configured routing table.

**A decision always has a reason.** Every :class:`RouteDecision` carries a
sentence explaining why this slot and not another, so a run served by a fallback
can be explained afterwards from a log or a terminal rather than guessed at.
"No usable model" is a *decision*, not an exception: an operator inspecting
routing wants to be told nothing can serve, not handed a traceback.

**Deciding is not calling.** This module imports no gateway, no network library,
and nothing from ``forge.tools``. ``forge route`` is an inspection command and
stays safe to run offline and unauthenticated.

It holds no permission policy and cannot acquire any: it does not import the
classifiers, does not build an approval request, and has no workspace. A change
that made this module decide *whether something may run* would be a change in
the wrong direction, and ``tests/test_model_router.py`` asserts the absence
structurally.
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import ModelRegistry, ModelSlot, SlotState
from .tiers import CapabilityTier, Health, SelectionKind

__all__ = ["RouteRequest", "RouteDecision", "ModelRouter"]


@dataclass(frozen=True)
class RouteRequest:
    """One unit of work, described without naming a model.

    Attributes:
        agent: Who is asking. Recorded on the decision so a routing audit can
            say whose request this was; it does not influence selection, because
            one agent getting a different model from another is a policy decision
            this layer is not entitled to make.
        task: A task kind matching ``[routing.tasks.<task>]`` in configuration.
            Unknown values resolve to the default tier rather than failing.
        difficulty: A difficulty matching a key under that task. Unknown values
            fall back to ``standard`` for the task.
        tier: An explicit capability tier, bypassing the routing table. Present
            for an operator override, not for an agent to set.
    """

    agent: str = "forge"
    task: str | None = None
    difficulty: str | None = None
    tier: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """What Forge will do about a request, and why.

    A decision with :attr:`selection` ``NONE`` is a real, reportable outcome. It
    carries a tier and a reason so a RED capability is legible in a status view
    without anything having to interpret an exception.

    Attributes:
        tier: The capability tier chosen, or attempted when nothing was usable.
            A :class:`CapabilityTier` for a tier Forge knows, and the raw string
            for one it does not -- so an unconfigured tier is still reported
            faithfully rather than replaced by a default.
        model: The provider-specific model id, or ``""`` when nothing was usable.
        provider: The provider serving it, or ``""``.
        selection: PRIMARY, FALLBACK, or NONE.
        health: The tier's health. GREEN, YELLOW, or RED.
        reason: Why this outcome. Never empty.
        tier_reason: Which routing rule selected the tier.
        agent: The requester, echoed from the request.
        fallback_index: 1-based position among fallbacks, or ``0`` for the
            primary or for no selection.
    """

    tier: str
    model: str = ""
    provider: str = ""
    selection: SelectionKind = SelectionKind.NONE
    health: Health = Health.RED
    reason: str = ""
    tier_reason: str = ""
    agent: str = "forge"
    fallback_index: int = 0

    @property
    def usable(self) -> bool:
        """Whether this decision names a model that could serve the request."""
        return self.selection is not SelectionKind.NONE

    @property
    def slot(self) -> str:
        """``"provider/model"``, or ``""`` when nothing was selected.

        A single stable identity for logs and a future UI, matching the key
        availability sources are keyed by.
        """
        return f"{self.provider}/{self.model}" if self.model else ""

    def to_json(self) -> dict[str, object]:
        """One flat JSON-ready object.

        Every field is present, including the empty ones. A status consumer
        showing a green/yellow/red indicator needs to distinguish "no model
        selected" from "the field was missing", and an omitted key cannot.
        """
        return {
            "agent": self.agent,
            "tier": self.tier,
            "model": self.model,
            "provider": self.provider,
            "selection": self.selection.value,
            "health": self.health.value,
            "reason": self.reason,
            "tier_reason": self.tier_reason,
            "fallback_index": self.fallback_index,
            "usable": self.usable,
        }


class ModelRouter:
    """Chooses a model for a request, from a registry.

    Args:
        registry: The configured tiers. Held, never mutated, and never written
            to: routing is read-only, so two concurrent decisions cannot
            disagree because one of them changed a tier.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    @property
    def registry(self) -> ModelRegistry:
        """The registry this router decides against."""
        return self._registry

    # -- routing ---------------------------------------------------------

    def route(self, request: RouteRequest) -> RouteDecision:
        """Choose a model for ``request``.

        Never raises for an unserviceable request. A RED tier, a missing tier,
        and a slot on a provider with no gateway all produce a decision with
        :attr:`RouteDecision.selection` ``NONE`` and a specific reason, because
        the caller's next move is to read the reason -- not to catch something.
        """
        tier_name, tier_reason = self._select_tier(request)
        tier = self._registry.tier(tier_name)
        # Normalise a known tier to the enum member. `str, Enum` compares equal
        # to its value, so this changes no behaviour -- but a consumer holding
        # the member gets identity comparison and `.value` for free, which is
        # what a future UI indicator will want.
        reported: str
        if tier is None:
            # Not a tier Forge knows; report the string as configured rather
            # than substituting a default and hiding the mistake.
            reported = str(tier_name)
        else:
            try:
                reported = CapabilityTier(tier.tier)
            except ValueError:  # pragma: no cover - parse_registry rejects these
                reported = tier.tier

        if tier is None:
            return RouteDecision(
                tier=str(tier_name),
                selection=SelectionKind.NONE,
                health=Health.RED,
                reason=(
                    f"capability tier {tier_name!r} is not configured; "
                    f"configured tiers: "
                    f"{', '.join(self._registry.tier_names()) or 'none'}"
                ),
                tier_reason=tier_reason,
                agent=request.agent,
            )

        decision = self._select_slot(tier)
        return RouteDecision(
            tier=reported,
            model=decision.model,
            provider=decision.provider,
            selection=decision.selection,
            health=decision.health,
            reason=decision.reason,
            tier_reason=tier_reason,
            agent=request.agent,
            fallback_index=decision.fallback_index,
        )

    def _select_tier(self, request: RouteRequest) -> tuple[str, str]:
        """The tier name and the routing rule that chose it.

        An explicit ``request.tier`` wins over the table, and says so in the
        reason -- an override that looked identical to a table hit would be
        impossible to spot in an audit.
        """
        if request.tier:
            return str(request.tier).strip().lower(), "explicit request tier"
        return (
            self._registry.routing.tier_for(request.task, request.difficulty),
            self._registry.routing.describe(request.task, request.difficulty),
        )

    def _select_slot(self, tier) -> RouteDecision:
        """Walk the tier: primary, then fallbacks in declaration order."""
        primary_state = self._registry.slot_state(tier.primary)

        if primary_state is SlotState.CONFIGURED:
            return RouteDecision(
                tier=tier.tier,
                model=tier.primary.model,
                provider=tier.primary.provider,
                selection=SelectionKind.PRIMARY,
                health=Health.GREEN,
                reason=(
                    f"primary {tier.primary.key!r} is enabled and available"
                ),
                fallback_index=0,
            )

        # The primary's reason is carried into the fallback decision: "beta was
        # chosen" is not actionable without "alpha is unavailable".
        primary_note = self._explain(tier.primary, primary_state)

        for index, slot in enumerate(tier.fallbacks, start=1):
            if self._registry.slot_usable(slot):
                return RouteDecision(
                    tier=tier.tier,
                    model=slot.model,
                    provider=slot.provider,
                    selection=SelectionKind.FALLBACK,
                    health=Health.YELLOW,
                    reason=(
                        f"primary {primary_note}; selected fallback #{index} "
                        f"{slot.key!r}, which is enabled and available"
                    ),
                    fallback_index=index,
                )

        return RouteDecision(
            tier=tier.tier,
            selection=SelectionKind.NONE,
            health=Health.RED,
            reason=(
                f"primary {primary_note}, and none of "
                f"{len(tier.fallbacks)} fallback(s) are usable; no usable model "
                f"for tier {tier.tier!r}"
            ),
            fallback_index=0,
        )

    @staticmethod
    def _explain(slot: ModelSlot, state: SlotState) -> str:
        """Name a slot's state in one clause, for a reason string."""
        if state is SlotState.DISABLED:
            return f"{slot.key!r} is disabled in configuration"
        if state is SlotState.UNAVAILABLE:
            return f"{slot.key!r} is reported unavailable"
        if state is SlotState.UNROUTABLE:
            return (
                f"{slot.key!r} names provider {slot.provider!r}, which has no "
                "gateway implementation in Forge"
            )
        return f"{slot.key!r} is configured"

    # -- health ----------------------------------------------------------

    def status(self) -> list[dict[str, object]]:
        """One entry per capability tier: health, the model that would serve it,
        and every slot's individual state.

        This is the data a future UI health indicator consumes, shaped once here
        so the browser, the terminal, and a script cannot each invent their own
        notion of "which model is this tier using right now".

        Every known tier name appears, including one the registry does not
        carry -- reported RED with no model -- so an indicator iterating tier
        names always receives a value and never has to decide what absence
        means.

        Each slot carries ``enabled`` and ``state`` separately. Collapsing them
        into one boolean is what makes a health display lose the difference
        between "an operator switched this off" and "this is currently
        unreachable", which are different problems with different fixes.
        """
        health = self.health_by_tier()
        entries: list[dict[str, object]] = []

        for name in health:
            tier = self._registry.tier(name)
            state = self._registry.health(name)

            if tier is None:
                entries.append(
                    {
                        "tier": name,
                        "health": state.value,
                        "model": "",
                        "provider": "",
                        "selection": SelectionKind.NONE.value,
                        "slot": "not configured",
                        "verified": self._registry.availability.verified,
                        "slots": [],
                    }
                )
                continue

            # Route with the tier named explicitly rather than inferring one from
            # a task, so a status row shows what this tier would choose and not
            # what some task happens to map to.
            decision = self.route(RouteRequest(agent="forge-status", tier=name))
            slot_label = (
                f"fallback #{decision.fallback_index}"
                if decision.selection is SelectionKind.FALLBACK
                else decision.selection.value
            )

            entries.append(
                {
                    "tier": name,
                    "health": state.value,
                    "model": decision.model,
                    "provider": decision.provider,
                    "selection": decision.selection.value,
                    "slot": slot_label,
                    "reason": decision.reason,
                    "verified": self._registry.availability.verified,
                    "slots": [
                        {
                            "model": slot.model,
                            "provider": slot.provider,
                            "enabled": slot.enabled,
                            "state": self._registry.slot_state(slot).value,
                            "position": position,
                        }
                        for position, slot in self._slots_with_positions(tier)
                    ],
                }
            )

        return entries

    @staticmethod
    def _slots_with_positions(tier) -> list[tuple[int, ModelSlot]]:
        """``(position, slot)`` with the primary at 0 and fallbacks from 1."""
        return [(0, tier.primary)] + [
            (index, slot) for index, slot in enumerate(tier.fallbacks, start=1)
        ]

    def health_by_tier(self) -> dict[str, Health]:
        """Health for every configured tier, keyed by tier name.

        The shape a future UI health indicator needs: one green/yellow/red per
        capability, with no lookup logic of its own. A tier this registry does
        not carry is included as RED, so the indicator has a value for every
        known tier name and never has to decide what absence means.

        Every value comes from the registry's own :meth:`ModelRegistry.health`.
        The router recomputes nothing, because a status view and a routing
        decision that disagreed about health would make both untrustworthy.
        """
        names = set(self._registry.tier_names()) | {t.value for t in CapabilityTier}
        return {name: self._registry.health(name) for name in sorted(names)}