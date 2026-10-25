"""Offline tests for the Jira synchronization workflow.

JiraSync is the orchestration layer between the REST client and the SQLite
cache: first sync, repeat sync (skip window), and failure handling that must
never destroy successfully cached issues. Each test drives the real JiraClient
with a urllib double, so the wire protocol (POST /rest/api/3/search/jql) and
the JQL that is actually sent are asserted, not stubbed away.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from forge.jira.client import JIRA_SEARCH_PATH, JiraIssue
from forge.jira.store import JiraCache
from forge.jira.sync import JiraSync, sync_workspace

CONFIG_TOML = """
[jira.workshop]
url = "https://workshop.atlassian.net"
email = "forge@example.com"
api_token = "secret-file-token"
projects = ["FORGE", "STEEL"]

[jira.workshop.status_mapping]
"To Do" = "orders_received"
"In Progress" = "at_the_forge"
"""

NO_CREDENTIALS_TOML = """
[jira.workshop]
url = "https://workshop.atlassian.net"
projects = ["FORGE"]
"""


class _FakeHTTPResponse:
    """Context-manager-compatible stand-in for urllib's response object."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _server_info() -> _FakeHTTPResponse:
    return _FakeHTTPResponse({"version": "9.0.0"})


def _search_page(*keys: str) -> _FakeHTTPResponse:
    issues = [
        {
            "id": "10001",
            "key": key,
            "fields": {
                "summary": f"issue {key}",
                "description": None,
                "project": {"key": key.split("-")[0]},
                "issuetype": {"name": "Task"},
                "status": {"name": "To Do"},
                "priority": None,
                "assignee": None,
                "reporter": None,
                "created": "2024-01-15T10:30:00.000Z",
                "updated": "2024-01-15T11:30:00.000Z",
                "labels": [],
            },
        }
        for key in keys
    ]
    return _FakeHTTPResponse({"issues": issues})


