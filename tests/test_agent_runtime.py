"""Tests for the agent runtime: the tool-call loop, and what it must not hold.

Two groups. The first is behaviour -- the loop, the turn accounting, the error
paths. The second is **structural**: assertions about which modules import what,
which is how the central design property is kept from decaying.

That structural group exists because the property is invisible to behavioural
tests. A runtime that gained a classifier reference would still pass every loop
test in the first group; only an import scan notices. The property is that the
runtime holds no opinion about permissions -- ``AgentRuntime`` must never import
``CommandClassifier``, ``Approver``, or ``Executor``, and
``forge/tools/dispatcher.py`` must be the only runtime path to
``forge/tools/execution.py``.

The gateway is always :class:`ScriptedGateway`. No test in this file makes a
network call, needs a credential, or costs money.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from forge.agents import runtime as runtime_module
from forge.agents.byte import BLOCKS, BYTE, assemble_system_prompt
from forge.agents.profile import AgentProfile
from forge.agents.runtime import (
    MAX_TOOL_TURNS,
    AgentRuntime,
    MaxTurnsExceeded,
)
from forge.models.base import GatewayError
from forge.models.scripted import ScriptedGateway, ScriptedTurn
from forge.tools.approval import Approver, ScriptedApprovalChannel
from forge.tools.dispatcher import ToolDispatcher, ToolStatus
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def dispatcher_factory(workspace: Workspace):
    """Build a dispatcher with a scripted channel.

    The channel is returned too, so a test can count how many prompts were
    actually shown -- which is how the denial-cache tests prove a second prompt
    did not happen.
    """

    def build(responses: list[str | None] | None = None, *, agent: str = "byte"):
        channel = ScriptedApprovalChannel(responses if responses is not None else [])
        dispatcher = ToolDispatcher(
            workspace,
            approver=Approver(channel),
            executor=Executor(workspace),
            agent_name=agent,
            allowed_tools=BYTE.allowed_tools,
            system_prompt=assemble_system_prompt(BYTE, str(workspace.root)),
        )
        return dispatcher, channel

    return build


def _runtime(gateway: ScriptedGateway, dispatcher: ToolDispatcher, **kw) -> AgentRuntime:
    return AgentRuntime(
        BYTE, gateway=gateway, dispatcher=dispatcher, model_id="scripted", **kw
    )


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def test_text_only_response_ends_the_run(dispatcher_factory) -> None:
    """A model that answers immediately finishes after one turn."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway([ScriptedTurn(text="Nothing to do.")])

    result = _runtime(gateway, dispatcher).run("status?")

    assert result.text == "Nothing to do."
    assert result.turns == 1
    assert result.tool_calls == 0


def test_tool_call_dispatches_appends_and_continues(dispatcher_factory, workspace) -> None:
    """A tool call is dispatched, its result appended, and the loop continues."""
    (workspace.root / "notes.txt").write_text("hello\n")
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "notes.txt"}),)),
            ScriptedTurn(text="The file says hello."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("what does notes.txt say?")

    assert result.text == "The file says hello."
    assert result.turns == 2
    assert result.tool_calls == 1
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0].content


def test_multiple_tool_calls_in_one_turn_count_as_one_turn(dispatcher_factory) -> None:
    """Turns are model round-trips, not tool calls.

    A response carrying five calls costs one turn: the round-trip is what costs
    tokens, and a model that batches well should not be penalised for it.
    """
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("read_file", {"path": "a.txt"}),
                    ("read_file", {"path": "b.txt"}),
                    ("read_file", {"path": "c.txt"}),
                )
            ),
            ScriptedTurn(text="done"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read three files")

    assert result.turns == 2
    assert result.tool_calls == 3


def test_max_turn_limit_raises(dispatcher_factory) -> None:
    """Exhausting the budget raises rather than returning a truncated success."""
    dispatcher, _ = dispatcher_factory()
    # One turn, repeated forever: the model never finishes.
    gateway = ScriptedGateway([ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),))])

    with pytest.raises(MaxTurnsExceeded) as caught:
        _runtime(gateway, dispatcher, max_turns=3).run("loop forever")

    assert caught.value.turns == 3
    assert "3 permitted turns" in str(caught.value)


def test_max_turns_is_configurable_downward_only(dispatcher_factory) -> None:
    """A caller may lower the ceiling but never raise it past the constant."""
    dispatcher, _ = dispatcher_factory()

    lowered = _runtime(ScriptedGateway([]), dispatcher, max_turns=5)
    assert lowered.max_turns == 5

    attempted = _runtime(ScriptedGateway([]), dispatcher, max_turns=10_000)
    assert attempted.max_turns == MAX_TOOL_TURNS


