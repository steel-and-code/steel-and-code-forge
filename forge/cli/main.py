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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence, TextIO

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    # Type-only so `forge check` still does not pay for the agent package. The
    # concrete profiles are imported inside the functions that need them, for
    # the same reason the gateway and registry imports are deferred.
    from ..agents.profile import AgentProfile

from ..tools.approval import (
    ApprovalChannel,
    Approver,
    TerminalApprovalChannel,
    effective_classification,
)
from ..tools.commands import Command, CommandClassifier, CommandVerdict
from ..tools.secrets import SecretClassifier, SecretVerdict
from ..models.router import ModelRouter
from ..tools.workspace import Classification, Workspace
from . import render
from .render import (
    EXIT_INTERNAL,
    EXIT_MODEL_UNAVAILABLE,
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
#: default to be overridden, never a baked-in choice. There is deliberately no
#: DEFAULT_MODEL constant: the default model now comes from the registry, and a
#: fallback string here would be a second source of truth that silently wins
#: whenever configuration is broken -- which is exactly the failure the registry
#: was built to remove.
DEFAULT_MODEL_ENV = "FORGE_MODEL"

#: The task kind each agent's default routing asks about, and the difficulty it
#: assumes. Named here because it is a statement about what an agent *is for*,
#: not a model choice: Byte writes and fixes code, Neighbor advises and
#: carries out requested work. Neither this pair nor any model id appears in an
#: agent profile -- a task kind is a statement about purpose, and purpose is
#: known by the surface invoking the agent, not by the agent's configuration.
BYTE_TASK_KIND = "coding"
BYTE_TASK_DIFFICULTY = "standard"

#: Neighbor's pair. ``conversation`` already exists in the packaged
#: ``models.toml``, so this is a reuse of a configured task kind rather than a
#: new one: an unlisted kind would still route, but it would route to
#: ``default_tier`` by falling through rather than by being configured, and
#: routing-by-accident is not a decision worth making on an agent's behalf.
NEIGHBOR_TASK_KIND = "conversation"
NEIGHBOR_TASK_DIFFICULTY = "standard"

#: Model provider. OpenRouter remains the default, while local selects the
#: llama.cpp gateway running on this machine.
MODEL_PROVIDER_ENV = "FORGE_MODEL_PROVIDER"
DEFAULT_MODEL_PROVIDER = "openrouter"

#: The model configuration file. Imported rather than duplicated: the CLI names
#: no model and no configuration path of its own, it only tells the registry
#: which path to prefer. See `forge.models.registry`.
from ..models.registry import LOCAL_CONFIG_NAME, MODELS_CONFIG_ENV  # noqa: E402

#: Where `forge byte` reads a long task from. A file rather than stdin, because a
#: piped prompt collides awkwardly with `--workspace` and with a future chat mode.
TASK_FILE_FLAG = "--task-file"

#: Wall-clock ceiling for one external-agent run. An external agent is a program
#: Forge does not control, so an unbounded subprocess is an unbounded run. Long
#: enough for a real reasoning task, short enough that a wedged child does not
#: hold a terminal indefinitely.
DEFAULT_HERMES_TIMEOUT = 900.0

#: Lets an operator point Forge at a Hermes installation that is not on PATH.
#: A program *name or absolute path*; never a shell fragment, because it is
#: passed as argv[0] and no shell is involved.
HERMES_PROGRAM_ENV = "FORGE_HERMES_PROGRAM"

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
`forge neighbor` does the same for Neighbor, on the same pipeline and the same
model routing.
`forge hermes` invokes the external Hermes Agent as a subprocess, text-only:
Forge cannot classify its internal tool calls, so it grants it no capabilities.
Hermes picks its own model; Forge only reports it.
`forge workbench` serves a local browser view of one Byte run on loopback.

Usage:
  forge check command --workspace DIR [-- PROGRAM [ARG...]]
  forge check path --workspace DIR PATH [PATH...]
  forge byte --workspace DIR [--model ID] [--max-turns N] "task"
  forge byte --workspace DIR --task-file PATH
  forge neighbor --workspace DIR [--model ID] [--max-turns N] "task"
  forge neighbor --workspace DIR --task-file PATH
  forge hermes --workspace DIR [--max-turns N] [--timeout SECONDS] "task"
  forge hermes --workspace DIR --task-file PATH
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
            "provider model id. Overrides ${DEFAULT_MODEL_ENV} and the model "
            "registry; the default is resolved by routing."
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

    # `forge neighbor` is deliberately a second subcommand rather than a
    # `--agent` flag on `forge byte`. A flag would mean `forge byte --agent
    # neighbor`, which names two agents in one command and makes the eventual
    # `forge run --agent X` form look like a bugfix rather than the shape it
    # should have been. One verb per agent keeps every existing spelling of
    # `forge byte` exactly as it was, and the parser mirrors the runner: two
    # thin wrappers over one `_run_agent`.
    #
    # The options are the same six Byte's are, and that is a decision rather
    # than a copy: an agent is permitted to be configured narrower than the
    # baseline but the baseline is otherwise identical for every agent, so a
    # divergent option set here would be Forge inventing a per-agent permission
    # surface. `--model` is an operator override on the run, not a property of
    # the agent, and is present for the same reason it is on `forge byte`.
    neighbor_parser = sub.add_parser(
        "neighbor",
        help="run the Neighbor agent on a task",
        description=(
            "Run Neighbor on one task. Neighbor calls Forge tools, and every "
            "call is classified by Forge exactly as Byte's are: the model "
            "decides what to attempt, Forge decides whether it is allowed.\n\n"
            "Neighbor is collaborative and general-purpose -- it advises and "
            "carries out requested work rather than fixing what it notices. "
            "That difference is in its instructions, not in its permissions: "
            "the tool set and the safety stack are Byte's."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  forge neighbor --workspace . \"what does this project do?\"\n"
            "  forge neighbor --workspace . --task-file task.md\n"
            "  forge neighbor --workspace . --max-turns 10 \"explain this module\"\n"
        ),
    )
    neighbor_parser.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "explicit workspace root (required; may also be set via "
            f"{WORKSPACE_ENV}). Not inferred from the current directory."
        ),
    )
    neighbor_parser.add_argument(
        "--task-file",
        metavar="PATH",
        default=None,
        help="read the task from a file instead of argv",
    )
    neighbor_parser.add_argument(
        "--model",
        metavar="ID",
        default=None,
        help=(
            "provider model id. Overrides ${DEFAULT_MODEL_ENV} and the model "
            "registry; the default is resolved by routing."
        ),
    )
    neighbor_parser.add_argument(
        "--max-turns",
        metavar="N",
        type=int,
        default=None,
        help=(
            "cap model round-trips for this run. May lower the ceiling, never "
            "raise it."
        ),
    )
    neighbor_parser.add_argument(
        "--unattended",
        action="store_true",
        help=(
            "run with no interactive approval channel. Anything "
            "approval-required fails closed as denied unattended."
        ),
    )
    neighbor_parser.add_argument(
        "task",
        nargs="?",
        default=None,
        metavar="TASK",
        help="the task, as one argument",
    )

    hermes_parser = sub.add_parser(
        "hermes",
        help="invoke the external Hermes Agent on a task",
        description=(
            "Invoke the Hermes Agent -- an existing, separate agent runtime -- as "
            "an external Forge agent. Hermes runs as a subprocess with its own "
            "tools and its own model; Forge cannot see or classify its internal "
            "tool calls, so it is granted no capabilities at all in this "
            "version.\n\n"
            "TEXT-ONLY. Hermes receives no toolsets: no web, no terminal, no "
            "file, no code execution. It reads the task and answers in prose. "
            "That is the safety boundary, not an oversight -- there is no flag "
            "to widen it, because widening an external agent's authority is a "
            "change to Forge's safety architecture and belongs in a reviewed "
            "code change.\n\n"
            "The task text is classified by Forge before anything is spawned, "
            "and a task that reads a secret source or asks for shell execution "
            "is refused. Forge cannot classify what it cannot observe, so this "
            "is the only gate available and it fails closed.\n\n"
            "HERMES CHOOSES ITS OWN MODEL. There is no --model flag: Hermes "
            "manages its own provider, credentials, and routing, and Forge "
            "reports the model it chose rather than overriding it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  forge hermes --workspace . \"what does this project do?\"\n"
            "  forge hermes --workspace . --task-file task.md\n"
            "  forge hermes --workspace . --max-turns 4 \"summarise the docs\"\n"
        ),
    )
    hermes_parser.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help=(
            "explicit workspace root (required; may also be set via "
            f"{WORKSPACE_ENV}). Not inferred from the current directory. Also "
            f"becomes the subprocess's working directory."
        ),
    )
    hermes_parser.add_argument(
        "--task-file",
        metavar="PATH",
        default=None,
        help="read the task from a file instead of argv",
    )
    hermes_parser.add_argument(
        "--max-turns",
        metavar="N",
        type=int,
        default=None,
        help=(
            "advisory ceiling on the child's tool-calling iterations. Advisory "
            "because Forge cannot enforce it; it is passed through to Hermes."
        ),
    )
    hermes_parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=DEFAULT_HERMES_TIMEOUT,
        help=(
            "wall-clock ceiling for the whole run (default: %(default)s). An "
            "external agent is a program Forge does not control, so this is "
            "Forge's only bound on it."
        ),
    )
    hermes_parser.add_argument(
        "task",
        nargs="?",
        default=None,
        metavar="TASK",
        help="the task, as one argument",
    )

    route_parser = sub.add_parser(
        "route",
        help="report which model would serve a request, and why",
        description=(
            "Resolve a request to a model through the model registry and report "
            "the decision: the capability tier, the model, the provider, "
            "whether the primary or a fallback was selected, and the reason.\n\n"
            "Decides nothing else and calls nothing. No gateway is built, no "
            "network request is made, and no credential is read, so this is "
            "safe to run offline and unauthenticated."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  forge route --agent byte --task coding --difficulty standard\n"
            "  forge route --task coding --difficulty hard --json\n"
            "  forge route --tier reasoning_heavy\n"
        ),
    )
    route_parser.add_argument(
        "--agent",
        metavar="NAME",
        default=DEFAULT_AGENT,
        help=f"requester this routing decision is for (default: {DEFAULT_AGENT})",
    )
    route_parser.add_argument(
        "--task",
        metavar="KIND",
        default=None,
        help=(
            "task kind, matched against routing.tasks in the model "
            "configuration. An unlisted kind uses the default tier."
        ),
    )
    route_parser.add_argument(
        "--difficulty",
        metavar="LEVEL",
        default=None,
        help="difficulty within the task kind; defaults to standard",
    )
    route_parser.add_argument(
        "--tier",
        metavar="NAME",
        default=None,
        help=(
            "override the routing table and use this capability tier directly "
            "(one of general_light, coding_standard, reasoning_heavy)"
        ),
    )
    route_parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            f"model configuration file. Defaults to ${MODELS_CONFIG_ENV}, then "
            f"./{LOCAL_CONFIG_NAME}, then the packaged default."
        ),
    )
    route_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit one JSON object instead of human-readable text",
    )

    models_parser = sub.add_parser(
        "models",
        help="inspect the model registry",
        description=(
            "Inspect the model registry: which capability tiers exist, their "
            "health, and the model each would use. Reads configuration only."
        ),
    )
    models_sub = models_parser.add_subparsers(dest="leaf", metavar="SUBJECT")

    models_status_parser = models_sub.add_parser(
        "status",
        help="show each capability tier's health",
        description=(
            "Show one line per capability tier: its health (GREEN when the "
            "primary is usable, YELLOW when only a fallback is, RED when "
            "nothing is), the model and provider that would serve it, and "
            "whether that is the primary or a fallback."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    models_status_parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            f"model configuration file. Defaults to ${MODELS_CONFIG_ENV}, then "
            f"./{LOCAL_CONFIG_NAME}, then the packaged default."
        ),
    )
    models_status_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit one JSON object per tier, newline-delimited",
    )
    models_status_parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "actively check each configured model with its provider and report "
            "verified health. Makes network requests (one per distinct model, "
            "no retries, no completion), and reads the provider credential "
            "needed to check. Without this flag no provider is contacted and "
            "health is configuration only."
        ),
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
            "provider model id. Overrides ${DEFAULT_MODEL_ENV} and the model "
            "registry; the default is resolved by routing."
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


