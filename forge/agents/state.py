"""Conversation state: session-only, in-process, for one run.

Implements the v1 memory model that ``docs/agent-contract.md`` settles: session
memory only, durable project memory designed and disabled, personal memory out of
scope. Nothing here is written to disk, serialised, or pickled, and the object is
returned in ``RunResult`` for inspection and dropped when the process exits.

The security-relevant part is what this module *refuses* to hold. It stores
messages, and every message that reaches it has already passed through the
dispatcher -- which means a credential cannot get here unless the dispatcher put
it there deliberately. The provider key is never a message, never a tool result,
and never a system instruction; it lives in the gateway object alone. That is why
the state needs no scrubbing pass of its own: there is nothing to scrub, because
nothing secret-shaped is ever constructed as a message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading

from ..models.base import Message

__all__ = ["ConversationState", "ConversationStateStore", "DenialRecord"]


@dataclass(frozen=True)
class DenialRecord:
    """One cached refusal.

    Only *final permission refusals* are recorded: ``DENIED_BY_POLICY``,
    ``REJECTED``, and ``DENIED_UNATTENDED``. The distinction is not cosmetic --
    these are the outcomes where an operator (or policy) has already answered the
    question, so asking again would either re-prompt someone who said no or
    re-run work whose verdict cannot change.

    Malformed arguments, unknown tools, execution failures, timeouts, and
    internal errors are *not* cached. They are different failure classes, and
    several of them are legitimately worth retrying: a malformed call may be
    retried with corrected arguments and should reach the model again, while a
    denial retried identically cannot succeed.

    Attributes:
        canonical: The :meth:`ToolCall.canonical` identity of the call.
        result: The ``ToolResult`` to return again verbatim.
    """

    canonical: str
    result: object


@dataclass
class ConversationState:
    """The messages of one run, in order.

    Attributes:
        messages: The transcript, oldest first. The system instructions are *not*
            here -- they travel in every ``CompletionRequest.system`` because
            that is how providers accept them, and duplicating them as a message
            would make them look like something the operator said.
        turns: Model round-trips consumed so far.
        denials: Permission refusals already answered this run, keyed by
            canonical call identity.
    """

    messages: list[Message] = field(default_factory=list)
    turns: int = 0
    denials: dict[str, DenialRecord] = field(default_factory=dict)

    def add(self, message: Message) -> None:
        """Append one message."""
        self.messages.append(message)

    def add_user(self, content: str) -> None:
        """Record the operator's task."""
        self.add(Message(role="user", content=content))

    def add_assistant(
        self, content: str, tool_calls: tuple = ()
    ) -> None:
        """Record a model reply."""
        self.add(Message(role="assistant", content=content, tool_calls=tuple(tool_calls)))

    def add_tool_result(
        self, tool_call_id: str, content: str, name: str
    ) -> None:
        """Record a tool result, keyed to the call it answers."""
        self.add(
            Message(
                role="tool",
                content=content,
                tool_call_id=tool_call_id,
                name=name,
            )
        )

    def transcript_text(self) -> str:
        """Every message rendered as one string.

        For tests that need to assert something never reached the model. The
        alternative -- inspecting dataclass fields -- would miss anything encoded
        inside a JSON payload, and a credential smuggled into a tool result would
        be exactly that shape.
        """
        return "\n".join(
            f"[{m.role}] {m.name or ''} {m.content}".strip() for m in self.messages
        )

    def __len__(self) -> int:
        return len(self.messages)


class ConversationStateStore:
    """In-memory conversation states, isolated by workspace and agent."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], ConversationState] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(workspace: str | Path, agent: str) -> tuple[str, str]:
        return str(Path(workspace).expanduser().resolve()), agent.strip().lower()

    def get(self, workspace: str | Path, agent: str) -> ConversationState:
        key = self._key(workspace, agent)
        with self._lock:
            return self._states.setdefault(key, ConversationState())
