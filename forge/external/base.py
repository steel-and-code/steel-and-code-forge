"""The external-agent contract: what an adapter must provide, and what it returns.

Kept separate from :mod:`forge.agents` on purpose. That package is native agents
-- values Forge hosts and governs. This one is subprocesses Forge starts and
bounds. They are siblings, not parent and child, and the interface below is
deliberately *not* a subclass of :class:`~forge.agents.profile.AgentProfile`:
the two enforce different things, and a shared type would invite exactly the
decorative-narrowing bug the package docstring is about.

The contract has three parts and no more:

*   :class:`ExternalInvocation` -- what Forge is about to run.
*   :class:`ExternalResult` -- what came back, with the child's own success
    verdict preserved.
*   :class:`ExternalAgent` -- the two-question interface an adapter answers.

**Nothing here is a policy.** No classifier, no ``Approver``, no ``Executor``.
Whether an invocation may happen at all is decided by
:func:`forge.external.gating.gate_task`, which runs *before* an adapter is
called, and by the operator at the CLI edge. An adapter's job is to describe a
subprocess faithfully, not to decide whether it should run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ExternalAgent",
    "ExternalInvocation",
    "ExternalResult",
    "ExternalUnavailable",
    "SpawnRefused",
]


class ExternalUnavailable(Exception):
    """The external agent's program is missing or cannot be launched.

    Separate from a *failed run* because the honest responses differ. A run that
    failed did start and did work; this one never began, and the operator's next
    step is installation rather than debugging. Carries an operator-readable
    message and is caught at the CLI edge, where it becomes a usage error rather
    than a traceback.
    """


class SpawnRefused(Exception):
    """Forge declined to start the external agent at all.

    Raised by the CLI *before* any adapter is invoked, from the task-text gate.
    It is not an adapter failure and never reaches an adapter. Distinct from
    :class:`ExternalUnavailable` because nothing is missing: Forge understood
    the request and declined it.
    """


@dataclass(frozen=True)
class ExternalInvocation:
    """One external agent run Forge is about to perform.

    Attributes:
        agent: The adapter's name, for the operator's benefit.
        task: The task text, exactly as the operator wrote it. Passed to the
            child as a single argument -- never interpolated into a shell
            string, so quotes, ``$(...)``, and backticks in a task are data and
            never syntax.
        workspace: The explicit workspace root. An external agent gets this as
            an explicit instruction because a subprocess inherits whatever
            directory Forge happened to be launched from, which is not a safety
            decision.
        timeout: Wall-clock ceiling for the whole run. Bounded because an
            external agent is a program Forge does not control, and an unbounded
            subprocess is an unbounded run.
        max_turns: An advisory limit the adapter may pass through if the child
            supports one.
    """

    agent: str
    task: str
    workspace: str
    timeout: float = 900.0
    max_turns: int | None = None


@dataclass(frozen=True)
class ExternalResult:
    """What an external agent produced, and whether it thinks it succeeded.

    Attributes:
        agent: Which adapter ran.
        text: The agent's final response. Its own words -- Forge does not
            synthesise a summary, for the same reason
            :class:`~forge.agents.runtime.RunResult` does not.
        ok: The child's *own* verdict. **Not** the subprocess exit status: the
            verified behaviour of the one adapter in this MVP is that its process
            exits 0 even when the run failed, so the authoritative signal is
            parsed out of the structured output. Trusting the exit code would
            report success for a failed run.
        exit_code: The code the child reported, which may be nonzero even when
            the process exited 0.
        model: The model the child says it used, or ``""``. Reported, never
            selected -- see :mod:`forge.external.hermes`.
        provider: Likewise for the provider.
        session_id: The child's session handle, so an operator can resume or
            inspect the run outside Forge.
        events: The parsed structured events, kept for inspection and for
            surfaces that want to render progress rather than wait.
        detail: Why the run failed, in operator-readable terms. Empty on
            success.
    """

    agent: str
    text: str = ""
    ok: bool = False
    exit_code: int = 0
    model: str = ""
    provider: str = ""
    session_id: str = ""
    events: tuple[dict[str, object], ...] = field(default_factory=tuple)
    detail: str = ""


@runtime_checkable
class ExternalAgent(Protocol):
    """An agent Forge invokes as a subprocess.

    Two questions, because those are the only two an operator has before
    running one: *is it there*, and *what would you do*.

    Deliberately **not** a ``CommandClassifier`` client and **not** a
    ``ToolDispatcher`` builder. An adapter cannot classify the child's tool
    calls, and pretending otherwise would put a second opinion about permissions
    in a layer that has no way to enforce one.
    """

    @property
    def name(self) -> str:
        """The name this agent is invoked by."""
        ...

    @property
    def capabilities(self) -> tuple[str, ...]:
        """What this adapter is permitted to grant the child.

        Almost always empty in the MVP. Recorded rather than assumed so the
        value is *reported* to the operator rather than inferred from the
        adapter's implementation, and so a future adapter that does grant a
        capability has to say so in one visible place.
        """
        ...

    def available(self) -> bool:
        """Whether the program can be found. Cheap; opens nothing."""
        ...

    def describe(self) -> str:
        """One operator-readable line: what this agent is and what it can do."""
        ...

    def argv(self, invocation: ExternalInvocation) -> tuple[str, ...]:
        """The exact argv Forge will execute.

        A tuple, never a string. A command line assembled as a string and handed
        to a shell would make every task a shell-injection surface, and Forge's
        own execution layer already forbids that for the same reason.

        Raises:
            SpawnRefused: if this invocation must not run. Adapters are not
                expected to refuse -- gating happens before they are called --
                but the hook exists so a future adapter with a hard requirement
                cannot be wired in without somewhere to say no.
        """
        ...

    def environment(self, invocation: ExternalInvocation) -> dict[str, str]:
        """The complete environment for the child.

        A full mapping, not an overlay. Inheriting the ambient environment would
        hand the child Forge's credentials, the operator's ``PATH``, and every
        other session variable, which is the opposite of a capability boundary.
        """
        ...

    def parse(self, stdout: str, stderr: str, returncode: int) -> ExternalResult:
        """Turn the child's output into a result.

        Must not raise for a failed run: a child that reports failure in its
        output has still produced a parseable answer, and raising would discard
        it. ``ExternalUnavailable`` is the one exception, for output that
        indicates the program was never really there.
        """
        ...
