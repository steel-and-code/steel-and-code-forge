"""Durable, SQLite-backed job persistence (FORGE-11, FORGE-12).

This module gives the Phase A ``JobStore`` and ``DedupeStore`` interfaces a
real, restart-safe implementation. It is the layer that turns the pure
``forge.jobs.state`` machine -- which deliberately claims nothing about
cross-process atomicity -- into a store whose ``claim`` and ``transition`` are
transactional and durable.

FORGE-12 adds durable leases, lease-generation fencing, and the operator
recovery path on top of the FORGE-11 store, without moving any of the state
machine here. What is *not* moved or reimplemented:

*   The state machine. :func:`forge.jobs.state.transition` stays the single
    source of truth for what ``(state, event)`` is legal and for every
    ownership/role/budget refusal. This store loads a row, passes it to that
    pure function, and writes back the successor. The machine is exercised, not
    copied.
*   Idempotency key derivation. :func:`~forge.jobs.idempotency.idempotency_key`
    and :func:`~forge.jobs.idempotency.event_key` are untouched.

Design decisions (deliberate):

*   **Timestamps and leases live in the store, not on the value.** ``Job`` is
    frozen and clock-free by house invariant ("no clock/default transitions").
    Row ``created_at`` / ``updated_at`` / ``lease_expires_at`` and the
    ``lease_generation`` counter are store metadata, surfaced through
    :class:`JobRecord`, never added to ``Job``.
*   **One authoritative mutation path.** ``save`` is intake/registration only
    (it refuses a duplicate id). Every state change runs through ``transition``,
    ``claim``, or ``recover`` inside a ``BEGIN IMMEDIATE`` transaction with an
    optimistic version-guarded update. A progressed job is never silently
    overwritten.
*   **Fail closed on budget.** ``claim`` and ``start`` require a
    :class:`~forge.jobs.state.BudgetCheck`; an absent or failing check refuses
    exactly as the pure machine does. The durable ``BudgetLedger`` remains
    deferred.
*   **Refusals are audited, not swallowed.** A refused transition records a
    ``job_history`` row with ``from_state == to_state`` and ``outcome`` set to
    the exception class name, then re-raises.
*   **No general exactly-once claim.** Atomic ``claim``, durable dedupe, and
    lease fencing make *claim ownership* and *duplicate-event recognition* safe
    across processes. They do **not** make external side effects exactly-once:
    recording a key and committing an outcome must still be one transaction at
    the call site (the later intake phase). A recovered-and-re-run job may
    repeat its side effects; fencing only guarantees no two workers hold a live
    lease at once.

Leases and fencing (FORGE-12):

*   A claim mints a lease (``lease_expires_at = now + lease_ttl``) and bumps
    ``lease_generation``. The generation is the worker's *fencing token*: every
    renew and worker mutation must present the generation the worker was handed
    at claim time. A mismatched generation means the claim was superseded (by a
    same-identity re-claim or an operator recovery) and is refused with
    :class:`LeaseExpired`.
*   A worker mutation is refused as :class:`LeaseExpired` once the lease has
    lapsed. Renewal after expiry is refused the same way (no self-healing); the
    only way forward is an explicit operator :meth:`SqliteJobStore.recover`.
*   Leases never expire a job *into* a state. "Expired" is a liveness label on
    the row, not a transition: a job is never auto-marked done or failed by a
    timeout.

Concurrency model: every write opens a ``BEGIN IMMEDIATE`` transaction (the
writer lock), so read-then-write is serialised across connections and processes
against a shared file. The version-guarded ``UPDATE ... WHERE id = ? AND
version = ?`` is a second, correctness defence that raises
:class:`ConcurrencyConflict` if a row changed underneath. The generation fence
is a third, lease-specific defence: it guards ownership continuity, which the
version counter (bumped on every write) does not.

This module is stdlib-only (``sqlite3``), matching the zero-runtime-dependency
rule in ``pyproject.toml``. It imports no subprocess, network, or credential
module.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .idempotency import DedupeStore
from .state import (
    Actor,
    ActorRole,
    AuthorizationRefused,
    BudgetCheck,
    ClaimConflict,
    Job,
    JobEvent,
    JobState,
    JobStore,
    JobTransitionError,
    transition as _transition,
)

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_LEASE_TTL",
    "JobRecord",
    "JobStoreError",
    "JobNotFound",
    "JobAlreadyExists",
    "ConcurrencyConflict",
    "LeaseExpired",
    "MigrationError",
    "SqliteJobStore",
    "SqliteDedupeStore",
]


# --------------------------------------------------------------------------
# Schema and migrations
# --------------------------------------------------------------------------

#: Ordered migrations. Each entry is ``(target_version, statements)``; applying
#: entry ``i`` moves ``PRAGMA user_version`` from ``i`` to ``i + 1``. The
#: migration list is the single source of truth for the schema.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE jobs (
                id            TEXT PRIMARY KEY,
                ticket_ref    TEXT NOT NULL,
                repo          TEXT NOT NULL,
                state         TEXT NOT NULL,
                allowed_tools TEXT NOT NULL,
                budget        REAL,
                owner         TEXT,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL,
                version       INTEGER NOT NULL DEFAULT 0
            )
            """,
            """
            CREATE TABLE job_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id      TEXT NOT NULL,
                event       TEXT NOT NULL,
                from_state  TEXT,
                to_state    TEXT NOT NULL,
                owner       TEXT,
                outcome     TEXT NOT NULL,
                at          REAL NOT NULL
            )
            """,
            "CREATE INDEX ix_job_history_job_id ON job_history (job_id)",
            """
            CREATE TABLE dedupe (
                key        TEXT PRIMARY KEY,
                outcome    TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """,
        ),
    ),
    (
        2,
        (
            # FORGE-12: durable leases + fencing + actor/role audit. Additive
            # only, so a v1 database upgrades in place with no rewrite. Owned
            # rows from a v1 database carry lease_expires_at = NULL, which the
            # store treats as expired (operator-recoverable, fail-closed).
            "ALTER TABLE jobs ADD COLUMN lease_expires_at REAL",
            "ALTER TABLE jobs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE job_history ADD COLUMN actor_identity TEXT",
            "ALTER TABLE job_history ADD COLUMN actor_role TEXT",
            "ALTER TABLE job_history ADD COLUMN reason TEXT",
        ),
    ),
)

