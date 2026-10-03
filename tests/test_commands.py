"""Tests for command classification.

Covers every denied and approval-required category from
``docs/byte-security-classification.md``, plus the explicit automatic set.

The organising principle under test: a miss should produce a prompt, not silent
execution. An operation is automatic only because a rule in ``ALLOWED_PROGRAMS``
or a git/read inspector said so; anything unrecognised is gated.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from forge.tools.commands import (
    ALLOWED_PROGRAMS,
    TRUSTED_EXECUTABLE_ROOTS,
    Command,
    CommandClassifier,
)
from forge.tools.workspace import Classification, Workspace


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def classifier(workspace: Workspace) -> CommandClassifier:
    return CommandClassifier(workspace)


def _cmd(program: str, *args: str, **kw: object) -> Command:
    return Command(program=program, args=tuple(args), **kw)  # type: ignore[arg-type]


# ==========================================================================
# Denied: cwd
# ==========================================================================


def test_out_of_workspace_cwd_is_denied(
    classifier: CommandClassifier, tmp_path: Path
) -> None:
    verdict = classifier.classify(_cmd("git", "status", cwd=tmp_path))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_lexical"


def test_cwd_escaping_via_parent_is_denied(classifier: CommandClassifier) -> None:
    ws = classifier.workspace.root
    verdict = classifier.classify(_cmd("ls", cwd=ws / ".." / ".."))

    assert verdict.classification is Classification.DENY


def test_missing_cwd_defaults_to_workspace_root(
    classifier: CommandClassifier,
) -> None:
    verdict = classifier.classify(_cmd("git", "status"))

    assert verdict.classification is Classification.ALLOW
    assert verdict.cwd == classifier.workspace.root


def test_cwd_symlink_escape_is_denied(
    classifier: CommandClassifier, workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    link = workspace.root / "linked"
    link.symlink_to(outside, target_is_directory=True)

    verdict = classifier.classify(_cmd("ls", cwd=link))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_symlink"


# ==========================================================================
# Denied: privilege escalation
# ==========================================================================


@pytest.mark.parametrize("program", ["sudo", "su", "doas", "pkexec", "runuser"])
def test_privilege_escalation_is_denied(
    classifier: CommandClassifier, program: str
) -> None:
    verdict = classifier.classify(_cmd(program, "rm", "file.txt"))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.sudo"


# ==========================================================================
# Denied: raw devices and filesystem formatting
# ==========================================================================


@pytest.mark.parametrize(
    ("program", "args"),
    [
        ("mkfs", ("-t", "ext4", "/dev/sda1")),
        ("mke2fs", ("/dev/sda1",)),
        ("fdisk", ("/dev/sda",)),
        ("parted", ("/dev/sda", "mklabel", "gpt")),
        ("sgdisk", ("--zap-all", "/dev/sda")),
        ("wipefs", ("-a", "/dev/sda1")),
    ],
)
def test_device_and_format_operations_are_denied(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    verdict = classifier.classify(_cmd(program, *args))

    assert verdict.classification is Classification.DENY


def test_dd_with_if_is_denied(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("dd", "if=/dev/zero", "of=/dev/sda"))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.device_write"


def test_dd_without_if_is_not_a_device_write(classifier: CommandClassifier) -> None:
    """``dd`` within the workspace is ordinary, and should not be denied."""
    verdict = classifier.classify(
        _cmd("dd", "if=input.bin", "of=output.bin", cwd=classifier.workspace.root)
    )

    assert verdict.classification is not Classification.DENY


# ==========================================================================
# Denied: system state
# ==========================================================================


@pytest.mark.parametrize(
    "program",
    ["systemctl", "service", "launchctl", "shutdown", "reboot", "poweroff", "halt"],
)
def test_system_state_programs_are_denied(
    classifier: CommandClassifier, program: str
) -> None:
    verdict = classifier.classify(_cmd(program, "restart", "nginx"))

    assert verdict.classification is Classification.DENY


# ==========================================================================
# Denied: package installation
# ==========================================================================


@pytest.mark.parametrize(
    ("program", "args"),
    [
        ("pip", ("install", "requests")),
        ("pip3", ("install", "-r", "requirements.txt")),
        ("uv", ("pip", "install", "black")),
        ("poetry", ("add", "flask")),
        ("npm", ("install",)),
        ("npm", ("i", "lodash")),
        ("pnpm", ("add", "left-pad")),
        ("yarn", ("add", "thing")),
    ],
)
def test_language_package_installation_is_denied(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    verdict = classifier.classify(_cmd(program, *args))

    assert verdict.classification is Classification.DENY


@pytest.mark.parametrize(
    "program",
    ["apt", "apt-get", "dnf", "yum", "apk", "pacman", "snap", "brew", "port"],
)
def test_system_package_managers_are_denied(
    classifier: CommandClassifier, program: str
) -> None:
    verdict = classifier.classify(_cmd(program, "install", "something"))

    assert verdict.classification is Classification.DENY


def test_package_install_is_denied_not_gated(classifier: CommandClassifier) -> None:
    """Excluded from v1 entirely -- there is no prompt that would help."""
    verdict = classifier.classify(_cmd("pip", "install", "requests"))

    assert verdict.classification is Classification.DENY


def test_npm_run_is_not_installation(classifier: CommandClassifier) -> None:
    """``npm run build`` is development work, not package installation."""
    verdict = classifier.classify(_cmd("npm", "run", "build"))

    assert verdict.classification is not Classification.DENY


# ==========================================================================
# Denied: git history and force push
# ==========================================================================


@pytest.mark.parametrize(
    "args",
    [
        ("push", "--force"),
        ("push", "-f"),
        ("push", "--force-with-lease"),
        ("push", "origin", "main", "--force"),
    ],
)
def test_force_push_is_denied(classifier: CommandClassifier, args: tuple[str, ...]) -> None:
    verdict = classifier.classify(_cmd("git", *args))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.git_force_push"


def test_force_push_via_send_pack_is_denied(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("git", "send-pack", "--force", "origin"))

    assert verdict.classification is Classification.DENY


@pytest.mark.parametrize(
    ("args", "expected_rule"),
    [
        (("push",), "cmd.git_push"),
        (("commit", "-m", "fix"), "cmd.git_commit"),
        (("merge", "feature"), "cmd.git_merge"),
    ],
)
def test_git_write_actions_are_approval_required(
    classifier: CommandClassifier, args: tuple[str, ...], expected_rule: str
) -> None:
    verdict = classifier.classify(_cmd("git", *args))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == expected_rule


@pytest.mark.parametrize(
    "args",
    [
        ("status",),
        ("diff",),
        ("log", "--oneline"),
        ("show",),
        ("branch",),  # listing only
        ("stash", "list",),
    ],
)
def test_git_inspection_is_allowed(classifier: CommandClassifier, args: tuple[str, ...]) -> None:
    verdict = classifier.classify(_cmd("git", *args))

    assert verdict.classification is Classification.ALLOW


@pytest.mark.parametrize(
    ("args", "expected_rule"),
    [
        (("filter-branch", "--all"), "cmd.git_filter"),
        (("filter-repo", "--invert-paths"), "cmd.git_filter"),
        (("gc", "--prune=now"), "cmd.git_gc"),
        (("reflog", "expire", "--expire=now"), "cmd.git_destructive"),
    ],
)
def test_destructive_git_history_is_denied(
    classifier: CommandClassifier, args: tuple[str, ...], expected_rule: str
) -> None:
    verdict = classifier.classify(_cmd("git", *args))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == expected_rule


def test_plain_gc_requires_approval(classifier: CommandClassifier) -> None:
    """`git gc` without aggressive flags is gated, not automatic.

    `git gc --prune=now --aggressive` is denied outright by `cmd.git_gc`. Plain
    `git gc` is not on List 3's git inspection list, so it falls to the unknown
    default. It was automatic before only because nothing matched it.
    """
    verdict = classifier.classify(_cmd("git", "gc"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown"


# ==========================================================================
# Denied: credential inspection
# ==========================================================================


@pytest.mark.parametrize("program", ["env", "printenv", "set", "export"])
def test_environment_dumping_is_denied(
    classifier: CommandClassifier, program: str
) -> None:
    verdict = classifier.classify(_cmd(program))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.env_dump"


def test_aws_configure_list_is_denied(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("aws", "configure", "list"))

    assert verdict.classification is Classification.DENY


def test_git_status_is_not_credential_inspection(
    classifier: CommandClassifier
) -> None:
    """``git`` + ``status`` must not trip the ``git config``-style rules."""
    verdict = classifier.classify(_cmd("git", "status"))

    assert verdict.classification is Classification.ALLOW


# ==========================================================================
# Denied: wildcard recursive deletion
# ==========================================================================


@pytest.mark.parametrize(
    "args",
    [
        ("-rf", "*"),
        ("-rf", "./*"),
        ("-rf", "build/*"),
        ("-rf", "**/*.tmp"),
        ("-r", "src/*"),
        ("-rf", "--", "*"),
    ],
)
def test_wildcard_recursive_delete_is_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = classifier.classify(
        _cmd("rm", *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.delete_wildcard"


def test_wildcard_in_a_destructive_delete_reason_is_explicit(
    classifier: CommandClassifier,
) -> None:
    verdict = classifier.classify(_cmd("rm", "-rf", "*"))

    assert "wildcard" in verdict.reason.lower()
    assert "explicit paths" in verdict.reason


@pytest.mark.parametrize(
    "args",
    [("-rf", "build"), ("-rf", "./dist"), ("-rf", "node_modules"),
     ("-rf", "build/output"), ("-r", "a", "b")],
)
def test_explicit_recursive_delete_is_approval_required(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = classifier.classify(_cmd("rm", *args, cwd=classifier.workspace.root))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_explicit_recursive_delete_outside_workspace_is_denied(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    verdict = classifier.classify(
        _cmd("rm", "-rf", str(workspace.root.parent / "sibling"))
    )

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_lexical"


def test_non_recursive_delete_with_glob_is_denied(
    classifier: CommandClassifier,
) -> None:
    """``rm *.log`` has an equally unknown extent, so it denies too.

    The spec's reason is about the prompt being unable to show the affected
    targets, which applies to any unresolved wildcard regardless of recursion.
    """
    verdict = classifier.classify(
        _cmd("rm", "*.log", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.delete_wildcard"


def test_single_file_delete_is_gated_not_denied(
    classifier: CommandClassifier,
) -> None:
    """A named file has a knowable target, so it is a prompt rather than a refusal."""
    verdict = classifier.classify(
        _cmd("rm", "old.txt", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.delete_file"


# ==========================================================================
# Approval: interpreter inline execution
# ==========================================================================


@pytest.mark.parametrize(
    ("program", "flag"),
    [
        ("python", "-c"),
        ("python3", "-c"),
        ("pypy", "-c"),
        ("bash", "-c"),
        ("sh", "-c"),
        ("zsh", "-c"),
        ("node", "-e"),
        ("perl", "-e"),
        ("ruby", "-e"),
    ],
)
def test_inline_code_execution_is_approval_required(
    classifier: CommandClassifier, program: str, flag: str
) -> None:
    verdict = classifier.classify(_cmd(program, flag, "print('hi')"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.inline_code"


def test_inline_code_is_gated_on_form_not_content(
    classifier: CommandClassifier,
) -> None:
    """A harmless one-liner is still gated: the *form* allows anything.

    Forge does not analyse the source, and the rule says so.
    """
    harmless = classifier.classify(_cmd("python", "-c", "print(1)"))
    dangerous = classifier.classify(
        _cmd("python", "-c", "import shutil; shutil.rmtree('/')")
    )

    assert harmless.classification is Classification.APPROVAL_REQUIRED
    assert dangerous.classification is Classification.APPROVAL_REQUIRED
    assert harmless.classification is dangerous.classification


def test_running_a_script_file_is_not_inline_code(
    classifier: CommandClassifier,
) -> None:
    """A script path is not inline code, and is still gated.

    `cmd.inline_code` fires on the *form* `--code`, and `python manage.py` is
    not that form. The test asserts the rule that must not fire, rather than
    asserting a classification, so it keeps testing what it was written to test
    even though the verdict changed.
    """
    verdict = classifier.classify(_cmd("python", "manage.py", "migrate"))

    assert verdict.rule != "cmd.inline_code"
    # Running a repository script is repository-code execution.
    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_interpreter_with_module_requires_approval(
    classifier: CommandClassifier,
) -> None:
    """`python -m pytest` is a test run by another spelling.

    Also the unknown-default case: `python -m <anything-else>` is gated for
    being unrecognised, so the explicit rule exists to give the operator the
    real reason rather than "unrecognised command".
    """
    pytest_verdict = classifier.classify(_cmd("python", "-m", "pytest"))

    assert pytest_verdict.classification is Classification.APPROVAL_REQUIRED
    assert pytest_verdict.rule == "cmd.repo_runner_module"

    other_verdict = classifier.classify(_cmd("python", "-m", "http.server"))

    assert other_verdict.classification is Classification.APPROVAL_REQUIRED
    assert other_verdict.rule == "cmd.unknown"


# ==========================================================================
# Approval: network egress
# ==========================================================================


@pytest.mark.parametrize(
    "program",
    ["curl", "wget", "http", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync"],
)
def test_network_egress_is_approval_required(
    classifier: CommandClassifier, program: str
) -> None:
    verdict = classifier.classify(_cmd(program, "https://example.com"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.net_egress"


def test_curl_is_not_automatic(classifier: CommandClassifier) -> None:
    """Arbitrary shell network access is not a web interface."""
    verdict = classifier.classify(_cmd("curl", "https://example.com"))

    assert verdict.classification is not Classification.ALLOW


def test_git_fetch_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("git", "fetch", "origin"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


# ==========================================================================
# Approval: shell strings
# ==========================================================================


def test_shell_true_is_approval_required(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(Command(program="ls", shell=True))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.shell_string"


def test_shell_true_with_harmless_looking_program_is_still_gated(
    classifier: CommandClassifier,
) -> None:
    """Forge does not parse the string, so even ``ls`` is gated."""
    verdict = classifier.classify(
        Command(program="ls", args=("-la",), shell=True)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_shell_true_does_not_downgrade_a_denial(classifier: CommandClassifier) -> None:
    """A denial outranks the shell floor: strictest match wins."""
    verdict = classifier.classify(Command(program="sudo", shell=True))

    assert verdict.classification is Classification.DENY


def test_shell_false_does_not_gate(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(Command(program="ls", shell=False, args=("-la",)))

    assert verdict.classification is Classification.ALLOW


# ==========================================================================
# Approval: process killing
# ==========================================================================


def test_kill_of_owned_pid_is_allowed(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "4242"))

    assert verdict.classification is Classification.ALLOW
    assert verdict.rule == "cmd.kill_owned"


def test_kill_of_owned_pid_with_signal_is_allowed(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "-9", "4242"))

    assert verdict.classification is Classification.ALLOW


def test_kill_of_foreign_pid_is_approval_required(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "9999"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.kill_foreign"


def test_kill_with_no_owned_pids_defaults_to_gated(workspace: Workspace) -> None:
    """An empty owned set is the conservative default."""
    clf = CommandClassifier(workspace)
    verdict = clf.classify(_cmd("kill", "4242"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_kill_of_mixed_pids_is_gated(workspace: Workspace) -> None:
    """All targets must be owned; one foreign PID gates the whole call."""
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "4242", "9999"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_kill_of_pid_one_is_denied(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({1}))
    verdict = clf.classify(_cmd("kill", "1"))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.kill_init"


def test_kill_against_another_user_is_denied(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "-u", "root", "9999"))

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "cmd.kill_privileged_user"


def test_kill_with_unresolvable_target_is_gated(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({4242}))
    verdict = clf.classify(_cmd("kill", "some-target"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


@pytest.mark.parametrize("program", ["killall", "pkill"])
def test_pattern_process_kill_is_gated(
    classifier: CommandClassifier, program: str
) -> None:
    """``killall``/``pkill`` match by pattern, so ownership is unprovable."""
    verdict = classifier.classify(_cmd(program, "pytest"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


# ==========================================================================
# Approval: destructive SQL
# ==========================================================================


@pytest.mark.parametrize(
    ("program", "args", "expected_rule"),
    [
        ("psql", ("-c", "DROP DATABASE production"), "cmd.sql_destructive"),
        ("psql", ("-c", "DROP SCHEMA public CASCADE"), "cmd.sql_destructive"),
        ("psql", ("-c", "DROP TABLE users"), "cmd.sql_destructive"),
        ("psql", ("-c", "TRUNCATE TABLE events"), "cmd.sql_destructive"),
        ("mysql", ("-e", "TRUNCATE logs"), "cmd.sql_destructive"),
        ("psql", ("-c", "DELETE FROM users"), "cmd.sql_destructive"),
        ("psql", ("-c", "ALTER TABLE users ADD COLUMN x int"), "cmd.sql_destructive"),
        ("sqlite3", ("db.sqlite", "DELETE FROM records"), "cmd.sql_destructive"),
    ],
)
def test_destructive_sql_is_approval_required(
    classifier: CommandClassifier, program: str, args: tuple[str, ...], expected_rule: str
) -> None:
    verdict = classifier.classify(_cmd(program, *args))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == expected_rule


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM users",
        "DELETE FROM users WHERE id = 1",
        "INSERT INTO logs VALUES (1)",
    ],
)
def test_non_destructive_sql_is_not_flagged_as_destructive(
    classifier: CommandClassifier, statement: str
) -> None:
    """A non-destructive statement is not gated *for being destructive*.

    It is still gated, by `cmd.unknown`: database clients are not on List 3, so
    every statement reaches the unknown default. What this test pins is the rule
    that must not fire -- gating a `SELECT` as if it were a `DROP` would be
    wrong regardless of the default, and would be invisible if the test only
    asserted `requires_approval`.
    """
    verdict = classifier.classify(_cmd("psql", "-c", statement))

    assert verdict.rule not in {"cmd.sql_destructive", "cmd.sql_ambiguous"}
    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown"


def test_ambiguous_sql_is_gated_not_allowed(classifier: CommandClassifier) -> None:
    """SQL Forge cannot classify is gated, never automatic."""
    verdict = classifier.classify(_cmd("psql", "-c", "GRANT ALL ON db TO role"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.sql_ambiguous"


def test_database_client_help_is_allowed(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("psql", "--version"))

    assert verdict.classification is Classification.ALLOW
    assert verdict.rule == "allow.version_probe"


def test_select_star_is_not_destructive(classifier: CommandClassifier) -> None:
    """A read must not be gated as if it were a delete.

    Gated for being unrecognised rather than for being destructive; the rule
    assertion is what carries the meaning.
    """
    verdict = classifier.classify(_cmd("psql", "-c", "SELECT * FROM t"))

    assert verdict.rule not in {"cmd.sql_destructive", "cmd.sql_ambiguous"}
    assert verdict.rule == "cmd.unknown"


# ==========================================================================
# Approval: misc gated categories
# ==========================================================================


@pytest.mark.parametrize("program", ["chmod", "chown", "chgrp"])
def test_permission_changes_are_gated(classifier: CommandClassifier, program: str) -> None:
    verdict = classifier.classify(
        _cmd(program, "755", "script.sh", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_chmod_outside_workspace_is_denied(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("chmod", "777", "/etc/passwd"))

    assert verdict.classification is Classification.DENY


def test_truncate_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("truncate", "-s", "0", "app.log", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_sed_inplace_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("sed", "-i", "s/a/b/", "file.txt", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_docker_run_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("docker", "run", "-v", "/:/host", "ubuntu")
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_docker_system_prune_is_denied(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("docker", "system", "prune", "-a"))

    assert verdict.classification is Classification.DENY


def test_crontab_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("crontab", "-e"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_git_global_config_is_gated(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("git", "config", "--global", "user.name", "Agent")
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


# ==========================================================================
# Allow: ordinary development
# ==========================================================================


@pytest.mark.parametrize(
    "program,args",
    [
        ("ls", ("-la",)),
        ("cat", ("README.md",)),
        ("pwd", ()),
        ("wc", ("-l", "file.txt")),
        ("grep", ("-r", "pattern", "src/")),
        ("find", (".", "-name", "*.py")),
        ("head", ("-n", "10", "file.txt")),
        ("diff", ("a.txt", "b.txt")),
        ("stat", ("file.txt",)),
    ],
)
def test_listed_read_only_commands_are_allowed(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    """Every read-only form in List 3 is automatic.

    The list is the documented one and no longer than that. Commands that were
    automatic only because nothing matched -- `echo`, `sort`, `tree` -- are
    asserted as approval-required in
    `test_commands_omitted_from_list_3_are_approval_required`.
    """
    verdict = classifier.classify(
        _cmd(program, *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.ALLOW, (
        f"{program} should be allowed, got {verdict.rule}: {verdict.reason}"
    )


@pytest.mark.parametrize(
    "program,args",
    [
        # Omitted from List 3 as inconsistent rather than dangerous.
        ("tree", (".",)),
        # Omitted from List 3; `df` would report filesystems outside the
        # workspace, and `du` was left out with it.
        ("du", ("-sh", ".")),
        # Never in List 3: v1 has no use for them.
        ("which", ("git",)),
        # Never in List 3 either.
        ("echo", ("hello",)),
        ("sort", ("file.txt",)),
    ],
)
def test_commands_omitted_from_list_3_are_approval_required(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    """A command missing from List 3 is gated, not automatic.

    This is the ruling "if we are not confident it belongs in automatic, leave
    it at APPROVAL_REQUIRED" made executable. Each of these is inspection-shaped
    and none is dangerous, which is exactly why the test exists: they are the
    cases a future broadener would reach for first.
    """
    verdict = classifier.classify(
        _cmd(program, *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown", (
        f"{program} should be gated as unrecognised, got {verdict.rule}"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("--version",),
        ("--help",),
        ("-q", "tests/"),
        ("tests/test_a.py",),
    ],
)
def test_pytest_requires_approval(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Running the test suite is running repository code.

    `pytest` executes `conftest.py`, loads installed plugins, imports fixtures,
    and runs arbitrary repository-supplied test code. That is the same trust
    proposition as `./scripts/check.sh`, so it is gated the same way.

    Every argv shape is asserted, including `--version` and `--help`: the
    program-level gate applies to the program, so a version probe on a
    repository-code runner is still gated. That is deliberate -- the runner rule
    is about what the program *is*, and the read-only rules in List 3 name their
    programs individually rather than matching by category.
    """
    verdict = classifier.classify(
        _cmd("pytest", *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.repo_runner"


def test_unknown_program_is_approval_required(
    classifier: CommandClassifier,
) -> None:
    """An unrecognised argv invocation is gated, never automatic.

    This reverses the old convenience case, which classified anything unmatched
    as ALLOW and was recorded as the largest soft spot in the design: a
    classifier miss landed as silent execution rather than as a prompt.
    """
    verdict = classifier.classify(_cmd("some-unknown-tool", "--check"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown"


def test_full_path_program_is_normalized(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("/usr/bin/git", "push", "--force"))

    assert verdict.classification is Classification.DENY
    assert verdict.program == "git"


def test_program_case_is_normalized(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("GIT", "push", "--force"))

    assert verdict.classification is Classification.DENY


# ==========================================================================
# Result structure
# ==========================================================================


def test_verdict_exposes_structured_fields(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("rm", "-rf", "build", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.delete_wildcard" or verdict.rule != ""
    assert verdict.program == "rm"
    assert verdict.args == ("-rf", "build")
    assert verdict.cwd == classifier.workspace.root


def test_verdict_extracts_targets(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(
        _cmd("rm", "-rf", "build", "dist", cwd=classifier.workspace.root)
    )

    assert verdict.targets == (Path("build"), Path("dist"))
    assert verdict.targets_resolved == (
        classifier.workspace.root / "build",
        classifier.workspace.root / "dist",
    )


def test_verdict_extracts_absolute_targets(classifier: CommandClassifier) -> None:
    target = classifier.workspace.root / "file.txt"
    verdict = classifier.classify(_cmd("cat", str(target)))

    assert verdict.targets_resolved == (target,)


def test_verdict_reports_all_matched_rules(classifier: CommandClassifier) -> None:
    """A gated git push from a shell string reports both rules."""
    verdict = classifier.classify(
        Command(program="git", args=("push",), shell=True)
    )

    assert "cmd.git_push" in verdict.matched_rules
    assert "cmd.shell_string" in verdict.matched_rules
    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_verdict_reports_owned_pids(workspace: Workspace) -> None:
    clf = CommandClassifier(workspace, owned_pids=frozenset({7, 8}))
    verdict = clf.classify(_cmd("kill", "7"))

    assert verdict.owned_pids == frozenset({7, 8})
    assert verdict.allowed


def test_verdict_is_deterministic(classifier: CommandClassifier) -> None:
    first = classifier.classify(_cmd("git", "push", "--force"))
    second = classifier.classify(_cmd("git", "push", "--force"))

    assert first.rule == second.rule
    assert first.classification is second.classification


def test_verdict_string_is_operator_readable(classifier: CommandClassifier) -> None:
    rendered = str(classifier.classify(_cmd("sudo", "ls")))

    assert rendered.startswith("deny: cmd.sudo: sudo ls")


def test_command_str_is_operator_readable(classifier: CommandClassifier) -> None:
    rendered = str(_cmd("git", "status", cwd=Path("/tmp")))

    assert "git status" in rendered
    assert "shell=False" in rendered


def test_flags_expand_bundled_clusters(classifier: CommandClassifier) -> None:
    """``-rf`` must be seen as both ``-r`` and ``-f``."""
    verdict = classifier.classify(
        _cmd("rm", "-rf", "target", cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_classification_helpers_agree(classifier: CommandClassifier) -> None:
    deny = classifier.classify(_cmd("sudo", "ls"))
    gate = classifier.classify(_cmd("git", "push"))
    allow = classifier.classify(_cmd("ls"))

    assert deny.denied and not deny.allowed and not deny.requires_approval
    assert gate.requires_approval and not gate.denied
    assert allow.allowed and not allow.denied and not allow.requires_approval

# ==========================================================================
# Rules added to close gaps between the shipped tables and the spec
# ==========================================================================
#
# Each rule below existed in docs/byte-security-classification.md but had no
# matching entry in DENIED_PROGRAMS or APPROVAL_PROGRAMS, so the command fell
# through to the convenience case and classified ALLOW.
#
# The negative cases matter more than the positives here. Several of these rules
# share a program with an existing rule or with ordinary development commands, so
# a careless flag set would turn a normal command into a denial. Every addition
# is paired with the case that must stay open.


def _classify_one(
    classifier: CommandClassifier, *args: str, workspace: Workspace | None = None
):
    """Classify a single command and return its verdict."""
    return classifier.classify(Command(args[0], tuple(args[1:])))


# --------------------------------------------------------------------------
# Filesystem formatting: the mkfs.<fstype> family
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "ntfs", "fat32"])
def test_mkfs_compound_binary_is_denied(
    classifier: CommandClassifier, fstype: str
) -> None:
    """The real binaries are mkfs.ext4 and friends; only bare `mkfs` was matched."""
    verdict = _classify_one(classifier, f"mkfs.{fstype}", "/dev/sda")

    assert verdict.denied
    assert verdict.rule == "cmd.mkfs"


def test_bare_mkfs_is_still_denied(classifier: CommandClassifier) -> None:
    assert _classify_one(classifier, "mkfs", "/dev/sda").denied


@pytest.mark.parametrize("name", ["mkfsext4", "mkfoo.bar", "mk", "notmkfs.ext4"])
def test_mkfs_wildcard_does_not_over_match(
    classifier: CommandClassifier, name: str
) -> None:
    """The wildcard is a dot-prefix, not a bare prefix.

    `mkfsext4` is an unrelated program. A general `mkfs` prefix would deny it as
    filesystem formatting, which is a false positive on a name that merely
    shares leading characters.
    """
    verdict = _classify_one(classifier, name, "/dev/sda")

    assert not verdict.denied


def test_other_formatting_binaries_remain_denied(
    classifier: CommandClassifier,
) -> None:
    for program in ("mke2fs", "mkswap", "newfs"):
        assert _classify_one(classifier, program, "/dev/sda").denied, program


# --------------------------------------------------------------------------
# Destructive git history
# --------------------------------------------------------------------------


def test_git_reset_hard_is_denied(classifier: CommandClassifier) -> None:
    verdict = _classify_one(classifier, "git", "reset", "--hard")

    assert verdict.denied
    assert verdict.rule == "cmd.git_reset_hard"


def test_git_reset_hard_with_a_target_is_denied(
    classifier: CommandClassifier,
) -> None:
    assert _classify_one(classifier, "git", "reset", "--hard", "HEAD~1").denied


@pytest.mark.parametrize(
    "args",
    [("git", "reset", "--soft", "HEAD~1"), ("git", "reset", "HEAD~1"), ("git", "reset")],
)
def test_non_hard_reset_is_not_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """A soft or mixed reset is not the spec's destructive operation."""
    verdict = _classify_one(classifier, *args)

    assert not verdict.denied


@pytest.mark.parametrize("args", [("git", "clean", "-fdx"), ("git", "clean", "-fd"), ("git", "clean", "--force")])
def test_git_clean_destructive_is_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = _classify_one(classifier, *args)

    assert verdict.denied
    assert verdict.rule == "cmd.git_clean"


@pytest.mark.parametrize(
    "args",
    [("git", "clean", "-n"), ("git", "clean", "--dry-run"), ("git", "clean")],
)
def test_git_clean_dry_run_is_not_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = _classify_one(classifier, *args)

    assert not verdict.denied


@pytest.mark.parametrize("path", [".", "./"])
def test_git_checkout_discard_is_denied(
    classifier: CommandClassifier, path: str
) -> None:
    """The spec names `git checkout -- .` specifically."""
    verdict = _classify_one(classifier, "git", "checkout", "--", path)

    assert verdict.denied
    assert verdict.rule == "cmd.git_checkout_discard"


@pytest.mark.parametrize(
    "args",
    [
        ("git", "checkout", "main"),
        ("git", "checkout", "-b", "feature"),
        ("git", "checkout", "--", "file.txt"),
        ("git", "checkout"),
    ],
)
def test_ordinary_git_checkout_is_not_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Only the tree-wide discard is denied.

    A rule matching "any checkout" would deny everyday development, which is the
    over-blocking the classifier is supposed to avoid.
    """
    verdict = _classify_one(classifier, *args)

    assert not verdict.denied


@pytest.mark.parametrize("flag", ["-d", "-D"])
def test_git_branch_deletion_is_denied(
    classifier: CommandClassifier, flag: str
) -> None:
    verdict = _classify_one(classifier, "git", "branch", flag, "feature")

    assert verdict.denied
    assert verdict.rule == "cmd.git_branch_delete"


@pytest.mark.parametrize(
    "args", [("git", "branch"), ("git", "branch", "-a"), ("git", "branch", "new")]
)
def test_git_branch_without_deletion_is_not_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = _classify_one(classifier, *args)

    assert not verdict.denied


@pytest.mark.parametrize("flag", ["-d", "--delete"])
def test_git_update_ref_delete_is_denied(
    classifier: CommandClassifier, flag: str
) -> None:
    verdict = _classify_one(classifier, "git", "update-ref", flag, "refs/heads/x")

    assert verdict.denied
    assert verdict.rule == "cmd.git_update_ref_delete"


def test_git_update_ref_write_stays_approval_required(
    classifier: CommandClassifier,
) -> None:
    """Ref writes stay gated; only the delete form is denied."""
    verdict = _classify_one(classifier, "git", "update-ref", "refs/heads/x", "abc123")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.git_ref_write"


def test_git_push_delete_flag_is_denied(classifier: CommandClassifier) -> None:
    verdict = _classify_one(classifier, "git", "push", "--delete", "origin", "feature")

    assert verdict.denied
    assert verdict.rule == "cmd.git_push_delete"


def test_git_push_empty_refspec_is_denied(classifier: CommandClassifier) -> None:
    """`git push origin :branch` is the older spelling of the same deletion."""
    verdict = _classify_one(classifier, "git", "push", "origin", ":feature")

    assert verdict.denied
    assert verdict.rule == "cmd.git_push_refspec_delete"


@pytest.mark.parametrize(
    "args", [("git", "push"), ("git", "push", "origin", "main"), ("git", "push", "origin")]
)
def test_ordinary_git_push_stays_approval_required(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = _classify_one(classifier, *args)

    assert verdict.requires_approval
    assert verdict.rule == "cmd.git_push"


# --------------------------------------------------------------------------
# System administration
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("sysctl", "-w", "net.ipv4.ip_forward=1"),
        # `-p` applies a config file, which is the same host change by another
        # route. It was the one write form the spec did not name.
        ("sysctl", "-p"),
        ("sysctl", "-p", "/etc/sysctl.conf"),
        ("sysctl", "-p", "custom.conf"),
    ],
)
def test_sysctl_write_forms_are_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    verdict = _classify_one(classifier, *args)

    assert verdict.denied, args
    assert verdict.rule == "cmd.sysctl_write"


@pytest.mark.parametrize(
    "args",
    [
        ("sysctl", "-a"),
        ("sysctl", "net.ipv4.ip_forward"),
        ("sysctl", "-n", "net.ipv4.ip_forward"),
        ("sysctl"),
    ],
)
def test_sysctl_read_forms_are_not_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Reading kernel parameters is harmless.

    Every write form is denied and every read form stays open, which is what
    makes the rule a write-only gate rather than a blanket `sysctl` denial.
    """
    verdict = _classify_one(classifier, *args)

    assert not verdict.denied, args


def test_module_tools_remain_denied(classifier: CommandClassifier) -> None:
    for program in ("modprobe", "insmod", "rmmod"):
        assert _classify_one(classifier, program, "x").denied, program


def test_setfattr_is_denied(classifier: CommandClassifier) -> None:
    verdict = _classify_one(classifier, "setfattr", "-n", "user.x", "file")

    assert verdict.denied
    assert verdict.rule == "cmd.fs_repair"


def test_sc_service_control_is_denied(classifier: CommandClassifier) -> None:
    verdict = _classify_one(classifier, "sc", "stop", "nginx")

    assert verdict.denied
    assert verdict.rule == "cmd.service_sc"


@pytest.mark.parametrize(
    "program", ["systemctl", "service", "launchctl"]
)
def test_other_service_controls_remain_denied(
    classifier: CommandClassifier, program: str
) -> None:
    assert _classify_one(classifier, program, "restart", "x").denied, program


# --------------------------------------------------------------------------
# Credential inspection
# --------------------------------------------------------------------------


def test_docker_login_is_denied(classifier: CommandClassifier) -> None:
    verdict = _classify_one(
        classifier, "docker", "login", "--password", "hunter2"
    )

    assert verdict.denied
    assert verdict.rule == "cmd.credential_inspect"


@pytest.mark.parametrize(
    "args", [("aws", "configure", "list"), ("gh", "auth", "token")]
)
def test_existing_credential_inspection_stays_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """`docker login` joins the rule; the pre-existing forms are unaffected."""
    verdict = _classify_one(classifier, *args)

    assert verdict.denied, args


@pytest.mark.parametrize("args", [("docker", "ps"), ("docker", "images")])
def test_read_only_docker_commands_require_approval(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Listing containers is gated, but not as a host-mounting form.

    `cmd.container` gates `run`, `exec`, `stop`, `rm`, and `kill`. `ps` and
    `images` match none of the rules and reach the unknown default. The negative
    assertion is the point: it proves widening `cmd.credential_inspect` to
    include `login` cannot quietly widen anything else, and that these are not
    being treated as dangerous.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.rule not in {"cmd.container", "cmd.docker_system"}
    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown", args


# --------------------------------------------------------------------------
# Destructive file operations
# --------------------------------------------------------------------------


def test_shred_of_a_file_is_approval_required(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    verdict = _classify_one(classifier, "shred", "notes.txt")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.shred"


def test_shred_of_a_device_is_still_denied(
    classifier: CommandClassifier,
) -> None:
    """Device erasure was already denied and stays denied."""
    assert _classify_one(classifier, "shred", "/dev/sda").denied


def test_dd_writing_a_file_is_approval_required(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    verdict = _classify_one(classifier, "dd", "if=a", "of=b.bin")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.dd_file_write"


def test_dd_writing_a_device_is_still_denied(
    classifier: CommandClassifier,
) -> None:
    verdict = _classify_one(classifier, "dd", "if=a", "of=/dev/sda")

    assert verdict.denied
    assert verdict.rule == "cmd.device_write"


def test_dd_without_an_output_target_is_not_gated_as_a_write(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    """`dd if=x` alone writes nowhere, so the file-write gate must not fire.

    It is still gated, by `cmd.unknown`: `dd` is not on List 3. The assertion is
    on the rule rather than the verdict, because "not gated as a write" is the
    property being tested and it survives the default changing.
    """
    verdict = _classify_one(classifier, "dd", "if=notes.txt")

    assert verdict.rule != "cmd.dd_file_write"
    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.unknown"


def test_umask_is_approval_required(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    verdict = _classify_one(classifier, "umask", "077")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.chmod"


@pytest.mark.parametrize("program", ["cp", "mv", "ln"])
def test_file_move_commands_are_approval_required(
    classifier: CommandClassifier, workspace: Workspace, program: str
) -> None:
    """Arbitrary filesystem commands are gated even inside the workspace.

    The automatic-write language covers the structured `write_file` and
    `patch_file` tools, not arbitrary commands. v1 does not try to tell a safe
    `cp` from an unsafe one.
    """
    verdict = _classify_one(classifier, program, "a", "b")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.file_move"


@pytest.mark.parametrize("program", ["cp", "mv", "ln"])
def test_file_move_outside_workspace_is_still_denied_by_containment(
    classifier: CommandClassifier, tmp_path: Path, program: str
) -> None:
    """Gating the in-workspace form must not weaken the boundary check."""
    verdict = _classify_one(classifier, program, "a", str(tmp_path / "b"))

    assert verdict.denied


# --------------------------------------------------------------------------
# git rebase
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args", [("git", "rebase", "main"), ("git", "rebase", "-i", "HEAD~3")]
)
def test_git_rebase_is_approval_required(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """The spec's denial is conditional on a published ref, which a static
    classifier cannot determine, so approval is the conservative answer."""
    verdict = _classify_one(classifier, *args)

    assert verdict.requires_approval
    assert verdict.rule == "cmd.git_rebase"


# --------------------------------------------------------------------------
# Nothing was weakened
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        (("sudo", "ls"), "deny"),
        (("env",), "deny"),
        (("systemctl", "restart", "x"), "deny"),
        (("pip", "install", "x"), "deny"),
        (("npm", "install",), "deny"),
        (("docker", "system", "prune"), "deny"),
        (("rm", "-rf", "*"), "deny"),
        (("mkfs.ext4", "/dev/sda"), "deny"),
        (("git", "push", "--force"), "deny"),
        (("git", "filter-repo"), "deny"),
        (("iptables", "-F"), "deny"),
        (("useradd", "bob"), "deny"),
    ],
)
def test_existing_denials_are_preserved(
    classifier: CommandClassifier, args: tuple[str, ...], expected: str
) -> None:
    """This pass only ever added rules; nothing was loosened."""
    verdict = _classify_one(classifier, *args)

    assert verdict.classification.value == expected


def test_docker_system_prune_remains_denied_not_downgraded(
    classifier: CommandClassifier,
) -> None:
    """A stricter existing denial must not be weakened to match the spec table.

    The spec lists `docker system prune` under approval-required, but the shipped
    rule denies it. That over-blocking predates this pass and is left alone.
    """
    verdict = _classify_one(classifier, "docker", "system", "prune")

    assert verdict.denied


# ==========================================================================
# Workspace-local executables
#
# Implemented rule: an executable that *resolves* inside the workspace is
# approval-required. The test is where the resolved path lands, not whether the
# token contains a separator -- `/usr/bin/git` contains one and is ordinary.
# ==========================================================================


@pytest.mark.parametrize(
    "program",
    ["./configure", "./tool", "./scripts/check.sh", "./build"],
)
def test_relative_workspace_executable_is_approval_required(
    classifier: CommandClassifier, program: str
) -> None:
    """`./configure` and `./scripts/check.sh` are repository code by another name.

    A repository is attacker-reachable input, so living inside the workspace is
    not evidence of safety. This is the same argument that gates
    `cmd.inline_code`, kept consistent on purpose.
    """
    verdict = _classify_one(classifier, program)

    assert verdict.requires_approval
    assert verdict.rule == "cmd.workspace_executable"


def test_absolute_workspace_executable_is_approval_required(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    """An absolute path into the workspace is gated the same way."""
    inside = workspace.root / "bin" / "lint"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("#!/bin/sh\n")

    verdict = _classify_one(classifier, str(inside))

    assert verdict.requires_approval
    assert verdict.rule == "cmd.workspace_executable"


@pytest.mark.parametrize(
    "args",
    [
        ("/usr/bin/git", "status"),
        ("/usr/bin/python", "--version"),
        ("/usr/bin/ls", "-la"),
        ("/bin/cat", "README.md"),
    ],
)
def test_system_path_program_is_not_treated_as_workspace_executable(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """A system path contains a "/" and is not a workspace-local executable.

    The negative case that matters most: a rule keyed on "the token contains a
    slash" would gate all four of these, and `/usr/bin/git status` would stop
    being the automatic inspection the spec says it is.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.rule != "cmd.workspace_executable", args


def test_absolute_workspace_path_behaves_like_the_relative_one(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    """`/usr/bin/git status` classifies identically to `git status`.

    Path normalisation for classification, proven end to end: the same
    inspection operation is automatic whichever spelling is used.
    """
    bare = classifier.classify(_cmd("git", "status"))
    full = classifier.classify(_cmd("/usr/bin/git", "status"))

    assert bare.classification is Classification.ALLOW
    assert full.classification is Classification.ALLOW
    assert full.classification is bare.classification


def test_relative_workspace_executable_outside_workspace_is_denied(
    classifier: CommandClassifier, tmp_path: Path
) -> None:
    """A relative token escaping the workspace is denied, not gated.

    Containment cannot catch this on its own: the program token is not a path
    argument, so `_extract_targets` never sees it. Without an explicit check
    here `../tool` would reach the unknown default and be *gated* -- an operator
    could approve running an executable outside the workspace root, which the
    spec forbids unconditionally.
    """
    outside = tmp_path / "tool"
    outside.write_text("#!/bin/sh\n")
    relative = f"../{outside.parent.name}/{outside.name}"

    verdict = _classify_one(classifier, relative)

    assert verdict.denied
    assert verdict.rule == "cmd.workspace_executable_outside"


@pytest.mark.parametrize(
    "token",
    [
        "/tmp/evil-tool",
        "/home/user/random-tool",
        "/opt/random/tool",
        "/var/tmp/tool",
    ],
)
def test_absolute_executable_outside_trusted_roots_is_denied(
    classifier: CommandClassifier, token: str
) -> None:
    """An arbitrary external executable is denied, not gated.

    These are absolute paths outside the workspace and outside every trusted
    system root. Gating them would put a prompt in front of an operator who can
    be shown nothing useful: the file is arbitrary, external, and not something
    Forge's fixed PATH could launch either. A prompt with nothing to evaluate is
    the "prompt without information" failure the secret-source principle
    rejects.
    """
    verdict = _classify_one(classifier, token)

    assert verdict.denied
    assert verdict.rule == "cmd.workspace_executable_outside"


def test_trusted_executable_root_is_the_executor_search_path(
    classifier: CommandClassifier,
) -> None:
    """The trusted roots and the Executor's search path must not drift apart.

    `TRUSTED_EXECUTABLE_ROOTS` is duplicated as text in `commands.py` rather
    than imported, because `execution.py` imports the approval layer which
    imports `commands.py` and an import would be circular. This test is what
    makes the duplication safe: if someone changes the Executor's PATH without
    changing the roots, this fails.

    The reasoning behind sharing one source: Forge cannot launch a program
    outside its fixed search path, so a program outside it is a program Forge
    does not trust. Two independent lists would be free to disagree.
    """
    from forge.tools.execution import EXECUTABLE_SEARCH_PATH

    search_roots = tuple(EXECUTABLE_SEARCH_PATH.split(":"))

    assert TRUSTED_EXECUTABLE_ROOTS == search_roots


@pytest.mark.parametrize(
    "token",
    [
        "/usr/bin/git",
        "/usr/bin/ls",
        "/usr/bin/python3",
        "/bin/cat",
        "/bin/sh",
        "/usr/local/bin/tool",
    ],
)
def test_trusted_system_executable_is_not_denied_by_the_root_check(
    classifier: CommandClassifier, token: str
) -> None:
    """A system executable keeps normal classification.

    Denying `/usr/bin/git` would make every absolute system invocation
    unusable, and the spec names `/usr/bin/git` and `/usr/bin/python` as
    ordinary system programs.
    """
    verdict = _classify_one(classifier, token)

    assert verdict.rule != "cmd.workspace_executable_outside", token


def test_symlink_escape_from_a_trusted_root_fails_closed(
    classifier: CommandClassifier, tmp_path: Path
) -> None:
    """An apparent `/usr/bin/...` symlink pointing outside is denied.

    Without realpath resolution the check would pass on the *apparent*
    location and an operator could approve an arbitrary external executable
    wearing a trusted path. The check resolves first and trusts the real
    location, so this denies.
    """
    outside = tmp_path / "evil-tool"
    outside.write_text("#!/bin/sh\n")

    # A symlink in the workspace, pointing at an external file. The workspace
    # check runs first and denies it on the real location; the trusted-root
    # check is not reached and must not be relied on to save us.
    link = classifier.workspace.root / "tool"
    link.symlink_to(outside)

    verdict = _classify_one(classifier, str(link))

    assert verdict.denied


def test_symlink_in_a_trusted_root_pointing_outside_is_denied(
    classifier: CommandClassifier, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink planted in a trusted root cannot smuggle a file out of it.

    This is the case the realpath resolution exists for: the token's apparent
    location is inside a trusted root, so a *lexical* check would classify it
    normally, but its real location is outside every root. The check trusts the
    real path.

    The trusted root is a temporary directory rather than `/usr/local/bin`, so
    the test needs no root, mutates no host path, and cannot skip. The module
    constant is monkeypatched for this test only; the product behaviour is
    unchanged, and `_in_trusted_executable_root` reads the constant at call time.
    """
    trusted_root = tmp_path / "trusted-bin"
    trusted_root.mkdir()

    outside = tmp_path / "evil-tool"
    outside.write_text("#!/bin/sh\n")

    link = trusted_root / "tool"
    link.symlink_to(outside)

    monkeypatch.setattr(
        "forge.tools.commands.TRUSTED_EXECUTABLE_ROOTS", (str(trusted_root),)
    )

    verdict = _classify_one(classifier, str(link))

    assert verdict.denied
    assert verdict.rule == "cmd.workspace_executable_outside"


def test_real_executable_in_a_temporary_trusted_root_is_not_denied(
    classifier: CommandClassifier, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for the test above.

    Without this, the symlink test could pass for the wrong reason -- the file
    being denied as a path rather than as a symlink escape. A real executable
    that genuinely lives in the trusted root must classify normally.
    """
    trusted_root = tmp_path / "trusted-bin"
    trusted_root.mkdir()

    tool = trusted_root / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)

    monkeypatch.setattr(
        "forge.tools.commands.TRUSTED_EXECUTABLE_ROOTS", (str(trusted_root),)
    )

    verdict = _classify_one(classifier, str(tool))

    assert verdict.rule != "cmd.workspace_executable_outside"


def test_unresolvable_path_is_not_treated_as_trusted(
    classifier: CommandClassifier, tmp_path: Path
) -> None:
    """A path that cannot be resolved is not trusted; the check fails closed.

    Consistent with `Workspace.resolve`, which denies rather than allows on
    every resolution failure. An unresolvable absolute path outside the
    workspace must not be granted the trusted-root exemption.
    """
    verdict = _classify_one(
        classifier, str(tmp_path / "no" / "such" / "dir" / "tool")
    )

    assert verdict.denied


def test_project_local_venv_interpreter_is_approval_required(
    classifier: CommandClassifier, workspace: Workspace
) -> None:
    """`<workspace>/.venv/bin/python` is repository-local, so it is gated.

    This is the case that makes the three interpreter categories distinct. A
    project venv lives inside the workspace, so it is repository code by the
    workspace-trust argument -- approval-required, exactly like
    `./scripts/check.sh`. It is emphatically not denied, and not treated as a
    trusted system binary either.
    """
    interpreter = workspace.root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)

    verdict = _classify_one(classifier, str(interpreter))

    assert verdict.requires_approval
    assert verdict.rule == "cmd.workspace_executable"


def test_the_three_interpreter_categories_are_distinguished(
    classifier: CommandClassifier, workspace: Workspace, tmp_path: Path
) -> None:
    """Project-local, trusted system, and arbitrary external differ.

    All three end at a *different* place, which is the property worth pinning:

    | Interpreter | Lands in | Verdict |
    | --- | --- | --- |
    | `<workspace>/.venv/bin/python` | inside the workspace | `cmd.workspace_executable` (approval) |
    | `/usr/bin/python3` | a trusted system root | normal program rules |
    | `<tmp>/elsewhere/python` | neither | `cmd.workspace_executable_outside` (deny) |

    Pinning them in one test makes the contrast explicit and stops a future
    change from collapsing two categories into one.
    """
    project_interpreter = workspace.root / "venv" / "bin" / "python"
    project_interpreter.parent.mkdir(parents=True, exist_ok=True)
    project_interpreter.write_text("#!/bin/sh\n")
    project_interpreter.chmod(0o755)

    external = tmp_path / "elsewhere" / "python"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("#!/bin/sh\n")
    external.chmod(0o755)

    local_verdict = _classify_one(classifier, str(project_interpreter))
    system_verdict = _classify_one(classifier, "/usr/bin/python3")
    external_verdict = _classify_one(classifier, str(external))

    assert local_verdict.rule == "cmd.workspace_executable"
    assert system_verdict.rule != "cmd.workspace_executable"
    assert system_verdict.rule != "cmd.workspace_executable_outside"
    assert external_verdict.rule == "cmd.workspace_executable_outside"
    assert external_verdict.denied


def test_nested_workspace_executable_is_approval_required(
    classifier: CommandClassifier,
) -> None:
    """A `./`-prefixed token deeper in the tree is gated too."""
    verdict = _classify_one(classifier, "./scripts/nested/run-checks")

    assert verdict.requires_approval
    assert verdict.rule == "cmd.workspace_executable"


# ==========================================================================
# Git operation granularity
#
# `git branch`, `git tag`, and `git stash` are not one permission level each.
# ==========================================================================


@pytest.mark.parametrize(
    "args",
    [
        ("git", "status"),
        ("git", "status", "--short"),
        ("git", "diff"),
        ("git", "diff", "--cached"),
        ("git", "diff", "--stat"),
        ("git", "log"),
        ("git", "log", "--oneline", "-5"),
        ("git", "show"),
        ("git", "show", "HEAD"),
        ("git", "rev-parse", "HEAD"),
        ("git", "ls-files"),
    ],
)
def test_git_inspection_is_automatic(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Every List 3 git inspection form is automatic."""
    verdict = _classify_one(classifier, *args)

    assert verdict.classification is Classification.ALLOW, (
        f"{' '.join(args)} should be automatic, got {verdict.rule}"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("git", "branch"),
        ("git", "branch", "--list"),
        ("git", "branch", "-a", "-v"),
        ("git", "tag"),
        ("git", "tag", "--list"),
        ("git", "tag", "-l"),
        ("git", "stash", "list"),
    ],
)
def test_git_listing_forms_are_automatic(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """The listing form of each family is automatic.

    Each of these shares a program with a gated mutating form. The pairing with
    `test_git_mutation_forms_are_approval_required` is the point: neither half
    of the test would catch a rule that collapsed the two.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.classification is Classification.ALLOW, (
        f"{' '.join(args)} should be automatic, got {verdict.rule}"
    )


@pytest.mark.parametrize(
    "args",
    [
        ("git", "add", "."),
        ("git", "add", "-A"),
        ("git", "add", "src/"),
        ("git", "branch", "feature"),
        ("git", "branch", "-c", "feature"),
        ("git", "switch", "main"),
        ("git", "switch", "-c", "feature"),
        ("git", "tag", "v1.0.0"),
        ("git", "tag", "-a", "v1.0.0", "-m", "release"),
        ("git", "stash"),
        ("git", "stash", "push"),
        ("git", "stash", "pop"),
        ("git", "stash", "drop"),
        ("git", "stash", "clear"),
        ("git", "commit", "-m", "x"),
        ("git", "merge", "main"),
        ("git", "push"),
        ("git", "push", "origin", "main"),
        ("git", "fetch"),
        ("git", "pull"),
        ("git", "clone", "url"),
        ("git", "rebase", "main"),
    ],
)
def test_git_mutation_forms_are_approval_required(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Every mutating git form is gated, none of them denied.

    `git add` is the one write that never touches the network, so its gate is
    about the operator reviewing what belongs in a commit rather than about
    publication. `git branch`/`tag`/`stash` are here as the mutating half of
    their automatic listing forms.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.requires_approval, f"{' '.join(args)}: {verdict.rule}"
    assert not verdict.denied, f"{' '.join(args)} must not be denied"


@pytest.mark.parametrize(
    "args,expected_rule",
    [
        (("git", "branch", "-d", "feature"), "cmd.git_branch_delete"),
        (("git", "branch", "-D", "feature"), "cmd.git_branch_delete"),
        (("git", "tag", "-d", "v1.0.0"), "cmd.git_tag_delete"),
    ],
)
def test_git_branch_and_tag_deletion_are_denied(
    classifier: CommandClassifier, args: tuple[str, ...], expected_rule: str
) -> None:
    """Deleting a branch or a tag is the same operation under two nouns.

    Denying one while approving the other would be arbitrary, so `git tag -d`
    joins `git branch -d` in the denied set.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.denied
    assert verdict.rule == expected_rule


@pytest.mark.parametrize(
    "args",
    [
        ("git", "describe"),
        ("git", "config", "--get", "user.name"),
        ("git", "remote", "-v"),
        ("git", "gc"),
        ("git", "gc", "--prune=now", "--aggressive"),
    ],
)
def test_git_forms_omitted_from_list_3_are_gated_or_denied(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Git commands left out of List 3 are gated, or denied where already denied.

    `git describe`, `git config --get`, and `git remote -v` are reads the spec
    chose not to list, so each one costs a prompt. That is the ruling applied
    literally: no entry means approval-required, not automatic.
    """
    verdict = _classify_one(classifier, *args)

    assert not verdict.classification is Classification.ALLOW, args


# ==========================================================================
# Repository-code runners
# ==========================================================================


@pytest.mark.parametrize(
    "args",
    [
        ("pytest",),
        ("pytest", "-q"),
        ("pytest", "tests/", "-q"),
        ("pytest", "tests/test_a.py::test_x"),
        ("python", "-m", "pytest"),
        ("python3", "-m", "pytest"),
        ("make",),
        ("make", "test"),
        ("make", "build"),
        ("npm", "test"),
        ("npm", "run", "build"),
        ("npm", "run", "test:unit"),
        ("pnpm", "test"),
        ("yarn", "test"),
        ("cargo", "test"),
        ("cargo", "build"),
        ("cargo", "run"),
        ("go", "test"),
        ("go", "test", "./..."),
        ("go", "build", "./..."),
        ("tox",),
        ("nox",),
    ],
)
def test_repository_code_runners_require_approval(
    classifier: CommandClassifier, args: tuple[str, ...]
) -> None:
    """Running repository code is approval-required.

    `pytest` executes `conftest.py`, loads plugins, and runs arbitrary
    repository-supplied test code. Auto-approving it while gating
    `./scripts/check.sh` would be incoherent, so every documented runner is
    gated here explicitly rather than by falling through to the default.
    """
    verdict = _classify_one(classifier, *args)

    assert verdict.requires_approval, (
        f"{' '.join(args)} should require approval, got {verdict.rule}"
    )
    assert not verdict.denied, f"{' '.join(args)} must not be denied"


def test_reading_a_test_file_is_not_running_repository_code(
    classifier: CommandClassifier,
) -> None:
    """The gate is on executing repository code, not on looking at it.

    Without this, a future reader could reasonably conclude that test files
    became sensitive, which they are not.
    """
    verdict = _classify_one(classifier, "cat", "tests/test_commands.py")

    assert verdict.classification is Classification.ALLOW


# ==========================================================================
# Architecture guard: the default must never become ALLOW again
# ==========================================================================


def test_unmatched_fallback_is_not_allow() -> None:
    """The unmatched fallback is APPROVAL_REQUIRED, and that must not regress.

    This is the guard against default-allow returning. It exists because that
    was the previous behaviour, the spec recorded it as the largest soft spot in
    the design, and a future refactor of `classify` could reintroduce it
    silently: every other test in this file would still pass, because they
    assert specific commands rather than the shape of the fallback.

    It checks the fallback structurally rather than by classifying one program,
    so it holds even if `cmd.unknown` is later given a different rule id.
    """
    source = inspect.getsource(CommandClassifier.classify)

    assert "_classify_unknown_program" in source, (
        "classify() must apply the unknown-command default"
    )
    # The default is applied by _classify_unknown_program, which returns
    # APPROVAL_REQUIRED. Guard the helper too, so inlining the logic back into
    # classify() without changing the classification cannot pass this test.
    helper = inspect.getsource(CommandClassifier._classify_unknown_program)
    assert "Classification.APPROVAL_REQUIRED" in helper
    assert "Classification.ALLOW" not in helper


def test_every_allow_rule_is_explicitly_classified_allow() -> None:
    """No entry in ALLOWED_PROGRAMS can quietly claim a non-automatic result.

    Guards the table itself: a rule added with the wrong classification would
    raise an operation's severity silently rather than failing here.
    """
    for rule in ALLOWED_PROGRAMS:
        assert rule.classification is Classification.ALLOW, rule.rule_id
        assert rule.reason, f"{rule.rule_id} has no reason"


def test_allowed_table_is_small_and_deliberate() -> None:
    """The automatic table stays small on purpose.

    The ruling was "if we are not confident it belongs in automatic, leave it
    at APPROVAL_REQUIRED", and it explicitly forbids broadening the list to
    reduce prompt count. A guard on the *count* is the only thing that makes
    that stick: individual rows are each defensible, and without a ceiling the
    table grows one "obviously safe" addition at a time until it is the
    blocklist the design replaced.

    Raise this deliberately, with a review, if a genuinely new read-only form
    needs covering.
    """
    assert len(ALLOWED_PROGRAMS) <= 3, (
        "ALLOWED_PROGRAMS grew past the reviewed size; widening it needs an "
        "explicit decision, not a convenience"
    )

