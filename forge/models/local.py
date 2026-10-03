"""Local llama.cpp model gateway.

Connects Forge to an OpenAI-compatible llama.cpp server running locally.
No credential is required. Policy and permissions remain outside the gateway.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import (
    CompletionRequest,
    ContextTooLarge,
    GatewayError,
    ModelResponse,
    ToolCall,
    Usage,
)
import re
import xml.etree.ElementTree as ET

LOCAL_LLAMA_ENDPOINT = "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 120.0


class LocalLlamaGateway:
    """ModelGateway backed by a local llama.cpp OpenAI-compatible server."""

    def __init__(
        self,
        *,
        endpoint: str = LOCAL_LLAMA_ENDPOINT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._endpoint = endpoint
        self._timeout = timeout

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
