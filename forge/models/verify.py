"""The probe: turn configured slots into a verified :class:`Availability`.

This is the module ``forge models status --verify`` reaches for, and it is the
**only** module in ``forge.models`` that opens a socket.

**Direction of dependency, which is the whole design.** This module imports the
registry and the provider modules; the registry and the router import neither
this one nor ``urllib``. So a probe can be added, changed, or broken without any
ability to alter what a routing decision or an offline status means. The health
computation in ``forge.models.registry`` is untouched by verification -- a probe
supplies facts through the existing :class:`Availability` seam and nothing more.

**What "verified" means here.** That one cheap, bounded request was made and
produced an answer. Not that the model is healthy, not that a completion would
succeed, and emphatically not that the request was repeated until it worked. A
probe that found every model broken returns availability with ``verified=True``
and every slot unavailable, which is the honest report -- verified means
*checked*, and the checked answer can be no.

**Bounds, by construction:**

*   One attempt per distinct ``provider/model``. Deduplicated across tiers, so a
    model standing behind three tiers is still one request.
*   No retries and no backoff. A verification loop is an outage amplifier.
*   No completion, ever. The cheapest provider-supported check that reliably
    establishes usability is a model listing: free, no tokens, no tool loop,
    nothing that can be billed. There is no code path here that reaches
    ``ModelGateway.complete``.
*   No agent runtime, no workspace, no approval. This module cannot execute
    anything; it asks a provider about a list.

**What is never probed, and why that is not a gap.** A slot an operator has
switched off, and a slot on a provider Forge cannot check, are never contacted.
Probing a disabled model would spend a request on a question nobody asked and
produce a result that could tempt a status view into reporting coverage the
operator explicitly switched off. Probing an unimplemented provider is
impossible, and that is reported as
:class:`~forge.models.base.VerificationCode.UNIMPLEMENTED` -- a clean, reportable
result rather than an exception out of an inspection command.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .base import VerificationCode, VerificationResult
from .registry import Availability, ModelRegistry, ModelSlot

__all__ = [
    "VERIFY_TIMEOUT_SECONDS",
    "VerificationRun",
    "verify_slots",
    "verified_registry",
]

#: The bound for a whole verification run's per-provider attempt. Chosen so a
#: status check over three tiers cannot turn into a minute of waiting.
VERIFY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class VerificationRun:
    """What one verification pass found.

    Attributes:
        results: One :class:`~forge.models.base.VerificationResult` per distinct
            ``provider/model``, keyed by ``ModelSlot.key``. Slots that were not
            probed are **absent**, which is the honest encoding: a key missing
            here was never asked about, and
            :meth:`forge.models.registry.Availability` treats absence as "not
            known to be broken" rather than "known good".
    """

    results: Mapping[str, VerificationResult]

    def result_for(self, slot: ModelSlot) -> VerificationResult | None:
        """The result for ``slot``, or ``None`` when it was never probed."""
        return self.results.get(slot.key)

    @property
    def verified(self) -> bool:
        """Whether anything was checked at all.

        True even when every answer was a failure, and False when every slot was
        unprobeable. Reporting "verified" for a run that contacted nobody is the
        exact confusion this layer exists to prevent.
        """
        return any(
            result.code is not VerificationCode.UNIMPLEMENTED
            for result in self.results.values()
        )

    def __contains__(self, key: object) -> bool:
        return key in self.results

    def __getitem__(self, key: str) -> VerificationResult:
        """The result for ``key``. A :class:`KeyError` here means the slot was
        never probed, which :meth:`result_for` states as a ``None`` instead --
        this subscript exists so a caller can write ``run[key]`` naturally."""
        return self.results[key]

    def availability(self) -> Availability:
        """This run, as the availability source the registry already accepts.

        Verified whenever :attr:`verified` -- and note the asymmetry with the
        property above is deliberate and one-directional: a failed probe still
        counts as verified, because the failure *is* the information.
        """
        return Availability.verified_results(self.results)

    def to_json(self) -> list[dict[str, object]]:
        """One flat object per probed model, for ``--json`` consumers."""
        return [
            {"provider": key.split("/", 1)[0], **result.to_json()}
            for key, result in sorted(self.results.items())
        ]


def verify_slots(
    registry: ModelRegistry,
    *,
    environ: Mapping[str, str],
    timeout: float = VERIFY_TIMEOUT_SECONDS,
) -> VerificationRun:
    """Check every probeable configured slot once each.

    Args:
        registry: What is configured. Read only; never mutated, so a caller
            holding the offline registry keeps a registry that reports
            ``verified=False`` afterwards.
        environ: The credential mapping, handed to each verifier. Injected, so a
            test never mutates the process environment to exercise a probe.
        timeout: Seconds per attempt.

    Returns:
        A :class:`VerificationRun`. **Never raises**: a verifier that raises is
        caught and reported as an unreachable provider, because an inspection
        command that crashes tells the operator nothing they can act on.
    """
    slots = _probeable_slots(registry)
    catalog = registry.catalog
    results: dict[str, VerificationResult] = {}

    # One verifier per provider, built once. Two tiers naming the same provider
    # must not build two of them, and a provider answering a listing once per
    # run rather than once per model is the difference between a status check
    # and a load test.
    verifiers: dict[str, object] = {}

    for slot in slots:
        factory = catalog.verifier_for(slot.provider)

        if factory is None:
            # Declared but unimplemented, or an unknown provider. A result, not
            # a failure to construct one.
            results[slot.key] = VerificationResult.failure(
                slot.model,
                VerificationCode.UNIMPLEMENTED,
                reason=(
                    f"provider {slot.provider!r} has no verification "
                    "implementation in Forge, so this model was not checked"
                ),
                detail="unimplemented provider",
            )
            continue

        if slot.provider not in verifiers:
            verifiers[slot.provider] = _build(factory, environ, timeout)

        result = _attempt(verifiers[slot.provider], slot)

        # First result wins, so a repeated slot cannot produce a second network
        # call that might disagree with the first.
        if slot.key not in results:
            results[slot.key] = result

    return VerificationRun(results=results)


def _build(factory, environ: Mapping[str, str], timeout: float):
    """Build a verifier, tolerating a factory that will not cooperate.

    A factory raising is still a reportable state about that provider, so it is
    converted into a verifier that reports the problem for every model rather
    than aborting the whole run.
    """

    class _Unbuildable:
        def verify(self, model: str) -> VerificationResult:
            return VerificationResult.failure(
                model,
                VerificationCode.UNIMPLEMENTED,
                reason=(
                    "Forge could not construct this provider's verifier, so "
                    "the model was not checked"
                ),
                detail="verifier construction failed",
            )

    try:
        return factory(environ)
    except Exception:  # noqa: BLE001 - a bad factory is a status, not a crash
        return _Unbuildable()


def _attempt(verifier, slot: ModelSlot) -> VerificationResult:
    """One call, with every exception path turned into a result value."""
    try:
        result = verifier.verify(slot.model)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        return VerificationResult.failure(
            slot.model,
            VerificationCode.UNREACHABLE,
            reason=(
                f"verification of provider {slot.provider!r} failed "
                f"({type(exc).__name__}); the model was not confirmed usable"
            ),
            detail=type(exc).__name__,
        )

    if isinstance(result, VerificationResult):
        return result

    # A verifier returning something that is not a result is a broken
    # implementation. Treat it as unchecked rather than guessing at truthiness:
    # a truthy non-result would make an unimplemented probe look healthy.
    return VerificationResult.failure(
        slot.model,
        VerificationCode.MALFORMED_RESPONSE,
        reason=(
            f"provider {slot.provider!r} returned a verification result of an "
            f"unexpected type ({type(result).__name__})"
        ),
        detail="unexpected result type",
    )


def _probeable_slots(registry: ModelRegistry) -> tuple[ModelSlot, ...]:
    """The enabled slots a verification pass should have an opinion about.

    A slot qualifies when it is **enabled**. Configuration presence is not
    enough -- a disabled model is an operator's decision and a probe must not
    spend a request overriding it, so a disabled slot is left out entirely
    rather than being reported as a failure that reads like coverage.

    A slot on a provider Forge cannot verify **does** appear, and gets an
    ``UNIMPLEMENTED`` result: "Forge could not check this" is a fact an operator
    needs, and encoding it as absence would let it be read as "not known to be
    broken". Nothing is contacted for it.

    Deduplicated by ``provider/model``, so a model standing behind several tiers
    is one entry and therefore one request.
    """
    seen: set[str] = set()
    ordered: list[ModelSlot] = []

    for name in registry.tier_names():
        tier = registry.tier(name)
        if tier is None:  # pragma: no cover - tier_names comes from the mapping
            continue
        for slot in tier.slots:
            if not slot.enabled:
                continue
            if slot.key in seen:
                continue
            seen.add(slot.key)
            ordered.append(slot)

    return tuple(ordered)


def verified_registry(
    registry: ModelRegistry,
    *,
    environ: Mapping[str, str],
    timeout: float = VERIFY_TIMEOUT_SECONDS,
) -> ModelRegistry:
    """A copy of ``registry`` whose availability comes from a real probe.

    The seam :meth:`forge.models.registry.ModelRegistry.with_availability` was
    built for. Health is then computed by the registry exactly as it always was,
    so ``--verify`` cannot change what GREEN, YELLOW, or RED mean -- only
    whether the facts underneath them have been checked.

    The input is not mutated: a caller holding the offline registry keeps
    reporting ``verified=False``, which is what makes "plain status stays
    offline" a property of the code rather than a promise.
    """
    run = verify_slots(registry, environ=environ, timeout=timeout)
    return registry.with_availability(run.availability())