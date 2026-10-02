"""Forge terminal entry point.

The first operator-facing CLI. It drives the existing safety stack so a person
can see what Forge decides and, where a decision needs one, ask for approval.

**It executes nothing.** There is no subprocess here, and there will not be one
until an execution layer exists behind the approval gate. This CLI classifies and
reports. When an operator approves an operation, the CLI says so plainly and
says that nothing ran, because an approval that silently did nothing would be
worse than no approval at all.

Three properties hold, and the tests assert each one:

*   **No policy lives here.** Every rule is in ``forge/tools``. This module
    builds a ``Workspace``, calls a classifier, hands the verdict to
    ``Approver``, and renders what comes back. It reads ``classification`` from a
    verdict only to choose a label, never to choose an outcome.
*   **No ``ApprovalRequest`` is constructed here.** Requests are built by the
    approval layer from a verdict, which is what makes a denial incapable of
    becoming a prompt. Building one in the CLI would route around that.
*   **Secret verdicts precede approval.** Targets the classifier resolved are
    classified against the secret rules and passed to ``Approver`` as
    ``secret_verdicts``, matching the order in
    ``docs/byte-tools.md``. This is why ``cat secrets.json`` is denied by policy
    even though the command itself would be ordinary.

Option placement is deliberately *after* the leaf subcommand -- ``forge check
command --workspace . -- pytest tests/`` -- because that is the form that reads
as "these options apply to this check". The options are attached to the
check-command and check-path parsers through a shared parent, rather than being
declared global and then required to precede the leaf.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence, TextIO

from ..tools.approval import (
    ApprovalChannel,
    Approver,
    TerminalApprovalChannel,
    effective_classification,
)
from ..tools.commands import Command, CommandClassifier, CommandVerdict
from ..tools.secrets import SecretClassifier, SecretVerdict
from ..tools.workspace import Classification, Workspace
from . import render
from .render import (
    EXIT_INTERNAL,
    EXIT_NEEDS_APPROVAL,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
)

__all__ = ["main", "build_parser"]

# The requester identity this CLI reports. Deliberately *not* "byte": there is no
# Byte runtime yet, so claiming Byte requested an operation would be a
# fabrication an operator could reasonably act on. When ``forge byte`` exists it
# passes "byte" explicitly, and that will be a true statement.
DEFAULT_AGENT = "forge-cli"

# Workspace roots are explicit by settled decision -- not a default, not
# inferred. The environment variable is an explicit statement of intent from
# whoever set it; falling back to the current directory would not be, and over
# SSH a wrong working directory is exactly the failure that matters.
WORKSPACE_ENV = "FORGE_WORKSPACE"

DESCRIPTION = """\
Forge — orchestration layer for Steel & Code agents.

Classifies operations against the Forge safety stack and, where a decision
needs one, asks for approval. Executes nothing: approving an operation records
the decision, and says so.
"""

STATUS = """\
Forge terminal interface — current state

  Workspace containment    implemented
  Secret classification    implemented
  Command classification   implemented
  Terminal approval        implemented
  Command execution        NOT implemented — this CLI classifies and asks only
  Agent runtime            NOT implemented
  Model gateway            NOT implemented
  Session memory           NOT implemented
  Byte                     contract specified, not runnable

Byte cannot run yet. This CLI exercises the safety stack that will sit in front
of it.

Usage:
  forge check command --workspace DIR [-- PROGRAM [ARG...]]
  forge check path --workspace DIR PATH [PATH...]
