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
from enum import Enum
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
    "VerificationCode",
    "VerificationResult",
    "ModelVerifier",
    "GatewayError",
    "ContextTooLarge",
    "ModelListing",
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


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
#
# Deliberately *not* a second method on ``ModelGateway``. A gateway answers a
# conversation; a verifier answers "can this model serve at all". Widening the
# gateway Protocol would force every implementation -- including the offline
# ``ScriptedGateway`` -- to grow a method the agent runtime has no use for, and
# the gateway's one-method shape is a decision that layer documents defending.
# So verification is a separate, narrower Protocol that a provider may or may
# not implement, reached through ``ProviderSpec.verifier_factory``.


class VerificationCode(str, Enum):
    """Why a verification attempt came out the way it did.

    A flat vocabulary rather than a bitmask, because each of these is a
    different thing for an operator to *do*: a bad key is fixed in the
    environment, a timeout is retried later, and a model the provider no longer
    serves is fixed in the configuration file. Collapsing them into one boolean
    is what makes a health display useless exactly when something breaks.
    """

    #: The provider answered and offers this model.
    OK = "ok"

    #: The provider rejected the credential (HTTP 401/403).
    UNAUTHENTICATED = "unauthenticated"

    #: No credential was available to present. A configuration fact, not proof
    #: the model is unreachable -- hence distinct from ``UNREACHABLE``.
    CREDENTIAL_MISSING = "credential_missing"

    #: The provider did not answer within the bound. Distinct from
    #: ``UNREACHABLE`` because it is transient and usually not the model's fault.
    TIMEOUT = "timeout"

    #: The provider answered and does not list this model.
    NOT_FOUND = "not_found"

    #: Transport failure: connection refused, DNS failure, TLS error.
    UNREACHABLE = "unreachable"

    #: The provider answered with a body this verifier cannot parse.
    MALFORMED_RESPONSE = "malformed_response"

    #: The provider has no verifier in Forge. Declared-but-unimplemented is a
    #: first-class, reportable outcome, not a failure to construct one.
    UNIMPLEMENTED = "unimplemented"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of one verification attempt.

    A **value, never an exception**. A verification failure is an operator-facing
    status (``forge models status --verify``), and a traceback out of an
    inspection command tells the operator nothing about what to fix. Every
    construction site in ``forge/models/verify.py`` produces one of these.

    Attributes:
        model: The provider-specific model id that was checked.
        code: The machine-readable outcome. ``VerificationCode.OK`` iff
            :attr:`usable`.
        reason: A bounded, operator-safe sentence. Never contains a credential
            or provider response prose -- see :mod:`forge.models.openrouter`
            for why a provider body is never echoed.
        detail: Optional extra context (an HTTP status, a timeout bound). Kept
            separate so it can be surfaced without widening :attr:`reason`.
    """

    model: str
    code: VerificationCode
    reason: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether this attempt established the model could serve."""
        return self.code is VerificationCode.OK

    @classmethod
    def success(cls, model: str, *, reason: str, detail: str = "") -> (
        "VerificationResult"
    ):
        return cls(model=model, code=VerificationCode.OK, reason=reason, detail=detail)

    @classmethod
    def failure(
        cls, model: str, code: VerificationCode, *, reason: str, detail: str = ""
    ) -> "VerificationResult":
        return cls(model=model, code=code, reason=reason, detail=detail)

    def to_json(self) -> dict[str, object]:
        """One flat JSON-ready object, every field present."""
        return {
            "model": self.model,
            "code": self.code.value,
            "reason": self.reason,
            "detail": self.detail,
            "usable": self.usable,
        }


@runtime_checkable
class ModelVerifier(Protocol):
    """Can this provider serve this model, right now, cheaply?

    One method, deliberately distinct from :class:`ModelGateway`: this one is
    for the health seam and must never grow a completion path, a retry loop, or
    a tool-calling capability. A verifier that could generate tokens would make
    ``forge models status --verify`` an unbounded, billable operation.
    """

    def verify(self, model: str) -> VerificationResult:
        """Check ``model`` once and report the outcome.

        Returns:
            A :class:`VerificationResult`. **Never raises** for a provider-side
            failure -- a transport error, a timeout, a rejected credential, or
            an unparseable body all come back as a result value.
        """
        ...


@dataclass(frozen=True)
class ModelListing:
    """The models a provider is serving, or why that could not be determined.

    This is the *catalog* seam, distinct from :class:`VerificationResult`:
    verification asks "is this one model usable", a listing asks "what models
    does this provider offer". The Workbench model selector reads it to
    populate its per-provider model list.

    An unavailable provider is a reportable value, never an exception: the
    selector must be able to show "this provider is down" rather than crash, and
    a listing that returns ``available=False`` is exactly that. ``error`` is a
    short, operator-safe reason -- a transport exception type name, an HTTP
    status, or a shape complaint -- never provider prose, for the same reason a
    :class:`VerificationResult` never echoes a provider body.

    Attributes:
        provider: The provider name (``"local"``, ``"openrouter"``, ...).
        models: The model ids the provider offered, sorted. Empty when the
            listing failed or the provider offered nothing.
        available: Whether the provider answered with a usable listing.
        error: Why ``available`` is False. Empty on success.
    """

    provider: str
    models: tuple[str, ...] = ()
    available: bool = True
    error: str = ""
