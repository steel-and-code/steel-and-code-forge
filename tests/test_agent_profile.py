"""Neighbor: the second agent, and the registry that names it.

Byte's tests live in ``test_agent_runtime.py`` and this file does not repeat
them. What is here is what is *new* about a second agent, plus the two
invariants that only become checkable once there is more than one:

* the contract-to-constant sync, for Neighbor's seven blocks;
* that Neighbor is structurally narrower-or-equal and never wider;
* that the safety stack is genuinely shared rather than merely similar;
* that Byte is untouched by any of it.

The last one is the test most worth having. "Byte behaviour is unchanged" is
the claim this change is most able to break silently, because almost every edit
here touched the code path Byte runs on -- ``_run_byte`` was refactored into
``_run_agent``. A claim that is only true by inspection is not a claim.
"""

from __future__ import annotations

import pathlib

import pytest

from forge.agents.byte import BLOCKS as BYTE_BLOCKS
from forge.agents.byte import BYTE
from forge.agents.neighbor import BLOCKS as NEIGHBOR_BLOCKS
from forge.agents.neighbor import (
    NEIGHBOR,
    NEIGHBOR_TOOLS,
    assemble_system_prompt as neighbor_prompt,
)
from forge.agents.profile import AgentProfile
from forge.agents.registry import AGENTS, agent_names, get_agent, system_prompt_for
from forge.agents.runtime import AgentRuntime
from forge.events import Event
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import Approver
from forge.tools.dispatcher import ToolDispatcher, ToolStatus
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace

from .test_agent_runtime import (
    CONTRACT_PATH,
    contract_section,
    normalise,
    parse_contract_blocks,
)


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------


def test_neighbor_is_a_profile_not_a_class() -> None:
    """Agents are configuration. A second agent changes nothing about that."""
    assert isinstance(NEIGHBOR, AgentProfile)
    assert NEIGHBOR.name == "neighbor"


def test_neighbor_has_no_fixed_model() -> None:
    """The settled decision, and the one this change had most chance of breaking.

    Neighbor could trivially have been given a model to make it "work", and a
    model id here would be a routing decision smuggled into configuration --
    exactly what the registry exists to avoid. Byte's own test asserts the same
    for Byte; this asserts the new agent did not quietly diverge.
    """
    assert NEIGHBOR.model is None
    assert BYTE.model is None


def test_neighbor_carries_no_task_kind() -> None:
    """A task kind describes what a *caller* believes an agent is for.

    If it were a profile field, an agent would be asserting its own purpose and
    the CLI could not route the same profile differently for a different
    surface. Asserted structurally over the dataclass fields so adding the
    field later fails here rather than passing quietly.
    """
    fields = frozenset(AgentProfile.__dataclass_fields__)
    assert "task_kind" not in fields
    assert "tier" not in fields
    assert "difficulty" not in fields
    # And the one model-bearing field is the one that already existed.
    assert fields == {
        "name",
        "instructions",
        "allowed_tools",
        "model",
        "max_tool_turns",
        "max_output_tokens",
        "metadata",
    }


def test_neighbor_takes_the_baseline_tool_set() -> None:
    """Six tools, the Forge-wide baseline, and the same six Byte has."""
    assert NEIGHBOR_TOOLS == BYTE.allowed_tools
    assert set(NEIGHBOR_TOOLS) == {
        "read_file",
        "search_files",
        "search_content",
        "write_file",
        "patch_file",
        "exec_command",
    }


def test_neighbor_is_not_wider_than_the_baseline() -> None:
    """Widening is structurally impossible; this pins the direction.

    The contract permits an agent to be narrower and provides no way to be
    wider. That is enforced by the dispatcher refusing any name not in the
    tuple, so the property to assert is that Neighbor's set is a subset of
    Byte's -- equal today, and the assertion still holds if Neighbor is later
    narrowed.
    """
    assert set(NEIGHBOR.allowed_tools) <= set(BYTE.allowed_tools)


def test_neighbor_allows_nothing_byte_does_not() -> None:
    """``allows`` is the whole enforcement mechanism, so check it per tool."""
    for tool in BYTE.allowed_tools:
        assert NEIGHBOR.allows(tool)
    assert not NEIGHBOR.allows("some_tool_that_does_not_exist")


