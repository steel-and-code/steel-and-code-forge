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
from ..events import EventSink, emit
from ..external.base import ExternalAgent, ExternalInvocation, ExternalResult
from ..external.hermes import HERMES
from ..external.runner import spawn
from ..models.base import ModelGateway
from ..tools.approval import Approver
from ..tools.dispatcher import ToolDispatcher
from ..tools.execution import Executor
from ..tools.workspace import Workspace
from .broker import SseBroker
from .channel import WebApprovalChannel

__all__ = ["build_runtime", "build_dispatcher", "build_hermes_runner"]


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


def build_hermes_runner(
    workspace: Workspace,
    *,
    adapter: ExternalAgent = HERMES,
) -> Callable[[SseBroker, str], ExternalResult]:
    """Build a runner that executes one text-only Hermes task.

    This is the Studio Workbench's *second* run path, and it is deliberately
    narrower than the native one. The native path composes
    ``ModelGateway -> ToolDispatcher -> AgentRuntime`` with Forge's classifier
    and approval layer; this one invokes Hermes as an external subprocess whose
    capability boundary is fixed in the adapter -- a mandatory dedicated
    text-only profile (``-t``/``--toolsets`` is never used), a minimal
    environment, and no ``--yolo``, asserted in ``tests/test_external_hermes.py``.
    Forge can see the task text and the result, but never the child's tool
    calls, which is exactly why this path grants no tools and must not grow any
    without a separate, reviewed change to the adapter's ``capabilities``.

    The returned callable matches what :class:`~forge.ui.session.WorkbenchSession`
    needs: it takes the broker and the task and returns the parsed
    :class:`ExternalResult`. There is intentionally no profile argument -- the
    profile is a fixed safety decision owned by the adapter, never a value a
    browser request can choose. Events are emitted through the broker so the
    existing SSE activity feed carries the run.

    Args:
        workspace: The run's fixed root, passed as the child's ``--in``
            directory.
        adapter: The external agent to invoke. Defaults to the shared Hermes
            adapter; a test passes a stub that records its argv instead of
            spawning a process.
    """

    def run(broker: SseBroker, task: str) -> ExternalResult:
        invocation = ExternalInvocation(
            agent=adapter.name,
            task=task,
            workspace=str(workspace.root),
        )
        emit(broker, "hermes.started", task=task)
        outcome = spawn(adapter, invocation)
        result = outcome.result
        if outcome.launch_error:
            emit(broker, "hermes.failed", detail=outcome.launch_error)
        elif outcome.timed_out or not result.ok:
            emit(
                broker,
                "hermes.failed",
                detail=result.detail or "the run reported failure",
            )
        else:
            emit(
                broker,
                "hermes.result",
                text=result.text,
                model=result.model,
                session_id=result.session_id,
            )
        return result

    return run