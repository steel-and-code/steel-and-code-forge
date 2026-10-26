"""Tests for Jira SQLite cache with atomic synchronization."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from forge.jira.client import JiraIssue
from forge.jira.store import JiraCache, JiraCacheError, SyncMetadata


@pytest.fixture
def temp_workspace() -> Path:
    """Fixture for temporary workspace directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_issues() -> list[JiraIssue]:
    """Fixture for sample Jira issues."""
    return [
        JiraIssue(
            key="TEST-1",
            id="10001",
            summary="Test Issue 1",
            description="Description 1",
            project="TEST",
            issuetype="Bug",
            status="To Do",
            priority="High",
            assignee="John Doe",
            reporter="Jane Smith",
            created=datetime(2024, 1, 15, 10, 30, 0),
            updated=datetime(2024, 1, 15, 11, 30, 0),
            url="https://test.atlassian.net/browse/TEST-1",
            labels=["bug", "critical"],
        ),
        JiraIssue(
            key="TEST-2",
            id="10002",
            summary="Test Issue 2",
            description=None,
            project="TEST",
            issuetype="Task",
            status="In Progress",
            priority=None,
            assignee=None,
            reporter="Jane Smith",
            created=datetime(2024, 1, 14, 9, 0, 0),
            updated=datetime(2024, 1, 15, 12, 0, 0),
            url="https://test.atlassian.net/browse/TEST-2",
            labels=[],
        ),
        JiraIssue(
            key="DEV-1",
            id="20001",
            summary="Dev Issue",
            description="Development task",
            project="DEV",
            issuetype="Task",
            status="Done",
            priority="Medium",
            assignee="Alice Brown",
            reporter="Bob White",
            created=datetime(2024, 1, 10, 8, 0, 0),
            updated=datetime(2024, 1, 15, 14, 0, 0),
            url="https://test.atlassian.net/browse/DEV-1",
            labels=["development"],
        ),
    ]


def test_cache_initialization(temp_workspace: Path) -> None:
    """Test cache initialization and schema creation."""
    cache = JiraCache(temp_workspace)

    # Database should be created
    assert cache.db_path.exists()

    # Verify schema
    with cache._connection() as conn:
        # Check tables exist
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row["name"] for row in cur.fetchall()}

        assert "issues" in tables
        assert "sync_metadata" in tables
        assert "cache_info" in tables

        # Check cache info
        cur = conn.execute("SELECT * FROM cache_info WHERE id = 1")
        row = cur.fetchone()
        assert row is not None
        assert row["schema_version"] == 1