@dataclass(frozen=True)
class ResolvedModel:
    """The model one run will use, and where that decision came from.

    A plain string is not enough at this boundary. Resolving the default now
    means routing, and routing produces three things a caller genuinely needs:
    the model id, the *provider* it belongs to, and the reason. Dropping the
    provider would mean re-deriving it from ``$FORGE_MODEL_PROVIDER`` and
    pairing a locally-routed model with a remote gateway.
    """

    model_id: str
    provider: str
    tier: str = ""
    selection: str = ""
    reason: str = ""
    #: True when this came from the registry rather than an operator override.
    routed: bool = False


class ModelResolutionError(Exception):
    """No usable model, or no usable configuration, for a run.

    Separate from ``GatewayError`` because the honest response differs. A
    gateway error is a provider that answered unusably; this is Forge declining
    to start a run on a model it cannot justify. It carries an operator-readable
    message and is caught at the CLI edge, where it becomes a usage error rather
    than a traceback.
    """


#: Shown on stderr when ``--model`` names a model this registry does not carry
#: as usable. A *warning*, never a block -- see ``_override_advisory``.
OVERRIDE_WARNING_PREFIX = "forge: warning:"

#: The override is an invocation-level decision, so these two sources are the
#: whole of it. Both are read fresh per run and neither is written anywhere.
#: Kept as names rather than inlined because the CLI parser, the help text, and
#: the docs all refer to them.
OVERRIDE_FLAG = "--model"
OVERRIDE_ENV = DEFAULT_MODEL_ENV


