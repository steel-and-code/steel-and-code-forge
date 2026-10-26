"""Tests for Jira configuration and credential loading."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from forge.jira.config import JiraConfig, load_jira_config, get_default_config


def test_jira_config_creation() -> None:
    """Test basic JiraConfig creation."""
    config = JiraConfig(
        url="https://example.atlassian.net",
        email="user@example.com",
        api_token="test-token",
        projects=["PROJ", "DEV"],
        status_mapping={"To Do": "orders_received"},
        instance_name="test-instance",
    )

    assert config.url == "https://example.atlassian.net"
    assert config.email == "user@example.com"
    assert config.api_token == "test-token"
    assert config.projects == ["PROJ", "DEV"]
    assert config.status_mapping == {"To Do": "orders_received"}
    assert config.instance_name == "test-instance"
    assert config.has_credentials is True
    assert config.auth_header is not None
    assert "Basic" in config.auth_header


def test_jira_config_no_credentials() -> None:
    """Test JiraConfig without credentials."""
    config = JiraConfig(
        url="https://example.atlassian.net",
        email=None,
        api_token=None,
    )

    assert config.url == "https://example.atlassian.net"
    assert config.email is None
    assert config.api_token is None
    assert config.has_credentials is False
    assert config.auth_header is None


def test_jira_config_missing_credential() -> None:
    """Test JiraConfig with partial credentials."""
    # Missing email
    config1 = JiraConfig(
        url="https://example.atlassian.net",
        email=None,
        api_token="test-token",
    )
    assert config1.has_credentials is False

    # Missing token
    config2 = JiraConfig(
        url="https://example.atlassian.net",
        email="user@example.com",
        api_token=None,
    )
    assert config2.has_credentials is False


def test_load_jira_config_from_env() -> None:
    """Test loading Jira config from environment variables."""
    env = {
        "FORGE_JIRA_URL": "https://env.atlassian.net",
        "FORGE_JIRA_EMAIL": "env@example.com",
        "FORGE_JIRA_API_TOKEN": "env-token",
    }

    configs = load_jira_config(environ=env)

    assert "default" in configs
    config = configs["default"]
    assert config.url == "https://env.atlassian.net"
    assert config.email == "env@example.com"
    assert config.api_token == "env-token"
    assert config.instance_name == "default"


def test_load_jira_config_from_file() -> None:
    """Test loading Jira config from TOML file."""
    toml_content = """
    [jira.test-instance]
    url = "https://file.atlassian.net"
    email = "file@example.com"
    api_token = "file-token"
    projects = ["PROJ", "DEV"]

    [jira.test-instance.status_mapping]
    "To Do" = "orders_received"
    "In Progress" = "at_the_forge"
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config()

            assert "test-instance" in configs
            config = configs["test-instance"]
            assert config.url == "https://file.atlassian.net"
            assert config.email == "file@example.com"
            assert config.api_token == "file-token"
            assert config.projects == ["PROJ", "DEV"]
            assert config.status_mapping == {
                "To Do": "orders_received",
                "In Progress": "at_the_forge",
            }
            assert config.instance_name == "test-instance"


def test_load_jira_config_workspace_override() -> None:
    """Test workspace config overriding user config."""
    user_toml = """
    [jira.test-instance]
    url = "https://user.atlassian.net"
    email = "user@example.com"
    api_token = "user-token"
    projects = ["USER-PROJ"]
    """

    workspace_toml = """
    [jira.test-instance]
    url = "https://workspace.atlassian.net"
    projects = ["WORKSPACE-PROJ", "OTHER"]

    [jira.test-instance.status_mapping]
    "To Do" = "orders_received"
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_root = Path(tmpdir)
        user_config = workspace_root / "user.toml"
        workspace_config = workspace_root / ".forge" / "jira.toml"

        workspace_config.parent.mkdir(parents=True, exist_ok=True)

        user_config.write_text(user_toml)
        workspace_config.write_text(workspace_toml)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", user_config):
            configs = load_jira_config(workspace_root)

            assert "test-instance" in configs
            config = configs["test-instance"]
            # Workspace overrides URL and projects
            assert config.url == "https://workspace.atlassian.net"
            # But preserves credentials from user config
            assert config.email == "user@example.com"
            assert config.api_token == "user-token"
            # Projects from workspace
            assert config.projects == ["WORKSPACE-PROJ", "OTHER"]
            # Status mapping from workspace
            assert config.status_mapping == {"To Do": "orders_received"}


def test_load_jira_config_invalid_file() -> None:
    """Test handling of invalid config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "invalid.toml"
        config_file.write_text("this is not valid toml {")

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config()
            # Should silently fail and return empty dict
            assert configs == {}


def test_load_jira_config_missing_file() -> None:
    """Test handling of missing config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "nonexistent.toml"

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config()
            # Should silently fail and return empty dict
            assert configs == {}


def test_load_jira_config_empty_file() -> None:
    """Test handling of empty config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "empty.toml"
        config_file.write_text("")

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config()
            # Should silently fail and return empty dict
            assert configs == {}


