"""Tests for the model layer: the protocol, the scripted gateway, and the key.

Two things are being protected here, and they are different in kind.

The first is **structural**: the model layer holds no policy and the gateway
interface stays minimal. Those are assertions about imports and about the shape
of a Protocol, because neither is visible to a behavioural test.

The second is the **provider credential**, which is a real security boundary
defined in ``docs/byte-tools.md`` and only partially implemented. The rules under
test are that the key is readable by the bootstrap layer alone, that it never
becomes a message, a tool result, a system prompt, or model-visible exception,
and that its absence fails visibly before any request is attempted.

Nothing here makes a network call.
"""

from __future__ import annotations

import ast
import json
import pathlib
from typing import Any

import pytest

from forge.models.base import (
    CompletionRequest,
    ContextTooLarge,
    GatewayError,
    Message,
    ModelGateway,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from forge.models.scripted import ScriptedGateway, ScriptedTurn


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


def test_tool_call_canonical_is_stable_across_key_order() -> None:
    """Equivalent arguments are the same call.

    A model retrying a denied call usually mints a fresh id and reorders keys. The
    denial cache keys on this, so instability would mean re-prompting an operator
    who already said no.
    """
    first = ToolCall(id="a", name="t", arguments=json.dumps({"x": 1, "y": 2}))
    second = ToolCall(id="b", name="t", arguments=json.dumps({"y": 2, "x": 1}))

    assert first.canonical() == second.canonical()


def test_tool_call_canonical_distinguishes_different_arguments() -> None:
    assert (
        ToolCall(id="a", name="t", arguments=json.dumps({"x": 1})).canonical()
        != ToolCall(id="a", name="t", arguments=json.dumps({"x": 2})).canonical()
    )


def test_tool_call_canonical_ignores_the_call_id() -> None:
    first = ToolCall(id="call_1", name="t", arguments='{"x":1}')
    second = ToolCall(id="call_99", name="t", arguments='{"x": 1}')

    assert first.canonical() == second.canonical()


def test_tool_call_canonical_survives_malformed_json() -> None:
    """A malformed call still gets a stable identity; it is simply not cached."""
    call = ToolCall(id="a", name="t", arguments="{oops")

    assert call.canonical() == ToolCall(id="b", name="t", arguments="{oops").canonical()


def test_model_response_reports_whether_it_wants_tools() -> None:
    assert ModelResponse(text="done").wants_tools is False
    assert ModelResponse(tool_calls=(ToolCall("1", "t", "{}"),)).wants_tools is True


def test_usage_distinguishes_unreported_from_zero() -> None:
    """`None` means "not reported"; a fabricated zero would corrupt totals."""
    usage = Usage()

    assert usage.input_tokens is None
    assert usage.output_tokens is None


# --------------------------------------------------------------------------
# ScriptedGateway
# --------------------------------------------------------------------------


def test_scripted_gateway_returns_turns_in_order() -> None:
    gateway = ScriptedGateway([ScriptedTurn(text="one"), ScriptedTurn(text="two")])

    assert gateway.complete(CompletionRequest(system="s", messages=())).text == "one"
    assert gateway.complete(CompletionRequest(system="s", messages=())).text == "two"
    assert gateway.call_count == 2


def test_scripted_gateway_records_every_request() -> None:
    """What the runtime actually sent, for a test to assert on."""
    gateway = ScriptedGateway([ScriptedTurn(text="ok")])
    request = CompletionRequest(
        system="system prompt", messages=(Message(role="user", content="hi"),)
    )

    gateway.complete(request)

    assert gateway.last_request is request
    assert gateway.last_request.system == "system prompt"


def test_scripted_gateway_builds_tool_calls() -> None:
    gateway = ScriptedGateway(
        [ScriptedTurn(tool_calls=(("read_file", {"path": "a.txt"}),))]
    )

    response = gateway.complete(CompletionRequest(system="s", messages=()))

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {"path": "a.txt"}


def test_scripted_gateway_accepts_raw_argument_text() -> None:
    """Used to script deliberately malformed JSON."""
    gateway = ScriptedGateway([ScriptedTurn(tool_calls=(("t", "{not json"),))])

    response = gateway.complete(CompletionRequest(system="s", messages=()))

    assert response.tool_calls[0].arguments == "{not json"


def test_scripted_gateway_can_raise_a_configured_error() -> None:
    gateway = ScriptedGateway([ScriptedTurn(raises=GatewayError("down"))])

    with pytest.raises(GatewayError):
        gateway.complete(CompletionRequest(system="s", messages=()))


def test_scripted_gateway_rejects_an_empty_turn() -> None:
    """A silent empty reply would make a test pass for the wrong reason."""
    gateway = ScriptedGateway([ScriptedTurn()])

    with pytest.raises(GatewayError, match="neither text nor tool_calls"):
        gateway.complete(CompletionRequest(system="s", messages=()))


def test_scripted_gateway_repeats_the_last_turn_by_default() -> None:
    """A runaway loop must be testable without an unbounded script."""
    gateway = ScriptedGateway([ScriptedTurn(tool_calls=(("t", {}),))])

    for _ in range(5):
        gateway.complete(CompletionRequest(system="s", messages=()))

    assert gateway.call_count == 5


def test_scripted_gateway_can_refuse_to_repeat() -> None:
    gateway = ScriptedGateway([ScriptedTurn(text="only")], repeat_last=False)
    gateway.complete(CompletionRequest(system="s", messages=()))

    with pytest.raises(GatewayError, match="exhausted"):
        gateway.complete(CompletionRequest(system="s", messages=()))


def test_scripted_gateway_needs_no_credential(monkeypatch) -> None:
    """The offline path works with no environment at all.

    This is why the suite can never need a paid call: the model is a parameter,
    not a dependency of the tests.
    """
    monkeypatch.delenv("FORGE_OPENROUTER_API_KEY", raising=False)
    gateway = ScriptedGateway([ScriptedTurn(text="ok")])

    assert gateway.complete(CompletionRequest(system="s", messages=())).text == "ok"


# --------------------------------------------------------------------------
# Protocol shape
# --------------------------------------------------------------------------


def test_gateway_protocol_has_one_method() -> None:
    """One method keeps the runtime provider-neutral.

    A second method is a reason the runtime must branch on provider capability,
    which is the thing the Protocol exists to prevent.
    """
    public = [
        name
        for name in vars(ModelGateway)
        if not name.startswith("_") and callable(getattr(ModelGateway, name, None))
    ]

    assert public == ["complete"]


def test_scripted_gateway_satisfies_the_protocol() -> None:
    assert isinstance(ScriptedGateway([ScriptedTurn(text="x")]), ModelGateway)


def test_completion_request_carries_an_explicit_output_bound() -> None:
    """A caller that forgets a bound still cannot ask for unbounded output."""
    request = CompletionRequest(system="s", messages=())

    assert request.max_output_tokens > 0


# --------------------------------------------------------------------------
# Structural: the model layer holds no policy
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_model_layer_imports_no_policy_module() -> None:
    """The model decides what it wants; Forge decides whether it is allowed.

    A gateway that could reach a classifier would collapse the separation the
    whole safety stack rests on.
    """
    package = pathlib.Path(__file__).resolve().parent.parent / "forge" / "models"
    forbidden = {
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.dispatcher",
        "forge.tools.workspace",
        "forge.agents.runtime",
    }

    for path in sorted(package.glob("*.py")):
        overlap = _imported_modules(path) & forbidden
        assert overlap == set(), f"{path.name} imports {overlap}"


def test_model_layer_defines_no_classifier() -> None:
    """A named prohibition, checked by source rather than by behaviour."""
    package = pathlib.Path(__file__).resolve().parent.parent / "forge" / "models"

    for path in sorted(package.glob("*.py")):
        source = path.read_text()
        for symbol in ("CommandClassifier", "SecretClassifier", "Approver", "Executor"):
            assert symbol not in source, f"{path.name} references {symbol}"


# --------------------------------------------------------------------------
# Provider credential boundary
# --------------------------------------------------------------------------


def test_api_key_environment_variable_name() -> None:
    """The one supported name, asserted so a rename is deliberate."""
    from forge.models.openrouter import OPENROUTER_API_KEY_ENV

    assert OPENROUTER_API_KEY_ENV == "FORGE_OPENROUTER_API_KEY"


def test_missing_key_fails_before_any_request() -> None:
    """Construction fails, so no request is ever attempted without a key."""
    from forge.models.openrouter import OpenRouterGateway, ProviderCredentialMissing

    with pytest.raises(ProviderCredentialMissing) as caught:
        OpenRouterGateway(environ={})

    # The message names the variable but never a value, because there is none.
    assert "FORGE_OPENROUTER_API_KEY" in str(caught.value)


def test_missing_key_error_does_not_echo_the_environment() -> None:
    """The error names the variable and nothing else.

    Even with a credential-shaped environment present, the message must contain
    no value and no prefix. Asserting the *absence* of specific secrets is the
    only way to catch a future edit that interpolates ``environ`` into the text.
    """
    from forge.models.openrouter import OpenRouterGateway, ProviderCredentialMissing

    # The key is blank, so construction fails -- while other credential-shaped
    # values sit in the same mapping.
    environ = {
        "FORGE_OPENROUTER_API_KEY": "   ",
        "AWS_SECRET_ACCESS_KEY": "aws-SECRET-999",
        "OPENAI_API_KEY": "sk-proj-SECRET-888",
    }

    with pytest.raises(ProviderCredentialMissing) as caught:
        OpenRouterGateway(environ=environ)

    message = str(caught.value)
    assert "SECRET-999" not in message
    assert "SECRET-888" not in message
    assert "sk-proj" not in message
    # It names the variable it wants, and that is the whole message.
    assert "FORGE_OPENROUTER_API_KEY" in message


def test_gateway_repr_never_exposes_the_key() -> None:
    """Formatting the gateway for a log or traceback must not print the key."""
    from forge.models.openrouter import OpenRouterGateway

    key = "sk-or-v1-REPR-SHOULD-NOT-LEAK"
    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": key})

    assert key not in repr(gateway)
    assert key not in str(gateway)
    assert key not in f"{gateway}"
    # Not exposed as an attribute a caller could read by name.
    assert not hasattr(gateway, "api_key")


def test_key_is_never_written_into_a_request(monkeypatch) -> None:
    """The key authenticates the call; it is never part of the payload."""
    from forge.models.openrouter import OpenRouterGateway

    key = "sk-or-v1-DO-NOT-LEAK-THIS"
    monkeypatch.setenv("FORGE_OPENROUTER_API_KEY", key)
    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": key})

    captured: dict[str, Any] = {}

    def fake_post(url, body, headers, timeout):  # type: ignore[no-untyped-def]
        # Body arrives as encoded bytes; decode so the assertion can search it.
        captured["body"] = body.decode("utf-8")
        captured["headers"] = headers
        return 200, '{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'

    monkeypatch.setattr(gateway, "_post", fake_post)

    request = CompletionRequest(system="a system prompt", messages=(), model="m")
    gateway.complete(request)

    # The key is nowhere in the payload -- not in the system prompt, not in the
    # messages, not in any field.
    assert key not in captured["body"]
    assert key not in request.system

    # It is in the Authorization header, which is where it belongs, and nowhere
    # else.
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert key not in json.dumps(
        {k: v for k, v in captured["headers"].items() if k != "Authorization"}
    )


def test_provider_error_body_is_not_echoed(monkeypatch) -> None:
    """A provider error body may echo the request, so it is never surfaced.

    The failing test in the brief: an error body containing a fake secret must
    not reach the operator's terminal or an exception message.
    """
    from forge.models.openrouter import OpenRouterGateway

    key = "sk-or-v1-FAKE-KEY-IN-ERROR"
    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": key})

    body = json.dumps(
        {
            "error": {
                "message": f"invalid key sk-or-v1-FAKE-KEY-IN-ERROR for request",
                "code": 401,
                "type": "auth_error",
            }
        }
    )

    monkeypatch.setattr(
        gateway, "_post", lambda *a, **k: (401, body)
    )

    with pytest.raises(GatewayError) as caught:
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))

    message = str(caught.value)
    assert "FAKE-KEY-IN-ERROR" not in message
    assert "invalid key" not in message
    # What it does report: bounded, operator-safe metadata.
    assert "401" in message
    assert "auth_error" in message


