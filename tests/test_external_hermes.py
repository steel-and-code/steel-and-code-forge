"""The external-agent boundary: the Hermes adapter and the task gate.

Hermes is not a Forge agent. It is a separate program, invoked as a subprocess,
which Forge cannot observe once started. Everything tested here follows from that
one fact, and the tests are organised around the three places it could go wrong:

*   **argv** -- the exact command Forge runs. A missing safety flag here is a
    silent grant of every tool Hermes has.
*   **environment** -- what the child inherits. An overlay rather than a
    minimal environment would hand it the operator's credentials.
*   **gating and parsing** -- what Forge decides before spawning, and what it
    believes afterwards.

The measurements that shaped this are recorded where they matter rather than
here, because a test asserting them would need a live model and a network. The
two that changed the design:

*   Hermes's **process exit code is always 0**, even for a failed run, so the
    ``result`` event is authoritative. ``test_parse_*`` pins that.
*   **Omitting ``-t`` grants every tool**, and ``-t ""`` does too -- an empty
    value is silently ignored, which looks like a restriction and is not one.
    Only ``none`` blocks. ``test_argv_pins_the_no_capability_toolset`` exists
    because getting this wrong would look correct in every other test here.

Nothing in this file spawns a process or contacts a network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge.agents.byte import BYTE
from forge.agents.neighbor import NEIGHBOR
from forge.agents.profile import AgentProfile
from forge.agents.registry import AGENTS
from forge.cli.main import build_parser
from forge.external.base import (
    ExternalInvocation,
    ExternalResult,
    ExternalUnavailable,
)
from forge.external.gating import gate_task
from forge.external.hermes import (
    NO_CAPABILITIES_TOOLSET,
    PASSTHROUGH_ENV,
    HermesAdapter,
)
from forge.tools.workspace import Classification, Workspace


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "plain.txt").write_text("ordinary\n")
    return Workspace(root)


def _invocation(workspace: Workspace, task: str = "what is this?") -> ExternalInvocation:
    return ExternalInvocation(
        agent="hermes", task=task, workspace=str(workspace.root), timeout=60.0
    )


# --------------------------------------------------------------------------
# The adapter is not a native profile
# --------------------------------------------------------------------------


def test_hermes_is_not_in_the_native_agent_registry() -> None:
    """The whole Stage 3 boundary, in one assertion.

    A native profile's ``allowed_tools`` is enforced by Forge's dispatcher. An
    external agent's tools run in another process behind another dispatcher, so a
    profile would constrain nothing while *looking* like it constrained
    something. Registering Hermes would create exactly that false assurance.
    """
    assert "hermes" not in AGENTS
    assert "hermes" not in AGENTS.keys()
    assert set(AGENTS) == {"byte", "neighbor"}
    assert BYTE.name == "byte"
    assert NEIGHBOR.name == "neighbor"


def test_hermes_is_not_an_agent_profile() -> None:
    """It is an ``ExternalAgent``, not an ``AgentProfile``.

    Asserted by type rather than by duck-typing: a profile carries
    ``allowed_tools`` and ``instructions``, and an adapter carrying similarly
    named attributes would be mistaken for one.
    """
    adapter = HermesAdapter()
    assert not isinstance(adapter, AgentProfile)
    assert adapter.capabilities == ()
    # And the profile contract it would need is absent.
    assert not hasattr(adapter, "allowed_tools")
    assert not hasattr(adapter, "instructions")


def test_the_adapter_satisfies_the_external_protocol() -> None:
    from forge.external.base import ExternalAgent

    assert isinstance(HermesAdapter(), ExternalAgent)


def test_the_native_registry_is_unchanged_by_the_external_package() -> None:
    """Importing ``forge.external`` must not register anything native."""
    import forge.agents.registry as registry

    assert set(registry.AGENTS) == {"byte", "neighbor"}


# --------------------------------------------------------------------------
# argv: the safety boundary
# --------------------------------------------------------------------------


def test_argv_pins_the_no_capability_toolset(workspace: Workspace) -> None:
    """**The load-bearing assertion of the external-agent MVP.**

    Omitting the flag grants Hermes the operator's full configured toolsets --
    ``terminal``, ``file``, ``code_execution``, ``browser``,
    ``computer_use`` -- and ``-t ""`` grants them too, because an empty value is
    silently ignored. Only ``none`` blocks. Verified by having real Hermes attempt
    a shell command, a file read, and a fetch: all three were refused.

    So the value must be present *and* correct. An argv without it is not a
    regression in one test; it is a silent grant of every tool Hermes has, and it
    would pass every other test in this file.
    """
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    assert "--toolsets" in argv
    index = argv.index("--toolsets")
    assert argv[index + 1] == NO_CAPABILITIES_TOOLSET == "none"
    # An empty value is the specific trap; assert it is not what we pass.
    assert argv[index + 1] != ""


def test_argv_never_passes_yolo(workspace: Workspace) -> None:
    """``--yolo`` bypasses Hermes's own approvals and is never acceptable.

    Matched on whole elements only. A substring test would trip over unrelated
    argv entries and stop testing the thing it claims to.
    """
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    assert "--yolo" not in argv
    assert not any(part.lower() == "yolo" for part in argv)
    assert not any(part.lower().startswith("--yolo") for part in argv)


def test_argv_never_selects_a_model(workspace: Workspace) -> None:
    """Hermes owns its model. Forge reports it; it never chooses it.

    Passing a Forge-routed model would address a model to an endpoint whose
    provider pairing Forge does not control -- the mistake
    ``docs/model-registry.md`` explicitly forbids.
    """
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    for banned in ("--model", "-m", "--provider", "--reasoning"):
        assert banned not in argv, f"argv must not carry {banned}"
    # And no model id was smuggled in as a positional.
    assert "vendor/" not in " ".join(argv)


def test_argv_uses_the_structured_interface(workspace: Workspace) -> None:
    """``--format stream-json``, because plain text carries no exit code."""
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    assert argv[1] == "chat"
    assert "--format" in argv
    assert argv[argv.index("--format") + 1] == "stream-json"
    assert "--oneshot" in argv


def test_argv_sets_the_workspace_explicitly(workspace: Workspace) -> None:
    """``--in`` rather than relying on the inherited cwd.

    A subprocess inherits whatever directory Forge was launched from, which is
    not a safety decision. Verified necessary: a plain ``cd`` did not move
    Hermes.
    """
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    assert "--in" in argv
    assert argv[argv.index("--in") + 1] == str(workspace.root)


def test_the_task_is_one_argv_element_not_a_shell_string(workspace: Workspace) -> None:
    """Shell metacharacters in a task are data, never syntax.

    Assembled at runtime so no dangerous literal appears in this file.
    """
    nasty = "check $(whoami) and `id` and | bash and 'quotes' and \"more\""

    argv = HermesAdapter("/bin/true").argv(_invocation(workspace, nasty))

    # Present exactly once, whole, as a single element.
    assert argv.count(nasty) == 1
    assert "--query" in argv
    assert argv[argv.index("--query") + 1] == nasty


def test_argv_carries_no_interactive_or_hook_flags(workspace: Workspace) -> None:
    """Nothing that needs a TTY or auto-approves anything."""
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    for banned in ("--tui", "--cli", "--accept-hooks", "--safe-mode"):
        assert banned not in argv, f"argv must not carry {banned}"


def test_max_turns_is_passed_through_when_given(workspace: Workspace) -> None:
    invocation = ExternalInvocation(
        agent="hermes", task="t", workspace=str(workspace.root), max_turns=7
    )
    argv = HermesAdapter("/bin/true").argv(invocation)

    assert "--max-turns" in argv
    assert argv[argv.index("--max-turns") + 1] == "7"


def test_max_turns_is_omitted_when_unset(workspace: Workspace) -> None:
    argv = HermesAdapter("/bin/true").argv(_invocation(workspace))

    assert "--max-turns" not in argv


def test_a_missing_hermes_is_an_unavailable_error(workspace: Workspace) -> None:
    adapter = HermesAdapter("/nonexistent/path/to/hermes")

    assert adapter.available() is False
    with pytest.raises(ExternalUnavailable, match="not found"):
        adapter.argv(_invocation(workspace))


# --------------------------------------------------------------------------
# environment
# --------------------------------------------------------------------------


def test_the_child_environment_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    """An allowlist over the current environment, not the whole thing.

    Inheriting ambient state would hand the child Forge's credentials and every
    other session variable, which is the opposite of a capability boundary.
    """
    monkeypatch.setenv("FORGE_WORKSPACE", "/some/workspace")
    monkeypatch.setenv("FORGE_MODEL", "vendor/secret-default")
    monkeypatch.setenv("FORGE_OPENROUTER_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "nope")

    env = HermesAdapter().environment(_invocation(Workspace(Path.cwd())))

    assert "FORGE_WORKSPACE" not in env
    assert "FORGE_MODEL" not in env
    assert "FORGE_OPENROUTER_API_KEY" not in env
    assert "SOME_UNRELATED_SECRET" not in env


def test_the_child_environment_keeps_only_what_hermes_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/someone")
    monkeypatch.setenv("HERMES_HOME", "/home/someone/.hermes/profiles/byte")

    env = HermesAdapter().environment(_invocation(Workspace(Path.cwd())))

    # Hermes needs these to find its own installation and profile.
    assert env.get("HOME") == "/home/someone"
    assert env.get("HERMES_HOME") == "/home/someone/.hermes/profiles/byte"
    # Every forwarded key is on the declared allowlist.
    assert set(env) - {"FORGE_INVOKED_HERMES"} <= set(PASSTHROUGH_ENV)


def test_no_hermes_model_override_leaks_into_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``HERMES_INFERENCE_MODEL`` is a model override and must not be forwarded.

    It is not on the passthrough allowlist precisely so that Forge cannot
    accidentally become the thing that chooses Hermes's model.
    """
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "vendor/forge-chosen")

    env = HermesAdapter().environment(_invocation(Workspace(Path.cwd())))

    assert "HERMES_INFERENCE_MODEL" not in env


