"""Durable, SQLite-backed job persistence (FORGE-11).

This module gives the Phase A ``JobStore`` and ``DedupeStore`` interfaces a
real, restart-safe implementation. It is the layer that turns the pure
``forge.jobs.state`` machine -- which deliberately claims nothing about
cross-process atomicity -- into a store whose ``claim`` and ``transition`` are
transactional and durable.

What is *not* moved or reimplemented here:

*   The state machine. :func:`forge.jobs.state.transition` stays the single
    source of truth for what ``(state, event)`` is legal and for every
    ownership/budget refusal. This store loads a row, passes it to that pure
    function, and writes back the successor. The machine is exercised, not
    copied, so requirement "preserve the existing state machine" holds by
    construction.
*   Idempotency key derivation. :func:`~forge.jobs.idempotency.idempotency_key`
    and :func:`~forge.jobs.idempotency.event_key` are untouched. Only the
    durable *storage* behind ``DedupeStore`` is added.

Design decisions (deliberate):

*   **Timestamps live in the store, not on the value.** ``Job`` is frozen and
    clock-free by house invariant ("no clock/default transitions"). Row
    ``created_at`` / ``updated_at`` are store metadata, surfaced through
    :class:`JobRecord`, never added to ``Job``.
*   **One authoritative mutation path.** ``save`` is intake/registration only
    (it refuses a duplicate id). Every state change runs through ``transition``
    or its ``claim`` convenience, inside a ``BEGIN IMMEDIATE`` transaction with
    an optimistic version-guarded update. A progressed job is never silently
    overwritten.
*   **Fail closed on budget.** ``claim`` and ``start`` require a
    :class:`~forge.jobs.state.BudgetCheck`; an absent or failing check refuses
    (``BudgetRefused``) exactly as the pure machine does. The durable
    ``BudgetLedger`` remains deferred -- this store persists the per-job budget
    *ceiling*, but does not meter cross-process spend. No caller-supplied
    authorization is invented by the store.
*   **Refusals are audited, not swallowed.** A refused transition records a
    ``job_history`` row with ``from_state == to_state`` and ``outcome`` set to
    the exception class name, then re-raises -- the caller is never handed an
    unchanged job as if nothing happened, and the attempt is visible in the
    audit trail.
*   **No general exactly-once claim.** Atomic ``claim`` and durable dedupe make
    *claim ownership* and *duplicate-event recognition* safe across processes.
    They do **not** make external side effects exactly-once: recording a key
    and committing an outcome must still be one transaction at the call site
    (the later intake phase), which this store does not attempt to promise.

Concurrency model: every write opens a ``BEGIN IMMEDIATE`` transaction (the
writer lock), so read-then-write is serialised across connections and processes
against a shared file. The version-guarded ``UPDATE`` is a second, correctness
defence that raises :class:`ConcurrencyConflict` if a row changed underneath --
defence in depth, not the primary mechanism.

This module is stdlib-only (``sqlite3``), matching the zero-runtime-dependency
rule in ``pyproject.toml``. It imports no subprocess, network, or credential
module, so the existing ``forge/jobs`` structural import-scan in
``tests/test_job_state.py`` continues to pass over it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .idempotency import DedupeStore
from .state import (
    BudgetCheck,
    Job,
    JobEvent,
    JobState,
    JobStore,
    JobTransitionError,
    transition as _transition,
)

__all__ = [
    "SCHEMA_VERSION",
    "JobRecord",
    "JobStoreError",
    "JobNotFound",
    "JobAlreadyExists",
    "ConcurrencyConflict",
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
)

#: The current schema version. Kept equal to ``MIGRATIONS[-1][0]``; a change to
#: the migration list that forgets to move the tail is a bug.
SCHEMA_VERSION: int = MIGRATIONS[-1][0]

#: Default SQLite busy timeout (milliseconds) for concurrent openers.
DEFAULT_BUSY_TIMEOUT_MS: int = 5000

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
    ``created_at`` / ``updated_at`` / ``version`` are the persistence metadata
    the frozen, clock-free ``Job`` value deliberately does not carry.
    """

    job: Job
    created_at: float
    updated_at: float
    version: int


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


class MigrationError(JobStoreError):
    """A schema migration failed; the database was left at its prior version."""


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

    Every mutating method first runs migrations, so opening a store against a
    fresh or an older file brings it to :data:`SCHEMA_VERSION`.
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
            "created_at, updated_at, version FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return JobRecord(
            job=_row_to_job(row),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version=row["version"],
        )

    # -- intake ------------------------------------------------------------

    def save(self, job: Job) -> Job:
        """Register a new job (intake). Refuses an already-stored id.

        This is the only way a job enters the store. It persists the validated
        value, records a ``register`` history entry, and returns the job
        unchanged. State changes afterwards must go through ``transition``.
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
            self._conn.execute(
                "INSERT INTO job_history (job_id, event, from_state, to_state, "
                "owner, outcome, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, "register", None, job.state.value, job.owner, "ok", now),
            )
            self._conn.execute("COMMIT")
        except Exception:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        return job

    # -- mutation ----------------------------------------------------------

    def claim(
        self, job_id: str, owner: str, budget_check: BudgetCheck | None = None
    ) -> Job:
        """Atomically claim ``job_id`` for ``owner``.

        ``budget_check`` failures close exactly as the pure machine does: an
        absent authorization refuses before paid execution. A job that is not
        ``ready`` (already claimed, running, or terminal) is refused.
        """
        return self.transition(job_id, JobEvent.CLAIM, owner=owner, budget_check=budget_check)

    def transition(
        self,
        job_id: str,
        event: JobEvent,
        *,
        owner: str | None = None,
        budget_check: BudgetCheck | None = None,
    ) -> Job:
        """Apply ``event`` to the stored job inside one transaction.

        Loads the row, runs the pure :func:`~forge.jobs.state.transition`, then
        writes the successor and its history entry atomically. Ownership and
        budget refusals raised by the pure machine are recorded to history
        (with ``from_state == to_state`` and the refusal as ``outcome``) and
        re-raised; nothing about the stored job changes.
        """
        if not isinstance(event, JobEvent):
            raise TypeError(f"event must be a JobEvent, got {event!r}")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT id, ticket_ref, repo, state, allowed_tools, budget, "
                "owner, version FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            job = _row_to_job(row)
            version = row["version"]

            try:
                successor = _transition(
                    job,
                    event,
                    owner=owner,
                    budget_check=budget_check,
                )
            except JobTransitionError as exc:
                self._conn.execute(
                    "INSERT INTO job_history (job_id, event, from_state, "
                    "to_state, owner, outcome, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        event.value,
                        job.state.value,
                        job.state.value,
                        owner,
                        type(exc).__name__,
                        self._clock(),
                    ),
                )
                self._conn.execute("COMMIT")
                raise

            now = self._clock()
            updated = self._conn.execute(
                "UPDATE jobs SET state = ?, owner = ?, updated_at = ?, "
                "version = version + 1 WHERE id = ? AND version = ?",
                (successor.state.value, successor.owner, now, job_id, version),
            )
            if updated.rowcount != 1:
                raise ConcurrencyConflict(
                    f"job {job_id!r} changed between read and write"
                )
            self._conn.execute(
                "INSERT INTO job_history (job_id, event, from_state, to_state, "
                "owner, outcome, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    event.value,
                    job.state.value,
                    successor.state.value,
                    successor.owner,
                    "ok",
                    now,
                ),
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