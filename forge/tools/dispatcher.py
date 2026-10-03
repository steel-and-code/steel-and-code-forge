"""Tool dispatch: the only path from a model's request to Forge's policy.

This module is the single runtime route into the safety stack. It classifies,
asks, and executes -- in that order, through the existing pipeline, adding no
decision of its own. ``forge/agents/runtime.py`` holds no classifier, no
``Approver`` and no ``Executor``; it calls a dispatcher. That is what keeps the
runtime from growing a second opinion about permissions.

Four properties, each of which exists because a decision in the spec depends on
it:

*   **Every tool call ends in a :class:`ToolResult`, never an exception.**
    A refusal is a value the model can read and act on. Only *internal* failures
    propagate, because those are not the model's to fix.

*   **No arbitrary callable is ever reachable by name.** Tools live in a closed
    registry built at construction. A model naming something absent from it gets
    ``unknown_tool``; there is no attribute lookup, no import, and no ``eval``
    anywhere in this file.

*   **A denial is answered once.** Final permission refusals are cached by
    canonical call identity, so an identical repeat returns the same result
    without re-classifying or re-prompting. Other failure classes are not cached.

*   **No file content is read before it is classified.** ``read_file`` and
    ``patch_file`` both resolve containment, classify against the secret rules,
    and obtain approval *before* opening the file. ``patch_file`` in particular
    does not read the existing content and classify afterwards -- that ordering
    would make the approval prompt unable to show what was about to change.

On process output, one honest statement rather than a reassuring one: **process
output is untrusted and may contain sensitive data, and Forge v1 provides no
general output-redaction guarantee.** Bounded stdout and stderr are returned to
the model so an agent can diagnose a failing test. Forge prevents known secret
sources from being *deliberately* read where the secret classifier can identify
them, but arbitrary subprocess output cannot be guaranteed secret-free, and
applying the path-based secret classifier to an unbounded byte stream is a
different problem with different failure modes. Bounding is what v1 does;
redaction is separate work that belongs after an audit trail exists to review
what was redacted.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..models.base import ToolCall
from .approval import (
    ApprovalChannel,
    ApprovalDecision,
    Approver,
)
from .commands import Command, CommandClassifier, CommandVerdict
from .execution import ExecutionRequest, ExecutionState, Executor
from .secrets import SecretClassifier, SecretVerdict
from .workspace import Resolution, Workspace, resolve_within

__all__ = [
    "ToolStatus",
    "ToolResult",
    "ToolImpl",
    "ToolDispatcher",
    "MAX_READ_LINE_LIMITS",
    "MAX_SEARCH_RESULTS",
    "MAX_WRITE_BYTES",
]


class ToolStatus(str, Enum):
    """The outcome of one tool call.

    A model can tell these apart, and the distinctions carry real information:

    * ``denied`` versus ``rejected`` -- policy said no, or a human said no.
      Block F of the system instructions tells Byte to report the second to the
      operator, and it cannot do that if both look the same.
    * ``denied_unattended`` -- nobody was there to ask. A distinct value because
      it is a reportable operational condition rather than an answer.
    * ``error`` versus ``invalid_arguments`` -- Forge broke, or the model did.
      Only the second is worth retrying with different arguments.
    """

    OK = "ok"
    ERROR = "error"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    DENIED = "denied"
    REJECTED = "rejected"
    DENIED_UNATTENDED = "denied_unattended"
    TIMEOUT = "timeout"
    LAUNCH_FAILED = "launch_failed"

    @property
    def is_permission_refusal(self) -> bool:
        """Whether this outcome is a final answer to a permission question.

        Only these three are cached. ``error``, ``timeout``, and
        ``invalid_arguments`` describe something that could legitimately succeed
        on a retry, so re-asking the model is correct rather than wasteful.
        """
        return self in (
            ToolStatus.DENIED,
            ToolStatus.REJECTED,
            ToolStatus.DENIED_UNATTENDED,
        )


@dataclass(frozen=True)
class ToolResult:
    """What one tool call produced.

    Always JSON-serialisable, because it crosses into provider JSON. ``content``
    is a plain dict rather than a dataclass so the runtime never has to know how
    to encode a result type it does not recognise.
    """

    tool: str
    status: ToolStatus
    content: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    def to_json(self) -> str:
        """Serialise for the model.

        Compact and single-line: a multi-line tool result is still one message in
        the transcript, and providers reject embedded newlines in some
        positions.
        """
        return json.dumps(
            {
                "tool": self.tool,
                "status": self.status.value,
                "content": self.content,
                "detail": self.detail,
            },
            separators=(",", ":"),
            default=str,
        )


@dataclass(frozen=True)
class ToolImpl:
    """One registered tool.

    ``handler`` receives already-validated keyword arguments and returns a
    :class:`ToolResult`. Validation happens in :meth:`ToolDispatcher.dispatch`
    before the handler is reached, so a handler never sees a malformed call and
    never has to defend against one.

    ``requires_workspace`` marks a tool that resolves paths against the run's
    workspace. Every tool here does, and the flag is what makes forgetting to
    resolve impossible for a future addition rather than a review item.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., ToolResult]
    requires_workspace: bool = True


