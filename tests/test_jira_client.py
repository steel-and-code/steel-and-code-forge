"""Jira REST client tests, offline by construction.

An autouse fixture replaces urllib.request.urlopen with a tripwire, so a test
that forgets its own double fails loudly instead of touching the network.
Retry tests patch forge.jira.client.time.sleep, making backoff deterministic.
The client must use the supported POST /rest/api/3/search/jql endpoint with
nextPageToken pagination, never the deprecated startAt offset form.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from unittest import mock

import pytest

from forge.jira.client import (
    JIRA_SEARCH_PATH,
    MAX_RETRIES,
    JiraAuthenticationError,
    JiraClient,
    JiraClientError,
    JiraIssue,
    JiraRateLimitError,
)
from forge.jira.config import JiraConfig


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any un-patched urlopen call is a test failure, not an accident."""

    def _tripwire(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "a Jira client test attempted a real network request; patch "
            "urllib.request.urlopen with a double"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _tripwire)


class _FakeHTTPResponse:
    """Context-manager-compatible stand-in for urllib's response object.

    _make_request consumes the response with `with`, so a plain Mock is not
    enough (no __exit__); this double implements the real protocol.
    """

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _http_error(code: int, msg: str) -> urllib.error.HTTPError:
    """A genuine urllib HTTPError, the way urlopen actually raises."""
    return urllib.error.HTTPError(
        url="https://test.atlassian.net/rest/api/3/search/jql",
        code=code, msg=msg, hdrs={}, fp=None,
    )


def _issue_payload(key: str) -> dict:
    return {
        "id": f"100{key[-1]}", "key": key,
        "fields": {
            "summary": f"issue {key}", "description": None,
            "project": {"key": key.split("-")[0]},
            "issuetype": {"name": "Task"}, "status": {"name": "To Do"},
            "priority": None, "assignee": None, "reporter": None,
            "created": "2024-01-15T10:30:00.000Z",
            "updated": "2024-01-15T11:30:00.000Z", "labels": [],
        },
    }


@pytest.fixture
def mock_jira_config() -> JiraConfig:
    return JiraConfig(
        url="https://test.atlassian.net",
        email="test@example.com",
        api_token="test-token",
        instance_name="test-instance",
    )


@pytest.fixture
def mock_jira_response() -> dict:
    return {
        "isLast": True,
        "issues": [
            {
                "id": "10001", "key": "TEST-1",
                "fields": {
                    "summary": "Test Issue 1", "description": "Description 1",
                    "project": {"key": "TEST"}, "issuetype": {"name": "Bug"},
                    "status": {"name": "To Do"}, "priority": {"name": "High"},
                    "assignee": {"displayName": "John Doe"},
                    "reporter": {"displayName": "Jane Smith"},
                    "created": "2024-01-15T10:30:00.000Z",
                    "updated": "2024-01-15T11:30:00.000Z",
                    "labels": ["bug", "critical"],
                },
            },
            {
                "id": "10002", "key": "TEST-2",
                "fields": {
                    "summary": "Test Issue 2", "description": None,
                    "project": {"key": "TEST"}, "issuetype": {"name": "Task"},
                    "status": {"name": "In Progress"}, "priority": None,
                    "assignee": None,
                    "reporter": {"displayName": "Jane Smith"},
                    "created": "2024-01-14T09:00:00.000Z",
                    "updated": "2024-01-15T12:00:00.000Z", "labels": [],
                },
            },
        ],
    }


def test_jira_issue_from_api(mock_jira_response: dict) -> None:
    issue = JiraIssue.from_jira_api(
        mock_jira_response["issues"][0], "https://test.atlassian.net"
    )
    assert issue.key == "TEST-1"
    assert issue.id == "10001"
    assert issue.summary == "Test Issue 1"
    assert issue.description == "Description 1"
    assert issue.project == "TEST"
    assert issue.issuetype == "Bug"
    assert issue.status == "To Do"
    assert issue.priority == "High"
    assert issue.assignee == "John Doe"
    assert issue.reporter == "Jane Smith"
    assert issue.url == "https://test.atlassian.net/browse/TEST-1"
    assert issue.labels == ["bug", "critical"]
    assert isinstance(issue.created, datetime)
    assert isinstance(issue.updated, datetime)


