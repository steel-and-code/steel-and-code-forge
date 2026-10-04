"""External agents: subprocesses Forge invokes but does not host.

**A native agent is not an external agent, and this package is where that
difference lives.**

Byte and Neighbor are :class:`~forge.agents.profile.AgentProfile` values. Forge
holds them in process, routes their tool calls through its own
:class:`~forge.tools.dispatcher.ToolDispatcher`, and enforces every permission
decision itself. That is the whole safety story for a native agent, and it works
because Forge sees every call the agent makes.

An external agent is a program Forge starts. Forge does **not** see its tool
calls, cannot classify them, and cannot stop them. Everything in this package
follows from that one fact:

*   **Authority is bounded by what the subprocess is given, not by what it asks
    for.** Forge cannot approve a request it cannot observe, so the only lever
    that works is narrowing the child's capabilities up front. This is the same
    "narrowing is structural, widening is impossible" principle as
    :class:`AgentProfile`, applied one layer out -- but it is a *different*
    mechanism, because it happens in a command line rather than in a
    membership test.
*   **An external agent is never an ``AgentProfile``.** A profile's
    ``allowed_tools`` is enforced by Forge's dispatcher. An external agent's
    tools run inside another process behind another dispatcher, so a profile
    would be a *decorative* narrowing: it would constrain nothing while
    appearing to. That is strictly worse than having no profile at all, because
    a reader would reasonably believe the limit was real.
*   **The request is gated before the agent exists.** Forge classifies the task
    text -- the only thing it can see -- and refuses to spawn at all when that
    classification is not ``allow``. Fail closed on any error.

What this package deliberately does **not** do:

*   Route the external agent's model. Hermes owns its own model, provider,
    reasoning configuration, and credentials; see ``forge/external/hermes.py``.
*   Offer a per-agent toolset flag. Capability is fixed per adapter in the MVP
    and granting it is a code change, reviewed like any other policy change --
    not something an operator can widen from the command line.
*   Promise containment. Forge can constrain a child's *toolset*; it cannot
    confine a child that has been granted filesystem or shell authority to a
    directory. No adapter in this MVP grants either.
"""

from __future__ import annotations

from .base import (
    ExternalAgent,
    ExternalInvocation,
    ExternalResult,
    ExternalUnavailable,
    SpawnRefused,
)
from .gating import GateDecision, TaskGate, gate_task
from .hermes import HERMES, HermesAdapter
from .runner import ExternalRun, spawn

__all__ = [
    "HERMES",
    "ExternalAgent",
    "ExternalInvocation",
    "ExternalResult",
    "ExternalRun",
    "ExternalUnavailable",
    "GateDecision",
    "HermesAdapter",
    "SpawnRefused",
    "TaskGate",
    "gate_task",
    "spawn",
]