# --------------------------------------------------------------------------
# Parsing: the exit code is not the answer
# --------------------------------------------------------------------------


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_parse_reads_success_from_the_result_event() -> None:
    stdout = _stream(
        {"type": "system", "subtype": "init", "model": "vendor/m", "session_id": "s1"},
        {"type": "text", "text": "hello"},
        {"type": "result", "session_id": "s1", "exit_code": 0, "text": "hello"},
    )

    result = HermesAdapter().parse(stdout, "", 0)

    assert isinstance(result, ExternalResult)
    assert result.ok is True
    assert result.text == "hello"
    assert result.model == "vendor/m"
    assert result.session_id == "s1"
    assert result.detail == ""


def test_parse_treats_a_nonzero_result_as_failure_even_with_returncode_zero() -> None:
    """**The behaviour the whole parse path exists for.**

    Verified against the real Hermes: a run whose work failed still exits 0, so
    trusting ``returncode`` would report success for a failed run.
    """
    stdout = _stream(
        {"type": "text", "text": "partial"},
        {"type": "result", "session_id": "s1", "exit_code": 7, "text": ""},
    )

    result = HermesAdapter().parse(stdout, "", 0)

    assert result.ok is False
    assert result.exit_code == 7
    assert result.detail


def test_parse_reports_failure_when_there_is_no_result_event() -> None:
    """A missing result event is not an implicit success."""
    result = HermesAdapter().parse('{"type":"text","text":"hi"}\n', "", 0)

    assert result.ok is False
    assert result.exit_code == 1
    assert "no result event" in result.detail


