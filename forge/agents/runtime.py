"""The agent runtime: the tool-call loop.

What lives here is the loop and nothing else. The runtime holds a profile, a
gateway, a dispatcher, and a conversation, and it has **no classifier, no
``Approver`` and no ``Executor``**. Every permission decision belongs to
``forge/tools/dispatcher.py`` and the layers beneath it.

That separation is the whole design, and it is worth stating why it is enforced
by omission rather than by discipline. A runtime that could reach a classifier
would be one edit away from holding a second opinion about what is allowed -- and
two opinions is how the "approval narrows a permission, it never widens one"
rule decays. ``tests/test_agent_runtime.py`` asserts the absence structurally, by
scanning imports, so the property cannot rot quietly.

The loop:

    send context to the model
    the model returns text, or tool calls
    if tool calls:
        dispatch each, sequentially, appending one result per call
        continue
    if final text:
        return to the operator

Three properties that are decisions rather than defaults:

*   **Turns are model round-trips, not tool calls.** A response carrying five
    parallel tool calls costs one turn, because the round-trip is what costs
    tokens. Capping calls instead would penalise a model for batching.

*   **A cached denial still costs a turn.** The cache stops repeated classifier
    work and repeated approval prompts; it does not exempt the model from the
    bound. An agent looping on a denied call therefore still terminates, which
    is the property that actually matters -- the cache is a politeness
    optimisation, not the thing preventing a runaway.

*   **Exhausting the limit raises.** The model does not get to narrate a
    conclusion it did not produce, and the operator is not shown a truncated run
    dressed up as a finished one.

No internal error is ever converted into a success. A gateway failure raises; a
refusal becomes a tool result the model can read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models.base import CompletionRequest, Message, ModelGateway
from ..tools.dispatcher import ToolDispatcher, ToolResult, ToolStatus
from .profile import AgentProfile
from .state import ConversationState

__all__ = [
    "AgentRuntime",
    "RunResult",
    "MaxTurnsExceeded",
    "MAX_TOOL_TURNS",
]


#: Ceiling on model round-trips for one run. A real task -- "fix the failing
#: tests" -- plausibly needs ten to twenty tool calls, so this leaves headroom
#: without permitting a runaway. Configurable *downward* per run; a profile may
#: not raise it.
MAX_TOOL_TURNS = 25


class MaxTurnsExceeded(RuntimeError):
    """The model used its whole turn budget without finishing.

    Raised rather than returned, because a truncated run is not a result. The
    message carries the limit and the call count so the operator can see that the
    agent was still working rather than stuck.
    """

    def __init__(self, turns: int, tool_calls: int) -> None:
        super().__init__(
            f"model used all {turns} permitted turns "
            f"({tool_calls} tool calls) without finishing"
        )
        self.turns = turns
        self.tool_calls = tool_calls


@dataclass
class RunResult:
    """What one run produced.

    Attributes:
        text: The model's final message. Its own words -- the runtime never
            synthesises a summary, because an operator reading a summary cannot
            tell it apart from the model's actual answer.
        turns: Model round-trips consumed.
        tool_calls: Individual tool calls dispatched. Greater than ``turns`` when
            a turn carried parallel calls.
        state: The conversation, returned for inspection. Session-only: it is
            never written anywhere and is dropped when the process exits.
        denied: Permission refusals that occurred, as ``(tool, detail)`` pairs.
            Surfaced separately because an operator needs to know what was
            refused without reading the transcript.
    """

    text: str
    turns: int
    tool_calls: int
    state: ConversationState
    denied: tuple[tuple[str, str], ...] = ()


class AgentRuntime:
    """Runs one agent against one model, through Forge's tools.

    Args:
        profile: Whose run this is, and what it may ask for.
        gateway: The model.
        dispatcher: The only route to Forge's policy.
        model_id: Provider-specific model identifier, resolved from configuration.
        max_turns: Round-trip ceiling. Clamped to the profile's value and to
            ``MAX_TOOL_TURNS``; it can lower the bound but never raise it.
        max_output_tokens: Per-request output bound.
    """

    def __init__(
        self,
        profile: AgentProfile,
        *,
        gateway: ModelGateway,
        dispatcher: ToolDispatcher,
        model_id: str,
        max_turns: int | None = None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.profile = profile
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.model_id = model_id

        ceiling = MAX_TOOL_TURNS
        if profile.max_tool_turns is not None:
            ceiling = min(ceiling, profile.max_tool_turns)
        if max_turns is not None:
            # Downward only. A caller that could raise the ceiling past the
            # module constant could also raise it past a profile's narrowing,
            # which would make "configurable downward" a lie.
            ceiling = min(ceiling, max_turns)
        self.max_turns = max(1, ceiling)

        self.max_output_tokens = max_output_tokens or profile.max_output_tokens

    def run(self, task: str) -> RunResult:
        """Execute one task to completion.

        Returns:
            The model's final message and the conversation that produced it.

        Raises:
            MaxTurnsExceeded: the turn budget ran out.
            GatewayError: the model could not be reached or answered unusably.
                Propagates deliberately -- a provider failure is not something
                the model can be told about and recover from.
        """
        state = ConversationState()
        state.add_user(task)

        tool_specs = self.dispatcher.tool_specs()
        system = self.dispatcher.system_prompt

        tool_call_count = 0
        denied: list[tuple[str, str]] = []

        for turn in range(self.max_turns):
            state.turns = turn + 1

            response = self.gateway.complete(
                CompletionRequest(
                    system=system,
                    messages=tuple(state.messages),
                    tools=tool_specs,
                    model=self.model_id,
                    max_output_tokens=self.max_output_tokens,
                )
            )

            state.add_assistant(response.text, response.tool_calls)

            if not response.wants_tools:
                return RunResult(
                    text=response.text,
                    turns=turn + 1,
                    tool_calls=tool_call_count,
                    state=state,
                    denied=tuple(denied),
                )

            # Sequential, never concurrent: each call gets its own approval
            # prompt, and two prompts on one terminal at once is not a thing.
            for call in response.tool_calls:
                result = self.dispatcher.dispatch(call)
                tool_call_count += 1
                state.add_tool_result(call.id, result.to_json(), result.tool)

                if result.status.is_permission_refusal:
                    denied.append((result.tool, result.detail))

        raise MaxTurnsExceeded(self.max_turns, tool_call_count)


def summarise(result: RunResult) -> str:
    """A short operator-facing account of a run.

    Reports what happened, including refusals, so an operator does not have to
    read the transcript to learn that something they asked for never ran.
    """
    lines = [
        f"agent: {result.turns} model turn(s), {result.tool_calls} tool call(s)"
    ]
    if result.denied:
        lines.append("permission refusals:")
        lines.extend(f"  - {tool}: {detail}" for tool, detail in result.denied)
    return "\n".join(lines)