def test_gateway_failure_propagates_rather_than_ending_the_run(dispatcher_factory) -> None:
    """A provider failure raises. It is not the model's to recover from."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [ScriptedTurn(raises=GatewayError("provider unreachable"))]
    )

    with pytest.raises(GatewayError):
        _runtime(gateway, dispatcher).run("anything")


def test_system_prompt_carries_the_workspace_and_no_classifier_rules(
    dispatcher_factory, workspace
) -> None:
    """The prompt states the workspace; it does not restate the safety layer."""
    dispatcher, _ = dispatcher_factory()
    prompt = dispatcher.system_prompt

    assert str(workspace.root) in prompt
    # Identity blocks present.
    assert "You are Byte" in prompt
    # No restatement of classifier rules: these would be a second, weaker copy
    # of the safety layer that drifts on every future improvement.
    for banned in ("sudo", "force push", "denied commands", "approval_required"):
        assert banned not in prompt, f"prompt restates classifier detail: {banned}"


def test_byte_instructions_match_the_contract_blocks() -> None:
    """All seven standing instruction blocks are present, in order."""
    assert len(BLOCKS) == 7
    joined = BYTE.instructions
    for index, block in enumerate(BLOCKS):
        assert block in joined, f"block {index} missing from the profile"

    assert joined.index("You are Byte") < joined.index("Investigate before")
    assert joined.index("Investigate before") < joined.index("You operate inside")


#: The quoted blocks in the contract, in document order.
CONTRACT_PATH = pathlib.Path(__file__).resolve().parent.parent / "docs" / "agent-contract.md"

#: ``#### Block B -- Responsibilities`` and friends. The letter is captured so
#: a renamed or reordered block fails rather than silently re-pairing.
BLOCK_HEADING = re.compile(r"^#### Block ([A-G])\b")

#: Dashes that mean the same punctuation in both representations. The contract
#: is prose and uses a real em dash; ``byte.py`` is source and uses ``--``
#: because the em dash is not ASCII and the module is read by people as well as
#: imported. This is the only wording difference the two copies are allowed.
DASHES = str.maketrans({"—": "--", "–": "--", "―": "--"})


def parse_contract_blocks(text: str) -> dict[str, str]:
    """Pull the quoted Block A-G text out of the contract, keyed by letter.

    The contract holds two kinds of prose per block: the block itself, in a
    Markdown blockquote, and commentary underneath explaining why the block
    says what it says. Only the blockquote is the instruction, so parsing stops
    at the first line that is not a quote. This is what lets the test compare
    against the contract instead of holding a second copy of the wording --
    a second copy is precisely the thing that drifts.
    """
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    after_break = False

    for line in text.splitlines():
        heading = BLOCK_HEADING.match(line)
        if heading:
            letter = heading.group(1)
            assert letter is not None
            current = letter
            blocks[letter] = []
            continue
        if current is None:
            continue
        if not line.strip():
            # A blank line between the heading and the quote. Markdown needs it;
            # it does not end the block.
            continue
        if not line.startswith(">"):
            # Prose between blocks. The block ended.
            current = None
            continue
        quoted = line[1:].strip()
        if not quoted:
            # A bare ``>``: a paragraph break. Recorded now and dropped if the
            # quote ends here, so a trailing break cannot invent a paragraph.
            after_break = True
            continue
        if blocks[current] and after_break:
            blocks[current].append("")
        after_break = False
        blocks[current].append(quoted)

    assert sorted(blocks) == list("ABCDEFG"), f"contract blocks found: {sorted(blocks)}"
    # One entry per line, with an empty string standing in for a paragraph
    # break, so joining with a single newline yields the paragraph structure
    # ``normalise`` then re-reads.
    return {letter: normalise("\n".join(paragraphs))
            for letter, paragraphs in blocks.items()}


def normalise(block: str) -> str:
    """Reduce a block to comparable form, touching formatting only.

    Whitespace inside a paragraph is collapsed and dash punctuation is unified.
    Nothing else is touched: case, punctuation, word choice, word order, and
    paragraph structure all survive, so substantive wording drift still fails
    the comparison. Collapsing whitespace is safe because neither file has
    meaningful indentation or alignment in a block.
    """
    paragraphs = [" ".join(p.split()) for p in block.split("\n\n")]
    return "\n\n".join(paragraphs).translate(DASHES).strip()


def test_byte_blocks_are_verbatim_copies_of_the_contract() -> None:
    """Each runtime block is the contract's quoted text, wording included.

    ``test_byte_instructions_match_the_contract_blocks`` above only proves the
    blocks are internally consistent: it compares the constants to the profile
    they are joined into, so all seven could drift from the contract together
    and it would still pass. This is the test that reads the contract.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))
    runtime = {letter: normalise(block) for letter, block in zip("ABCDEFG", BLOCKS)}

    for letter, expected in contract.items():
        actual = runtime[letter]
        assert actual == expected, (
            f"Block {letter} has drifted from {CONTRACT_PATH.name}.\n"
            f"contract: {expected!r}\nruntime:  {actual!r}"
        )


def test_the_contract_comparison_would_catch_wording_drift() -> None:
    """The comparison is strict enough to be worth having.

    A synchronisation test that normalises away too much is a test that always
    passes, which is worse than no test: it certifies a guarantee it never
    checked. This asserts the check itself -- a changed word, a changed
    paragraph break, and a dropped sentence each have to be visible.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))

    baseline = contract["B"]

    # A substituted word must fail.
    assert normalise(baseline.replace("summarize", "paraphrase")) != normalise(baseline)
    # So must a dropped clause -- the failure this whole test exists for.
    assert normalise(baseline.replace("; do not stop", "")) != normalise(baseline)
    # So must a collapsed paragraph break, which changes what the block claims
    # to be one commitment versus two.
    assert normalise(baseline.replace("\n\n", " ")) != normalise(baseline)
    # And a case change.
    assert normalise(baseline).lower() != normalise(baseline)


#: The paragraph index of Block D's denial rule. Block D gained a recoverable-
#: error rule, and the ordering is load-bearing: the model must read a malformed
#: call as a fixable mistake *before* it reads the denial rule, or the denial
#: paragraph reads as the general answer to every non-ok tool result.
BLOCK_D_DENIAL_PHRASE = "If a tool call comes back denied, that is the answer."


def test_invalid_arguments_is_instructed_as_a_recoverable_error() -> None:
    """A malformed call is a fixable mistake, and Block D says so.

    The dispatcher already returns ``invalid_arguments`` and already declines to
    cache it, so the runtime invites a retry. If the standing instructions do not
    say what to do with it, the only rule the model has been given about a
    non-ok tool result is Block D's denial rule, and the correct behaviour --
    read the error, fix the named field, call again -- gets read as forbidden.
    That is the failure this test pins.
    """
    block = next(b for b in BLOCKS if b.startswith("Call tools with the exact"))

    assert "invalid_arguments" in block
    # It names the corrective action, not merely the condition.
    assert "Read the error" in block
    assert "again" in block
    # A retry that changes nothing is not a retry.
    assert "unchanged" in block


def test_the_recoverable_error_rule_does_not_license_retrying_a_denial() -> None:
    """The new paragraph is bounded; the denial rule is untouched.

    The two rules sit in one block because they share a sentence boundary, which
    is exactly why they can be confused. A recoverable-error instruction that
    reached denials would be the worst version of this change: it would turn
    Byte's single strongest standing commitment into a retry loop. So the denial
    paragraph must still read as final, and the recoverable rule must be about
    arguments, never about permission.
    """
    block = next(b for b in BLOCKS if b.startswith("Call tools with the exact"))

    assert BLOCK_D_DENIAL_PHRASE in block
    # Every circumvention route the denial rule closes is still closed.
    for banned_route in ("alias", "wrapper script", "different tool"):
        assert banned_route in block, f"denial rule lost a route: {banned_route}"
    # Ordering: recoverable error first, denial second.
    assert block.index("invalid_arguments") < block.index(BLOCK_D_DENIAL_PHRASE)
    # The denial paragraph itself must not have picked up retry language.
    denial_paragraph = block[block.index(BLOCK_D_DENIAL_PHRASE):]
    assert "again" not in denial_paragraph, "denial rule now permits a retry"


def test_the_recoverable_error_rule_survives_the_contract_comparison() -> None:
    """The rule lives in the contract, so a prompt-only edit cannot introduce it.

    ``test_invalid_arguments_is_instructed_as_a_recoverable_error`` would pass
    against a change made only in ``byte.py``. The rule has to be settled in the
    contract like every other block, so this asserts the contract's own Block D
    carries it -- the contract is the record, and the verbatim sync test then
    propagates it to the prompt rather than the prompt being edited directly.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert "invalid_arguments" in contract["D"]