#: The current schema version. Kept equal to ``MIGRATIONS[-1][0]``; a change to
#: the migration list that forgets to move the tail is a bug.
SCHEMA_VERSION: int = MIGRATIONS[-1][0]

#: Default SQLite busy timeout (milliseconds) for concurrent openers.
DEFAULT_BUSY_TIMEOUT_MS: int = 5000

#: Default lease duration (seconds) handed to a worker at claim time. A worker
#: renews on a shorter heartbeat cadence; this is the ceiling it must never
#: allow to lapse. Injectable so tests can use sub-second leases.
DEFAULT_LEASE_TTL: float = 300.0

#: ``PRAGMA journal_mode=WAL`` writes to the file and can hit SQLITE_BUSY when a
#: peer process is mid-creation or mid-WAL-switch on a brand-new database; the
#: busy handler does not reliably retry that specific pragma, so retry here.
_WAL_ATTEMPTS: int = 20
_WAL_RETRY_SLEEP: float = 0.05


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL, retrying the first-open race on a fresh database."""
    for attempt in range(_WAL_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if attempt == _WAL_ATTEMPTS - 1:
                raise
            time.sleep(_WAL_RETRY_SLEEP)


def _connect(path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    """Open a connection configured for durability and concurrent writers."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA synchronous=FULL")
    _enable_wal(conn)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring ``conn`` up to :data:`SCHEMA_VERSION` in one writer transaction.

    The writer lock is acquired *before* the version is read, so two processes
    opening a fresh file concurrently cannot both decide to run the same
    migration: the writer lock serialises the read-decide-apply-commit cycle,
    and a second connection's read sees the version the first already wrote.

    A database whose version is *newer* than this implementation supports is
    rejected rather than silently opened -- that database was written by a
    future Forge and must not be read as if it were current.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise MigrationError(
                f"database schema version {current} is newer than the "
                f"supported version {SCHEMA_VERSION}; refusing to open"
            )
        if current < SCHEMA_VERSION:
            try:
                for version, statements in MIGRATIONS[current:]:
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version = {version}")
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(
                    f"migration to schema version {SCHEMA_VERSION} failed"
                ) from exc
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# Records and errors
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class JobRecord:
    """A stored job plus its store-level metadata.

    ``job`` is the reconstructed :class:`~forge.jobs.state.Job` value;
    ``created_at`` / ``updated_at`` / ``version`` / ``lease_expires_at`` /
    ``lease_generation`` are the persistence metadata the frozen, clock-free
    ``Job`` value deliberately does not carry.

    ``lease_expires_at`` and ``lease_generation`` are read-only diagnostics: they
    let a caller *display* that a lease is stale, but the store's in-transaction
    comparison remains the only authority on whether a lease has lapsed.
    """

    job: Job
    created_at: float
    updated_at: float
    version: int
    lease_expires_at: float | None
    lease_generation: int


