"""Tests for deterministic idempotency keys and duplicate-event protection."""

from __future__ import annotations

from forge.jobs.idempotency import (
    DedupeStore,
    MemoryDedupeStore,
    event_key,
    idempotency_key,
)


def test_same_inputs_yield_the_same_key():
    assert idempotency_key("OUT-1", "created", "{}") == idempotency_key(
        "OUT-1", "created", "{}"
    )


def test_different_ticket_refs_yield_different_keys():
    assert idempotency_key("OUT-1", "created") != idempotency_key("OUT-2", "created")


def test_different_events_yield_different_keys():
    assert idempotency_key("OUT-1", "created") != idempotency_key("OUT-1", "updated")


def test_different_payloads_yield_different_keys():
    assert idempotency_key("OUT-1", "created", "a") != idempotency_key(
        "OUT-1", "created", "b"
    )


def test_ticket_and_event_cannot_collide_by_concatenation():
    # The separator prevents ("a/b", "") vs ("a", "/b") style collisions.
    assert idempotency_key("OUT-1", "created", "x") != idempotency_key(
        "OUT-1x", "created", ""
    )


def test_keys_are_stable_hex_of_fixed_length():
    key = idempotency_key("OUT-1", "created", "{}")
    assert len(key) == 64
    int(key, 16)  # raises if not hex


def test_event_key_is_ordering_independent():
    a = event_key("OUT-1", "updated", {"b": 1, "a": 2})
    b = event_key("OUT-1", "updated", {"a": 2, "b": 1})
    assert a == b


def test_event_key_distinguishes_different_arguments():
    a = event_key("OUT-1", "updated", {"a": 2, "b": 1})
    b = event_key("OUT-1", "updated", {"a": 2, "b": 2})
    assert a != b


def test_event_key_matches_string_form_of_the_same_payload():
    import json

    from forge.jobs.idempotency import _SEP

    canonical = json.dumps({"a": 2, "b": 1}, sort_keys=True, separators=(",", ":"))
    direct = idempotency_key("OUT-1", "updated", canonical)
    assert event_key("OUT-1", "updated", {"b": 1, "a": 2}) == direct
    assert _SEP not in "OUT-1"


def test_event_key_rejects_unserialisable_payload():
    import pytest

    with pytest.raises(TypeError):
        event_key("OUT-1", "updated", {"bad": object()})


# --------------------------------------------------------------------------
# Duplicate-event protection semantics (via the in-memory reference store)
# --------------------------------------------------------------------------


def test_first_record_returns_new():
    store = MemoryDedupeStore()
    assert store.seen("key") is None
    assert store.record("key", "done") is True


def test_second_record_returns_duplicate():
    store = MemoryDedupeStore()
    store.record("key", "done")
    assert store.record("key", "done") is False


def test_seen_returns_the_recorded_outcome():
    store = MemoryDedupeStore()
    store.record("key", "done:123")
    assert store.seen("key") == "done:123"


def test_distinct_keys_are_independent():
    store = MemoryDedupeStore()
    assert store.record("k1", "done") is True
    assert store.record("k2", "done") is True
    assert store.seen("k1") == "done"
    assert store.seen("k2") == "done"


def test_dedupe_store_is_an_interface_not_a_claim():
    # The abstract contract is separate from the in-memory reference: nothing
    # in the interface promises durability or cross-process atomicity.
    assert DedupeStore.seen is not None
    assert DedupeStore.record is not None
    assert not hasattr(MemoryDedupeStore, "commit")