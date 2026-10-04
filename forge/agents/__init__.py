"""Agent profiles, the registry that names them, and the runtime that serves them.

Byte is a profile in ``byte.py``; Neighbor is a profile in ``neighbor.py``;
``registry.py`` is the one place a name becomes a profile; and the loop that
serves any profile is in ``runtime.py``. No module here imports a classifier, an
``Approver``, or an ``Executor`` -- every permission decision belongs to
``forge.tools.dispatcher`` -- and ``tests/test_agent_runtime.py`` asserts that
structurally rather than trusting it.
"""

from .profile import AgentProfile
from .registry import AGENTS, agent_names, get_agent, system_prompt_for
from .runtime import (
    MAX_TOOL_TURNS,
    AgentRuntime,
    MaxTurnsExceeded,
    RunResult,
    summarise,
)
from .state import ConversationState, DenialRecord

__all__ = [
    "AGENTS",
    "AgentProfile",
    "AgentRuntime",
    "ConversationState",
    "DenialRecord",
    "MaxTurnsExceeded",
    "RunResult",
    "MAX_TOOL_TURNS",
    "agent_names",
    "get_agent",
    "summarise",
    "system_prompt_for",
]
