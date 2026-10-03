"""Composition for one Workbench run.

This module assembles the pipeline and decides nothing, exactly as
``_run_byte`` does in ``forge/cli/main.py``. It is kept separate from the server
so that the composition can be tested, and reasoned about, without an HTTP
server anywhere in sight.

The shape of the assembly is deliberately identical to the CLI's:

    ModelGateway -> ToolDispatcher -> AgentRuntime

with an :class:`~forge.tools.approval.Approver` and an
:class:`~forge.tools.execution.Executor` wired into the dispatcher. That is the
sanctioned composition: this module *constructs* an ``Executor`` in order to hand
it to a dispatcher, and it never calls ``execute`` itself. A second path from
this package to a subprocess would bypass approval entirely, which is why
``tests/test_ui.py`` asserts structurally that no module under ``forge/ui`` calls
``execute``.

The gateway is injected rather than constructed here. That is what lets every UI
test use :class:`~forge.models.scripted.ScriptedGateway` -- no network, no key,
no cost -- while the CLI passes a real
:class:`~forge.models.openrouter.OpenRouterGateway` built from the environment
in the process, never in the browser.
"""

from __future__ import annotations

from typing import Callable

from ..agents.byte import assemble_system_prompt
from ..agents.profile import AgentProfile
from ..agents.runtime import AgentRuntime
from ..events import EventSink
from ..models.base import ModelGateway
from ..tools.approval import Approver
from ..tools.dispatcher import ToolDispatcher
from ..tools.execution import Executor
from ..tools.workspace import Workspace
from .broker import SseBroker
from .channel import WebApprovalChannel

__all__ = ["build_runtime", "build_dispatcher"]


def build_dispatcher(
    workspace: Workspace,
    *,
    profile: AgentProfile,
    channel: WebApprovalChannel,
    events: EventSink | None = None,
) -> ToolDispatcher:
    """Wire the safety stack for one Workbench run.

    Identical in shape to the CLI's assembly: a dispatcher owning the
    workspace, the approver backed by ``channel``, an executor it will only
    reach through an approved outcome, and Byte's system prompt.

    Args:
        workspace: The run's fixed root.
        profile: Whose run this is. Supplies the allowed-tool set, which is the
            structural narrowing mechanism -- a request cannot widen it,
            because this surface offers no way to configure it.
        channel: The browser approval bridge.
        events: Observational sink, normally the broker.
    """
    return ToolDispatcher(
        workspace,
        approver=Approver(channel),
        executor=Executor(workspace),
        # The request genuinely is Byte's here, so the approval prompt says so.
        agent_name=profile.name,
        allowed_tools=profile.allowed_tools,
        system_prompt=assemble_system_prompt(profile, str(workspace.root)),
        events=events,
    )


def build_runtime(
    workspace: Workspace,
    *,
    profile: AgentProfile,
    gateway: ModelGateway,
    model_id: str,
    channel: WebApprovalChannel,
    broker: SseBroker,
    max_turns: int | None = None,
) -> AgentRuntime:
    """Build one run's runtime, wired to the broker and the approval channel."""
    dispatcher = build_dispatcher(
        workspace, profile=profile, channel=channel, events=broker
    )
    return AgentRuntime(
        profile,
        gateway=gateway,
        dispatcher=dispatcher,
        model_id=model_id,
        max_turns=max_turns,
        events=broker,
    )


def runtime_factory(
    workspace: Workspace,
    *,
    profile: AgentProfile,
    gateway: ModelGateway,
    model_id: str,
    max_turns: int | None = None,
) -> Callable[[SseBroker, WebApprovalChannel], AgentRuntime]:
    """A factory matching :class:`~forge.ui.session.WorkbenchSession`.

    The session builds a fresh runtime per run because the dispatcher caches
    permission refusals for the length of a run, and a refusal cached across two
    different tasks would be a stale answer about an operation nobody asked about
    this time. The gateway is shared deliberately: it holds the credential, and
    the credential belongs to the process rather than to a run.
    """

    def factory(broker: SseBroker, channel: WebApprovalChannel) -> AgentRuntime:
        return build_runtime(
            workspace,
            profile=profile,
            gateway=gateway,
            model_id=model_id,
            channel=channel,
            broker=broker,
            max_turns=max_turns,
        )

    return factory