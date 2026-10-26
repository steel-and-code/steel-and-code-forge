"""SQLite cache for Jira issues and synchronization metadata.

This module provides atomic synchronization and persistence for Jira issues.
Incomplete or failed API fetches never replace successfully cached data.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, NamedTuple

from .client import JiraIssue


class SyncMetadata(NamedTuple):
    """Synchronization metadata for a Jira instance."""

    instance_name: str
    last_success: datetime | None
    last_attempt: datetime | None
    error_message: str | None
    total_issues: int
    cache_version: int


class JiraCacheError(Exception):
    """Base exception for Jira cache errors."""
    pass


class JiraCache:
    """SQLite cache for Jira issues with atomic per-instance synchronization.

    The cache stores:
    1. Jira issues (normalized format, scoped to their Jira instance)
    2. Synchronization metadata per instance
    3. Cache bookkeeping (schema version, timestamps)

    Atomicity comes from SQLite transactions rather than from swapping
    database files:
    - A successful sync deletes and re-inserts the issues of *one* instance
      inside a single transaction, so other instances' issues are untouched.
    - A failed sync writes metadata only; cached issues, ``last_success`` and
      the successful issue count stay intact.
    - If any step (including validation) fails the transaction rolls back and
      the cache keeps its last successfully synchronized state.

    Threading: the cache holds one persistent sqlite3 connection, which Python
    binds to the thread that opened it. Use a JiraCache from a single thread,
    or construct one per thread; cross-thread use of the same object raises
    sqlite3.ProgrammingError. Reads and writes are safe against a *separate*
    process, which is why writes take BEGIN IMMEDIATE and connections set a
    busy timeout.
    """

    SCHEMA_VERSION = 1

    #: Tables that must exist for the cache to be usable.
    _REQUIRED_TABLES = ("issues", "sync_metadata", "cache_info")

    def __init__(self, workspace_root: Path):
        """Initialize cache for a workspace.

        The database file and its schema are created eagerly, so every table
        is available as soon as the cache object exists.

        Args:
            workspace_root: Workspace root path
        """
        self.workspace_root = Path(workspace_root)
        self.cache_dir = self.workspace_root / ".forge"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "jira_cache.db"
        self._conn: sqlite3.Connection | None = None

        # Create the database file and initialize the schema.
        self._ensure_schema()

    # ------------------------------------------------------------------ #
    # Connection management
    # ------------------------------------------------------------------ #

    def _new_connection(self, path: Path) -> sqlite3.Connection:
        """Open a configured connection to the database at ``path``."""
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        # Transactions are opened explicitly by _transaction(), so put the
        # driver in autocommit mode and let the cache own BEGIN/COMMIT.
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Return the cache's persistent connection, opening it if needed."""
        if self._conn is None:
            self._conn = self._new_connection(self.db_path)
        return self._conn

    def _ensure_schema(self) -> sqlite3.Connection:
        """Return the persistent connection with the schema guaranteed."""
        conn = self._get_connection()
        try:
            self._init_schema(conn)
        except sqlite3.Error:
            self.close()
            raise
        return conn

    @contextmanager
    def _connection(
        self, path: Path | None = None
    ) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for database connections.

        Without ``path`` the cache's persistent connection is yielded and left
        open for later calls. With ``path`` a separate short-lived connection
        to another database file is opened (schema initialized) and closed
        again.
        """
        if path is not None and Path(path) != self.db_path:
            conn = self._new_connection(Path(path))
            try:
                self._init_schema(conn)
                yield conn
            finally:
                conn.close()
            return

        yield self._ensure_schema()

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Generator[None, None, None]:
        """Run a block inside one explicit transaction (commit or rollback)."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def close(self) -> None:
        """Close the persistent connection. The database file is kept."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Initialize database schema.

        Idempotent: safe to call on every connection, repeatedly.
        """
        # Issues table. A Jira issue key is unique only within one instance,
        # so the primary key is (instance_name, key): one instance's rows can
        # never be overwritten or removed by another instance's data.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                key TEXT NOT NULL,
                id TEXT NOT NULL,
                summary TEXT NOT NULL,
                description TEXT,
                project TEXT NOT NULL,
                issuetype TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT,
                assignee TEXT,
                reporter TEXT,
                created TEXT NOT NULL,
                updated TEXT NOT NULL,
                url TEXT NOT NULL,
                labels TEXT NOT NULL,
                instance_name TEXT NOT NULL,
                sync_time TEXT NOT NULL,
                PRIMARY KEY (instance_name, key)
            )
        """)

        # Create indexes
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_key ON issues(key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_instance ON issues(instance_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_updated ON issues(updated)")

        # Sync metadata table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_metadata (
                instance_name TEXT PRIMARY KEY,
                last_success TEXT,
                last_attempt TEXT,
                error_message TEXT,
                total_issues INTEGER DEFAULT 0,
                cache_version INTEGER DEFAULT 1
            )
        """)

        # Cache info table (singleton)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache_info (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL,
                created TEXT NOT NULL,
                last_modified TEXT NOT NULL
            )
        """)

        # Initialize cache info if needed
        now = datetime.now().isoformat()
        conn.execute("""
            INSERT OR IGNORE INTO cache_info (id, schema_version, created, last_modified)
            VALUES (1, ?, ?, ?)
        """, (self.SCHEMA_VERSION, now, now))

    def _update_cache_info(self, conn: sqlite3.Connection, now: datetime) -> None:
        """Update the last modified timestamp (inside the caller's transaction)."""
        conn.execute("""
            UPDATE cache_info
            SET last_modified = ?
            WHERE id = 1
        """, (now.isoformat(),))

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    def store_sync(
        self,
        instance_name: str,
        issues: list[JiraIssue],
        error_message: str | None = None,
    ) -> None:
        """Atomically store synchronized issues for a Jira instance.

        A successful sync replaces the cached issues of ``instance_name`` and
        only those: issues cached for other Jira instances are never deleted.

        A failed sync (``error_message`` set) updates metadata only. Issues
        from the last successful sync, ``last_success`` and the successful
        issue count are preserved.

        Args:
            instance_name: Name of the Jira instance
            issues: List of issues to store
            error_message: Error message if sync failed, None if successful
        """
        now = datetime.now()
        conn = self._ensure_schema()

        with self._transaction(conn):
            if error_message is None:
                # Successful sync: replace this instance's issues only.
                conn.execute(
                    "DELETE FROM issues WHERE instance_name = ?",
                    (instance_name,)
                )

                for issue in issues:
                    conn.execute("""
                        INSERT INTO issues (
                            key, id, summary, description, project, issuetype,
                            status, priority, assignee, reporter, created,
                            updated, url, labels, instance_name, sync_time
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        issue.key,
                        issue.id,
                        issue.summary,
                        issue.description,
                        issue.project,
                        issue.issuetype,
                        issue.status,
                        issue.priority,
                        issue.assignee,
                        issue.reporter,
                        issue.created.isoformat(),
                        issue.updated.isoformat(),
                        issue.url,
                        json.dumps(issue.labels),
                        instance_name,
                        now.isoformat(),
                    ))

                self._validate_issue_count(conn, instance_name, len(issues))

                conn.execute("""
                    INSERT OR REPLACE INTO sync_metadata (
                        instance_name, last_success, last_attempt,
                        error_message, total_issues, cache_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    instance_name,
                    now.isoformat(),
                    now.isoformat(),
                    None,
                    len(issues),
                    self.SCHEMA_VERSION,
                ))
            else:
                # Failed sync: keep the cached issues and the last successful
                # sync metadata; record the attempt and the error only.
                row = conn.execute(
                    "SELECT last_success FROM sync_metadata WHERE instance_name = ?",
                    (instance_name,)
                ).fetchone()
                last_success = row["last_success"] if row else None

                total_issues = self._count_instance_issues(conn, instance_name)

                conn.execute("""
                    INSERT OR REPLACE INTO sync_metadata (
                        instance_name, last_success, last_attempt,
                        error_message, total_issues, cache_version
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    instance_name,
                    last_success,
                    now.isoformat(),
                    error_message,
                    total_issues,
                    self.SCHEMA_VERSION,
                ))

            self._update_cache_info(conn, now)

            # Validate before committing; a failure rolls the transaction back
            # and the previous cache state stays visible.
            self._validate_cache(conn)

    @staticmethod
    def _count_instance_issues(conn: sqlite3.Connection, instance_name: str) -> int:
        """Number of issues currently cached for one instance."""
        cur = conn.execute(
            "SELECT COUNT(*) AS count FROM issues WHERE instance_name = ?",
            (instance_name,)
        )
        return int(cur.fetchone()["count"])

    @classmethod
    def _validate_issue_count(
        cls,
        conn: sqlite3.Connection,
        instance_name: str,
        expected: int,
    ) -> None:
        """Check that the issues just written are all present."""
        actual = cls._count_instance_issues(conn, instance_name)
        if actual != expected:
            raise JiraCacheError(
                f"Cache validation failed for '{instance_name}': "
                f"expected {expected} issues, found {actual}"
            )

    def _validate_cache(self, conn: sqlite3.Connection | None = None) -> None:
        """Validate cache schema and integrity.

        Args:
            conn: Connection to validate. ``None`` validates the live cache.
        """
        if conn is None:
            with self._connection() as live_conn:
                self._validate_cache(live_conn)
            return

        # All expected tables exist.
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row["name"] for row in cur.fetchall()}
        missing = [table for table in self._REQUIRED_TABLES if table not in tables]
        if missing:
            raise JiraCacheError(f"Cache tables missing: {', '.join(missing)}")

        # Check schema version.
        cur = conn.execute("SELECT schema_version FROM cache_info WHERE id = 1")
        row = cur.fetchone()
        if not row:
            raise JiraCacheError("Cache info not found")

        schema_version = row["schema_version"]
        if schema_version != self.SCHEMA_VERSION:
            raise JiraCacheError(
                f"Cache schema mismatch: expected {self.SCHEMA_VERSION}, got {schema_version}"
            )

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_issue(row: sqlite3.Row) -> JiraIssue:
        """Convert an ``issues`` row to a JiraIssue."""
        return JiraIssue(
            key=row["key"],
            id=row["id"],
            summary=row["summary"],
            description=row["description"],
            project=row["project"],
            issuetype=row["issuetype"],
            status=row["status"],
            priority=row["priority"],
            assignee=row["assignee"],
            reporter=row["reporter"],
            created=datetime.fromisoformat(row["created"]),
            updated=datetime.fromisoformat(row["updated"]),
            url=row["url"],
            labels=json.loads(row["labels"]),
        )

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> SyncMetadata:
        """Convert a ``sync_metadata`` row to SyncMetadata."""
        last_success = None
        if row["last_success"]:
            last_success = datetime.fromisoformat(row["last_success"])

        last_attempt = None
        if row["last_attempt"]:
            last_attempt = datetime.fromisoformat(row["last_attempt"])

        return SyncMetadata(
            instance_name=row["instance_name"],
            last_success=last_success,
            last_attempt=last_attempt,
            error_message=row["error_message"],
            total_issues=row["total_issues"],
            cache_version=row["cache_version"],
        )

    def get_issues(
        self,
        instance_name: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        updated_since: datetime | None = None,
    ) -> list[JiraIssue]:
        """Get issues from cache with optional filtering.

        Args:
            instance_name: Filter by Jira instance name
            project: Filter by project key
            status: Filter by status name
            limit: Maximum number of issues to return
            updated_since: Only return issues updated after this time

        Returns:
            List of JiraIssue objects
        """
        with self._connection() as conn:
            # Build query
            query_parts = ["SELECT * FROM issues"]
            params: list[Any] = []

            where_parts = []
            if instance_name is not None:
                where_parts.append("instance_name = ?")
                params.append(instance_name)

            if project:
                where_parts.append("project = ?")
                params.append(project)

            if status:
                where_parts.append("status = ?")
                params.append(status)

            if updated_since:
                where_parts.append("updated >= ?")
                params.append(updated_since.isoformat())

            if where_parts:
                query_parts.append("WHERE " + " AND ".join(where_parts))

            query_parts.append("ORDER BY updated DESC, key ASC")

            if limit is not None:
                query_parts.append("LIMIT ?")
                params.append(int(limit))

            query = " ".join(query_parts)

            cur = conn.execute(query, params)
            rows = cur.fetchall()

            return [self._row_to_issue(row) for row in rows]

    def get_sync_metadata(self, instance_name: str) -> SyncMetadata | None:
        """Get synchronization metadata for a Jira instance."""
        with self._connection() as conn:
            cur = conn.execute(
                "SELECT * FROM sync_metadata WHERE instance_name = ?",
                (instance_name,)
            )
            row = cur.fetchone()

            if not row:
                return None

            return self._row_to_metadata(row)

    def get_all_sync_metadata(self) -> dict[str, SyncMetadata]:
        """Get synchronization metadata for all instances."""
        with self._connection() as conn:
            cur = conn.execute("SELECT * FROM sync_metadata ORDER BY instance_name")

            return {
                row["instance_name"]: self._row_to_metadata(row)
                for row in cur.fetchall()
            }

    # ------------------------------------------------------------------ #
    # Clearing
    # ------------------------------------------------------------------ #

    def clear_instance(self, instance_name: str) -> None:
        """Clear all data for one Jira instance (other instances are kept)."""
        now = datetime.now()
        conn = self._ensure_schema()

        with self._transaction(conn):
            conn.execute(
                "DELETE FROM issues WHERE instance_name = ?",
                (instance_name,)
            )
            conn.execute(
                "DELETE FROM sync_metadata WHERE instance_name = ?",
                (instance_name,)
            )
            self._update_cache_info(conn, now)

    def clear_all(self) -> None:
        """Clear all cached data by deleting the database file."""
        self.close()
        try:
            self.db_path.unlink(missing_ok=True)
        except OSError as exc:
            raise JiraCacheError(f"Failed to clear cache: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Statistics
    # ------------------------------------------------------------------ #

    def get_cache_stats(self) -> dict[str, Any]:
        """Get cache statistics."""
        with self._connection() as conn:
            # Total issues
            cur = conn.execute("SELECT COUNT(*) as count FROM issues")
            total_issues = cur.fetchone()["count"]

            # Issues per instance
            cur = conn.execute("""
                SELECT instance_name, COUNT(*) as count
                FROM issues
                GROUP BY instance_name
            """)
            issues_by_instance = {row["instance_name"]: row["count"] for row in cur.fetchall()}

            # Issues per project
            cur = conn.execute("""
                SELECT project, COUNT(*) as count
                FROM issues
                GROUP BY project
                ORDER BY count DESC
                LIMIT 10
            """)
            issues_by_project = {row["project"]: row["count"] for row in cur.fetchall()}

            # Last sync times
            cur = conn.execute("""
                SELECT instance_name, last_success, last_attempt
                FROM sync_metadata
            """)
            sync_times = {}
            for row in cur.fetchall():
                sync_times[row["instance_name"]] = {
                    "last_success": row["last_success"],
                    "last_attempt": row["last_attempt"],
                }

            return {
                "total_issues": total_issues,
                "issues_by_instance": issues_by_instance,
                "top_projects": issues_by_project,
                "sync_times": sync_times,
            }