#: Block B already says "Use search to locate evidence, not as a substitute for
#: reading it", which constrains *investigation*. The grounding rule constrains
#: something different: *claims*. The distinction is load-bearing, so both the
#: runtime constant and the contract are checked for the exact phrase.
GROUNDING_PHRASE = "unless you have read it in this session"

#: The opening words of Block B, used to find the block without holding a
#: second copy of its full text.
BLOCK_B_OPENING = "Investigate before changing"


def _block_b() -> str:
    return next(b for b in BLOCKS if b.startswith(BLOCK_B_OPENING))


def test_grounding_is_instructed_as_a_rule_about_claims() -> None:
    """A claim about unread code is the failure the rule exists to stop.

    Block B tells Byte how far to investigate, but nothing told it that a
    summary of code it has not opened is a fabrication with extra steps. Search
    output is not reading: an agent that finds a symbol name, a grep hit, or a
    filename can produce a fluent and entirely invented description of what is
    behind it. Nothing in the runtime catches that -- every tool result really
    was ok, and the final answer is text, so the only place to stop it is the
    standing instruction.
    """
    block = _block_b()

    # The condition, stated as a bar on the claim rather than on the tool call.
    assert GROUNDING_PHRASE in block
    # It names the corrective action: read it, or say what you did read.
    assert "read it" in block
    assert "did read" in block, "the rule says what to do instead, not just what not to do"
    # And it is scoped to claims, so it cannot be read as forbidding the tool
    # calls that make reading possible.
    assert "claims" in block or "describe" in block


def test_grounding_is_bounded_and_does_not_license_a_guess() -> None:
    """The rule constrains claims about unread code, and only that.

    Two ways this could rot. A grounding rule that says "do not act on what you
    have not read" would swallow Block B's *investigate first* instruction and
    push the agent to claim certainty it does not have. A grounding rule that
    permits an acknowledged inference would restore the failure under a new
    name. Both are pinned here, and the denial rule is checked for the same
    reason: this is a new paragraph in a block that already carries the single
    strongest standing commitment, and a new paragraph is exactly where a
    commitment gets loosened by accident.
    """
    block = _block_b()
    grounding = block[block.index(GROUNDING_PHRASE):]

    # Bounded: it is about reporting, not about refusing to work. The
    # investigate-first commitment is untouched.
    assert BLOCK_B_OPENING in block
    assert "Investigate before changing anything" in block
    # Not a licence to assert a guess with a caveat attached.
    for banned in ("likely", "probably", "assume", "inferred", "guess"):
        assert banned not in grounding, f"grounding rule licenses a guess: {banned}"
    # The grounding paragraph is not a route around a permission decision, and
    # the denial rule is still in Block D saying so.
    for route in ("alias", "wrapper script", "different tool"):
        assert route not in grounding, f"grounding rule opened a route: {route}"
    assert BLOCK_D_DENIAL_PHRASE in next(
        b for b in BLOCKS if b.startswith("Call tools with the exact")
    )


