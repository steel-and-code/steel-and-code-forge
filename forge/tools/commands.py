"""Command classification.

Implements List 2 of ``docs/byte-security-classification.md``: which
structured command invocations are automatic, which need a human, and which are
refused outright.

Governing rules, from the spec:

    Out of workspace          -> deny
    Structural danger         -> deny   (root, devices, history rewrite, secrets)
    Known-but-gated           -> approval_required
    Unrecognised or ambiguous -> approval_required, never automatic
    Anything ordinary         -> allow

Two properties this module holds to:

*   **It classifies, it does not execute.** Nothing here runs a subprocess.
*   **It does not parse shell.** A ``shell=True`` invocation is gated on sight.
    Partial parsing that reports "safe" is worse than no parsing.

The classifier consumes a structured ``Command`` — program, args, cwd, shell
flag — and returns a ``CommandVerdict`` carrying enough detail for an approval
prompt to render without re-running any of these checks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import Classification, Workspace, strictest

__all__ = [
    "Command",
    "CommandVerdict",
    "CommandClassifier",
    "ProgramRule",
    "DENIED_PROGRAMS",
    "APPROVAL_PROGRAMS",
    "ALLOWED_PROGRAMS",
    "WILDCARD_CHARACTERS",
]

# Characters that make a target unenumerable before execution. A destructive
# operation containing any of these is denied: an approval prompt cannot show an
# operator what a glob will expand to, so it cannot be meaningfully approved.
WILDCARD_CHARACTERS = ("*", "?", "[", "]", "{", "}")

# Flags that take inline source code. Matched on the flag *and* the program
# being an interpreter, so `-c` on some unrelated tool is not misread.
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-E", "--command"})

# Programs whose non-flag arguments are filesystem paths. Used to decide which
# arguments are path subjects for containment checking.
PATH_TAKING_PROGRAMS = frozenset(
    {
        "rm", "rmdir", "mv", "cp", "install", "ln", "touch", "mkdir",
        "chmod", "chown", "chgrp", "truncate", "dd", "shred", "cat",
        "head", "tail", "less", "more", "file", "stat", "realpath",
        "readlink", "tree", "find", "grep", "rg", "sed", "awk", "sort",
        "wc", "ls", "du", "diff", "patch", "tar", "zip", "unzip",
    }
)

# Programs that perform a recursive delete when given a recursive flag.
RECURSIVE_DELETE_PROGRAMS = frozenset({"rm", "rmdir", "shred"})


@dataclass(frozen=True)
class Command:
    """A structured command invocation.

    Args:
        program: The executable. May be a bare name (``"git"``) or a path
            (``"/usr/bin/git"``); the basename is used for matching.
        args: Arguments, excluding ``program``. Wildcard characters are
            detected here and never expanded.
        cwd: Working directory. Resolved against the workspace before any other
            check, because a bad ``cwd`` makes every path argument meaningless.
        shell: ``True`` when this will be handed to a shell as a single string.
            Forge does not parse it; the invocation is gated on sight.
        env_keys: Names only, never values. Present so a caller can confirm an
            allowlisted environment is in play without this module ever reading
            a credential value.
    """

    program: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    shell: bool = False
    env_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def normalized_program(self) -> str:
        """The program's basename, lowercased.

        Matching on basename rather than the full path is a known limitation:
        ``rm`` on the host may not be ``/bin/rm``. Recorded in the spec's open
        items.
        """
        return Path(self.program).name.casefold()

    def __str__(self) -> str:  # pragma: no cover - operator convenience
        parts = " ".join([self.normalized_program, *self.args])
        return f"{parts} (cwd={self.cwd}, shell={self.shell})"


@dataclass(frozen=True)
class ProgramRule:
    """A classification rule for a program and, optionally, its subcommand.

    Attributes:
        rule_id: Stable identifier surfaced in verdicts.
        reason: Human-readable explanation, safe for an operator.
        classification: Result when this rule matches.
        programs: Program basenames this applies to. ``"*"`` means any.
        subcommands: Subcommands or first non-flag arguments. ``None`` means
            the rule matches on the program alone. Matched against the first
            non-flag argument, which is how git subcommands arrive.
        flags: Long or short flags that must be present.
        all_of_flags: When ``True``, *every* flag listed must be present for a
            match. When ``False`` (default), any one is enough.
        any_subcommand: When ``True``, a match requires *any* non-flag argument
            to be in ``subcommands`` rather than the first one. Needed for
            programs with sub-subcommands (``uv pip install``), kept as data so
            the matcher needs no program-specific branches.
        args_contain: Literal non-flag tokens that must appear in the
            arguments. For forms where the operative word is positional rather
            than a flag (``git reflog expire``).
    """

    rule_id: str
    reason: str
    classification: Classification
    programs: frozenset[str]
    subcommands: frozenset[str] | None = None
    flags: frozenset[str] = frozenset()
    all_of_flags: bool = False
    any_subcommand: bool = False
    args_contain: frozenset[str] = frozenset()


def _deny(
    rule_id: str, reason: str, programs: frozenset[str], **kw: object
) -> ProgramRule:
    return ProgramRule(rule_id, reason, Classification.DENY, programs, **kw)  # type: ignore[arg-type]


def _gate(
    rule_id: str, reason: str, programs: frozenset[str], **kw: object
) -> ProgramRule:
    return ProgramRule(
        rule_id, reason, Classification.APPROVAL_REQUIRED, programs, **kw  # type: ignore[arg-type]
    )


_GIT = frozenset({"git"})
_ANY = frozenset({"*"})

# --------------------------------------------------------------------------
# Denied. Root, devices, filesystem destruction, history rewrite, system state,
# package installation, and secret inspection.
# --------------------------------------------------------------------------
_DEVICE_PATH = re.compile(r"^/dev/(sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|xvd[a-z]|disk\d|mmcblk\d|loop\d|md\d)", re.IGNORECASE)

DENIED_PROGRAMS: tuple[ProgramRule, ...] = (
    # Privilege escalation
    _deny("cmd.sudo", "privilege escalation is denied: an agent that can become root has no permission model", frozenset({"sudo", "su", "doas", "pkexec", "runuser"})),
    # Raw devices and filesystem destruction
    _deny("cmd.disk_table", "disk partitioning and erasure is system-level and can render a host unbootable", frozenset({"fdisk", "parted", "sgdisk", "partprobe", "wipefs", "hdparm", "blkdiscard"})),
    # `mkfs` on Linux is a family dispatcher: the real binaries are `mkfs.ext4`,
    # `mkfs.xfs`, `mkfs.btrfs`, `mkfs.ntfs` and so on, and matching only the bare
    # name missed every one of them. The `mkfs.*` entry is matched as a literal
    # dot-prefix, NOT as a general prefix -- `mkfsext4` must not match.
    _deny("cmd.mkfs", "filesystem formatting, irreversible", frozenset({"mkfs", "mkfs.*", "mke2fs", "newfs", "diskutil", "mkswap"})),
    _deny("cmd.device_format", "device formatting or erasure", frozenset({"nvme"})),
    # System service and power state
    _deny("cmd.systemctl", "system service modification is outside the workspace", frozenset({"systemctl"})),
    _deny("cmd.service", "service modification is outside the workspace", frozenset({"service"})),
    _deny("cmd.launchctl", "service modification is outside the workspace", frozenset({"launchctl"})),
    _deny("cmd.power", "shutdown or reboot is never an agent operation", frozenset({"shutdown", "reboot", "poweroff", "halt", "init"})),
    # Package installation: excluded from v1, not gated
    _deny("cmd.pip_install", "dependency installation is excluded from v1", frozenset({"pip", "pip3", "pipx", "poetry"}), subcommands=frozenset({"install", "add", "uninstall", "remove"})),
    # `uv` takes a sub-subcommand: `uv pip install`. Match on the second
    # non-flag argument for this program rather than adding a program-specific
    # exception to the shared matcher.
    _deny("cmd.pip_install", "dependency installation is excluded from v1", frozenset({"uv"}), subcommands=frozenset({"install", "add", "remove", "uninstall", "sync"}), any_subcommand=True),
    _deny("cmd.npm_install", "dependency installation is excluded from v1", frozenset({"npm", "pnpm", "yarn"}), subcommands=frozenset({"install", "i", "add", "remove", "uninstall", "ci"})),
    _deny("cmd.system_pkg", "system package installation is denied", frozenset({"apt", "apt-get", "aptitude", "dnf", "yum", "apk", "pacman", "zypper", "snap", "port"})),
    _deny("cmd.user_pkg", "package installation is denied", frozenset({"brew", "gem", "cargo", "conda", "go"}), subcommands=frozenset({"install", "add", "get"})),
    # Credential inspection: the agent would see a secret
    _deny("cmd.env_dump", "environment dump would expose credential values", frozenset({"env", "printenv", "set", "export"})),
    _deny("cmd.credential_inspect", "credential inspection is denied", frozenset({"aws", "gcloud", "gh", "docker", "vault"}), subcommands=frozenset({"configure", "auth", "login"})),
    # Resource exhaustion
    _deny("cmd.fork_bomb", "fork bomb", _ANY, flags=frozenset({":(){", ":|:&"})),
    # Kernel, network, user management
    _deny("cmd.firewall", "network configuration changes are system-level", frozenset({"iptables", "nft", "ufw", "firewall-cmd"})),
    _deny("cmd.user_mgmt", "user and group management is system-level", frozenset({"useradd", "usermod", "groupadd", "passwd", "chpasswd", "visudo"})),
    # Git: history rewrite and force push
    _deny("cmd.git_force_push", "force push destroys other people's commits", _GIT, subcommands=frozenset({"push", "send-pack"}), flags=frozenset({"--force", "-f", "--force-with-lease"})),
    _deny("cmd.git_filter", "history rewriting is irreversible for other clones", _GIT, subcommands=frozenset({"filter-branch", "filter-repo"})),
    _deny("cmd.git_destructive", "destructive git state change", _GIT, subcommands=frozenset({"reflog"}), args_contain=frozenset({"expire"})),
    _deny("cmd.git_gc", "aggressive gc is irreversible", _GIT, subcommands=frozenset({"gc"}), flags=frozenset({"--prune=now", "--aggressive"})),
    # Filesystem repair tools that can corrupt
    _deny("cmd.fs_repair", "filesystem repair or attribute manipulation", frozenset({"debugfs", "tune2fs", "resize2fs", "e2fsck", "xfs_repair", "chattr", "lsattr", "setfattr"})),
    # Containers that can mount the host
    _deny("cmd.docker_system", "container system pruning is destructive", frozenset({"docker", "podman"}), subcommands=frozenset({"system"})),
    # Sysctl writes change kernel parameters. `sysctl -a` is a read and stays
    # allowed; only the write form is denied.
    _deny("cmd.kernel", "kernel and device module management is system-level", frozenset({"modprobe", "insmod", "rmmod"})),
    # Only the write forms. `-w` sets a parameter directly and `-p` applies a
    # config file, so both change the host and are denied. Reading kernel
    # parameters (`sysctl -a`, or a bare name) is harmless and stays allowed.
    #
    # This cannot simply join cmd.kernel: a rule's flag requirement applies to
    # every program in its set, and modprobe/insmod/rmmod take no such flag.
    _deny(
        "cmd.sysctl_write",
        "writing a kernel parameter is a host configuration change",
        frozenset({"sysctl"}),
        flags=frozenset({"-w", "-p"}),
    ),
    # `sc` reaches the same service state as `systemctl`/`service`. Named
    # separately because `sc` is a short, ambiguous program name.
    _deny("cmd.service_sc", "system service modification is outside the workspace", frozenset({"sc"})),
    # Destructive git operations the spec lists but no shipped rule covered.
    _deny("cmd.git_reset_hard", "history rewrite is irreversible for other clones", _GIT, subcommands=frozenset({"reset"}), flags=frozenset({"--hard"})),
    _deny("cmd.git_clean", "clean discards uncommitted work irrecoverably", _GIT, subcommands=frozenset({"clean"}), flags=frozenset({"-f", "--force"})),
    _deny("cmd.git_branch_delete", "branch deletion destroys work, sometimes other people's", _GIT, subcommands=frozenset({"branch"}), flags=frozenset({"-d", "-D"})),
    # Tag deletion is the same operation as branch deletion under a different
    # noun. Denying one and approving the other would be arbitrary.
    _deny("cmd.git_tag_delete", "tag deletion destroys work", _GIT, subcommands=frozenset({"tag"}), flags=frozenset({"-d", "-D"})),
    _deny("cmd.git_update_ref_delete", "ref deletion destroys work", _GIT, subcommands=frozenset({"update-ref"}), flags=frozenset({"-d", "--delete"})),
    # `git push --delete origin x` and the older `git push origin :x` spelling
    # both delete a remote ref. Both forms are denied; a plain push stays gated.
    _deny("cmd.git_push_delete", "pushing a deletion destroys the remote ref", _GIT, subcommands=frozenset({"push"}), flags=frozenset({"--delete"})),
    _deny("cmd.git_push_refspec_delete", "pushing a deletion destroys the remote ref", _GIT, subcommands=frozenset({"push"}), args_contain=frozenset({":", ":refs/heads/"})),
)

# --------------------------------------------------------------------------
# Approval-required. Legitimate development, destructive-within-workspace,
# inline code execution, network egress.
# --------------------------------------------------------------------------
APPROVAL_PROGRAMS: tuple[ProgramRule, ...] = (
    # Git
    _gate("cmd.git_commit", "commit records intent; the operator decides", _GIT, subcommands=frozenset({"commit"})),
    _gate("cmd.git_merge", "merge creates a commit", _GIT, subcommands=frozenset({"merge"})),
    _gate("cmd.git_push", "push publishes commits", _GIT, subcommands=frozenset({"push"})),
    _gate("cmd.git_remote_write", "remote mutation", _GIT, subcommands=frozenset({"remote"}), flags=frozenset({"add", "remove", "rename", "set-url", "set-head"})),
    _gate("cmd.git_fetch", "fetch reaches the network and updates refs", _GIT, subcommands=frozenset({"fetch", "clone", "pull", "submodule"})),
    _gate("cmd.git_ref_write", "ref manipulation is powerful and rarely benign", _GIT, subcommands=frozenset({"update-ref", "symbolic-ref", "replace"})),
    _gate("cmd.git_config_global", "changes machine-wide git behaviour", _GIT, subcommands=frozenset({"config"}), flags=frozenset({"--global", "--system"})),
    # Permission changes inside the workspace
    _gate("cmd.chmod", "permission change", frozenset({"chmod", "chown", "chgrp", "setfacl", "umask"})),
    # Arbitrary filesystem moves. `write_file` and `patch_file` are automatic
    # inside the workspace, but a `cp`/`mv`/`ln` invocation is a command, and
    # v1 gates them rather than reasoning about whether a given copy is safe.
    # Containment still denies any target outside the workspace.
    _gate("cmd.file_move", "copy, move, and link operations are approval-required in v1", frozenset({"cp", "mv", "ln"})),
    # `git rebase` rewrites history. The spec distinguishes rewriting published
    # history (denied) from ordinary development (approval), and a static
    # classifier cannot tell whether a ref is published, so approval is the
    # conservative v1 answer.
    _gate("cmd.git_rebase", "rebase rewrites history; approval is the conservative v1 answer", _GIT, subcommands=frozenset({"rebase"})),
    # `shred` and a file-targeting `dd` overwrite are destructive but scoped and
    # knowable. Device targets are still denied outright by the device check.
    _gate("cmd.shred", "overwrite is destructive and irreversible", frozenset({"shred"})),
    # Network egress. Not an implicit web interface.
    _gate("cmd.net_egress", "network egress is approval-required; arbitrary shell access is not a web interface", frozenset({"curl", "wget", "http", "httpie", "nc", "ncat", "netcat", "telnet", "ftp", "sftp", "scp", "ssh", "rsync", "socat"})),
    # Process control by pattern: cannot be resolved to an owned PID
    _gate("cmd.process_pattern", "pattern-based process kill cannot be resolved to a process this run started", frozenset({"killall", "pkill"})),
    # Database clients: gated only when the SQL classifier sees something it
    # cannot call safe. A blanket gate here would prompt on every SELECT, and a
    # prompt on every read is a prompt nobody reads.
    # Scheduling: n8n owns scheduling per architecture.md
    _gate("cmd.crontab", "an agent scheduling itself contradicts the design", frozenset({"crontab"})),
    # Containers
    _gate("cmd.container", "container operations can mount the host filesystem", frozenset({"docker", "podman"}), subcommands=frozenset({"run", "exec", "stop", "rm", "kill"})),
    # Broad in-place rewrites
    _gate("cmd.inplace_rewrite", "bulk in-place rewrite across files", frozenset({"sed", "perl"}), flags=frozenset({"-i", "--in-place"})),
    # Truncation
    _gate("cmd.truncate", "truncation destroys file contents", frozenset({"truncate"})),
    # Non-recursive deletion. Gated rather than automatic because a delete is
    # irreversible. The recursive case is handled by the sharper rule in
    # `_classify_wildcard_delete`, which distinguishes wildcard targets (denied)
    # from explicit ones (gated).
    _gate("cmd.delete_file", "file deletion is irreversible", frozenset({"rm", "rmdir", "unlink"})),
    # ------------------------------------------------------------------
    # Repository-code execution.
    #
    # `pytest` executes `conftest.py`, loads installed plugins, imports
    # fixtures, and runs arbitrary repository-supplied test code, so it is
    # repository code executing under a familiar name. Gating it here keeps it
    # in the same bucket as a workspace-local shell script; auto-approving
    # `pytest` while gating `./scripts/check.sh` would be incoherent.
    #
    # These are explicit rules rather than a consequence of the unknown
    # default, so the reason an operator sees names the actual cause instead of
    # "unrecognised".
    # ------------------------------------------------------------------
    _gate("cmd.repo_runner", "test execution runs repository code", frozenset({"pytest"})),
    _gate("cmd.repo_runner", "make executes repository build recipes", frozenset({"make"})),
    _gate("cmd.repo_runner", "test execution runs repository code", frozenset({"tox", "nox"})),
    _gate(
        "cmd.repo_runner",
        "npm runs repository scripts",
        frozenset({"npm", "pnpm", "yarn"}),
        subcommands=frozenset({"test", "run", "start", "rebuild"}),
    ),
    _gate(
        "cmd.repo_runner",
        "cargo runs repository build and test code",
        frozenset({"cargo"}),
        subcommands=frozenset({"test", "build", "run", "bench", "doc", "check", "clippy"}),
    ),
    _gate(
        "cmd.repo_runner",
        "go runs repository build and test code",
        frozenset({"go"}),
        subcommands=frozenset({"test", "build", "run", "generate", "vet"}),
    ),
    # `python -m pytest` is a test run by another spelling. The `-m` flag is
    # required so this cannot match `python -m <unrelated-module>`, which the
    # unknown default already gates.
    _gate(
        "cmd.repo_runner_module",
        "python -m pytest runs repository test code",
        frozenset({"python", "python3", "pypy"}),
        flags=frozenset({"-m"}),
        args_contain=frozenset({"pytest"}),
    ),
    # Git mutations with no prior rule. `git add` stages into the operator's
    # index on the agent's judgement about what belongs in a commit; the
    # operator reviewing that is the point of the gate. `git switch` moves the
    # working tree.
    #
    # `git branch`, `git tag`, and `git stash` are deliberately absent here.
    # Each has an automatic listing form and a gated mutating form, and a
    # program-level rule cannot tell them apart -- `cmd.git_branch_mutate` would
    # also match `git branch --list`, and strictest-wins would then gate the
    # listing form. They are discriminated by operation in
    # `_classify_git_inspection`.
    _gate("cmd.git_add", "staging changes the operator's index", _GIT, subcommands=frozenset({"add"})),
    _gate("cmd.git_switch", "switching branches moves the working tree", _GIT, subcommands=frozenset({"switch", "restore"})),
)

# --------------------------------------------------------------------------
# Allowed. Automatic, with no prompt.
#
# This table exists because the classifier's default changed. Previously an
# unmatched argv invocation classified ALLOW, so `ALLOW` was reachable only as
# an absence -- and a miss landed as silent execution rather than as a prompt.
# Now the default is APPROVAL_REQUIRED (see `_unknown_program`), which means
# `ALLOW` has to be a rule that matches, or the model would deny itself.
#
# The list is deliberately small, per the ruling in
# byte-security-classification.md "List 3":
#
#     if we are not confident it belongs in automatic,
#     leave it at APPROVAL_REQUIRED
#
# It is not broadened to reduce prompt count. A missing row costs one prompt; a
# wrongly-added row costs a silent execution, and that asymmetry sets the size
# of the table. Coverage grows one reviewed row at a time.
#
# Two things are deliberately absent and their absence is a decision, not an
# oversight:
#
# * `du` and `df` -- `df` reports mounted filesystems outside the workspace.
# * `env`, `printenv`, `set` -- denied outright by `cmd.env_dump`. There is no
#   name-only form here; the matcher cannot structurally distinguish a safe
#   by-name read from a value dump, so the denied reading stands.
#
# Git inspection and the `find` read forms are not in this table because they
# need operation-level or flag-level discrimination that `ProgramRule` cannot
# express without also matching the mutating forms of the same program. They are
# in `_classify_git_inspection` and `_classify_find_read`.
# --------------------------------------------------------------------------

_READ_ONLY_PROGRAMS = frozenset(
    {"ls", "pwd", "cat", "head", "tail", "wc", "stat", "file", "diff",
     "grep", "rg"}
)


def _allow(
    rule_id: str, reason: str, programs: frozenset[str], **kw: object
) -> ProgramRule:
    return ProgramRule(rule_id, reason, Classification.ALLOW, programs, **kw)  # type: ignore[arg-type]


ALLOWED_PROGRAMS: tuple[ProgramRule, ...] = (
    _allow(
        "allow.read_only",
        "read-only inspection inside the workspace",
        _READ_ONLY_PROGRAMS,
    ),
    _allow(
        "allow.version_probe",
        "version probe on a recognised program; cannot act on the workspace",
        _ANY,
        flags=frozenset({"--version", "-V"}),
    ),
)

#: Directories whose executables Forge is willing to run directly.
#:
#: Derived from the Executor's fixed executable search path rather than being a
#: second, independent list: a program Forge cannot launch is a program whose
#: location it does not trust, and two lists would be free to disagree.
#:
#: The value is duplicated as text rather than imported from `execution.py`
#: because that module imports the approval layer, which imports this one, so an
#: import here would be circular. `tests/test_commands.py` asserts the two stay
#: equal, which is the part that actually matters.
TRUSTED_EXECUTABLE_ROOTS: tuple[str, ...] = ("/usr/local/bin", "/usr/bin", "/bin")


# A destructive statement that names a target. Keyword presence, not parsing.
# `update` is destructive only when it lacks a WHERE, which the negative
# lookahead handles; the `.*` spans newlines so a multi-line statement is
# classified by its whole text rather than one line of it.
_DESTRUCTIVE_SQL = re.compile(
    r"drop\s+(database|schema|table)\b"
    r"|\btruncate\b"
    r"|\bdelete\s+from\b"
    r"|\balter\s+table\b"
    r"|\bupdate\b(?![\s\S]*\bwhere\b)",
    re.IGNORECASE,
)
_DELETE_WITH_WHERE = re.compile(r"\bdelete\s+from\b[\s\S]*\bwhere\b", re.IGNORECASE)
_UPDATE_WITH_WHERE = re.compile(r"\bupdate\b[\s\S]*\bwhere\b", re.IGNORECASE)
_ANY_SQL_STATEMENT = re.compile(
    r"\b(select|insert|update|delete|drop|create|alter|truncate|grant|revoke)\b",
    re.IGNORECASE,
)
# Statements that Forge will not gate on their own: reads, and writes that
# add rows rather than remove or alter them. Anything else that looks like SQL
# is gated, so an unrecognised form is never automatic.
_NOT_DESTRUCTIVE = re.compile(
    r"^\s*(select|show|describe|explain|with|insert)\b", re.IGNORECASE
)

_INTERPRETERS = frozenset(
    {"python", "python3", "pypy", "bash", "sh", "zsh", "dash", "node",
     "perl", "ruby", "php", "deno", "bun", "lua", "R", "awk", "gawk"}
)

# Git subcommands that only read repository state. Every form is automatic in
# List 3: `git status`, `git diff`, `git log`, `git show`, `git rev-parse`,
# `git ls-files`. A mutating git subcommand is not here and is gated.
#
# `git describe` and `git config --get` are deliberately absent: the spec omits
# them from List 3, so they fall through to the unknown default. That is the
# documented outcome, not an oversight.
_GIT_INSPECT_SUBCOMMANDS = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files"}
)

# `find` flags that write or delete. Without one of these, `find` only
# enumerates paths and is automatic.
_FIND_WRITE_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls", "-fprint",
     "-fprint0", "-fprintf"}
)

# Flags after which the remainder is inline source or a script path, so the
# next argument is not a path subject for containment purposes.
_SOURCE_FLAGS = frozenset({"-c", "-e", "-m"})


@dataclass(frozen=True)
class CommandVerdict:
    """Structured result of classifying a command.

    Attributes:
        classification: Final verdict after every rule, strictest match wins.
        rule: Identifier of the deciding rule.
        reason: Human-readable explanation. Safe for an operator. Never
            contains environment values.
        program: The normalised program basename.
        program_token: The program exactly as supplied, before normalisation.
            Execution binding uses this rather than ``program``: the normalised
            basename exists to match classification rules against, and binding
            on it would make ``/usr/bin/rm`` and ``rm`` the same approval.
        args: The arguments as supplied.
        cwd: The resolved working directory.
        matched_rules: Every rule that fired, not only the deciding one.
        targets: Filesystem paths extracted from the arguments, when the
            program takes paths and any were identifiable. Empty when none.
        targets_resolved: Targets resolved against the resolved cwd, for the
            approval prompt to display.
        owned_pids: PIDs Forge started this run, when supplied. Used by the
            process-kill decision.
        is_shell: Whether the invocation is a shell string.
    """

    classification: Classification
    rule: str
    reason: str
    program: str
    args: tuple[str, ...]
    cwd: Path
    matched_rules: tuple[str, ...] = ()
    targets: tuple[Path, ...] = ()
    targets_resolved: tuple[Path, ...] = ()
    owned_pids: frozenset[int] = field(default_factory=frozenset)
    is_shell: bool = False
    # The program exactly as supplied, before normalisation. Declared after the
    # non-default fields deliberately: a dataclass rejects a defaulted field
    # that precedes a non-defaulted one.
    program_token: str = ""

    @property
    def allowed(self) -> bool:
        return self.classification is Classification.ALLOW

    @property
    def denied(self) -> bool:
        return self.classification is Classification.DENY

    @property
    def requires_approval(self) -> bool:
        return self.classification is Classification.APPROVAL_REQUIRED

    def __str__(self) -> str:  # pragma: no cover - operator convenience
        return (
            f"{self.classification.value}: {self.rule}: {self.program} "
            f"{' '.join(self.args)}".rstrip()
        )


class CommandClassifier:
    """Classifies structured command invocations.

    Args:
        workspace: The single explicit workspace root. Used to resolve ``cwd``
            and every path argument.
        owned_pids: Process IDs Forge started as part of the current run. A
            ``kill`` targeting one of these is automatic; anything else is
            gated. Empty by default, which is the conservative default.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        owned_pids: frozenset[int] = frozenset(),
    ) -> None:
        self.workspace = workspace
        self.owned_pids = owned_pids

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _subcommand(args: tuple[str, ...]) -> str | None:
        """The first non-flag argument, lowercased. How git subcommands arrive."""
        for arg in args:
            if not arg.startswith("-"):
                return arg.casefold()
        return None

    @staticmethod
    def _flags(args: tuple[str, ...]) -> frozenset[str]:
        """Flags present, including flags bundled with a value (``-rf``) and
        flags written GNU-style as ``--long=value``.

        A bundled short flag expands to its constituents so ``rm -rf`` reports
        ``-r`` and ``-f`` individually. A ``--long=value`` argument reports
        ``--long`` so a rule matching the flag also matches the assignment form.
        """
        found: set[str] = set()
        for arg in args:
            if not arg.startswith("-"):
                continue
            found.add(arg)
            if "=" in arg:
                found.add(arg.split("=", 1)[0])
            # Bundled short flags: -rf -> -r, -f. Only for a bare cluster of
            # letters; "-c" and "--flag" are left alone.
            if re.fullmatch(r"-[a-zA-Z]{2,}", arg):
                found.update(f"-{ch}" for ch in arg[1:])
        return frozenset(found)

    @staticmethod
    def _program_matches(rule: ProgramRule, cmd: Command) -> bool:
        """Whether the rule's program set covers this command's program.

        `"*"` in the set means any program. Otherwise the normalised basename
        must be a member -- with one exception: an entry of the form `mkfs.*`
        matches the `mkfs.<fstype>` family of dispatchers.

        The wildcard is deliberately a *dot-prefix*, not a bare prefix. The
        difference is not cosmetic: `mkfsext4` is an unrelated program and must
        not be denied as filesystem formatting, while `mkfs.ext4` is the very
        binary the spec names. A general `mkfs` prefix would over-deny on a
        name that shares characters.
        """
        if "*" in rule.programs:
            return True
        program = cmd.normalized_program
        if program in rule.programs:
            return True
        return any(
            entry.endswith(".*") and program.startswith(entry[:-1])
            for entry in rule.programs
        )

    def _match(self, rule: ProgramRule, cmd: Command) -> bool:
        if not self._program_matches(rule, cmd):
            return False
        if rule.subcommands is not None:
            non_flag = [a.casefold() for a in cmd.args if not a.startswith("-")]
            if rule.any_subcommand:
                if not set(non_flag) & rule.subcommands:
                    return False
            else:
                sub = non_flag[0] if non_flag else None
                if sub is None or sub not in rule.subcommands:
                    return False
        if rule.args_contain:
            # A token matches when it equals the required value, or when the
            # argument is written `token=value`. `dd if=/dev/zero` names the
            # `if` operand that way, with no leading dash.
            present_args = [a.casefold() for a in cmd.args]
            found = any(
                arg == wanted or arg.startswith(wanted + "=")
                for arg in present_args
                for wanted in rule.args_contain
            )
            if not found:
                return False
        if rule.flags:
            present = self._flags(cmd.args)
            if rule.all_of_flags:
                if not rule.flags <= present:
                    return False
            elif not (rule.flags & present):
                return False
        return True

    def _extract_targets(self, cmd: Command, cwd: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        """Extract filesystem paths from arguments.

        Only for programs known to take paths. Non-flag arguments are treated as
        paths except:
        - the argument immediately after a source flag (``-c``, ``-e``, ``-m``),
          which is code or a module name, not a path
        - arguments that are obviously not paths (pure flags, ``-``)
        """
        if cmd.normalized_program not in PATH_TAKING_PROGRAMS:
            return (), ()

        raw: list[str] = []
        skip_next = False
        for arg in cmd.args:
            if skip_next:
                skip_next = False
                continue
            if arg in _SOURCE_FLAGS:
                skip_next = True
                continue
            if arg.startswith("-") or arg == "-":
                continue
            raw.append(arg)

        targets = tuple(Path(a) for a in raw)
        resolved = tuple(
            (cwd / t) if not t.is_absolute() else t for t in targets
        )
        return targets, resolved

    def _classify_device_write(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Deny raw block-device writes.

        Detected by the *target path*, not by operand name: ``dd of=/dev/sda``
        is a device write and ``dd of=output.bin`` is ordinary file I/O. The
        distinction is where the bytes land, so that is what is checked.
        """
        program = cmd.normalized_program
        if program not in {"dd", "cp", "cat", "tee", "shred", "install", "ddrescue"}:
            return None

        for arg in cmd.args:
            # dd writes with of=/dev/..., cp and tee name the device directly.
            candidates = [arg]
            if "=" in arg:
                candidates.append(arg.split("=", 1)[1])
            for candidate in candidates:
                if _DEVICE_PATH.match(candidate.strip()):
                    return (
                        Classification.DENY,
                        "cmd.device_write",
                        f"raw block-device write to {candidate}, irreversible",
                    )
        return None

    def _classify_file_overwrite(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Gate `dd` when it writes a regular file rather than a device.

        The spec treats these as two different operations: `dd of=/dev/...` is
        raw device erasure and denied outright, while `dd of=<file>` is
        "destructive but scoped and knowable" and approval-required.

        This is the device check read from the other side. Rather than
        duplicating the device pattern, it asks whether *any* operand names a
        device; if none does, and an `of=` operand is present, the write is a
        file write and is gated. The earlier device classifier has already had its
        chance to deny by this point, so a genuine device write never reaches
        here.
        """
        if cmd.normalized_program != "dd":
            return None

        writes_file = False
        for arg in cmd.args:
            candidate = arg.split("=", 1)[1] if "=" in arg else arg
            if _DEVICE_PATH.match(candidate.strip()):
                # A device target. The device classifier owns this case.
                return None
            if arg.startswith("of="):
                writes_file = True
        if not writes_file:
            return None
        return (
            Classification.APPROVAL_REQUIRED,
            "cmd.dd_file_write",
            "dd overwrites its output file; the operation is destructive but scoped and knowable",
        )

    def _classify_wildcard_delete(self, cmd: Command, targets: tuple[Path, ...]) -> tuple[Classification, str, str] | None:
        """Deny destructive deletes whose targets cannot be enumerated.

        Per the spec: an operator cannot meaningfully approve a destructive
        action when the prompt cannot show the actual affected targets.
        """
        flags = self._flags(cmd.args)
        program = cmd.normalized_program
        recursive = program in RECURSIVE_DELETE_PROGRAMS and bool(
            flags & {"-r", "-R", "--recursive"}
        )
        if program not in RECURSIVE_DELETE_PROGRAMS:
            return None

        wildcard_targets = [
            str(t) for t in targets
            if any(ch in str(t) for ch in WILDCARD_CHARACTERS)
        ]
        if wildcard_targets:
            return (
                Classification.DENY,
                "cmd.delete_wildcard",
                (
                    f"destructive delete with unresolved wildcard target(s) "
                    f"{wildcard_targets}; resolve the globs and request deletion "
                    f"with explicit paths"
                ),
            )

        # Recursive delete with enumerable targets: approval-required. The
        # targets are knowable, so a prompt can show them. A non-recursive
        # delete is left to the cmd.delete_file rule, which describes it more
        # accurately than "recursive" would.
        if recursive:
            if targets:
                return (
                    Classification.APPROVAL_REQUIRED,
                    "cmd.delete_recursive",
                    f"recursive delete of explicit target(s) {[str(t) for t in targets]}",
                )
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.delete_recursive",
                "recursive delete; targets could not be enumerated",
            )

        return None

    @staticmethod
    def _is_checkout_discard(cmd: Command) -> bool:
        """Whether this is the documented destructive `git checkout -- .` form.

        The spec names `git checkout -- .` specifically. The `--` here is a
        separator that tells git to treat what follows as paths, so it is not a
        flag and cannot be matched by the flag table. Matching is therefore on
        the shape: a `--` separator followed by a path that names the whole
        working tree.

        Deliberately narrow. A plain `git checkout main` or `git checkout -- file`
        is ordinary development and stays allowed; only the tree-wide discard is
        denied, because only that form silently destroys uncommitted work.
        """
        sub = CommandClassifier._subcommand(cmd.args)
        if sub != "checkout":
            return False
        if "--" not in cmd.args:
            return False
        after = cmd.args[cmd.args.index("--") + 1 :]
        if not after:
            return False
        # Paths are the only thing that can follow the separator here, so the
        # last non-flag token decides.
        paths = [a for a in after if not a.startswith("-")]
        return bool(paths) and all(p in {".", "./"} for p in paths)

    def _classify_push_refspec_delete(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Deny `git push origin :branch`, the older spelling of a ref deletion.

        `git push --delete origin x` is caught by the flag-based rule. This form
        has no flag: a refspec beginning with a colon is a delete, and it is a
        conventional enough spelling that matching only `--delete` would leave a
        documented destructive operation approved.
        """
        if cmd.normalized_program != "git":
            return None
        if CommandClassifier._subcommand(cmd.args) != "push":
            return None
        for arg in cmd.args[1:]:
            if arg.startswith(":") or arg.startswith(":refs/"):
                return (
                    Classification.DENY,
                    "cmd.git_push_refspec_delete",
                    "pushing an empty refspec deletes the remote ref",
                )
        return None

    def _classify_inline_code(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Gate inline-code execution on the *form*, not the source.

        Forge does not analyse the code. ``python -c "import shutil; ..."`` has
        argv matching nothing dangerous; the gate exists because the form allows
        anything.
        """
        program = cmd.normalized_program
        if program not in _INTERPRETERS:
            return None
        flags = self._flags(cmd.args)
        if flags & INLINE_CODE_FLAGS:
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.inline_code",
                (
                    f"inline code execution via {program}; the operator should "
                    f"read the source before approving. Forge does not analyse it."
                ),
            )
        return None

    def _classify_sql(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Gate destructive SQL reaching a database client.

        Keyword presence, not parsing. Ambiguous SQL is gated, never automatic.
        """
        program = cmd.normalized_program
        if program not in {
            "psql", "mysql", "mariadb", "sqlite3", "sqlcmd",
            "mongosh", "clickhouse-client",
        }:
            return None

        # Strip the leading flag tokens so a statement is recognised regardless
        # of how the client was invoked: `psql -c "SELECT ..."` and
        # `psql "SELECT ..."` must classify identically.
        joined = " ".join(a for a in cmd.args if not a.startswith("-"))
        if not _ANY_SQL_STATEMENT.search(joined):
            return None

        # Reads and row-adding writes. Not gated: gating every SELECT would
        # make the rule useless, because a prompt on every read is a prompt
        # nobody reads.
        if _NOT_DESTRUCTIVE.match(joined):
            return None

        # A restrictive target is the documented safe case.
        if _DELETE_WITH_WHERE.search(joined) or _UPDATE_WITH_WHERE.search(joined):
            return None

        if _DESTRUCTIVE_SQL.search(joined):
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.sql_destructive",
                "destructive SQL statement; the operator should read the statement before approving",
            )

        # A statement we cannot classify is gated by default.
        return (
            Classification.APPROVAL_REQUIRED,
            "cmd.sql_ambiguous",
            "SQL statement could not be classified as non-destructive",
        )

    def _classify_checkout_discard(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Deny the tree-wide `git checkout -- .` discard form.

        The generic ``cmd.git_checkout_discard`` rule exists only so the deny
        appears in ``matched_rules`` with a stable id; the actual decision is made
        here, because the documented form is a specific argument shape rather
        than a flag or subcommand.
        """
        if not self._is_checkout_discard(cmd):
            return None
        return (
            Classification.DENY,
            "cmd.git_checkout_discard",
            "git checkout -- . discards every uncommitted change in the working tree",
        )

    def _classify_kill(self, cmd: Command, targets: tuple[Path, ...]) -> tuple[Classification, str, str] | None:
        """Automatic only for a process this run started.

        Provenance, not inspection: a PID Forge spawned is in ``owned_pids`` or
        it is not.
        """
        program = cmd.normalized_program
        if program != "kill":
            return None

        flags = self._flags(cmd.args)
        pids: list[int] = []
        positional = [a for a in cmd.args if not a.startswith("-")]

        if "-u" in flags or "--user" in " ".join(cmd.args):
            return (
                Classification.DENY,
                "cmd.kill_privileged_user",
                "killing another user's processes is system-level",
            )

        for arg in positional:
            try:
                pid = int(arg)
            except ValueError:
                continue
            pids.append(pid)

        if not pids:
            # No recognisable PID: cannot prove ownership. Gate.
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.kill_unresolved",
                "kill target could not be identified as a process this run started",
            )

        # PID 1 is the init process. Never ours.
        if any(pid == 1 for pid in pids):
            return (
                Classification.DENY,
                "cmd.kill_init",
                "signalling PID 1 is a system-level operation",
            )

        if all(pid in self.owned_pids for pid in pids):
            return (
                Classification.ALLOW,
                "cmd.kill_owned",
                "target is a process this run started",
            )

        return (
            Classification.APPROVAL_REQUIRED,
            "cmd.kill_foreign",
            "target is not a process this run started",
        )

    def _classify_git_inspection(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Automatic listing forms of git, gated mutating forms of the same.

        `git branch`, `git tag`, and `git stash` are not one permission level
        each. A program-level rule cannot express that -- any rule matching
        `git branch` also matches `git branch --list` -- so the discrimination is
        made here on the operation instead.

        Only ALLOW is decided here. A mutating form returns None and is gated by
        the unknown default or by an existing explicit rule, which keeps this
        method from becoming a second place that git policy lives.
        """
        if cmd.normalized_program != "git":
            return None

        sub = self._subcommand(cmd.args)
        if sub is None:
            return None

        # Drop the subcommand token itself before looking at operands. For
        # `git branch`, the operands are what follow "branch"; counting
        # "branch" as one would make every bare listing form look like a
        # mutation.
        operands = [
            a.casefold() for a in cmd.args[1:] if not a.startswith("-")
        ]
        flags = self._flags(cmd.args)

        # Inspection subcommands. Every form is a read: `git status`,
        # `git diff`, `git log`, `git show`, `git rev-parse`, `git ls-files`.
        if sub in _GIT_INSPECT_SUBCOMMANDS:
            return (
                Classification.ALLOW,
                "allow.git_inspect",
                f"git {sub} only reads repository state",
            )

        # `git branch` and `git tag`: automatic with no operand, gated when a
        # name is given (create/rename) and denied when a delete flag is
        # present. `cmd.git_branch_delete` / `cmd.git_tag_delete` own the
        # denial, so this only has to recognise the listing form.
        if sub in ("branch", "tag"):
            if operands:
                return None
            # A flag that mutates means this is not a listing form.
            if flags & {"-d", "-D", "-m", "-M", "-c", "-C", "--move", "--copy"}:
                return None
            return (
                Classification.ALLOW,
                f"allow.git_{sub}_list",
                f"git {sub} with no operation lists {sub}es",
            )

        # `git stash list` is a read. `git stash` bare and `git stash push` are
        # not: the bare form stashes the working tree, which mutates.
        if sub == "stash":
            if operands == ["list"]:
                return (
                    Classification.ALLOW,
                    "allow.git_stash_list",
                    "git stash list only reads the stash",
                )
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.git_stash_mutate",
                "stash mutation changes or discards stashed work",
            )

        return None

    def _classify_find_read(self, cmd: Command) -> tuple[Classification, str, str] | None:
        """Automatic only for `find` read forms.

        `find` is in List 3 for its read forms, but `-delete` and `-exec` make
        it destructive and `-fprintf`/`-fls` write files. Those are gated here
        rather than relying on the unknown default, so the reason an operator
        sees names the destructive flag instead of saying "unrecognised".
        """
        if cmd.normalized_program != "find":
            return None

        if self._flags(cmd.args) & _FIND_WRITE_FLAGS:
            return (
                Classification.APPROVAL_REQUIRED,
                "cmd.find_destructive",
                "find writes or deletes; the read forms are automatic",
            )

        # A bare `find` with no expression lists everything, which is the read
        # form and is bounded by the workspace.
        return (
            Classification.ALLOW,
            "allow.find_read",
            "find in a read form only enumerates paths",
        )

    def _classify_workspace_executable(
        self, cmd: Command, cwd: Path
    ) -> tuple[Classification, str, str] | None:
        """Gate an executable that resolves inside the workspace.

        The documented rule, from byte-security-classification.md "Workspace-local
        executables": resolve the program token; if it lands inside the
        workspace root it is approval-required.

        The test is *where the resolved path lands*, not whether the token
        contains a separator. `/usr/bin/git` contains a slash and is an
        ordinary system program classified by its normal rules; `./tool` and
        `/workspace/bin/lint` are gated.

        Only an explicit path is considered. A bare name like `git` is resolved
        by the executor against its fixed search path, which is never inside the
        workspace, so there is nothing here to gate.

        A *relative* token that escapes the workspace is denied here, rather than
        left to fall through to the unknown default. Containment cannot catch it:
        the program token is not a path argument, so `_extract_targets` never
        sees it and `../tool` would otherwise be gated rather than denied.

        An *absolute* token outside the workspace is only allowed to continue when
        it lands in a trusted system executable root -- the same directories the
        Executor would search. `/usr/bin/git` and `/usr/bin/python` are named in
        the spec as ordinary system programs and classify normally. Anything else
        is denied: `/tmp/evil-tool` and `/home/user/random-tool` are arbitrary
        external files that an operator prompt would be asked to approve on no
        useful information, which is the same "prompt without information"
        failure the secret-source principle rejects.

        Symlinks are resolved before the root check, so an apparent
        `/usr/bin/tool` pointing at `/tmp/evil` fails closed on its real location
        rather than passing on its apparent one.
        """
        token = cmd.program
        if not token or "/" not in token:
            return None

        candidate = Path(token)
        is_absolute = candidate.is_absolute()
        if not is_absolute:
            candidate = cwd / candidate

        resolution = self.workspace.resolve(candidate)
        if not resolution.allowed:
            if is_absolute and self._in_trusted_executable_root(candidate):
                # A system executable. Fall through to normal classification.
                return None
            return (
                Classification.DENY,
                "cmd.workspace_executable_outside",
                (
                    f"executable resolves outside the workspace: {token} -> "
                    f"{resolution.reason}"
                ),
            )

        return (
            Classification.APPROVAL_REQUIRED,
            "cmd.workspace_executable",
            (
                f"executable resolves inside the workspace ({token} -> "
                f"{resolution.resolved}); repository code is approval-required"
            ),
        )

    @staticmethod
    def _in_trusted_executable_root(candidate: Path) -> bool:
        """Whether ``candidate``'s real path lies in a trusted system root.

        Resolved with ``realpath`` so a symlink planted inside a trusted root
        cannot smuggle an arbitrary external executable past the check. A path
        that cannot be resolved is not trusted -- this fails closed, consistent
        with `Workspace.resolve`.
        """
        try:
            real = Path(os.path.realpath(str(candidate)))
        except (OSError, ValueError):
            return False

        for root in TRUSTED_EXECUTABLE_ROOTS:
            root_real = Path(os.path.realpath(root))
            if real == root_real or real.is_relative_to(root_real):
                return True
        return False

    def _classify_unknown_program(
        self, cmd: Command, matched: bool
    ) -> tuple[Classification, str, str] | None:
        """The default: an unrecognised argv invocation is approval-required.

        This is the change that made `ALLOW` an explicit rule rather than an
        absence. Previously anything unmatched fell through to automatic, so a
        classifier miss produced silent execution -- which contradicted the
        document's own claim that a miss degrades to a prompt.

        Called only when ``matched`` is False, so it never overrides a real
        decision. If it is skipped when a rule fired, strictest-wins cannot rank
        it against that rule, and a gated command would be gated twice or an
        automatic one silently re-gated.
        """
        if matched:
            return None
        return (
            Classification.APPROVAL_REQUIRED,
            "cmd.unknown",
            (
                f"unrecognised command '{cmd.program}': not on the automatic "
                f"list, so operator approval is required"
            ),
        )

    # -- public API ------------------------------------------------------

    def classify(self, command: Command) -> CommandVerdict:
        """Classify ``command``.

        Evaluation order:

        1. Resolve ``cwd``. A directory outside the workspace denies
           immediately -- every path argument would be relative to somewhere
           Forge has no authority.
        2. Extract targets and resolve them against the resolved ``cwd``.
        3. Workspace containment for every target.
        4. Wildcard deletes.
        5. Inline-code, SQL, and kill classification.
        6. Program rule tables: DENIED, APPROVAL, then ALLOWED.
        7. ``shell=True`` floors the result at approval_required.
        8. An operation no rule recognised is approval_required, never
           automatic.
        """
        program = command.normalized_program

        # 1. cwd
        if command.cwd is None:
            cwd_resolution = self.workspace.resolve(self.workspace.root)
        else:
            cwd_resolution = self.workspace.resolve(command.cwd)

        if not cwd_resolution.allowed:
            return CommandVerdict(
                classification=Classification.DENY,
                rule=cwd_resolution.rule,
                reason=f"working directory is outside the workspace: {cwd_resolution.reason}",
                program=program,
                program_token=command.program,
                args=command.args,
                cwd=cwd_resolution.resolved or self.workspace.root,
                matched_rules=(cwd_resolution.rule,),
                is_shell=command.shell,
            )

        cwd = cwd_resolution.resolved or self.workspace.root

        # 2. targets
        targets, resolved_targets = self._extract_targets(command, cwd)

        # ALLOW is the starting candidate, but it is not the outcome: step 8
        # replaces it when nothing matched, and any rule above can raise it.
        candidates: list[Classification] = [Classification.ALLOW]
        deciding: ProgramRule | None = None
        reason = "matched an explicit automatic rule"
        matched_ids: list[str] = []

        # 3. containment for each target
        for target, resolved_target in zip(targets, resolved_targets):
            target_resolution = self.workspace.resolve(resolved_target)
            if not target_resolution.allowed:
                candidates.append(Classification.DENY)
                matched_ids.append(target_resolution.rule)
                deciding = ProgramRule(
                    target_resolution.rule,
                    target_resolution.reason,
                    Classification.DENY,
                    _ANY,
                )
                reason = target_resolution.reason

        # 4-6. specific classifiers and rule tables
        for classify_fn in (
            lambda: self._classify_device_write(command),
            lambda: self._classify_wildcard_delete(command, targets),
            lambda: self._classify_inline_code(command),
            lambda: self._classify_sql(command),
            lambda: self._classify_kill(command, targets),
            lambda: self._classify_checkout_discard(command),
            lambda: self._classify_file_overwrite(command),
            lambda: self._classify_push_refspec_delete(command),
            lambda: self._classify_workspace_executable(command, cwd),
            lambda: self._classify_find_read(command),
            lambda: self._classify_git_inspection(command),
        ):
            outcome = classify_fn()
            if outcome is not None:
                classification, rule_id, why = outcome
                candidates.append(classification)
                matched_ids.append(rule_id)
                if classification.severity > (deciding.classification.severity if deciding else -1):
                    deciding = ProgramRule(rule_id, why, classification, _ANY)
                    reason = why

        for rule in (*DENIED_PROGRAMS, *APPROVAL_PROGRAMS, *ALLOWED_PROGRAMS):
            if self._match(rule, command):
                candidates.append(rule.classification)
                matched_ids.append(rule.rule_id)
                if rule.classification.severity > (deciding.classification.severity if deciding else -1):
                    deciding = rule
                    reason = rule.reason

        # 7. shell floors at approval_required; never automatic.
        if command.shell:
            candidates.append(Classification.APPROVAL_REQUIRED)
            matched_ids.append("cmd.shell_string")
            if deciding is None or deciding.classification is Classification.ALLOW:
                deciding = ProgramRule(
                    "cmd.shell_string",
                    "shell string is not parsed; approval required on sight",
                    Classification.APPROVAL_REQUIRED,
                    _ANY,
                )
                reason = "shell string is not parsed; approval required on sight"

        # 8. Nothing recognised. Previously this fell through to ALLOW, which
        # made a classifier miss mean silent execution. An unrecognised
        # operation is approval-required instead, so the failure mode of a miss
        # is a prompt.
        unknown = self._classify_unknown_program(
            command, matched=bool(matched_ids)
        )
        if unknown is not None:
            classification, rule_id, why = unknown
            candidates.append(classification)
            matched_ids.append(rule_id)
            deciding = ProgramRule(rule_id, why, classification, _ANY)
            reason = why

        final = strictest(*candidates)

        if deciding is None or deciding.classification is not final:
            # No rule decided, or a stricter rule did. Name the decision so an
            # operator is never asked to approve or denied something the
            # verdict cannot explain.
            deciding = ProgramRule(
                final.value if final is not Classification.ALLOW else "allow.unmatched",
                f"no rule classified this operation; resolved to {final.value}",
                final,
                _ANY,
            )
            reason = deciding.reason

        return CommandVerdict(
            classification=final,
            rule=deciding.rule_id if deciding else "none",
            reason=reason,
            program=program,
            program_token=command.program,
            args=command.args,
            cwd=cwd,
            matched_rules=tuple(matched_ids),
            targets=targets,
            targets_resolved=resolved_targets,
            owned_pids=self.owned_pids,
            is_shell=command.shell,
        )