class JobStoreError(Exception):
    """Base for errors the durable store raises that are not value-level
    transition refusals. The transition refusals themselves
    (:class:`~forge.jobs.state.BudgetRefused`, etc.) are raised untouched."""


class JobNotFound(JobStoreError):
    """The requested job id is not present in the store.

    Distinct from :meth:`SqliteJobStore.load`, which returns ``None`` for an
    absent id by contract; the mutating methods raise this instead."""


class JobAlreadyExists(JobStoreError):
    """``save`` was asked to register a job id that is already stored.

    Registration is intake-only; re-registering an id would let a second intake
    silently overwrite a job that may have progressed, so it is refused."""


class ConcurrencyConflict(JobStoreError):
    """A row changed between read and write (optimistic-version guard missed).

    Should not occur under the ``BEGIN IMMEDIATE`` writer lock; it exists as a
    correctness backstop, not the normal path."""


class LeaseExpired(JobStoreError):
    """The acting worker's lease has lapsed, or its generation is stale.

    Raised when a worker mutation or renewal presents no live lease, renews
    after expiry, or carries a generation token that no longer matches the
    stored one (the claim was superseded by a re-claim or an operator recovery).
    The only forward path from here is an explicit operator recovery."""


class MigrationError(JobStoreError):
    """A schema migration failed; the database was left at its prior version."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _record_history(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    event: str,
    from_state: str | None,
    to_state: str,
    owner: str | None,
    outcome: str,
    at: float,
    actor: Actor | None = None,
    reason: str | None = None,
) -> None:
    """Append one audit row, separating the acting party from the job owner.

    ``owner`` is the *job's* owner snapshot at the moment of the event (distinct
    from whoever acted); ``actor_identity`` / ``actor_role`` record the acting
    party, and ``reason`` carries the operator's justification on a recovery.
    """
    conn.execute(
        "INSERT INTO job_history (job_id, event, from_state, to_state, owner, "
        "outcome, at, actor_identity, actor_role, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            event,
            from_state,
            to_state,
            owner,
            outcome,
            at,
            actor.identity if actor is not None else None,
            actor.role.value if actor is not None else None,
            reason,
        ),
    )


def _enforce_lease(
    job_id: str, row: sqlite3.Row, generation: int | None, now: float
) -> None:
    """Refuse a worker mutation unless its lease is live and un-superseded.

    Two independent checks, both failing closed:

    *   the lease must exist and not have lapsed; a NULL or past expiry means
        the row is either legacy-unleased or stale, and only an operator
        recovery can move it;
    *   the presented generation token must equal the stored one; a mismatch
        means the claim was superseded by a re-claim or a recovery.
    """
    lease_expires_at = row["lease_expires_at"]
    if lease_expires_at is None or lease_expires_at < now:
        raise LeaseExpired(
            f"job {job_id!r} lease is not live; an operator recovery is required"
        )
    if generation is None:
        raise LeaseExpired(f"job {job_id!r} requires a lease generation token")
    if generation != row["lease_generation"]:
        raise LeaseExpired(
            f"job {job_id!r} lease generation is stale; the claim was superseded"
        )


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------


class SqliteJobStore(JobStore):
    """Durable, transactional :class:`JobStore` on a single SQLite file.

    ``path`` is the database file (created if absent). Pass ``":memory:"`` for
    a single-connection, non-shared store -- fine for tests, but restart
    recovery and cross-process/concurrent-claim tests need a real file path.

    ``clock`` is the timestamp source (default ``time.time``); inject a
    deterministic callable to make tests independent of the wall clock.
    ``lease_ttl`` is the lease duration handed out at claim time (default
    :data:`DEFAULT_LEASE_TTL`); inject a small value so lease-expiry tests do
    not sleep.

    Every mutating method first runs migrations, so opening a store against a
    fresh or an older file brings it to :data:`SCHEMA_VERSION`.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> None:
        self._path = path
        self._clock = clock
        self._lease_ttl = float(lease_ttl)
        self._conn = _connect(path, busy_timeout_ms)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection. Safe to call repeatedly."""
        self._conn.close()

    def __enter__(self) -> "SqliteJobStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        """The ``PRAGMA user_version`` currently recorded in the database."""
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # -- reads -------------------------------------------------------------

    def load(self, job_id: str) -> Job | None:
        """Return the stored :class:`Job` for ``job_id``, or ``None``."""
        row = self._conn.execute(
            "SELECT id, ticket_ref, repo, state, allowed_tools, budget, owner "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return None if row is None else _row_to_job(row)

    def get(self, job_id: str) -> JobRecord | None:
        """Return the stored job *and* its metadata, or ``None`` if absent."""
        row = self._conn.execute(
            "SELECT id, ticket_ref, repo, state, allowed_tools, budget, owner, "
            "created_at, updated_at, version, lease_expires_at, lease_generation "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return JobRecord(
            job=_row_to_job(row),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
            lease_expires_at=row["lease_expires_at"],
            lease_generation=row["lease_generation"],
        )

    # -- intake ------------------------------------------------------------

    def save(self, job: Job) -> Job:
        """Register a new job (intake). Refuses an already-stored id.

        This is the only way a job enters the store. It persists the validated
        value, records a ``register`` history entry, and returns the job
        unchanged. State changes afterwards must go through ``transition``,
        ``claim``, or ``recover``.
        """
        if not isinstance(job, Job):
            raise TypeError(f"save expects a Job, got {job!r}")
        now = self._clock()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            exists = self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job.id,)
            ).fetchone()
            if exists is not None:
                raise JobAlreadyExists(job.id)
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
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        return job

    # -- mutation ----------------------------------------------------------

    def claim(
        self, job_id: str, actor: Actor, budget_check: BudgetCheck | None = None
    ) -> JobRecord:
        """Atomically claim ``job_id`` for ``actor``.

        Sets the owner from ``actor.identity``, mints a lease (``now + lease_ttl``)
        and a fresh generation token, and returns the resulting
        :class:`JobRecord` so the caller holds the token it must present on
        later renew/mutation calls.

        ``budget_check`` failures close exactly as the pure machine does. A job
        that is not ``ready`` is refused.
        """
        if not isinstance(actor, Actor):
            raise TypeError(f"claim expects an Actor, got {actor!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id, ticket_ref, repo, state, allowed_tools, budget, "
                "owner, created_at, updated_at, version, lease_generation "
                "FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = _row_to_job(row)

            try:
                successor = _transition(
                    job, JobEvent.CLAIM, actor=actor, budget_check=budget_check
                )
            except JobTransitionError as exc:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event=JobEvent.CLAIM.value,
                    from_state=job.state.value,
                    to_state=job.state.value,
                    owner=job.owner,
                    outcome=type(exc).__name__,
                    at=self._clock(),
                    actor=actor,
                )
                self._conn.execute("COMMIT")
                raise

            now = self._clock()
            new_generation = row["lease_generation"] + 1
            lease_expires_at = now + self._lease_ttl
            updated = self._conn.execute(
                "UPDATE jobs SET state = ?, owner = ?, updated_at = ?, "
                "lease_expires_at = ?, lease_generation = ?, version = version + 1 "
                "WHERE id = ? AND version = ?",
                (
                    successor.state.value,
                    successor.owner,
                    now,
                    lease_expires_at,
                    new_generation,
                    job_id,
                    row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"job {job_id!r} changed between read and write"
                )
            _record_history(
                self._conn,
                job_id=job_id,
                event=JobEvent.CLAIM.value,
                from_state=job.state.value,
                to_state=successor.state.value,
                owner=successor.owner,
                outcome="ok",
                at=now,
                actor=actor,
            )
            self._conn.execute("COMMIT")
            return JobRecord(
                job=successor,
                created_at=row["created_at"],
                updated_at=now,
                version=row["version"] + 1,
                lease_expires_at=lease_expires_at,
                lease_generation=new_generation,
            )
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def transition(
        self,
        job_id: str,
        event: JobEvent,
        *,
        actor: Actor | None = None,
        budget_check: BudgetCheck | None = None,
        generation: int | None = None,
    ) -> Job:
        """Apply ``event`` to the stored job inside one transaction.

        Loads the row, enforces the lease/generation fence for worker-driven
        events, runs the pure :func:`~forge.jobs.state.transition`, then writes
        the successor and its history entry atomically. Ownership, role and
        budget refusals raised by the pure machine are recorded to history (with
        ``from_state == to_state`` and the refusal as ``outcome``) and re-raised.

        ``claim`` and ``recover`` are served by their dedicated methods; passing
        either here is a programming error and raises ``ValueError``.
        """
        if not isinstance(event, JobEvent):
            raise TypeError(f"event must be a JobEvent, got {event!r}")
        if event in (JobEvent.CLAIM, JobEvent.RECOVER):
            raise ValueError(
                f"the {event.value!r} operation is served by its dedicated "
                "method (claim() / recover()), not transition()"
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id, ticket_ref, repo, state, allowed_tools, budget, "
                "owner, version, lease_generation, lease_expires_at "
                "FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = _row_to_job(row)
            now = self._clock()

            try:
                # The lease/generation fence is a property of the owning
                # worker's lease, so it applies only when the actor IS the
                # owner. A non-owner worker is refused by the pure machine's
                # identity check (ClaimConflict), not by the fence.
                if (
                    actor is not None
                    and actor.role is ActorRole.WORKER
                    and actor.identity == row["owner"]
                ):
                    _enforce_lease(job_id, row, generation, now)
                successor = _transition(
                    job, event, actor=actor, budget_check=budget_check
                )
            except LeaseExpired as exc:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event=event.value,
                    from_state=job.state.value,
                    to_state=job.state.value,
                    owner=job.owner,
                    outcome=type(exc).__name__,
                    at=self._clock(),
                    actor=actor,
                )
                self._conn.execute("COMMIT")
                raise
            except JobTransitionError as exc:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event=event.value,
                    from_state=job.state.value,
                    to_state=job.state.value,
                    owner=job.owner,
                    outcome=type(exc).__name__,
                    at=self._clock(),
                    actor=actor,
                )
                self._conn.execute("COMMIT")
                raise

            now = self._clock()
            updated = self._conn.execute(
                "UPDATE jobs SET state = ?, owner = ?, updated_at = ?, "
                "version = version + 1 WHERE id = ? AND version = ?",
                (successor.state.value, successor.owner, now, job_id, row["version"]),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"job {job_id!r} changed between read and write"
                )
            _record_history(
                self._conn,
                job_id=job_id,
                event=event.value,
                from_state=job.state.value,
                to_state=successor.state.value,
                owner=successor.owner,
                outcome="ok",
                at=now,
                actor=actor,
            )
            self._conn.execute("COMMIT")
            return successor
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def renew(self, job_id: str, actor: Actor, generation: int) -> float:
        """Extend the owning worker's lease, returning the new expiry.

        A store-level liveness operation, not a state transition: it requires the
        owning WORKER (identity + role), a matching, un-expired lease, and the
        current generation token. Renewal after expiry is refused
        (:class:`LeaseExpired`) -- there is no implicit grace period and no
        worker self-healing once the lease lapses. Appends a ``renew`` audit row.
        """
        if not isinstance(actor, Actor):
            raise TypeError(f"renew expects an Actor, got {actor!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id, state, owner, version, lease_generation, "
                "lease_expires_at FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            now = self._clock()

            try:
                if actor.role is not ActorRole.WORKER:
                    raise AuthorizationRefused("only the owning worker may renew a lease")
                if actor.identity != row["owner"]:
                    raise ClaimConflict(
                        f"actor {actor.identity!r} does not match the job's owner"
                    )
                _enforce_lease(job_id, row, generation, now)
            except (LeaseExpired, JobTransitionError) as exc:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event="renew",
                    from_state=row["state"],
                    to_state=row["state"],
                    owner=row["owner"],
                    outcome=type(exc).__name__,
                    at=now,
                    actor=actor,
                )
                self._conn.execute("COMMIT")
                raise

            new_expiry = now + self._lease_ttl
            self._conn.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (new_expiry, now, job_id),
            )
            _record_history(
                self._conn,
                job_id=job_id,
                event="renew",
                from_state=row["state"],
                to_state=row["state"],
                owner=row["owner"],
                outcome="ok",
                at=now,
                actor=actor,
            )
            self._conn.execute("COMMIT")
            return new_expiry
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise

    def recover(
        self, job_id: str, actor: Actor, reason: str, *, force: bool = False
    ) -> Job:
        """Operator-only, atomic recovery of an owned job back to ``ready``.

        Returns the job to ``ready``, clears the owner and lease, and bumps the
        generation in the same transaction -- atomically invalidating the prior
        worker's token so any in-flight worker mutation or renewal fences closed.

        ``actor`` must carry role ``OPERATOR`` (that authority is established by
        the trusted Forge boundary, not by this store). ``reason`` is mandatory
        and non-empty; it is recorded verbatim in the audit trail. Recovering a
        job whose lease is *still live* additionally requires ``force=True`` --
        the explicit operator acknowledgment that a healthy lease is being
        overridden.
        """
        if not isinstance(actor, Actor):
            raise TypeError(f"recover expects an Actor, got {actor!r}")
        if actor.role is not ActorRole.OPERATOR:
            raise AuthorizationRefused("recovery requires the operator role")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("recover requires a non-empty reason")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id, ticket_ref, repo, state, allowed_tools, budget, "
                "owner, version, lease_generation, lease_expires_at "
                "FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = _row_to_job(row)
            now = self._clock()

            lease_live = (
                row["lease_expires_at"] is not None
                and row["lease_expires_at"] >= now
            )
            if lease_live and not force:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event=JobEvent.RECOVER.value,
                    from_state=job.state.value,
                    to_state=job.state.value,
                    owner=job.owner,
                    outcome="AuthorizationRefused",
                    at=now,
                    actor=actor,
                    reason=reason,
                )
                self._conn.execute("COMMIT")
                raise AuthorizationRefused(
                    f"job {job_id!r} has a live lease; recovering it requires "
                    "explicit operator authorization (force=True)"
                )

            try:
                successor = _transition(job, JobEvent.RECOVER, actor=actor)
            except JobTransitionError as exc:
                _record_history(
                    self._conn,
                    job_id=job_id,
                    event=JobEvent.RECOVER.value,
                    from_state=job.state.value,
                    to_state=job.state.value,
                    owner=job.owner,
                    outcome=type(exc).__name__,
                    at=now,
                    actor=actor,
                    reason=reason,
                )
                self._conn.execute("COMMIT")
                raise

            new_generation = row["lease_generation"] + 1
            updated = self._conn.execute(
                "UPDATE jobs SET state = ?, owner = ?, lease_expires_at = NULL, "
                "lease_generation = ?, updated_at = ?, version = version + 1 "
                "WHERE id = ? AND version = ?",
                (
                    successor.state.value,
                    successor.owner,
                    new_generation,
                    now,
                    job_id,
                    row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"job {job_id!r} changed between read and write"
                )
            _record_history(
                self._conn,
                job_id=job_id,
                event=JobEvent.RECOVER.value,
                from_state=job.state.value,
                to_state=successor.state.value,
                owner=successor.owner,
                outcome="ok",
                at=now,
                actor=actor,
                reason=reason,
            )
            self._conn.execute("COMMIT")
            return successor
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise


class SqliteDedupeStore(DedupeStore):
    """Durable :class:`DedupeStore` over the same schema's ``dedupe`` table.

    May share a database file with :class:`SqliteJobStore`: both run the same
    migrations, and each holds its own connection. ``record`` is a single
    auto-committed ``INSERT`` guarded by the ``key`` primary key, so the
    first-writer-wins guarantee is atomic and durable across processes.
    """

    def __init__(
        self,
        path: str,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._path = path
        self._clock = clock
        self._conn = _connect(path, busy_timeout_ms)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteDedupeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def seen(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT outcome FROM dedupe WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row["outcome"]

    def record(self, key: str, outcome: str) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO dedupe (key, outcome, created_at) VALUES (?, ?, ?)",
                (key, outcome, self._clock()),
            )
        except sqlite3.IntegrityError:
            return False
        return True


# --------------------------------------------------------------------------
# Row reconstruction
# --------------------------------------------------------------------------


def _row_to_job(row: sqlite3.Row) -> Job:
    """Reconstruct the :class:`Job` value from a ``jobs`` row."""
    return Job(
        id=row["id"],
        ticket_ref=row["ticket_ref"],
        repo=row["repo"],
        state=JobState(row["state"]),
        allowed_tools=tuple(json.loads(row["allowed_tools"])),
        budget=row["budget"],
        owner=row["owner"],
    )