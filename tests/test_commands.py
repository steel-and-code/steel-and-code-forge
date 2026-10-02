"""Tests for command classification.

Covers every denied and approval-required category from
``docs/byte-security-classification.md``, plus clean allow cases.

The organising principle under test: a miss should produce a prompt, not silent
execution. Where a rule is not recognised, the command is gated rather than
allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.tools.commands import Command, CommandClassifier
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


def test_plain_gc_is_allowed(classifier: CommandClassifier) -> None:
    """``git gc`` without aggressive flags is ordinary maintenance."""
    verdict = classifier.classify(_cmd("git", "gc"))

    assert verdict.classification is Classification.ALLOW


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


def test_running_a_script_file_is_not_inline_code(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("python", "manage.py", "migrate"))

    assert verdict.classification is not Classification.APPROVAL_REQUIRED


def test_interpreter_with_module_is_allowed(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("python", "-m", "pytest"))

    assert verdict.classification is not Classification.APPROVAL_REQUIRED


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
def test_non_destructive_sql_is_allowed(
    classifier: CommandClassifier, statement: str
) -> None:
    verdict = classifier.classify(_cmd("psql", "-c", statement))

    assert verdict.classification is not Classification.APPROVAL_REQUIRED


def test_ambiguous_sql_is_gated_not_allowed(classifier: CommandClassifier) -> None:
    """SQL Forge cannot classify is gated, never automatic."""
    verdict = classifier.classify(_cmd("psql", "-c", "GRANT ALL ON db TO role"))

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "cmd.sql_ambiguous"


def test_database_client_help_is_allowed(classifier: CommandClassifier) -> None:
    verdict = classifier.classify(_cmd("psql", "--version"))

    assert verdict.classification is not Classification.APPROVAL_REQUIRED


def test_select_star_is_not_destructive(classifier: CommandClassifier) -> None:
    """A read must not be gated as if it were a delete."""
    verdict = classifier.classify(_cmd("psql", "-c", "SELECT * FROM t"))

    assert verdict.classification is not Classification.APPROVAL_REQUIRED


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
    ("program", "args"),
    [
        ("ls", ("-la",)),
        ("cat", ("README.md",)),
        ("echo", ("hello",)),
        ("pwd", ()),
        ("wc", ("-l", "file.txt")),
        ("grep", ("-r", "pattern", "src/")),
        ("find", (".", "-name", "*.py")),
        ("head", ("-n", "10", "file.txt")),
        ("sort", ("file.txt",)),
        ("diff", ("a.txt", "b.txt")),
        ("tree", (".",)),
        ("stat", ("file.txt",)),
    ],
)
def test_ordinary_development_commands_are_allowed(
    classifier: CommandClassifier, program: str, args: tuple[str, ...]
) -> None:
    verdict = classifier.classify(
        _cmd(program, *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.ALLOW, (
        f"{program} should be allowed, got {verdict.rule}: {verdict.reason}"
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
def test_pytest_is_allowed(classifier: CommandClassifier, args: tuple[str, ...]) -> None:
    verdict = classifier.classify(
        _cmd("pytest", *args, cwd=classifier.workspace.root)
    )

    assert verdict.classification is Classification.ALLOW


def test_unknown_program_is_allowed_not_gated(
    classifier: CommandClassifier,
) -> None:
    """Unrecognised but harmless programs stay automatic.

    This is the convenience case, and the spec records it as the largest soft
    spot in the design. It is correct for v1 and is a known trade.
    """
    verdict = classifier.classify(_cmd("some-unknown-tool", "--check"))

    assert verdict.classification is Classification.ALLOW


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