# --------------------------------------------------------------------------
# Instructions and the contract sync
# --------------------------------------------------------------------------


def test_neighbor_has_seven_blocks_in_order() -> None:
    """Same shape as Byte: seven blocks, identity first, boundaries third."""
    assert len(NEIGHBOR_BLOCKS) == 7
    joined = NEIGHBOR.instructions
    for index, block in enumerate(NEIGHBOR_BLOCKS):
        assert block in joined, f"block {index} missing from the profile"
    assert joined.index("You are Neighbor") < joined.index("Your job is")
    assert joined.index("Your job is") < joined.index("You operate inside")


def test_neighbor_blocks_are_verbatim_copies_of_the_contract() -> None:
    """Neighbor's runtime blocks are the contract's quoted text, wording included.

    The same guarantee Byte has. Without it the contract would be prose nobody
    reads and a prompt nobody reviewed.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"), "Neighbor")
    runtime = {
        letter: normalise(block) for letter, block in zip("ABCDEFG", NEIGHBOR_BLOCKS)
    }

    assert sorted(contract) == list("ABCDEFG"), f"found: {sorted(contract)}"
    for letter, expected in contract.items():
        actual = runtime[letter]
        assert actual == expected, (
            f"Neighbor Block {letter} has drifted from {CONTRACT_PATH.name}.\n"
            f"contract: {expected!r}\nruntime:  {actual!r}"
        )


def test_byte_still_reads_the_byte_section_not_the_neighbor_one() -> None:
    """The regression this change could most easily have introduced.

    ``parse_contract_blocks`` keys by block letter, and the contract now holds
    two sets of seven. A document-wide scan pairs *whichever section came last*
    with Byte's constants -- which fails loudly here, and would fail silently
    if the two agents' shared blocks ever happened to agree.
    """
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    byte_blocks = parse_contract_blocks(text, "Byte")
    neighbor_blocks = parse_contract_blocks(text, "Neighbor")

    assert normalise(BYTE_BLOCKS[1]) == byte_blocks["B"]
    assert normalise(NEIGHBOR_BLOCKS[1]) == neighbor_blocks["B"]
    # Byte's Block B and Neighbor's are genuinely different text, so an
    # unscoped parser cannot pass this by accident -- and the two agents share
    # Block D verbatim, which is precisely why the scoping is asserted rather
    # than assumed to be doing work.
    assert byte_blocks["B"] != neighbor_blocks["B"]
    assert byte_blocks["D"] == neighbor_blocks["D"]


def test_contract_section_rejects_an_agent_the_contract_does_not_document() -> None:
    """A missing section raises instead of returning an empty slice.

    An empty slice would make all seven block comparisons vacuously pass, which
    is the failure mode this whole parser exists to prevent. The name used here
    is one no section claims -- *not* Ember, which is documented as a
    placeholder and so does resolve.
    """
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="no '## Agent: Nobody' section"):
        contract_section(text, "Nobody")
    # A documented placeholder resolves, and carries no blocks -- so parsing it
    # yields nothing rather than raising. That is the honest result: Ember has
    # no instruction text to compare against.
    assert "TBD" in contract_section(text, "Ember")
    assert parse_contract_blocks(text, "Ember", require_blocks=False) == {}


def test_neighbor_prompt_states_the_workspace_and_no_classifier_rules() -> None:
    """The environment section is Byte's, and neither prompt restates policy."""
    prompt = neighbor_prompt(NEIGHBOR, "/tmp/example")

    assert "/tmp/example" in prompt
    assert "You are Neighbor" in prompt
    for banned in ("sudo", "force push", "denied commands", "approval_required"):
        assert banned not in prompt, f"prompt restates classifier detail: {banned}"


def test_the_two_prompt_assemblers_agree_on_shape() -> None:
    """Two duplicate functions must not become two behaviours.

    ``neighbor.py`` and ``byte.py`` each define ``assemble_system_prompt`` on
    purpose -- one shared helper would make Neighbor's prompt depend on Byte's
    module. That is only safe while they agree, so the agreement is asserted.
    """
    byte_prompt = system_prompt_for(BYTE, "/tmp/example")
    neighbor_system = neighbor_prompt(NEIGHBOR, "/tmp/example")

    for text in (byte_prompt, neighbor_system):
        assert "/tmp/example" in text
        assert "You are operating in this workspace:" in text
        assert "schema is an error, not a request for permission" in text


