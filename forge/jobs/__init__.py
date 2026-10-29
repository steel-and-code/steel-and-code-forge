"""Forge job contract: Phase A surface plus the durable store (Phase B).

``state`` is the job model and its deterministic transitions; ``idempotency`` is
the duplicate-event key and its storage contract; ``store`` is the durable,
SQLite-backed implementation of :class:`JobStore` and :class:`DedupeStore`.

The pure value layer (no execution, no network, no credentials, no persistence)
is unchanged by the addition of ``store``: the durable layer builds on it
without modifying it.
"""

from .idempotency import (
    DedupeStore,
    MemoryDedupeStore,
    event_key,
    idempotency_key,
)
from .state import (
    BASELINE_TOOLS,
    TERMINAL_STATES,
    BudgetCheck,
    BudgetLedger,
    BudgetRefused,
    ClaimConflict,
    InvalidTransition,
    Job,
    JobEvent,
    JobState,
    JobStore,
    JobTransitionError,
    new_job,
    transition,
)
from .store import (
    SCHEMA_VERSION,
    ConcurrencyConflict,
    JobAlreadyExists,
    JobNotFound,
    JobRecord,
    JobStoreError,
    MigrationError,
    SqliteDedupeStore,
    SqliteJobStore,
)

__all__ = [
    "BASELINE_TOOLS",
    "TERMINAL_STATES",
    "SCHEMA_VERSION",
    "BudgetCheck",
    "BudgetLedger",
    "BudgetRefused",
    "ClaimConflict",
    "ConcurrencyConflict",
    "DedupeStore",
    "InvalidTransition",
    "Job",
    "JobAlreadyExists",
    "JobEvent",
    "JobNotFound",
    "JobRecord",
    "JobState",
    "JobStore",
    "JobStoreError",
    "JobTransitionError",
    "MemoryDedupeStore",
    "MigrationError",
    "SqliteDedupeStore",
    "SqliteJobStore",
    "event_key",
    "idempotency_key",
    "new_job",
    "transition",
]