# Output bounds. Every one of these exists because unbounded tool output is
# unbounded context, and context is the one resource the model loop cannot
# refuse to spend.
MAX_READ_LINE_LIMITS = 2000
MAX_READ_BYTES = 100_000
MAX_SEARCH_RESULTS = 200
MAX_WRITE_BYTES = 1_000_000


class ToolDispatcher:
    """Routes model tool calls through the Forge safety stack.

    Args:
        workspace: The run's single explicit workspace. Held for the run; the
            model cannot change it.
        approver: Where approval questions go. ``None`` means unattended, which
            correctly yields ``DENIED_UNATTENDED`` for anything gated.
        executor: Runs approved operations. Reached only through
            ``Approver.submit_command`` and only with an approved outcome.
        agent_name: Whose request this is, reported in the approval prompt.
        owned_pids: PIDs this run started, for the kill-provenance rule.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        approver: Approver,
        executor: Executor,
        agent_name: str,
        allowed_tools: Sequence[str] = (),
        owned_pids: frozenset[int] = frozenset(),
        system_prompt: str = "",
    ) -> None:
        self.workspace = workspace
        self.approver = approver
        self.executor = executor
        self.agent_name = agent_name
        self.allowed_tools = frozenset(allowed_tools)
        #: Assembled by the caller from the agent profile. Held here because the
        #: dispatcher already owns the workspace root, and the prompt needs it.
        self.system_prompt = system_prompt
        self._denials: dict[str, ToolResult] = {}
        self._classifier = CommandClassifier(workspace, owned_pids=owned_pids)
        self._secrets = SecretClassifier(workspace)
        self._registry = self._build_registry()

    # -- registry --------------------------------------------------------

    def tool_specs(self) -> tuple:
        """``ToolSpec`` values for the tools this agent may call.

        Only the allowed subset is offered. Offering a tool and then refusing it
        on every call would waste the model's attention on something that can
        never succeed.
        """
        from ..models.base import ToolSpec

        return tuple(
            ToolSpec(
                name=impl.name,
                description=impl.description,
                input_schema=impl.input_schema,
            )
            for impl in self._registry.values()
            if impl.name in self.allowed_tools
        )

    def _build_registry(self) -> dict[str, ToolImpl]:
        """The closed set of callable tools.

        Built here and nowhere else. There is no path from a model-supplied name
        to a Python object outside this dict.
        """
        impls = (
            ToolImpl(
                "read_file",
                "Read a text file inside the workspace.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "offset": {"type": "integer", "minimum": 1},
                        "limit": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                self._tool_read_file,
            ),
            ToolImpl(
                "search_files",
                "Find files by glob pattern inside the workspace.",
                {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["pattern"],
                },
                self._tool_search_files,
            ),
            ToolImpl(
                "search_content",
                "Search file contents by regular expression.",
                {
                    "type": "object",
                    "properties": {
                        "regex": {"type": "string"},
                        "path": {"type": "string"},
                        "glob": {"type": "string"},
                    },
                    "required": ["regex"],
                },
                self._tool_search_content,
            ),
            ToolImpl(
                "write_file",
                "Create a file or replace its contents entirely.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                self._tool_write_file,
            ),
            ToolImpl(
                "patch_file",
                "Replace one exact region of an existing file.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["path", "old", "new"],
                },
                self._tool_patch_file,
            ),
            ToolImpl(
                "exec_command",
                "Run a command as an argument array, not a shell string.",
                {
                    "type": "object",
                    "properties": {
                        "program": {"type": "string"},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["program"],
                },
                self._tool_exec_command,
            ),
        )
        return {impl.name: impl for impl in impls}

    # -- dispatch --------------------------------------------------------

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Turn one model tool call into a result.

        Never raises for a model-facing failure. Every branch returns a
        ``ToolResult`` whose status tells the model -- and the operator, since
        these results are rendered -- exactly what happened.
        """
        canonical = call.canonical()

        # A cached refusal. Returns before any classifier or channel work, and
        # does not consume a model turn: the turn was already spent getting here.
        cached = self._denials.get(canonical)
        if cached is not None:
            return cached

        if call.name not in self._registry:
            return ToolResult(
                tool=call.name,
                status=ToolStatus.UNKNOWN_TOOL,
                detail=f"no tool named {call.name!r}",
            )

        if call.name not in self.allowed_tools:
            # Narrowing is structural. An agent configured without a tool cannot
            # reach it even by naming it directly.
            return ToolResult(
                tool=call.name,
                status=ToolStatus.TOOL_NOT_ALLOWED,
                detail=(
                    f"the {self.agent_name} agent is not permitted to call "
                    f"{call.name!r}"
                ),
            )

        impl = self._registry[call.name]

        try:
            arguments = json.loads(call.arguments or "{}")
        except (ValueError, TypeError) as exc:
            # Not cached: a malformed call may be retried with corrected
            # arguments, and should reach the model again.
            return ToolResult(
                tool=call.name,
                status=ToolStatus.INVALID_ARGUMENTS,
                detail=f"arguments were not valid JSON: {exc.__class__.__name__}",
            )

        if not isinstance(arguments, dict):
            return ToolResult(
                tool=call.name,
                status=ToolStatus.INVALID_ARGUMENTS,
                detail="arguments must be a JSON object",
            )

        problem = _validate(impl.input_schema, arguments)
        if problem:
            return ToolResult(
                tool=call.name,
                status=ToolStatus.INVALID_ARGUMENTS,
                detail=problem,
            )

        try:
            result = impl.handler(**arguments)
        except Exception as exc:  # noqa: BLE001 - deliberate boundary
            # The exception class name and a bounded message, never a traceback.
            # A traceback reaches the model through the tool result, and carries
            # paths and file contents on its way.
            return ToolResult(
                tool=call.name,
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:500]}",
            )

        if result.status.is_permission_refusal:
            self._denials[canonical] = result

        return result

    # -- shared pre-flight -----------------------------------------------

    def _resolve_in_workspace(self, raw: str) -> Resolution:
        """Resolve a model-supplied path against the run's workspace.

        A relative path is resolved *against the workspace*, never the process
        working directory -- the same reasoning the CLI already applies: over SSH
        those differ, and classifying or reading a different file than the agent
        named is a silent wrong answer.

        Returns the ``Resolution`` rather than a path, so every caller must
        check ``allowed`` before touching ``resolved``. A helper that returned a
        bare path would make "did I check containment?" a review question.
        """
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace.root / candidate
        return resolve_within(self.workspace, candidate)

    def _approve_path_read(
        self, tool: str, path: Path
    ) -> ToolResult | None:
        """Containment, secret classification, and approval, in that order.

        Returns a ``ToolResult`` when the read must not proceed, or ``None`` when
        it may. Called *before* any file is opened.
        """
        resolution = self._resolve_in_workspace(str(path))
        if not resolution.allowed:
            return ToolResult(
                tool=tool,
                status=ToolStatus.DENIED,
                detail=f"path is outside the workspace: {resolution.reason}",
            )
        if resolution.resolved is None:
            return ToolResult(
                tool=tool,
                status=ToolStatus.DENIED,
                detail="path could not be resolved safely",
            )

        verdict = self._secrets.classify(resolution.resolved)
        outcome = self.approver.submit_secret(
            verdict,
            agent=self.agent_name,
            tool=tool,
        )

        if outcome.decision is ApprovalDecision.APPROVED:
            return None
        return self._result_from_outcome(tool, outcome.decision, outcome.detail)

    def _result_from_outcome(
        self, tool: str, decision: ApprovalDecision, detail: str
    ) -> ToolResult:
        """Map an approval decision onto a model-visible status."""
        if decision is ApprovalDecision.APPROVED:
            return ToolResult(tool=tool, status=ToolStatus.OK)
        if decision is ApprovalDecision.REJECTED:
            return ToolResult(tool=tool, status=ToolStatus.REJECTED, detail=detail)
        if decision is ApprovalDecision.DENIED_UNATTENDED:
            return ToolResult(
                tool=tool, status=ToolStatus.DENIED_UNATTENDED, detail=detail
            )
        return ToolResult(tool=tool, status=ToolStatus.DENIED, detail=detail)

    # -- tools -----------------------------------------------------------

    def _tool_read_file(
        self, path: str, offset: int | None = None, limit: int | None = None
    ) -> ToolResult:
        """Read a file, with an explicit byte budget.

        Classification happens before the open. Reading first and classifying
        after would mean the approval prompt could not show what the agent was
        about to see.
        """
        refusal = self._approve_path_read("read_file", Path(path))
        if refusal is not None:
            return refusal

        resolution = self._resolve_in_workspace(str(path))
        assert resolution.resolved is not None  # checked above

        if not resolution.resolved.exists():
            return ToolResult(
                tool="read_file",
                status=ToolStatus.ERROR,
                detail=f"no such file: {path}",
            )

        try:
            raw = resolution.resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError) as exc:
            return ToolResult(
                tool="read_file",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        truncated_bytes = len(raw.encode("utf-8")) > MAX_READ_BYTES
        raw = raw.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        lines = raw.splitlines()
        total_lines = len(lines)

        start = max(1, int(offset or 1))
        end = start + max(1, int(limit or MAX_READ_LINE_LIMITS)) - 1
        end = min(end, total_lines, start + MAX_READ_LINE_LIMITS - 1)
        selected = lines[start - 1 : end]

        return ToolResult(
            tool="read_file",
            status=ToolStatus.OK,
            content={
                "path": str(resolution.resolved),
                "content": "\n".join(selected),
                "start_line": start,
                "end_line": start + len(selected) - 1,
                "total_lines": total_lines,
                "truncated": truncated_bytes or (start + len(selected) - 1) < total_lines,
            },
        )

    def _tool_search_files(
        self, pattern: str, path: str | None = None
    ) -> ToolResult:
        """Find files by name. Read-only, so containment is the only gate."""
        base = self._resolve_in_workspace(path or ".")
        if not base.allowed or base.resolved is None:
            return ToolResult(
                tool="search_files",
                status=ToolStatus.DENIED,
                detail=f"path is outside the workspace: {base.reason}",
            )

        try:
            matches = sorted(
                str(p)
                for p in base.resolved.rglob(pattern)
                if p.is_file()
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                tool="search_files",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        return ToolResult(
            tool="search_files",
            status=ToolStatus.OK,
            content={
                "matches": matches[:MAX_SEARCH_RESULTS],
                "count": len(matches),
                "truncated": len(matches) > MAX_SEARCH_RESULTS,
            },
        )

    def _tool_search_content(
        self, regex: str, path: str | None = None, glob: str | None = None
    ) -> ToolResult:
        """Search file contents. Read-only."""
        import re

        base = self._resolve_in_workspace(path or ".")
        if not base.allowed or base.resolved is None:
            return ToolResult(
                tool="search_content",
                status=ToolStatus.DENIED,
                detail=f"path is outside the workspace: {base.reason}",
            )

        try:
            pattern = re.compile(regex)
        except re.error as exc:
            return ToolResult(
                tool="search_content",
                status=ToolStatus.INVALID_ARGUMENTS,
                detail=f"invalid regular expression: {exc}",
            )

        matches: list[dict[str, Any]] = []
        truncated = False

        try:
            for candidate in sorted(base.resolved.rglob(glob or "*")):
                if not candidate.is_file():
                    continue
                try:
                    text = candidate.read_text(
                        encoding="utf-8", errors="replace"
                    )[:MAX_READ_BYTES]
                except (OSError, UnicodeError):
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if pattern.search(line):
                        if len(matches) >= MAX_SEARCH_RESULTS:
                            truncated = True
                            break
                        matches.append(
                            {
                                "path": str(candidate),
                                "line": number,
                                "text": line[:500],
                            }
                        )
                if truncated:
                    break
        except (OSError, ValueError) as exc:
            return ToolResult(
                tool="search_content",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        return ToolResult(
            tool="search_content",
            status=ToolStatus.OK,
            content={"matches": matches, "count": len(matches), "truncated": truncated},
        )

    def _tool_write_file(self, path: str, content: str) -> ToolResult:
        """Create or replace a file, entirely.

        The secret check runs on the *target*, before the write, so an agent
        cannot create a file whose own name makes it a secret source.
        """
        payload = content.encode("utf-8")
        if len(payload) > MAX_WRITE_BYTES:
            return ToolResult(
                tool="write_file",
                status=ToolStatus.ERROR,
                detail=(
                    f"content is {len(payload)} bytes, over the "
                    f"{MAX_WRITE_BYTES}-byte limit"
                ),
            )

        resolution = self._resolve_in_workspace(path)
        if not resolution.allowed or resolution.resolved is None:
            return ToolResult(
                tool="write_file",
                status=ToolStatus.DENIED,
                detail=f"path is outside the workspace: {resolution.reason}",
            )

        verdict = self._secrets.classify(resolution.resolved)
        outcome = self.approver.submit_secret(
            verdict,
            agent=self.agent_name,
            tool="write_file",
        )
        if outcome.decision is not ApprovalDecision.APPROVED:
            return self._result_from_outcome(
                "write_file", outcome.decision, outcome.detail
            )

        try:
            resolution.resolved.parent.mkdir(parents=True, exist_ok=True)
            resolution.resolved.write_text(content, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return ToolResult(
                tool="write_file",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        return ToolResult(
            tool="write_file",
            status=ToolStatus.OK,
            content={"path": str(resolution.resolved), "bytes": len(payload)},
        )

    def _tool_patch_file(self, path: str, old: str, new: str) -> ToolResult:
        """Replace one exact region of an existing file.

        Classification happens **before** the file is read. Reading the current
        content and classifying afterwards would leave the approval prompt unable
        to show what was about to change -- which is the whole reason this path
        is gated at all.
        """
        refusal = self._approve_path_read("patch_file", Path(path))
        if refusal is not None:
            return refusal

        resolution = self._resolve_in_workspace(str(path))
        assert resolution.resolved is not None  # checked above

        if not resolution.resolved.exists():
            return ToolResult(
                tool="patch_file",
                status=ToolStatus.ERROR,
                detail=f"no such file: {path}",
            )

        try:
            text = resolution.resolved.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError) as exc:
            return ToolResult(
                tool="patch_file",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        occurrences = text.count(old)
        if occurrences == 0:
            return ToolResult(
                tool="patch_file",
                status=ToolStatus.ERROR,
                detail="the region to replace was not found; it must match exactly",
            )
        if occurrences > 1:
            return ToolResult(
                tool="patch_file",
                status=ToolStatus.ERROR,
                detail=(
                    f"the region to replace appears {occurrences} times; it must "
                    "be unique, so include more surrounding context"
                ),
            )

        try:
            resolution.resolved.write_text(
                text.replace(old, new, 1), encoding="utf-8"
            )
        except (OSError, UnicodeError) as exc:
            return ToolResult(
                tool="patch_file",
                status=ToolStatus.ERROR,
                detail=f"{exc.__class__.__name__}: {str(exc)[:300]}",
            )

        return ToolResult(
            tool="patch_file",
            status=ToolStatus.OK,
            content={"path": str(resolution.resolved), "replaced": 1},
        )

    def _tool_exec_command(
        self,
        program: str,
        args: Sequence[str] | None = None,
        cwd: str | None = None,
        reason: str | None = None,
    ) -> ToolResult:
        """Run a command through the full pipeline.

        The order is the settled one and none of it is a decision this module
        makes::

            CommandClassifier
              -> SecretClassifier on resolved targets
              -> Approver.submit_command
              -> Executor.execute

        A non-approved outcome never reaches ``Executor``. The executor's own
        fingerprint binding is therefore a second line rather than the only one.

        ``ExecutionRequest.program`` carries the **exact model-supplied token**,
        not ``verdict.normalized_program``. The approval fingerprint binds the
        exact program token, so substituting the normalised basename would break
        the binding and surface as a confusing ``INVALID_APPROVAL`` instead of a
        clear answer.
        """
        argv = tuple(str(a) for a in (args or ()))

        working_dir = None
        if cwd:
            resolution = self._resolve_in_workspace(cwd)
            if not resolution.allowed or resolution.resolved is None:
                return ToolResult(
                    tool="exec_command",
                    status=ToolStatus.DENIED,
                    detail=f"cwd is outside the workspace: {resolution.reason}",
                )
            working_dir = resolution.resolved

        command = Command(program=program, args=argv, cwd=working_dir)
        verdict: CommandVerdict = self._classifier.classify(command)

        secret_verdicts: tuple[SecretVerdict, ...] = tuple(
            self._secrets.classify(target) for target in verdict.targets_resolved
        )

        outcome = self.approver.submit_command(
            verdict,
            agent=self.agent_name,
            secret_verdicts=secret_verdicts,
            tool="exec_command",
            intent=reason,
            # The workspace root, not the cwd. The verdict carries only the
            # resolved cwd, and binding that as the workspace would make the
            # approval fingerprint disagree with the execution one for any
            # command in a subdirectory -- a correctly-approved operation
            # refused as INVALID_APPROVAL.
            workspace=self.workspace.root,
        )

        if outcome.decision is not ApprovalDecision.APPROVED:
            return self._result_from_outcome(
                "exec_command", outcome.decision, outcome.detail
            )

        result = self.executor.execute(
            outcome,
            ExecutionRequest(
                program=program,
                args=argv,
                cwd=working_dir,
                agent=self.agent_name,
            ),
        )

        return self._execution_result(result)

    @staticmethod
    def _execution_result(result: Any) -> ToolResult:
        """Map an ``ExecutionResult`` onto a model-visible result.

        Bounded stdout and stderr are returned as-is. That is a deliberate,
        documented limitation rather than an oversight: process output is
        untrusted, may contain sensitive data, and Forge v1 offers no general
        redaction guarantee. Bounding is what v1 provides.
        """
        status_map = {
            ExecutionState.COMPLETED: ToolStatus.OK,
            ExecutionState.FAILED: ToolStatus.OK,
            ExecutionState.TIMED_OUT: ToolStatus.TIMEOUT,
            ExecutionState.LAUNCH_FAILED: ToolStatus.LAUNCH_FAILED,
            ExecutionState.INVALID_APPROVAL: ToolStatus.DENIED,
            ExecutionState.INVALID_REQUEST: ToolStatus.INVALID_ARGUMENTS,
        }

        return ToolResult(
            tool="exec_command",
            status=status_map.get(result.state, ToolStatus.ERROR),
            content={
                "state": result.state.value,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "stdout_truncated": result.stdout_truncated,
                "stderr_truncated": result.stderr_truncated,
            },
            detail=result.error or "",
        )


def _validate(schema: dict[str, Any], arguments: dict[str, Any]) -> str:
    """The minimum JSON-Schema subset the tools actually use.

    Hand-checked rather than pulled from ``jsonschema``: this project has zero
    runtime dependencies by deliberate decision, and the subset below -- type,
    required, minimum -- is all six tool schemas need. Returns a message naming
    the offending field, because "invalid arguments" tells a model nothing it
    can act on.
    """
    properties: dict[str, Any] = schema.get("properties", {})

    for name in schema.get("required", []):
        if name not in arguments:
            return f"missing required argument: {name}"

    for name, value in arguments.items():
        spec = properties.get(name)
        if not spec:
            # Unknown keys are ignored rather than rejected: providers add their
            # own bookkeeping fields, and refusing those would break otherwise
            # valid calls.
            continue

        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"{name} must be a string"
        if expected == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            return f"{name} must be an integer"
        if expected == "array" and not isinstance(value, list):
            return f"{name} must be an array"
        if "minimum" in spec and isinstance(value, int) and value < spec["minimum"]:
            return f"{name} must be at least {spec['minimum']}"

    return ""