def test_the_registry_routes_each_profile_to_its_own_assembler() -> None:
    """A name reaches the right prompt, and the wrong agent is not substituted."""
    for name, profile in AGENTS.items():
        prompt = system_prompt_for(profile, "/tmp/example")
        assert f"You are {name.capitalize()}" in prompt


def test_system_prompt_for_refuses_an_unregistered_agent() -> None:
    """A KeyError, not a fallback.

    Falling back to some other agent's assembler would produce a prompt whose
    instructions contradict the ``agent_name`` reported to the approval layer --
    an approval prompt attributing a request to the wrong agent, which is the
    exact failure the profile docstring says an identity field must prevent.
    """
    stranger = AgentProfile(name="stranger", instructions="hello", allowed_tools=())
    with pytest.raises(KeyError):
        system_prompt_for(stranger, "/tmp/example")


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_both_agents_are_registered() -> None:
    assert agent_names() == ("byte", "neighbor")
    assert AGENTS["byte"] is BYTE
    assert AGENTS["neighbor"] is NEIGHBOR


def test_the_registry_is_keyed_by_each_profiles_own_name() -> None:
    """A hand-typed key that disagrees with ``profile.name`` is a real bug.

    It would register an agent under one name while every approval prompt,
    event, and runtime read reported another.
    """
    for key, profile in AGENTS.items():
        assert key == profile.name


def test_an_unknown_name_is_none_rather_than_an_exception() -> None:
    """An operator typo should be able to list what does exist."""
    assert get_agent("nope") is None
    assert get_agent("Byte") is None  # case-sensitive on purpose
    assert get_agent("byte") is BYTE


def test_the_registry_has_no_entry_point_or_discovery_mechanism() -> None:
    """Explicit membership, asserted structurally.

    A registry that discovers its own agents is a registry whose contents depend
    on what happened to be imported. The docstring claims there is no scan; this
    is what stops that claim decaying into an import of ``pkgutil``.
    """
    import forge.agents.registry as registry_module

    source = pathlib.Path(registry_module.__file__ or "").read_text(encoding="utf-8")

    for banned in (
        "pkgutil",
        "importlib",
        "entry_point",
        "iter_modules",
        "__subclasses__",
    ):
        assert banned not in source, f"registry grew a discovery mechanism: {banned}"


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


def _dispatcher_for(
    profile: AgentProfile, root: pathlib.Path, *, registered: bool = True
) -> ToolDispatcher:
    """The real safety stack, wired for one profile.

    The real ``ToolDispatcher`` rather than a stand-in: the claim under test is
    that Neighbor reaches the *same* pipeline Byte does, and a substitute
    dispatcher would make that vacuous. ``Approver(None)`` means unattended, so
    anything approval-required fails closed -- correct for a run that makes no
    tool call, and it keeps the test from needing a TTY.

    ``registered=False`` builds the prompt directly instead of through
    :func:`system_prompt_for`. That function raises for an unregistered profile
    *on purpose* -- see its docstring -- so a test for a deliberately ad-hoc
    profile has to supply its own prompt rather than quietly registering a
    throwaway agent.
    """
    prompt = (
        system_prompt_for(profile, str(root))
        if registered
        else f"{profile.instructions}\n\n---\nYou are operating in this workspace:\n{root}"
    )
    return ToolDispatcher(
        Workspace(root),
        approver=Approver(None),
        executor=Executor(Workspace(root)),
        agent_name=profile.name,
        allowed_tools=profile.allowed_tools,
        system_prompt=prompt,
    )