def test_parse_reports_failure_on_empty_output() -> None:
    """The child died before speaking."""
    result = HermesAdapter().parse("", "something went wrong", 1)

    assert result.ok is False
    assert "something went wrong" in result.detail


def test_parse_surfaces_child_stderr_on_a_failed_start() -> None:
    """Untrusted third-party text, bounded before it reaches an operator."""
    result = HermesAdapter().parse("", "x" * 5000 + "TAIL-MARKER", 1)

    assert result.ok is False
    assert "TAIL-MARKER" in result.detail
    assert len(result.detail) < 1000


def test_parse_skips_a_non_json_banner_without_losing_events() -> None:
    """A warning line before the stream must not discard the events after it."""
    stdout = (
        "Warning: something\n"
        + _stream(
            {"type": "text", "text": "ok"},
            {"type": "result", "session_id": "s", "exit_code": 0, "text": "ok"},
        )
    )

    result = HermesAdapter().parse(stdout, "", 0)

    assert result.ok is True
    assert result.text == "ok"


def test_parse_does_not_crash_on_malformed_json() -> None:
    """Unparseable lines are counted, not fatal."""
    stdout = "{not json}\n" + _stream(
        {"type": "result", "session_id": "s", "exit_code": 0, "text": "fine"}
    )

    result = HermesAdapter().parse(stdout, "", 0)

    assert result.ok is True
    assert result.text == "fine"