def test_get_default_config() -> None:
    """Test getting default config."""
    env = {
        "FORGE_JIRA_URL": "https://default.atlassian.net",
        "FORGE_JIRA_EMAIL": "default@example.com",
        "FORGE_JIRA_API_TOKEN": "default-token",
    }

    config = get_default_config(environ=env)
    assert config is not None
    assert config.url == "https://default.atlassian.net"
    assert config.instance_name == "default"


def test_get_default_config_no_config() -> None:
    """Test getting default config when none exists."""
    config = get_default_config(environ={})
    assert config is None


def test_config_precedence_env_over_file() -> None:
    """Test environment variables override file config."""
    toml_content = """
    [jira.default]
    url = "https://file.atlassian.net"
    email = "file@example.com"
    api_token = "file-token"
    """

    env = {
        "FORGE_JIRA_URL": "https://env.atlassian.net",
        "FORGE_JIRA_EMAIL": "env@example.com",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config(environ=env)

            assert "default" in configs
            config = configs["default"]
            # URL and email from env
            assert config.url == "https://env.atlassian.net"
            assert config.email == "env@example.com"
            # Token from file (env doesn't have it)
            assert config.api_token == "file-token"


def test_env_url_overrides_existing_default_instance() -> None:
    """An explicit FORGE_JIRA_URL wins over a file-defined default instance.

    Regression: the env URL was previously only applied when no "default"
    config existed, so a file URL silently took precedence.
    """
    toml_content = """
    [jira.default]
    url = "https://file.atlassian.net"
    email = "file@example.com"
    api_token = "file-token"
    projects = ["PROJ", "DEV"]

    [jira.default.status_mapping]
    "To Do" = "orders_received"
    """

    env = {"FORGE_JIRA_URL": "https://env.atlassian.net/"}

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            config = load_jira_config(environ=env)["default"]

            assert config.url == "https://env.atlassian.net"
            # Unspecified settings survive the override
            assert config.email == "file@example.com"
            assert config.api_token == "file-token"
            assert config.projects == ["PROJ", "DEV"]
            assert config.status_mapping == {"To Do": "orders_received"}


def test_env_credentials_override_file_without_url() -> None:
    """FORGE_JIRA_EMAIL / FORGE_JIRA_API_TOKEN override matching file values."""
    toml_content = """
    [jira.default]
    url = "https://file.atlassian.net"
    email = "file@example.com"
    api_token = "file-token"
    projects = ["PROJ"]
    """

    env = {
        "FORGE_JIRA_EMAIL": "env@example.com",
        "FORGE_JIRA_API_TOKEN": "env-token",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            config = load_jira_config(environ=env)["default"]

            assert config.email == "env@example.com"
            assert config.api_token == "env-token"
            # URL and projects are not addressed by env, so they stay
            assert config.url == "https://file.atlassian.net"
            assert config.projects == ["PROJ"]


def test_blank_env_settings_do_not_erase_file_values() -> None:
    """An empty FORGE_JIRA_* value means "not specified", not "clear it"."""
    toml_content = """
    [jira.default]
    url = "https://file.atlassian.net"
    email = "file@example.com"
    api_token = "file-token"
    """

    env = {
        "FORGE_JIRA_URL": "",
        "FORGE_JIRA_EMAIL": "   ",
        "FORGE_JIRA_API_TOKEN": "",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config(environ=env)

            assert "default" in configs
            config = configs["default"]
            assert config.url == "https://file.atlassian.net"
            assert config.email == "file@example.com"
            assert config.api_token == "file-token"
            assert config.has_credentials is True


def test_env_creates_default_instance_when_no_file_config() -> None:
    """With no config file at all, env alone defines the default instance."""
    env = {
        "FORGE_JIRA_URL": "https://env.atlassian.net",
        "FORGE_JIRA_API_TOKEN": "env-token",
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        missing = Path(tmpdir) / "absent.toml"

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", missing):
            config = load_jira_config(environ=env)["default"]

            assert config.url == "https://env.atlassian.net"
            assert config.api_token == "env-token"
            assert config.email is None
            assert config.has_credentials is False


def test_env_does_not_leak_onto_named_instances() -> None:
    """Env settings address the default instance only; named sections stay."""
    toml_content = """
    [jira.team-a]
    url = "https://a.atlassian.net"
    email = "a@example.com"
    api_token = "a-token"
    """

    env = {"FORGE_JIRA_URL": "https://env.atlassian.net"}

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config(environ=env)

            assert configs["team-a"].url == "https://a.atlassian.net"
            assert configs["default"].url == "https://env.atlassian.net"


def test_config_filter_no_url() -> None:
    """Test configs without URL are filtered out."""
    toml_content = """
    [jira.with-url]
    url = "https://valid.atlassian.net"
    email = "test@example.com"

    [jira.without-url]
    email = "test2@example.com"
    # Missing url
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "jira.toml"
        config_file.write_text(toml_content)

        with mock.patch("forge.jira.config.USER_CONFIG_FILE", config_file):
            configs = load_jira_config()

            # Should only include config with URL
            assert "with-url" in configs
            assert "without-url" not in configs
            assert len(configs) == 1
