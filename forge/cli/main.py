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

#: The requester identity this CLI reports when the *operator* invokes a check.
#: `forge byte` passes "byte" explicitly, because there the request genuinely is
#: Byte's and claiming otherwise would make the approval prompt misleading.
DEFAULT_AGENT = "forge-cli"

#: Workspace roots are explicit by settled decision -- not a default, not
#: inferred. The environment variable is an explicit statement of intent from
#: whoever set it; falling back to the current directory would not be, and over
#: SSH a wrong working directory is exactly the failure that matters.
WORKSPACE_ENV = "FORGE_WORKSPACE"

#: Model configuration. Byte has no fixed model by settled decision, so this is a
#: default to be overridden, never a baked-in choice.
DEFAULT_MODEL_ENV = "FORGE_MODEL"
DEFAULT_MODEL = "anthropic/claude-sonnet-4"

#: Model provider. OpenRouter remains the default, while local selects the
#: llama.cpp gateway running on this machine.
MODEL_PROVIDER_ENV = "FORGE_MODEL_PROVIDER"
DEFAULT_MODEL_PROVIDER = "openrouter"

#: Where `forge byte` reads a long task from. A file rather than stdin, because a
#: piped prompt collides awkwardly with `--workspace` and with a future chat mode.
TASK_FILE_FLAG = "--task-file"

#: The Workbench bind address is fixed, and not configurable. Stated as a
#: constant rather than merely omitted from the parser so that a future edit
#: adding a `--host` flag has to delete a named safety property rather than
#: just supply a new argument.
WORKBENCH_BIND_HOST = "127.0.0.1"

#: Default loopback port for `forge workbench`.
DEFAULT_WORKBENCH_PORT = 8765

DESCRIPTION = """\
Forge — orchestration layer for Steel & Code agents.

Classifies operations against the Forge safety stack and, where a decision
needs one, asks for approval. Executes nothing: approving an operation records
the decision, and says so.

`forge byte` runs the agent: it sends a task to a model, routes every tool call
through the same safety stack, and executes only what Forge decided was
permitted.
"""