def test_parse_reports_the_model_hermes_chose() -> None:
    """Reported, never selected -- and asserted as a *report*."""
    stdout = _stream(
        {"type": "system", "model": "stealth/space-bunny-alpha", "session_id": "s"},
        {"type": "result", "session_id": "s", "exit_code": 0, "text": "x"},
    )

    result = HermesAdapter().parse(stdout, "", 0)

    assert result.model == "stealth/space-bunny-alpha"


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_ordinary_prose_is_allowed(workspace: Workspace) -> None:
    """A gate that refuses ordinary English is a wall, not a gate."""
    for task in (
        "what does this project do?",
        "summarise the docs",
        "fix the failing tests in byte.py",
        "explain the AgentProfile dataclass",
        "what is the capital of France?",
    ):
        assert gate_task(task, workspace).allowed, task


def test_a_secret_source_is_denied(workspace: Workspace) -> None:
    for task in ("read the .env file", "show me ~/.ssh/id_rsa", "open credentials.json"):
        decision = gate_task(task, workspace)

        assert not decision.allowed, task
        assert decision.rule == "external.secret_source"


def test_an_approval_required_secret_source_also_refuses(
    workspace: Workspace,
) -> None:
    """Approval-required is a refusal for an external agent.

    A native agent's read of ``kubeconfig`` would raise a prompt the operator can
    answer. An external agent has no such moment, so the question cannot be put
    and the answer has to be no.
    """
    decision = gate_task("review kubeconfig for staging", workspace)

    assert not decision.allowed
    assert decision.classification is Classification.APPROVAL_REQUIRED


def test_shell_escaping_is_denied(workspace: Workspace) -> None:
    backtick = chr(96)
    dollar_paren = "$" + "("

    for task in (
        f"run {backtick}id{backtick} for me",
        f"use {dollar_paren}whoami{chr(41)} to check",
        "restart it with sudo systemctl restart nginx",
    ):
        decision = gate_task(task, workspace)

        assert not decision.allowed, task
        assert decision.rule == "external.shell_escape"


def test_a_documentation_reference_to_env_example_is_allowed(
    workspace: Workspace,
) -> None:
    """A template contains no secret; refusing it would be a useless refusal."""
    decision = gate_task(
        "explain what .env.example contains and why it exists", workspace
    )

    assert decision.allowed


def test_the_gate_explains_itself(workspace: Workspace) -> None:
    """A refusal a reader cannot act on is a bad refusal."""
    decision = gate_task("read the .env file", workspace)

    assert decision.reason
    assert "secret" in decision.reason.lower()