def test_grounding_survives_the_contract_comparison() -> None:
    """The rule lives in the contract, so a prompt-only edit cannot introduce it.

    Same reasoning as the recoverable-error rule: the contract is the record,
    and the verbatim sync test then carries the wording into the prompt. A test
    that only read ``byte.py`` would pass against a change that never went
    through review.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert GROUNDING_PHRASE in contract["B"]


# --------------------------------------------------------------------------
# Persistence: keep gathering evidence until it is sufficient
# --------------------------------------------------------------------------

#: The opening words of the persistence paragraph. Located by text rather than
#: by index so a future paragraph inserted into Block B does not silently
#: repoint these assertions at the wrong one.
PERSISTENCE_PHRASE = "One successful read or search is not sufficient"

#: The move the persistence rule exists to prohibit. Grounding's closing
#: instruction -- "say what you did read and name what you would read next" --
#: is legitimate for code Byte cannot reach, so the two cannot be separated by
#: the general instruction alone; the persistence rule has to name this move.
WOULD_NEED_MOVE = "Do not end the run by listing what you would need to read next"


def _persistence_paragraph() -> str:
    block = _block_b()
    assert PERSISTENCE_PHRASE in block, "Block B has no persistence paragraph"
    start = block.index(PERSISTENCE_PHRASE)
    rest = block[start:]
    # Paragraphs are separated by a blank line inside the block.
    end = rest.find("\n\n")
    return rest if end == -1 else rest[:end]


def test_persistence_is_instructed_for_inspect_explain_and_summarize() -> None:
    """A single successful read is not on its own a reason to answer.

    Block B's second paragraph states the destination -- enough evidence to
    answer the question -- but not what to do while still travelling toward it.
    The gap this closes is specific: Byte reads one file, correctly judges the
    architecture is larger, and then reports what it read and stops. That run
    is wrong in the way that is hardest to notice, because the tool calls it did
    make all succeeded and the report is internally consistent.
    """
    block = _block_b()

    # The condition is Byte's own judgement, not a count: a quota fails in both
    # directions at once.
    assert PERSISTENCE_PHRASE in block
    # ...and it is conditioned on being able to name what is still missing.
    assert "If you can name" in _persistence_paragraph()
    # The named action is continued tool use, not a longer report.
    assert "keep calling tools" in _persistence_paragraph()
    # And the exit that would otherwise look correct is closed explicitly.
    assert WOULD_NEED_MOVE in _persistence_paragraph()


def test_persistence_does_not_license_a_read_everything_run() -> None:
    """The rule is about sufficiency, and says so in both directions.

    A persistence instruction with no stopping condition turns Byte into an
    agent that cannot finish an investigation, which is a different failure with
    the same shape: the operator never gets an answer. So the legitimate stop
    must be stated -- the evidence answering the question actually asked -- and
    the bound must be explicit rather than left to the model's mood.
    """
    paragraph = _persistence_paragraph()

    # A real stopping condition, tied to the question that was asked.
    assert "sufficient to answer the question" in paragraph
    # Blocked is a legitimate end, and each blocker is named.
    for blocker in ("ambiguous", "refused", "does not exist"):
        assert blocker in paragraph, f"persistence rule lost a stopping case: {blocker}"
    # The bound against totality is stated outright, not implied.
    assert "sufficiency, not" in paragraph
    assert "reading everything" in paragraph
    assert "not expected to traverse the whole" in paragraph
    # No quota language crept in.
    for banned in ("at least", "minimum of", "every file", "all files"):
        assert banned not in paragraph, f"persistence rule became a quota: {banned}"


def test_persistence_is_not_a_route_around_a_refusal_or_a_guess() -> None:
    """A continuation rule can be misread as a retry rule, so it is bounded.

    "Keep calling tools until you have" and Block D's denial rule share a
    subject. Read carelessly, persistence would tell Byte to keep going after a
    refusal -- which is exactly the circumvention the denial rule closes. The
    persistence paragraph therefore has to treat a refused read as a stopping
    condition rather than a thing to route around, and it must not become a
    licence to state an inference it has not read.
    """
    paragraph = _persistence_paragraph()

    # A refusal ends the investigation; it is not retried under another name.
    assert "a read is refused" in paragraph
    for banned_route in ("alias", "wrapper script", "different tool", "again"):
        assert banned_route not in paragraph, f"persistence opened a route: {banned_route}"
    # And it does not license a guess, the way the grounding rule would if it
    # were loosened.
    for banned in ("likely", "probably", "assume", "inferred", "guess"):
        assert banned not in paragraph, f"persistence rule licenses a guess: {banned}"
    # The grounding rule itself is untouched and still downstream of it.
    assert GROUNDING_PHRASE in _block_b()
    assert BLOCK_D_DENIAL_PHRASE in next(
        b for b in BLOCKS if b.startswith("Call tools with the exact")
    )


def test_persistence_survives_the_contract_comparison() -> None:
    """The rule lives in the contract, so a prompt-only edit cannot introduce it.

    The same reason as the grounding and recoverable-error rules: the contract
    is the record, and the verbatim sync test then carries the wording into the
    prompt. Without this, the paragraph could be added to ``byte.py`` alone and
    never reviewed as an instruction.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert PERSISTENCE_PHRASE in contract["B"]
    assert WOULD_NEED_MOVE in contract["B"]
    # Grounding was preserved rather than traded away for it.
    assert GROUNDING_PHRASE in contract["B"]


def test_persistence_is_a_separate_paragraph_from_grounding() -> None:
    """Two rules, two paragraphs -- they are not allowed to blur.

    They are neighbours and they pull in opposite directions: grounding says
    describe only what you read, persistence says stop answering and go read
    more. Held in one paragraph the model can satisfy either at the expense of
    the other, and the cheapest reading of the pair is always "report early".
    """
    block = _block_b()

    # Persistence is not inside the grounding paragraph.
    grounding_start = block.index(GROUNDING_PHRASE)
    assert PERSISTENCE_PHRASE not in block[grounding_start:]
    # ...and the grounding rule's own escape hatch is not inside persistence.
    assert GROUNDING_PHRASE not in _persistence_paragraph()
    # Investigation still comes first; persistence extends it rather than
    # replacing it.
    assert block.index("When you are asked to inspect") < block.index(
        PERSISTENCE_PHRASE
    )


# --------------------------------------------------------------------------
# Discover before guessing: establish the real tree before searching it
# --------------------------------------------------------------------------

#: The opening words of the discover-before-guessing paragraph. Located by text
#: rather than by paragraph index, so the neighbouring persistence and grounding
#: tests keep pointing at their own paragraphs.
DISCOVERY_PHRASE = "Establish the project's actual structure"

#: The presumed path the rule names. It is deliberately a real-looking path in
#: neither this repository nor any other, because that is the point: it is what
#: a filename search invents out of the request's own vocabulary.
PRESUMED_PATH_EXAMPLE = "agent/runtime.js"


