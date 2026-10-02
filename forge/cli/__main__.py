"""``python -m forge.cli`` entry point.

So the CLI can be exercised without installing the console script, which matters
on a Forge host where the environment may be provisioned for the agent rather
than for the operator. ``forge.cli.main:main`` remains the supported entry point;
this is the same function, wrapped for module invocation.
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":  # pragma: no cover - exercised by manual invocation
    sys.exit(main())