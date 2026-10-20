"""Forge job contract: Phase A surface.

``state`` is the job model and its deterministic transitions; ``idempotency`` is
the duplicate-event key and its storage contract. Together they are the
"explicit Jira -> Forge job contract" issue #3 asks for, as a pure, testable
value layer -- no execution, no network, no credentials, no persistence.

The interfaces that need durable, transactional storage (:class:`JobStore`,
:class:`BudgetLedger`, :class:`DedupeStore`) are defined but deliberately not
wired to anything here. Exactly-once execution is a later requirement that the
pure module does not claim.
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

__all__ = [
    "BASELINE_TOOLS",
    "TERMINAL_STATES",
    "BudgetCheck",
    "BudgetLedger",
    "BudgetRefused",
    "ClaimConflict",
    "DedupeStore",
    "InvalidTransition",
    "Job",
    "JobEvent",
    "JobState",
    "JobStore",
    "JobTransitionError",
    "MemoryDedupeStore",
    "event_key",
    "idempotency_key",
    "new_job",
    "transition",
]