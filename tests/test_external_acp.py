"""ACP engineering adapter: policy, approval, lifecycle, and its boundaries.

These tests drive the adapter against a scripted transport and Forge's own
:class:`~forge.tools.approval.ScriptedApprovalChannel` -- no sandbox, no model,
no network, no ``acp`` dependency. They prove the requirements that do not need
a live Hermes: allow-once/deny narrowing, profile restriction, session
recovery, cancellation, sandbox failure, and the credential boundary that
blocks live execution.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from forge.external.acp import (
    ACPApprovalBridge,
    ACPSession,
    ALLOWED_PERMISSION_OPTIONS,
    ENGINEERING_TOOLSETS,
    EngineeringGate,
    RealACPTransport,
    SandboxRefusal,
    build_sandbox_argv,
    gate_engineering,
    verify_credential_boundary,
    verify_profile,
    verify_sandbox,
)
from forge.tools.approval import Approver, ScriptedApprovalChannel


# ---------------------------------------------------------------------------
# Scripted transport
# ---------------------------------------------------------------------------


class ScriptedTransport:
    """A transport that records calls and replays canned events.

    ``updates`` are streamed to ``on_update`` in order; ``permission`` (when set)
    is raised to ``on_permission`` once and its returned decision is recorded.
    """

    def __init__(self, *, updates=None, permission_request=None, result=None):
        self.new_sessions: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.loads: list[tuple[str, str]] = []
        self.lists: list[str] = []
        self.cancelled: list[str] = []
        self.opened = False
        self.closed = False
        self.permission_decision: str | None = None
        self.updates = updates if updates is not None else []
        self.permission_request = permission_request
        self.result = result if result is not None else {"ok": True}

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def new_session(self, cwd: str) -> str:
        sid = f"sid-{len(self.new_sessions) + 1}"
        self.new_sessions.append(sid)
        return sid

    async def list_sessions(self, cwd: str) -> list[str]:
        self.lists.append(cwd)
        return list(self.new_sessions)

    async def load_session(self, session_id: str, cwd: str) -> str | None:
        self.loads.append((session_id, cwd))
        return session_id if session_id in self.new_sessions else None

    async def prompt(self, session_id: str, text: str, on_update, on_permission) -> dict:
        self.prompts.append((session_id, text))
        for update in self.updates:
            on_update(update)
        if self.permission_request is not None:
            self.permission_decision = on_permission(self.permission_request)
        return dict(self.result)

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


def _bridge(answers, workspace: Path | None = None) -> ACPApprovalBridge:
    approver = Approver(ScriptedApprovalChannel(answers))
    return ACPApprovalBridge(approver, workspace or Path("/workspace"))


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Policy: default-disabled, file-only, allow-once/deny
# ---------------------------------------------------------------------------


def test_engineering_is_disabled_by_default() -> None:
    gate = gate_engineering(enabled=False, sandbox=None, credential=None)
    assert not gate.permits
    assert gate.refusal_reason is not None
    assert "disabled" in gate.refusal_reason


def test_engineering_enabled_without_boundaries_refuses_on_credential() -> None:
    gate = gate_engineering(
        enabled=True,
        sandbox=None,
        credential="key is reachable by the file tool",
    )
    assert not gate.permits
    assert gate.refusal_reason == "key is reachable by the file tool"


def test_toolset_allowlist_is_file_only() -> None:
    assert ENGINEERING_TOOLSETS == ("file",)
    assert ALLOWED_PERMISSION_OPTIONS == ("allow_once", "deny")


def test_verify_profile_accepts_exactly_the_file_toolset() -> None:
    assert verify_profile({"read_file", "write_file", "patch", "search_files"}) is None


def test_verify_profile_refuses_a_widened_toolset() -> None:
    reason = verify_profile({"read_file", "write_file", "patch", "search_files", "terminal"})
    assert reason is not None
    assert "terminal" in reason


def test_verify_profile_refuses_a_missing_file_tool() -> None:
    reason = verify_profile({"read_file", "patch", "search_files"})
    assert reason is not None
    assert "write_file" in reason


def test_verify_sandbox_refuses_when_program_is_missing() -> None:
    reason = verify_sandbox(program="forge-no-such-sandbox-xyz")
    assert reason is not None
    assert "not found" in reason


def test_verify_credential_boundary_is_a_refusal() -> None:
    assert verify_credential_boundary() != ""


def test_build_sandbox_argv_is_isolating() -> None:
    argv = build_sandbox_argv(
        serve_script="/srv/serve.py",
        hermes_home="/disposable",
        workspace="/ws",
        python="/usr/bin/python3",
    )
    joined = " ".join(argv)
    assert "--unshare-all" in joined
    assert "--clearenv" in joined
    assert "--die-with-parent" in joined
    assert "--tmpfs" in joined
    assert "--ro-bind" in joined
    # The disposable home is what gets bound and becomes the session's state root.
    assert "/disposable" in joined
    assert "HERMES_HOME" in joined
    # No auto-approval / no permanent grant / no terminal toolset anywhere.
    assert "--yolo" not in joined
    assert "allow_always" not in joined
    assert "terminal" not in joined


# ---------------------------------------------------------------------------
# Approval: allow-once and deny only
# ---------------------------------------------------------------------------


def _decide(answers, kind="execute", title="run `make`") -> str:
    bridge = _bridge(answers)
    return bridge.decide(title=title, kind=kind)


def test_permission_allow_once_approves() -> None:
    assert _decide(["y"]) == "allow_once"


def test_permission_deny_rejects() -> None:
    assert _decide(["n"]) == "deny"


def test_permission_unattended_denies() -> None:
    approver = Approver(None)  # no channel -> DENIED_UNATTENDED
    bridge = ACPApprovalBridge(approver, Path("/workspace"))
    assert bridge.decide(title="run `make`", kind="execute") == "deny"


def test_bridge_never_returns_a_persistent_grant() -> None:
    # Whatever the input, the bridge's output set is exactly allow-once / deny.
    for _ in range(5):
        assert _decide(["y"]) in ALLOWED_PERMISSION_OPTIONS
        assert _decide(["n"]) in ALLOWED_PERMISSION_OPTIONS


# ---------------------------------------------------------------------------
# Session: lifecycle, streaming, recovery, cancellation
# ---------------------------------------------------------------------------


def test_session_lifecycle_streams_and_answers_permission() -> None:
    async def flow() -> None:
        transport = ScriptedTransport(
            updates=[{"text": "one"}, {"text": "two"}],
            permission_request={"kind": "edit", "title": "Approve edit: /ws/x.txt"},
        )
        bridge = _bridge(["y"])
        events: list[tuple[str, dict]] = []
        session = ACPSession(
            transport, bridge, event_sink=lambda n, f: events.append((n, f))
        )
        sid = await session.start("/ws")
        result = await session.prompt("write the file")
        await session.close()

        assert transport.opened and transport.closed
        assert sid == "sid-1"
        assert transport.permission_decision == "allow_once"
        updates = [f.get("update") for n, f in events if n == "session.update"]
        assert {"text": "one"} in updates
        assert {"text": "two"} in updates
        names = [n for n, _ in events]
        assert "session.started" in names
        assert "acp.permission" in names
        assert "acp.permission.decided" in names
        assert "session.result" in names
        assert result["ok"] is True

    _run(flow())


def test_denied_permission_reaches_the_transport_as_deny() -> None:
    async def flow() -> None:
        transport = ScriptedTransport(
            permission_request={"kind": "execute", "title": "rm -rf /tmp/x"},
        )
        session = ACPSession(transport, _bridge(["n"]))
        await session.start("/ws")
        await session.prompt("delete it")
        assert transport.permission_decision == "deny"

    _run(flow())


def test_session_recovery_lists_and_loads() -> None:
    async def flow() -> None:
        transport = ScriptedTransport()
        bridge = _bridge(["y"])
        session = ACPSession(transport, bridge)
        sid = await session.start("/ws")
        recovered = await session.load_session(sid, "/ws")
        listed = await session.list_sessions("/ws")

        assert recovered == sid
        assert sid in listed
        assert transport.loads == [(sid, "/ws")]
        assert transport.lists == ["/ws"]

    _run(flow())


def test_cancel_forwards_to_the_transport() -> None:
    async def flow() -> None:
        transport = ScriptedTransport()
        session = ACPSession(transport, _bridge(["y"]))
        sid = await session.start("/ws")
        await session.cancel()
        assert transport.cancelled == [sid]

    _run(flow())


def test_prompt_before_start_refuses() -> None:
    async def flow() -> None:
        session = ACPSession(ScriptedTransport(), _bridge(["y"]))
        with pytest.raises(RuntimeError):
            await session.prompt("hello")

    _run(flow())


# ---------------------------------------------------------------------------
# Live execution is gated on the boundaries
# ---------------------------------------------------------------------------


def test_real_transport_refuses_when_gate_blocks() -> None:
    gate = EngineeringGate(
        enabled=True,
        sandbox_reason=None,
        credential_reason=verify_credential_boundary(),
    )
    transport = RealACPTransport(gate)

    async def flow() -> None:
        with pytest.raises(SandboxRefusal):
            await transport.open()

    _run(flow())


def test_real_transport_refuses_on_missing_sandbox() -> None:
    gate = EngineeringGate(enabled=True, sandbox_reason="no bwrap", credential_reason=None)
    transport = RealACPTransport(gate)

    async def flow() -> None:
        with pytest.raises(SandboxRefusal) as exc:
            await transport.open()
        assert "bwrap" in str(exc.value)

    _run(flow())