STATUS = """\
Forge terminal interface — current state

  Workspace containment    implemented
  Secret classification    implemented
  Command classification   implemented
  Terminal approval        implemented
  Command execution        implemented
  Agent runtime            implemented
  Model gateway            implemented (OpenRouter + offline ScriptedGateway)
  Session memory           in-process, per run

`forge check` classifies and asks; it executes nothing.
`forge byte` runs an agent and does execute -- through the same pipeline.
`forge workbench` serves a local browser view of one Byte run on loopback.

Usage:
  forge check command --workspace DIR [-- PROGRAM [ARG...]]
  forge check path --workspace DIR PATH [PATH...]
  forge byte --workspace DIR [--model ID] [--max-turns N] "task"
  forge byte --workspace DIR --task-file PATH
  forge workbench --workspace DIR [--model ID] [--port 8765]
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

    byte_parser = sub.add_parser(
        "byte",
        help="run the Byte agent on a task",
        description=(
            "Run Byte on one task. Byte calls Forge tools, and every call is "
            "classified by Forge: the model decides what to attempt, Forge "
            "decides whether it is allowed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  forge byte --workspace . \"fix the failing tests\"\n"
            "  forge byte --workspace . --task-file task.md\n"
            "  forge byte --workspace . --max-turns 10 \"summarise this repo\"\n"
        ),
    )
    byte_parser.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "explicit workspace root (required; may also be set via "
            f"{WORKSPACE_ENV}). Not inferred from the current directory."
        ),
    )
    byte_parser.add_argument(
        "--task-file",
        metavar="PATH",
        default=None,
        help="read the task from a file instead of argv",
    )
    byte_parser.add_argument(
        "--model",
        metavar="ID",
        default=None,
        help=(
            f"provider model id (default: ${DEFAULT_MODEL_ENV}, else "
            f"{DEFAULT_MODEL})"
        ),
    )
    byte_parser.add_argument(
        "--max-turns",
        metavar="N",
        type=int,
        default=None,
        help=(
            "cap model round-trips for this run. May lower the ceiling, never "
            "raise it."
        ),
    )
    byte_parser.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "run with no interactive approval channel. Anything "
            "approval-required fails closed as denied unattended."
        ),
    )
    byte_parser.add_argument(
        "task",
        nargs="?",
        default=None,
        metavar="TASK",
        help="the task, as one argument",
    )

    workbench_parser = sub.add_parser(
        "workbench",
        help="serve a local browser surface for one Byte run",
        description=(
            "Serve the Forge Workbench on this machine: a single-page view of "
            "one Byte run, its tool activity, and any approval it needs. A "
            "human is present, so an approval-required operation prompts and "
            "waits -- this is the same question the terminal asks, asked "
            "through a browser.\n\n"
            "Binds 127.0.0.1 only, with no option to widen it. Every request "
            "carrying a capability requires a per-process token plus a "
            "same-origin check (exact Origin on a POST; on a GET, exact "
            "Origin or the browser's own Sec-Fetch-Site, since a same-origin "
            "GET carries no Origin header). There is no flag for a public "
            "host in v1, and an approval-required operation with no browser "
            "attached still fails closed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  forge workbench --workspace .\n"
            "  forge workbench --workspace . --model anthropic/claude-sonnet-4\n"
            "  forge workbench --workspace . --port 9000\n"
        ),
    )
    workbench_parser.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "explicit workspace root (required; may also be set via "
            f"{WORKSPACE_ENV}). Not inferred from the current directory."
        ),
    )
    workbench_parser.add_argument(
        "--model",
        metavar="ID",
        default=None,
        help=(
            f"provider model id (default: ${DEFAULT_MODEL_ENV}, else "
            f"{DEFAULT_MODEL})"
        ),
    )
    workbench_parser.add_argument(
        "--port",
        metavar="N",
        type=int,
        default=DEFAULT_WORKBENCH_PORT,
        help=(
            "loopback port to bind (default: %(default)s). This is not a host "
            "setting: the bind address is fixed at 127.0.0.1 and there is no "
            "flag to change it."
        ),
    )

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
        workspace=workspace.root,
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
# forge byte
# --------------------------------------------------------------------------


def _read_task(args: argparse.Namespace, err: TextIO) -> str | None:
    """Resolve the task from argv or a file.

    Exactly one source, and the error says so rather than silently preferring
    one. Two sources is ambiguous, and picking a winner would run a task the
    operator did not ask for.
    """
    from_file = getattr(args, "task_file", None)
    inline = getattr(args, "task", None)

    if from_file and inline:
        print(
            "forge: give the task either as an argument or with --task-file, "
            "not both",
            file=err,
        )
        return None

    if from_file:
        try:
            text = Path(from_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"forge: task file could not be read: {exc}", file=err)
            return None
        if not text.strip():
            print(f"forge: task file is empty: {from_file}", file=err)
            return None
        return text.strip()

    if inline and inline.strip():
        return inline.strip()

    print(
        'forge: no task given. For example:\n'
        '  forge byte --workspace . "fix the failing tests"',
        file=err,
    )
    return None

def _build_model_gateway(
    environ: dict[str, str],
):
    """Construct the configured model gateway."""

    from ..models.base import GatewayError
    from ..models.local import LocalLlamaGateway
    from ..models.openrouter import OpenRouterGateway

    provider = (
        environ.get(MODEL_PROVIDER_ENV, DEFAULT_MODEL_PROVIDER)
        .strip()
        .lower()
    )

    if provider == "local":
        return LocalLlamaGateway()

    if provider == "openrouter":
        return OpenRouterGateway(environ=environ)

    raise GatewayError(
        f"unknown model provider {provider!r}; "
        "expected 'openrouter' or 'local'"
    )

def _run_byte(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    out: TextIO,
    err: TextIO,
    environ: dict[str, str],
    channel: ApprovalChannel | None,
) -> int:
    """Run Byte on one task.

    This is the first place in Forge that executes, and it does so through the
    dispatcher -- the same pipeline `forge check` stops short of. Nothing here
    classifies, approves, or executes directly; the CLI holds no policy, exactly
    as it did before the runtime existed.
    """
    from ..agents.byte import BYTE, assemble_system_prompt
    from ..agents.runtime import AgentRuntime, MaxTurnsExceeded, summarise
    from ..models.base import GatewayError
    from ..tools.dispatcher import ToolDispatcher
    from ..tools.execution import Executor

    task = _read_task(args, err)
    if task is None:
        return EXIT_USAGE

    model_id = (
        getattr(args, "model", None)
        or environ.get(DEFAULT_MODEL_ENV)
        or DEFAULT_MODEL
    )

    # The provider key is read by the gateway layer alone, from the injected
    # mapping. It is never threaded through the profile, the state, or a tool
    # result, and the runtime is constructed before anything can reach it.
    try:
        gateway = _build_model_gateway(environ)
    except GatewayError as exc:
        print(f"forge: {exc}", file=err)
        return EXIT_USAGE

    # `--unattended` means no channel, which correctly turns every
    # approval-required operation into DENIED_UNATTENDED. Otherwise use an
    # injected channel if one was supplied (tests), else the terminal.
    approver_channel: ApprovalChannel | None
    if getattr(args, "unattended", False):
        approver_channel = None
    else:
        approver_channel = channel or TerminalApprovalChannel(prompt_stream=out)

    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(approver_channel),
        executor=Executor(workspace),
        # The request genuinely is Byte's here, so the approval prompt says so.
        agent_name=BYTE.name,
        allowed_tools=BYTE.allowed_tools,
        system_prompt=assemble_system_prompt(BYTE, str(workspace.root)),
    )

    runtime = AgentRuntime(
        BYTE,
        gateway=gateway,
        dispatcher=dispatcher,
        model_id=model_id,
        max_turns=getattr(args, "max_turns", None),
    )

    try:
        result = runtime.run(task)
    except MaxTurnsExceeded as exc:
        print(f"forge: {exc}", file=err)
        return EXIT_INTERNAL
    except GatewayError as exc:
        # Already operator-safe: the gateway never puts provider bodies, request
        # payloads, or credentials in its messages.
        print(f"forge: {exc}", file=err)
        return EXIT_INTERNAL

    print(summarise(result), file=out)
    print("", file=out)
    print(result.text, file=out)
    return EXIT_OK


# --------------------------------------------------------------------------
# forge workbench
# --------------------------------------------------------------------------


def _run_workbench(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    out: TextIO,
    err: TextIO,
    environ: dict[str, str],
) -> int:
    """Serve the Workbench on loopback.

    Like `_run_byte`, this assembles a pipeline and decides nothing: the model
    id is resolved from configuration, the gateway is built from the injected
    environment, and every permission decision stays in ``forge/tools``.

    Two things are deliberately absent. There is no ``--host``, because a
    browser surface that can be bound to a public interface is a different
    product with a different threat model. And there is no automatic browser
    launch, because spawning a browser is a side effect of starting a server
    that nobody asked for, and the operator who typed the command is already
    at a terminal with the URL in front of them.
    """
    from ..agents.byte import BYTE
    from ..models.base import GatewayError
    from ..ui import UiUnavailable
    from ..ui.broker import SseBroker
    from ..ui.runner import runtime_factory
    from ..ui.security import UiToken
    from ..ui.server import create_app
    from ..ui.session import WorkbenchSession

    port = getattr(args, "port", DEFAULT_WORKBENCH_PORT)
    if not (1 <= int(port) <= 65535):
        print(f"forge: port must be between 1 and 65535, not {port}", file=err)
        return EXIT_USAGE

    model_id = (
        getattr(args, "model", None)
        or environ.get(DEFAULT_MODEL_ENV)
        or DEFAULT_MODEL
    )

    # The credential is read here, in this process, from the injected mapping.
    # It is never threaded into the session, the broker, the app, or a response
    # body -- a page that can read the provider key is a page that can spend the
    # operator's money.
    try:
        gateway = _build_model_gateway(environ)
    except GatewayError as exc:
        print(f"forge: {exc}", file=err)
        return EXIT_USAGE

    try:
        import uvicorn  # noqa: PLC0415 - optional dependency, checked at use
    except ImportError:
        print(
            "forge: the Workbench needs its optional dependencies.\n"
            "  pip install 'forge[workbench]'",
            file=err,
        )
        return EXIT_USAGE

    broker = SseBroker()
    session = WorkbenchSession(
        workspace=workspace,
        profile=BYTE,
        model_id=model_id,
        runtime_factory=runtime_factory(
            workspace,
            profile=BYTE,
            gateway=gateway,
            model_id=model_id,
        ),
        broker=broker,
    )

    token = UiToken()
    try:
        app = create_app(
            workspace=workspace,
            session=session,
            gateway=gateway,
            token=token,
            port=int(port),
        )
    except ImportError as exc:  # pragma: no cover - guarded above
        raise UiUnavailable(str(exc)) from exc

    print(f"forge: workbench on http://{WORKBENCH_BIND_HOST}:{port}", file=out)
    print(f"forge: workspace {workspace.root}", file=out)
    print(f"forge: model {model_id}", file=out)
    print("forge: bound to loopback; approval-required operations need you", file=out)

    uvicorn.run(app, host=WORKBENCH_BIND_HOST, port=int(port), log_level="warning")
    return EXIT_OK


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

        if args.subcommand == "byte":
            workspace, problem = _resolve_workspace(
                getattr(args, "workspace", None), env
            )
            if workspace is None:
                print(f"forge: {problem}", file=err_stream)
                return EXIT_USAGE

            return _run_byte(
                args,
                workspace,
                out=out_stream,
                err=err_stream,
                environ=env,
                channel=channel,
            )

        if args.subcommand == "workbench":
            workspace, problem = _resolve_workspace(
                getattr(args, "workspace", None), env
            )
            if workspace is None:
                print(f"forge: {problem}", file=err_stream)
                return EXIT_USAGE

            return _run_workbench(
                args,
                workspace,
                out=out_stream,
                err=err_stream,
                environ=env,
            )

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
