"""Model health for the Workbench: one cached snapshot, one explicit re-check.

This module is the whole of the Workbench's model-health behaviour. It exists so
that the HTTP surface stays thin: a read returns a cached value and a re-check is
one bounded call into the existing verifier layer.

Four decisions live here, and each is a decision rather than a detail:

**The router decides health; this module decides nothing.** Every value it
publishes comes from :meth:`forge.models.router.ModelRouter.status`. A GREEN here
means exactly what a GREEN in ``forge models status`` means, and the tests assert
the two agree rather than asserting a literal.

**A read never probes.** ``GET /api/models`` returns the last snapshot. Probing
opens a socket and presents a credential, and a page that re-probed on every load
would make a refresh button a way to spend money repeatedly. So the snapshot is
built once, at startup, and only an explicit POST replaces it.

**A re-check is a command, so it is a POST.** It carries the same exact-Origin
and token guards as every other state-changing route on this surface, and it
refuses while a run is in flight. A verification pass opening sockets from inside
a process that is already mid-task would be an incidental side effect of running
Byte, which is exactly what health checking must not be.

**The verifier's bounds are the only bounds.** One attempt per distinct model,
no retries, and the module's own timeout, reached through
:func:`forge.models.verify.verified_registry`. This module holds no timeout
constant of its own; a second one here would be a second, more generous policy
dressed as a convenience.

**Credentials stop here.** The ``environ`` mapping is held so a verifier can be
built, and it is never placed in a return value, a log line, or an error
message. :func:`ModelHealth` publishes ``status()`` entries, a ``checked_at``
timestamp, and nothing else.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone

__all__ = ["ModelHealth", "NO_REGISTRY_DETAIL"]


#: What the surface reports when no registry could be loaded. Named so the
#: server and its tests say the same sentence, and short enough to be a detail
#: string on a 503 rather than a paragraph.
NO_REGISTRY_DETAIL = (
    "no model registry is loaded, so this surface has no model health to "
    "report; forge could not read the model configuration"
)


class ModelHealth:
    """A cached :meth:`ModelRouter.status` snapshot, and how to refresh it.

    Args:
        router: The router to report from, or ``None`` when the CLI could not
            load a registry. ``None`` is a reportable state rather than a
            silent one: the page says the configuration is unusable instead of
            showing an empty indicator that reads as "everything is fine".
        environ: The credential mapping, handed to a verifier when one is
            built. Read only by :mod:`forge.models.verify`, and never published.
        error: Why ``router`` is ``None``, when it is. Shown to the operator;
            registry errors name a table and a rule and carry no file contents,
            which is the guarantee the registry itself makes.

    Thread-safe. The snapshot is replaced whole under a lock rather than
    mutated, so a read during a re-check sees either the old snapshot or the
    new one and never a half-written mixture.
    """

    def __init__(
        self,
        router=None,
        *,
        environ: Mapping[str, str] | None = None,
        error: str = "",
    ) -> None:
        self._router = router
        self._environ = environ if environ is not None else {}
        self._error = error or (NO_REGISTRY_DETAIL if router is None else "")

        self._lock = threading.Lock()
        # The offline snapshot, built once at construction so the very first read
        # is served from a value rather than from a probe. `verified` is taken
        # from the registry's own availability source and is False for a
        # registry nothing has checked -- claiming otherwise here would be the
        # confident-green-about-an-unchecked-provider failure this layer exists
        # to prevent, stated as a constructor default instead of a measured fact.
        self._entries = list(router.status()) if router is not None else []
        self._verified = (
            bool(router.registry.availability.verified) if router is not None else False
        )
        self._checked_at = None

    # -- read -----------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether there is a registry to report on at all."""
        return self._router is not None

    @property
    def error(self) -> str:
        """Why this surface has no health, or ``""``."""
        return self._error

    def status(self) -> dict[str, object]:
        """The cached snapshot, wrapped as thinly as it can be.

        ``tiers`` is the router's status list verbatim -- same keys, same
        values, same order. Nothing is renamed, recomputed, or filtered, so a
        consumer cannot tell this endpoint from ``forge models status --json``
        and cannot be misled by a field this layer dropped.
        """
        with self._lock:
            return {
                "tiers": [dict(entry) for entry in self._entries],
                "verified": self._verified,
                "checked_at": self._checked_at,
            }

    # -- re-check -------------------------------------------------------

    def verify(self) -> dict[str, object]:
        """Run one verification pass and replace the snapshot with it.

        Synchronous and blocking, bounded by the verifier layer's own policy:
        one attempt per distinct ``provider/model``, no retries, and
        ``forge.models.verify.VERIFY_TIMEOUT_SECONDS`` per attempt. The caller
        runs it off the event loop.

        Never raises for a provider-side failure -- that is the whole point of
        :mod:`forge.models.verify` -- but a registry that cannot be copied does
        raise, and the previous snapshot is left in place rather than replaced
        with an error the page would have to interpret.
        """
        if self._router is None:
            raise HealthUnavailable(self._error)

        # Imported here, by name, for the same reason every other model-layer
        # import in this codebase is inside a function: this is the only module
        # in the UI package that must be able to reach a probe, and importing it
        # at module scope would make that reachability unconditional.
        from ..models.router import ModelRouter
        from ..models.verify import verified_registry

        # The default timeout is used by not passing one, so the bound lives in
        # exactly one place. The offline router is the input; nothing here is
        # mutated.
        probed = ModelRouter(
            verified_registry(self._router.registry, environ=self._environ)
        )
        entries = probed.status()

        with self._lock:
            self._router = probed
            self._entries = list(entries)
            self._verified = True
            self._checked_at = self._now()

        return self.status()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class HealthUnavailable(RuntimeError):
    """No registry was loaded, so there is nothing to verify.

    Its own type so the server can answer 503 without matching on a message,
    and so the condition cannot be confused with a probe that failed -- a probe
    that fails is a reported health value, not an error.
    """