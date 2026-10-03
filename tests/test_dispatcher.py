"""Tests for tool dispatch: the permission integration, tool by tool.

These are the tests that matter most in the whole runtime milestone. The loop in
``test_agent_runtime.py`` proves the machinery turns; these prove that every tool
call actually goes through Forge's safety stack and cannot route around it.

No network, no credential, no live provider.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from forge.models.base import ToolCall
from forge.tools.approval import Approver, ScriptedApprovalChannel
from forge.tools.dispatcher import (
    ToolDispatcher,
    ToolResult,
    ToolStatus,
    _validate,
)
from forge.tools.execution import Executor
from forge.tools.workspace import Workspace


@pytest.fixture()
def workspace(tmp_path: pathlib.Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def make(workspace: Workspace):
    def build(responses: list[str | None] | None = None, *, agent: str = "byte"):
        channel = ScriptedApprovalChannel(responses if responses is not None else [])
        dispatcher = ToolDispatcher(
            workspace,
            approver=Approver(channel),
            executor=Executor(workspace),
            agent_name=agent,
            allowed_tools=(
                "read_file",
                "search_files",
                "search_content",
                "write_file",
                "patch_file",
                "exec_command",
            ),
            system_prompt="test",
        )
        return dispatcher, channel

    return build


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=json.dumps(arguments))


# --------------------------------------------------------------------------
# exec_command through the full pipeline
# --------------------------------------------------------------------------


def test_automatic_command_runs_without_prompting(make) -> None:
    """`git status` is on List 3: automatic, and no prompt appears."""
    dispatcher, channel = make()
    result = dispatcher.dispatch(call("exec_command", program="git", args=["status"]))

    assert result.status is ToolStatus.OK
    assert channel.presented == []


def test_approval_required_command_prompts(make) -> None:
    """`git commit` is gated, and approval lets it run."""
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(
        call("exec_command", program="git", args=["commit", "-m", "x"])
    )

    assert len(channel.presented) == 1
    assert result.status is not ToolStatus.REJECTED


def test_rejected_command_reports_rejected(make) -> None:
    """A refusal is a value, not an exception."""
    dispatcher, channel = make(["n"])
    result = dispatcher.dispatch(
        call("exec_command", program="git", args=["commit", "-m", "x"])
    )

    assert result.status is ToolStatus.REJECTED
    assert len(channel.presented) == 1


def test_unattended_command_reports_denied_unattended(workspace: Workspace) -> None:
    """No channel means unattended, which is its own status."""
    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(None),
        executor=Executor(workspace),
        agent_name="byte",
        allowed_tools=("exec_command",),
    )
    result = dispatcher.dispatch(
        call("exec_command", program="git", args=["commit", "-m", "x"])
    )

    assert result.status is ToolStatus.DENIED_UNATTENDED


@pytest.mark.parametrize(
    "program,args",
    [
        ("sudo", ["ls"]),
        ("git", ["push", "--force"]),
        ("git", ["reset", "--hard"]),
        ("git", ["checkout", "--", "."]),
        ("git", ["branch", "-D", "main"]),
        ("pip", ["install", "requests"]),
        ("env", []),
        ("systemctl", ["restart", "x"]),
        ("mkfs.ext4", ["/dev/sda"]),
        ("rm", ["-rf", "*"]),
    ],
)
def test_denied_commands_are_denied_and_never_prompt(make, program, args) -> None:
    """Every hard-deny class reaches ``denied`` with nobody asked.

    A denial is not approvable, so the channel being untouched is the property
    that matters -- not just the status.
    """
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("exec_command", program=program, args=args))

    assert result.status is ToolStatus.DENIED, f"{program} {args}: {result.detail}"
    assert channel.presented == []


def test_unknown_program_requires_approval(make) -> None:
    """An unrecognised program is gated by the default-ask baseline."""
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("exec_command", program="frobnicate"))

    assert result.status is not ToolStatus.DENIED
    assert len(channel.presented) == 1


def test_workspace_local_executable_requires_approval(make, workspace) -> None:
    """`./scripts/check.sh` is repository code and is gated."""
    script = workspace.root / "scripts" / "check.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")

    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("exec_command", program="./scripts/check.sh"))

    assert result.status is not ToolStatus.DENIED
    assert len(channel.presented) == 1


def test_external_absolute_executable_is_denied(make) -> None:
    """`/tmp/evil-tool` is outside the workspace and outside every trusted root."""
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("exec_command", program="/tmp/evil-tool"))

    assert result.status is ToolStatus.DENIED
    assert channel.presented == []


def test_trusted_system_executable_classifies_normally(make) -> None:
    """`/usr/bin/git status` is the same verdict as `git status`."""
    dispatcher, _ = make()
    bare = dispatcher.dispatch(call("exec_command", program="git", args=["status"]))
    full = dispatcher.dispatch(
        call("exec_command", program="/usr/bin/git", args=["status"])
    )

    assert bare.status is full.status


def test_cwd_outside_workspace_is_denied(make, tmp_path: pathlib.Path) -> None:
    """The model cannot move the run to another directory."""
    dispatcher, _ = make()
    result = dispatcher.dispatch(
        call("exec_command", program="git", args=["status"], cwd=str(tmp_path))
    )

    assert result.status is ToolStatus.DENIED


def test_relative_cwd_resolves_against_the_workspace(make, workspace) -> None:
    """A relative cwd is relative to the workspace, not the shell's directory."""
    (workspace.root / "sub").mkdir()
    dispatcher, _ = make()
    result = dispatcher.dispatch(
        call("exec_command", program="pwd", args=[], cwd="sub")
    )

    assert result.status is not ToolStatus.DENIED


