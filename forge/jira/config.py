"""Secure Jira configuration and credential loading for Forge.

This module provides secure loading of Jira configuration from filesystem,
specifically for loading Jira credentials and project configurations.

Key security properties:
- Never prints, logs, or commits secrets
- Environment variables always take precedence over file contents
- File reading errors are silent (fail open)
- Supports multiple Jira instances (config sections)
- Workspace-specific config overrides user config
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

# Configuration file paths
USER_CONFIG_FILE = Path("~/.config/forge/jira.toml").expanduser()
WORKSPACE_CONFIG_FILE = Path(".forge/jira.toml")
JIRA_API_TOKEN_ENV = "FORGE_JIRA_API_TOKEN"
JIRA_EMAIL_ENV = "FORGE_JIRA_EMAIL"
JIRA_URL_ENV = "FORGE_JIRA_URL"


def _env_setting(env: Mapping[str, str], name: str) -> str | None:
    """Read one FORGE_JIRA_* setting.

    An unset or blank variable means "not specified": it must never erase the
    value a config file provided, so it is reported as None rather than "".
    """
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _safe_read_config(path: Path) -> dict[str, Any] | None:
    """Read a TOML config file, silently failing on any error.

    Returns:
        The parsed config as a dictionary, or None if the file cannot be read
        or is invalid.
    """
    try:
        if not path.exists():
            return None

        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return None

        return tomllib.loads(content)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        # Silent failure: a broken config file is the same as no config file
        return None


class JiraConfig:
    """Jira configuration for a single instance."""

    def __init__(
        self,
        url: str,
        email: str | None = None,
        api_token: str | None = None,
        projects: list[str] | None = None,
        status_mapping: dict[str, str] | None = None,
        instance_name: str = "default",
    ):
        self.url = url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.projects = projects or []
        self.status_mapping = status_mapping or {}
        self.instance_name = instance_name

    @property
    def has_credentials(self) -> bool:
        """Check if both email and API token are available."""
        return bool(self.email and self.api_token)

    @property
    def auth_header(self) -> str | None:
        """Generate Basic Auth header if credentials are available."""
        if not self.has_credentials:
            return None
        import base64
        credentials = f"{self.email}:{self.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def __repr__(self) -> str:
        safe_url = self.url
        return f"JiraConfig(url={safe_url!r}, instance_name={self.instance_name!r})"


def load_jira_config(
    workspace_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, JiraConfig]:
    """Load Jira configuration from multiple sources with precedence.

    Precedence order (highest to lowest):
    1. Environment variables (FORGE_JIRA_URL, FORGE_JIRA_EMAIL, FORGE_JIRA_API_TOKEN)
    2. Workspace config (.forge/jira.toml)
    3. User config (~/.config/forge/jira.toml)

    Args:
        workspace_root: Workspace root path. If None, workspace config is skipped.
        environ: Environment mapping. If None, uses os.environ.

    Returns:
        Dictionary mapping instance names to JiraConfig objects.
    """
    env = dict(environ) if environ is not None else dict(os.environ)
    configs = {}

    # Start with user config
    user_config = _safe_read_config(USER_CONFIG_FILE)
    if user_config:
        for instance_name, instance_config in user_config.get("jira", {}).items():
            if not isinstance(instance_config, dict):
                continue
            configs[instance_name] = JiraConfig(
                url=instance_config.get("url", ""),
                email=instance_config.get("email"),
                api_token=instance_config.get("api_token"),
                projects=instance_config.get("projects"),
                status_mapping=instance_config.get("status_mapping", {}),
                instance_name=instance_name,
            )

    # Override with workspace config if workspace_root is provided
    if workspace_root:
        workspace_config_path = workspace_root / WORKSPACE_CONFIG_FILE
        workspace_config = _safe_read_config(workspace_config_path)
        if workspace_config:
            for instance_name, instance_config in workspace_config.get("jira", {}).items():
                if not isinstance(instance_config, dict):
                    continue
                # Update or create config
                if instance_name in configs:
                    # Update existing config
                    config = configs[instance_name]
                    if "url" in instance_config:
                        config.url = instance_config["url"]
                    if "email" in instance_config:
                        config.email = instance_config["email"]
                    if "api_token" in instance_config:
                        config.api_token = instance_config["api_token"]
                    if "projects" in instance_config:
                        config.projects = instance_config["projects"]
                    if "status_mapping" in instance_config:
                        config.status_mapping.update(instance_config["status_mapping"])
                else:
                    # Create new config
                    configs[instance_name] = JiraConfig(
                        url=instance_config.get("url", ""),
                        email=instance_config.get("email"),
                        api_token=instance_config.get("api_token"),
                        projects=instance_config.get("projects"),
                        status_mapping=instance_config.get("status_mapping", {}),
                        instance_name=instance_name,
                    )

    # Override with environment variables for the default instance.
    # Each setting is applied independently: an explicit FORGE_JIRA_* value
    # replaces the matching file value, and an unset one leaves the file value
    # in place. (The URL previously only reached an existing "default" config
    # when that config did not exist, so a file URL silently won.)
    env_url = _env_setting(env, JIRA_URL_ENV)
    env_email = _env_setting(env, JIRA_EMAIL_ENV)
    env_token = _env_setting(env, JIRA_API_TOKEN_ENV)

    if env_url or env_email or env_token:
        default_config = configs.get("default")
        if default_config is None:
            default_config = JiraConfig(url=env_url or "", instance_name="default")
            configs["default"] = default_config

        if env_url:
            default_config.url = env_url.rstrip("/")
        if env_email:
            default_config.email = env_email
        if env_token:
            default_config.api_token = env_token

    # Filter out configs without URL
    return {name: config for name, config in configs.items() if config.url}


def get_default_config(
    workspace_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> JiraConfig | None:
    """Get the default Jira configuration.

    Returns the "default" instance if it exists, otherwise the first config.
    """
    configs = load_jira_config(workspace_root, environ)
    if not configs:
        return None

    # Prefer "default" instance
    if "default" in configs:
        return configs["default"]

    # Otherwise return the first config
    return next(iter(configs.values()))
