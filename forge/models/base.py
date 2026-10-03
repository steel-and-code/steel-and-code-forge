"""Model gateway: the provider-neutral boundary.

One interface, and nothing else above it. Forge talks to a model through
:class:`ModelGateway` and to nothing else, so swapping a provider is a change to
one implementation rather than to the agent runtime.

**This module holds no policy, and must not acquire any.** No classifier is
imported here, no workspace is consulted, and no approval is possible. The model
decides what it wants to attempt; Forge decides whether it is allowed, and that
decision happens one layer down in ``forge/tools/dispatcher.py``. A gateway that
could widen a permission would collapse the separation the whole safety stack
rests on, and ``tests/test_models.py`` asserts the absence structurally.

Design choices that are not obvious:

*   ``ToolCall.arguments`` stays a **raw JSON string**, exactly as the provider
    returned it. Decoding is the dispatcher's job. A provider that returns
    malformed JSON therefore produces a ``ToolResult`` with
    ``status="invalid_arguments"`` — a value the model can read and correct —
    rather than an exception raised inside the gateway where nobody can act on
    it.

*   No streaming. A tool loop does not benefit from it, and streaming would force
    the gateway to hold a connection open across a human approval prompt.

*   ``ToolSpec.input_schema`` is a plain JSON-Schema dict because that is what
    every major provider's wire format expects. There is deliberately **no**
    output-schema field: providers do not consume one, so inventing it would be
    a field that always carries the same information and always has to be kept in
    sync by hand. Output shape is documented per tool instead.

*   Usage counts are optional. Providers differ in what they report, and a
    missing count is ``None`` rather than a zero that would silently pollute
    totals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "Role",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Usage",
    "CompletionRequest",
    "ModelResponse",
    "ModelGateway",
    "GatewayError",
    "ContextTooLarge",
]

Role = Literal["user", "assistant", "tool"]


class GatewayError(RuntimeError):
    """The model could not be reached, or answered with something unusable.

    Raised for every provider-side failure: a transport error, a timeout, an
    HTTP error status, or a body that is not the shape this protocol describes.

    Raised rather than returned because there is nothing the model can do about
    a gateway failure. A refusal the operator declined is model-visible; a
    provider that is down is not, and retrying inside the runtime would turn a
    visible failure into a stall.

    **Messages must be operator-safe.** A provider error body can echo the
    request that produced it, and the request carried an authorization header.
    Nothing that reached a provider may be repeated into an exception message;
    see :mod:`forge.models.openrouter` for how that is enforced.
    """


class ContextTooLarge(GatewayError):
    """The conversation no longer fits the model's context window.

    Separate from :class:`GatewayError` because it is not a transport problem and
    the honest response differs: the conversation is too big, and truncating it
    silently would produce confident wrong answers from a partial history.
    """


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the model asked for.

    Attributes:
        id: Provider-assigned identifier, echoed back on the result so the
            provider can match a call to its answer.
        name: The tool's name. Matched against a closed registry; an unknown name
            is a ``ToolResult``, not an exception.
        arguments: Raw JSON text. Undecoded on purpose -- see the module
            docstring.
    """

    id: str
    name: str
    arguments: str

    def canonical(self) -> str:
        """A stable identity for this call, used by the denial cache.

        Built from name and arguments only, deliberately excluding ``id``: a
        model retrying the same denied call will usually mint a fresh id, and the
        cache must recognise the repeat. Falls back to the raw arguments when
        they are not decodable, since a malformed call is not cached anyway.
        """
        import json

        try:
            decoded = json.loads(self.arguments)
            stable = json.dumps(decoded, sort_keys=True, separators=(",", ":"))
        except (ValueError, TypeError):
            stable = self.arguments.strip()
        return f"{self.name}\x1f{stable}"


@dataclass(frozen=True)
class Message:
    """One turn of the conversation.

    Attributes:
        role: ``user``, ``assistant``, or ``tool``. There is no ``system`` role
            here: the system instructions travel in
            :attr:`CompletionRequest.system`, separately, because that is how
            providers actually accept them and folding them in would make them
            look like a user message in the transcript.
        content: Text. Tool results are text; the structured form is in
            :class:`~forge.tools.dispatcher.ToolResult`.
        tool_call_id: Set on a ``tool`` message, matching the call it answers.
        tool_calls: Set on an ``assistant`` message carrying requested calls.
        name: Which tool produced a ``tool`` message. Diagnostics for the
            operator, and provider-required on some wire formats.
    """

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    name: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model.

    The schema travels in the request rather than being described in the system
    prompt: the agent contract forbids duplicating the allowed-tools list in
    prose, because two copies drift and only one gets updated.
    """

    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token accounting, when the provider reports it.

    Every field is optional because providers differ, and ``None`` means "not
    reported" rather than zero -- a fabricated zero would corrupt any total.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class CompletionRequest:
    """One request to a model.

    Attributes:
        system: The assembled system instructions.
        messages: The conversation so far, oldest first. Never contains the
            system message.
        tools: The tools offered. Empty is legal -- a model with no tools simply
            answers in text.
        model: Provider-specific model identifier. Resolved from configuration,
            never invented here.
        max_output_tokens: An explicit bound. It lives in the request rather than
            only in configuration so the gateway always has a ceiling even if a
            caller forgets to set one.
    """

    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    model: str = ""
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class ModelResponse:
    """What a model said.

    Attributes:
        text: Prose. Present even alongside tool calls, because a model
            frequently narrates before acting and that narration is worth
            showing the operator.
        tool_calls: Calls requested. Empty means the model is finished.
        usage: Token accounting if reported.
        stop_reason: Provider-reported reason, for diagnostics.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None

    @property
    def wants_tools(self) -> bool:
        """Whether the model asked for something rather than finishing."""
        return bool(self.tool_calls)


@runtime_checkable
class ModelGateway(Protocol):
    """The whole provider interface.

    One method. Every provider addition is a reason to want more than one, and
    each one also widens what the runtime must implement to stay
    provider-neutral -- so v1 has exactly this.
    """

    def complete(self, request: CompletionRequest) -> ModelResponse:
        """Answer ``request``.

        Raises:
            GatewayError: transport failure, timeout, HTTP error, or an
                unusable body.
            ContextTooLarge: the conversation exceeds the model's window.
        """
        ...
