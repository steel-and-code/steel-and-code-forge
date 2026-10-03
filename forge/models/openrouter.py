"""OpenRouter: the live provider, behind :class:`ModelGateway`.

Stdlib only. ``pyproject.toml`` declares zero runtime dependencies with the note
that the security layer is stdlib by design, and one POST plus one JSON parse
belongs in ``urllib`` rather than in a dependency tree with its own maintenance
tail. If that call is later judged wrong, replacing this file is additive: nothing
above the Protocol changes.

**The credential boundary.** This module owns ``FORGE_OPENROUTER_API_KEY``, and
that is the whole of its authority over it. The key is:

*   read from an injected mapping at construction, never from ``os.environ``
    directly -- so a test supplies its own and the process environment is never
    passed wholesale anywhere;
*   used only in the ``Authorization`` header;
*   never placed in a request payload, a system prompt, a message, a tool
    schema, a tool result, an execution environment, an audit record, or a log;
*   never present in an exception message, and neither is any prefix of it;
*   required at construction, so a missing key fails *before* a request is
    attempted rather than after one is sent.

Two rules here are about provider behaviour rather than about Forge, and both
exist because of a specific failure mode:

**A provider error body is never echoed.** Providers routinely quote the request
that produced the error, and the request carried the authorization header. Only
bounded, operator-safe metadata is surfaced: the HTTP status, and the provider's
error code and type when they parse as short identifiers. The message is
discarded, because "the message" is the field most likely to contain request
material.

**A 400 that names context is a distinct failure.** Truncating a conversation
silently would produce confident wrong answers from a partial history, so it
raises :class:`ContextTooLarge` and the run fails visibly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from .base import (
    CompletionRequest,
    ContextTooLarge,
    GatewayError,
    Message,
    ModelResponse,
    ToolCall,
    Usage,
)

__all__ = [
    "OPENROUTER_API_KEY_ENV",
    "OPENROUTER_ENDPOINT",
    "OpenRouterGateway",
    "ProviderCredentialMissing",
]


#: The one supported credential name. Named in errors, never valued in them.
OPENROUTER_API_KEY_ENV = "FORGE_OPENROUTER_API_KEY"

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_TIMEOUT_SECONDS = 120.0

#: Hard ceiling on a provider error message we ever construct ourselves. Any
#: text here is Forge's, not the provider's, but the bound is kept anyway so a
#: future edit that interpolates provider text has somewhere obvious to fail.
_MAX_SAFE_MESSAGE = 200

#: Field names in an error body that may be surfaced. A code or type is a short
#: identifier chosen by the provider; a message is free text that may quote the
#: request.
_SAFE_ERROR_FIELDS = ("code", "type")


class ProviderCredentialMissing(GatewayError):
    """The provider key is absent.

    Raised at construction so no request is ever attempted without one. The
    message names the variable and nothing else -- not the environment, not a
    value, not a prefix.
    """


def _safe_error_metadata(payload: Any) -> str:
    """Bounded, operator-safe description of a provider error.

    Deliberately narrow: the status, plus short identifier fields. A provider
    ``message`` is excluded because providers quote the offending request in it,
    and the offending request carried an authorization header.
    """
    parts: list[str] = []

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for field in _SAFE_ERROR_FIELDS:
                value = error.get(field)
                if isinstance(value, (int, str)) and str(value).strip():
                    candidate = str(value).strip()[:_MAX_SAFE_MESSAGE]
                    # Only short identifiers are safe to reproduce verbatim.
                    if len(str(value)) <= 64:
                        parts.append(f"{field}={candidate}")

    return ", ".join(parts)


def _looks_like_context_error(payload: Any) -> bool:
    """Whether an error is about the conversation being too large.

    Checked on the *status* and a small set of codes rather than by scanning the
    message: scanning provider prose for keywords is the same class of guess the
    command classifier refuses to make elsewhere.
    """
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if code in (400, 413):
        return True
    kind = error.get("type")
    return isinstance(kind, str) and "context" in kind.lower()


class OpenRouterGateway:
    """A :class:`~forge.models.base.ModelGateway` backed by OpenRouter.

    Args:
        environ: The credential mapping. Passed in rather than read from
            ``os.environ`` so a test never has to mutate the process
            environment, and so nothing passes the ambient environment wholesale.
        endpoint: Override for the API URL. Present for testing and for a future
            self-hosted route; not a policy hook.
        timeout: Seconds.

    Raises:
        ProviderCredentialMissing: ``FORGE_OPENROUTER_API_KEY`` is absent or
            empty. Raised here, at construction, so a missing key cannot become a
            failed request halfway through a run.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        endpoint: str = OPENROUTER_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        key = environ.get(OPENROUTER_API_KEY_ENV, "")
        if not key or not key.strip():
            raise ProviderCredentialMissing(
                f"{OPENROUTER_API_KEY_ENV} is not set; the model gateway cannot "
                "authenticate. Set it in the environment Forge was launched from."
            )

        # Held privately and never exposed on the instance, so it cannot reach a
        # repr, a log line, or anything that formats the gateway for display.
        self._api_key = key.strip()
        self._endpoint = endpoint
        self._timeout = timeout

    # -- transport -------------------------------------------------------

    def _post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[int, str]:
        """POST and return ``(status, body_text)``.

        Separated from :meth:`complete` so tests can substitute transport without
        substituting policy -- the same reason the approval channel is injectable
        rather than optional.
        """
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read().decode(
                    "utf-8", errors="replace"
                )
        except urllib.error.HTTPError as exc:
            # Read the body for its status code only. It is parsed and then
            # discarded; see the module docstring.
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - best effort on an error path
                text = ""
            return int(exc.code), text

    # -- protocol --------------------------------------------------------

    def complete(self, request: CompletionRequest) -> ModelResponse:
        """Answer ``request``, or raise a :class:`GatewayError`."""
        payload = self._build_payload(request)

        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GatewayError(
                f"request could not be encoded as JSON: {exc.__class__.__name__}"
            ) from None

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://steelandcode.com",
            "X-Title": "Steel & Code Forge",
        }

        try:
            status, text = self._post(
                self._endpoint, body, headers, self._timeout
            )
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            # The exception's own message can quote a URL. Bounded, and it
            # carries no request body and no key.
            reason = getattr(exc, "reason", exc)
            raise GatewayError(
                f"model provider could not be reached: "
                f"{type(reason).__name__}"
            ) from None

        parsed = self._parse_body(text)

        if status != 200:
            if _looks_like_context_error(parsed):
                raise ContextTooLarge(
                    "the conversation exceeded the model's context window "
                    f"(provider status {status})"
                )
            metadata = _safe_error_metadata(parsed)
            suffix = f" ({metadata})" if metadata else ""
            raise GatewayError(
                f"model provider returned HTTP {status}{suffix}"
            )

        return self._parse_response(parsed)

    # -- encoding --------------------------------------------------------

    def _build_payload(self, request: CompletionRequest) -> dict[str, Any]:
        """Encode the request in OpenRouter's wire format.

        The system prompt is a real ``system`` message here rather than a special
        parameter, because that is what the OpenAI-compatible schema expects. The
        credential is *not* in this payload; it is in a header.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system}
        ]

        for message in request.messages:
            entry: dict[str, Any] = {"role": message.role, "content": message.content}

            if message.role == "tool":
                # The provider matches a result to its call by id.
                entry["tool_call_id"] = message.tool_call_id
                if message.name:
                    entry["name"] = message.name

            if message.role == "assistant" and message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]

            messages.append(entry)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]

        return payload

    @staticmethod
    def _parse_body(text: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError) as exc:
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                f"({exc.__class__.__name__})"
            ) from None

    @staticmethod
    def _parse_response(payload: Any) -> ModelResponse:
        """Decode a successful body.

        Shape errors become ``GatewayError`` rather than ``KeyError``: a provider
        that changes its response shape is a gateway failure, not a crash inside
        the agent runtime.
        """
        if not isinstance(payload, dict):
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                "(expected an object)"
            )

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                "(no choices)"
            )

        first = choices[0]
        if not isinstance(first, dict):
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                "(malformed choice)"
            )

        message = first.get("message")
        if not isinstance(message, dict):
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                "(no message)"
            )

        text = message.get("content") or ""
        if not isinstance(text, str):
            raise GatewayError(
                "model provider returned a body this gateway cannot use "
                "(content was not text)"
            )

        calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for entry in raw_calls:
                if not isinstance(entry, dict):
                    continue
                function = entry.get("function")
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    continue
                calls.append(
                    ToolCall(
                        id=str(entry.get("id") or f"call_{len(calls) + 1}"),
                        name=name,
                        arguments=str(function.get("arguments") or "{}"),
                    )
                )

        usage_payload = payload.get("usage")
        usage = Usage()
        if isinstance(usage_payload, dict):
            prompt_tokens = usage_payload.get("prompt_tokens")
            completion_tokens = usage_payload.get("completion_tokens")
            usage = Usage(
                input_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
                output_tokens=(
                    completion_tokens if isinstance(completion_tokens, int) else None
                ),
            )

        return ModelResponse(
            text=text,
            tool_calls=tuple(calls),
            usage=usage,
            stop_reason=first.get("finish_reason"),
        )
