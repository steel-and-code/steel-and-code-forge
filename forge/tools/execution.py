"""Command execution.

The one place Forge is allowed to launch a process.

Everything upstream of here decides *whether* an operation may run; this module
runs the ones that were allowed to. It holds no policy of its own: it does not
classify, does not approve, does not reinterpret a verdict, and cannot widen a
permission. Its entire job is to prove that the operation being launched is the
operation a human approved, and to run exactly that.

Four properties hold, and the tests assert each rather than trusting it:

*   **An approved :class:`~forge.tools.approval.ApprovalOutcome` is required.**
    There is no path to ``subprocess`` that does not begin with a validated,
    claimed approval. A caller with a denied, rejected, unattended, spent, or
    mismatched approval gets a result and no process.

*   **The approval is structurally bound to the invocation.** The digest covers
    the exact program token, the exact argv token boundaries, the resolved
    workspace, the resolved cwd, and the invocation form. An approval for
    ``rm -rf build`` does not authorise ``rm -rf src``, and an approval for one
    workspace does not authorise another. The caller does not get to assert this
    holds; it is recomputed on both sides and compared.

*   **Output is bounded while the child runs.** A drain thread per stream reads
    to EOF continuously and retains only the first N bytes, so a child may emit
    without limit while Forge's memory does not grow and neither stream can
    block on a full pipe.

*   **Nothing secret is returned.** The environment is Forge-owned, output is
    untrusted by policy, and argv is absent from the result and the audit event
    because arguments carry secrets as readily as files do
    (``curl -H "Authorization: Bearer ..."``).

**This is not a sandbox.** The workspace is revalidated immediately before
launch and any detectable drift fails closed, but a race necessarily remains
between that final check and whatever an arbitrary child opens afterwards.
Closing it would need an OS primitive -- namespaces, containerisation, or
capability restriction -- which is a separate design and not part of this layer.
Neither is the resolved executable path bound: resolution happens at launch and
a file can be replaced between resolution and ``execve``. What the digest proves
is that the invocation matches the approval; the resolved path recorded in
metadata is evidence of what ran, not a guarantee about it.

Linux-first, like the rest of the security layer: ``start_new_session`` and
``killpg`` are POSIX, and Windows job objects are out of scope.

Scope: argv only, no shell, no stdin, no caller-supplied environment, no
credentials. Credential injection with opaque handles is its own later design.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .approval import ApprovalDecision, ApprovalOutcome, ApprovalRequest
from .workspace import Workspace

__all__ = [
    "ExecutionState",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionEvent",
    "Executor",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "TERMINATE_GRACE_SECONDS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "HARD_MAX_OUTPUT_BYTES",
    "EXECUTABLE_SEARCH_PATH",
    "MAX_ARG_COUNT",
    "MAX_ARG_LENGTH",
]

# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

#: Applied when neither the executor nor the request says otherwise.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Ceiling on the *configured* default. Trusted configuration may choose any
#: default up to this; it may not exceed it.
MAX_TIMEOUT_SECONDS = 900.0

#: Seconds a signalled process group has to exit after SIGTERM before SIGKILL.
TERMINATE_GRACE_SECONDS = 5.0

#: Bytes retained per stream. Total bytes seen are counted separately.
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
HARD_MAX_OUTPUT_BYTES = 16 * 1024 * 1024

#: The child's PATH. Fixed and minimal on purpose -- see ``_child_environment``.
EXECUTABLE_SEARCH_PATH = "/usr/local/bin:/usr/bin:/bin"

#: How long to wait for the drain threads after the child is gone. They see EOF
#: once the pipes close, so this only bounds a pathological reader.
DRAIN_JOIN_SECONDS = 5.0

#: argv guards. An execution request is a structured invocation, and a caller
#: passing a megabyte of arguments is a bug rather than a use case.
MAX_ARG_COUNT = 4096
MAX_ARG_LENGTH = 128 * 1024

_READ_CHUNK = 64 * 1024


class ExecutionState(str, Enum):
    """Forge-level outcome, deliberately separate from the child's exit code.

    ``COMPLETED`` and ``FAILED`` both mean the process ran. The rest mean it did
    not, which is why they carry ``exit_code=None``: there is no child exit code
    to report when no child produced one.

    ``INVALID_APPROVAL`` is about the capability -- not approved, already spent,
    or not matching this invocation. ``INVALID_REQUEST`` is about the input --
    a bad timeout, a bad cwd, an unsupported option. Splitting them means a
    caller can tell "you may not do this" from "that request was malformed".
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LAUNCH_FAILED = "launch_failed"
    INVALID_APPROVAL = "invalid_approval"
    INVALID_REQUEST = "invalid_request"


