"""Jira synchronization controller.

This module orchestrates fetching issues from Jira and storing them in the cache.
It handles errors gracefully and ensures atomic synchronization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from .client import JiraClient, JiraIssue
from .config import load_jira_config
from .store import JiraCache

logger = logging.getLogger(__name__)


class SyncResult(NamedTuple):
    """Result of a synchronization attempt."""

    instance_name: str
    success: bool
    issues_fetched: int
    error_message: str | None
    duration_seconds: float


class JiraSync:
    """Jira synchronization controller."""

    def __init__(
        self,
        workspace_root: Path,
        cache: JiraCache | None = None,
    ):
        """Initialize sync controller.

        Args:
            workspace_root: Workspace root path
            cache: Optional cache instance (created if None)
        """
        self.workspace_root = workspace_root
        self.cache = cache or JiraCache(workspace_root)

        # Load configuration
        self.configs = load_jira_config(workspace_root)

        # Default status mapping (configurable per instance)
        self.default_status_mapping = {
            "To Do": "orders_received",
            "In Progress": "at_the_forge",
            "Done": "finished_goods",
            "Review": "ready_for_inspection",
            "Testing": "in_the_quench",
        }

    def sync_instance(
        self,
        instance_name: str,
        force: bool = False,
        max_issues: int = 1000,
    ) -> SyncResult:
        """Synchronize issues for a specific Jira instance.

        Args:
            instance_name: Name of the Jira instance to sync
            force: If True, sync even if recently synced
            max_issues: Maximum number of issues to fetch

        Returns:
            SyncResult with success status and metadata
        """
        if instance_name not in self.configs:
            return SyncResult(
                instance_name=instance_name,
                success=False,
                issues_fetched=0,
                error_message=f"Jira instance '{instance_name}' not configured",
                duration_seconds=0,
            )

        config = self.configs[instance_name]

        # Check if we should skip (not forced and recently synced)
        if not force:
            metadata = self.cache.get_sync_metadata(instance_name)
            if metadata and metadata.last_success:
                # Skip if synced successfully in the last hour
                time_since_sync = datetime.now() - metadata.last_success
                if time_since_sync < timedelta(hours=1):
                    logger.info(
                        f"Skipping sync for '{instance_name}': "
                        f"last successful sync was {time_since_sync} ago"
                    )
                    return SyncResult(
                        instance_name=instance_name,
                        success=True,
                        issues_fetched=metadata.total_issues,
                        error_message=None,
                        duration_seconds=0,
                    )

        start_time = datetime.now()

        try:
            # Create Jira client
            client = JiraClient(config)

            # Test connection
            if not client.test_connection():
                error_msg = f"Failed to connect to Jira instance '{instance_name}'"
                self.cache.store_sync(instance_name, [], error_msg)
                return SyncResult(
                    instance_name=instance_name,
                    success=False,
                    issues_fetched=0,
                    error_message=error_msg,
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                )

            # Fetch issues
            logger.info(f"Fetching issues from Jira instance '{instance_name}'")

            # Use instance-specific projects or all projects
            projects = config.projects
            if not projects:
                # If no projects specified, we need to fetch all
                # This is less efficient but works
                projects_query = ""
            else:
                # Build project filter
                projects_query = f"project in ({', '.join(projects)})"

            # Build JQL
            jql_parts = []
            if projects_query:
                jql_parts.append(projects_query)

            # Add status filter if configured
            statuses = list(config.status_mapping.keys())
            if statuses:
                status_list = ", ".join(f'"{s}"' for s in statuses)
                jql_parts.append(f"status in ({status_list})")

            # ORDER BY is a clause, not a condition: joining it with " AND "
            # produced '... AND ORDER BY updated DESC', invalid JQL that the
            # endpoint rejects with a 400, which the broad except below would
            # have reported as a failed sync. Same rule as JiraClient.
            conditions = " AND ".join(jql_parts)
            if conditions:
                jql = f"{conditions} ORDER BY updated DESC"
            else:
                jql = "ORDER BY updated DESC"

            # Fetch issues
            issues = []
            for issue in client.search_issues(jql, max_results=max_issues):
                issues.append(issue)
                if len(issues) >= max_issues:
                    logger.warning(
                        f"Reached maximum issues limit ({max_issues}) "
                        f"for instance '{instance_name}'"
                    )
                    break

            # Store in cache
            self.cache.store_sync(instance_name, issues)

            duration = (datetime.now() - start_time).total_seconds()
            logger.info(
                f"Successfully synced {len(issues)} issues "
                f"from '{instance_name}' in {duration:.1f}s"
            )

            return SyncResult(
                instance_name=instance_name,
                success=True,
                issues_fetched=len(issues),
                error_message=None,
                duration_seconds=duration,
            )

        except Exception as exc:
            # Log error but don't raise - store failure in cache
            error_msg = f"Sync failed: {exc}"
            logger.error(f"Sync failed for '{instance_name}': {exc}")

            # Store failure in cache (preserving any existing successful data)
            self.cache.store_sync(instance_name, [], error_msg)

            duration = (datetime.now() - start_time).total_seconds()
            return SyncResult(
                instance_name=instance_name,
                success=False,
                issues_fetched=0,
                error_message=error_msg,
                duration_seconds=duration,
            )

    def sync_all(self, force: bool = False, max_issues_per_instance: int = 1000) -> list[SyncResult]:
        """Synchronize all configured Jira instances.

        Args:
            force: If True, sync even if recently synced
            max_issues_per_instance: Maximum issues per instance

        Returns:
            List of SyncResult for each instance
        """
        results = []

        for instance_name in self.configs.keys():
            result = self.sync_instance(
                instance_name,
                force=force,
                max_issues=max_issues_per_instance,
            )
            results.append(result)

        return results

    def get_sync_status(self) -> dict[str, dict[str, Any]]:
        """Get synchronization status for all instances.

        Returns:
            Dictionary with instance names as keys and status info as values
        """
        status = {}

        for instance_name, config in self.configs.items():
            metadata = self.cache.get_sync_metadata(instance_name)

            status[instance_name] = {
                "configured": True,
                "has_credentials": config.has_credentials,
                "projects": config.projects,
                "last_success": metadata.last_success.isoformat() if metadata and metadata.last_success else None,
                "last_attempt": metadata.last_attempt.isoformat() if metadata and metadata.last_attempt else None,
                "error_message": metadata.error_message if metadata else None,
                "total_issues": metadata.total_issues if metadata else 0,
                "status_mapping": config.status_mapping,
            }

        return status

    def get_issues(
        self,
        instance_name: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        updated_since: datetime | None = None,
    ) -> list[JiraIssue]:
        """Get issues from cache with optional filtering.

        This is a convenience method that delegates to the cache.

        Args:
            instance_name: Filter by Jira instance name
            project: Filter by project key
            status: Filter by status name
            limit: Maximum number of issues to return
            updated_since: Only return issues updated after this time

        Returns:
            List of JiraIssue objects
        """
        return self.cache.get_issues(
            instance_name=instance_name,
            project=project,
            status=status,
            limit=limit,
            updated_since=updated_since,
        )

    def map_jira_status_to_forge(self, jira_status: str, instance_name: str) -> str:
        """Map Jira status to Forge status.

        Args:
            jira_status: Raw Jira status name
            instance_name: Jira instance name

        Returns:
            Forge status name
        """
        config = self.configs.get(instance_name)
        if config and jira_status in config.status_mapping:
            return config.status_mapping[jira_status]

        # Fall back to default mapping
        if jira_status in self.default_status_mapping:
            return self.default_status_mapping[jira_status]

        # Default fallback
        return "on_the_rack"


def sync_workspace(
    workspace_root: Path,
    instance_name: str | None = None,
    force: bool = False,
    max_issues: int = 1000,
) -> SyncResult | list[SyncResult]:
    """Convenience function to sync Jira issues for a workspace.

    Args:
        workspace_root: Workspace root path
        instance_name: Specific instance to sync (None for all)
        force: If True, sync even if recently synced
        max_issues: Maximum issues to fetch per instance

    Returns:
        Single SyncResult if instance_name specified, else list of SyncResult
    """
    sync = JiraSync(workspace_root)

    if instance_name:
        return sync.sync_instance(instance_name, force=force, max_issues=max_issues)
    else:
        return sync.sync_all(force=force, max_issues_per_instance=max_issues)
