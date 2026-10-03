"""The provider-neutral model boundary.

``base`` holds the protocol and its value types. ``scripted`` is the offline
implementation every test uses; ``openrouter`` is the live one. No module in
this package may import a classifier or hold permission policy: the model decides
what it wants to attempt, and Forge decides whether it is allowed, one layer down.
"""

from .base import (
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
from .scripted import ScriptedGateway, ScriptedTurn

__all__ = [
    "CompletionRequest",
    "ContextTooLarge",
    "GatewayError",
    "Message",
    "ModelGateway",
    "ModelResponse",
    "ScriptedGateway",
    "ScriptedTurn",
    "ToolCall",
    "ToolSpec",
    "Usage",
]