def _issue(key: str) -> JiraIssue:
    return JiraIssue(
        key=key, id="1", summary=key, description=None,
        project=key.split("-")[0], issuetype="Task", status="To Do",
        priority=None, assignee=None, reporter=None,
        created=datetime(2024, 1, 1), updated=datetime(2024, 1, 2),
        url=f"https://workshop.atlassian.net/browse/{key}", labels=[],
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose Jira config comes from a temp file, not the user's."""
    config_file = tmp_path / "jira.toml"
    config_file.write_text(CONFIG_TOML)
    monkeypatch.setattr("forge.jira.config.USER_CONFIG_FILE", config_file)
    return tmp_path


def test_sync_first_success_populates_cache(workspace: Path) -> None:
    """A first successful sync reaches search/jql and fills the cache."""
    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = [_server_info(), _search_page("FORGE-1", "STEEL-7")]
        result = JiraSync(workspace).sync_instance("workshop")

    assert result.success is True
    assert result.issues_fetched == 2
    assert result.error_message is None

    cache = JiraCache(workspace)
    assert {issue.key for issue in cache.get_issues("workshop")} == {"FORGE-1", "STEEL-7"}
    metadata = cache.get_sync_metadata("workshop")
    assert metadata is not None
    assert metadata.last_success is not None
    assert metadata.error_message is None
    assert metadata.total_issues == 2


def test_sync_sends_valid_jql_to_supported_endpoint(workspace: Path) -> None:
    """The JQL sync builds must be valid and must use POST search/jql.

    Regression: ORDER BY was joined with " AND ", producing
    '... AND ORDER BY updated DESC', which Jira rejects with a 400; the broad
    except then reported that as a failed sync with the cache left empty.
    """
    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = [_server_info(), _search_page()]
        JiraSync(workspace).sync_instance("workshop")

    assert mock_urlopen.call_count == 2

    probe = mock_urlopen.call_args_list[0][0][0]
    assert probe.get_method() == "GET"
    assert probe.full_url == "https://workshop.atlassian.net/rest/api/3/serverInfo"

    search = mock_urlopen.call_args_list[1][0][0]
    assert search.get_method() == "POST"
    assert search.full_url.endswith(f"/rest/api/3/{JIRA_SEARCH_PATH}")
    assert search.get_header("Authorization") == "Basic " + _basic_credentials()

    body = json.loads(search.data)
    jql = body["jql"]
    # Project keys are interpolated as a list, statuses quoted; ordering is a
    # trailing clause. Both quoted and unquoted project forms are valid JQL.
    assert "project in (FORGE, STEEL)" in jql
    assert 'status in ("To Do", "In Progress")' in jql
    assert jql.endswith("ORDER BY updated DESC")
    assert "AND ORDER BY" not in jql
    # First page: token pagination only, never the deprecated startAt offset.
    assert "startAt" not in body
    assert "nextPageToken" not in body


def _basic_credentials() -> str:
    import base64

    token = "secret-file-token"
    return base64.b64encode(f"forge@example.com:{token}".encode()).decode()


def test_sync_repeat_within_window_uses_cache_without_network(workspace: Path) -> None:
    """A recent successful sync is served from the cache: zero network calls."""
    cache = JiraCache(workspace)
    cache.store_sync("workshop", [_issue("FORGE-1"), _issue("STEEL-2")])

    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = AssertionError("no network on a skipped sync")
        result = JiraSync(workspace).sync_instance("workshop", force=False)

    assert result.success is True
    assert result.issues_fetched == 2
    assert mock_urlopen.call_count == 0


def test_sync_stale_success_runs_again(workspace: Path) -> None:
    """Once the one-hour window expires, the instance is fetched again."""
    cache = JiraCache(workspace)
    cache.store_sync("workshop", [_issue("FORGE-1")])
    stale = (datetime.now() - timedelta(hours=2)).isoformat()
    with cache._connection() as conn:
        conn.execute(
            "UPDATE sync_metadata SET last_success = ? WHERE instance_name = ?",
            (stale, "workshop"),
        )

    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = [_server_info(), _search_page("FORGE-9")]
        result = JiraSync(workspace).sync_instance("workshop")

    assert result.success is True
    assert [issue.key for issue in JiraCache(workspace).get_issues("workshop")] == ["FORGE-9"]


def test_sync_failure_preserves_cached_issues(workspace: Path) -> None:
    """A broken fetch records the error and keeps the previous good cache."""
    cache = JiraCache(workspace)
    cache.store_sync("workshop", [_issue("FORGE-1")])
    before = cache.get_sync_metadata("workshop")
    assert before is not None and before.last_success is not None

    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        result = JiraSync(workspace).sync_instance("workshop", force=True)

    assert result.success is False
    assert result.issues_fetched == 0
    # The failed probe is reported generically; the underlying cause stays in
    # the transport layer and is never echoed with credentials.
    assert result.error_message == (
        "Failed to connect to Jira instance 'workshop'"
    )
    assert "secret-file-token" not in result.error_message

    reopened = JiraCache(workspace)
    assert [issue.key for issue in reopened.get_issues("workshop")] == ["FORGE-1"]
    metadata = reopened.get_sync_metadata("workshop")
    assert metadata is not None
    assert metadata.error_message is not None
    assert metadata.last_success == before.last_success
    assert metadata.total_issues == 1


def test_sync_unknown_instance_is_reported_not_raised(workspace: Path) -> None:
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = AssertionError("no network for an unknown instance")
        result = JiraSync(workspace).sync_instance("nope")

    assert result.success is False
    assert "not configured" in (result.error_message or "")


def test_sync_without_credentials_fails_without_leaking_secrets(tmp_path: Path) -> None:
    """Missing credentials fail the sync; no token or address is in the error."""
    config_file = tmp_path / "noauth.toml"
    config_file.write_text(NO_CREDENTIALS_TOML)

    with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file), \
            mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = AssertionError("no network without credentials")
        result = JiraSync(tmp_path).sync_instance("workshop")

    assert result.success is False
    error = result.error_message or ""
    assert "No credentials" in error
    assert "secret-file-token" not in error
    assert "forge@example.com" not in error


def test_sync_all_and_convenience_helper(workspace: Path) -> None:
    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = [_server_info(), _search_page("FORGE-1")]
        results = JiraSync(workspace).sync_all()

    assert [r.instance_name for r in results] == ["workshop"]
    assert results[0].success is True

    with mock.patch("urllib.request.urlopen") as mock_urlopen, \
            mock.patch("forge.jira.client.time.sleep"):
        mock_urlopen.side_effect = [_server_info(), _search_page("FORGE-2")]
        result = sync_workspace(workspace, instance_name="workshop", force=True)

    assert isinstance(result, tuple)
    assert result.success is True  # type: ignore[union-attr]


def test_get_sync_status_reports_cache_state(workspace: Path) -> None:
    """Status is derived from the cache, so it works fully offline."""
    cache = JiraCache(workspace)
    cache.store_sync("workshop", [_issue("FORGE-1")])
    cache.store_sync("workshop", [], "boom")

    status = JiraSync(workspace).get_sync_status()

    assert status["workshop"]["configured"] is True
    assert status["workshop"]["has_credentials"] is True
    assert status["workshop"]["projects"] == ["FORGE", "STEEL"]
    assert status["workshop"]["error_message"] == "boom"
    assert status["workshop"]["last_success"] is not None
    assert status["workshop"]["total_issues"] == 1
