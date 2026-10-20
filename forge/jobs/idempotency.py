"""Deterministic idempotency keys and the duplicate-event protection contract.

Phase A (issue #3) names "webhook authentication/replay protection,
idempotency key" and "duplicate event protection" among the job contract. What
belongs in a pure module is the *key derivation*: a function that maps a
(ticket, event, payload) to a stable identifier, so the same event sent twice
-- a Jira webhook delivered twice, a retried trigger -- yields the same key and
can be recognised as a repeat.

What does **not** belong here is the claim that recognition alone gives you
exactly-once execution. Recognising a duplicate needs durable storage, and
turning recognition into *exactly-once* needs a transaction: record the key and
commit its outcome atomically, so a crash between "first seen" and "first done"
re-runs the work exactly once rather than zero or twice. That is a later
requirement, and it is represented by :class:`DedupeStore` -- an interface, not
an implementation -- so the pure key function stays independent of how and where
the deduplication is actually stored.

The key itself follows the same canonicalisation discipline as
``ToolCall.canonical`` in ``forge/models/base.py``: dictionary payloads are
serialised with sorted keys and tight separators, so two structurally equal
payloads with different key orderings still produce the same key.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

__all__ = [
    "idempotency_key",
    "event_key",
    "DedupeStore",
    "MemoryDedupeStore",
]

#: Field separator that cannot appear in a well-formed ticket ref or event name.
_SEP = "\x1f"


def idempotency_key(ticket_ref: str, event: str, payload: str = "") -> str:
    """A stable sha256 key for one ``(ticket, event, payload)`` triple.

    Deterministic: identical inputs yield an identical key, and the inputs are
    joined with a separator that the identifiers cannot contain, so distinct
    triples cannot collide by concatenation. ``payload`` is treated as an opaque
    string here; callers with structured payloads should canonicalise first via
    :func:`event_key`.

    The key is a recognition token, not an authorization. It says "have I seen
    this before"; it does not, by itself, make the operation happen once.
    """
    canonical = f"{ticket_ref}{_SEP}{event}{_SEP}{payload}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_key(ticket_ref: str, event: str, arguments: Mapping[str, object]) -> str:
    """Canonical key for a structured event, ordering-independent.

    ``arguments`` is serialised with ``sort_keys`` and tight separators before
    being passed to :func:`idempotency_key`, so ``{"b": 1, "a": 2}`` and
    ``{"a": 2, "b": 1}`` produce the same key -- a retried webhook whose JSON
    key order changed is still recognised as the same event.

    Values must be JSON-serialisable. Anything that is not raises rather than
    silently producing a key for a payload that was not fully captured.
    """
    raw = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return idempotency_key(ticket_ref, event, raw)


class DedupeStore:
    """Interface for durable duplicate-event protection, wired in a later phase.

    ``record`` must be atomic and durable: the first call for a key records it
    and returns that it was seen first; any later call for the same key returns
    the already-recorded outcome. Until an implementation behind this interface
    exists, duplicate protection is best-effort, and exactly-once execution is
    **not** claimed -- see the module docstring.
    """

    def seen(self, key: str) -> str | None:  # pragma: no cover - interface
        """Return the outcome recorded for ``key``, or ``None`` if unseen."""
        raise NotImplementedError

    def record(self, key: str, outcome: str) -> bool:  # pragma: no cover - interface
        """Record ``outcome`` for ``key``. True if this call recorded it first."""
        raise NotImplementedError


class MemoryDedupeStore:
    """An in-memory, single-process duplicate store.

    Exists to exercise the deduplication semantics in tests and to demonstrate
    the contract. It is **not durable and not transactional**: it does not
    survive a crash, and it makes no guarantee across processes. It must not be
    mistaken for the durable :class:`DedupeStore` implementation the "no
    exactly-once without storage" note requires.
    """

    def __init__(self) -> None:
        self._outcomes: dict[str, str] = {}

    def seen(self, key: str) -> str | None:
        return self._outcomes.get(key)

    def record(self, key: str, outcome: str) -> bool:
        if key in self._outcomes:
            return False
        self._outcomes[key] = outcome
        return True