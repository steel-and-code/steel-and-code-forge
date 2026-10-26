"""Secure credential loading for Forge.

This module provides secure loading of API keys from the filesystem,
specifically for loading FORGE_OPENROUTER_API_KEY from
~/.config/forge/openrouter.key when the environment variable is absent.

Key security properties:
- Never prints, logs, or commits secrets
- Environment variables always take precedence over file contents
- File is read as text with whitespace stripped
- Missing file is not an error
- File reading errors are silent (fail open)
- No parsing of arbitrary .env files
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# The one credential we load from file
OPENROUTER_KEY_FILE = Path("~/.config/forge/openrouter.key").expanduser()
OPENROUTER_ENV_VAR = "FORGE_OPENROUTER_API_KEY"


def _safe_read_keyfile(path: Path) -> str | None:
    """Read an API key from a file, silently failing on any error.
    
    Returns:
        The key as a stripped string, or None if the file cannot be read
        or contains only whitespace.
    """
    try:
        if not path.exists():
            return None
        
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        
        return content
    except (OSError, UnicodeError):
        # Silent failure: a broken keyfile is the same as no keyfile
        return None


def enrich_environ(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Enrich environment with API key from filesystem when absent.
    
    If FORGE_OPENROUTER_API_KEY is not set in the environment, this function
    reads it from ~/.config/forge/openrouter.key (if that file exists and
    contains a non-empty string).
    
    All other environment variables pass through unchanged. No arbitrary .env
    files are parsed.
    
    Args:
        environ: Base environment mapping. If None, uses os.environ.
    
    Returns:
        New dictionary with environment values, potentially including the
        OpenRouter API key from file.
    """
    base = dict(environ) if environ is not None else dict(os.environ)
    
    # Only load from file if the environment variable is not already set
    if OPENROUTER_ENV_VAR not in base or not base[OPENROUTER_ENV_VAR].strip():
        key = _safe_read_keyfile(OPENROUTER_KEY_FILE)
        if key is not None:
            base[OPENROUTER_ENV_VAR] = key
    
    return base