def test_openrouter_top_level_error_envelope_is_provider_error(monkeypatch) -> None:
    """An error envelope must not fall through to the no-choices diagnostic."""
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    body = json.dumps(
        {
            "error": {
                "code": "model_not_found",
                "message": "provider request details must not be exposed",
                "metadata": {"secret": "must-not-leak"},
            },
            "id": "provider-request-id",
        }
    )
    monkeypatch.setattr(gateway, "_post", lambda *a, **k: (200, body))

    with pytest.raises(GatewayError) as caught:
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))

    message = str(caught.value)
    assert "returned an error" in message
    assert "model_not_found" in message
    assert "no choices" not in message
    assert "provider request details" not in message
    assert "must-not-leak" not in message
    assert "provider-request-id" not in message


def test_openrouter_http_error_status_is_not_a_parse_failure(monkeypatch) -> None:
    """A non-JSON HTTP error is reported by status, not as a body parse error."""
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(gateway, "_post", lambda *a, **k: (502, "upstream unavailable"))

    with pytest.raises(GatewayError, match=r"returned HTTP 502"):
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))


def test_openrouter_gateway_satisfies_the_protocol(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    assert isinstance(gateway, ModelGateway)


def test_openrouter_parses_a_text_response(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *a, **k: (
            200,
            json.dumps(
                {
                    "choices": [
                        {"message": {"content": "hello"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }
            ),
        ),
    )

    response = gateway.complete(
        CompletionRequest(system="s", messages=(), model="m")
    )

    assert response.text == "hello"
    assert response.usage.input_tokens == 12
    assert not response.wants_tools


def test_openrouter_parses_responses_style_text_body(monkeypatch) -> None:
    """Some OpenRouter model routes return output blocks, not chat choices."""
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *a, **k: (
            200,
            json.dumps(
                {
                    "id": "resp_123",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "hello from Neighbor"}
                            ],
                        }
                    ],
                }
            ),
        ),
    )

    response = gateway.complete(
        CompletionRequest(system="s", messages=(), model="openai/gpt-5.6-luna")
    )

    assert response.text == "hello from Neighbor"
    assert not response.wants_tools