# --------------------------------------------------------------------------
# Approval binding: workspace vs cwd
#
# Regression coverage for a defect where `ApprovalRequest.from_command` bound
# `verdict.cwd` to the workspace field. The two coincide at the root and diverge
# in a subdirectory, so the approval fingerprint disagreed with the execution
# fingerprint and a correctly-approved operation was refused.
# --------------------------------------------------------------------------


def test_command_at_workspace_root_binds_the_root(make, workspace) -> None:
    """At the root, cwd and workspace coincide -- the common case still works."""
    dispatcher, _ = make(["y"])
    result = dispatcher.dispatch(call("exec_command", program="pwd"))

    assert result.status is not ToolStatus.DENIED
    assert result.status is not ToolStatus.INVALID_ARGUMENTS


def test_command_in_nested_cwd_executes(make, workspace) -> None:
    """The regression itself: a subdirectory command now runs.

    Before the fix this returned INVALID_APPROVAL with a fingerprint mismatch.
    """
    (workspace.root / "sub").mkdir()
    dispatcher, _ = make(["y"])
    result = dispatcher.dispatch(
        call("exec_command", program="/usr/bin/pwd", args=[], cwd="sub")
    )

    assert result.status is ToolStatus.OK, result.detail
    assert str(workspace.root / "sub") in result.content["stdout"]


def test_approval_fingerprint_matches_execution_request(workspace) -> None:
    """The two digests agree for a nested cwd.

    Compared directly rather than inferred from an execution outcome, so a
    future mismatch is reported as a digest disagreement rather than as an
    executor refusal.
    """
    from forge.tools.approval import ApprovalRequest
    from forge.tools.commands import Command, CommandClassifier
    from forge.tools.execution import ExecutionRequest

    sub = workspace.root / "sub"
    sub.mkdir()

    verdict = CommandClassifier(workspace).classify(
        Command(program="pwd", args=(), cwd=sub)
    )

    request = ApprovalRequest.from_command(
        verdict, agent="byte", workspace=workspace.root
    )
    approved_digest = request.execution_fingerprint()

    execution = ExecutionRequest(program="pwd", args=(), cwd=sub, agent="byte")
    requested_digest = execution.fingerprint(workspace.root, sub)

    assert approved_digest == requested_digest


def test_changing_cwd_after_approval_still_fails(workspace) -> None:
    """The binding is real: moving the cwd invalidates the approval.

    This is the property that would break if the fix had been to *relax* the
    digest rather than to bind the correct fields.
    """
    from forge.tools.approval import ApprovalRequest
    from forge.tools.commands import Command, CommandClassifier
    from forge.tools.execution import ExecutionRequest

    sub = workspace.root / "sub"
    other = workspace.root / "other"
    sub.mkdir()
    other.mkdir()

    verdict = CommandClassifier(workspace).classify(
        Command(program="pwd", args=(), cwd=sub)
    )
    request = ApprovalRequest.from_command(
        verdict, agent="byte", workspace=workspace.root
    )
    approved_digest = request.execution_fingerprint()

    moved = ExecutionRequest(program="pwd", args=(), cwd=other, agent="byte")
    assert moved.fingerprint(workspace.root, other) != approved_digest


