"""Forge terminal interface.

A thin adapter over the existing safety stack: workspace containment, secret
classification, command classification, and the approval gate. It classifies and
asks. It does not execute.

`forge.cli.render` is pure presentation. `forge.cli.main` owns argv, wiring, and
exit codes. Neither holds a rule.
"""

from __future__ import annotations

from .main import DEFAULT_AGENT, WORKSPACE_ENV, build_parser, main

__all__ = ["main", "build_parser", "DEFAULT_AGENT", "WORKSPACE_ENV"]