def test_jira_issue_to_dict(mock_jira_response: dict) -> None:
    issue = JiraIssue.from_jira_api(
        mock_jira_response["issues"][0], "https://test.atlassian.net"
    )
    d = issue.to_dict()
    assert d["key"] == "TEST-1"
    assert d["summary"] == "Test Issue 1"
    assert d["project"] == "TEST"
    assert d["status"] == "To Do"
    assert isinstance(d["created"], str)
    assert isinstance(d["updated"], str)


def test_jira_issue_with_invalid_timestamp() -> None:
    issue_data = {
        "id": "10001", "key": "TEST-1",
        "fields": {
            "summary": "Test Issue", "description": None,
            "project": {"key": "TEST"}, "issuetype": {"name": "Bug"},
            "status": {"name": "To Do"}, "priority": None,
            "assignee": None, "reporter": None,
            "created": "invalid-timestamp", "updated": "also-invalid",
            "labels": [],
        },
    }
    issue = JiraIssue.from_jira_api(issue_data, "https://test.atlassian.net")
    assert isinstance(issue.created, datetime)
    assert isinstance(issue.updated, datetime)


def test_jira_client_creation(mock_jira_config: JiraConfig) -> None:
    client = JiraClient(mock_jira_config)
    assert client.config == mock_jira_config
    assert client.base_url == "https://test.atlassian.net"


def test_jira_client_missing_credentials() -> None:
    config = JiraConfig(
        url="https://test.atlassian.net", email=None, api_token=None
    )
    with pytest.raises(JiraClientError, match="No credentials"):
        JiraClient(config)


@mock.patch("urllib.request.urlopen")
def test_jira_client_successful_request(
    mock_urlopen: mock.Mock,
    mock_jira_config: JiraConfig,
    mock_jira_response: dict,
) -> None:
    mock_urlopen.return_value = _FakeHTTPResponse(mock_jira_response)

    client = JiraClient(mock_jira_config)
    response = client._make_request("POST", JIRA_SEARCH_PATH, {"jql": "x=1"})

    assert response["issues"] == mock_jira_response["issues"]
    mock_urlopen.assert_called_once()
    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "https://test.atlassian.net/rest/api/3/search/jql"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == mock_jira_config.auth_header
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"jql": "x=1"}