def test_changing_workspace_after_approval_still_fails(tmp_path) -> None:
    """A different workspace root invalidates the approval too.

    Both fields enter the digest independently, so neither can be substituted
    for the other without detection.
    """
    from forge.tools.approval import ApprovalRequest
    from forge.tools.commands import Command, CommandClassifier
    from forge.tools.execution import ExecutionRequest

    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    (first_root / "sub").mkdir(parents=True)
    (second_root / "sub").mkdir(parents=True)

    first = Workspace(first_root)
    verdict = CommandClassifier(first).classify(
        Command(program="pwd", args=(), cwd=first_root / "sub")
    )
    request = ApprovalRequest.from_command(
        verdict, agent="byte", workspace=first.root
    )
    approved_digest = request.execution_fingerprint()

    # Same cwd path relative to a different workspace root.
    second = Workspace(second_root)
    moved = ExecutionRequest(
        program="pwd", args=(), cwd=second_root / "sub", agent="byte"
    )
    assert moved.fingerprint(second.root, second_root / "sub") != approved_digest


def test_workspace_field_is_the_root_not_the_cwd(workspace) -> None:
    """The approval records the workspace root separately from the cwd.

    Both are prompt-visible and both enter the digest, so conflating them would
    also misreport where an operation runs.
    """
    from forge.tools.approval import ApprovalRequest
    from forge.tools.commands import Command, CommandClassifier

    sub = workspace.root / "sub"
    sub.mkdir()

    verdict = CommandClassifier(workspace).classify(
        Command(program="pwd", args=(), cwd=sub)
    )
    request = ApprovalRequest.from_command(
        verdict, agent="byte", workspace=workspace.root
    )

    assert request.workspace == workspace.root
    assert request.cwd == sub
    assert request.workspace != request.cwd


def test_workspace_cannot_be_omitted_from_a_command_approval() -> None:
    """Both approval entry points require the workspace root explicitly.

    This replaces a test that asserted the *fallback* worked. The fallback is
    gone: `workspace` was optional and defaulted to `verdict.cwd`, which is
    correct for a root-level command and wrong for a nested one, so a caller that
    forgot the argument got an approval that was silently mis-bound and could
    never be spent. A loud `TypeError` at the call site is the better failure.

    Structural rather than behavioural on purpose: no call can succeed without
    passing the argument, so the assertion is made by inspecting the signatures
    rather than by catching an exception from one particular invocation.
    """
    import inspect

    from forge.tools.approval import ApprovalRequest, Approver

    for target in (
        ApprovalRequest.from_command.__func__,  # type: ignore[attr-defined]
        Approver.submit_command,
    ):
        signature = inspect.signature(target)
        parameter = signature.parameters["workspace"]

        assert parameter.default is inspect.Parameter.empty, (
            f"{target.__qualname__} gives workspace a default; a caller that "
            "omits it gets a mis-bound approval instead of a TypeError"
        )
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    # And the annotation forbids the value the old fallback used.
    assert "None" not in str(
        inspect.signature(ApprovalRequest.from_command.__func__).parameters["workspace"].annotation  # type: ignore[attr-defined]
    )


def test_execution_result_is_reported_faithfully(make, workspace) -> None:
    """A failing command is ``ok`` with a non-zero exit code, not a failure.

    The child ran. Reporting that as an error would make a failing test suite
    look like a broken tool, which is exactly the confusion Block G warns about.
    """
    dispatcher, _ = make(["y"])
    result = dispatcher.dispatch(
        call("exec_command", program="/usr/bin/false")
    )

    assert result.status is ToolStatus.OK
    assert result.content["exit_code"] != 0


def test_program_token_is_used_verbatim(make) -> None:
    """The exact supplied program reaches execution, not a normalised form.

    The approval fingerprint binds the exact program token, so a substituted
    basename would break the binding and surface as a confusing
    ``INVALID_APPROVAL`` rather than a clear answer.
    """
    dispatcher, _ = make(["y"])
    result = dispatcher.dispatch(
        call("exec_command", program="/usr/bin/printf", args=["%s", "hi"])
    )

    assert result.status is ToolStatus.OK
    assert "hi" in result.content["stdout"]


# --------------------------------------------------------------------------
# Filesystem tools: classified before any read
# --------------------------------------------------------------------------


def test_read_file_returns_content(make, workspace) -> None:
    (workspace.root / "a.txt").write_text("line one\nline two\n")
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("read_file", path="a.txt"))

    assert result.status is ToolStatus.OK
    assert "line one" in result.content["content"]


