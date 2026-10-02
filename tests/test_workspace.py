"""Tests for workspace resolution and containment.

Covers the v1 workspace boundary decision: one explicit root, ``..`` and
symlinks resolved before the check, anything outside denied, fail closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.tools.workspace import Classification, Workspace, strictest


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    """A workspace root with a nested directory already created."""
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    return Workspace(root)


# -- the explicitly required cases ----------------------------------------


def test_normal_file_inside_workspace_is_allowed(workspace: Workspace) -> None:
    target = workspace.root / "README.md"
    target.write_text("hello", encoding="utf-8")

    result = workspace.resolve(target)

    assert result.classification is Classification.ALLOW
    assert result.allowed
    assert result.resolved == target
    assert result.exists


def test_parent_escape_is_denied(workspace: Workspace) -> None:
    escape = workspace.root / ".." / "outside.txt"

    result = workspace.resolve(escape)

    assert result.classification is Classification.DENY
    assert not result.allowed
    assert result.rule == "workspace.outside_lexical"
    assert result.resolved is not None
    assert not result.resolved.is_relative_to(workspace.root)


def test_deep_parent_escape_is_denied(workspace: Workspace) -> None:
    escape = workspace.root / "src" / ".." / ".." / ".." / "etc" / "passwd"

    result = workspace.resolve(escape)

    assert result.classification is Classification.DENY


def test_symlink_resolving_outside_workspace_is_denied(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_target.txt"
    outside.write_text("secret location", encoding="utf-8")

    link = workspace.root / "innocent_looking.txt"
    link.symlink_to(outside)

    result = workspace.resolve(link)

    assert result.classification is Classification.DENY
    assert result.rule == "workspace.outside_symlink"
    assert result.resolved == outside


def test_symlinked_parent_directory_escape_is_denied(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A symlinked directory that points outside the workspace root."""
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "target.txt").write_text("x", encoding="utf-8")

    link_dir = workspace.root / "linked_dir"
    link_dir.symlink_to(outside_dir, target_is_directory=True)

    result = workspace.resolve(link_dir / "target.txt")

    assert result.classification is Classification.DENY
    assert result.rule == "workspace.outside_symlink"


def test_symlink_inside_workspace_is_allowed(workspace: Workspace) -> None:
    real = workspace.root / "src" / "real.txt"
    real.write_text("x", encoding="utf-8")

    link = workspace.root / "link.txt"
    link.symlink_to(real)

    result = workspace.resolve(link)

    assert result.classification is Classification.ALLOW
    assert result.resolved == real


# -- containment edge cases -----------------------------------------------


def test_workspace_root_itself_is_contained(workspace: Workspace) -> None:
    assert workspace.contains(workspace.root)


def test_prefix_sibling_is_not_contained(workspace: Workspace, tmp_path: Path) -> None:
    """``/work`` must not contain ``/workspace-evil``.

    A string-prefix comparison would wrongly allow this.
    """
    sibling = tmp_path / "workspace-evil"
    sibling.mkdir()
    evil = sibling / "file.txt"
    evil.write_text("x", encoding="utf-8")

    assert not workspace.contains(evil)
    assert workspace.resolve(evil).classification is Classification.DENY


def test_non_existent_path_inside_workspace_is_allowed(workspace: Workspace) -> None:
    """``rm -rf build`` must be contained even when ``build`` is absent."""
    result = workspace.resolve(workspace.root / "build")

    assert result.classification is Classification.ALLOW
    assert result.exists is False
    assert result.resolved == workspace.root / "build"


def test_non_existent_path_outside_workspace_is_denied(workspace: Workspace) -> None:
    result = workspace.resolve(workspace.root.parent / "nope.txt")

    assert result.classification is Classification.DENY


def test_root_resolves_symlinked_workspace(workspace: Workspace) -> None:
    """A workspace named via a symlink compares equal to the real path."""
    aliased = workspace.root.parent / "alias"
    aliased.symlink_to(workspace.root, target_is_directory=True)

    other = Workspace(aliased)

    assert other.root == workspace.root
    assert other.resolve(workspace.root / "file.txt").allowed


def test_relative_path_is_resolved_against_cwd(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(workspace.root)
    (workspace.root / "here.txt").write_text("x", encoding="utf-8")

    result = workspace.resolve(Path("here.txt"))

    assert result.classification is Classification.ALLOW


def test_unresolvable_path_fails_closed(workspace: Workspace) -> None:
    """A path Forge cannot resolve is denied, not allowed."""
    # A NUL byte makes the path unresolvable at the OS level.
    result = workspace.resolve("bad\x00path.txt")

    assert result.classification is Classification.DENY


# -- strictest-match helper ------------------------------------------------


def test_strictest_prefers_deny() -> None:
    result = strictest(
        Classification.ALLOW, Classification.APPROVAL_REQUIRED, Classification.DENY
    )
    assert result is Classification.DENY


def test_strictest_prefers_approval_over_allow() -> None:
    assert (
        strictest(Classification.ALLOW, Classification.APPROVAL_REQUIRED)
        is Classification.APPROVAL_REQUIRED
    )


def test_strictest_of_nothing_is_allow() -> None:
    assert strictest() is Classification.ALLOW


def test_classification_compares_equal_to_its_value() -> None:
    """Verdicts serialise cleanly, which the approval and HTTP surfaces need."""
    assert Classification.DENY == "deny"
    assert Classification.APPROVAL_REQUIRED == "approval_required"


def test_workspace_root_property_is_resolved(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    assert Workspace(root).root == Path(os.path.realpath(root))