def test_neighbor_runs_through_the_unchanged_runtime() -> None:
    """The runtime never learns which agent it is serving.

    This is the architectural claim the whole change rests on, so it is
    exercised rather than asserted: a Neighbor profile, a scripted gateway, and
    the same ``AgentRuntime`` class, with no branching on the agent's name.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    runtime = AgentRuntime(
        NEIGHBOR,
        gateway=ScriptedGateway([ScriptedTurn(text="All done.")]),
        dispatcher=_dispatcher_for(NEIGHBOR, root),
        model_id="scripted",
    )

    result = runtime.run("what does this project do?")

    assert result.text == "All done."
    assert result.turns == 1


def test_the_runtime_reports_the_real_agent_name_and_no_other() -> None:
    """Observability must not attribute a Neighbor run to Byte.

    ``run.started`` reports ``profile.name`` with no agent-specific branch, so
    this is really a check that the profile carried its own name through --
    which is the property that keeps an approval prompt honest.
    """
    seen: list[Event] = []

    class _Sink:
        def emit(self, event: Event) -> None:
            seen.append(event)

    root = pathlib.Path(__file__).resolve().parent.parent
    runtime = AgentRuntime(
        NEIGHBOR,
        gateway=ScriptedGateway([ScriptedTurn(text="ok")]),
        dispatcher=_dispatcher_for(NEIGHBOR, root),
        model_id="scripted",
        events=_Sink(),
    )
    runtime.run("hello")

    started = [event for event in seen if event.name == "run.started"]
    assert started, "no run.started event was emitted"
    assert started[0].get("agent") == "neighbor"
    assert "byte" not in {event.get("agent") for event in seen}


def test_neighbor_is_refused_a_tool_outside_its_own_set() -> None:
    """Per-agent narrowing is a membership test, and it holds for Neighbor too.

    This is the one permission behaviour a second agent could plausibly have
    been given its own bypass for. It is checked through the dispatcher rather
    than through ``allows`` alone, because the dispatcher's refusal is what
    actually reaches the model.
    """
    from forge.models.base import ToolCall

    root = pathlib.Path(__file__).resolve().parent.parent
    dispatcher = _dispatcher_for(NEIGHBOR, root)

    result = dispatcher.dispatch(
        ToolCall(id="call_1", name="not_a_real_tool", arguments="{}")
    )

    # A name no agent holds is UNKNOWN_TOOL, not TOOL_NOT_ALLOWED: Forge has
    # never heard of it, so there is no per-agent permission question to ask.
    assert result.status is ToolStatus.UNKNOWN_TOOL
    assert not result.status.is_permission_refusal


def test_a_narrower_profile_is_refused_a_tool_byte_may_call() -> None:
    """The narrowing direction, exercised rather than described.

    A profile with a strict subset must lose access even to a tool Byte holds.
    This is what "an agent may be narrower" means operationally, and it is the
    mechanism a future agent would rely on to be safe by default.
    """
    from forge.models.base import ToolCall

    root = pathlib.Path(__file__).resolve().parent.parent
    narrow = AgentProfile(
        name="narrow",
        instructions="read only",
        allowed_tools=("read_file",),
    )
    dispatcher = _dispatcher_for(narrow, root, registered=False)

    refused = dispatcher.dispatch(
        ToolCall(id="call_1", name="exec_command", arguments='{"argv": ["ls"]}')
    )

    # The subset agent keeps the tool it was granted...
    assert narrow.allows("read_file")
    # ...and loses the one it was not, even though Byte holds it.
    assert not narrow.allows("exec_command")
    assert "exec_command" in BYTE.allowed_tools
    assert refused.status is ToolStatus.TOOL_NOT_ALLOWED
    # Deliberately *not* a cached permission refusal: the dispatcher caches only
    # DENIED / REJECTED / DENIED_UNATTENDED, because a tool this agent may never
    # call is a configuration fact rather than a decision a human made. Asserted
    # so a future edit does not quietly reclassify it.
    assert not refused.status.is_permission_refusal
    assert "narrow" in refused.detail


# --------------------------------------------------------------------------
# Byte is unchanged
# --------------------------------------------------------------------------


def test_byte_is_unchanged_in_the_registry() -> None:
    """Byte is still the same object, under the same key, with the same tools."""
    assert AGENTS["byte"] is BYTE
    assert BYTE.name == "byte"
    assert BYTE.allowed_tools == NEIGHBOR_TOOLS
    assert BYTE.model is None


def test_byte_blocks_are_untouched_by_the_neighbor_section() -> None:
    """Byte's seven constants still match Byte's contract section exactly."""
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"), "Byte")
    runtime = {letter: normalise(block) for letter, block in zip("ABCDEFG", _BYTE_BLOCKS)}

    for letter, expected in contract.items():
        assert runtime[letter] == expected, f"Byte Block {letter} drifted"


from forge.agents.byte import BLOCKS as _BYTE_BLOCKS  # noqa: E402 - avoids shadowing