"""Jira REST API v3 client with authentication, pagination, and error handling.

This client only performs read operations and never modifies Jira data.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generator, Literal
from urllib.parse import quote

import urllib.request
import urllib.error

from .config import JiraConfig

# Jira API constants
JIRA_API_VERSION = "3"
#: The supported Jira Cloud search endpoint. ``POST /rest/api/3/search`` with
#: ``startAt`` offsets is deprecated and Atlassian has announced its removal;
#: ``search/jql`` paginates with an opaque ``nextPageToken`` instead.
JIRA_SEARCH_PATH = "search/jql"
#: The field set the normalizer actually reads. ``key`` and ``id`` are always
#: returned by the endpoint, so they are not requested.
DEFAULT_FIELDS = [
    "summary",
    "description",
    "project",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "labels",
]
DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds
MAX_PAGE_SIZE = 100  # Jira's maximum


@dataclass(frozen=True)
class JiraIssue:
    """Normalized Jira issue representation."""

    # Core identifiers
    key: str  # e.g., "PROJ-123"
    id: str  # numeric ID

    # Basic fields
    summary: str
    description: str | None
    project: str  # e.g., "PROJ"
    issuetype: str  # e.g., "Bug", "Task"
    status: str  # raw Jira status name
    priority: str | None  # e.g., "High", "Medium"
    assignee: str | None  # display name
    reporter: str | None  # display name

    # Timestamps
    created: datetime
    updated: datetime

    # Additional metadata
    url: str  # browser URL
    labels: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "key": self.key,
            "id": self.id,
            "summary": self.summary,
            "description": self.description,
            "project": self.project,
            "issuetype": self.issuetype,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "created": self.created.isoformat(),
            "updated": self.updated.isoformat(),
            "url": self.url,
            "labels": self.labels,
        }

    @classmethod
    def from_jira_api(cls, issue_data: dict[str, Any], base_url: str) -> JiraIssue:
        """Create from Jira API response."""
        fields = issue_data["fields"]
        key = issue_data["key"]

        # Parse timestamps
        created_str = fields.get("created", "")
        updated_str = fields.get("updated", "")

        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            created = datetime.now()

        try:
            updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            updated = datetime.now()

        # Extract assignee and reporter display names
        assignee = None
        if fields.get("assignee"):
            assignee = fields["assignee"].get("displayName")

        reporter = None
        if fields.get("reporter"):
            reporter = fields["reporter"].get("displayName")

        # Extract priority name
        priority = None
        if fields.get("priority"):
            priority = fields["priority"].get("name")

        return cls(
            key=key,
            id=issue_data["id"],
            summary=fields.get("summary", ""),
            description=fields.get("description"),
            project=fields.get("project", {}).get("key", ""),
            issuetype=fields.get("issuetype", {}).get("name", "Task"),
            status=fields.get("status", {}).get("name", "To Do"),
            priority=priority,
            assignee=assignee,
            reporter=reporter,
            created=created,
            updated=updated,
            url=f"{base_url}/browse/{key}",
            labels=fields.get("labels", []),
        )


class JiraClientError(Exception):
    """Base exception for Jira client errors."""
    pass


class JiraAuthenticationError(JiraClientError):
    """Authentication failed (401, 403)."""
    pass


class JiraRateLimitError(JiraClientError):
    """Rate limit exceeded (429)."""
    pass


class JiraClient:
    """Jira REST API client with read-only operations."""

    def __init__(self, config: JiraConfig, timeout: int = DEFAULT_TIMEOUT):
        self.config = config
        self.timeout = timeout
        self.base_url = config.url

        if not config.has_credentials:
            raise JiraClientError(f"No credentials for Jira instance: {config.instance_name}")

    def _make_request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        data: dict[str, Any] | None = None,
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """Make HTTP request to Jira API with retries and error handling."""
        url = f"{self.base_url}/rest/api/{JIRA_API_VERSION}/{path.lstrip('/')}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": self.config.auth_header,
        }

        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = response.read().decode("utf-8")
                if not response_data:
                    return {}
                try:
                    return json.loads(response_data)
                except json.JSONDecodeError as exc:
                    # A 200 whose body is not JSON (proxy page, HTML error
                    # screen) is reported, never silently returned as {}.
                    # The body is not echoed: it may carry data we must not log.
                    raise JiraClientError(
                        f"Jira returned a malformed JSON response "
                        f"({exc.msg} at line {exc.lineno})"
                    ) from exc

        except urllib.error.HTTPError as exc:
            status = exc.code

            # Authentication errors
            if status in (401, 403):
                raise JiraAuthenticationError(
                    f"Jira authentication failed: {status} {exc.reason}"
                ) from exc

            # Rate limiting
            elif status == 429:
                if retry_count < MAX_RETRIES:
                    # Exponential backoff
                    delay = RETRY_DELAY * (2 ** retry_count)
                    time.sleep(delay)
                    return self._make_request(method, path, data, retry_count + 1)
                raise JiraRateLimitError(
                    f"Jira rate limit exceeded after {MAX_RETRIES} retries"
                ) from exc

            # Other HTTP errors
            elif status >= 400:
                raise JiraClientError(
                    f"Jira API error: {status} {exc.reason}"
                ) from exc

            # Should not reach here for successful status codes
            raise

        except urllib.error.URLError as exc:
            # Network errors
            if retry_count < MAX_RETRIES:
                delay = RETRY_DELAY * (2 ** retry_count)
                time.sleep(delay)
                return self._make_request(method, path, data, retry_count + 1)
            raise JiraClientError(
                f"Network error connecting to Jira: {exc}"
            ) from exc

        except TimeoutError as exc:
            # Timeout errors
            if retry_count < MAX_RETRIES:
                delay = RETRY_DELAY * (2 ** retry_count)
                time.sleep(delay)
                return self._make_request(method, path, data, retry_count + 1)
            raise JiraClientError(
                f"Timeout connecting to Jira after {self.timeout}s"
            ) from exc

    def search_issues(
        self,
        jql: str,
        fields: list[str] | None = None,
        max_results: int = MAX_PAGE_SIZE,
    ) -> Generator[JiraIssue, None, None]:
        """Search issues using JQL with pagination.

        Args:
            jql: JQL query string
            fields: List of field names to request (None for default set)
            max_results: Maximum number of issues to return

        Yields:
            JiraIssue objects
        """
        if fields is None:
            fields = list(DEFAULT_FIELDS)

        page_token = None
        yielded = 0

        while yielded < max_results:
            # Prepare request body. search/jql paginates with nextPageToken;
            # there is no startAt and no total to compute against.
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": min(MAX_PAGE_SIZE, max_results - yielded),
                "fields": fields,
            }
            if page_token is not None:
                body["nextPageToken"] = page_token

            # Make request
            response = self._make_request("POST", JIRA_SEARCH_PATH, body)

            # Extract issues
            issues = response.get("issues", []) or []
            for issue_data in issues:
                yield JiraIssue.from_jira_api(issue_data, self.base_url)
                yielded += 1
                if yielded >= max_results:
                    return

            # A page with no token is the last page. Stopping on absence, not
            # on emptiness, is what keeps a 0-issue backlog from looping.
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def get_project_issues(
        self,
        project_keys: list[str],
        statuses: list[str] | None = None,
        updated_since: datetime | None = None,
        max_issues: int = 1000,
    ) -> list[JiraIssue]:
        """Get issues for specific projects.

        Args:
            project_keys: List of project keys (e.g., ["PROJ", "DEV"])
            statuses: Optional list of status names to filter
            updated_since: Only return issues updated after this time
            max_issues: Maximum number of issues to return

        Returns:
            List of JiraIssue objects
        """
        # Build JQL query
        jql_parts = []

        # Project filter
        if project_keys:
            project_list = ", ".join(f'"{key}"' for key in project_keys)
            jql_parts.append(f"project in ({project_list})")

        # Status filter
        if statuses:
            status_list = ", ".join(f'"{status}"' for status in statuses)
            jql_parts.append(f"status in ({status_list})")

        # Updated since filter
        if updated_since:
            # JQL expects format: "2024-01-15 10:30:00"
            updated_str = updated_since.strftime("%Y-%m-%d %H:%M")
            jql_parts.append(f"updated >= \"{updated_str}\"")

        # Conditions are ANDed; ORDER BY is a clause, not a condition. Joining
        # every part with " AND " produced '... AND ORDER BY updated DESC',
        # which is invalid JQL and would be rejected by the endpoint.
        if jql_parts:
            jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"
        else:
            jql = "ORDER BY updated DESC"

        # Fetch issues
        issues = []
        for issue in self.search_issues(jql, max_results=max_issues):
            issues.append(issue)
            if len(issues) >= max_issues:
                break

        return issues

    def test_connection(self) -> bool:
        """Test connection and authentication.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Try to fetch server info (small, cheap endpoint)
            response = self._make_request("GET", "serverInfo")
            return "version" in response
        except JiraClientError:
            return False