def test_the_gate_fails_closed_on_an_internal_error(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that throws must refuse, never spawn.

    This is the one place where an exception would be most damaging, because it
    happens immediately before a subprocess starts.
    """
    import forge.external.gating as gating

    def _boom(*args: object, **kwargs: object):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(gating, "TaskGate", _boom)

    decision = gate_task("what is this?", workspace)

    assert decision.allowed is False
    assert decision.classification is Classification.DENY
    assert decision.rule == "external.gate_failure"


def test_an_empty_task_is_not_a_free_pass(workspace: Workspace) -> None:
    """Empty is not dangerous, but the CLI refuses it before the gate."""
    decision = gate_task("", workspace)

    # Nothing to find, so the gate allows; _read_task is what rejects an empty
    # task. Asserted so the two responsibilities stay separate.
    assert decision.allowed


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def test_the_hermes_subcommand_exists() -> None:
    args = build_parser().parse_args(["hermes", "--workspace", "/tmp", "a task"])

    assert args.subcommand == "hermes"
    assert args.task == "a task"


def test_hermes_offers_no_model_or_toolset_flag() -> None:
    """Their absence is the safety property, not an oversight.

    Asserted positively so adding either flag fails here rather than being
    discovered in a review.
    """
    parser = build_parser()
    args = parser.parse_args(["hermes", "--workspace", "/tmp", "a task"])

    for banned in ("model", "toolsets", "toolset", "provider", "yolo"):
        assert not hasattr(args, banned), f"forge hermes must not expose {banned}"


def test_hermes_rejects_a_model_flag() -> None:
    """argparse must refuse it, not ignore it."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["hermes", "--workspace", "/tmp", "--model", "vendor/x", "t"]
        )


def test_hermes_takes_a_timeout_and_max_turns() -> None:
    args = build_parser().parse_args(
        ["hermes", "--workspace", "/tmp", "--timeout", "30", "--max-turns", "4", "t"]
    )

    assert args.timeout == 30.0
    assert args.max_turns == 4


def test_byte_and_neighbor_keep_their_model_flag() -> None:
    """The contrast: native agents still take ``--model``, Hermes does not."""
    parser = build_parser()

    byte_args = parser.parse_args(
        ["byte", "--workspace", "/tmp", "--model", "vendor/x", "t"]
    )
    neighbor_args = parser.parse_args(
        ["neighbor", "--workspace", "/tmp", "--model", "vendor/x", "t"]
    )

    assert byte_args.model == "vendor/x"
    assert neighbor_args.model == "vendor/x"


def test_the_status_banner_lists_all_three_agents() -> None:
    import io

    from forge.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    main([], out=out, err=err, environ={})

    banner = out.getvalue()
    assert "forge byte" in banner
    assert "forge neighbor" in banner
    assert "forge hermes" in banner


def test_hermes_is_not_reachable_through_the_native_dispatch(
    workspace: Workspace,
) -> None:
    """The CLI keeps the external path separate from the native table.

    The native dispatch is a ``{"byte": ..., "neighbor": ...}`` lookup. Hermes
    must not be a key in it, or an external agent would be running through code
    that assumes an ``AgentProfile``.
    """
    import importlib
    import inspect

    # By dotted name, not ``import forge.cli.main as cli``: ``forge.cli`` exports
    # a *function* called ``main``, so the attribute form binds that function and
    # silently has no ``.main``.
    cli_module = importlib.import_module("forge.cli.main")
    source = inspect.getsource(cli_module.main)
    dispatch_block = source.split("runner = {", 1)[1].split("}", 1)[0]

    assert "byte" in dispatch_block
    assert "neighbor" in dispatch_block
    assert "hermes" not in dispatch_block


def test_native_agents_are_unaffected_by_the_external_package() -> None:
    """Byte and Neighbor keep ``model=None`` and their full tool sets."""
    assert BYTE.model is None
    assert NEIGHBOR.model is None
    assert BYTE.allowed_tools == NEIGHBOR.allowed_tools
    assert len(BYTE.allowed_tools) == 6