def _discovery_paragraph() -> str:
    block = _block_b()
    assert DISCOVERY_PHRASE in block, "Block B has no discover-before-guessing paragraph"
    rest = block[block.index(DISCOVERY_PHRASE) :]
    end = rest.find("\n\n")
    return rest if end == -1 else rest[:end]


def test_discovery_precedes_any_search_for_an_expected_path() -> None:
    """The tree is established before a path inside it is searched for.

    The failure this pins is a run that obeys both existing Block B rules and is
    still wrong: it gathers evidence diligently, at real paths it invented,
    because nothing said where a path is supposed to come from. So the rule is
    asserted as an ordering claim -- establish the actual structure, and only
    then search -- and as a discovery of what is *actually present*, not as a
    bar on any particular file.
    """
    paragraph = _discovery_paragraph()

    # The ordering is stated as a before, not implied by word order.
    assert "before you search" in paragraph
    # Discovery is of the real tree: top-level directories and real file types.
    assert "top-level directories" in paragraph
    assert "actually present" in paragraph
    # And the high-signal files, named rather than gestured at, because reading
    # the project's own documentation is the cheapest substitute for guessing.
    for signal_file in ("README", "manifest", "configuration", "conventions"):
        assert signal_file in paragraph, f"discovery rule lost a source: {signal_file}"


def test_discovery_does_not_assume_a_language_from_the_request() -> None:
    """Operator shorthand is not a description of the repository.

    This is the specific inference that makes the others tempting: "it's the
    JavaScript one", "it's Python", so `src/main.py` or `index.js`. Asserted as
    a bar on the presumption and not as a ban on the words themselves, so the
    fix stays discovery rather than squeamishness about the request.
    """
    paragraph = _discovery_paragraph()

    # The prohibition is on inferring, stated as an instruction.
    assert "Do not infer" in paragraph
    assert "the language or the layout from the wording of the request" in paragraph
    # The counterexample is given, so the rule is concrete rather than abstract.
    for language in ("Python", "JavaScript", "Rust"):
        assert language in paragraph, f"language inference not illustrated: {language}"
    # A guess is still called a guess. Hiding the word would let the paragraph
    # be satisfied by talking about "expected paths".
    assert "still only a guess" in paragraph


def test_a_presumed_path_is_not_treated_as_a_finding() -> None:
    """The plausible filename is named as the thing it is: not evidence.

    Grounding already bars claiming what unread code does. This is the same
    error one step earlier, and it needs its own bar because the moment a
    plausible path appears in the transcript it starts to look like knowledge
    of the layout.
    """
    paragraph = _discovery_paragraph()

    assert PRESUMED_PATH_EXAMPLE in paragraph
    assert "is not a finding" in paragraph
    # The grounding rule is still downstream and still intact, so this
    # paragraph cannot be read as replacing it.
    assert GROUNDING_PHRASE in _block_b()
    assert paragraph.index(DISCOVERY_PHRASE) < _block_b().index(GROUNDING_PHRASE)


def test_a_failed_presumed_path_is_evidence_rather_than_a_retry() -> None:
    """The not-found result changes what Byte does next.

    Without this, the default continuation after a nonexistent path is "that
    was the wrong path, try another" -- a retry loop in different vocabulary,
    several guesses deep, with every tool call returning ok. So the rule has to
    state that a failed read is information about the tree, and that the
    response is to discover rather than to re-guess.
    """
    paragraph = _discovery_paragraph()

    # A missing path is evidence about the structure, not a typo to absorb.
    assert "evidence about the tree" in paragraph
    assert "paper over with another" in paragraph
    # And the named next move is discovery, which is the whole rule.
    assert "discover the structure that does exist" in paragraph
    # Not a quota: no "keep trying until one works" reading is licensed.
    for banned in ("at least", "minimum of", "try each", "until one works"):
        assert banned not in paragraph, f"failed-path rule became a quota: {banned}"


def test_discovery_does_not_license_a_claim_or_a_route_around_a_denial() -> None:
    """The new paragraph is bounded on the two axes that matter.

    It could rot in two ways. Read as a licence to mention a plausible file,
    it becomes a loophole through grounding -- the agent did not claim to have
    read it, it merely noticed where it would be. Read as "keep looking for
    the file I expected", it becomes a route around a refusal. Both are
    checked against the denial rule, which is where circumvention is closed.
    """
    paragraph = _discovery_paragraph()

    # No hedging licence crept in, same banned set the grounding test uses.
    for banned in ("likely", "probably", "assume", "inferred"):
        assert banned not in paragraph, f"discovery rule licenses a guess: {banned}"
    # Not a retry or re-routing rule.
    for banned_route in ("alias", "wrapper script", "different tool"):
        assert banned_route not in paragraph, f"discovery opened a route: {banned_route}"
    # The evidence it reads is a *missing file*. A refusal is not a missing
    # file, so this cannot be read as "the read was refused, find another way":
    # persistence still names the refused read as a stopping condition, and the
    # denial rule still closes every re-routing route.
    assert "turns out not to exist" in paragraph
    assert "a read is refused" in _persistence_paragraph()
    assert BLOCK_D_DENIAL_PHRASE in next(
        b for b in BLOCKS if b.startswith("Call tools with the exact")
    )


def test_discovery_survives_the_contract_comparison() -> None:
    """The rule lives in the contract, so a prompt-only edit cannot introduce it.

    Same reasoning as the grounding, persistence, and recoverable-error rules.
    The prompt-only version of this test would pass against a change that was
    never reviewed as an instruction; the contract is the record, and the
    verbatim sync test carries the wording into the prompt from there.
    """
    contract = parse_contract_blocks(CONTRACT_PATH.read_text(encoding="utf-8"))
    block_b = contract["B"]

    assert DISCOVERY_PHRASE in block_b
    assert PRESUMED_PATH_EXAMPLE in block_b
    assert "Do not infer" in block_b
    # The rules it neighbours were preserved rather than traded away for it.
    assert GROUNDING_PHRASE in block_b
    assert PERSISTENCE_PHRASE in block_b
    assert WOULD_NEED_MOVE in block_b