def test_openrouter_parses_a_tool_call(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *a, **k: (
            200,
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":"a.txt"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        ),
    )

    response = gateway.complete(
        CompletionRequest(system="s", messages=(), model="m")
    )

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {"path": "a.txt"}


def test_openrouter_maps_a_transport_failure(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise OSError("connection reset by peer")

    monkeypatch.setattr(gateway, "_post", boom)

    with pytest.raises(GatewayError, match="could not be reached"):
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))


def test_openrouter_rejects_an_unusable_body(monkeypatch) -> None:
    """A body that is not JSON is a gateway failure, not a crash."""
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(gateway, "_post", lambda *a, **k: (200, "not json at all"))

    with pytest.raises(GatewayError, match="cannot use"):
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))


def test_openrouter_parse_failure_reports_safe_response_structure(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    secret_text = "provider text must never be echoed"
    arbitrary = "provider-id-123"
    body = json.dumps(
        {
            "id": arbitrary,
            "output": [
                {
                    "type": "unexpected_item",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": secret_text},
                        {"type": "tool_use", "id": arbitrary, "name": "read_file"},
                    ],
                }
            ],
            "metadata": "arbitrary metadata",
        }
    )
    monkeypatch.setattr(gateway, "_post", lambda *a, **k: (200, body))

    with pytest.raises(GatewayError) as caught:
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))

    message = str(caught.value)
    assert "no choices" in message
    assert "top_keys=id,metadata,output" in message
    assert "output:array,len=1" in message
    assert "content:array,len=2" in message
    assert "type=unexpected_item" in message
    assert "type=output_text" in message
    assert secret_text not in message
    assert arbitrary not in message
    assert "arbitrary metadata" not in message


