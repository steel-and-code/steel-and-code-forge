"""Observational events: the one way a surface learns what Forge is doing.

Forge core is silent. It runs, it classifies, it asks, it executes, and it
returns a value — and a caller who wants to watch that happen has no way to see
it without either scraping logs or reaching into runtime internals. Both of
those are worse than a hook. Logs are a rendering, not an interface, and
internals are not a contract: the first drifts from the thing it describes, and
the second turns any refactor into a breaking change for someone else's UI.

So the runtime and the dispatcher accept an optional :class:`EventSink` and call
``emit`` when something observable happened. That is the whole mechanism.

**This module holds no policy and cannot be given any.** Four properties hold,
and each exists because the alternative was a design where a display could
influence what Forge is allowed to do:

*   **A sink observes; it decides nothing.** :class:`EventSink` has one method
    and returns nothing. There is no ``approve``, no ``deny``, no ``retry``,
    and no return value for the runtime to branch on. A UI that wants a decision
    made asks the layer that owns decisions, which is not this one.
*   **An event is inert data, bounded by type.** :class:`Event` fields are
    ``str`` and ``str`` values only, clamped in count and length at
    construction. A sink cannot be handed a classifier verdict, an
    ``ApprovalOutcome``, an ``Executor``, or any other object with behaviour on
    it — so there is nothing in an event to call.
*   **No execution is reachable from here.** This module imports no part of
    ``forge.tools``, ``forge.agents``, or ``forge.models``, which
    ``tests/test_events.py`` asserts by scanning imports. An observer with a
    handle to an executor would be an observer one edit away from a second path
    to a process.
*   **Optional everywhere.** With no sink supplied, the runtime and the
    dispatcher behave exactly as before — same results, same exceptions, no
    allocation beyond a short-lived :class:`Event` that is discarded. The CLI
    supplies no sink and is unaffected.

Emission is best-effort and one-directional. A sink that raises does not change
a run's outcome: an observer that could fail a run would be a participant in
it, and the alternative — letting a display error abort an agent mid-task — is
worse than a missed event. So :func:`emit` swallows sink failures. That is a
deliberate trade, and the test suite pins it so nobody "fixes" it into a
propagation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "Event",
    "EventSink",
    "NullEventSink",
    "emit",
    "MAX_EVENT_FIELDS",
    "MAX_FIELD_CHARS",
    "MAX_NAME_CHARS",
]


#: Ceiling on distinct field names in one event. An event is a summary of one
#: observation, not a document; a caller wanting to send a payload has a
#: transport problem, not an events problem.
MAX_EVENT_FIELDS = 12

#: Ceiling on any single field value. Long enough for a path or a short
#: message, short enough that no event can carry a file's contents or a process
#: buffer to a display that has no redaction pass of its own.
MAX_FIELD_CHARS = 400

#: Ceiling on the event name itself. Names are stable identifiers, not prose.
MAX_NAME_CHARS = 48


def _clamp(value: object, limit: int = MAX_FIELD_CHARS) -> str:
    """Render ``value`` as a bounded string.

    ``str()`` rather than a type check, because a caller should not be able to
    crash the run by emitting something odd -- but bounded, because
    ``str(some_large_object)`` can be arbitrarily long. The result is
    truncated with an explicit marker rather than a bare slice, so a reader can
    tell a short value from a clipped one.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass(frozen=True)
class Event:
    """One observable thing that happened.

    Frozen, so a sink holding an event cannot alter what a later sink sees, and
    so the same event can be delivered to several subscribers unchanged.

    Attributes:
        name: What happened, as a stable dotted identifier, e.g.
            ``"tool.result"``. Stable rather than descriptive so a surface can
            branch on it without parsing prose.
        fields: Bounded string metadata. Declared ``object`` and stored ``str``:
            construction accepts whatever a caller has, and what is retained is
            always a clamped string, so an event cannot smuggle a large payload
            or a non-inert object past a sink.
    """

    name: str
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        clamped: dict[str, str] = {}
        for raw_key, raw_value in dict(self.fields).items():
            if len(clamped) >= MAX_EVENT_FIELDS:
                break
            key = _clamp(raw_key, MAX_NAME_CHARS)
            if key in clamped:
                # A duplicate name would silently drop a value, and a surface
                # reading the event would see a field it believes is complete.
                continue
            clamped[key] = _clamp(raw_value)
        object.__setattr__(self, "name", _clamp(self.name, MAX_NAME_CHARS))
        object.__setattr__(self, "fields", clamped)

    def get(self, key: str, default: str = "") -> str:
        """Read one field, with a default for anything absent."""
        value = self.fields.get(key, default)
        return value if isinstance(value, str) else str(value)

    def to_public_dict(self) -> dict[str, object]:
        """A wire-safe projection.

        The name is deliberately carried separately from ``fields`` so a
        consumer can dispatch on it without a string comparison against a
        field, and so no field can shadow it.
        """
        return {"event": self.name, **self.fields}


@runtime_checkable
class EventSink(Protocol):
    """Where observational events go.

    One method, returning nothing. That is the whole contract, and the
    one-method shape is load-bearing: a sink that returned a value would be a
    branch point in the runtime, and a sink with a second method would be an
    interface to grow policy into.
    """

    def emit(self, event: Event) -> None:
        """Record one event. Returns nothing; decides nothing."""
        ...


class NullEventSink:
    """A sink that discards everything.

    Used where a caller has no observer but still wants to hold a non-``None``
    sink, so the "is there a sink" check does not have to exist at every call
    site. Emitting here costs one function call and no allocation beyond the
    event the caller already built.
    """

    def emit(self, event: Event) -> None:
        """Discard ``event``."""
        return None


def emit(sink: EventSink | None, name: str, **fields: object) -> None:
    """Build one event and hand it to ``sink``, if there is one.

    The single entry point every emitting site uses, so the "is there a sink"
    check and the failure policy live in one place rather than at each call
    site where they could differ.

    A sink that raises is swallowed. An observer is not a participant in a run:
    a display bug must not abort an agent mid-task, and the alternative — a
    propagation rule that every future emitting site has to remember — is the
    kind of property that decays quietly. The cost is that a broken sink fails
    silently, which is why a real sink is responsible for its own logging.
    """
    if sink is None:
        return
    try:
        payload: dict[str, str] = {
            str(key): _clamp(value) for key, value in fields.items()
        }
        sink.emit(Event(name=name, fields=payload))
    except Exception:  # noqa: BLE001 - deliberate boundary, see docstring
        return
