"""Agent profiles: an agent is configuration.

Byte is not a class. It is one :class:`AgentProfile` value, and the runtime that
executes it knows nothing about which agent it is serving. That follows the
decision already recorded in architecture.md -- agents are configuration, not
classes -- and it means a second agent costs a profile, not a subclass.

Two fields carry real weight beyond their obvious meaning:

``allowed_tools``
    Narrowing is structural. The dispatcher refuses any call whose name is not in
    this tuple, so "an agent may be narrower than the baseline" is enforced by a
    membership test rather than documented as a convention. There is no
    corresponding field for *widening*, deliberately: no configuration here can
    grant a permission the Forge baseline withholds.

``instructions``
    The standing system instructions. They contain no tool names, no permission
    rules, and no gateway detail, because each of those already has an owner and
    a second copy would drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["AgentProfile"]


@dataclass(frozen=True)
class AgentProfile:
    """Who an agent is and what it may ask for.

    Attributes:
        name: Identity reported in approval prompts. Displayed to the operator,
            so it must be a true statement -- an agent claiming to be another one
            would make the prompt actively misleading.
        instructions: The standing system instructions, assembled once and reused
            every turn.
        allowed_tools: Names this agent may call. A subset of the dispatcher's
            registry; anything else is refused with ``tool_not_allowed``.
        model: Provider-specific model identifier, or ``None`` to resolve from
            configuration at run time. Byte has no fixed model by decision, so
            the default is genuinely unset rather than a placeholder that looks
            like a choice.
        max_tool_turns: Per-run ceiling on model round-trips. Always
            configurable downward; never upward past the module constant in
            ``forge/agents/runtime.py``.
        max_output_tokens: Bound passed to the gateway on every request.
    """

    name: str
    instructions: str
    allowed_tools: tuple[str, ...] = ()
    model: str | None = None
    max_tool_turns: int | None = None
    max_output_tokens: int = 4096
    metadata: dict[str, str] = field(default_factory=dict)

    def allows(self, tool_name: str) -> bool:
        """Whether this agent may call ``tool_name``.

        One membership test, called on every tool request. It is the whole
        enforcement mechanism for per-agent narrowing.
        """
        return tool_name in self.allowed_tools