def test_openrouter_parse_failure_preserves_non_object_diagnostic_safety() -> None:
    from forge.models.openrouter import OpenRouterGateway

    with pytest.raises(GatewayError) as caught:
        OpenRouterGateway._parse_response("response text with secret")

    message = str(caught.value)
    assert "expected an object" in message
    assert "type=string" in message
    assert "response text with secret" not in message


def test_openrouter_detects_an_oversized_context(monkeypatch) -> None:
    from forge.models.openrouter import OpenRouterGateway

    gateway = OpenRouterGateway(environ={"FORGE_OPENROUTER_API_KEY": "x"})
    monkeypatch.setattr(
        gateway,
        "_post",
        lambda *a, **k: (400, json.dumps({"error": {"code": 400, "message": "context"}})),
    )

    with pytest.raises(ContextTooLarge):
        gateway.complete(CompletionRequest(system="s", messages=(), model="m"))


# --------------------------------------------------------------------------
# Local llama.cpp gateway
# --------------------------------------------------------------------------


def test_local_gateway_parses_native_tool_call() -> None:
    """Native OpenAI-style tool_calls remain the preferred path."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_native",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"README.md"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"read_file"},
    )

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {
        "path": "README.md"
    }


def test_local_gateway_parses_qwen_tool_call_markup() -> None:
    """Qwen <tool_call> JSON is normalised into a Forge ToolCall."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '<tool_call>\n'
                        '{"name":"read_file","arguments":{"path":"README.md"}}\n'
                        '</tool_call>'
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"read_file"},
    )

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {
        "path": "README.md"
    }


def test_local_gateway_parses_qwen_xml_style_tool_call() -> None:
    """Qwen XML-style self-closing tool calls are normalised."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '<search_files pattern="*.md" '
                        'path="/workspace"/>'
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"search_files"},
    )

    assert response.wants_tools
    assert response.tool_calls[0].name == "search_files"
    assert json.loads(response.tool_calls[0].arguments) == {
        "pattern": "*.md",
        "path": "/workspace",
    }


def test_local_gateway_parses_fenced_json_only_for_allowed_tool() -> None:
    """Fenced JSON becomes a tool call only for an offered Forge tool."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '```json\n'
                        '{"name":"read_file",'
                        '"arguments":{"path":"README.md"}}\n'
                        '```'
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"read_file"},
    )

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {
        "path": "README.md"
    }

    blocked = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"search_files"},
    )

    assert not blocked.wants_tools


def test_local_gateway_parses_multiple_calls_in_one_json_fence() -> None:
    """A local model stacks several objects in one fence; all are promoted."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Checking the repo.\n"
                        "```json\n"
                        '{"name":"search_files","arguments":'
                        '{"pattern":"README.md","path":"/repo"}}\n'
                        '{"name":"search_files","arguments":'
                        '{"pattern":"LICENSE","path":"/repo"}}\n'
                        '{"name":"search_files","arguments":'
                        '{"pattern":"*config*","path":"/repo"}}\n'
                        "```\n"
                        "Done."
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"search_files"},
    )

    assert len(response.tool_calls) == 3
    assert [json.loads(call.arguments)["pattern"] for call in response.tool_calls] == [
        "README.md",
        "LICENSE",
        "*config*",
    ]
    assert all(call.name == "search_files" for call in response.tool_calls)
    # Prose outside the fence survives; the fence itself is consumed.
    assert "Checking the repo." in response.text
    assert "```" not in response.text


def test_local_gateway_promotes_only_allowed_names_in_one_fence() -> None:
    """An unoffered name inside an otherwise valid fence is not a tool call."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "```json\n"
                        '{"name":"search_files","arguments":{"pattern":"a"}}\n'
                        '{"name":"delete_everything","arguments":{"path":"/repo"}}\n'
                        '{"name":"read_file","arguments":{"path":"README.md"}}\n'
                        "```"
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"search_files"},
    )

    assert [call.name for call in response.tool_calls] == ["search_files"]
    assert json.loads(response.tool_calls[0].arguments) == {"pattern": "a"}