def test_read_file_outside_workspace_is_denied(make, tmp_path: pathlib.Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_text("nope\n")

    dispatcher, _ = make()
    result = dispatcher.dispatch(call("read_file", path=str(secret)))

    assert result.status is ToolStatus.DENIED
    assert "nope" not in json.dumps(result.content)


def test_read_file_of_structural_secret_is_denied_unread(make, workspace) -> None:
    """`.env` is denied, and its bytes never reach the result."""
    (workspace.root / ".env").write_text("TOKEN=leaked\n")
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("read_file", path=".env"))

    assert result.status is ToolStatus.DENIED
    assert "leaked" not in json.dumps(result.content)
    assert channel.presented == []


def test_read_file_of_name_guess_secret_requires_approval(make, workspace) -> None:
    """`secrets.json` is a name guess: approval, not denial."""
    (workspace.root / "secrets.json").write_text('{"api_key": "x"}\n')
    dispatcher, channel = make(["y"])
    result = dispatcher.dispatch(call("read_file", path="secrets.json"))

    assert len(channel.presented) == 1
    assert result.status is ToolStatus.OK


def test_read_file_line_range_is_bounded(make, workspace) -> None:
    (workspace.root / "big.txt").write_text("\n".join(str(i) for i in range(500)))
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("read_file", path="big.txt", offset=10, limit=5))

    assert result.content["start_line"] == 10
    assert len(result.content["content"].splitlines()) == 5


def test_write_file_creates_and_replaces(make, workspace) -> None:
    dispatcher, _ = make()
    first = dispatcher.dispatch(call("write_file", path="n.txt", content="one"))
    assert first.status is ToolStatus.OK

    second = dispatcher.dispatch(call("write_file", path="n.txt", content="two"))
    assert second.status is ToolStatus.OK
    assert (workspace.root / "n.txt").read_text() == "two"


def test_write_file_outside_workspace_is_denied(make, tmp_path: pathlib.Path) -> None:
    dispatcher, _ = make()
    result = dispatcher.dispatch(
        call("write_file", path=str(tmp_path / "x.txt"), content="no")
    )

    assert result.status is ToolStatus.DENIED
    assert not (tmp_path / "x.txt").exists()


def test_patch_file_replaces_exact_region(make, workspace) -> None:
    (workspace.root / "code.py").write_text("alpha\nbeta\ngamma\n")
    dispatcher, _ = make()
    result = dispatcher.dispatch(
        call("patch_file", path="code.py", old="beta", new="BETA")
    )

    assert result.status is ToolStatus.OK
    assert (workspace.root / "code.py").read_text() == "alpha\nBETA\ngamma\n"


def test_patch_file_requires_a_unique_region(make, workspace) -> None:
    (workspace.root / "dup.txt").write_text("x\nx\n")
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("patch_file", path="dup.txt", old="x", new="y"))

    assert result.status is ToolStatus.ERROR
    assert "appears 2 times" in result.detail


def test_patch_file_classifies_before_reading(make, workspace) -> None:
    """A secret file is denied, and its content never informed the decision.

    If the dispatcher read first and classified after, the approval path would
    have been reached with the content already in memory.
    """
    (workspace.root / ".env").write_text("SECRET=leaked\n")
    dispatcher, _ = make(["y"])
    result = dispatcher.dispatch(
        call("patch_file", path=".env", old="leaked", new="leaked2")
    )

    assert result.status is ToolStatus.DENIED
    assert "leaked" not in json.dumps(result.content)


def test_search_files_finds_by_glob(make, workspace) -> None:
    (workspace.root / "x.py").write_text("")
    (workspace.root / "y.txt").write_text("")
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("search_files", pattern="*.py"))

    assert result.status is ToolStatus.OK
    assert any(m.endswith("x.py") for m in result.content["matches"])


def test_search_content_matches_regex(make, workspace) -> None:
    (workspace.root / "s.py").write_text("import os\nvalue = 1\n")
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("search_content", regex=r"import \w+"))

    assert result.status is ToolStatus.OK
    assert result.content["count"] >= 1


def test_search_content_invalid_regex_is_reported(make) -> None:
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("search_content", regex="([unclosed"))

    assert result.status is ToolStatus.INVALID_ARGUMENTS


# --------------------------------------------------------------------------
# Dispatch-level behaviour
# --------------------------------------------------------------------------


def test_unknown_tool_name(make) -> None:
    dispatcher, _ = make()
    result = dispatcher.dispatch(call("no_such_tool"))

    assert result.status is ToolStatus.UNKNOWN_TOOL


