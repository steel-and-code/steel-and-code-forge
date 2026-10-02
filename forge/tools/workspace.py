"""Path resolution and workspace containment.

Implements the v1 workspace boundary decision recorded in
``docs/byte-security-classification.md``:

    - Each Byte runtime has exactly one explicit workspace root.
    - Automatic file and command operations must resolve inside that root.
    - Symlinks and ``..`` are resolved *before* the containment check.
    - Any resolved target outside the workspace root is denied.
    - Multi-project / named workspace sets are deferred.

Containment is the one check the classifier can rely on absolutely, so every
failure mode here fails closed: an unresolvable path is denied, never allowed.

Nothing in this module executes anything. It resolves and checks paths only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Classification",
    "Workspace",
    "ResolutionError",
    "Resolution",
    "resolve_within",
]


class Classification(str, Enum):
    """Outcome of a classification or containment check.

    Inherits from :class:`str` so results serialise cleanly and compare equal to
    their names, which keeps the approval UX and HTTP surfaces simple later.
    """

    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"

    @property
    def severity(self) -> int:
        """Ordering for "strictest match wins" comparisons.

        ``DENY`` outranks ``APPROVAL_REQUIRED`` outranks ``ALLOW``.
        """
        return _SEVERITY[self]


_SEVERITY = {
    Classification.ALLOW: 0,
    Classification.APPROVAL_REQUIRED: 1,
    Classification.DENY: 2,
}


def strictest(*classifications: Classification) -> Classification:
    """Return the most restrictive of ``classifications``.

    A path may match several rules at once -- for example an in-workspace
    ``secrets.json`` matches a name-guess rule *and* passes containment. The
    answer is the strictest match, not the first.
    """
    if not classifications:
        return Classification.ALLOW
    return max(classifications, key=lambda c: c.severity)


class ResolutionError(Exception):
    """Raised when a path cannot be resolved safely.

    Callers should treat this as a denial. The message is for the operator; it
    is never returned to an agent as tool output.
    """


@dataclass(frozen=True)
class Resolution:
    """The result of resolving a candidate path against a workspace.

    Attributes:
        classification: ``ALLOW`` or ``DENY``. Containment alone cannot produce
            ``APPROVAL_REQUIRED`` -- that comes from secret-source rules.
        rule: Stable identifier for the rule that decided, e.g.
            ``"workspace.contained"``. Used by the approval UX to explain a
            decision.
        reason: Human-readable explanation. Safe to show an operator. Never
            contains file contents.
        resolved: The real path, symlinks and ``..`` resolved. ``None`` only
            when resolution failed, in which case ``classification`` is ``DENY``.
        exists: Whether the resolved path exists. A path that does not exist yet
            is still resolved and still checked -- ``rm -rf build`` must be
            contained even when ``build`` is absent.
        original: The path as supplied, before resolution.
    """

    classification: Classification
    rule: str
    reason: str
    resolved: Path | None
    exists: bool
    original: Path

    @property
    def allowed(self) -> bool:
        return self.classification is Classification.ALLOW

    def __str__(self) -> str:  # pragma: no cover - operator convenience
        target = self.resolved if self.resolved is not None else "<unresolved>"
        return f"{self.classification.value}: {self.rule}: {target}"


def _normalise(path: Path) -> Path:
    """Lexically collapse ``..`` and ``.`` without touching the filesystem.

    ``Path.resolve()`` alone is not enough: it requires the path to be
    resolvable and, on a broken symlink, either raises or leaves the link
    unresolved depending on platform. We want lexical normalisation to always
    succeed so containment can be checked even for paths that do not exist.
    """
    return Path(os.path.normpath(os.path.abspath(str(path))))


class Workspace:
    """A single explicit workspace root.

    Args:
        root: The workspace root directory. Resolved to its real path on
            construction so that a workspace specified via a symlinked path
            compares equal to the same directory named directly.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        """The resolved workspace root."""
        return self._root

    def contains(self, path: Path) -> bool:
        """Whether ``path`` is ``path`` itself or lies under the workspace root.

        Uses :meth:`Path.is_relative_to`, so ``/work`` does not contain
        ``/workspace``. Symlinks are *not* followed by the caller here; see
        :meth:`resolve` for the full check.
        """
        return path == self._root or path.is_relative_to(self._root)

    def resolve(self, candidate: str | os.PathLike[str]) -> Resolution:
        """Resolve ``candidate`` and check containment.

        Resolution order, and it matters:

        1. Lexical normalisation -- ``..`` and ``.`` collapsed, made absolute.
           This happens first so that ``../`` cannot survive into the
           comparison.
        2. Containment check against the lexical result. This catches an
           obvious escape cheaply.
        3. Real-path resolution -- symlinks followed. This catches a symlink
           that points outside the workspace even though its own path is
           inside.
        4. Containment check again against the real path.

        Steps 3 and 4 are what make a symlink to ``/etc`` denied even though
        ``workspace/link`` is lexically inside the workspace.

        Fails closed. Every error path returns ``DENY``.
        """
        original = Path(candidate)
        requested = _normalise(original)

        # Step 1-2: lexical containment.
        if not self.contains(requested):
            return Resolution(
                classification=Classification.DENY,
                rule="workspace.outside_lexical",
                reason=(
                    f"path escapes the workspace root: {original} resolves to "
                    f"{requested}, which is outside {self._root}"
                ),
                resolved=requested,
                exists=requested.exists(),
                original=original,
            )

        # Step 3: follow symlinks. A path that does not exist still resolves --
        # strict=False keeps non-existent tails lexical rather than raising.
        try:
            real = Path(os.path.realpath(str(requested)))
        except (OSError, ValueError) as exc:
            # Fail closed: an unresolvable path is denied, not allowed.
            return Resolution(
                classification=Classification.DENY,
                rule="workspace.unresolvable",
                reason=f"path could not be resolved safely: {exc}",
                resolved=None,
                exists=False,
                original=original,
            )

        # A symlink loop makes realpath return the path unchanged on some
        # platforms rather than raising. If the lexical and real paths diverge
        # but the real path is not contained, that is an escape.
        if not self.contains(real):
            return Resolution(
                classification=Classification.DENY,
                rule="workspace.outside_symlink",
                reason=(
                    f"path resolves outside the workspace root after following "
                    f"symlinks: {original} -> {real}, outside {self._root}"
                ),
                resolved=real,
                exists=real.exists(),
                original=original,
            )

        return Resolution(
            classification=Classification.ALLOW,
            rule="workspace.contained",
            reason=f"path is inside the workspace root: {real}",
            resolved=real,
            exists=real.exists(),
            original=original,
        )


def resolve_within(
    workspace: Workspace, candidate: str | os.PathLike[str]
) -> Resolution:
    """Module-level convenience wrapper around :meth:`Workspace.resolve`."""
    return workspace.resolve(candidate)