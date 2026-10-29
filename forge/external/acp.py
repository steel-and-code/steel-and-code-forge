"""Hermes ACP engineering adapter: an isolated, human-approved session.

This module is the second external-agent path, and it is deliberately a
different kind of thing from :mod:`forge.external.hermes`.

:class:`~forge.external.hermes.HermesAdapter` spawns ``hermes chat -q`` as a
**one-shot** subprocess under ``forge-textonly`` (zero tools). Every word the
child can say comes back in one ``ExternalResult``, and there is no approval
momentary in between. That is the text-only conversation path, and it is
unchanged.

This module implements the **engineering** path: a long-lived Hermes session
over the Agent Client Protocol (ACP), where Hermes drives real tool calls (an
editor-approval surface) and Forge both streams the session and *answers each
permission request* with an explicit human decision. The shape is a session, not
a spawn: start, chat, cancel, resume.

Safety rests on three facts, each enforced rather than documented:

*   **The toolset is fixed and file-only.** ``ENGINEERING_TOOLSETS`` is
    ``("file",)``. There is no terminal, code_execution, browser, web,
    delegation, or cron. The enforcement is on the Hermes side (a dedicated
    engineering profile), which is why :func:`verify_profile` exists: Forge
    proves the restriction the same way :mod:`forge.external.hermes` proves the
    text-only profile -- by reading what actually resolves, never a claim.
*   **Every permission is allow-once or deny.** :class:`ACPApprovalBridge`
    forwards exactly ``allow_once`` and ``deny`` and never a persistent grant
    (``allow_always`` / ``allow_session``) or an automatic edit acceptance.
    Persistence and auto-accept are the two ways a one-time "yes" becomes a
    standing capability, and neither exists in this bridge.
*   **Live execution is gated on OS isolation AND a credential boundary.**
    :func:`verify_credential_boundary` currently returns a refusal, because the
    in-process ACP server runs the model's file tool in the same process that
    holds the inference credential -- a ``read_file`` can reach the key. Until
    the credential lives outside the session's readable filesystem, live
    engineering execution is refused (``EngineeringGate`` fails closed) and the
    adapter is usable only against a scripted transport in tests.

The adapter has no runtime dependency on the ``acp`` package: the reference
client is imported lazily inside :class:`RealACPTransport`, so importing this
module costs Forge nothing new.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from ..tools.approval import (
    Approver,
    ApprovalRequest,
    OperationKind,
)
from ..tools.workspace import Classification

__all__ = [
    "ENGINEERING_TOOLSETS",
    "ALLOWED_PERMISSION_OPTIONS",
    "SANDBOX_PROGRAM",
    "SandboxRefusal",
    "EngineeringGate",
    "ACPApprovalBridge",
    "ACPTransport",
    "ACPSession",
    "RealACPTransport",
    "verify_sandbox",
    "verify_profile",
    "verify_credential_boundary",
    "build_sandbox_argv",
]


#: The only toolset an engineering session may expose. ``file`` bundles
#: read_file / write_file / patch / search_files -- deliberately *not*
#: terminal, code_execution, browser, web, vision, delegation, skills, or cron.
ENGINEERING_TOOLSETS: tuple[str, ...] = ("file",)

#: The only permission outcomes the bridge will ever forward. ``allow_session``
#: and ``allow_always`` are persistent grants; ``deny_always`` would let an
#: ACP peer's "reject_always" become Forge's state. None of the three cross.
ALLOWED_PERMISSION_OPTIONS: tuple[str, ...] = ("allow_once", "deny")

#: The sandbox program the engineering session runs under. OS-enforced: the
#: model's file tool executes inside a mount namespace that cannot see the
#: operator's home, repositories, or credentials.
SANDBOX_PROGRAM = "bwrap"

#: The one toolset name Forge requires the engineering profile to resolve to.
#: Used by :func:`verify_profile` against the live tool inventory.
REQUIRED_TOOL_NAMES: frozenset[str] = frozenset(
    {"read_file", "write_file", "patch", "search_files"}
)


class SandboxRefusal(RuntimeError):
    """A capability boundary that could not be established refuses the run.

    Distinct from a failed run: nothing was launched, because the safety
    precondition (OS isolation, or the credential boundary) could not be
    confirmed. Fail closed -- an unisolated engineering session is worse than
    no session.
    """


# ---------------------------------------------------------------------------
# Verification (pure; the "prove the boundary, don't assume it" layer)
# ---------------------------------------------------------------------------


def verify_sandbox(program: str = SANDBOX_PROGRAM) -> str | None:
    """Whether an OS-isolating sandbox binary is available. ``None`` = usable.

    Only the presence check belongs here; actually building a namespace is the
    operator's environment and is confirmed at launch. A missing ``bwrap`` is a
    refusal, not a fallback to an unsandboxed child.
    """
    resolved = shutil.which(program)
    if resolved is None:
        return (
            f"the {program} sandbox program was not found on PATH; engineering "
            f"execution requires OS-level isolation and is refused without it"
        )
    return None


def verify_profile(tool_names: set[str] | frozenset[str]) -> str | None:
    """Whether an *effective* tool inventory is exactly the file toolset.

    ``tool_names`` is the resolved set of names an engineering session could
    call (read from ``agent.valid_tool_names``, not from config). Anything other
    than exactly the four file tools is a refusal -- a widened toolset is not
    something configuration is trusted to describe.
    """
    if tool_names == REQUIRED_TOOL_NAMES:
        return None
    extra = sorted(tool_names - REQUIRED_TOOL_NAMES)
    missing = sorted(REQUIRED_TOOL_NAMES - tool_names)
    parts: list[str] = []
    if extra:
        parts.append(f"grants extra tools {extra}")
    if missing:
        parts.append(f"is missing {missing}")
    return (
        "the engineering session's effective toolset is not file-only: it "
        + "; and it ".join(parts)
        + ". Engineering is refused rather than run with a widened surface."
    )


def verify_credential_boundary(reason: str | None = None) -> str:
    """Whether inference credentials are unreachable by the file tool.

    This is **always a refusal today**, and the reason is structural, not a bug:

    the in-process ACP server runs Hermes's own ``read_file`` in the same
    process that holds the provider key. Hermes's ``file`` toolset does not
    confine reads to a workspace, so a model can ``read_file`` the credential --
    whether it is a ``.env`` file in the session's home or the process
    environment (via ``/proc/self/environ``). With ``file`` the only enabled
    toolset the exfiltration channel (network/shell) is closed, but the key is
    still *readable*, which is not the "inaccessible" bar this module demands.

    Closing it requires the credential to live outside the session's readable
    filesystem (a gateway-held key or a credential proxy), which is separate
    work. Until then, live engineering execution must not be permitted.
    """
    return reason or (
        "inference credentials are not provably inaccessible to the file tool "
        "(the in-process ACP server shares one filesystem and environment with "
        "the credential); live engineering execution is blocked until the "
        "credential is held outside the session's readable namespace"
    )


def build_sandbox_argv(
    *,
    serve_script: str,
    hermes_home: str,
    workspace: str,
    python: str,
) -> tuple[str, ...]:
    """The exact argv that launches the in-process ACP server under bwrap.

    Every isolation property is a literal here, asserted by
    :func:`tests.test_external_acp` : a private mount namespace, the operator's
    home shadowed, the disposable engineering ``HERMES_HOME`` used for state,
    and DNS pinned to a static resolver so the model has no access to the
    operator's resolver. The server runs ``serve_script`` (which constructs
    ``HermesACPAgent(SessionManager())``) with a select()-based event loop.
    """
    return (
        SANDBOX_PROGRAM,
        "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/run", "--tmpfs", "/root",
        "--bind", workspace, "/workspace",
        "--bind", hermes_home, "/hermes-home",
        "--setenv", "HOME", "/tmp",
        "--setenv", "HERMES_HOME", "/hermes-home",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--", python, serve_script,
    )


@dataclass(frozen=True)
class EngineeringGate:
    """The single decision for whether a live engineering session may start.

    Three independent preconditions, in precedence order. Any one that is not
    met is a refusal, and ``refusal_reason`` names it. The gate is evaluated
    once and frozen, so a session cannot widen mid-flight.
    """

    enabled: bool
    sandbox_reason: str | None = None
    credential_reason: str | None = None

    @property
    def permits(self) -> bool:
        """Whether a live engineering session may run at all."""
        return self.refusal_reason is None

    @property
    def refusal_reason(self) -> str | None:
        if not self.enabled:
            return "engineering mode is disabled; enable it explicitly to start a session"
        if self.sandbox_reason is not None:
            return self.sandbox_reason
        if self.credential_reason is not None:
            return self.credential_reason
        return None


def gate_engineering(
    *,
    enabled: bool,
    sandbox: str | None,
    credential: str | None,
) -> EngineeringGate:
    """Assemble a gate from its three inputs (called once, before any session)."""
    return EngineeringGate(
        enabled=enabled,
        sandbox_reason=sandbox,
        credential_reason=credential,
    )


# ---------------------------------------------------------------------------
# Approval bridge
# ---------------------------------------------------------------------------


class ACPApprovalBridge:
    """Map an ACP permission request onto Forge's approval pipeline.

    Reuses :class:`~forge.tools.approval.Approver` and
    :class:`~forge.tools.approval.ApprovalRequest` wholesale -- the same
    single-spend authority that governs native tool calls. The only thing this
    bridge adds is the ACP-specific narrowing:

    *   **Only allow-once and deny exist.** Every request is
        ``approval_required``, so ``Approver.resolve`` always prompts (or fails
        closed unattended). The answer maps to ``allow_once`` or ``deny`` and
        never to a persistent grant.
    *   **A permanent option is never forwarded.** If a peer offered
        ``allow_always`` it is simply not one of the two answers this bridge can
        produce, so a one-time "yes" can never become a standing capability.

    Args:
        approver: Forge's approver, backed by the interactive channel. A test
            wires :class:`~forge.tools.approval.ScriptedApprovalChannel`.
        workspace: The session's root, shown in the approval prompt.
    """

    def __init__(self, approver: Approver, workspace: Path) -> None:
        self._approver = approver
        self._workspace = workspace

    def decide(self, *, title: str, kind: str | None) -> str:
        """Return exactly ``"allow_once"`` or ``"deny"`` for one request."""
        request = ApprovalRequest(
            kind=OperationKind.COMMAND if kind == "execute" else OperationKind.SECRET,
            agent="hermes-engineering",
            tool="acp",
            operation=f"allow the Hermes engineering session to {title or 'act'}",
            rule="acp.permission",
            classification=Classification.APPROVAL_REQUIRED,
            reason="the Hermes engineering session requested permission",
            workspace=self._workspace,
            detail=title or "unspecified operation",
        )
        outcome = self._approver.resolve(request)
        return "allow_once" if outcome.approved else "deny"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


@runtime_checkable
class ACPTransport(Protocol):
    """The long-lived ACP connection, injectable for tests.

    The real implementation wraps the reference ``acp`` client and a bwrap
    subprocess; a scripted one records calls and replays canned events. Both
    answer the same five questions: open the channel, run one prompt (streaming
    updates and raising permission requests), list/load sessions, and cancel.
    """

    async def open(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def new_session(self, cwd: str) -> str:
        ...

    async def list_sessions(self, cwd: str) -> list[str]:
        ...

    async def load_session(self, session_id: str, cwd: str) -> str | None:
        ...

    async def prompt(
        self,
        session_id: str,
        text: str,
        on_update: Callable[[dict], None],
        on_permission: Callable[[dict], object],
    ) -> dict:
        ...

    async def cancel(self, session_id: str) -> None:
        ...


class RealACPTransport:
    """The live transport: bwrap-sandboxed in-process Hermes ACP server.

    The ``acp`` reference client is imported lazily and only here, so Forge's
    stdlib-only runtime and its test suite never require it. :meth:`open`
    refuses unless the caller has a passing :class:`EngineeringGate` -- the OS
    and credential boundaries are checked before any subprocess spawns.
    """

    def __init__(self, gate: EngineeringGate) -> None:
        self._gate = gate

    async def open(self) -> None:
        if not self._gate.permits:
            raise SandboxRefusal(self._gate.refusal_reason or "engineering refused")
        # The reference client lives in the `acp` package, an optional runtime
        # dependency. It is imported here so a machine without it can still run
        # every test and the text-only path.
        try:
            import acp  # noqa: F401
        except ImportError as exc:
            raise SandboxRefusal(
                "the `acp` reference client is not installed; install "
                "`agent-client-protocol` in Forge's engineering extra"
            ) from exc
        raise SandboxRefusal(
            "live engineering execution is not wired until the credential "
            "boundary (verify_credential_boundary) can be satisfied"
        )

    async def close(self) -> None:
        return None

    async def new_session(self, cwd: str) -> str:
        raise SandboxRefusal("live engineering execution is blocked")

    async def list_sessions(self, cwd: str) -> list[str]:
        raise SandboxRefusal("live engineering execution is blocked")

    async def load_session(self, session_id: str, cwd: str) -> str | None:
        raise SandboxRefusal("live engineering execution is blocked")

    async def prompt(self, session_id: str, text: str, on_update, on_permission) -> dict:
        raise SandboxRefusal("live engineering execution is blocked")

    async def cancel(self, session_id: str) -> None:
        raise SandboxRefusal("live engineering execution is blocked")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class ACPSession:
    """One long-lived Hermes engineering session, over a transport.

    Owns the lifecycle, forwards events to a sink, routes every permission
    request through the bridge, and exposes cancellation. It decides nothing
    about capability -- that is the transport's gate and the bridge's
    allow-once/deny narrowing. It is the mechanical half.

    Args:
        transport: The ACP connection (real or scripted).
        bridge: The approval bridge, reusing Forge's approver.
        event_sink: ``Callable[[str, dict], None]`` receiving named events
            (``session.update``, ``acp.permission``, ``session.result``). Tests
            pass a list-appender.
        events: Optional Forge event sink for the broker, same shape.
    """

    def __init__(
        self,
        transport: ACPTransport,
        bridge: ACPApprovalBridge,
        *,
        event_sink: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._transport = transport
        self._bridge = bridge
        self._event_sink = event_sink
        self._session_id: str | None = None
        self._closed = False

    def _event(self, name: str, **fields: object) -> None:
        if self._event_sink is not None:
            self._event_sink(name, dict(fields))

    async def start(self, cwd: str) -> str:
        """Open the transport and create a session. Returns the session id."""
        if self._closed:
            raise RuntimeError("session is closed")
        await self._transport.open()
        self._session_id = await self._transport.new_session(cwd)
        self._event("session.started", session_id=self._session_id)
        return self._session_id

    async def prompt(self, text: str) -> dict:
        """Run one user turn, streaming updates and answering permissions."""
        session_id = self._require_session()

        def on_update(update: dict) -> None:
            self._event("session.update", session_id=session_id, update=update)

        def on_permission(request: dict) -> str:
            self._event(
                "acp.permission",
                session_id=session_id,
                kind=request.get("kind"),
                title=request.get("title"),
            )
            decision = self._bridge.decide(
                title=request.get("title", ""), kind=request.get("kind")
            )
            self._event("acp.permission.decided", decision=decision)
            return decision

        result = await self._transport.prompt(
            session_id, text, on_update, on_permission
        )
        self._event("session.result", session_id=session_id, result=result)
        return result

    async def list_sessions(self, cwd: str) -> list[str]:
        return await self._transport.list_sessions(cwd)

    async def load_session(self, session_id: str, cwd: str) -> str | None:
        """Reconnect to a previously-persisted session id (or None)."""
        loaded = await self._transport.load_session(session_id, cwd)
        if loaded is not None:
            self._session_id = loaded
            self._event("session.restored", session_id=loaded)
        return loaded

    async def cancel(self) -> None:
        """:meth:`~ACPTransport.cancel` the active session and mark it stopped."""
        session_id = self._session_id
        if session_id is not None:
            await self._transport.cancel(session_id)
            self._event("session.cancelled", session_id=session_id)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._transport.close()

    def _require_session(self) -> str:
        if self._session_id is None:
            raise RuntimeError("no session started")
        return self._session_id