"""Jira integration for Forge Order Board.

This package provides read-only synchronization of Jira issues to a local cache.
"""

from __future__ import annotations

from .client import JiraClient, JiraIssue
from .config import JiraConfig, get_default_config, load_jira_config
from .store import JiraCache, SyncMetadata
from .sync import JiraSync, SyncResult, sync_workspace

__all__ = [
    "JiraClient",
    "JiraIssue",
    "JiraConfig",
    "get_default_config",
    "load_jira_config",
    "JiraCache",
    "SyncMetadata",
    "JiraSync",
    "SyncResult",
    "sync_workspace",
]