def test_local_gateway_skips_malformed_object_between_valid_calls() -> None:
    """One bad object does not swallow the valid ones stacked around it."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "```json\n"
                        '{"name":"read_file","arguments":{"path":"a.txt"}}\n'
                        '{"name":"read_file", oops\n'
                        '{"name":"read_file","arguments":{"path":"b.txt"}}\n'
                        "```"
                    )
                },
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"read_file"},
    )

    assert [
        json.loads(call.arguments)["path"] for call in response.tool_calls
    ] == ["a.txt", "b.txt"]


def _bare_response(content: str, allowed: set[str]) -> Any:
    """Parse ``content`` as a local-model reply offering ``allowed`` tools."""
    from forge.models.local import LocalLlamaGateway

    payload = {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ]
    }

    return LocalLlamaGateway._parse_response(payload, allowed_tools=allowed)


def test_local_gateway_promotes_trailing_bare_json_tool_call() -> None:
    """A local model may answer with an unfenced JSON call; trailing prose stays."""
    content = (
        "Let us read the main README file to get an overview.\n\n"
        '{"name": "read_file", "arguments": '
        '{"path": "/home/cybon/Projects/Steel-and-code-forge/README.md"}}'
    )

    response = _bare_response(content, {"read_file"})

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {
        "path": "/home/cybon/Projects/Steel-and-code-forge/README.md"
    }
    assert "Let us read the main README file to get an overview." in response.text
    assert "{" not in response.text


def test_local_gateway_promotes_bare_json_call_as_whole_message() -> None:
    """The call alone, with no prose, is still a call."""
    content = '{"name": "read_file", "arguments": {"path": "README.md"}}'

    response = _bare_response(content, {"read_file"})

    assert response.wants_tools
    assert response.tool_calls[0].name == "read_file"
    assert response.text == ""


def test_local_gateway_rejects_bare_object_with_extra_keys() -> None:
    """Bare promotion is strict: exactly name and arguments, nothing else.

    The fence path tolerates extra keys; the bare path cannot afford to. An
    unfenced object with neighbours is far likelier to be something the model
    was merely *describing* than a request to run something.
    """
    content = '{"name": "read_file", "arguments": {"path": "a.txt"}, "id": "x"}'

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_bare_call_for_unoffered_tool() -> None:
    """An unoffered name is prose even in the exact trailing shape."""
    content = '{"name": "delete_everything", "arguments": {"path": "/repo"}}'

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_bare_call_with_non_dict_arguments() -> None:
    """arguments must be an object; a shell string is not a promotion."""
    content = '{"name": "exec_command", "arguments": "ls -la"}'

    response = _bare_response(content, {"exec_command"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_ignores_json_shaped_object_mid_prose() -> None:
    """Only the trailing segment is inspected; a mid-prose object is text."""
    content = (
        'Here is the config I read: {"database": {"host": "localhost"}}\n'
        "Now I will stop and report."
    )

    response = _bare_response(content, {"search_files"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_bare_call_with_trailing_prose() -> None:
    """A call-shaped object that is not last is not the trailing segment."""
    content = (
        '{"name": "read_file", "arguments": {"path": "README.md"}}\n'
        "Let me know if you want more."
    )

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_leaves_ordinary_trailing_json_as_text() -> None:
    """The real trailing JSON case: JSON that is not a call stays text."""
    content = 'The settings are {"database": {"host": "localhost"}, "retries": 3}'

    response = _bare_response(content, {"search_files"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_leaves_ordinary_json_fence_as_text() -> None:
    """A code fence is not a grant: non-tool JSON stays prose."""
    from forge.models.local import LocalLlamaGateway

    content = (
        "Here is the config I would write.\n"
        "```json\n"
        '{"database": {"host": "localhost"}, "retries": 3}\n'
        "```"
    )

    payload = {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop",
            }
        ]
    }

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"search_files"},
    )

    assert not response.wants_tools
    assert response.text == content.strip()


# --------------------------------------------------------------------------
# Local gateway <tools> block compatibility
# --------------------------------------------------------------------------


def test_local_gateway_promotes_a_call_in_a_tools_block() -> None:
    """One object inside a <tools> wrapper reaches the runtime as a call."""
    content = (
        "Reading the project manifest.\n"
        "<tools>"
        '{"name":"read_file","arguments":{"path":"pyproject.toml"}}'
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert response.wants_tools
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert json.loads(response.tool_calls[0].arguments) == {
        "path": "pyproject.toml"
    }
    assert "Reading the project manifest." in response.text
    assert "<tools>" not in response.text


def test_local_gateway_promotes_every_call_in_a_tools_block() -> None:
    """Every call in one block is promoted, in the order written.

    A block carries one JSON value, so several calls arrive as an array. Objects
    simply stacked side by side are not a JSON value at all and are not
    recovered from -- unlike a fence body, a block is a declared envelope.
    """
    content = (
        "<tools>\n"
        "[\n"
        '  {"name": "search_files", "arguments": {"pattern": "README.md"}},\n'
        '  {"name": "search_files", "arguments": {"pattern": "LICENSE"}},\n'
        '  {"name": "read_file", "arguments": {"path": "README.md"}}\n'
        "]\n"
        "</tools>"
    )

    response = _bare_response(content, {"search_files", "read_file"})

    assert len(response.tool_calls) == 3
    assert [call.name for call in response.tool_calls] == [
        "search_files",
        "search_files",
        "read_file",
    ]
    assert [
        json.loads(call.arguments)["pattern"] for call in response.tool_calls[:2]
    ] == ["README.md", "LICENSE"]
    assert response.text == ""


def test_local_gateway_promotes_the_array_form_in_a_tools_block() -> None:
    """The array form is a recognised call shape, members and all."""
    content = (
        "<tools>\n"
        "[\n"
        '  {"name": "read_file", "arguments": {"path": "a.txt"}},\n'
        '  {"name": "read_file", "arguments": {"path": "b.txt"}}\n'
        "]\n"
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert len(response.tool_calls) == 2
    assert [
        json.loads(call.arguments)["path"] for call in response.tool_calls
    ] == ["a.txt", "b.txt"]
    assert response.text == ""


def test_local_gateway_keeps_prose_around_a_tools_block() -> None:
    """Text before and after the block survives; the block itself is consumed."""
    content = (
        "I will check two files.\n"
        "<tools>"
        '{"name":"read_file","arguments":{"path":"a.txt"}}'
        "</tools>\n"
        "Then I will report what I found."
    )

    response = _bare_response(content, {"read_file"})

    assert len(response.tool_calls) == 1
    assert "I will check two files." in response.text
    assert "Then I will report what I found." in response.text
    assert "<tools>" not in response.text
    assert "read_file" not in response.text


def test_local_gateway_rejects_an_unknown_tool_in_a_tools_block() -> None:
    """An unoffered name is not promoted, and the block is left readable."""
    content = (
        "<tools>"
        '{"name":"delete_everything","arguments":{"path":"/repo"}}'
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_promotes_only_allowed_names_in_a_tools_block() -> None:
    """A good call survives an unoffered neighbour; the unoffered one does not."""
    content = (
        "<tools>"
        "[\n"
        '{"name": "delete_everything", "arguments": {"path": "/repo"}},\n'
        '{"name": "read_file", "arguments": {"path": "a.txt"}}\n'
        "]"
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert [call.name for call in response.tool_calls] == ["read_file"]


def test_local_gateway_leaves_a_malformed_tools_block_as_text() -> None:
    """JSON that does not decode is not scanned for a call that almost fits."""
    content = (
        "Here is what I would run.\n"
        "<tools>\n"
        '{"name": "read_file", oops\n'
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_leaves_a_non_call_json_tools_block_as_text() -> None:
    """Arbitrary JSON in a block is prose, not something to execute."""
    content = (
        "<tools>"
        '{"database": {"host": "localhost"}, "retries": 3}'
        "</tools>"
    )

    response = _bare_response(content, {"search_files"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_a_tools_block_with_non_dict_arguments() -> None:
    """arguments must be an object; a shell string is not a promotion."""
    content = (
        "<tools>"
        '{"name": "exec_command", "arguments": "ls -la"}'
        "</tools>"
    )

    response = _bare_response(content, {"exec_command"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_a_tools_block_missing_arguments() -> None:
    """A block that omits arguments does not get an empty object invented."""
    content = '<tools>{"name": "search_files"}</tools>'

    response = _bare_response(content, {"search_files"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_ignores_a_tool_shaped_object_outside_a_tools_block() -> None:
    """Only block contents are read; prose JSON elsewhere is never promoted."""
    content = (
        "The call would be "
        '{"name":"read_file","arguments":{"path":"a.txt"}}\n'
        "but I did not run it."
    )

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_rejects_objects_stacked_without_an_array() -> None:
    """A block holds one JSON value; stacked objects are not one value."""
    content = (
        "<tools>"
        '{"name":"read_file","arguments":{"path":"a.txt"}}\n'
        '{"name":"read_file","arguments":{"path":"b.txt"}}'
        "</tools>"
    )

    response = _bare_response(content, {"read_file"})

    assert not response.wants_tools
    assert response.text == content


def test_local_gateway_does_not_double_promote_a_tools_block_call() -> None:
    """Native tool_calls win outright; the block adds nothing and stays text."""
    content = "<tools>{\"name\":\"search_files\",\"arguments\":{}}</tools>"

    payload = {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": [
                        {
                            "id": "call_native",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    from forge.models.local import LocalLlamaGateway

    response = LocalLlamaGateway._parse_response(
        payload,
        allowed_tools={"read_file", "search_files"},
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"


def test_local_gateway_does_not_double_promote_after_a_fenced_call() -> None:
    """A fenced call already recognised means the block is never read."""
    content = (
        "```json\n"
        '{"name":"read_file","arguments":{"path":"a.txt"}}\n'
        "```\n"
        "<tools>"
        '{"name":"search_files","arguments":{"pattern":"b"}}'
        "</tools>"
    )

    response = _bare_response(content, {"read_file", "search_files"})

    assert [call.name for call in response.tool_calls] == ["read_file"]
    assert "<tools>" in response.text


# --------------------------------------------------------------------------
# Local gateway timeout configuration
# --------------------------------------------------------------------------


def test_local_timeout_environment_variable_name() -> None:
    """The one supported name, asserted so a rename is deliberate."""
    from forge.models.local import LOCAL_TIMEOUT_ENV

    assert LOCAL_TIMEOUT_ENV == "FORGE_LOCAL_TIMEOUT_SECONDS"


def test_local_gateway_defaults_to_120_seconds() -> None:
    """Unset means the documented default, not zero and not infinite."""
    from forge.models.local import DEFAULT_TIMEOUT_SECONDS, LocalLlamaGateway

    gateway = LocalLlamaGateway(environ={})

    assert DEFAULT_TIMEOUT_SECONDS == 120.0
    assert gateway.timeout_seconds == 120.0


def test_local_gateway_treats_blank_timeout_as_unset() -> None:
    """An empty export behaves like an unset one, not as an error."""
    from forge.models.local import LocalLlamaGateway

    gateway = LocalLlamaGateway(
        environ={"FORGE_LOCAL_TIMEOUT_SECONDS": "   "}
    )

    assert gateway.timeout_seconds == 120.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5.0),
        ("30.5", 30.5),
        ("  45  ", 45.0),
        ("0.25", 0.25),
    ],
)
def test_local_gateway_reads_a_custom_timeout(raw: str, expected: float) -> None:
    """A well-formed override is honoured, whitespace and all."""
    from forge.models.local import LocalLlamaGateway

    gateway = LocalLlamaGateway(environ={"FORGE_LOCAL_TIMEOUT_SECONDS": raw})

    assert gateway.timeout_seconds == expected


def test_local_gateway_explicit_timeout_wins_over_the_environment() -> None:
    """An argument beats the environment, and the environment is not consulted."""
    from forge.models.local import LocalLlamaGateway

    gateway = LocalLlamaGateway(
        timeout=7.0,
        environ={"FORGE_LOCAL_TIMEOUT_SECONDS": "not-a-number"},
    )

    assert gateway.timeout_seconds == 7.0


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "-0.5", "abc", "12s", "1,5", "nan", "inf", "-inf"],
)
def test_local_gateway_rejects_an_invalid_timeout(raw: str) -> None:
    """A bad value fails at construction, naming the variable.

    Left to ``urlopen`` it would surface as an opaque transport error, or for a
    negative value as no timeout at all.
    """
    from forge.models.local import LocalConfigurationError, LocalLlamaGateway

    with pytest.raises(LocalConfigurationError) as caught:
        LocalLlamaGateway(environ={"FORGE_LOCAL_TIMEOUT_SECONDS": raw})

    message = str(caught.value)
    assert "FORGE_LOCAL_TIMEOUT_SECONDS" in message

    # The offending value is not echoed back. Short values are skipped because
    # "0" and "inf" occur inside the variable name and the rule text itself.
    if len(raw) >= 4:
        assert raw not in message


def test_local_configuration_error_is_a_gateway_error() -> None:
    """The CLI already handles GatewayError at construction; this joins it."""
    from forge.models.local import LocalConfigurationError

    assert issubclass(LocalConfigurationError, GatewayError)


def test_local_gateway_error_does_not_echo_the_environment() -> None:
    """The message names the variable, never the surrounding mapping."""
    from forge.models.local import LocalConfigurationError, LocalLlamaGateway

    environ = {
        "FORGE_LOCAL_TIMEOUT_SECONDS": "soon",
        "OPENAI_API_KEY": "«redacted:sk-…»",
        "AWS_SECRET_ACCESS_KEY": "aws-SECRET-999",
    }

    with pytest.raises(LocalConfigurationError) as caught:
        LocalLlamaGateway(environ=environ)

    message = str(caught.value)
    assert "OPENAI_API_KEY" not in message
    assert "AWS_SECRET" not in message
    assert "sk-" not in message


def test_local_gateway_passes_its_timeout_to_the_transport(monkeypatch) -> None:
    """The configured value is what ``urlopen`` is actually given.

    Asserting only ``timeout_seconds`` would leave the wiring untested: the
    attribute could be right while the call site kept a literal.
    """
    import forge.models.local as local_module
    from forge.models.local import LocalLlamaGateway

    seen: dict[str, Any] = {}

    class _Response:
        status = 200

        def read(self) -> bytes:
            return json.dumps(
                {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
            ).encode("utf-8")

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

    def fake_urlopen(request: Any, timeout: Any = None) -> _Response:
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(local_module.urllib.request, "urlopen", fake_urlopen)

    gateway = LocalLlamaGateway(environ={"FORGE_LOCAL_TIMEOUT_SECONDS": "12.5"})
    gateway.complete(CompletionRequest(system="s", messages=(), model="m"))

    assert seen["timeout"] == 12.5


def test_local_gateway_still_needs_no_credential() -> None:
    """The offline path constructs with a bare mapping and no key."""
    from forge.models.local import LocalLlamaGateway

    gateway = LocalLlamaGateway(environ={})

    assert gateway.timeout_seconds == 120.0


# --------------------------------------------------------------------------
# Endpoint configuration
# --------------------------------------------------------------------------


def test_local_gateway_defaults_to_8080() -> None:
    """The two endpoints are the llama.cpp server by default."""
    from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier

    gateway = LocalLlamaGateway(environ={})
    verifier = LocalLlamaVerifier(environ={})

    assert gateway.endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert verifier.endpoint == "http://127.0.0.1:8080/v1/models"


def test_local_endpoint_env_overrides_chat_endpoint() -> None:
    """FORGE_LOCAL_ENDPOINT selects the completions endpoint."""
    from forge.models.local import LocalLlamaGateway

    gateway = LocalLlamaGateway(
        environ={"FORGE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions"}
    )

    assert gateway.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    # The timeout is still default.
    assert gateway.timeout_seconds == 120.0


def test_local_models_endpoint_env_overrides_listing_endpoint() -> None:
    """FORGE_LOCAL_MODELS_ENDPOINT selects the verification endpoint."""
    from forge.models.local import LocalLlamaVerifier

    verifier = LocalLlamaVerifier(
        environ={"FORGE_LOCAL_MODELS_ENDPOINT": "http://127.0.0.1:11434/v1/models"}
    )

    assert verifier.endpoint == "http://127.0.0.1:11434/v1/models"
    assert verifier._timeout == 5.0


def test_explicit_endpoint_wins_over_environment() -> None:
    """A caller passing endpoint=... is stating a choice, not being configured."""
    from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier

    gateway = LocalLlamaGateway(
        endpoint="http://test:9999/v1/chat/completions",
        environ={"FORGE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions"},
    )
    verifier = LocalLlamaVerifier(
        endpoint="http://test:9999/v1/models",
        environ={"FORGE_LOCAL_MODELS_ENDPOINT": "http://127.0.0.1:11434/v1/models"},
    )

    assert gateway.endpoint == "http://test:9999/v1/chat/completions"
    assert verifier.endpoint == "http://test:9999/v1/models"


def test_endpoint_env_blank_is_unset() -> None:
    """A blank value is the same as absent."""
    from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier

    gateway = LocalLlamaGateway(environ={"FORGE_LOCAL_ENDPOINT": "   "})
    verifier = LocalLlamaVerifier(environ={"FORGE_LOCAL_MODELS_ENDPOINT": ""})

    assert gateway.endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert verifier.endpoint == "http://127.0.0.1:8080/v1/models"


def test_endpoint_env_rejects_non_http_scheme() -> None:
    """Only http:// and https:// are allowed; anything else is a configuration error."""
    from forge.models.local import LocalConfigurationError, LocalLlamaGateway

    with pytest.raises(LocalConfigurationError) as caught:
        LocalLlamaGateway(environ={"FORGE_LOCAL_ENDPOINT": "ftp://localhost/v1/chat/completions"})

    assert "FORGE_LOCAL_ENDPOINT" in str(caught.value)
    assert "http:// or https://" in str(caught.value)
    # The message names the variable and the rule, never the value.
    assert "ftp://" not in str(caught.value)


