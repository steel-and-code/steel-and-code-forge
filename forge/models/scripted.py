"""A scripted model gateway: no network, no key, fully deterministic.

Not a testing convenience bolted on afterwards. It is a required part of the
first implementation for three reasons:

*   **The suite must never make a live API call.** A test that spends money or
    needs a secret is a test that gets skipped, and a skipped test is a test that
    silently stops protecting anything.
*   **The loop is worth testing before a provider exists.** An offline gateway is
    what lets the tool loop be proven on its own terms, with the model treated as
    the one unreliable component in the system.
*   **Offline development is real.** Running an agent without a network or a key
    should be possible, and this is how.

A gateway that returns whatever it was handed is not a mock in the loose sense;
it is the control surface for the model, which is exactly what the runtime needs
to be tested against.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .base import (
    CompletionRequest,
    ContextTooLarge,
    GatewayError,
    ModelResponse,
    ToolCall,
    Usage,
)

__all__ = ["ScriptedGateway", "ScriptedTurn"]


@dataclass(frozen=True)
class ScriptedTurn:
    """One canned reply.

    Exactly one of ``response``, ``text``, or ``tool_calls`` should be set. A
    turn that sets none raises, because a silent empty reply would make a test
    pass for the wrong reason.

    Attributes:
        text: Prose reply.
        tool_calls: ``(name, arguments)`` pairs. ``arguments`` may be a ``str``
            (used verbatim, including deliberately malformed JSON) or a ``dict``
            (serialised).
        raises: An exception to raise instead of replying, for testing provider
            failure.
    """

    text: str | None = None
    tool_calls: tuple[tuple[str, object], ...] = ()
    raises: BaseException | None = None


def _as_tool_calls(pairs: Iterable[tuple[str, object]]) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for index, (name, arguments) in enumerate(pairs):
        if isinstance(arguments, str):
            raw = arguments
        else:
            raw = json.dumps(arguments, separators=(",", ":"))
        calls.append(ToolCall(id=f"call_{index + 1}", name=name, arguments=raw))
    return tuple(calls)


class ScriptedGateway:
    """Replays prepared replies, in order.

    Args:
        turns: The replies to return, one per ``complete`` call. The last turn
            repeats if the runtime asks for more, so a runaway loop is testable
            without an unbounded script.

    Records every request it received in :attr:`requests`, which is how a test
    asserts what the runtime actually sent -- including that a credential never
    appeared in it.

    Raises:
        GatewayError: when a turn is exhausted with nothing configured to
            repeat, which is a test bug rather than a runtime condition.
    """

    def __init__(
        self,
        turns: Sequence[ScriptedTurn] | None = None,
        *,
        repeat_last: bool = True,
        usage: Usage | None = None,
    ) -> None:
        self._turns: list[ScriptedTurn] = list(turns or [])
        self._repeat_last = repeat_last
        self._usage = usage or Usage(input_tokens=10, output_tokens=5)
        self._index = 0
        #: Every request received, in order.
        self.requests: list[CompletionRequest] = []

    @property
    def call_count(self) -> int:
        """How many times the model was asked. Equals the turn count."""
        return self._index

    @property
    def last_request(self) -> CompletionRequest:
        """The most recent request. Raises if the gateway was never called."""
        if not self.requests:
            raise GatewayError("ScriptedGateway received no requests")
        return self.requests[-1]

    def complete(self, request: CompletionRequest) -> ModelResponse:
        """Return the next scripted reply."""
        self.requests.append(request)

        if not self._turns:
            raise GatewayError(
                "ScriptedGateway has no turns configured; the runtime asked for "
                f"turn {self._index + 1}"
            )

        if self._index < len(self._turns):
            turn = self._turns[self._index]
        elif self._repeat_last:
            turn = self._turns[-1]
        else:
            raise GatewayError(
                f"ScriptedGateway exhausted after {len(self._turns)} turns; "
                f"the runtime asked for turn {self._index + 1}"
            )
        self._index += 1

        if turn.raises is not None:
            raise turn.raises

        if turn.text is None and not turn.tool_calls:
            raise GatewayError(
                "ScriptedTurn has neither text nor tool_calls; a silent empty "
                "reply would make a test pass for the wrong reason"
            )

        return ModelResponse(
            text=turn.text or "",
            tool_calls=_as_tool_calls(turn.tool_calls),
            usage=self._usage,
            stop_reason="tool_calls" if turn.tool_calls else "stop",
        )
