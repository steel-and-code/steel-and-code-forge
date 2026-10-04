"""The agent registry: name to profile.

Forge has more than one agent, and a caller that has to know that in order to
pick one -- ``if name == "byte"`` in a CLI dispatch, an import per agent in a
surface -- is a caller that has to be edited again for the next agent. This
module is the single place where a name becomes a profile.

Three properties, and each is a decision rather than a convenience:

**The mapping is explicit, not discovered.** There is no entry-point scan, no
directory walk, no import-by-convention. A registry that finds its own
members is a registry whose contents depend on what is installed and what
imported first, and an agent that appears because something happened to be
imported is an agent nobody chose. Adding an agent is an edit to ``AGENTS``.

**Lookup failure is a value, not an exception.** :func:`get_agent` returns
``None`` for an unknown name so a CLI can print the names that do exist. An
exception here would make "operator typo" and "Forge is broken" the same
failure, and the second one is worth noticing.

**The registry holds profiles only.** It carries no model, no tier, and no
routing intent. Those belong to ``forge.models`` and to the surface that
knows what the agent is *for*; see ``forge/cli/main.py`` for why a task kind
is a statement about an agent's purpose rather than a property of its
configuration.

The one thing registered alongside a profile is its prompt assembler, and it
is here rather than imported per-agent because each agent module owns its own
copy on purpose -- see :func:`system_prompt_for`.

Nothing here imports a gateway, a dispatcher, or a classifier. The registry is
configuration, and it stays as inert as the profiles it contains.
"""

from __future__ import annotations

from collections.abc import Callable

from .byte import BYTE, assemble_system_prompt as _byte_system_prompt
from .neighbor import NEIGHBOR, assemble_system_prompt as _neighbor_system_prompt
from .profile import AgentProfile

__all__ = ["AGENTS", "agent_names", "get_agent", "system_prompt_for"]


#: Every agent Forge can run, keyed by the name used on the command line.
#: Insertion-ordered, because ``agent_names`` reports it and the order an
#: operator is offered is the order they read best in.
AGENTS: dict[str, AgentProfile] = {
    BYTE.name: BYTE,
    NEIGHBOR.name: NEIGHBOR,
}


#: Each profile's prompt assembler, keyed the same way.
#:
#: Every agent module defines its own ``assemble_system_prompt``, and those are
#: deliberately duplicate functions rather than one shared helper: the
#: assembler is the seam that turns *a profile's* instructions into a prompt, so
#: a single shared implementation would make Neighbor's prompt depend on
#: Byte's module, and deleting Byte would take Neighbor's prompt with it. Two
#: small functions that cannot interfere are cheaper than a shared helper
#: coupling two agents that share no contract.
#:
#: This mapping exists so a caller that was handed a *name* can still reach the
#: right assembler without importing every agent module. A caller that already
#: holds a profile should use :func:`system_prompt_for`, which looks the
#: assembler up the same way.
_ASSEMBLERS: dict[str, Callable[[AgentProfile, str], str]] = {
    BYTE.name: _byte_system_prompt,
    NEIGHBOR.name: _neighbor_system_prompt,
}


def agent_names() -> tuple[str, ...]:
    """Every registered agent name, in registration order."""
    return tuple(AGENTS)


def get_agent(name: str) -> AgentProfile | None:
    """The profile registered under ``name``, or ``None`` if there is none.

    ``None`` rather than a raise: an unknown name is almost always an
    operator typo, and the useful response is to list what does exist.
    """
    return AGENTS.get(name)


def system_prompt_for(profile: AgentProfile, workspace_root: str) -> str:
    """Assemble ``profile``'s system prompt using that agent's own assembler.

    Raises:
        KeyError: if ``profile`` is not a registered agent. Deliberate rather
            than a silent fallback: a caller running an agent Forge does not
            know about is a composition mistake, and quietly substituting
            another agent's assembler would produce a prompt whose instructions
            do not match the ``agent_name`` reported to the approval layer --
            an approval prompt attributing a request to the wrong agent.
    """
    assembler = _ASSEMBLERS[profile.name]
    return assembler(profile, workspace_root)