def test_malformed_json_arguments_are_reported(make) -> None:
    dispatcher, _ = make()
    result = dispatcher.dispatch(ToolCall(id="c", name="read_file", arguments="{oops"))

    assert result.status is ToolStatus.INVALID_ARGUMENTS


def test_missing_required_argument_names_it(make) -> None:
    dispatcher, _ = make()
    result = dispatcher.dispatch(ToolCall(id="c", name="read_file", arguments="{}"))

    assert result.status is ToolStatus.INVALID_ARGUMENTS
    assert "path" in result.detail


def test_tool_not_in_allowed_set(make, workspace) -> None:
    """An agent may be configured narrower, and the registry still holds the tool."""
    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel([])),
        executor=Executor(workspace),
        agent_name="narrow",
        allowed_tools=("read_file",),
    )
    result = dispatcher.dispatch(call("exec_command", program="git", args=["status"]))

    assert result.status is ToolStatus.TOOL_NOT_ALLOWED


def test_denial_cache_returns_the_same_result(make) -> None:
    dispatcher, channel = make(["n"])
    first = dispatcher.dispatch(
        call("exec_command", program="git", args=["push", "origin", "main"])
    )
    second = dispatcher.dispatch(
        call("exec_command", program="git", args=["push", "origin", "main"])
    )

    assert first.status is ToolStatus.REJECTED
    assert second is first  # the identical cached object
    assert len(channel.presented) == 1


def test_denial_cache_is_not_shared_across_different_calls(make) -> None:
    dispatcher, channel = make(["n", "n"])
    dispatcher.dispatch(call("exec_command", program="git", args=["push"]))
    dispatcher.dispatch(call("exec_command", program="git", args=["fetch"]))

    assert len(channel.presented) == 2


def test_argument_order_does_not_defeat_the_cache(make) -> None:
    """The cache keys on canonical JSON, so equivalent args are the same call."""
    dispatcher, channel = make(["n"])
    dispatcher.dispatch(
        ToolCall(
            id="a", name="exec_command",
            arguments=json.dumps({"program": "git", "args": ["push", "origin"]}),
        )
    )
    dispatcher.dispatch(
        ToolCall(
            id="b", name="exec_command",
            arguments=json.dumps({"args": ["push", "origin"], "program": "git"}),
        )
    )

    assert len(channel.presented) == 1


@pytest.mark.parametrize(
    "status",
    [
        ToolStatus.ERROR,
        ToolStatus.INVALID_ARGUMENTS,
        ToolStatus.UNKNOWN_TOOL,
        ToolStatus.TIMEOUT,
        ToolStatus.LAUNCH_FAILED,
    ],
)
def test_only_permission_refusals_are_cached(status: ToolStatus) -> None:
    """Other failure classes are not refusals and must stay retryable."""
    assert status.is_permission_refusal is False


def test_permission_refusal_statuses_are_cached() -> None:
    for status in (
        ToolStatus.DENIED,
        ToolStatus.REJECTED,
        ToolStatus.DENIED_UNATTENDED,
    ):
        assert status.is_permission_refusal is True


def test_result_serialises_to_json(make, workspace) -> None:
    (workspace.root / "a.txt").write_text("x\n")
    dispatcher, _ = make()
    payload = dispatcher.dispatch(call("read_file", path="a.txt")).to_json()

    decoded = json.loads(payload)
    assert decoded["tool"] == "read_file"
    assert decoded["status"] == "ok"


def test_offered_tools_are_only_the_allowed_subset(workspace) -> None:
    dispatcher = ToolDispatcher(
        workspace,
        approver=Approver(ScriptedApprovalChannel([])),
        executor=Executor(workspace),
        agent_name="narrow",
        allowed_tools=("read_file", "search_files"),
    )
    assert {spec.name for spec in dispatcher.tool_specs()} == {
        "read_file",
        "search_files",
    }


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------


def test_validate_reports_the_offending_field() -> None:
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 1}},
        "required": ["n"],
    }

    assert "n must be an integer" == _validate(schema, {"n": "x"})
    assert "at least 1" in _validate(schema, {"n": 0})
    assert _validate(schema, {"n": 5}) == ""


def test_validate_reports_a_missing_required_field() -> None:
    schema = {"type": "object", "properties": {}, "required": ["path"]}
    assert "missing required argument: path" == _validate(schema, {})


def test_validate_ignores_unknown_keys() -> None:
    """Providers add bookkeeping fields; refusing those breaks valid calls."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert _validate(schema, {"a": "x", "provider_meta": 1}) == ""