@dataclass(frozen=True)
class ExecutionRequest:
    """One invocation to launch.

    There is deliberately no ``env``, ``env_keys``, or ``stdin`` field. Not
    defaulted-to-empty, not present-and-ignored: absent, so there is no argument
    through which a caller could pass ``PYTHONPATH``, ``LD_PRELOAD``, or a
    secret. The child environment is constructed entirely by Forge.

    ``repr`` omits argv. Arguments carry secrets as often as files do, and a
    repr reaches tracebacks and logs.
    """

    program: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    timeout: float | None = None
    agent: str = ""

    def __repr__(self) -> str:
        return (
            f"ExecutionRequest(program={self.program!r}, "
            f"arg_count={len(self.args)}, cwd={self.cwd!r}, "
            f"timeout={self.timeout!r}, agent={self.agent!r})"
        )

    def fingerprint(self, workspace: Path, cwd: Path) -> str:
        """Digest of this request's launch identity.

        Delegates to the approval layer's own digest function rather than
        rebuilding one here. The executor has no approval yet at this point, so
        it derives the digest from the invocation alone; the approval side
        derives its digest from what the operator saw, and the two must match.

        Note this deliberately does not construct an ApprovalRequest: the
        executor has no business synthesising an approval-layer object, and
        inventing a classification it did not produce would put a second
        opinion about the same operation into the code that authorises it.
        """
        return ApprovalRequest.fingerprint_for(
            self.program, tuple(self.args), workspace, cwd, False
        )


@dataclass(frozen=True)
class ExecutionResult:
    """What happened when Forge ran (or refused to run) one operation.

    Carries bounded stdout and stderr, because a caller that executed something
    needs to see it. What it deliberately does not carry: argv (arguments hold
    secrets too), the environment (Forge-owned and fixed), or the originating
    approval object.

    Process output is **untrusted input**. It is bounded but not redacted --
    nothing here attempts to remove secrets a child printed, and no such claim
    is made. See the module docstring.
    """

    state: ExecutionState
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int
    workspace: Path
    cwd: Path
    fingerprint: str
    agent: str
    program: str
    arg_count: int
    approval_rule: str
    duration: float
    started_at: float
    finished_at: float
    terminated: bool = False
    resolved_program: str | None = None
    error: str | None = None

    @property
    def ran(self) -> bool:
        """Whether a process was actually launched and produced an exit code."""
        return self.state in (ExecutionState.COMPLETED, ExecutionState.FAILED)

    def event(self) -> ExecutionEvent:
        """The audit projection: metadata only, never output or argv."""
        return ExecutionEvent(
            agent=self.agent,
            program=self.program,
            arg_count=self.arg_count,
            workspace=self.workspace,
            cwd=self.cwd,
            fingerprint=self.fingerprint,
            approval_rule=self.approval_rule,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration=self.duration,
            state=self.state,
            child_exit_code=self.exit_code,
            stdout_bytes=self.stdout_bytes,
            stderr_bytes=self.stderr_bytes,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            timed_out=self.state is ExecutionState.TIMED_OUT,
            terminated=self.terminated,
            resolved_program=self.resolved_program,
        )


@dataclass(frozen=True)
class ExecutionEvent:
    """Minimal metadata for a future audit trail.

    Nothing that could carry a secret: no stdout, no stderr, no argv, no
    environment. Byte *counts* rather than content, so a reviewer can tell a
    quiet run from a noisy one without the record holding what was said.

    ``fingerprint`` is the join key that links an execution to the exact
    approval that authorised it, without recording either.
    """

    agent: str
    program: str
    arg_count: int
    workspace: Path
    cwd: Path
    fingerprint: str
    approval_rule: str
    started_at: float
    finished_at: float
    duration: float
    state: ExecutionState
    child_exit_code: int | None
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    terminated: bool
    resolved_program: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "program": self.program,
            "arg_count": self.arg_count,
            "workspace": str(self.workspace),
            "cwd": str(self.cwd),
            "fingerprint": self.fingerprint,
            "approval_rule": self.approval_rule,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "state": self.state.value,
            "child_exit_code": self.child_exit_code,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "terminated": self.terminated,
            "resolved_program": self.resolved_program,
        }