def test_endpoint_env_rejects_malformed_url() -> None:
    """A string that is not a URL at all is a configuration error."""
    from forge.models.local import LocalConfigurationError, LocalLlamaGateway

    with pytest.raises(LocalConfigurationError) as caught:
        LocalLlamaGateway(environ={"FORGE_LOCAL_ENDPOINT": "not-a-url"})

    assert "FORGE_LOCAL_ENDPOINT" in str(caught.value)
    # Either "has no scheme" or "not a parseable URL" is a valid detection.
    reason = str(caught.value)
    assert any(phrase in reason for phrase in ("has no scheme", "not a parseable URL"))


def test_endpoint_env_rejects_url_with_embedded_credentials() -> None:
    """A local endpoint takes no credential, and none may be suggested."""
    from forge.models.local import LocalConfigurationError, LocalLlamaGateway

    with pytest.raises(LocalConfigurationError) as caught:
        LocalLlamaGateway(
            environ={"FORGE_LOCAL_ENDPOINT": "http://user:pass@localhost/v1/chat/completions"}
        )

    assert "FORGE_LOCAL_ENDPOINT" in str(caught.value)
    assert "must not embed credentials" in str(caught.value)
    # The offending username and password are never echoed.
    assert "user" not in str(caught.value)
    assert "pass" not in str(caught.value)