def _override_advisory(
    model_id: str, environ: Mapping[str, str]
) -> str | None:
    """What Forge can tell the operator about an overridden model, or ``None``.

    **This never blocks and never changes the resolved model.** The override
    path short-circuits before the registry is read, and that is settled in
    ``docs/model-registry.md``: pinning a model has to keep working when the
    configuration file is broken, which is exactly when an operator reaches for
    it. Turning a warning into a refusal would remove the escape hatch at the
    moment it is needed.

    So this is a *report*, and it is best-effort in the strongest sense: every
    failure to produce one -- an unreadable config, an unknown provider, a
    catalog that will not answer -- returns ``None`` and the run continues in
    silence. A warning system that can itself fail a run is a second policy.

    Three things are worth saying when the registry *can* be read:

    *   the model is not configured here at all (most often a typo);
    *   it is configured but ``enabled = false`` -- the operator's own switch;
    *   it is configured and enabled, but the availability source says it cannot
        serve a request right now.

    The third is reported only when something actually checked. With no
    availability source the registry knows nothing about reachability, and
    claiming a model is unavailable because nobody asked would be inventing a
    finding.

    Args:
        model_id: The identifier the operator selected.
        environ: The injected environment, for registry discovery.

    Returns:
        One operator-readable sentence, or ``None`` when there is nothing worth
        saying or nothing could be established.
    """
    from ..models.registry import RegistryConfigError, load_registry

    try:
        registry = load_registry(environ=environ)
    except RegistryConfigError:
        # Deliberately silent. The registry being unreadable is the exact
        # situation the override exists to survive, so complaining here would
        # either block the run or bury the operator in a warning about a file
        # they are deliberately bypassing.
        return None
    except Exception:  # noqa: BLE001 - a warning must never fail a run
        return None

    # `registry.tiers` is a mapping of name -> TierConfig, so iterating it
    # directly would yield names. Match on the full "provider/model" key first,
    # then on the bare id, so an operator who pasted either form gets a real
    # answer instead of a spurious "not configured".
    candidates = [
        slot
        for tier in registry.tiers.values()
        for slot in tier.slots
        if slot.key == model_id or slot.model == model_id
    ]

    if not candidates:
        return (
            f"{model_id!r} is not configured in the model registry "
            f"({registry.source}); the run will use it anyway, and the provider "
            f"decides whether it exists"
        )

    # The most useful single state across every slot carrying this id: a model
    # listed twice, once enabled and once not, is a configuration question, and
    # reporting the enabled instance would hide it.
    states = [registry.slot_state(slot) for slot in candidates]
    enabled = [state for state in states if state.value == "configured"]
    disabled = [state for state in states if state.value == "disabled"]
    unavailable = [state for state in states if state.value == "unavailable"]

    if disabled and not enabled:
        return (
            f"{model_id!r} is configured but disabled (enabled = false); the "
            f"run will attempt it anyway"
        )
    if unavailable and not enabled:
        return (
            f"{model_id!r} is configured but its provider reports it unusable "
            f"right now; the run will attempt it anyway"
        )
    return None