# --------------------------------------------------------------------------
# Bounded output capture
# --------------------------------------------------------------------------


class _StreamDrain(threading.Thread):
    """Read one pipe to EOF, retaining at most ``cap`` bytes.

    The important subtlety is that reading does **not** stop at the cap. A
    drainer that returned once it had its N bytes would let the child fill the
    pipe buffer and block forever -- turning a truncation into a hang, which is
    a worse failure than the unbounded output it was meant to prevent. So this
    keeps reading and discarding for the child's whole life.

    Only the retained head is kept in memory, so a child emitting unbounded
    bytes costs Forge a bounded ``cap`` and nothing more. ``total`` counts what
    was seen without storing it.
    """

    def __init__(self, stream: object, cap: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self.retained = bytearray()
        self.total = 0
        self.truncated = False
        self.error: str | None = None

    def run(self) -> None:
        read = getattr(self._stream, "read", None)
        if read is None:  # pragma: no cover - defensive
            return
        try:
            while True:
                chunk = read(_READ_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                if len(self.retained) < self._cap:
                    room = self._cap - len(self.retained)
                    self.retained.extend(chunk[:room])
                    if len(chunk) > room:
                        self.truncated = True
                else:
                    self.truncated = True
        except (OSError, ValueError) as exc:
            self.error = f"output capture failed: {exc.__class__.__name__}"
        finally:
            try:
                self._stream.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - best effort
                pass

    def text(self) -> str:
        """The retained bytes, decoded once, never raising."""
        return bytes(self.retained).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------


class Executor:
    """Launches approved operations. The only place Forge spawns a process.

    Args:
        workspace: The single explicit workspace root. Revalidated at every
            execution rather than trusted from construction, because a root can
            be deleted, replaced, or re-pointed between runs.
        default_timeout: Seconds. May be anywhere up to ``max_timeout``.
        max_timeout: Hard ceiling for ``default_timeout``.
        max_output_bytes: Bytes retained per stream, up to
            ``HARD_MAX_OUTPUT_BYTES``.

    Raises:
        ValueError: A limit is out of range. Raised at construction rather than
            at launch, so a misconfiguration is caught when the executor is
            built instead of when an operator is waiting at a prompt.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_timeout: float = MAX_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if not _is_valid_timeout(default_timeout):
            raise ValueError(f"default_timeout is not a valid duration: {default_timeout!r}")
        if default_timeout > max_timeout:
            raise ValueError(
                f"default_timeout ({default_timeout}) exceeds max_timeout ({max_timeout})"
            )
        if not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise ValueError(f"max_output_bytes must be a positive int: {max_output_bytes!r}")
        if max_output_bytes > HARD_MAX_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes ({max_output_bytes}) exceeds the hard maximum "
                f"({HARD_MAX_OUTPUT_BYTES})"
            )

        self.workspace = workspace
        self.default_timeout = float(default_timeout)
        self.max_timeout = float(max_timeout)
        self.max_output_bytes = max_output_bytes

    # -- public API ------------------------------------------------------

    def execute(
        self, outcome: ApprovalOutcome, request: ExecutionRequest
    ) -> ExecutionResult:
        """Run ``request`` if and only if ``outcome`` approves exactly it.

        Validation, then a single atomic claim, then launch. The claim comes
        after every check, so a request that fails validation does *not* spend
        the approval -- the operator's yes is still there for the operation they
        actually approved. Once the claim succeeds the approval is spent, even
        if the launch then fails: one approval authorises one launch attempt,
        and a retry after a changed environment must go back through approval.
        """
        started_at = time.time()

        problem = self._validate_request(request)
        if problem is not None:
            return self._refuse(ExecutionState.INVALID_REQUEST, problem, request, started_at)

        # Capability checks that need no resolved path, before any filesystem
        # work: a rejected or already-spent approval should not cause a re-stat.
        early = self._check_approval_shape(outcome, request)
        if early is not None:
            return self._refuse(
                ExecutionState.INVALID_APPROVAL, early, request, started_at
            )

        requested_cwd = request.cwd if request.cwd is not None else self.workspace.root
        workspace_state = self._revalidate_workspace(requested_cwd)
        if isinstance(workspace_state, str):
            return self._refuse(
                ExecutionState.INVALID_APPROVAL,
                f"approved workspace state cannot be established: {workspace_state}",
                request,
                started_at,
            )
        cwd = workspace_state

        target_problem = self._revalidate_targets(outcome.request.targets)
        if target_problem is not None:
            return self._refuse(
                ExecutionState.INVALID_APPROVAL, target_problem, request, started_at
            )

        # Now that cwd is resolved, the fingerprint can be compared.
        binding_problem = self._check_approval(outcome, request, cwd)
        if binding_problem is not None:
            return self._refuse(
                ExecutionState.INVALID_APPROVAL, binding_problem, request, started_at
            )

        # ---- everything validated; spend the approval -------------------
        try:
            outcome.claim()
        except Exception as exc:
            return self._refuse(
                ExecutionState.INVALID_APPROVAL,
                f"approval could not be claimed: {exc.__class__.__name__}",
                request,
                started_at,
            )

        return self._launch(request, cwd, started_at, outcome.request.rule)

    # -- validation ------------------------------------------------------

    def _validate_request(self, request: ExecutionRequest) -> str | None:
        """Structural problems with the request itself. No approval consulted."""
        if not isinstance(request.program, str) or not request.program.strip():
            return "program is empty"
        if "\x00" in request.program:
            return "program contains a NUL byte"
        for arg in request.args:
            if not isinstance(arg, str):
                return "argv contains a non-string element"
            if "\x00" in arg:
                return "argv contains a NUL byte"
        if len(request.args) > MAX_ARG_COUNT:
            return f"argv has {len(request.args)} elements, over the {MAX_ARG_COUNT} limit"
        if any(len(a) > MAX_ARG_LENGTH for a in request.args):
            return f"an argv element exceeds the {MAX_ARG_LENGTH}-byte limit"

        if request.timeout is not None:
            if not _is_valid_timeout(request.timeout):
                return f"timeout is not a valid duration: {request.timeout!r}"
            # May only narrow. Silently clamping would hand the caller a shorter
            # run than it asked for while reporting success.
            if request.timeout > self.default_timeout:
                return (
                    f"timeout {request.timeout} exceeds the configured default "
                    f"{self.default_timeout}; a request may only lower it"
                )
        return None

    def _check_approval_shape(
        self, outcome: ApprovalOutcome, request: ExecutionRequest
    ) -> str | None:
        """Approval checks that need no resolved path.

        Decision, request shape, spent-or-not, requester identity. The
        fingerprint comparison lives in :meth:`_check_approval` because it
        needs the resolved cwd.
        """
        if not isinstance(outcome, ApprovalOutcome):
            return "not an ApprovalOutcome"
        if not outcome.decision.permitted:
            return (
                f"approval decision is {outcome.decision.value}, "
                f"not a permitted approval"
            )
        if outcome.request is None:
            return "approval carries no request"
        if outcome.request.is_shell_form:
            return (
                "approval was for a shell string; this executor runs argv only "
                "and cannot bind an unparsed invocation"
            )
        if outcome.claimed:
            return "approval has already been claimed"
        if outcome.request.program is None:
            return "approval carries no program identity"
        if request.agent and outcome.request.agent and request.agent != outcome.request.agent:
            return (
                f"requester mismatch: approval is for "
                f"{outcome.request.agent!r}, request is {request.agent!r}"
            )

        return None

    def _check_approval(
        self, outcome: ApprovalOutcome, request: ExecutionRequest, cwd: Path
    ) -> str | None:
        """Whether the approval binds to exactly this invocation.

        Both sides derive a digest from the fields that decide what gets
        launched, and they must agree. This is what makes the binding structural
        rather than a promise: a caller cannot assert its way past it, because
        Forge recomputes it from the request it was handed.
        """
        approved = outcome.request.execution_fingerprint()
        asked = request.fingerprint(self.workspace.root, cwd)
        if approved != asked:
            return (
                "approved operation does not match the requested operation "
                f"(approved fingerprint {approved[:12]}..., requested "
                f"{asked[:12]}...)"
            )
        return None

    def _revalidate_targets(self, targets: tuple[Path, ...]) -> str | None:
        """Re-prove every approved target immediately before launch.

        Containment at approval time and containment at launch are two different
        moments, and the gap between them is attacker-controlled. A target the
        classifier could name is re-resolved here through the same containment
        check, so swapping `data.txt` for a symlink out of the workspace between
        approval and launch is caught rather than followed.

        Targets that are *not* knowable -- a path named inside inline source, for
        instance -- cannot be re-proved, because nothing about them was ever
        enumerated. That limit is why this layer is documented as not a sandbox.
        """
        for target in targets:
            resolution = self.workspace.resolve(target)
            if not resolution.allowed:
                return (
                    f"approved target no longer resolves inside the workspace: "
                    f"{target} -> {resolution.reason}"
                )
        return None

    def _revalidate_workspace(self, requested: Path) -> "Path | str":
        """Re-prove the approved workspace, resolving cwd.

        Containment at approval time and containment at launch are two different
        moments, and the gap between them is attacker-controlled. Every check
        here runs again, immediately before launch.

        Returns:
            The resolved cwd, or a string explaining the failure. A string is
            used rather than an exception because every failure mode here is an
            expected refusal, not an error.
        """
        root = self.workspace.root
        if not root.is_dir():
            return "workspace root no longer exists or is not a directory"

        # Identity: a root re-pointed at a different directory is not the
        # directory that was approved.
        try:
            current_real = Path(os.path.realpath(str(root)))
        except (OSError, ValueError) as exc:
            return f"workspace root could not be resolved: {exc}"
        if current_real != root:
            return "workspace root resolves to a different directory than approved"

        resolution = self.workspace.resolve(requested)
        if not resolution.allowed:
            return f"cwd is outside the workspace: {resolution.reason}"
        return resolution.resolved or root

    # -- launch ----------------------------------------------------------

    def _launch(
        self,
        request: ExecutionRequest,
        cwd: Path,
        started_at: float,
        rule: str,
    ) -> ExecutionResult:
        """Spawn the process, drain both streams, and reap the group."""
        fingerprint = request.fingerprint(self.workspace.root, cwd)

        child_env = self._child_environment(cwd)
        timeout = (
            self.default_timeout if request.timeout is None else float(request.timeout)
        )

        resolved = shutil.which(request.program, path=EXECUTABLE_SEARCH_PATH)
        if resolved is None:
            # The approval is already spent. That is intended: one approval
            # authorises one attempt, and if the environment changes the caller
            # goes back through approval rather than reusing a stale yes.
            return self._result(
                state=ExecutionState.LAUNCH_FAILED,
                exit_code=None,
                request=request,
                workspace=self.workspace.root,
                cwd=cwd,
                fingerprint=fingerprint,
                rule=rule,
                started_at=started_at,
                stdout="",
                stderr="",
                error=f"program not found on the executor search path: {request.program!r}",
            )

        try:
            process = subprocess.Popen(  # noqa: S603 - argv only, shell=False
                [resolved, *request.args],
                cwd=str(cwd),
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            return self._result(
                state=ExecutionState.LAUNCH_FAILED,
                exit_code=None,
                request=request,
                workspace=self.workspace.root,
                cwd=cwd,
                fingerprint=fingerprint,
                rule=rule,
                started_at=started_at,
                stdout="",
                stderr="",
                error=f"launch failed: {exc.__class__.__name__}",
                resolved_program=resolved,
            )

        out_drain = _StreamDrain(process.stdout, self.max_output_bytes)
        err_drain = _StreamDrain(process.stderr, self.max_output_bytes)
        out_drain.start()
        err_drain.start()

        timed_out = False
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_group(process)
        finally:
            # Both pipes close when the child and its descendants are gone, so
            # the drainers see EOF. Joining is bounded in case a reader wedges.
            out_drain.join(timeout=DRAIN_JOIN_SECONDS)
            err_drain.join(timeout=DRAIN_JOIN_SECONDS)

        exit_code = process.returncode
        state = (
            ExecutionState.TIMED_OUT
            if timed_out
            else (ExecutionState.COMPLETED if exit_code == 0 else ExecutionState.FAILED)
        )

        return self._result(
            state=state,
            exit_code=None if timed_out else exit_code,
            request=request,
            workspace=self.workspace.root,
            cwd=cwd,
            fingerprint=fingerprint,
            rule=rule,
            started_at=started_at,
            stdout=out_drain.text(),
            stderr=err_drain.text(),
            stdout_truncated=out_drain.truncated,
            stderr_truncated=err_drain.truncated,
            stdout_bytes=out_drain.total,
            stderr_bytes=err_drain.total,
            terminated=timed_out,
            resolved_program=resolved,
        )

    def _terminate_group(self, process: "subprocess.Popen[bytes]") -> None:
        """Stop the whole process tree, escalating to SIGKILL.

        Signalling only the direct child would leave its descendants running:
        ``bash -c 'sleep 300 & wait'`` would keep holding the workspace and a
        process slot long after the timeout. The child was started in its own
        session, so its process group is exactly its tree.

        SIGKILL is sent unconditionally after the grace window rather than
        polling: a process that ignores SIGTERM would otherwise survive, and a
        "timed out" command that keeps running is worse than one killed hard.
        """
        pgid = process.pid  # start_new_session makes the child its own leader

        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            process.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is final
            pass

    # -- helpers ---------------------------------------------------------

    def _child_environment(self, cwd: Path) -> dict[str, str]:
        """The child's entire environment, constructed by Forge.

        Not inherited. The parent environment of a Forge host holds whatever the
        operator's session had -- SSH_AUTH_SOCK, cloud tokens, database URLs,
        CI credentials -- and passing all of it through would hand every executed
        command every secret the shell held. The v1 permission model requires an
        explicit allowlist and says the ambient environment is not passed
        through wholesale; inheriting would contradict that by accident.

        ``HOME`` points at the workspace so tools do not scatter state into the
        operator's real home directory. A test runner writing ``~/.cache`` is an
        entirely normal thing to do, and it should not reach outside.
        """
        return {
            "PATH": EXECUTABLE_SEARCH_PATH,
            "HOME": str(self.workspace.root),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PWD": str(cwd),
        }

    def _refuse(
        self,
        state: ExecutionState,
        detail: str,
        request: ExecutionRequest,
        started_at: float,
    ) -> ExecutionResult:
        """A result for a request that never ran.

        Every refusal is a result rather than an exception. A caller that forgot
        to check something should not be handed a traceback; it should get
        something it cannot mistake for a completed run.
        """
        return self._result(
            state=state,
            exit_code=None,
            request=request,
            workspace=self.workspace.root,
            cwd=self.workspace.root,
            fingerprint="",
            rule="",
            started_at=started_at,
            stdout="",
            stderr="",
            error=detail,
        )

    def _result(
        self,
        *,
        state: ExecutionState,
        exit_code: int | None,
        request: ExecutionRequest,
        workspace: Path,
        cwd: Path,
        fingerprint: str,
        rule: str,
        started_at: float,
        stdout: str,
        stderr: str,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        stdout_bytes: int | None = None,
        stderr_bytes: int | None = None,
        terminated: bool = False,
        resolved_program: str | None = None,
        error: str | None = None,
    ) -> ExecutionResult:
        finished_at = time.time()
        return ExecutionResult(
            state=state,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_bytes=stdout_bytes if stdout_bytes is not None else len(stdout.encode("utf-8")),
            stderr_bytes=stderr_bytes if stderr_bytes is not None else len(stderr.encode("utf-8")),
            workspace=workspace,
            cwd=cwd,
            fingerprint=fingerprint,
            agent=request.agent,
            program=request.program,
            arg_count=len(request.args),
            approval_rule=rule,
            duration=finished_at - started_at,
            started_at=started_at,
            finished_at=finished_at,
            terminated=terminated,
            resolved_program=resolved_program,
            error=error,
        )


def _is_valid_timeout(value: object) -> bool:
    """A usable duration: finite, real, and positive."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    numeric = float(value)
    return math.isfinite(numeric) and numeric > 0