@mock.patch("urllib.request.urlopen")
def test_jira_client_authentication_error(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 401 maps to JiraAuthenticationError with no pointless retries."""
    mock_urlopen.side_effect = _http_error(401, "Unauthorized")
    client = JiraClient(mock_jira_config)
    with pytest.raises(JiraAuthenticationError, match="authentication failed"):
        client._make_request("GET", "serverInfo")
    assert mock_urlopen.call_count == 1


@mock.patch("forge.jira.client.time.sleep")
@mock.patch("urllib.request.urlopen")
def test_jira_client_rate_limit_with_retry(
    mock_urlopen: mock.Mock,
    mock_sleep: mock.Mock,
    mock_jira_config: JiraConfig,
) -> None:
    mock_urlopen.side_effect = [
        _http_error(429, "Too Many Requests"),
        _FakeHTTPResponse({"version": "9.0.0"}),
    ]
    client = JiraClient(mock_jira_config, timeout=1)
    response = client._make_request("GET", "serverInfo")
    assert response["version"] == "9.0.0"
    assert mock_urlopen.call_count == 2
    assert mock_sleep.call_args_list == [mock.call(1)]


@mock.patch("forge.jira.client.time.sleep")
@mock.patch("urllib.request.urlopen")
def test_jira_client_rate_limit_exhausted(
    mock_urlopen: mock.Mock,
    mock_sleep: mock.Mock,
    mock_jira_config: JiraConfig,
) -> None:
    mock_urlopen.side_effect = _http_error(429, "Too Many Requests")
    client = JiraClient(mock_jira_config, timeout=1)
    with pytest.raises(JiraRateLimitError, match="rate limit exceeded"):
        client._make_request("GET", "serverInfo")
    # Initial call + MAX_RETRIES (3) retries; backoffs 1, 2, 4.
    assert mock_urlopen.call_count == 4
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 2, 4]


@mock.patch("forge.jira.client.time.sleep")
@mock.patch("urllib.request.urlopen")
def test_jira_client_network_error_with_retry(
    mock_urlopen: mock.Mock,
    mock_sleep: mock.Mock,
    mock_jira_config: JiraConfig,
) -> None:
    mock_urlopen.side_effect = [
        urllib.error.URLError("Network unreachable"),
        _FakeHTTPResponse({"version": "9.0.0"}),
    ]
    client = JiraClient(mock_jira_config, timeout=1)
    response = client._make_request("GET", "serverInfo")
    assert response["version"] == "9.0.0"
    assert mock_urlopen.call_count == 2
    assert mock_sleep.call_count == 1


@mock.patch("forge.jira.client.time.sleep")
@mock.patch("urllib.request.urlopen")
def test_jira_client_timeout_with_retry(
    mock_urlopen: mock.Mock,
    mock_sleep: mock.Mock,
    mock_jira_config: JiraConfig,
) -> None:
    mock_urlopen.side_effect = [
        TimeoutError(),
        _FakeHTTPResponse({"version": "9.0.0"}),
    ]
    client = JiraClient(mock_jira_config, timeout=1)
    response = client._make_request("GET", "serverInfo")
    assert response["version"] == "9.0.0"
    assert mock_urlopen.call_count == 2
    assert mock_sleep.call_count == 1


@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_success(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    mock_urlopen.return_value = _FakeHTTPResponse({"version": "9.0.0"})
    assert JiraClient(mock_jira_config).test_connection() is True


@mock.patch("forge.jira.client.time.sleep")
@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_failure(
    mock_urlopen: mock.Mock,
    mock_sleep: mock.Mock,
    mock_jira_config: JiraConfig,
) -> None:
    """test_connection reports False rather than propagating.

    Decorators bind bottom-up, so the urlopen double is the first parameter:
    naming them the other way round injected a MagicMock where the client
    expected a URLError, and json.loads raised TypeError instead.
    """
    mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
    assert JiraClient(mock_jira_config, timeout=1).test_connection() is False

    # A network error is retried with backoff before being reported as False.
    assert mock_urlopen.call_count == MAX_RETRIES + 1
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1, 2, 4]


@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_http_server_error(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 5xx from Jira is False, not an exception escaping the health probe."""
    mock_urlopen.side_effect = _http_error(500, "Internal Server Error")
    assert JiraClient(mock_jira_config, timeout=1).test_connection() is False
    # HTTP errors are not retried.
    assert mock_urlopen.call_count == 1


@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_authentication_error(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 401 is reported as a failed connection (it subclasses JiraClientError)."""
    mock_urlopen.side_effect = _http_error(401, "Unauthorized")
    assert JiraClient(mock_jira_config, timeout=1).test_connection() is False


@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_malformed_json(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 200 with a non-JSON body (proxy page) is False, not a TypeError."""

    class _RawBodyResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> "_RawBodyResponse":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    mock_urlopen.return_value = _RawBodyResponse(b"<html>gateway</html>")
    assert JiraClient(mock_jira_config, timeout=1).test_connection() is False


@mock.patch("urllib.request.urlopen")
def test_jira_client_test_connection_missing_version_field(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 200 that is valid JSON but has no version is not a healthy server."""
    mock_urlopen.return_value = _FakeHTTPResponse({"status": "ok"})
    assert JiraClient(mock_jira_config, timeout=1).test_connection() is False


@mock.patch("urllib.request.urlopen")
def test_jira_client_malformed_json_response(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A 200 whose body is not JSON is a visible client error, not a crash."""

    class _BadBodyResponse:
        def read(self) -> bytes:
            return b"<html>not json at all</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

    mock_urlopen.return_value = _BadBodyResponse()
    client = JiraClient(mock_jira_config)

    with pytest.raises(JiraClientError, match="malformed JSON"):
        client._make_request("GET", "serverInfo")


# --------------------------------------------------------------------------
# search_issues: nextPageToken pagination
# --------------------------------------------------------------------------


@mock.patch("urllib.request.urlopen")
def test_jira_client_search_issues(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """Pagination follows nextPageToken, never startAt offsets."""
    page1 = {"issues": [_issue_payload("TEST-1")], "nextPageToken": "tok_abc"}
    page2 = {"issues": [_issue_payload("TEST-2")]}

    mock_urlopen.side_effect = [
        _FakeHTTPResponse(page1),
        _FakeHTTPResponse(page2),
    ]

    client = JiraClient(mock_jira_config)
    issues = list(client.search_issues("project=TEST", max_results=50))

    assert [i.key for i in issues] == ["TEST-1", "TEST-2"]
    assert mock_urlopen.call_count == 2

    first_body = json.loads(mock_urlopen.call_args_list[0][0][0].data)
    assert first_body["jql"] == "project=TEST"
    assert first_body["maxResults"] == 50
    assert "startAt" not in first_body, "deprecated offset pagination must be gone"

    second_body = json.loads(mock_urlopen.call_args_list[1][0][0].data)
    assert second_body["nextPageToken"] == "tok_abc", (
        "continuation must carry the token from the previous page"
    )
    assert "startAt" not in second_body


@mock.patch("urllib.request.urlopen")
def test_jira_client_search_issues_respects_max_results(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """A large page cap is bounded to one page and stops at max_results."""
    page = {
        "issues": [_issue_payload(f"TEST-{n}") for n in (1, 2, 3)],
        "nextPageToken": "tok_more",
    }
    mock_urlopen.return_value = _FakeHTTPResponse(page)

    client = JiraClient(mock_jira_config)
    issues = list(client.search_issues("project=TEST", max_results=2))

    assert len(issues) == 2
    assert mock_urlopen.call_count == 1
    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert body["maxResults"] == 2


@mock.patch("urllib.request.urlopen")
def test_jira_client_search_issues_single_page_no_token(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """No nextPageToken means one page: the client must not loop forever."""
    mock_urlopen.return_value = _FakeHTTPResponse(
        {"issues": [_issue_payload("TEST-1")]}
    )

    client = JiraClient(mock_jira_config)
    issues = list(client.search_issues("project=TEST"))

    assert len(issues) == 1
    assert mock_urlopen.call_count == 1


@mock.patch("urllib.request.urlopen")
def test_jira_client_search_issues_empty_result(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """An empty backlog is a clean empty iterator, not an error."""
    mock_urlopen.return_value = _FakeHTTPResponse({"issues": []})

    client = JiraClient(mock_jira_config)
    assert list(client.search_issues("project=NONE")) == []
    assert mock_urlopen.call_count == 1


@mock.patch("urllib.request.urlopen")
def test_jira_client_search_issues_requests_only_needed_fields(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """The request asks for the normalized fields, not every field."""
    mock_urlopen.return_value = _FakeHTTPResponse({"issues": []})

    client = JiraClient(mock_jira_config)
    list(client.search_issues("project=TEST"))

    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert set(body["fields"]) == {
        "summary", "description", "project", "issuetype", "status",
        "priority", "assignee", "reporter", "created", "updated", "labels",
    }


# --------------------------------------------------------------------------
# get_project_issues: JQL construction
# --------------------------------------------------------------------------


@mock.patch("urllib.request.urlopen")
def test_jira_client_get_project_issues(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """Project filter and ordering are combined into valid JQL."""
    mock_urlopen.return_value = _FakeHTTPResponse(
        {"issues": [_issue_payload("TEST-1"), _issue_payload("DEV-1")]}
    )

    client = JiraClient(mock_jira_config)
    issues = client.get_project_issues(["TEST", "DEV"])

    assert len(issues) == 2
    jql = json.loads(mock_urlopen.call_args[0][0].data)["jql"]
    assert 'project in ("TEST", "DEV")' in jql
    assert jql.endswith("ORDER BY updated DESC")
    # ORDER BY is a clause, not a condition: it must never follow an AND.
    assert "AND ORDER BY" not in jql


@mock.patch("urllib.request.urlopen")
def test_jira_client_get_project_issues_with_filters(
    mock_urlopen: mock.Mock, mock_jira_config: JiraConfig
) -> None:
    """Status and date filters are ANDed, ordering stays the final clause."""
    mock_urlopen.return_value = _FakeHTTPResponse({"issues": []})

    client = JiraClient(mock_jira_config)
    client.get_project_issues(
        ["TEST"],
        statuses=["To Do", "In Progress"],
        updated_since=datetime(2024, 1, 1),
        max_issues=10,
    )

    body = json.loads(mock_urlopen.call_args[0][0].data)
    jql = body["jql"]
    assert 'project in ("TEST")' in jql
    assert 'status in ("To Do", "In Progress")' in jql
    assert 'updated >= "2024-01-01 00:00"' in jql
    assert "AND ORDER BY" not in jql
    assert body["maxResults"] == 10
