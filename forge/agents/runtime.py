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

One seam exists for surfaces that need to watch a run: an optional
:class:`~forge.events.EventSink` passed as ``events``. It receives lifecycle
events and nothing else, it cannot influence the loop, and ``None`` -- which is
what every existing caller passes -- changes nothing about behaviour. The
runtime still holds no opinion about permissions; an observer that could grant
one would be the same second opinion, wearing a different hat.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..events import EventSink, emit
from ..models.base import (
    CompletionRequest,
    GatewayError,
    Message,
    ModelGateway,
)
from ..tools.dispatcher import (
    FINISH_TOOL,
    ToolDispatcher,
    ToolResult,
    ToolStatus,
)
from .profile import AgentProfile
from .state import ConversationState

__all__ = [
    "AgentRuntime",
    "RunResult",
    "Completion",
    "MaxTurnsExceeded",
    "MAX_TOOL_TURNS",
    "MAX_CONTINUATION_NUDGES",
]


#: Ceiling on model round-trips for one run. Seventy-five is the recovered
#: Byte maximum: enough for a long, genuine task ("fix the failing tests")
#: while still bounding a runaway agent. Configurable *downward* per run and by
#: profile; a profile may never raise it.
MAX_TOOL_TURNS = 75


class Completion(str, Enum):
    """How a run ended.

    The model is the only thing that can *declare* completion: it does so by
    calling the ``finish`` tool with a ``status`` of ``done`` or ``blocked``.
    Absent that declaration the runtime never reports success -- a run whose
    model merely stopped emitting tool calls ends ``INCOMPLETE``, not ``DONE``.

        DONE        the model declared the task complete, with a report.
        BLOCKED     the model declared it could not proceed, with a report.
        INCOMPLETE  the model stopped talking without ever declaring an
                    outcome; its last prose is preserved, flagged as unresolved.
    """

    DONE = "done"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"


#: How many consecutive narration turns (a reply with no tool call and no
#: ``finish`` declaration) the runtime tolerates before it ends the run
#: ``INCOMPLETE``. Bounded so a model that only ever narrates intent cannot
#: spin the whole 75-turn budget; each nudge is appended as a model-visible
#: prompt to either act or declare an outcome.
MAX_CONTINUATION_NUDGES = 2