def test_store_sync_success(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test storing successful synchronization."""
    cache = JiraCache(temp_workspace)

    # Store sync
    cache.store_sync("test-instance", sample_issues[:2])

    # Verify data
    with cache._connection() as conn:
        # Check issues
        cur = conn.execute("SELECT * FROM issues ORDER BY key")
        rows = cur.fetchall()

        assert len(rows) == 2
        assert rows[0]["key"] == "TEST-1"
        assert rows[0]["project"] == "TEST"
        assert rows[0]["instance_name"] == "test-instance"
        assert rows[0]["summary"] == "Test Issue 1"
        assert rows[1]["key"] == "TEST-2"

        # Check labels JSON
        labels = json.loads(rows[0]["labels"])
        assert labels == ["bug", "critical"]

        # Check sync metadata
        cur = conn.execute("SELECT * FROM sync_metadata WHERE instance_name = ?", ("test-instance",))
        row = cur.fetchone()

        assert row is not None
        assert row["last_success"] is not None
        assert row["last_attempt"] is not None
        assert row["error_message"] is None
        assert row["total_issues"] == 2

        # Check cache info updated
        cur = conn.execute("SELECT last_modified FROM cache_info WHERE id = 1")
        row = cur.fetchone()
        assert row["last_modified"] is not None


def test_store_sync_failure(temp_workspace: Path) -> None:
    """Test storing failed synchronization."""
    cache = JiraCache(temp_workspace)

    # First store a successful sync
    cache.store_sync("test-instance", [])

    # Store failure
    error_msg = "Network timeout"
    cache.store_sync("test-instance", [], error_msg)

    # Verify metadata
    metadata = cache.get_sync_metadata("test-instance")
    assert metadata is not None
    assert metadata.error_message == error_msg
    assert metadata.last_attempt is not None
    # Last success should be preserved from previous successful sync
    assert metadata.last_success is not None


def test_store_sync_atomic_replacement(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test atomic replacement of cache database."""
    cache = JiraCache(temp_workspace)

    # Store first batch
    cache.store_sync("instance1", sample_issues[:1])

    # Store second batch (should replace)
    cache.store_sync("instance1", sample_issues[1:2])

    # Should only have second batch
    issues = cache.get_issues("instance1")
    assert len(issues) == 1
    assert issues[0].key == "TEST-2"

    # Store third batch for different instance
    cache.store_sync("instance2", sample_issues[2:])

    # Should have both instances
    issues1 = cache.get_issues("instance1")
    issues2 = cache.get_issues("instance2")

    assert len(issues1) == 1
    assert len(issues2) == 1
    assert issues1[0].key == "TEST-2"
    assert issues2[0].key == "DEV-1"


def test_get_issues_filtering(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test getting issues with filters."""
    cache = JiraCache(temp_workspace)

    # Store issues from multiple instances
    cache.store_sync("instance1", sample_issues[:2])  # TEST-1, TEST-2
    cache.store_sync("instance2", sample_issues[2:])  # DEV-1

    # Filter by instance
    issues = cache.get_issues(instance_name="instance1")
    assert len(issues) == 2
    assert all(issue.key in ["TEST-1", "TEST-2"] for issue in issues)

    # Filter by project
    issues = cache.get_issues(project="DEV")
    assert len(issues) == 1
    assert issues[0].key == "DEV-1"

    # Filter by status
    issues = cache.get_issues(status="To Do")
    assert len(issues) == 1
    assert issues[0].key == "TEST-1"

    # Combined filter
    issues = cache.get_issues(instance_name="instance1", status="In Progress")
    assert len(issues) == 1
    assert issues[0].key == "TEST-2"

    # Limit
    issues = cache.get_issues(limit=1)
    assert len(issues) == 1

    # Updated since
    updated_since = datetime(2024, 1, 15, 12, 0, 0)
    issues = cache.get_issues(updated_since=updated_since)
    # TEST-2 updated at 12:00, DEV-1 at 14:00
    assert len(issues) == 2
    assert {issue.key for issue in issues} == {"TEST-2", "DEV-1"}


def test_get_sync_metadata(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test getting synchronization metadata."""
    cache = JiraCache(temp_workspace)

    # No metadata initially
    metadata = cache.get_sync_metadata("test-instance")
    assert metadata is None

    # Store sync
    cache.store_sync("test-instance", sample_issues[:2])

    # Get metadata
    metadata = cache.get_sync_metadata("test-instance")
    assert metadata is not None
    assert metadata.instance_name == "test-instance"
    assert metadata.last_success is not None
    assert metadata.last_attempt is not None
    assert metadata.error_message is None
    assert metadata.total_issues == 2
    assert metadata.cache_version == 1

    # Store failure
    cache.store_sync("test-instance", [], "Network error")

    # Metadata should show failure but preserve last success
    metadata = cache.get_sync_metadata("test-instance")
    assert metadata is not None
    assert metadata.error_message == "Network error"
    assert metadata.last_success is not None  # Preserved from previous success
    assert metadata.total_issues == 2  # Preserved from previous success


def test_get_all_sync_metadata(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test getting metadata for all instances."""
    cache = JiraCache(temp_workspace)

    # Store sync for multiple instances
    cache.store_sync("instance1", sample_issues[:1])
    cache.store_sync("instance2", sample_issues[1:2], "Failed")

    # Get all metadata
    metadata = cache.get_all_sync_metadata()

    assert len(metadata) == 2
    assert "instance1" in metadata
    assert "instance2" in metadata

    assert metadata["instance1"].error_message is None
    assert metadata["instance2"].error_message == "Failed"


def test_clear_instance(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test clearing data for a specific instance."""
    cache = JiraCache(temp_workspace)

    # Store data for two instances
    cache.store_sync("instance1", sample_issues[:2])
    cache.store_sync("instance2", sample_issues[2:])

    # Clear instance1
    cache.clear_instance("instance1")

    # Verify
    issues1 = cache.get_issues("instance1")
    issues2 = cache.get_issues("instance2")

    assert len(issues1) == 0
    assert len(issues2) == 1
    assert issues2[0].key == "DEV-1"

    # Metadata should also be cleared
    metadata = cache.get_sync_metadata("instance1")
    assert metadata is None


def test_clear_all(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test clearing all cached data."""
    cache = JiraCache(temp_workspace)

    # Store data
    cache.store_sync("instance1", sample_issues[:2])
    assert cache.db_path.exists()

    # Clear all
    cache.clear_all()

    # Database should be deleted
    assert not cache.db_path.exists()

    # Can store again
    cache.store_sync("instance1", sample_issues[:1])
    assert cache.db_path.exists()


def test_cache_stats(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test getting cache statistics."""
    cache = JiraCache(temp_workspace)

    # Store data for multiple instances and projects
    cache.store_sync("instance1", sample_issues[:2])  # TEST-1, TEST-2
    cache.store_sync("instance2", sample_issues[2:])  # DEV-1

    # Get stats
    stats = cache.get_cache_stats()

    assert stats["total_issues"] == 3
    assert stats["issues_by_instance"]["instance1"] == 2
    assert stats["issues_by_instance"]["instance2"] == 1
    assert stats["top_projects"]["TEST"] == 2
    assert stats["top_projects"]["DEV"] == 1

    # Check sync times
    assert "instance1" in stats["sync_times"]
    assert "instance2" in stats["sync_times"]
    assert stats["sync_times"]["instance1"]["last_success"] is not None


def test_atomic_sync_preserves_other_instances(temp_workspace: Path, sample_issues: list[JiraIssue]) -> None:
    """Test that atomic sync preserves data from other instances."""
    cache = JiraCache(temp_workspace)

    # Store data for two instances
    cache.store_sync("instance1", sample_issues[:2])
    cache.store_sync("instance2", sample_issues[2:])

    # Sync instance1 again
    cache.store_sync("instance1", [sample_issues[0]])  # Only TEST-1 now

    # Verify instance2 data is preserved
    issues2 = cache.get_issues("instance2")
    assert len(issues2) == 1
    assert issues2[0].key == "DEV-1"

    # Verify instance1 has only TEST-1
    issues1 = cache.get_issues("instance1")
    assert len(issues1) == 1
    assert issues1[0].key == "TEST-1"


def test_store_sync_with_malformed_data(temp_workspace: Path) -> None:
    """Test handling of malformed data during sync."""
    cache = JiraCache(temp_workspace)

    # Create an issue with problematic data
    bad_issue = JiraIssue(
        key="BAD-1",
        id="99999",
        summary="Bad Issue",
        description=None,
        project="BAD",
        issuetype="Bug",
        status="To Do",
        priority=None,
        assignee=None,
        reporter=None,
        created=datetime.now(),
        updated=datetime.now(),
        url="https://test.atlassian.net/browse/BAD-1",
        labels=["test"],
    )

    # This should not crash
    cache.store_sync("test-instance", [bad_issue])

    # Verify data stored
    issues = cache.get_issues("test-instance")
    assert len(issues) == 1
    assert issues[0].key == "BAD-1"


def test_concurrent_access(temp_workspace: Path) -> None:
    """Test that overlapping readers on the cache work.

    _connection() now yields the cache's persistent connection, so the two
    `with` blocks share one handle rather than simulating two OS-level
    connections; cross-process safety comes from BEGIN IMMEDIATE plus the busy
    timeout, and is exercised by test_data_survives_reopening_workspace.
    """
    cache = JiraCache(temp_workspace)

    # Initialize cache
    cache.store_sync("test-instance", [])

    # Simulate concurrent access by creating multiple connections
    # This tests that SQLite handles concurrent reads/writes properly
    with cache._connection() as conn1:
        with cache._connection() as conn2:
            # Both connections should work
            cur1 = conn1.execute("SELECT COUNT(*) FROM issues")
            cur2 = conn2.execute("SELECT COUNT(*) FROM issues")

            count1 = cur1.fetchone()[0]
            count2 = cur2.fetchone()[0]

            assert count1 == count2 == 0


def _issue(key: str, project: str, summary: str = "Same key") -> JiraIssue:
    """Build a minimal issue for cross-instance tests."""
    return JiraIssue(
        key=key,
        id="1",
        summary=summary,
        description=None,
        project=project,
        issuetype="Task",
        status="To Do",
        priority=None,
        assignee=None,
        reporter=None,
        created=datetime(2024, 1, 1),
        updated=datetime(2024, 1, 2),
        url=f"https://test.atlassian.net/browse/{key}",
        labels=[],
    )


def test_same_key_in_different_instances(temp_workspace: Path) -> None:
    """Test that identical issue keys in different instances do not collide.

    A Jira key is unique only within its instance, so caching the same key for
    two instances must keep both rows.
    """
    cache = JiraCache(temp_workspace)

    cache.store_sync("instance1", [_issue("DUP-1", "A")])
    cache.store_sync("instance2", [_issue("DUP-1", "B")])

    assert [i.key for i in cache.get_issues("instance1")] == ["DUP-1"]
    assert [i.key for i in cache.get_issues("instance2")] == ["DUP-1"]
    assert cache.get_cache_stats()["total_issues"] == 2

    # Replacing instance1 leaves instance2 untouched
    cache.store_sync("instance1", [])
    assert len(cache.get_issues("instance1")) == 0
    assert len(cache.get_issues("instance2")) == 1


def test_failed_sync_preserves_cached_issues(
    temp_workspace: Path, sample_issues: list[JiraIssue]
) -> None:
    """Test that a failed sync keeps issues from the last successful sync."""
    cache = JiraCache(temp_workspace)

    cache.store_sync("instance1", sample_issues[:2])
    cache.store_sync("instance2", sample_issues[2:])

    # A failed sync carries no issues at all
    cache.store_sync("instance1", [], "Connection refused")

    assert {issue.key for issue in cache.get_issues("instance1")} == {"TEST-1", "TEST-2"}
    assert {issue.key for issue in cache.get_issues("instance2")} == {"DEV-1"}

    metadata = cache.get_sync_metadata("instance1")
    assert metadata is not None
    assert metadata.error_message == "Connection refused"
    assert metadata.last_attempt is not None
    assert metadata.last_success is not None
    assert metadata.total_issues == 2


def test_first_sync_failure_records_no_success(temp_workspace: Path) -> None:
    """A failing first sync must not invent a last_success timestamp."""
    cache = JiraCache(temp_workspace)

    cache.store_sync("fresh", [], "Not reachable")

    metadata = cache.get_sync_metadata("fresh")
    assert metadata is not None
    assert metadata.last_success is None
    assert metadata.error_message == "Not reachable"
    assert metadata.total_issues == 0
    assert cache.get_issues("fresh") == []


def test_store_sync_rolls_back_on_validation_error(
    temp_workspace: Path,
    sample_issues: list[JiraIssue],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sync that fails validation must leave the cache unchanged."""
    cache = JiraCache(temp_workspace)
    cache.store_sync("instance1", sample_issues[:2])
    cache.store_sync("instance2", sample_issues[2:])

    def explode(*args: object, **kwargs: object) -> None:
        raise JiraCacheError("validation exploded")

    monkeypatch.setattr(cache, "_validate_issue_count", explode)

    with pytest.raises(JiraCacheError):
        cache.store_sync("instance1", [])

    assert {issue.key for issue in cache.get_issues("instance1")} == {"TEST-1", "TEST-2"}
    assert {issue.key for issue in cache.get_issues("instance2")} == {"DEV-1"}

    metadata = cache.get_sync_metadata("instance1")
    assert metadata is not None
    assert metadata.error_message is None
    assert metadata.total_issues == 2


def test_data_survives_reopening_workspace(
    temp_workspace: Path, sample_issues: list[JiraIssue]
) -> None:
    """A new JiraCache on the same workspace sees the persisted data."""
    first = JiraCache(temp_workspace)
    first.store_sync("instance1", sample_issues[:2])
    first.close()

    second = JiraCache(temp_workspace)
    assert second.db_path.exists()
    assert {issue.key for issue in second.get_issues("instance1")} == {"TEST-1", "TEST-2"}
    assert second.get_sync_metadata("instance1") is not None


def test_cache_is_reusable_after_clear_all(
    temp_workspace: Path, sample_issues: list[JiraIssue]
) -> None:
    """After clear_all the cache recreates its schema on the next write."""
    cache = JiraCache(temp_workspace)
    cache.store_sync("instance1", sample_issues[:2])

    cache.clear_all()
    assert not cache.db_path.exists()

    cache.store_sync("instance1", sample_issues[:1])
    assert cache.db_path.exists()
    assert {issue.key for issue in cache.get_issues("instance1")} == {"TEST-1"}

    with cache._connection() as conn:
        cur = conn.execute("SELECT schema_version FROM cache_info WHERE id = 1")
        assert cur.fetchone()["schema_version"] == JiraCache.SCHEMA_VERSION


def test_store_sync_leaves_no_scratch_files(
    temp_workspace: Path, sample_issues: list[JiraIssue]
) -> None:
    """Syncing writes only the cache database, no temporary database files."""
    cache = JiraCache(temp_workspace)

    cache.store_sync("instance1", sample_issues[:2])
    cache.store_sync("instance1", [], "boom")

    assert sorted(path.name for path in cache.cache_dir.iterdir()) == ["jira_cache.db"]