def _warn_about_override(
    model_id: str, environ: Mapping[str, str], warn: TextIO | None
) -> None:
    """Emit the advisory for an overridden model, if there is one to emit.

    A no-op when no stream is supplied, which is what every existing caller and
    every existing test does -- so adding the advisory changed no caller. The
    two run paths that can show an operator pass their ``err`` stream.

    The print is not wrapped in a try. A closed stderr is already handled at the
    CLI boundary by the ``BrokenPipeError`` branch in ``main``, and swallowing
    errors here would mean a warning could not be reported *and* could not be
    seen failing, which is the worst of both.
    """
    if warn is None:
        return
    message = _override_advisory(model_id, environ)
    if message:
        print(f"{OVERRIDE_WARNING_PREFIX} {message}", file=warn)


def _resolve_default_model(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    profile: AgentProfile | None = None,
    task_kind: str = BYTE_TASK_KIND,
    difficulty: str = BYTE_TASK_DIFFICULTY,
    warn: TextIO | None = None,
) -> ResolvedModel:
    """Decide which model a run uses, honouring overrides first.

    Precedence, unchanged from before this function existed:

    1. ``--model`` on the command line.
    2. ``$FORGE_MODEL`` in the environment.
    3. The configured registry, by routing.

    Only the third step is new. An explicit override short-circuits before the
    registry is read at all, so pinning a model keeps working when the
    configuration is broken -- the escape hatch stays usable precisely when it is
    needed.

    An override also keeps the pre-existing provider behaviour: the provider
    comes from ``$FORGE_MODEL_PROVIDER``, exactly as it always has. Only a
    *routed* default takes its provider from the selected slot, because that is
    the pairing configuration stated and mixing them would produce a model
    addressed to the wrong endpoint.

    The keyword arguments exist so a second agent routes by its own purpose
    rather than inheriting Byte's. They default to Byte's values, so every
    existing caller -- including the workbench, which is a browser surface onto
    a Byte run -- is byte-for-byte unchanged, and ``task_kind`` is a parameter
    precisely because it is not a profile field: it describes what the *caller*
    believes the agent is for, and a profile cannot assert its own purpose.
    """
    explicit = getattr(args, "model", None)
    if explicit:
        _warn_about_override(explicit, environ, warn)
        return ResolvedModel(
            model_id=explicit,
            provider=(
                environ.get(MODEL_PROVIDER_ENV, DEFAULT_MODEL_PROVIDER)
                .strip()
                .lower()
            ),
            reason="--model on the command line",
            routed=False,
        )

    from_env = environ.get(DEFAULT_MODEL_ENV, "").strip()
    if from_env:
        _warn_about_override(from_env, environ, warn)
        return ResolvedModel(
            model_id=from_env,
            provider=(
                environ.get(MODEL_PROVIDER_ENV, DEFAULT_MODEL_PROVIDER)
                .strip()
                .lower()
            ),
            reason=f"${DEFAULT_MODEL_ENV} in the environment",
            routed=False,
        )

    from ..models.registry import RegistryConfigError, load_registry
    from ..models.router import ModelRouter, RouteRequest

    try:
        registry = load_registry(environ=environ)
    except RegistryConfigError as exc:
        raise ModelResolutionError(str(exc)) from None

    # Imported here for the same reason every other runtime import in this
    # module is: `forge check` must not pay for the agent package, and the
    # requester identity has to be a true statement about who is asking.
    if profile is None:
        from ..agents.byte import BYTE

        profile = BYTE

    decision = ModelRouter(registry).route(
        RouteRequest(
            agent=profile.name,
            task=task_kind,
            difficulty=difficulty,
        )
    )

    if not decision.usable:
        raise ModelResolutionError(
            f"no usable model for {profile.name}'s default routing "
            f"(task {task_kind!r}, difficulty {difficulty!r}): "
            f"{decision.reason}. Pass --model ID or set ${DEFAULT_MODEL_ENV} to "
            f"choose one explicitly."
        )

    return ResolvedModel(
        model_id=decision.model,
        provider=decision.provider,
        tier=decision.tier,
        selection=decision.selection.value,
        reason=decision.reason,
        routed=True,
    )

