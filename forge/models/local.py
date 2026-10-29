"""Local llama.cpp model gateway.

Connects Forge to an OpenAI-compatible server running on the local machine --
llama.cpp's own server by default, and anything else that speaks the same two
paths (Ollama among them) once the endpoints are configured. No credential is
required. Policy and permissions remain outside the gateway.

Two configuration variables select the endpoints, because a tunnel or an
alternate port is a fact about the machine, not about the model:

``FORGE_LOCAL_ENDPOINT``
    The ``/v1/chat/completions`` URL. Defaults to
    ``http://127.0.0.1:8080/v1/chat/completions``.
``FORGE_LOCAL_MODELS_ENDPOINT``
    The ``/v1/models`` URL the health probe reads. Defaults to
    ``http://127.0.0.1:8080/v1/models``.

They are configured independently rather than derived from one base URL because
a proxy can serve the two paths on different prefixes, and because deriving one
from the other by string surgery is how a URL that happens to contain ``/v1``
twice ends up pointed at the wrong place.
"""

from __future__ import annotations

import json
import math
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit

from .base import (
    CompletionRequest,
    ContextTooLarge,
    GatewayError,
    ModelListing,
    ModelResponse,
    ToolCall,
    Usage,
    VerificationCode,
    VerificationResult,
)
import re
import xml.etree.ElementTree as ET

LOCAL_LLAMA_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_LLAMA_MODELS_ENDPOINT = "http://127.0.0.1:8080/v1/models"
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Seconds per verification attempt. Bounded and short: a health check an
#: operator waits a minute for is a health check they stop running.
VERIFY_TIMEOUT_SECONDS = 5.0

#: The one supported override name. Named in errors, never valued in them.
LOCAL_TIMEOUT_ENV = "FORGE_LOCAL_TIMEOUT_SECONDS"

#: Endpoint override for chat completions.
LOCAL_ENDPOINT_ENV = "FORGE_LOCAL_ENDPOINT"

#: Endpoint override for model verification.
LOCAL_MODELS_ENDPOINT_ENV = "FORGE_LOCAL_MODELS_ENDPOINT"

#: Sentinel meaning "no endpoint was passed; consult the environment".
#:
#: Comparing against ``LOCAL_LLAMA_ENDPOINT`` cannot express this: a caller who
#: explicitly passes the default URL is stating a choice, and a configuration
#: variable must not silently win over it. ``None`` costs nothing to represent
#: and makes the two cases distinguishable.
_UNSET = None


class LocalConfigurationError(GatewayError):
    """The local gateway was configured with a value it cannot use.

    Raised at construction, so a bad timeout cannot become a request that hangs
    or a socket that closes mid-response. The message names the variable and the
    rule it broke -- never the surrounding environment.
    """


def resolve_timeout_seconds(environ: Mapping[str, str]) -> float:
    """Seconds to allow a local model call, from ``FORGE_LOCAL_TIMEOUT_SECONDS``.

    An absent or blank value means "not set", so an empty export behaves like an
    unset one rather than as a configuration error.

    Raises:
        LocalConfigurationError: the value is not a finite number, or is not
            positive. Both would otherwise reach ``urlopen`` and fail there as a
            transport error with nothing to point at the real cause.
    """
    raw = environ.get(LOCAL_TIMEOUT_ENV, "").strip()

    if not raw:
        return DEFAULT_TIMEOUT_SECONDS

    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise LocalConfigurationError(
            f"{LOCAL_TIMEOUT_ENV} must be a number of seconds greater than 0; "
            "it is not a number"
        ) from None

    if not math.isfinite(value) or value <= 0:
        raise LocalConfigurationError(
            f"{LOCAL_TIMEOUT_ENV} must be a number of seconds greater than 0; "
            "it is not a positive, finite duration"
        )

    return value