"""


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _shared_options() -> argparse.ArgumentParser:
    """Options attached to both check leaf parsers.

    A parent parser rather than a set of globals, so that the supported
    placement is the only placement. Declaring these global and then documenting
    "put them after the subcommand" produces an interface where the documented
    form is the one that happens to work.
    """
    parent = argparse.ArgumentParser(add_help=False)

    parent.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "explicit workspace root (required; may also be set via "
            f"{WORKSPACE_ENV}). Not inferred from the current directory."
        ),
    )
    parent.add_argument(
        "--agent",
        metavar="NAME",
        default=DEFAULT_AGENT,
        help=f"requester identity reported in the approval prompt (default: {DEFAULT_AGENT})",
    )
    parent.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit one JSON object instead of human-readable text",
    )
    parent.add_argument(
        "--classify-only",
        dest="classify_only",
        action="store_true",
        help=(
            "report the classification without asking anyone. Grants nothing: "
            "nothing can run from this CLI either way."
        ),
    )
    parent.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "behave as if no interactive human were present. Approval-required "
            "operations fail closed as denied unattended."
        ),
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    """Build the parser.

    ``check`` carries two leaves rather than one verb per subject type, because
    the two need genuinely different arguments -- a ``--``-delimited argv for a
    command, a path list for a file -- and different classifiers.
    """
    parser = argparse.ArgumentParser(
        prog="forge",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="forge 0.1.0",
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="COMMAND")

    check = sub.add_parser(
        "check",
        help="classify an operation and ask for approval if required",
        description=(
            "Classify an operation against the Forge safety stack. Does not "
            "execute it."
        ),
    )
    check_sub = check.add_subparsers(dest="leaf", metavar="SUBJECT")

    shared = _shared_options()

    command_parser = check_sub.add_parser(
        "command",
        parents=[shared],
        help="classify a command invocation",
        description=(
            "Classify a structured command invocation. Arguments after -- are "
            "the command's own program and arguments; Forge's options go before "
            "the --."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  forge check command --workspace . -- pytest tests/ -q\n"
            "  forge check command --workspace . --classify-only -- rm -rf build\n"
        ),
    )
    command_parser.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        metavar="-- PROGRAM [ARG...]",
        help="the command, after a -- separator",
    )

    path_parser = check_sub.add_parser(
        "path",
        parents=[shared],
        help="classify one or more file paths as secret sources",
        description=(
            "Classify paths against the secret-source rules. Structural or "
            "content-proven sensitivity is denied; a filename that merely "
            "suggests sensitivity is approval-required."
        ),
    )
    path_parser.add_argument("paths", nargs="+", metavar="PATH", help="paths to classify")

    return parser


# --------------------------------------------------------------------------
# Workspace resolution
# --------------------------------------------------------------------------


def _resolve_workspace(
    explicit: str | None, environ: dict[str, str]
) -> tuple[Workspace | None, str]:
    """Build a workspace, or explain why one could not be built.

    Resolution order: the flag, then the environment variable. There is no third
    step. Defaulting to the current directory would contradict the settled
    decision that the workspace root is an explicit part of runtime
    configuration, and would make a mis-scoped SSH session look legitimate.
    """
    root = explicit or environ.get(WORKSPACE_ENV)
    if not root:
        return None, (
            "no workspace configured: pass --workspace DIR or set "
            f"{WORKSPACE_ENV}. Forge does not infer one from the current "
            "directory."
        )
    try:
        return Workspace(Path(root)), ""
    except (OSError, ValueError) as exc:
        return None, f"workspace root could not be resolved: {exc}"


# --------------------------------------------------------------------------
# Presentation helpers
# --------------------------------------------------------------------------


def _emit(
    text: str, *, as_json: bool, out: TextIO, prefix: str = ""
) -> None:
    if prefix:
        print(prefix, file=out)
    print(text, file=out)


def _command_label(command: Command) -> str:
    return " ".join([command.normalized_program, *command.args]).rstrip()


def _make_channel(
    args: argparse.Namespace, out: TextIO, injected: ApprovalChannel | None
) -> ApprovalChannel | None:
    """Build the approval channel for this invocation.

    ``None`` means unattended, which is what ``--unattended`` asks for and also
    what ``--classify-only`` implies. An injected channel exists so tests can
    drive the real approval path without a TTY; it is not a second policy.
    """
    if args.classify_only or args.unattended:
        return None
    if injected is not None:
        return injected
    return TerminalApprovalChannel(prompt_stream=out)


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------


def _run_check_command(
    args: argparse.Namespace,
    workspace: Workspace,
    approver: Approver,
    *,
    as_json: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    argv: list[str] = list(args.argv or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print(
            "no command given: put the program after --, for example\n"
            "  forge check command --workspace . -- pytest tests/ -q",
            file=err,
        )
        return EXIT_USAGE

    # A leading -- is required so that a command's own flags are never mistaken
    # for Forge's. Without it, `forge check command --workspace . pytest -q`
    # would have argparse try to interpret -q.
    raw = list(getattr(args, "_raw_argv", []) or [])
    if "--" not in raw:
        print(
            "missing -- separator: Forge's options come before it, the "
            "command's own arguments after it, for example\n"
            "  forge check command --workspace . -- pytest tests/ -q",
            file=err,
        )
        return EXIT_USAGE

    program, *command_args = argv
    command = Command(program=program, args=tuple(command_args))

    classifier = CommandClassifier(workspace)
    verdict = classifier.classify(command)

    label = _command_label(command)

    # Secret verdicts for every target the command would touch, computed before
    # approval so the pipeline order is preserved: a structural secret denial
    # holds even when the command itself is merely approval-required, and a
    # name guess escalates an otherwise-automatic command.
    secret_verdicts = _secret_verdicts(workspace, verdict.targets_resolved)

    if args.classify_only:
        # The ranking comes from the approval layer rather than being re-done
        # here, so --classify-only and the full approval path can never disagree
        # about the same operation. That disagreement is exactly the bug this
        # fixes: `cat secrets.json` once reported ALLOW here while the approval
        # path prompted.
        state = render.state_for_classification(
            effective_classification(
                verdict.classification,
                *(s.classification for s in secret_verdicts),
            )
        )
        if as_json:
            _emit(
                render.render_json(
                    state, verdict=verdict, command=label, workspace=str(workspace.root)
                ),
                as_json=True,
                out=out,
            )
        else:
            _emit(
                render.render_verdict(
                    verdict,
                    command=label,
                    workspace=str(workspace.root),
                    state=state,
                    secret_verdicts=secret_verdicts,
                ),
                as_json=False,
                out=out,
            )
        return render.EXIT_FOR_STATE[state]

    outcome = approver.submit_command(
        verdict,
        agent=args.agent,
        secret_verdicts=secret_verdicts,
        tool="exec_command",
        intent=None,
        git_context=None,
    )

    state = render.state_for_outcome(outcome)
    if as_json:
        _emit(
            render.render_json(
                state,
                verdict=verdict,
                outcome=outcome,
                command=label,
                workspace=str(workspace.root),
            ),
            as_json=True,
            out=out,
        )
    else:
        _emit(
            render.render_outcome(outcome, verdict=verdict, command=label),
            as_json=False,
            out=out,
        )
    return render.EXIT_FOR_STATE[state]


def _secret_verdicts(
    workspace: Workspace, targets: Sequence[Path]
) -> tuple[SecretVerdict, ...]:
    """Classify resolved targets against the secret rules.

    Errors here are not swallowed. A classifier that cannot decide should not
    produce a quiet "probably fine": ``secrets.py`` already fails closed on an
    unreadable candidate, and this must not paper over that by catching it.
    """
    if not targets:
        return ()
    secrets = SecretClassifier(workspace)
    return tuple(secrets.classify(target) for target in targets)


def _run_check_path(
    args: argparse.Namespace,
    workspace: Workspace,
    approver: Approver,
    *,
    as_json: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    secrets = SecretClassifier(workspace)
    paths: list[str] = list(args.paths)

    # A relative path an operator typed is relative to the workspace they named,
    # not to whatever shell directory they happened to be in. `Workspace.resolve`
    # resolves relative to the process working directory, so handing it "x.json"
    # from elsewhere would classify a different file than the operator meant --
    # and on an SSH host that is a silent wrong answer, not a visible error.
    # This is path interpretation, not policy: the classifier still decides.
    resolved_paths: list[Path] = [
        Path(p) if Path(p).is_absolute() else workspace.root / p for p in paths
    ]

    exit_code = EXIT_OK
    emitted: list[str] = []
    payloads: list[str] = []

    for candidate, resolved in zip(paths, resolved_paths):
        verdict = secrets.classify(resolved)

        if args.classify_only:
            state = render.state_for_verdict(verdict)
            if as_json:
                payloads.append(
                    render.render_json(
                        state,
                        verdict=verdict,
                        command=candidate,
                        workspace=str(workspace.root),
                    )
                )
            else:
                emitted.append(
                    render.render_verdict(
                        verdict, command=candidate, workspace=str(workspace.root)
                    )
                )
            exit_code = max(exit_code, render.EXIT_FOR_STATE[state])
            continue

        outcome = approver.submit_secret(
            verdict,
            agent=args.agent,
            tool="read_file",
            intent=None,
            git_context=None,
        )
        state = render.state_for_outcome(outcome)
        if as_json:
            payloads.append(
                render.render_json(
                    state,
                    verdict=verdict,
                    outcome=outcome,
                    command=candidate,
                    workspace=str(workspace.root),
                )
            )
        else:
            emitted.append(
                render.render_outcome(outcome, verdict=verdict, command=candidate)
            )
        exit_code = max(exit_code, render.EXIT_FOR_STATE[state])

    if as_json:
        # One path per object, newline-delimited: a list would need a wrapper
        # shape that a single-path call would then not have.
        for payload in payloads:
            print(payload, file=out)
    else:
        for block in emitted:
            print(block, file=out)

    return exit_code


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    channel: ApprovalChannel | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    """Run the CLI and return an exit code.

    Args:
        argv: Arguments excluding the program name. Defaults to ``sys.argv[1:]``.
        channel: An approval channel to use instead of constructing a terminal
            one. Present so tests can drive the real approval path without a
            TTY. It is not an override of policy -- it goes through the same
            ``Approver`` and the same availability check.
        out: Stream for results. Defaults to stdout.
        err: Stream for usage and failure messages. Defaults to stderr.
        environ: Environment mapping. Defaults to ``os.environ``.

    Returns:
        One of the exit codes in ``forge.cli.render``. Never raises for an
        expected refusal; an unexpected exception becomes ``EXIT_INTERNAL``
        with a message on stderr rather than a traceback, because a CLI that
        dumps a traceback at an operator tells them nothing actionable and may
        print internals.
    """
    raw = list(sys.argv[1:] if argv is None else argv)

    out_stream: TextIO = out if out is not None else sys.stdout
    err_stream: TextIO = err if err is not None else sys.stderr
    env: dict[str, str] = environ if environ is not None else dict(os.environ)

    parser = build_parser()

    try:
        args = parser.parse_args(raw)
        # Retained because the -- separator is load-bearing and argparse's
        # REMAINDER swallows it; the check needs to know it was present.
        setattr(args, "_raw_argv", raw)

        if args.subcommand is None:
            print(STATUS, file=out_stream, end="" if STATUS.endswith("\n") else "\n")
            return EXIT_OK

        if args.subcommand == "check":
            if getattr(args, "leaf", None) is None:
                parser.parse_args(["check", "--help"])
                return EXIT_USAGE

            workspace, problem = _resolve_workspace(
                getattr(args, "workspace", None), env
            )
            if workspace is None:
                print(f"forge: {problem}", file=err_stream)
                return EXIT_USAGE

            approver = Approver(
                _make_channel(args, out_stream, channel)
            )
            as_json = bool(getattr(args, "as_json", False))

            if args.leaf == "command":
                return _run_check_command(
                    args,
                    workspace,
                    approver,
                    as_json=as_json,
                    out=out_stream,
                    err=err_stream,
                )
            if args.leaf == "path":
                return _run_check_path(
                    args,
                    workspace,
                    approver,
                    as_json=as_json,
                    out=out_stream,
                    err=err_stream,
                )

            print(f"forge: unknown check subject: {args.leaf!r}", file=err_stream)
            return EXIT_USAGE

        print(f"forge: unknown command: {args.subcommand!r}", file=err_stream)
        return EXIT_USAGE

    except SystemExit:
        # argparse raises SystemExit for --help, --version, and usage errors. It
        # already wrote its own message; translate the code rather than letting
        # a bare SystemExit(0) escape from a function documented to return int.
        raise
    except BrokenPipeError:
        # Piping into head, or an SSH session that dropped mid-write. Not an
        # internal failure and not a refusal.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print("interrupted", file=err_stream)
        return EXIT_INTERNAL
    except Exception as exc:  # noqa: BLE001 - deliberate boundary
        print(f"forge: internal error: {exc.__class__.__name__}: {exc}", file=err_stream)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover - exercised by manual invocation
    sys.exit(main())