def test_endpoint_and_models_endpoint_are_independent() -> None:
    """Setting one does not affect the other."""
    from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier

    gateway = LocalLlamaGateway(
        environ={"FORGE_LOCAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions"}
    )
    verifier = LocalLlamaVerifier(environ={})

    assert gateway.endpoint == "http://127.0.0.1:11434/v1/chat/completions"
    assert verifier.endpoint == "http://127.0.0.1:8080/v1/models"


def test_gateway_and_verifier_read_same_environment() -> None:
    """Both receive the injected mapping; the verifier factory passes it through."""
    from forge.models.local import LocalLlamaGateway, LocalLlamaVerifier

    env = {"FORGE_LOCAL_MODELS_ENDPOINT": "http://127.0.0.1:11434/v1/models"}
    gateway = LocalLlamaGateway(environ=env)
    verifier = LocalLlamaVerifier(environ=env)

    assert gateway.endpoint == "http://127.0.0.1:8080/v1/chat/completions"
    assert verifier.endpoint == "http://127.0.0.1:11434/v1/models"


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("true", True),
        ("false", False),
        ("", None),
    ],
)
def test_local_gateway_thinking_payload(
    monkeypatch, setting, expected
):
    import json
    import forge.models.local as local_module
    from forge.models.local import LocalLlamaGateway

    captured = {}

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "OK"},
                    "finish_reason": "stop",
                }]
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured.update(json.loads(request.data))
        return FakeResponse()

    monkeypatch.setattr(
        local_module.urllib.request,
        "urlopen",
        fake_urlopen,
    )

    gateway = LocalLlamaGateway(
        environ={"FORGE_LOCAL_THINK": setting}
    )

    gateway.complete(
        CompletionRequest(
            system="test",
            messages=(),
            model="qwen3.5:9b",
        )
    )

    if expected is None:
        assert "think" not in captured
    else:
        assert captured["think"] is expected