def resolve_endpoint(
    environ: Mapping[str, str], env_var: str, default: str
) -> str:
    """An OpenAI-compatible endpoint URL read from ``environ``.

    An absent or blank value means "not set", so an empty export behaves like an
    unset one rather than as a configuration error -- the same rule
    :func:`resolve_timeout_seconds` applies.

    Only the *configured* value is validated. An endpoint passed explicitly to a
    constructor is trusted rather than validated, as the timeout has always been:
    a caller that supplies a literal is not being configured.

    Raises:
        LocalConfigurationError: the value is not a string, is not an ``http``
            or ``https`` URL, names no host, or carries embedded credentials.

    **The message names the variable and the rule, never the value.** A URL can
    carry a user-info component, and this endpoint takes no credential; echoing
    the offending string is how a rejected configuration becomes a leaked one.
    """
    raw = environ.get(env_var, "")

    if raw is None:
        return default

    if not isinstance(raw, str):
        raise LocalConfigurationError(
            f"{env_var} must be a URL string beginning with http:// or https://; "
            f"it is {type(raw).__name__}"
        )

    value = raw.strip()

    if not value:
        return default

    try:
        parts = urlsplit(value)
    except ValueError:
        raise LocalConfigurationError(
            f"{env_var} must be a URL string beginning with http:// or https://; "
            "it is not a parseable URL"
        ) from None

    scheme = parts.scheme.lower() if parts.scheme else ""

    if scheme not in ("http", "https"):
        raise LocalConfigurationError(
            f"{env_var} must be an http:// or https:// URL; it "
            f"{f'uses the {scheme!r} scheme' if scheme else 'has no scheme'}"
        )

    if not parts.netloc:
        raise LocalConfigurationError(
            f"{env_var} must name a host; it has a scheme but no host"
        )

    if parts.username is not None or parts.password is not None:
        raise LocalConfigurationError(
            f"{env_var} must not embed credentials in the URL; this endpoint "
            "takes no credential and sends none"
        )

    return value


