"""Durable, atomic Jira intake: delivery receipt and job admission (FORGE-13).

This module is the boundary where a Jira webhook delivery becomes a *received
event* and, separately, where an explicitly authorized work request becomes a
*job*. The two are deliberately not the same call:

*   :meth:`SqliteIntakeStore.record_delivery` receives and audits a delivery. It
    deduplicates the transport identity and the canonical event content, records
    every attempt, and -- crucially -- never creates work.
*   :meth:`SqliteIntakeStore.admit_job` is the explicit, authorized action that
    creates (or resolves) a job for a ticket. It is gated on an OPERATOR-role
    actor and is idempotent per ``admission_request_id``.

Three identities are kept separate, matching the three tables that guard them:

*   ``delivery_id`` (transport identity, nullable) -> ``delivery_ids``.
*   ``content_key`` (canonical event content) -> ``intake_events``.
*   ``admission_request_id`` (authorized work request) -> ``admissions``.

The append-only tables ``intake_deliveries`` and ``admission_denials`` audit
every delivery attempt and every refused admission respectively; neither has a
uniqueness constraint that would erase a retry.

Authorisation is **not** this module's to establish. ``admit_job`` trusts an
:class:`~forge.jobs.state.Actor` whose role is ``OPERATOR`` only because the
future Forge integration layer -- never a Jira ticket, an agent prompt, or webhook
content -- is the sole minter of such actors. ``actor.role`` is a shape and a
comparison point, not an authentication mechanism; trusted identity and admission
authorization are enforced upstream. Nothing here imports a network or credential
module, and nothing here manufactures an OPERATOR from delivered content.

Exactly-once boundary, stated plainly: the atomic transaction below makes *job
creation* exactly-once per admission request and per ticket cycle. It does not
make the job's later engineering work exactly-once -- a recovered-and-re-run job
may repeat side effects. Intake guarantees at most one job, not at most one
execution.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from .idempotency import event_key
from .state import (
    BASELINE_TOOLS,
    Actor,
    ActorRole,
    AuthorizationRefused,
    new_job,
)
from .store import DEFAULT_BUSY_TIMEOUT_MS, _connect, _record_history

__all__ = [
    "DeliveryOutcome",
    "AdmissionOutcome",
    "DeliveryResult",
    "AdmissionResult",
    "SqliteIntakeStore",
    "job_id_for",
]

#: States a job can be in that still count as an "open" cycle for admission.
#: Derived by joining ticket_jobs -> jobs; never duplicated as a column.
_TERMINAL_STATES = ("done", "failed")


def job_id_for(ticket_ref: str, cycle: int) -> str:
    """The deterministic job id for one (ticket, cycle) work unit."""
    return f"jira:{ticket_ref}:{cycle}"


class DeliveryOutcome(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    INVALID = "invalid"


class AdmissionOutcome(str, Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"
    OPEN_EXISTS = "open_exists"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class DeliveryResult:
    outcome: DeliveryOutcome
    issue_key: str
    content_key: str | None
    job_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AdmissionResult:
    outcome: AdmissionOutcome
    job_id: str | None = None
    cycle: int | None = None
    reason: str | None = None


def _request_inputs_hash(
    *,
    ticket_ref: str,
    repo: str,
    allowed_tools: Iterable[str],
    budget: float | None,
    allowed_repos: Iterable[str] | None,
    baseline_tools: Iterable[str],
    global_budget: float | None,
) -> str:
    """A canonical fingerprint of the admission inputs.

    Collections are sorted and deduplicated before serialisation so equivalent
    but reordered inputs produce the same hash -- a retry with reordered
    ``allowed_tools``/``allowed_repos``/``baseline_tools`` is recognised as the
    same request, not a conflict.
    """
    canonical = json.dumps(
        {
            "ticket_ref": ticket_ref,
            "repo": repo,
            "allowed_tools": sorted(set(allowed_tools)),
            "budget": budget,
            "allowed_repos": (
                sorted(set(allowed_repos)) if allowed_repos is not None else None
            ),
            "baseline_tools": sorted(set(baseline_tools)),
            "global_budget": global_budget,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SqliteIntakeStore:
    """Durable, transactional intake store over one SQLite connection.

    ``path`` shares the same database file and migrations as
    :class:`~forge.jobs.store.SqliteJobStore`; a fresh file is migrated to the
    current schema on open. A single connection is load-bearing: ``record_delivery``
    and ``admit_job`` each run their dedupe + write as one ``BEGIN IMMEDIATE``
    transaction, which is exactly what makes intake atomic across processes.

    ``clock`` is the timestamp source (default ``time.time``); inject a
    deterministic callable in tests.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._clock = clock
        self._conn = _connect(path, busy_timeout_ms)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteIntakeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- helpers -----------------------------------------------------------

    def _open_job_row(self, ticket_ref: str) -> sqlite3.Row | None:
        """The ticket's newest non-terminal job, or None. State is joined from
        the authoritative ``jobs`` table, not copied into ``ticket_jobs``."""
        return self._conn.execute(
            "SELECT tj.cycle, tj.job_id FROM ticket_jobs tj "
            "JOIN jobs j ON j.id = tj.job_id "
            "WHERE tj.ticket_ref = ? AND j.state NOT IN ('done', 'failed') "
            "ORDER BY tj.cycle DESC LIMIT 1",
            (ticket_ref,),
        ).fetchone()

    def _next_cycle(self, ticket_ref: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(cycle) AS m FROM ticket_jobs WHERE ticket_ref = ?",
            (ticket_ref,),
        ).fetchone()
        return 1 if row["m"] is None else row["m"] + 1

    def _record_denial(
        self,
        admission_request_id: str,
        ticket_ref: str,
        actor: Actor | None,
        reason: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO admission_denials (admission_request_id, ticket_ref, "
            "actor_identity, actor_role, reason, at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                admission_request_id,
                ticket_ref,
                actor.identity if isinstance(actor, Actor) else None,
                actor.role.value if isinstance(actor, Actor) else None,
                reason,
                self._clock(),
            ),
        )

    # -- delivery receipt --------------------------------------------------

    def record_delivery(
        self,
        *,
        delivery_id: str | None = None,
        issue_key: str,
        event_type: str,
        arguments: Mapping[str, object],
        jira_version: str | None = None,
        jira_updated_at: float | None = None,
        status: str | None = None,
    ) -> DeliveryResult:
        """Receive one delivery: dedupe + audit, never create work.

        Dedupe and conflict rules, in precedence order:

        1. ``arguments`` that cannot be canonicalised -> INVALID (audited with a
           NULL ``content_key``; ``delivery_ids``/``intake_events`` untouched).
        2. a ``delivery_id`` already bound to a *different* content -> CONFLICT.
        3. ``content_key`` already seen -> DUPLICATE.
        4. otherwise -> ACCEPTED.

        A new ``delivery_id`` is bound to its content in every non-CONFLICT case,
        so a later conflicting replay of that id is detectable. Every attempt is
        appended to ``intake_deliveries`` regardless of outcome.
        """
        content_key: str | None
        reason: str | None = None
        try:
            content_key = event_key(issue_key, event_type, arguments)
        except (TypeError, ValueError) as exc:
            content_key = None
            reason = f"payload not canonicalizable: {type(exc).__name__}"

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            outcome: DeliveryOutcome
            if content_key is None:
                outcome = DeliveryOutcome.INVALID
            else:
                outcome = DeliveryOutcome.ACCEPTED
                if delivery_id is not None:
                    bound = self._conn.execute(
                        "SELECT content_key FROM delivery_ids WHERE delivery_id = ?",
                        (delivery_id,),
                    ).fetchone()
                    if bound is not None and bound["content_key"] != content_key:
                        outcome = DeliveryOutcome.CONFLICT
                        reason = "delivery_id reused with different content"
                if outcome is not DeliveryOutcome.CONFLICT:
                    seen = self._conn.execute(
                        "SELECT 1 FROM intake_events WHERE content_key = ?",
                        (content_key,),
                    ).fetchone()
                    if seen is not None:
                        outcome = DeliveryOutcome.DUPLICATE
                    # Bind a new delivery_id to its content even when the content
                    # is a duplicate, so future conflicting replays are detected.
                    if delivery_id is not None:
                        bound = self._conn.execute(
                            "SELECT 1 FROM delivery_ids WHERE delivery_id = ?",
                            (delivery_id,),
                        ).fetchone()
                        if bound is None:
                            self._conn.execute(
                                "INSERT INTO delivery_ids (delivery_id, content_key) "
                                "VALUES (?, ?)",
                                (delivery_id, content_key),
                            )
                    if outcome is DeliveryOutcome.ACCEPTED:
                        self._conn.execute(
                            "INSERT INTO intake_events (content_key, issue_key, "
                            "event_type, outcome, first_seen_at) VALUES (?, ?, ?, ?, ?)",
                            (content_key, issue_key, event_type, "accepted", self._clock()),
                        )

            resolved_job_id = self._open_job_row(issue_key)
            now = self._clock()
            self._conn.execute(
                "INSERT INTO intake_deliveries (delivery_id, content_key, issue_key, "
                "event_type, jira_version, jira_updated_at, status, outcome, job_id, "
                "reason, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    content_key,
                    issue_key,
                    event_type,
                    jira_version,
                    jira_updated_at,
                    status,
                    outcome.value,
                    resolved_job_id["job_id"] if resolved_job_id is not None else None,
                    reason,
                    now,
                ),
            )
            self._conn.execute("COMMIT")
            return DeliveryResult(
                outcome=outcome,
                issue_key=issue_key,
                content_key=content_key,
                job_id=(
                    resolved_job_id["job_id"] if resolved_job_id is not None else None
                ),
                reason=reason,
            )
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    # -- admission ---------------------------------------------------------

    def admit_job(
        self,
        admission_request_id: str,
        ticket_ref: str,
        actor: Actor,
        *,
        repo: str,
        allowed_tools: Iterable[str] = (),
        budget: float | None = None,
        allowed_repos: Iterable[str] | None = None,
        baseline_tools: Iterable[str] = BASELINE_TOOLS,
        global_budget: float | None = None,
    ) -> AdmissionResult:
        """Admit exactly one job for ``ticket_ref``, idempotently per request.

        Requires an OPERATOR actor (trusted boundary). Repeat the same
        ``admission_request_id`` and the original job is returned even if it is
        now terminal; a reused id with *changed* inputs is a CONFLICT, not a
        duplicate. A distinct request while an open job exists returns that job
        for visibility (``OPEN_EXISTS``) without binding the new request; a new
        cycle is minted only when no non-terminal job exists.

        Denied admissions (unauthorized, changed inputs, open cycle) are audited
        durably in ``admission_denials``; none creates a job.
        """
        if not isinstance(actor, Actor) or actor.role is not ActorRole.OPERATOR:
            # Audit the denial durably and COMMIT it BEFORE raising, so the outer
            # exception handling cannot roll the denial back with the job.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._record_denial(
                    admission_request_id, ticket_ref, actor, "unauthorized admission"
                )
                self._conn.execute("COMMIT")
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            raise AuthorizationRefused("admission requires the operator role")

        # Canonicalize each collection ONCE and reuse the materialized tuples for
        # BOTH the request fingerprint and job construction. Without this, a
        # generator passed as allowed_tools/allowed_repos/baseline_tools would be
        # consumed by the fingerprint, leaving new_job() an exhausted iterable.
        tools = tuple(sorted(set(allowed_tools)))
        repos = None if allowed_repos is None else tuple(sorted(set(allowed_repos)))
        baseline = tuple(sorted(set(baseline_tools)))

        request_hash = _request_inputs_hash(
            ticket_ref=ticket_ref,
            repo=repo,
            allowed_tools=tools,
            budget=budget,
            allowed_repos=repos,
            baseline_tools=baseline,
            global_budget=global_budget,
        )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            prior = self._conn.execute(
                "SELECT cycle, job_id, request_inputs_hash FROM admissions "
                "WHERE admission_request_id = ?",
                (admission_request_id,),
            ).fetchone()
            if prior is not None:
                if prior["request_inputs_hash"] != request_hash:
                    self._record_denial(
                        admission_request_id,
                        ticket_ref,
                        actor,
                        "admission_request_id reused with changed inputs",
                    )
                    self._conn.execute("COMMIT")
                    return AdmissionResult(
                        AdmissionOutcome.CONFLICT, reason="changed inputs"
                    )
                self._conn.execute("COMMIT")
                return AdmissionResult(
                    AdmissionOutcome.DUPLICATE,
                    job_id=prior["job_id"],
                    cycle=prior["cycle"],
                )

            open_row = self._open_job_row(ticket_ref)
            if open_row is not None:
                self._record_denial(
                    admission_request_id, ticket_ref, actor, "open cycle exists"
                )
                self._conn.execute("COMMIT")
                return AdmissionResult(
                    AdmissionOutcome.OPEN_EXISTS,
                    job_id=open_row["job_id"],
                    cycle=open_row["cycle"],
                    reason="open cycle exists",
                )

            cycle = self._next_cycle(ticket_ref)
            job_id = job_id_for(ticket_ref, cycle)
            job = new_job(
                job_id=job_id,
                ticket_ref=ticket_ref,
                repo=repo,
                allowed_tools=tools,
                budget=budget,
                baseline_tools=baseline,
                allowed_repos=repos,
                global_budget=global_budget,
            )
            now = self._clock()
            self._conn.execute(
                "INSERT INTO jobs (id, ticket_ref, repo, state, allowed_tools, "
                "budget, owner, created_at, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    job.id,
                    job.ticket_ref,
                    job.repo,
                    job.state.value,
                    json.dumps(list(job.allowed_tools)),
                    job.budget,
                    job.owner,
                    now,
                    now,
                ),
            )
            _record_history(
                self._conn,
                job_id=job.id,
                event="register",
                from_state=None,
                to_state=job.state.value,
                owner=job.owner,
                outcome="ok",
                at=now,
            )
            self._conn.execute(
                "INSERT INTO ticket_jobs (ticket_ref, cycle, job_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (ticket_ref, cycle, job_id, now),
            )
            self._conn.execute(
                "INSERT INTO admissions (admission_request_id, ticket_ref, cycle, "
                "job_id, request_inputs_hash, actor_identity, actor_role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    admission_request_id,
                    ticket_ref,
                    cycle,
                    job_id,
                    request_hash,
                    actor.identity,
                    actor.role.value,
                    now,
                ),
            )
            self._conn.execute("COMMIT")
            return AdmissionResult(
                AdmissionOutcome.CREATED, job_id=job_id, cycle=cycle
            )
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise