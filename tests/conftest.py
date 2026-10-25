"""Pytest configuration for Forge tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from forge.cli.env_loader import OPENROUTER_KEY_FILE as ORIGINAL_OPENROUTER_KEY_FILE


@pytest.fixture(autouse=True)
def isolate_credential_loader() -> None:
    """Isolate tests from real credential files.
    
    This fixture runs automatically for every test and ensures that
    the credential loader cannot read from real user directories.
    It patches OPENROUTER_KEY_FILE to point to a non-existent file
    in a temporary directory.
    """
    # Create a temporary directory for test credentials
    with tempfile.TemporaryDirectory() as tmpdir:
        # Point to a non-existent file in the temp directory
        non_existent_key_file = Path(tmpdir) / "openrouter.key"
        # This file doesn't exist, so _safe_read_keyfile will return None
        
        with mock.patch("forge.cli.env_loader.OPENROUTER_KEY_FILE", non_existent_key_file):
            # Yield control to the test
            yield