class LocalLlamaGateway:
    """ModelGateway backed by a local llama.cpp OpenAI-compatible server.

    Args:
        endpoint: Override for the local API URL. Present for testing; not a
            policy hook. Given, it wins over ``FORGE_LOCAL_ENDPOINT`` and is
            trusted rather than validated, as the timeout has always been.
        timeout: Seconds. Left ``None``, the timeout comes from
            ``FORGE_LOCAL_TIMEOUT_SECONDS``; an explicit value here wins over the
            environment, and is trusted rather than validated, as it always was.
        environ: The mapping the timeout and the endpoint are read from. Passed
            in rather than read from ``os.environ`` so a test never has to mutate
            the process environment; ``None`` means the real process environment.
            The gateway and the verifier read the **same** mapping, which is what
            lets one environment describe one local service.

    Raises:
        LocalConfigurationError: ``FORGE_LOCAL_TIMEOUT_SECONDS`` is set to a
            value that is not a positive, finite number of seconds, or
            ``FORGE_LOCAL_ENDPOINT`` is set to a value that is not a usable URL.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = _UNSET,
        timeout: float | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ

        self._endpoint = (
            endpoint
            if endpoint is not _UNSET
            else resolve_endpoint(env, LOCAL_ENDPOINT_ENV, LOCAL_LLAMA_ENDPOINT)
        )
        self._timeout = (
            float(timeout) if timeout is not None else resolve_timeout_seconds(env)
        )

    @property
    def endpoint(self) -> str:
        """The URL this gateway sends completions to.

        Read-only, for the same reason :attr:`timeout_seconds` is: a caller can
        report the effective endpoint without being able to change it after
        construction.
        """
        return self._endpoint

    @property
    def timeout_seconds(self) -> float:
        """The seconds this gateway will wait on a local model call.

        Read-only, so a caller can report the effective timeout without being
        able to change it after construction.
        """
        return self._timeout

    def complete(self, request: CompletionRequest) -> ModelResponse:
        payload = self._build_payload(request)

        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GatewayError(
                f"request could not be encoded as JSON: {exc.__class__.__name__}"
            ) from None

        http_request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                http_request,
                timeout=self._timeout,
            ) as response:
                status = int(response.status)
                text = response.read().decode("utf-8", errors="replace")

        except urllib.error.HTTPError as exc:
            status = int(exc.code)

            try:
                text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                text = ""

        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GatewayError(
                f"local model could not be reached: {type(reason).__name__}"
            ) from None

        parsed = self._parse_body(text)

        if status != 200:
            if status in (400, 413):
                raise ContextTooLarge(
                    "the conversation may exceed the local model context window "
                    f"(provider status {status})"
                )

            raise GatewayError(
                f"local model returned HTTP {status}"
            )

        return self._parse_response(
            parsed,
            allowed_tools={tool.name for tool in request.tools},
        )

    @staticmethod
    def _build_payload(request: CompletionRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system}
        ]

        for message in request.messages:
            entry: dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }

            if message.role == "tool":
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
                "local model returned a body this gateway cannot use "
                f"({exc.__class__.__name__})"
            ) from None

    @staticmethod
    def _parse_response(
        payload: Any,
        allowed_tools: set[str] | None = None,
    ) -> ModelResponse:
        if not isinstance(payload, dict):
            raise GatewayError(
                "local model returned a body this gateway cannot use "
                "(expected an object)"
            )

        choices = payload.get("choices")

        if not isinstance(choices, list) or not choices:
            raise GatewayError(
                "local model returned a body this gateway cannot use "
                "(no choices)"
            )

        first = choices[0]

        if not isinstance(first, dict):
            raise GatewayError(
                "local model returned a body this gateway cannot use "
                "(malformed choice)"
            )

        message = first.get("message")

        if not isinstance(message, dict):
            raise GatewayError(
                "local model returned a body this gateway cannot use "
                "(no message)"
            )

        text = message.get("content") or ""

        if not isinstance(text, str):
            raise GatewayError(
                "local model returned a body this gateway cannot use "
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
               
        # llama.cpp/Qwen may return tool calls inside message content
        # instead of the OpenAI-style tool_calls field.
        if not calls and text:
            pattern = re.compile(
                r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
                re.DOTALL,
            )

            for match in pattern.finditer(text):
                try:
                    decoded = json.loads(match.group(1))
                except (TypeError, ValueError):
                    continue

                if not isinstance(decoded, dict):
                    continue

                name = decoded.get("name")
                arguments = decoded.get("arguments", {})

                if not isinstance(name, str) or not name:
                    continue

                try:
                    argument_text = json.dumps(
                        arguments,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    continue

                calls.append(
                    ToolCall(
                        id=f"call_{len(calls) + 1}",
                        name=name,
                        arguments=argument_text,
                    )
                )

            if calls:
                text = pattern.sub("", text).strip()

        
        # Some local Qwen responses emit tool calls as XML-style
        # self-closing tags, for example:
        # <search_files pattern="*.md" path="/workspace"/>
        if not calls and text and allowed_tools:
            xml_pattern = re.compile(
                r"<([A-Za-z_][A-Za-z0-9_]*)\s+([^<>]*?)/>",
                re.DOTALL,
            )

            matched_spans: list[tuple[int, int]] = []

            for match in xml_pattern.finditer(text):
                name = match.group(1)

                if name not in allowed_tools:
                    continue

                try:
                    element = ET.fromstring(match.group(0))
                except ET.ParseError:
                    continue

                arguments = dict(element.attrib)

                calls.append(
                    ToolCall(
                        id=f"call_{len(calls) + 1}",
                        name=name,
                        arguments=json.dumps(
                            arguments,
                            separators=(",", ":"),
                        ),
                    )
                )

                matched_spans.append(match.span())

            if calls and matched_spans:
                for start, end in reversed(matched_spans):
                    text = text[:start] + text[end:]

                text = text.strip()

        text, fenced_calls = LocalLlamaGateway._fenced_json_calls(text, allowed_tools)
        calls.extend(fenced_calls)

        # A <tools> wrapper is a compatibility fallback for models that emit
        # the tag but not a shape any other path here understands. It runs
        # after the fence pass and before the bare pass, and only when nothing
        # has been recognised yet, so it can never double-promote a call an
        # earlier parser already claimed.
        if not calls:
            text, block_calls = LocalLlamaGateway._tools_block_calls(
                text,
                allowed_tools,
            )
            calls.extend(block_calls)

        # Bare JSON is last resort, so it runs last and only on what the
        # fenced pass left behind.
        if not calls:
            text, bare_calls = LocalLlamaGateway._bare_json_calls(text, allowed_tools)
            calls.extend(bare_calls)

        usage_payload = payload.get("usage")
        usage = Usage()

        if isinstance(usage_payload, dict):
            prompt_tokens = usage_payload.get("prompt_tokens")
            completion_tokens = usage_payload.get("completion_tokens")

            usage = Usage(
                input_tokens=(
                    prompt_tokens
                    if isinstance(prompt_tokens, int)
                    else None
                ),
                output_tokens=(
                    completion_tokens
                    if isinstance(completion_tokens, int)
                    else None
                ),
            )

        return ModelResponse(
            text=text,
            tool_calls=tuple(calls),
            usage=usage,
            stop_reason=first.get("finish_reason"),
        )

    @classmethod
    def _fenced_json_calls(
        cls,
        text: str,
        allowed_tools: set[str] | None,
    ) -> tuple[str, list[ToolCall]]:
        """Lift tool calls out of ``json`` code fences in the model text.

        A local model asked for several tool calls at once frequently answers
        with several JSON objects stacked inside one fence rather than an array
        or several fences, so the fence body is scanned value by value instead of
        being read as a single object.

        Only objects naming a tool Forge actually offered are promoted; anything
        else stays prose. A fence that yields no allowed call is left untouched,
        because a code fence is not a grant to run whatever it contains.
        """
        calls: list[ToolCall] = []

        if not text or not allowed_tools:
            return text, calls

        fence_pattern = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
        matched_spans: list[tuple[int, int]] = []

        for match in fence_pattern.finditer(text):
            promoted = 0

            for value in cls._scan_json_values(match.group(1)):
                for candidate in value if isinstance(value, list) else (value,):
                    if not isinstance(candidate, dict):
                        continue

                    name = candidate.get("name")
                    arguments = candidate.get("arguments", {})

                    if not isinstance(name, str) or name not in allowed_tools:
                        continue

                    if not isinstance(arguments, dict):
                        continue

                    calls.append(
                        ToolCall(
                            id=f"call_{len(calls) + 1}",
                            name=name,
                            arguments=json.dumps(
                                arguments,
                                separators=(",", ":"),
                            ),
                        )
                    )
                    promoted += 1

            if promoted:
                matched_spans.append(match.span())

        if calls and matched_spans:
            for start, end in reversed(matched_spans):
                text = text[:start] + text[end:]

            text = text.strip()

        return text, calls

    @classmethod
    def _tools_block_calls(
        cls,
        text: str,
        allowed_tools: set[str] | None,
    ) -> tuple[str, list[ToolCall]]:
        """Promote tool calls wrapped in a ``<tools>`` block.

        Some local models emit their calls inside a ``<tools>...</tools>``
        wrapper rather than in a fence, in ``<tool_call>`` tags, or as trailing
        bare JSON. This path exists so that shape still reaches the runtime
        instead of being read to the operator as prose.

        It is a compatibility fallback, so it is deliberately narrow:

        *   Only the contents of the block are read. Prose outside the block is
            never inspected, so a tool-shaped object sitting in ordinary text
            stays text.
        *   The body must decode as a JSON object, or as an array of JSON
            objects. Anything else is not a call shape this path recognises.
        *   A call is promoted only when ``name`` names a tool Forge actually
            offered and ``arguments`` is an object. Naming a tool is not a
            grant to run it, and no other JSON is executed or executed-by-proxy
            on the way through.
        *   A block that is malformed, or that yields no allowed call, leaves
            the message completely untouched. There is no partial promotion:
            a wrapper the model got wrong is text the operator should see.
        """
        calls: list[ToolCall] = []

        if not text or not allowed_tools:
            return text, calls

        block_pattern = re.compile(
            r"<tools\b[^>]*>(.*?)</tools\s*>",
            re.DOTALL | re.IGNORECASE,
        )
        matched_spans: list[tuple[int, int]] = []

        for match in block_pattern.finditer(text):
            promoted = 0

            for candidate in cls._block_candidates(match.group(1)):
                name = candidate.get("name")
                arguments = candidate.get("arguments")

                if not isinstance(name, str) or name not in allowed_tools:
                    continue

                if not isinstance(arguments, dict):
                    continue

                calls.append(
                    ToolCall(
                        id=f"call_{len(calls) + 1}",
                        name=name,
                        arguments=json.dumps(
                            arguments,
                            separators=(",", ":"),
                        ),
                    )
                )
                promoted += 1

            if promoted:
                matched_spans.append(match.span())

        if calls and matched_spans:
            for start, end in reversed(matched_spans):
                text = text[:start] + text[end:]

            text = text.strip()

        return text, calls

    @classmethod
    def _block_candidates(cls, body: str) -> list[dict[str, Any]]:
        """The call-shaped objects inside a ``<tools>`` block body.

        A body that decodes as one object yields one candidate; a body that
        decodes as an array yields the dictionaries in it, skipping any
        non-object members. A body that decodes as neither, or not at all,
        yields nothing -- there is no scanning for objects buried in a string.
        """
        stripped = body.strip()

        if not stripped:
            return []

        try:
            value = json.loads(stripped)
        except (TypeError, ValueError):
            return []

        if isinstance(value, dict):
            return [value]

        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]

        return []

    @classmethod
    def _bare_json_calls(
        cls,
        text: str,
        allowed_tools: set[str] | None,
    ) -> tuple[str, list[ToolCall]]:
        """Promote an unfenced JSON tool call sitting at the end of the message.

        A local model that has not learned the OpenAI ``tool_calls`` field often
        writes its call as a bare object anyway, with no fence around it. Without
        this, the object stays in the prose, ``wants_tools`` is false, and the
        runtime hands the operator a JSON blob dressed up as the agent's answer.

        Every other recovery path in this module is forgiving, and this one is
        not, because bare JSON is the shape a model is most likely to be
        *describing* rather than requesting. So the promotion is narrow by
        design:

        *   Only the trailing non-whitespace segment is inspected. An object in
            the middle of prose is text, however tool-shaped it looks.
        *   The whole of that segment must be exactly one JSON object, with
            exactly the keys ``name`` and ``arguments``. The fence path tolerates
            extra keys because a fence is already a strong signal; an unfenced
            object with neighbours is usually payload, not a request.
        *   ``name`` must name a tool Forge actually offered and ``arguments``
            must be an object. The allowed-tools check is the same one the fence
            path applies: naming a tool is not a grant to run it.

        Anything else leaves the message untouched. There is no partial result
        and no attempt to find a call that "almost" fits -- a message that fails
        these tests is a message Forge did not understand, and returning it
        unchanged is the honest outcome.
        """
        calls: list[ToolCall] = []

        if not text or not allowed_tools:
            return text, calls

        stripped = text.rstrip()
        start = cls._trailing_object_start(stripped)

        if start is None:
            return text, calls

        decoder = json.JSONDecoder()

        try:
            value, end = decoder.raw_decode(stripped, start)
        except ValueError:
            return text, calls

        # The object must consume the trailing segment whole. Anything after it
        # -- a closing bracket, trailing commentary, a second object -- means
        # this was not a lone trailing call.
        if end != len(stripped) or not isinstance(value, dict):
            return text, calls

        if set(value) != {"name", "arguments"}:
            return text, calls

        name = value["name"]
        arguments = value["arguments"]

        if not isinstance(name, str) or name not in allowed_tools:
            return text, calls

        if not isinstance(arguments, dict):
            return text, calls

        try:
            argument_text = json.dumps(arguments, separators=(",", ":"))
        except (TypeError, ValueError):
            return text, calls

        calls.append(
            ToolCall(
                id="call_1",
                name=name,
                arguments=argument_text,
            )
        )

        return text[:start].rstrip(), calls

    @staticmethod
    def _trailing_object_start(stripped: str) -> int | None:
        """Where the final top-level JSON object in ``stripped`` begins.

        Scans forward for each ``{`` and returns the one whose object runs to
        the very end of the message. Text before that point is prose to be kept.
        Braces nested inside string values are skipped by the decoder itself:
        only a candidate that decodes cleanly to the end can win.
        """
        index = stripped.find("{")

        while index != -1:
            try:
                _, end = json.JSONDecoder().raw_decode(stripped, index)
            except ValueError:
                index = stripped.find("{", index + 1)
                continue

            if end == len(stripped):
                return index

            index = stripped.find("{", index + 1)

        return None

    @staticmethod
    def _scan_json_values(body: str) -> list[Any]:
        """Decode every top-level JSON value in ``body``, ignoring the rest.

        Rescanning past a decode error is what keeps one malformed object from
        swallowing the valid ones stacked around it.
        """
        decoder = json.JSONDecoder()
        found: list[Any] = []
        index = 0
        length = len(body)

        while index < length:
            char = body[index]

            if char.isspace() or char in ",":
                index += 1
                continue

            try:
                value, index = decoder.raw_decode(body, index)
            except ValueError:
                index += 1
                continue

            found.append(value)

        return found


class LocalLlamaVerifier:
    """A :class:`~forge.models.base.ModelVerifier` for a local llama.cpp server.

    Reads ``GET /v1/models`` and matches the model id. Cheap in the strongest
    sense: a loopback request, no generation, no money, and it never touches the
    agent runtime.

    **A local endpoint takes no credential, and this sends none.** No
    ``Authorization`` header is attached even if an operator has one in the
    environment -- an unauthenticated loopback service has no use for it, and
    attaching one would leak it across the machine to a process Forge does not
    control.

    **A refused connection is the common case, not an exception.** With nothing
    listening on the port, ``urlopen`` raises immediately, so the failure an
    operator hits most often is the fastest one to report.

    **The endpoint is configuration, not a constant.** ``FORGE_LOCAL_MODELS_ENDPOINT``
    selects the listing URL so a verification pass can ask the same service the
    gateway will send to. Left unset the llama.cpp default applies, and a probe
    of a machine with nothing running on it still reports the operational
    "service may not be running" wording rather than a new failure mode.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = _UNSET,
        timeout: float = VERIFY_TIMEOUT_SECONDS,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        env = os.environ if environ is None else environ

        self._endpoint = (
            endpoint
            if endpoint is not _UNSET
            else resolve_endpoint(
                env, LOCAL_MODELS_ENDPOINT_ENV, LOCAL_LLAMA_MODELS_ENDPOINT
            )
        )
        self._timeout = timeout

    @property
    def endpoint(self) -> str:
        """The URL this verifier asks for a model listing.

        Read-only for the same reason the gateway's is: an operator can check
        which endpoint a probe actually contacted without being able to move it
        after construction.
        """
        return self._endpoint

    def verify(self, model: str) -> VerificationResult:
        """Check ``model`` once. Returns a result; never raises."""
        try:
            status, text = self._get(self._endpoint, self._timeout)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            text = ""
        except (socket.timeout, TimeoutError):
            return VerificationResult.failure(
                model,
                VerificationCode.TIMEOUT,
                reason=(
                    f"the local endpoint did not answer the model listing "
                    f"within {self._timeout:g}s"
                ),
                detail="timeout",
            )
        except urllib.error.URLError as exc:
            return self._transport_failure(model, exc)
        except OSError as exc:
            return self._transport_failure(model, exc)

        if status == 404:
            # A server answering but serving no listing is a different problem
            # from no server at all, and worth saying so.
            return VerificationResult.failure(
                model,
                VerificationCode.NOT_FOUND,
                reason=(
                    "the local endpoint answered but does not expose a model "
                    "listing at this path"
                ),
                detail="http 404",
            )

        if status != 200:
            return VerificationResult.failure(
                model,
                VerificationCode.UNREACHABLE,
                reason=f"the local endpoint returned HTTP {status} for the model listing",
                detail=f"http {status}",
            )

        ids, malformed = _parse_local_listing(text)
        if malformed:
            return VerificationResult.failure(
                model,
                VerificationCode.MALFORMED_RESPONSE,
                reason=(
                    "the local endpoint returned a model listing this verifier "
                    f"cannot use ({malformed})"
                ),
                detail="unparseable listing",
            )

        if model in ids:
            return VerificationResult.success(
                model,
                reason="the local endpoint is serving this model",
                detail=f"{len(ids)} model(s) listed",
            )

        return VerificationResult.failure(
            model,
            VerificationCode.NOT_FOUND,
            reason=(
                "the local endpoint is running but is not serving this model; "
                "it may not be loaded"
            ),
            detail=f"{len(ids)} model(s) listed",
        )

    def _get(self, url: str, timeout: float) -> tuple[int, str]:
        """One GET, returning ``(status, body_text)``."""
        request = urllib.request.Request(
            url, headers={"Accept": "application/json"}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode(
                "utf-8", errors="replace"
            )

    @staticmethod
    def _transport_failure(
        model: str, exc: urllib.error.URLError | OSError
    ) -> VerificationResult:
        """Name the exception type, never its message.

        The exception message from a loopback failure can carry a path or a
        socket detail that has no place in a status line.

        The timeout and unreachable wordings are deliberately different, because
        the operator's next action differs. A timeout is worth waiting out; a
        refused connection means nothing is listening, and the cause is almost
        always that a configured local service was never started. That is a
        configuration fact about the machine rather than a model that cannot be
        served, and it should read as one -- otherwise a laptop with no
        llama.cpp running reports in the same voice as a provider outage.
        """
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return VerificationResult.failure(
                model,
                VerificationCode.TIMEOUT,
                reason=(
                    "local endpoint unreachable; the configured service did not "
                    "answer within the timeout and may not be running"
                ),
                detail="timeout",
            )
        return VerificationResult.failure(
            model,
            VerificationCode.UNREACHABLE,
            reason=(
                "local endpoint unreachable; configured service may not be "
                f"running ({type(reason).__name__})"
            ),
            detail=type(reason).__name__,
        )

    def list_models(self) -> ModelListing:
        """The model ids the local endpoint is serving, or why that is unknown.

        Distinct from :meth:`verify`: a listing asks "what models does this
        provider offer", not "is this one model usable". It is the method the
        Workbench model selector reads to populate its per-provider model list,
        and it reuses the same ``/v1/models`` GET and the same parse as
        verification -- one listing shape, one endpoint configuration, two
        questions asked of it.

        Never raises for a provider-side failure. An unreachable endpoint, a
        non-200 status, and an unparseable listing are all a
        :class:`~forge.models.base.ModelListing` with ``available=False`` and a
        short reason, because an unavailable local service is a fact the
        selector must surface, not an exception to catch.
        """
        try:
            status, text = self._get(self._endpoint, self._timeout)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            text = ""
        except (socket.timeout, TimeoutError):
            return ModelListing(provider="local", available=False, error="timeout")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return ModelListing(
                provider="local", available=False, error=type(reason).__name__
            )

        if status != 200:
            return ModelListing(
                provider="local", available=False, error=f"http {status}"
            )

        ids, malformed = _parse_local_listing(text)
        if malformed:
            return ModelListing(
                provider="local", available=False, error="unparseable listing"
            )

        return ModelListing(provider="local", models=tuple(sorted(ids)), available=True)


def _parse_local_listing(text: str) -> tuple[frozenset[str], str]:
    """The model ids in an OpenAI-compatible listing, plus a shape complaint.

    ``/v1/models`` is specified as ``{"data": [{"id": ...}, ...]}``, the same
    shape OpenRouter serves, but parsed here rather than shared: a local server
    is not required to track a hosted provider's exact behaviour, and coupling
    the two would make a divergence in either one a change to both.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        return frozenset(), f"{exc.__class__.__name__}"

    if not isinstance(payload, dict):
        return frozenset(), "expected an object"

    entries = payload.get("data")
    if not isinstance(entries, list):
        return frozenset(), "no data array"

    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            identifier = entry.get("id")
            if isinstance(identifier, str) and identifier.strip():
                ids.add(identifier.strip())

    if not ids:
        return frozenset(), "no usable model ids"

    return frozenset(ids), ""