def test_discovery_is_a_separate_paragraph_from_persistence_and_grounding() -> None:
    """Three rules, three paragraphs.

    Discovery and persistence share a trigger -- a failed or absent path is
    exactly when "keep going until you have enough" could decay into "keep
    guessing until one lands". Merged, the model could satisfy the cheaper
    reading at the expense of the expensive one, and the expensive one is the
    one that prevents four invented paths.
    """
    block = _block_b()

    # Neither rule leaked *into* the discovery paragraph. (Ordering the other
    # way is asserted below: discovery precedes persistence, so persistence
    # legitimately appears later in the block.)
    assert PERSISTENCE_PHRASE not in _discovery_paragraph()
    assert GROUNDING_PHRASE not in _discovery_paragraph()
    assert DISCOVERY_PHRASE not in _persistence_paragraph()
    # And it sits after the evidence-gathering paragraph it constrains: gather
    # the evidence, from the paths the project actually has.
    discovery_start = block.index(DISCOVERY_PHRASE)
    assert block.index("When you are asked to inspect") < discovery_start
    assert discovery_start < block.index(PERSISTENCE_PHRASE)


# --------------------------------------------------------------------------
# Approval integration
# --------------------------------------------------------------------------


def test_approval_required_command_prompts_and_runs(dispatcher_factory) -> None:
    """`git commit` is gated: the operator is asked, and approval runs it."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["commit", "-m", "x"]}),
                )
            ),
            ScriptedTurn(text="committed"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("commit")

    assert len(channel.presented) == 1
    assert result.tool_calls == 1
    assert not result.denied


def test_rejected_approval_is_returned_to_the_model(dispatcher_factory) -> None:
    """A rejection becomes a tool result the model can read, and nothing runs."""
    dispatcher, channel = dispatcher_factory(["n"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["commit", "-m", "x"]}),
                )
            ),
            ScriptedTurn(text="Understood, I did not commit."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("commit")

    assert len(channel.presented) == 1
    assert result.denied
    assert result.denied[0][0] == "exec_command"
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"rejected"' in tool_messages[0].content


def test_denied_command_never_consults_the_channel(dispatcher_factory) -> None:
    """A policy denial prompts nobody. There is no path to approve a denial."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(text="Cannot do that."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("list root files")

    assert channel.presented == []
    assert result.denied
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied"' in tool_messages[0].content


def test_unattended_approval_required_reports_distinctly(dispatcher_factory) -> None:
    """Unattended is its own status, not a rejection.

    A human saying no and nobody being there are different events, and an
    operator reading the transcript needs to tell them apart.
    """
    dispatcher, _ = dispatcher_factory()
    dispatcher.approver = Approver(None)  # unattended

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "pytest"},),)),
            ScriptedTurn(text="Could not run the tests."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("run the tests")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied_unattended"' in tool_messages[0].content


# --------------------------------------------------------------------------
# Denial cache
# --------------------------------------------------------------------------


def test_repeated_rejection_does_not_reprompt(dispatcher_factory) -> None:
    """An identical rejected call returns the cached result without prompting."""
    dispatcher, channel = dispatcher_factory(["n"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["push", "origin", "main"]}),
                )
            ),
            ScriptedTurn(
                tool_calls=(
                    ("exec_command", {"program": "git", "args": ["push", "origin", "main"]}),
                )
            ),
            ScriptedTurn(text="I cannot push."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("push it")

    # One prompt, not two: the operator said no once and is not asked again.
    assert len(channel.presented) == 1
    assert result.turns == 3
    assert result.tool_calls == 2


def test_repeated_denial_does_not_invoke_the_classifier_again(dispatcher_factory) -> None:
    """A cached denial short-circuits before any classifier or channel work."""
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),)),
            ScriptedTurn(text="done"),
        ]
    )

    calls: list[object] = []
    original = dispatcher._classifier.classify

    def counting(command):  # type: ignore[no-untyped-def]
        calls.append(command)
        return original(command)

    dispatcher._classifier.classify = counting  # type: ignore[method-assign]

    _runtime(gateway, dispatcher).run("escalate")

    assert len(calls) == 1, "the cached denial re-entered the classifier"
    assert channel.presented == []


def test_repeated_denied_turns_still_reach_max_turns(dispatcher_factory) -> None:
    """The denial cache does not exempt the model from the turn limit.

    The cache stops repeated prompts; it is not what stops a runaway. An agent
    looping on a denied call must still terminate.
    """
    dispatcher, channel = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [ScriptedTurn(tool_calls=(("exec_command", {"program": "sudo", "args": ["ls"]}),))]
    )

    with pytest.raises(MaxTurnsExceeded):
        _runtime(gateway, dispatcher, max_turns=25).run("keep escalating")

    # The prompt was shown at most once across all 25 turns.
    assert len(channel.presented) == 0


def test_different_failure_classes_are_not_cached(dispatcher_factory, tmp_path) -> None:
    """Malformed arguments are not cached: a retry with fixed arguments must work."""
    dispatcher, _ = dispatcher_factory()
    call = ("read_file", "{not valid json")

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(call,)),
            ScriptedTurn(tool_calls=(("read_file", {"path": "ok.txt"}),)),
            ScriptedTurn(text="done"),
        ]
    )
    (dispatcher.workspace.root / "ok.txt").write_text("fine\n")

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"invalid_arguments"' in tool_messages[0].content
    assert '"status":"ok"' in tool_messages[1].content


