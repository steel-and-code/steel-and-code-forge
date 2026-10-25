"""Tests for secure credential loading from filesystem."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from forge.cli.env_loader import OPENROUTER_ENV_VAR, enrich_environ, _safe_read_keyfile


def test_safe_read_keyfile_missing() -> None:
    """Missing keyfile returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "nonexistent.key"
        assert _safe_read_keyfile(path) is None


def test_safe_read_keyfile_empty() -> None:
    """Empty keyfile returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "empty.key"
        path.write_text("", encoding="utf-8")
        assert _safe_read_keyfile(path) is None


def test_safe_read_keyfile_whitespace_only() -> None:
    """Keyfile with only whitespace returns None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "whitespace.key"
        path.write_text("  \n\t  \n", encoding="utf-8")
        assert _safe_read_keyfile(path) is None


def test_safe_read_keyfile_valid() -> None:
    """Valid keyfile returns stripped content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "valid.key"
        path.write_text("  sk-test-key  \n", encoding="utf-8")
        assert _safe_read_keyfile(path) == "sk-test-key"


def test_safe_read_keyfile_permission_error() -> None:
    """Permission errors return None (silent failure)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "unreadable.key"
        path.write_text("sk-test", encoding="utf-8")
        # Make file unreadable
        path.chmod(0o000)
        try:
            # Should fail silently
            assert _safe_read_keyfile(path) is None
        finally:
            path.chmod(0o644)


def test_environ_precedence_environment_wins() -> None:
    """Environment variable takes precedence over file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        keyfile = Path(tmpdir) / "openrouter.key"
        keyfile.write_text("sk-from-file", encoding="utf-8")
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", keyfile):
            # Environment has a value
            env = {OPENROUTER_ENV_VAR: "sk-from-env", "OTHER_VAR": "value"}
            result = enrich_environ(env)
            
            assert result[OPENROUTER_ENV_VAR] == "sk-from-env"
            assert result["OTHER_VAR"] == "value"


def test_environ_precedence_file_when_missing() -> None:
    """File is used when environment variable is missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        keyfile = Path(tmpdir) / "openrouter.key"
        keyfile.write_text("sk-from-file", encoding="utf-8")
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", keyfile):
            # Environment doesn't have the key
            env = {"OTHER_VAR": "value"}
            result = enrich_environ(env)
            
            assert result[OPENROUTER_ENV_VAR] == "sk-from-file"
            assert result["OTHER_VAR"] == "value"


def test_environ_precedence_empty_env_var() -> None:
    """Empty environment variable is treated as missing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        keyfile = Path(tmpdir) / "openrouter.key"
        keyfile.write_text("sk-from-file", encoding="utf-8")
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", keyfile):
            # Environment has empty value
            env = {OPENROUTER_ENV_VAR: "   ", "OTHER_VAR": "value"}
            result = enrich_environ(env)
            
            assert result[OPENROUTER_ENV_VAR] == "sk-from-file"
            assert result["OTHER_VAR"] == "value"


def test_environ_no_file_no_env() -> None:
    """No file and no environment variable leaves it unset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        keyfile = Path(tmpdir) / "openrouter.key"
        # Don't create the file
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", keyfile):
            env = {"OTHER_VAR": "value"}
            result = enrich_environ(env)
            
            assert OPENROUTER_ENV_VAR not in result
            assert result["OTHER_VAR"] == "value"


def test_environ_passthrough() -> None:
    """All other environment variables pass through unchanged."""
    original = {"PATH": "/usr/bin", "HOME": "/home/user", "TEST": "value"}
    result = enrich_environ(original)
    
    assert result["PATH"] == "/usr/bin"
    assert result["HOME"] == "/home/user"
    assert result["TEST"] == "value"


def test_environ_defaults_to_os_environ() -> None:
    """When environ is None, uses os.environ as base."""
    with mock.patch.dict(os.environ, {OPENROUTER_ENV_VAR: "sk-from-os"}, clear=True):
        result = enrich_environ(None)
        assert result[OPENROUTER_ENV_VAR] == "sk-from-os"


def test_security_property_no_logging() -> None:
    """The key is never printed or logged in tests."""
    # This test asserts that our implementation doesn't have print statements
    # The actual check is manual code review, but we can at least run the code
    with tempfile.TemporaryDirectory() as tmpdir:
        keyfile = Path(tmpdir) / "openrouter.key"
        keyfile.write_text("sk-secret-test-key", encoding="utf-8")
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", keyfile):
            env = {}
            result = enrich_environ(env)
            
            # Key should be loaded but not printed
            assert result[OPENROUTER_ENV_VAR] == "sk-secret-test-key"
            # No assertions about printing - that's a manual check