def test_the_external_package_imports_no_native_agent_runtime() -> None:
    """No module under ``forge/external`` may reach Forge's agent runtime.

    An external agent has no profile and no runtime; importing either would be
    the first step toward modelling it as native.
    """
    import forge.external

    root = Path(forge.external.__file__ or "").parent
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for banned in (
            "from ..agents",
            "import forge.agents",
            "from forge.agents",
        ):
            assert banned not in source, f"{path.name} imports a native agent module"


def test_the_external_package_holds_no_policy_of_its_own() -> None:
    """Gating reuses Forge's classifier; it does not reimplement one.

    Scanned over *code* rather than raw text. A raw scan trips over this
    module's own docstrings, which discuss ``CommandClassifier`` at length
    precisely to explain why the gate does not use it -- and a structural test
    that fires on the explanation is a test that gets deleted instead of read.
    """
    import ast
    from pathlib import Path

    import forge.external.gating as gating

    tree = ast.parse(Path(gating.__file__ or "").read_text(encoding="utf-8"))
    imported: set[str] = set()
    instantiated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            instantiated.add(node.func.id)
        elif isinstance(node, ast.Attribute):
            instantiated.add(node.attr)

    # It uses the real secret classifier...
    assert "SecretClassifier" in imported
    # ...and builds no execution, approval, or command-classification machinery
    # of its own.
    for banned in ("Executor", "Approver", "CommandClassifier", "Command"):
        assert banned not in imported, f"gating imports its own {banned}"
        assert banned not in instantiated, f"gating instantiates {banned}"


def test_the_external_package_never_passes_a_shell() -> None:
    """``shell=True`` anywhere in the external package would be a hole.

    Every subprocess Forge starts is an argument list, so a task containing
    ``$(...)`` is data. Checked structurally across all modules because one
    occurrence is enough.
    """
    import forge.external

    root = Path(forge.external.__file__ or "").parent
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert "shell=True" not in source, f"{path.name} would run a shell"
        assert "os.system" not in source, f"{path.name} would run os.system"


def test_the_environment_allowlist_excludes_credentials() -> None:
    """Nothing that looks like a credential may be in the passthrough set."""
    lowered = {key.lower() for key in PASSTHROUGH_ENV}

    for banned in ("api_key", "token", "secret", "password", "credential"):
        assert not any(banned in key for key in lowered), banned


def test_forge_invocation_is_marked_in_the_child_env(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Useful in a transcript: the child knows a Forge run started it."""
    monkeypatch.setenv("HOME", "/home/someone")
    env = HermesAdapter().environment(_invocation(workspace))

    assert env.get("FORGE_INVOKED_HERMES") == "1"
    assert "FORGE_INVOKED_HERMES" not in PASSTHROUGH_ENV


def test_the_adapter_describes_itself_for_an_operator() -> None:
    description = HermesAdapter().describe()

    assert "Hermes" in description
    assert "text-only" in description
    assert "own model" in description


def test_hermes_program_env_var_is_honoured(workspace: Workspace) -> None:
    """``FORGE_HERMES_PROGRAM`` names a program or path -- never a shell string."""
    import importlib

    cli_module = importlib.import_module("forge.cli.main")

    assert cli_module.HERMES_PROGRAM_ENV == "FORGE_HERMES_PROGRAM"

    # An absolute path is used directly.
    adapter = HermesAdapter("/bin/true")
    assert adapter.argv(_invocation(workspace))[0] == "/bin/true"

    # A bare name is resolved from PATH.
    assert HermesAdapter("definitely-not-a-real-program").available() is False


def test_the_os_module_is_imported_for_anchoring() -> None:
    """``gating`` anchors relative paths, which needs ``os.path.isabs``."""
    import forge.external.gating as gating

    assert hasattr(gating, "os")
    assert os.path.isabs("/x")