#: The prompt appended when a turn ended in narration with no tool call. It is
#: a ``user`` message rather than the system prompt, because it is issued
#: mid-run in response to the model's own behaviour, not a standing instruction.
CONTINUE_NUDGE = (
    "You have not signalled completion. Continue the task: make the next tool "
    "call, or, if the work is done, call the completion tool with status "
    "'done' and a summary, validation, and limitations; if you are blocked, "
    "call it with status 'blocked'. Writing what you intend to do does not end "
    "the run."
)


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
            tell it apart from the model's actual answer. For ``DONE`` and
            ``BLOCKED`` this is a rendering of the model's own ``finish``
            declaration fields; for ``INCOMPLETE`` it is the model's last prose,
            preserved verbatim.
        turns: Model round-trips consumed.
        tool_calls: Individual tool calls dispatched. Greater than ``turns`` when
            a turn carried parallel calls.
        state: The conversation, returned for inspection. Session-only: it is
            never written anywhere and is dropped when the process exits.
        denied: Permission refusals that occurred, as ``(tool, detail)`` pairs.
            Surfaced separately because an operator needs to know what was
            refused without reading the transcript.
        completion: How the run ended. ``DONE`` and ``BLOCKED`` only when the
            model declared it via the ``finish`` tool; ``INCOMPLETE`` when it
            stopped talking without declaring anything.
    """

    text: str
    turns: int
    tool_calls: int
    state: ConversationState
    denied: tuple[tuple[str, str], ...] = ()
    completion: Completion = Completion.INCOMPLETE


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
        events: Optional observational sink. Emits run and turn lifecycle only;
            it cannot influence the run in any way. ``None`` -- the default --
            means nothing is observed and nothing else changes.
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
        events: EventSink | None = None,
    ) -> None:
        self.profile = profile
        self.gateway = gateway
        self.dispatcher = dispatcher
        self.model_id = model_id
        #: Held for observation only. A surface reads what the run did; it has
        #: no way to say what the run should do.
        self.events = events

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

    def run(self, task: str, state: ConversationState | None = None) -> RunResult:
        """Execute one task to completion.

        ``state`` may be supplied by a session surface. It is appended to, never
        reconstructed from prose, so assistant tool calls and their results keep
        their provider-required ordering.

        Returns:
            The model's final message and the conversation that produced it.

        Raises:
            MaxTurnsExceeded: the turn budget ran out.
            GatewayError: the model could not be reached or answered unusably.
                Propagates deliberately -- a provider failure is not something
                the model can be told about and recover from.
        """
        if state is None:
            state = ConversationState()
        state.add_user(task)

        tool_specs = self.dispatcher.tool_specs()
        system = self.dispatcher.system_prompt

        tool_call_count = 0
        denied: list[tuple[str, str]] = []

        sink = self.events
        emit(
            sink,
            "run.started",
            agent=self.profile.name,
            model=self.model_id,
            max_turns=str(self.max_turns),
        )

        try:
            nudges = 0
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

                emit(
                    sink,
                    "model.reply",
                    turn=str(turn + 1),
                    text=response.text,
                    tool_calls=str(len(response.tool_calls)),
                )

                if not response.wants_tools:
                    # Narration only: no tool call, no finish declaration. The
                    # model has signalled nothing, so this must not read as
                    # completion. Nudge it a bounded number of times to act or
                    # declare, then end the run INCOMPLETE -- never DONE.
                    nudges += 1
                    if nudges > MAX_CONTINUATION_NUDGES:
                        return self._result(
                            state,
                            turn,
                            tool_call_count,
                            tuple(denied),
                            Completion.INCOMPLETE,
                            response.text,
                        )
                    state.add_user(CONTINUE_NUDGE)
                    continue

                # A turn that acted resets the narration budget: the model is
                # still working, not stalled.
                nudges = 0

                # Sequential, never concurrent: each call gets its own approval
                # prompt, and two prompts on one terminal at once is not a thing.
                for call in response.tool_calls:
                    result = self.dispatcher.dispatch(call)
                    tool_call_count += 1
                    state.add_tool_result(call.id, result.to_json(), result.tool)

                    if result.status.is_permission_refusal:
                        denied.append((result.tool, result.detail))

                    # A valid finish declaration is terminal. A malformed one is
                    # an ordinary invalid_arguments result the model can correct.
                    if call.name == FINISH_TOOL and result.status is ToolStatus.OK:
                        return self._finish_result(
                            state, turn, tool_call_count, tuple(denied), result
                        )
        except GatewayError as exc:
            # Observed, then re-raised unchanged. A provider failure still
            # propagates to the caller; what is added is that a surface watching
            # learns the run ended rather than simply stopping emitting.
            emit(sink, "run.failed", reason="gateway_error", detail=str(exc))
            raise

        emit(
            sink,
            "run.failed",
            reason="max_turns",
            turns=str(self.max_turns),
            tool_calls=str(tool_call_count),
        )
        raise MaxTurnsExceeded(self.max_turns, tool_call_count)

    def _result(
        self,
        state: ConversationState,
        turn: int,
        tool_call_count: int,
        denied: tuple[tuple[str, str], ...],
        completion: Completion,
        text: str,
    ) -> RunResult:
        """Build and report a terminal ``RunResult``.

        The one place a run ends normally -- no path returns a ``RunResult``
        except through here, so the ``run.completed`` event (carrying the
        outcome) is always paired with the value the caller sees.
        """
        emit(
            self.events,
            "run.completed",
            turns=str(turn + 1),
            tool_calls=str(tool_call_count),
            denied=str(len(denied)),
            completion=completion.value,
        )
        return RunResult(
            text=text,
            turns=turn + 1,
            tool_calls=tool_call_count,
            state=state,
            denied=denied,
            completion=completion,
        )

    def _finish_result(
        self,
        state: ConversationState,
        turn: int,
        tool_call_count: int,
        denied: tuple[tuple[str, str], ...],
        result: ToolResult,
    ) -> RunResult:
        """Turn a valid ``finish`` tool result into the terminal run result.

        The declared ``status`` chooses ``DONE`` or ``BLOCKED``; the report text
        is a rendering of the model's own declaration fields, never a synthesis.
        """
        content = result.content if isinstance(result.content, dict) else {}
        completion = _completion_from_declaration(content)
        return self._result(
            state,
            turn,
            tool_call_count,
            denied,
            completion,
            _render_finish(content),
        )


def _completion_from_declaration(content: dict[str, Any]) -> Completion:
    """Map a ``finish`` declaration's ``status`` onto a :class:`Completion`."""
    declared = content.get("status")
    return Completion.BLOCKED if declared == "blocked" else Completion.DONE


def _render_finish(content: dict[str, Any]) -> str:
    """Render the model's own finish fields for the operator.

    A thin, labelled transcription of what the model declared -- status, then
    the summary, validation (done only), and limitations it chose to state. It
    is not a summary of the transcript: every line here came from the model's
    declaration, and nothing is paraphrased.
    """
    status = content.get("status", Completion.DONE.value)
    lines = [f"status: {status}"]
    summary = str(content.get("summary") or "").strip()
    if summary:
        lines.append(f"summary: {summary}")
    if status == Completion.BLOCKED.value:
        limitations = str(content.get("limitations") or "").strip()
        if limitations:
            lines.append(f"blocked on: {limitations}")
    else:
        validation = str(content.get("validation") or "").strip()
        if validation:
            lines.append(f"validation: {validation}")
        limitations = str(content.get("limitations") or "").strip()
        if limitations:
            lines.append(f"limitations: {limitations}")
    return "\n".join(lines)


def summarise(result: RunResult) -> str:
    """A short operator-facing account of a run.

    Reports what happened, including refusals, so an operator does not have to
    read the transcript to learn that something they asked for never ran.
    """
    lines = [
        f"agent: {result.turns} model turn(s), {result.tool_calls} tool call(s)"
    ]
    lines.append(f"outcome: {result.completion.value}")
    if result.denied:
        lines.append("permission refusals:")
        lines.extend(f"  - {tool}: {detail}" for tool, detail in result.denied)
    return "\n".join(lines)