def _build_model_gateway(
    environ: Mapping[str, str],
    provider: str | None = None,
):
    """Construct the model gateway for ``provider``.

    ``provider`` is passed in rather than read from the environment, because a
    routed default already decided it: a model selected out of a tier whose
    provider is ``local`` must be served by the local gateway even when
    ``$FORGE_MODEL_PROVIDER`` says otherwise. ``None`` keeps the old behaviour of
    reading the environment, which is still what an operator override uses.
    """

    from ..models.base import GatewayError
    from ..models.local import LocalLlamaGateway
    from ..models.openrouter import OpenRouterGateway

    if provider is None:
        provider = (
            environ.get(MODEL_PROVIDER_ENV, DEFAULT_MODEL_PROVIDER)
            .strip()
            .lower()
        )

    if provider == "local":
        # ``environ`` is the same injected mapping OpenRouter reads, so the
        # local timeout is configured the same way and no test has to mutate
        # the process environment to exercise either gateway.
        return LocalLlamaGateway(environ=environ)

    if provider == "openrouter":
        return OpenRouterGateway(environ=environ)

    raise GatewayError(
        f"unknown model provider {provider!r}; "
        "expected 'openrouter' or 'local'"
    )

def _run_agent(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    profile: AgentProfile,
    task_kind: str,
    difficulty: str,
    out: TextIO,
    err: TextIO,
    environ: dict[str, str],
    channel: ApprovalChannel | None,
) -> int:
    """Run one agent on one task.

    This is the first place in Forge that executes, and it does so through the
    dispatcher -- the same pipeline `forge check` stops short of. Nothing here
    classifies, approves, or executes directly; the CLI holds no policy, exactly
    as it did before the runtime existed.

    The function takes a profile rather than naming one, because the runtime
    already does: it holds a profile and never learns which agent it is serving,
    so a second agent needs a different *argument* here and not a different
    pipeline. Every permission decision below still belongs to
    ``forge/tools/dispatcher.py`` and the layers beneath it, and the
    ``agent_name`` handed to the dispatcher is the profile's real name, so the
    approval prompt cannot claim a request belongs to an agent that did not
    make it.
    """
    from ..agents.registry import system_prompt_for
    from ..agents.runtime import AgentRuntime, MaxTurnsExceeded, summarise
    from ..models.base import GatewayError
    from ..tools.dispatcher import ToolDispatcher
    from ..tools.execution import Executor

    task = _read_task(args, err)
    if task is None:
        return EXIT_USAGE

    # Resolved before the gateway is built, because the decision names the
    # provider. An operator override short-circuits inside the resolver without
    # reading the registry, so a broken configuration never blocks a run that
    # names its own model.
    try:
        resolved = _resolve_default_model(
            args,
            environ,
            profile=profile,
            task_kind=task_kind,
            difficulty=difficulty,
            warn=err,
        )
    except ModelResolutionError as exc:
        print(f"forge: {exc}", file=err)
        return EXIT_USAGE

    model_id = resolved.model_id

    # The provider key is read by the gateway layer alone, from the injected
    # mapping. It is never threaded through the profile, the state, or a tool
    # result, and the runtime is constructed before anything can reach it.
    try:
        gateway = _build_model_gateway(environ, provider=resolved.provider)
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
        # The request genuinely is this agent's here, so the approval prompt
        # says so.
        agent_name=profile.name,
        allowed_tools=profile.allowed_tools,
        system_prompt=system_prompt_for(profile, str(workspace.root)),
    )

    runtime = AgentRuntime(
        profile,
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

    Kept as its own function rather than folded into ``_run_agent`` so the
    Byte path is unchanged at the call site and in the parser: `forge byte`
    still means exactly what it meant before Neighbor existed.
    """
    from ..agents.byte import BYTE

    return _run_agent(
        args,
        workspace,
        profile=BYTE,
        task_kind=BYTE_TASK_KIND,
        difficulty=BYTE_TASK_DIFFICULTY,
        out=out,
        err=err,
        environ=environ,
        channel=channel,
    )


def _run_neighbor(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    out: TextIO,
    err: TextIO,
    environ: dict[str, str],
    channel: ApprovalChannel | None,
) -> int:
    """Run Neighbor on one task.

    Identical in shape to ``_run_byte`` and for the same reasons. The two differ
    only in which profile is passed and which task kind that profile's purpose
    routes under -- everything about how a call is classified, approved, or
    executed is the shared pipeline's business and neither function touches it.
    """
    from ..agents.neighbor import NEIGHBOR

    return _run_agent(
        args,
        workspace,
        profile=NEIGHBOR,
        task_kind=NEIGHBOR_TASK_KIND,
        difficulty=NEIGHBOR_TASK_DIFFICULTY,
        out=out,
        err=err,
        environ=environ,
        channel=channel,
    )


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
    from ..ui.models import ModelHealth
    from ..ui.runner import runtime_factory
    from ..ui.security import UiToken
    from ..ui.server import create_app
    from ..ui.session import WorkbenchSession

    port = getattr(args, "port", DEFAULT_WORKBENCH_PORT)
    if not (1 <= int(port) <= 65535):
        print(f"forge: port must be between 1 and 65535, not {port}", file=err)
        return EXIT_USAGE

    # Resolved the same way `forge byte` resolves it, because the DEFAULT_MODEL
    # constant this used to fall back on no longer exists -- and putting it back
    # would reintroduce the hard-coded model id the registry exists to remove.
    # The workbench is a browser surface onto the same Byte run, so it must not
    # be able to start a run on a model `forge byte` would refuse.
    try:
        resolved = _resolve_default_model(args, environ, warn=err)
    except ModelResolutionError as exc:
        print(f"forge: {exc}", file=err)
        return EXIT_USAGE

    model_id = resolved.model_id

    # The credential is read here, in this process, from the injected mapping.
    # It is never threaded into the session, the broker, the app, or a response
    # body -- a page that can read the provider key is a page that can spend the
    # operator's money.
    try:
        gateway = _build_model_gateway(environ, provider=resolved.provider)
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

    # The health snapshot comes from the same resolution order `forge route` and
    # `forge models status` use -- `load_registry` reads `$FORGE_MODELS_CONFIG`,
    # then `./models.toml`, then the packaged default -- rather than from a
    # second parse beside it. Two loads could disagree if the file changed
    # between them, and an indicator disagreeing with the run it reports on is
    # worse than no indicator.
    #
    # Not `_build_router`, deliberately: that helper prints the reason to stderr
    # and returns `None`, which would leave the page's 503 with a generic string
    # while the actual reason scrolled past in the terminal. An operator reading
    # the indicator is often not looking at the terminal.
    #
    # A configuration that will not load does NOT stop the workbench. An
    # operator who passed `--model` over a broken config still gets a surface
    # they can read and use, and the indicator reports why health is absent --
    # more useful than refusing to start with no diagnostic available.
    from ..models.registry import RegistryConfigError, load_registry

    try:
        health_router = ModelRouter(load_registry(environ=environ))
    except RegistryConfigError as exc:
        # Registry errors name a table and a rule and carry no file contents,
        # which is what makes this message safe to hand to a browser.
        health = ModelHealth(None, error=str(exc))
        print(f"forge: model health unavailable: {exc}", file=out)
    else:
        health = ModelHealth(health_router, environ=environ)
        # One verified pass at startup, before anything is served. The ruling is
        # that the operator does not have to ask "is it actually up?" -- the
        # first page load shows a checked answer. Once, never on a timer, and
        # never during a run: an operator re-checks explicitly from the page,
        # and the route refuses while a run is in flight.
        try:
            health.verify()
        except Exception as exc:  # noqa: BLE001 - startup must still serve
            # A provider failure is already a reported health value rather than
            # an exception, so reaching this means something structural went
            # wrong. Serving the offline snapshot beats refusing to start.
            print(
                f"forge: model health could not be checked ({exc}); showing "
                "configuration only",
                file=out,
            )

    token = UiToken()
    try:
        app = create_app(
            workspace=workspace,
            session=session,
            gateway=gateway,
            token=token,
            port=int(port),
            health=health,
            # Not passed: create_app derives the board from the workspace root,
            # so it lands in <workspace>/.forge/orders.json and no caller can
            # aim it somewhere else. Stated here because the CLI builds every
            # other collaborator explicitly.
        )
    except ImportError as exc:  # pragma: no cover - guarded above
        raise UiUnavailable(str(exc)) from exc

    print(f"forge: workbench on http://{WORKBENCH_BIND_HOST}:{port}", file=out)
    print(f"forge: workspace {workspace.root}", file=out)
    print(f"forge: model {model_id}", file=out)
    if resolved.routed:
        print(
            f"forge: tier {resolved.tier} ({resolved.selection}); {resolved.reason}",
            file=out,
        )
    print("forge: bound to loopback; approval-required operations need you", file=out)

    uvicorn.run(app, host=WORKBENCH_BIND_HOST, port=int(port), log_level="warning")
    return EXIT_OK




# --------------------------------------------------------------------------
# forge hermes
# --------------------------------------------------------------------------


def _run_hermes(
    args: argparse.Namespace,
    workspace: Workspace,
    *,
    out: TextIO,
    err: TextIO,
    environ: dict[str, str],
) -> int:
    """Invoke the external Hermes agent on one task.

    **This is not ``_run_agent``.** Byte and Neighbor are native profiles served
    by Forge's own runtime, with every tool call passing through Forge's
    dispatcher. Hermes is a separate program with its own runtime, its own tools,
    and its own safety decisions, and Forge cannot see a single one of its tool
    calls. The two are wired differently on purpose, and the difference is the
    point of this function.

    What it does, in order:

    1. reads the task (Byte's rules, unchanged -- one source, or it is ambiguous);
    2. **gates the task text** and refuses to spawn on anything but ``allow``;
    3. builds argv and a minimal environment through the adapter;
    4. runs it with a bounded timeout, never with ``--yolo``;
    5. prints Hermes's own text, and its own model/provider as a *report*.

    What it deliberately does not do: classify Hermes's internal calls (impossible
    from here), route Hermes's model (Hermes owns it), or offer a capability
    flag. There is no ``--model`` and no ``--toolsets`` on this subcommand, and
    their absence is the safety property rather than an oversight.
    """
    from ..external.base import ExternalInvocation
    from ..external.gating import gate_task
    from ..external.hermes import HermesAdapter
    from ..external.runner import spawn

    task = _read_task(args, err)
    if task is None:
        return EXIT_USAGE

    # Resolved per invocation rather than at import, so an operator can point
    # Forge at a different Hermes without editing configuration. Read from the
    # injected environment for the same reason every other setting is.
    program = (environ.get(HERMES_PROGRAM_ENV) or "").strip() or None
    agent = HermesAdapter(program) if program else HermesAdapter()

    if not agent.available():
        print(
            f"forge: the Hermes Agent was not found "
            f"({agent.describe() if program else 'not on PATH'}). Install it, "
            f"or set {HERMES_PROGRAM_ENV} to its path.",
            file=err,
        )
        return EXIT_USAGE

    # The gate runs before anything is spawned, and a refusal here means no
    # process ever starts. Failing closed is the whole point: an external agent
    # with no toolsets is a far smaller risk than one spawned because a
    # classifier threw.
    decision = gate_task(task, workspace)
    if not decision.allowed:
        print(
            f"forge: refusing to run the Hermes agent: {decision.reason}",
            file=err,
        )
        # Deliberately a usage error rather than a refusal code: the operator
        # asked for something this MVP does not do, and a distinct exit code
        # would suggest a policy verdict they could argue with.
        return EXIT_USAGE

    invocation = ExternalInvocation(
        agent=agent.name,
        task=task,
        workspace=str(workspace.root),
        timeout=float(
            getattr(args, "timeout", None) or DEFAULT_HERMES_TIMEOUT
        ),
        max_turns=getattr(args, "max_turns", None),
    )

    print(
        f"forge: {agent.name} (external, text-only) on {workspace.root}",
        file=out,
    )
    print(f"forge: gate {decision.rule}: {decision.reason}", file=out)

    # The launch itself lives in ``forge.external.runner`` so this module never
    # imports ``subprocess``. ``tests/test_cli.py`` asserts that structurally,
    # and the guard is settled: ``forge check`` must not be able to run
    # anything. Composition and rendering are this module's whole job.
    run_outcome = spawn(agent, invocation, environ=environ)
    result = run_outcome.result

    if run_outcome.timed_out:
        print(f"forge: {result.detail}", file=err)
        return EXIT_INTERNAL
    if run_outcome.launch_error:
        print(f"forge: {result.detail}", file=err)
        return EXIT_USAGE

    # Hermes reports the model it chose. Reported, never selected: Forge's
    # registry has no authority over an external agent's inference.
    if result.model:
        provider = result.provider or "unspecified"
        print(
            f"forge: hermes chose model {result.model} (provider {provider})",
            file=out,
        )
    if result.session_id:
        print(f"forge: hermes session {result.session_id}", file=out)

    if not result.ok:
        print(
            f"forge: hermes reported a failed run "
            f"(exit_code {result.exit_code}): {result.detail}",
            file=err,
        )
        # A nonzero child exit code is a visible failure, never swallowed. The
        # subprocess's own status is ignored for this purpose because it is
        # always 0 -- see forge/external/hermes.py.
        return EXIT_MODEL_UNAVAILABLE

    print("", file=out)
    print(result.text, file=out)
    return EXIT_OK


# --------------------------------------------------------------------------
# forge route / forge models status
# --------------------------------------------------------------------------


def _build_router(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    err: TextIO,
) -> ModelRouter | None:
    """Load the registry and build a router, or explain why it could not.

    Imported inside the function for the same reason the gateway imports are:
    `forge check` must not pay for the model layer, and a model layer that
    failed to import should surface as a usage error on the two commands that
    need it rather than as a broken `forge` overall.

    Returns ``None`` after printing, so the caller has one exit path.
    """
    from ..models.registry import RegistryConfigError, load_registry
    from ..models.router import ModelRouter as _ModelRouter

    try:
        registry = load_registry(getattr(args, "config", None), environ=environ)
    except RegistryConfigError as exc:
        print(f"forge: {exc}", file=err)
        return None

    return _ModelRouter(registry)


def _run_route(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    """Report which model would serve a request, and why.

    Executes nothing and builds no gateway. The exit code tells a script whether
    a model was available, which is the only thing here a caller might want to
    branch on: EXIT_MODEL_UNAVAILABLE when the tier cannot serve, EXIT_USAGE when
    the configuration itself is unusable.
    """
    from ..models.router import RouteRequest

    router = _build_router(args, environ, err=err)
    if router is None:
        return EXIT_USAGE

    if not (args.task or args.difficulty or args.tier):
        print(
            "forge: nothing to route. Name a task, a tier, or both, for\n"
            "example:\n"
            "  forge route --agent byte --task coding --difficulty standard",
            file=err,
        )
        return EXIT_USAGE

    decision = router.route(
        RouteRequest(
            agent=args.agent,
            task=args.task,
            difficulty=args.difficulty,
            tier=args.tier,
        )
    )

    if args.as_json:
        print(render.render_route_json(decision), file=out)
    else:
        print(
            render.render_route(decision, source=router.registry.source), file=out
        )

    return EXIT_OK if decision.usable else EXIT_MODEL_UNAVAILABLE


def _run_models_status(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    """One line per capability tier.

    Two paths, and the split is the important part:

    *   **Offline** (the default) reports configuration only. No socket, no
        credential, no gateway. This is what makes `forge models status` safe to
        run anywhere, including with no key exported.
    *   **``--verify``** asks each provider whether it can serve each configured
        model, then reports through the *same* health computation. Verification
        supplies facts; it never decides health, so a GREEN means exactly what it
        meant before verification existed.

    The dispatch happens here rather than inside the offline path so the offline
    handler provably cannot reach a probe: ``tests/test_model_cli.py`` and
    ``tests/test_model_verify.py`` both assert the offline function contains no
    credential reference and imports no gateway.

    Exit code reflects the worst tier: OK when every tier is GREEN, and
    EXIT_MODEL_UNAVAILABLE when any tier is RED. A RED capability is a real
    operational problem, so a script polling status should be able to notice
    without parsing the table.

    YELLOW does not affect the exit code. Traffic still flows on a degraded
    tier, and a command that failed on a working -- if not ideal -- capability
    would train an operator to ignore it.
    """
    if getattr(args, "verify", False):
        return _run_models_status_verified(args, environ, out=out, err=err)

    return _run_models_status_offline(args, environ, out=out, err=err)


def _run_models_status_offline(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    """Configuration-only health. Opens nothing, reads no credential.

    Kept separate from the verified path so "plain status performs zero network
    activity" is a structural property of a small function rather than a promise
    about a larger one.
    """
    router = _build_router(args, environ, err=err)
    if router is None:
        return EXIT_USAGE

    entries = router.status()

    if args.as_json:
        print(render.render_model_status_json(entries), file=out)
    else:
        print(
            render.render_model_status(
                tiers=entries,
                source=router.registry.source,
                verified=router.registry.availability.verified,
            ),
            file=out,
        )

    return _worst_tier_exit_code(entries)


def _run_models_status_verified(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    out: TextIO,
    err: TextIO,
) -> int:
    """Probe every configured model, then report the same health the offline
    path reports.

    **What this is allowed to do, and it is exactly this much.** Build a
    verifier per provider from the catalog, make one bounded request per distinct
    model, read the credential that request needs, and hand the results to the
    registry through the existing ``Availability`` seam. It does not build a
    gateway, does not generate a completion, does not retry, and does not touch
    the agent runtime, a workspace, or approvals.

    **Failure is a value here.** A provider that is down, a rejected key, a
    timeout, or a body that will not parse all produce a reported status. This is
    an inspection command; a traceback would tell an operator nothing about what
    to go and fix.
    """
    router = _build_router(args, environ, err=err)
    if router is None:
        return EXIT_USAGE

    # Imported here rather than at module scope for the same reason the
    # registry is: `forge check` must not import a module that opens sockets.
    from ..models.verify import verified_registry

    verified_router = ModelRouter(
        verified_registry(router.registry, environ=environ)
    )
    entries = verified_router.status()

    if args.as_json:
        print(render.render_model_status_json(entries), file=out)
    else:
        print(
            render.render_model_status(
                tiers=entries,
                source=verified_router.registry.source,
                verified=verified_router.registry.availability.verified,
            ),
            file=out,
        )

    return _worst_tier_exit_code(entries)


def _worst_tier_exit_code(entries: list[dict[str, object]]) -> int:
    """OK unless some tier is RED.

    Shared by both status paths so verification cannot introduce a second
    opinion about what the exit code means.
    """
    from ..models.tiers import Health

    return (
        EXIT_MODEL_UNAVAILABLE
        if any(entry["health"] == Health.RED.value for entry in entries)
        else EXIT_OK
    )


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

        if args.subcommand in ("byte", "neighbor"):
            workspace, problem = _resolve_workspace(
                getattr(args, "workspace", None), env
            )
            if workspace is None:
                print(f"forge: {problem}", file=err_stream)
                return EXIT_USAGE

            # Two names, two runners, one shared pipeline. The dispatch is a
            # table rather than an `if` chain so that adding a third agent is a
            # new entry here and a new wrapper, rather than a third branch
            # someone has to remember to widen.
            runner = {
                "byte": _run_byte,
                "neighbor": _run_neighbor,
            }[args.subcommand]

            return runner(
                args,
                workspace,
                out=out_stream,
                err=err_stream,
                environ=env,
                channel=channel,
            )

        if args.subcommand == "hermes":
            workspace, problem = _resolve_workspace(
                getattr(args, "workspace", None), env
            )
            if workspace is None:
                print(f"forge: {problem}", file=err_stream)
                return EXIT_USAGE

            # Kept out of the native dispatch table above on purpose: an
            # external agent is not a native profile and must not be reachable
            # through the code path that assumes one.
            return _run_hermes(
                args,
                workspace,
                out=out_stream,
                err=err_stream,
                environ=env,
            )

        if args.subcommand == "route":
            return _run_route(args, env, out=out_stream, err=err_stream)

        if args.subcommand == "models":
            if getattr(args, "leaf", None) is None:
                parser.parse_args(["models", "--help"])
                return EXIT_USAGE
            return _run_models_status(args, env, out=out_stream, err=err_stream)

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