# --------------------------------------------------------------------------
# Tool errors
# --------------------------------------------------------------------------


def test_unknown_tool_is_reported_not_raised(dispatcher_factory) -> None:
    """A name with no registry entry is a value the model can read."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("rm_rf_everything", {}),)),
            ScriptedTurn(text="That tool does not exist."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("do something")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"unknown_tool"' in tool_messages[0].content
    assert result.text == "That tool does not exist."


def test_schema_violation_names_the_field(dispatcher_factory) -> None:
    """"invalid arguments" tells a model nothing; the field name does."""
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": 42}),)),
            ScriptedTurn(text="ok"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert "path must be a string" in tool_messages[0].content


def test_tool_internal_error_carries_no_traceback(dispatcher_factory, monkeypatch) -> None:
    """An internal failure is reported by class name, never as a traceback.

    A traceback reaches the model through the tool result, and carries file
    paths and possibly file contents on its way.
    """
    dispatcher, _ = dispatcher_factory()

    def exploding(**_: object):
        raise RuntimeError("boom at /etc/shadow with SECRET=abc123")

    monkeypatch.setattr(dispatcher, "_tool_read_file", exploding)
    dispatcher._registry["read_file"] = dispatcher._registry["read_file"].__class__(
        "read_file", "d", {}, exploding
    )

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),)),
            ScriptedTurn(text="ok"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"error"' in tool_messages[0].content
    assert "RuntimeError" in tool_messages[0].content
    assert "Traceback" not in tool_messages[0].content


def test_tool_not_allowed_when_profile_is_narrower(dispatcher_factory, workspace) -> None:
    """An agent configured without a tool cannot reach it by naming it."""
    dispatcher, _ = dispatcher_factory()
    # Rebuild the dispatcher with a narrower allowance.
    narrow = ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel([])),
        executor=Executor(workspace),
        agent_name="narrow-agent",
        allowed_tools=("read_file",),
    )
    narrow.system_prompt = "x"

    result = narrow.dispatch(
        __import__("forge.models.base", fromlist=["ToolCall"]).ToolCall(
            id="1", name="exec_command", arguments="{}"
        )
    )

    assert result.status is ToolStatus.TOOL_NOT_ALLOWED
    # And the tool is not even offered to the model.
    offered = {spec.name for spec in narrow.tool_specs()}
    assert offered == {"read_file"}


# --------------------------------------------------------------------------
# Session state and secrets
# --------------------------------------------------------------------------


def test_session_state_stays_in_process(dispatcher_factory, workspace, tmp_path) -> None:
    """A run creates no file. Session memory is session-only by decision."""
    dispatcher, _ = dispatcher_factory()
    before = set(p for p in workspace.root.rglob("*"))
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("write_file", {"path": "out.txt", "content": "x"}),)),
            ScriptedTurn(text="wrote it"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("write a file")

    after = set(p for p in workspace.root.rglob("*"))
    # The only new file is the one the tool was asked to write.
    assert after - before == {workspace.root / "out.txt"}
    assert result.state.messages  # in memory, returned for inspection


def test_no_provider_key_reaches_model_context(dispatcher_factory) -> None:
    """Whatever a run sends to the model, it is not an environment value.

    Asserted over the whole rendered transcript rather than over dataclass
    fields: a credential smuggled into a JSON tool result would live inside a
    string, and field inspection would miss it.
    """
    dispatcher, _ = dispatcher_factory()
    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": "x"}),)),
            ScriptedTurn(text="done"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read a file")

    transcript = result.state.transcript_text()
    assert "FORGE_OPENROUTER_API_KEY" not in transcript
    assert "Authorization" not in transcript
    assert "Bearer " not in transcript


def test_reading_a_structural_secret_is_denied_without_reading_it(
    dispatcher_factory, workspace
) -> None:
    """`.env` is denied, and its contents never reach the transcript."""
    (workspace.root / ".env").write_text("TOKEN=super-secret-value\n")
    dispatcher, channel = dispatcher_factory(["y"])

    gateway = ScriptedGateway(
        [
            ScriptedTurn(tool_calls=(("read_file", {"path": ".env"}),)),
            ScriptedTurn(text="I cannot read that file."),
        ]
    )

    result = _runtime(gateway, dispatcher).run("read .env")

    assert "super-secret-value" not in result.state.transcript_text()
    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert '"status":"denied"' in tool_messages[0].content


def test_process_output_is_bounded_and_model_visible(
    dispatcher_factory, workspace
) -> None:
    """stdout reaches the model bounded, secrets and all.

    This documents the v1 limitation honestly rather than pretending a redaction
    guarantee exists. Forge prevents *known* secret sources from being read on
    purpose; arbitrary subprocess output cannot be guaranteed secret-free, and v1
    does not attempt to fix that. What it does is bound the output.

    `/usr/bin/echo` is used because the Executor's PATH is fixed to the trusted
    system roots -- a workspace script would fail to launch, which is the
    executor's own documented limitation rather than anything this test is about.
    """
    dispatcher, _ = dispatcher_factory(["y"])
    gateway = ScriptedGateway(
        [
            ScriptedTurn(
                tool_calls=(
                    (
                        "exec_command",
                        {
                            "program": "/usr/bin/echo",
                            "args": ["TOKEN=from-process-output"],
                        },
                    ),
                )
            ),
            ScriptedTurn(text="saw the output"),
        ]
    )

    result = _runtime(gateway, dispatcher).run("run a command")

    tool_messages = [m for m in result.state.messages if m.role == "tool"]
    assert "from-process-output" in tool_messages[0].content
    # Bounded output is returned whole here because it is small. The bound
    # itself is the Executor's, and this asserts the shape carries it.
    assert '"stdout"' in tool_messages[0].content
    assert '"stdout_truncated"' in tool_messages[0].content


# --------------------------------------------------------------------------
# Structural guards
# --------------------------------------------------------------------------


def _imported_modules(path: pathlib.Path, package: str | None = None) -> set[str]:
    """Every module imported by ``path``, as dotted names.

    Relative imports are resolved against ``package`` rather than skipped. That
    matters: a forbidden import written as ``from ..tools.commands import ...``
    is exactly the shape a future edit would take, and a scan that only handled
    ``node.level == 0`` would sail straight past it. That is a mistake this test
    suite actually made and corrected -- the guard was verified by injecting the
    violation and watching it pass.
    """
    tree = ast.parse(path.read_text())
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                if module:
                    found.add(module)
            elif package:
                # Walk up `level - 1` packages, then descend into `module`.
                parts = package.split(".")
                base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                found.add(".".join([*base, *module.split(".")]) if module else ".".join(base))

    return found


def cli_main_path() -> str:
    """Filesystem path of ``forge/cli/main.py``.

    Resolved from the package root rather than imported, because
    ``from forge.cli import main`` binds the *function* (the package re-exports
    it) and a function has no ``__file__``.
    """
    return str(
        pathlib.Path(__file__).resolve().parent.parent / "forge" / "cli" / "main.py"
    )


def test_runtime_module_holds_no_policy_objects() -> None:
    """The runtime must not be able to reach a classifier, approver, or executor.

    A runtime that could reach one would be one edit away from holding a second
    opinion about what is allowed, which is how "approval narrows a permission,
    it never widens one" decays.
    """
    source = pathlib.Path(runtime_module.__file__ or "")
    imported = _imported_modules(source, package="forge.agents")

    for forbidden in (
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
    ):
        assert forbidden not in imported, f"runtime imports {forbidden}"

    for relative in ("commands", "approval", "execution", "secrets", "workspace"):
        assert not any(
            name.endswith(f".{relative}") for name in imported
        ), f"runtime imports {relative}"


def test_agent_runtime_has_no_executor_attribute(dispatcher_factory) -> None:
    """The instance cannot reach an executor even if one were passed in."""
    dispatcher, _ = dispatcher_factory()
    runtime = AgentRuntime(
        BYTE,
        gateway=ScriptedGateway([]),
        dispatcher=dispatcher,
        model_id="scripted",
    )

    for attribute in ("executor", "approver", "classifier", "_classifier"):
        assert not hasattr(runtime, attribute), f"runtime exposes {attribute}"

def test_dispatcher_is_the_only_runtime_path_to_execution() -> None:
    """Only the dispatcher may import `forge.tools.execution`.

    Scoped to the *runtime* packages -- ``forge/agents`` and ``forge/models``.
    The CLI is excluded deliberately and correctly: `forge byte` wires an
    ``Executor`` into a dispatcher, which is the sanctioned composition rather
    than a second path to a process. What must not happen is an agent module
    reaching an executor on its own.
    """
    offenders: list[str] = []
    package_root = pathlib.Path(__file__).resolve().parent.parent / "forge"

    runtime_packages = ("agents", "models")

    for package in runtime_packages:
        for path in (package_root / package).rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            parts = list(path.relative_to(package_root.parent).parts)
            imported = _imported_modules(path, package=".".join(parts[:-1]))
            if "forge.tools.execution" in imported or any(
                name.endswith(".execution") for name in imported
            ):
                offenders.append("/".join(parts))

    assert offenders == [], f"runtime modules importing execution: {offenders}"


def test_cli_wires_an_executor_only_through_a_dispatcher() -> None:
    """`forge byte` may construct an executor, but only to hand to a dispatcher.

    The CLI is allowed to assemble the pipeline. It is not allowed to call
    ``execute`` itself, which would be a second path to a process that bypasses
    approval entirely.
    """
    source = pathlib.Path(cli_main_path()).read_text()
    tree = ast.parse(source)

    imported = _imported_modules(pathlib.Path(cli_main_path()), package="forge.cli")
    assert "forge.tools.execution" in imported, "the CLI wires an executor"

    # No direct call to Executor.execute anywhere in the CLI.
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
            elif isinstance(func, ast.Name):
                calls.append(func.id)

    assert "execute" not in calls, (
        "the CLI calls execute directly; execution belongs to the dispatcher"
    )


def test_model_gateway_package_holds_no_policy() -> None:
    """The model layer cannot hold permission policy.

    A gateway that could widen a permission would collapse the separation the
    safety stack rests on: the model decides what it wants, Forge decides whether
    it is allowed.
    """
    package_root = pathlib.Path(__file__).resolve().parent.parent / "forge" / "models"
    forbidden = {
        "forge.tools.commands",
        "forge.tools.approval",
        "forge.tools.execution",
        "forge.tools.secrets",
        "forge.tools.workspace",
        "forge.tools.dispatcher",
    }

    for path in package_root.glob("*.py"):
        imported = _imported_modules(path)
        overlap = imported & forbidden
        assert overlap == set(), f"{path.name} imports policy: {overlap}"


def test_gateway_protocol_has_exactly_one_method() -> None:
    """One method, so the runtime stays provider-neutral.

    Every additional gateway method is a reason the runtime must implement
    provider-specific behaviour, which is what `ModelGateway` exists to prevent.
    """
    from forge.models.base import ModelGateway

    methods = [
        name
        for name in vars(ModelGateway)
        if callable(getattr(ModelGateway, name, None)) and not name.startswith("_")
    ]
    assert methods == ["complete"], f"gateway protocol grew: {methods}"


def test_scripted_gateway_needs_no_credential() -> None:
    """The offline path requires no key, which is why tests can use it."""
    gateway = ScriptedGateway([ScriptedTurn(text="ok")])
    from forge.models.base import CompletionRequest

    response = gateway.complete(
        CompletionRequest(system="s", messages=(), model="m")
    )
    assert response.text == "ok"
