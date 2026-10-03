"""Tests for the execution layer.

Two things are under test, and the second matters more than the first.

The first is behaviour: an approved command runs, a mismatched one does not,
output is bounded while the child runs, and a timeout kills the whole process
tree. Every command here is `sys.executable` running a harmless snippet, or a
small script written into a temporary workspace. Nothing destructive, and
nothing outside `tmp_path`.

The second is architecture. The executor's entire claim is that it holds no
policy and cannot acquire power it was not given. That claim decays silently
unless something checks it, so the guards at the bottom assert that the
executor never classifies, never approves, cannot be reached without a claimed
approval, and never passes a shell.

A note on timing: tests that launch a subprocess are slower than the rest of the
suite and a few deliberately involve a timeout. They use `sys.executable`
rather than a shell builtin so the assertions hold on any host.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from forge.tools.approval import (
    ApprovalDecision,
    ApprovalOutcome,
    Approver,
    ScriptedApprovalChannel,
)
from forge.tools.commands import Command, CommandClassifier
from forge.tools.execution import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTABLE_SEARCH_PATH,
    HARD_MAX_OUTPUT_BYTES,
    MAX_TIMEOUT_SECONDS,
    Executor,
    ExecutionRequest,
    ExecutionState,
)
from forge.tools.workspace import Workspace

# A program the classifier treats as ordinary and the executor can definitely
# resolve, *and* that lives in a trusted executable root.
#
# This used to be `sys.executable`, which is an absolute path so it also
# exercised the absolute-token case. The permission model now denies an absolute
# executable outside the workspace unless it is in a trusted system root, and a
# virtualenv interpreter lives in neither -- so `sys.executable` is denied here
# by `cmd.workspace_executable_outside`. That denial is correct: the Executor's
# fixed PATH does not include a virtualenv, so it could not launch one either.
#
# `/usr/bin/python3` is the interpreter the Executor resolves on a search-path
# hit, so the tests exercise the same launch the product performs. The
# absolute-token case is still covered, in
# `tests/test_commands.py::test_system_path_program_is_not_treated_as_workspace_executable`.
PY = "/usr/bin/python3"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace_dir(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture()
def workspace(workspace_dir: Path) -> Workspace:
    return Workspace(workspace_dir)


@pytest.fixture()
def classifier(workspace: Workspace) -> CommandClassifier:
    return CommandClassifier(workspace)


@pytest.fixture()
def executor(workspace: Workspace) -> Executor:
    return Executor(workspace)


def approve(
    classifier: CommandClassifier,
    *args: str,
    program: str = PY,
    agent: str = "tester",
    approve_it: bool = True,
) -> ApprovalOutcome:
    """Classify and approve a command, the way a real caller would.

    ``workspace`` is taken from the classifier rather than passed separately,
    because the classifier was constructed with the very workspace the executor
    will launch in. Supplying the wrong root here would produce an approval that
    cannot be spent, which is the binding defect this argument exists to prevent.
    """
    verdict = classifier.classify(Command(program, args))
    channel = ScriptedApprovalChannel(["y" if approve_it else "n"])
    return Approver(channel).submit_command(
        verdict, agent=agent, workspace=classifier.workspace.root
    )


def request_for(
    *args: str, program: str = PY, agent: str = "tester", **kw: object
) -> ExecutionRequest:
    return ExecutionRequest(  # type: ignore[arg-type]
        program=program, args=tuple(args), agent=agent, **kw
    )


def run(
    executor: Executor,
    classifier: CommandClassifier,
    *args: str,
    agent: str = "tester",
    **kw: object,
):
    """The normal path: classify, approve, execute."""
    outcome = approve(classifier, *args, agent=agent)
    return executor.execute(outcome, request_for(*args, agent=agent, **kw))


# ==========================================================================
# Approved commands execute
# ==========================================================================


def test_approved_command_executes(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "print('hello')")

    assert result.state is ExecutionState.COMPLETED
    assert result.exit_code == 0
    assert result.stdout.strip() == "hello"


def test_completed_reports_ran(executor: Executor, classifier: CommandClassifier) -> None:
    assert run(executor, classifier, "-c", "print(1)").ran is True


def test_non_zero_exit_is_failed_not_an_error(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A failing test suite is a normal result to report, not an exception."""
    result = run(executor, classifier, "-c", "import sys; sys.exit(3)")

    assert result.state is ExecutionState.FAILED
    assert result.exit_code == 3
    assert result.ran is True


def test_large_exit_code_survives(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "import sys; sys.exit(42)")

    assert result.exit_code == 42


def test_stdout_and_stderr_are_separate(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(
        executor,
        classifier,
        "-c",
        "import sys; sys.stderr.write('ERR'); print('OUT')",
    )

    assert result.stdout.strip() == "OUT"
    assert result.stderr.strip() == "ERR"
    assert "ERR" not in result.stdout
    assert "OUT" not in result.stderr


def test_argv_is_passed_exactly(executor: Executor, classifier: CommandClassifier) -> None:
    """Arguments reach the child byte for byte, boundaries intact.

    `["-c", "print('a b')"]` is one argument containing a space, and must not
    arrive split into two.
    """
    result = run(executor, classifier, "-c", "import sys; print(repr(sys.argv[1:]))", "a b", "c")

    assert "'a b'" in result.stdout
    assert "'c'" in result.stdout


def test_unicode_arguments_survive(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(executor, classifier, "-c", "import sys; print(sys.argv[1])", "café—ok")

    assert "café—ok" in result.stdout


def test_glob_characters_are_not_expanded(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """No shell, so `*` is a literal argument.

    This is the intended consequence of argv execution, and it is why the
    classifier can deny a destructive wildcard delete rather than having Forge
    expand it on the agent's behalf.
    """
    result = run(executor, classifier, "-c", "import sys; print(sys.argv[1])", "*")

    assert result.stdout.strip() == "*"


def test_executable_is_resolved_and_recorded(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(executor, classifier, "-c", "pass")

    assert result.resolved_program is not None
    assert os.path.isabs(result.resolved_program)


# ==========================================================================
# Refusals: nothing runs without the right approval
# ==========================================================================


def test_rejected_outcome_cannot_execute(
    executor: Executor, classifier: CommandClassifier
) -> None:
    outcome = approve(classifier, "-c", "print('should not run')", approve_it=False)
    result = executor.execute(outcome, request_for("-c", "print('should not run')"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert result.stdout == ""
    assert result.exit_code is None


@pytest.mark.parametrize(
    "decision",
    [
        ApprovalDecision.DENIED_BY_POLICY,
        ApprovalDecision.DENIED_UNATTENDED,
        ApprovalDecision.REJECTED,
    ],
)
def test_non_approved_decisions_cannot_execute(
    executor: Executor, classifier: CommandClassifier, decision: ApprovalDecision
) -> None:
    outcome = ApprovalOutcome(decision=decision, detail="no")
    result = executor.execute(outcome, request_for("-c", "print(1)"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert result.ran is False


def test_a_plain_approval_cannot_execute_without_a_request(
    executor: Executor,
) -> None:
    """A decision with no request behind it authorises nothing.

    Without a request there is no invocation to bind to, so there is nothing to
    compare against and nothing to prove.
    """
    outcome = ApprovalOutcome(decision=ApprovalDecision.APPROVED, detail="trust me")
    result = executor.execute(outcome, request_for("-c", "print(1)"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert result.ran is False


def test_approval_cannot_be_reused(executor: Executor, classifier: CommandClassifier) -> None:
    outcome = approve(classifier, "-c", "print('once')")
    request = request_for("-c", "print('once')")

    first = executor.execute(outcome, request)
    second = executor.execute(outcome, request)

    assert first.state is ExecutionState.COMPLETED
    assert second.state is ExecutionState.INVALID_APPROVAL
    assert "claimed" in (second.error or "")


def test_two_executors_cannot_spend_one_approval(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """Single-use is intrinsic to the approval, not to any one executor.

    A per-executor spent-set would let a second executor launch the same
    approved command again. The claim lives in the outcome precisely so this
    cannot happen.
    """
    outcome = approve(classifier, "-c", "print('once')")
    request = request_for("-c", "print('once')")
    first_executor = Executor(workspace)
    second_executor = Executor(workspace)

    first = first_executor.execute(outcome, request)
    second = second_executor.execute(outcome, request)

    assert first.state is ExecutionState.COMPLETED
    assert second.state is ExecutionState.INVALID_APPROVAL


def test_concurrent_execution_across_two_executors_launches_once(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    outcome = approve(classifier, "-c", "print('once')")
    request = request_for("-c", "print('once')")
    executors = [Executor(workspace), Executor(workspace)]
    results: list[ExecutionState] = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def attempt(index: int) -> None:
        barrier.wait()
        state = executors[index % 2].execute(outcome, request).state
        with lock:
            results.append(state)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = Counter(results)
    assert counts[ExecutionState.COMPLETED] == 1
    assert counts[ExecutionState.INVALID_APPROVAL] == 11


# ==========================================================================
# Approval binding: the approved operation is the executed operation
# ==========================================================================


def test_command_mismatch_cannot_execute(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """An approval for `build` does not authorise `src`.

    The whole point of binding: the caller is not trusted to say the two match.
    """
    outcome = approve(classifier, "-c", "print('build')")
    result = executor.execute(outcome, request_for("-c", "print('src')"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert "does not match" in (result.error or "")


def test_extra_argument_cannot_execute(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """One extra argument is a different operation, not the same one."""
    outcome = approve(classifier, "-c", "print('x')")
    result = executor.execute(outcome, request_for("-c", "print('x')", "--extra"))

    assert result.state is ExecutionState.INVALID_APPROVAL


def test_token_boundary_matters_not_just_content(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """`["a b"]` and `["a", "b"]` are different operations.

    They would render identically in the human-readable detail string, which is
    exactly why binding cannot be built from it.
    """
    outcome = approve(classifier, "-c", "import sys; print(repr(sys.argv[1:]))", "a b")
    result = executor.execute(outcome, request_for("-c", "import sys; print(repr(sys.argv[1:]))", "a", "b"))

    assert result.state is ExecutionState.INVALID_APPROVAL


def test_workspace_mismatch_cannot_execute(
    tmp_path: Path, classifier: CommandClassifier
) -> None:
    """An approval bound to one workspace does not authorise another."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_executor = Executor(Workspace(first))
    outcome = approve(classifier, "-c", "print('x')")  # approved in `first`
    second_executor = Executor(Workspace(second))

    result = second_executor.execute(outcome, request_for("-c", "print('x')"))

    assert result.state is ExecutionState.INVALID_APPROVAL


def test_agent_mismatch_cannot_execute(
    executor: Executor, classifier: CommandClassifier
) -> None:
    outcome = approve(classifier, "-c", "print('x')", agent="worker-a")
    result = executor.execute(outcome, request_for("-c", "print('x')", agent="worker-b"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert "requester mismatch" in (result.error or "")


def test_shell_string_approval_cannot_be_spent_by_argv(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A shell string is not an argv invocation and cannot be bound as one."""
    verdict = classifier.classify(Command("bash", ("-c", "echo hi"), shell=True))
    outcome = Approver(ScriptedApprovalChannel(["y"])).submit_command(
        verdict, agent="t", workspace=classifier.workspace.root
    )

    result = executor.execute(outcome, request_for("-c", "print('x')", agent="t"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert "shell" in (result.error or "")


def test_fingerprint_is_stable_for_identical_input(
    workspace: Workspace,
) -> None:
    first = request_for("-c", "print('x')").fingerprint(workspace.root, workspace.root)
    second = request_for("-c", "print('x')").fingerprint(workspace.root, workspace.root)

    assert first == second


def test_fingerprint_changes_with_program(workspace: Workspace) -> None:
    a = ExecutionRequest(program="ls").fingerprint(workspace.root, workspace.root)
    b = ExecutionRequest(program="rm").fingerprint(workspace.root, workspace.root)

    assert a != b


def test_fingerprint_distinguishes_absolute_from_bare_program(
    workspace: Workspace,
) -> None:
    """`/usr/bin/rm` and `rm` are different operations.

    The classifier lowercases a basename to match rules against, but binding on
    that would make two different files one approval.
    """
    a = ExecutionRequest(program="/usr/bin/rm").fingerprint(workspace.root, workspace.root)
    b = ExecutionRequest(program="rm").fingerprint(workspace.root, workspace.root)

    assert a != b


def test_fingerprint_changes_with_cwd(workspace: Workspace) -> None:
    request = request_for("-c", "print('x')")
    a = request.fingerprint(workspace.root, workspace.root)
    b = request.fingerprint(workspace.root, workspace.root / "sub")

    assert a != b


def test_fingerprint_contains_no_environment_material(workspace: Workspace) -> None:
    """The digest is a public identifier, so it must carry nothing secret.

    There is no environment in v1's binding by design; this asserts the property
    rather than the current implementation, so a future binding change that
    added a secret would fail here.
    """
    fingerprint = request_for("-c", "print('x')").fingerprint(workspace.root, workspace.root)

    assert "SECRET" not in fingerprint
    assert fingerprint != ""


# ==========================================================================
# Invalid requests
# ==========================================================================


def test_timeout_above_default_is_invalid_request(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A request may only narrow, never widen.

    Silently clamping would run the command for less time than asked while
    reporting success, which is a bounded-but-wrong answer.
    """
    outcome = approve(classifier, "-c", "print('x')")
    result = executor.execute(
        outcome, request_for("-c", "print('x')", timeout=DEFAULT_TIMEOUT_SECONDS + 1)
    )

    assert result.state is ExecutionState.INVALID_REQUEST
    assert "may only lower" in (result.error or "")


@pytest.mark.parametrize("bad", [0, -1, -0.5, float("nan"), float("inf")])
def test_invalid_timeout_values_are_rejected(
    executor: Executor, classifier: CommandClassifier, bad: float
) -> None:
    outcome = approve(classifier, "-c", "print('x')")
    result = executor.execute(outcome, request_for("-c", "print('x')", timeout=bad))

    assert result.state is ExecutionState.INVALID_REQUEST


def test_empty_program_is_invalid_request(
    executor: Executor, classifier: CommandClassifier
) -> None:
    outcome = approve(classifier, "-c", "print('x')")
    result = executor.execute(
        outcome, ExecutionRequest(program="", args=("-c", "print('x')"), agent="tester")
    )

    assert result.state is ExecutionState.INVALID_REQUEST


def test_validation_failure_does_not_spend_the_approval(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A caller bug must not quietly spend the operator's yes.

    The claim happens after every check, so a malformed request leaves the
    approval usable for the operation that was actually approved.
    """
    outcome = approve(classifier, "-c", "print('x')")

    rejected = executor.execute(
        outcome, request_for("-c", "print('x')", timeout=-5)
    )

    assert rejected.state is ExecutionState.INVALID_REQUEST
    assert outcome.claimed is False
    assert executor.execute(outcome, request_for("-c", "print('x')")).state is (
        ExecutionState.COMPLETED
    )


def test_invalid_request_is_distinct_from_invalid_approval(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A caller can tell "you may not" from "that was malformed"."""
    outcome = approve(classifier, "-c", "print('x')")

    bad_input = executor.execute(outcome, request_for("-c", "print('x')", timeout=-1))
    bad_capability = executor.execute(outcome, request_for("-c", "print('other')"))

    assert bad_input.state is ExecutionState.INVALID_REQUEST
    assert bad_capability.state is ExecutionState.INVALID_APPROVAL


# ==========================================================================
# Launch failures
# ==========================================================================


def test_missing_program_is_launch_failed(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """A program that cannot be resolved never starts a process.

    Distinct from a non-zero exit: nothing ran, so there is no child exit code
    to report.
    """
    missing = "forge-test-no-such-program"
    outcome = approve(classifier, program=missing)
    result = Executor(workspace).execute(outcome, request_for(program=missing))

    assert result.state is ExecutionState.LAUNCH_FAILED
    assert result.exit_code is None
    assert result.ran is False
    assert "not found" in (result.error or "")


def test_launch_failure_still_spends_the_approval(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """One approval authorises one launch attempt, not one success.

    A retry after the environment changed -- the binary appeared, the workspace
    moved -- must go back through approval, because the executor cannot know
    what changed.
    """
    missing = "/usr/bin/forge-test-binary-does-not-exist"
    # Approve the program actually being requested, so the failure under test is
    # the launch and not (correctly) a binding mismatch. The path is inside a
    # trusted executable root so it classifies normally: an arbitrary external
    # path would be denied by the permission model before reaching the
    # executor, which is a different failure than the one under test.
    outcome = approve(classifier, program=missing)
    request = request_for(program=missing)

    first = Executor(workspace).execute(outcome, request)
    assert first.state is ExecutionState.LAUNCH_FAILED
    assert outcome.claimed is True

    second = Executor(workspace).execute(outcome, request)
    assert second.state is ExecutionState.INVALID_APPROVAL
    assert "claimed" in (second.error or "")


# ==========================================================================
# Environment
# ==========================================================================


def test_child_environment_is_exactly_the_forge_set(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(
        executor,
        classifier,
        "-c",
        "import os; print(','.join(sorted(os.environ)))",
    )

    assert result.stdout.strip() == "HOME,LANG,LC_ALL,PATH,PWD"


def test_parent_environment_is_not_inherited(
    executor: Executor, classifier: CommandClassifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentinel in Forge's own environment must not reach the child.

    The parent environment of a Forge host holds whatever the operator's shell
    had. Inheriting it would hand every executed command every secret the
    session held.
    """
    monkeypatch.setenv("FORGE_TEST_SENTINEL", "must-not-appear")

    result = run(
        executor,
        classifier,
        "-c",
        "import os; print(os.environ.get('FORGE_TEST_SENTINEL', 'absent'))",
    )

    assert result.stdout.strip() == "absent"


def test_home_points_at_the_workspace(
    executor: Executor, classifier: CommandClassifier, workspace_dir: Path
) -> None:
    result = run(executor, classifier, "-c", "import os; print(os.environ['HOME'])")

    assert result.stdout.strip() == str(workspace_dir)


def test_path_is_the_fixed_minimal_path(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(executor, classifier, "-c", "import os; print(os.environ['PATH'])")

    assert result.stdout.strip() == EXECUTABLE_SEARCH_PATH


def test_pwd_is_the_resolved_cwd(
    executor: Executor, classifier: CommandClassifier, workspace_dir: Path
) -> None:
    result = run(executor, classifier, "-c", "import os; print(os.environ['PWD'])")

    assert result.stdout.strip() == str(workspace_dir)


def test_execution_request_has_no_environment_fields() -> None:
    """Not present-and-ignored: absent.

    There is no argument through which a caller could pass PYTHONPATH or
    LD_PRELOAD, which is the point of narrowing v1.
    """
    fields = ExecutionRequest.__dataclass_fields__

    assert "env" not in fields
    assert "env_keys" not in fields
    assert "stdin" not in fields


def test_stdin_is_closed(executor: Executor, classifier: CommandClassifier) -> None:
    """The child sees EOF immediately rather than waiting for input."""
    result = run(
        executor,
        classifier,
        "-c",
        "import sys; print(repr(sys.stdin.read()))",
    )

    assert result.stdout.strip() == "''"


# ==========================================================================
# Bounded output
# ==========================================================================


def test_small_output_is_complete(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "print('a' * 100)")

    assert result.stdout_truncated is False
    assert len(result.stdout) >= 100


def test_output_over_the_cap_is_truncated_and_flagged(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    small = Executor(workspace, default_timeout=30, max_output_bytes=4096)
    result = run(small, classifier, "-c", "print('a' * 100000)")

    assert result.stdout_truncated is True
    assert len(result.stdout) <= 4096


def test_truncation_keeps_the_head(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """A traceback's useful part is at the top.

    Keeping the tail would mean holding everything to find it.
    """
    small = Executor(workspace, default_timeout=30, max_output_bytes=1024)
    result = run(small, classifier, "-c", "print('HEADMARKER'); print('b' * 50000)")

    assert "HEADMARKER" in result.stdout
    assert "b" * 2000 not in result.stdout


def test_bytes_seen_is_reported_separately_from_retained(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """A reviewer can tell a quiet run from a noisy one without the content."""
    small = Executor(workspace, default_timeout=30, max_output_bytes=4096)
    result = run(small, classifier, "-c", "print('a' * 100000)")

    # Total seen, not retained: a reviewer learns the run was noisy without the
    # record holding what it said.
    assert result.stdout_bytes == 100001
    assert len(result.stdout) == 4096


def test_streams_are_capped_independently(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    small = Executor(workspace, default_timeout=30, max_output_bytes=4096)
    result = run(
        small,
        classifier,
        "-c",
        "import sys; sys.stdout.write('o'*50000); sys.stderr.write('e'*100)",
    )

    assert result.stdout_truncated is True
    assert result.stderr_truncated is False
    assert len(result.stderr) == 100


def test_large_output_on_both_streams_does_not_deadlock(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    """The property the drainer exists for.

    A drainer that stopped reading at the cap would let the child fill the pipe
    buffer and block forever, turning a truncation into a hang. Ten megabytes on
    each stream must complete, retain the cap, and report the true totals.
    """
    big = Executor(workspace, default_timeout=60, max_output_bytes=64 * 1024)
    program = (
        "import sys; "
        "sys.stdout.buffer.write(b'o' * 10_000_000); "
        "sys.stderr.buffer.write(b'e' * 10_000_000)"
    )

    started = time.monotonic()
    result = run(big, classifier, "-c", program, timeout=50.0)
    elapsed = time.monotonic() - started

    assert result.state is ExecutionState.COMPLETED
    assert result.exit_code == 0
    assert result.stdout_bytes == 10_000_000
    assert result.stderr_bytes == 10_000_000
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout) <= 64 * 1024
    assert len(result.stderr) <= 64 * 1024
    # A deadlock would show up as the timeout, not as a fast return.
    assert elapsed < 45


def test_default_cap_is_one_mebibyte() -> None:
    assert DEFAULT_MAX_OUTPUT_BYTES == 1024 * 1024


def test_cap_above_hard_maximum_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ValueError):
        Executor(workspace, max_output_bytes=HARD_MAX_OUTPUT_BYTES + 1)


def test_invalid_capture_cap_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ValueError):
        Executor(workspace, max_output_bytes=0)


# ==========================================================================
# Timeout and process tree
# ==========================================================================


def test_fast_command_is_not_timed_out(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "print('quick')", timeout=30.0)

    assert result.state is ExecutionState.COMPLETED
    assert result.terminated is False


def test_slow_command_times_out(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "import time; time.sleep(30)", timeout=1.0)

    assert result.state is ExecutionState.TIMED_OUT
    assert result.terminated is True
    # Forge killed it, so there is no meaningful child exit code to report.
    assert result.exit_code is None


def test_timed_out_process_is_actually_dead(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """Not just "reported as timed out" -- the process is gone."""
    marker = "import time; time.sleep(3); open('STILL_ALIVE', 'w').close()"
    result = run(executor, classifier, "-c", marker, timeout=1.0)

    assert result.state is ExecutionState.TIMED_OUT
    time.sleep(4)
    assert not (executor.workspace.root / "STILL_ALIVE").exists()


def test_timeout_kills_grandchildren(
    workspace_dir: Path, workspace: Workspace, classifier: CommandClassifier
) -> None:
    """The whole tree is stopped, not just the direct child.

    Signalling only the child would leave `spawner`'s own child running, holding
    the workspace and a process slot indefinitely. start_new_session makes the
    child a process-group leader, so one killpg reaches the whole tree.
    """
    sentinel = workspace_dir / "GRANDCHILD_SURVIVED"
    spawner = workspace_dir / "spawner.py"
    spawner.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c",
                "import time, pathlib; time.sleep(4); pathlib.Path({str(sentinel)!r}).write_text('alive')"])
            time.sleep(30)
            """
        )
    )

    outcome = approve(classifier, "spawner.py")
    result = Executor(workspace, default_timeout=30).execute(
        outcome, ExecutionRequest(program=PY, args=("spawner.py",), agent="tester", timeout=1.5)
    )

    assert result.state is ExecutionState.TIMED_OUT
    time.sleep(5)
    assert not sentinel.exists(), "a grandchild outlived the timeout"


def test_default_timeout_is_the_documented_value() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 120.0


def test_hard_maximum_timeout_is_the_documented_value() -> None:
    assert MAX_TIMEOUT_SECONDS == 900.0


def test_default_timeout_may_be_raised_within_the_ceiling(workspace: Workspace) -> None:
    """Trusted configuration can widen, up to the hard maximum."""
    executor = Executor(workspace, default_timeout=300.0)

    assert executor.default_timeout == 300.0


def test_default_timeout_above_the_ceiling_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ValueError):
        Executor(workspace, default_timeout=MAX_TIMEOUT_SECONDS + 1)


def test_default_timeout_above_max_timeout_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ValueError):
        Executor(workspace, default_timeout=200.0, max_timeout=100.0)


def test_lowering_the_timeout_is_honoured(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(executor, classifier, "-c", "import time; time.sleep(30)", timeout=0.5)

    assert result.state is ExecutionState.TIMED_OUT


# ==========================================================================
# Workspace revalidation
# ==========================================================================


def test_workspace_deleted_after_approval_is_refused(
    tmp_path: Path, classifier: CommandClassifier
) -> None:
    root = tmp_path / "vanishing"
    root.mkdir()
    executor = Executor(Workspace(root))

    outcome = approve(classifier, "-c", "print('x')")
    _rmtree(root)

    result = executor.execute(outcome, request_for("-c", "print('x')"))

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert result.ran is False


def test_cwd_outside_the_workspace_is_refused(
    tmp_path: Path, workspace_dir: Path, classifier: CommandClassifier
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    executor = Executor(Workspace(workspace_dir))

    outcome = approve(classifier, "-c", "print('x')")
    result = executor.execute(
        outcome, request_for("-c", "print('x')", cwd=outside)
    )

    assert result.state is ExecutionState.INVALID_APPROVAL


def test_target_replaced_by_an_outward_symlink_is_refused(
    tmp_path: Path, workspace_dir: Path, classifier: CommandClassifier
) -> None:
    """Symlink drift between approval and launch is caught for known targets.

    Containment at approval time and containment at launch are two different
    moments. A command whose target the classifier could name is re-proved at
    launch, so swapping it for a symlink out of the workspace refuses.
    """
    outside = tmp_path / "secrets-outside"
    outside.mkdir()
    (outside / "payload.txt").write_text("secret\n")

    target = workspace_dir / "data.txt"
    target.write_text("innocent\n")

    executor = Executor(Workspace(workspace_dir))
    outcome = approve(classifier, "data.txt", program="cat")
    assert outcome.request.targets, "the target must be known for this to test anything"

    target.unlink()
    target.symlink_to(outside / "payload.txt")

    result = executor.execute(
        outcome, request_for("data.txt", program="cat")
    )

    assert result.state is ExecutionState.INVALID_APPROVAL
    assert result.ran is False


def test_inline_code_has_no_enumerable_targets_to_revalidate(
    tmp_path: Path, workspace_dir: Path, classifier: CommandClassifier
) -> None:
    """The documented limit of v1 revalidation, asserted rather than implied.

    A `python -c` command names its paths *inside the source string*, which the
    classifier does not parse -- so it reports no targets, and there is nothing
    for the executor to re-prove at launch. That is not an oversight in the
    revalidation; it is the boundary of it, and the reason the module documents
    that this layer is not a sandbox and does not eliminate TOCTOU.
    """
    executor = Executor(Workspace(workspace_dir))
    outcome = approve(classifier, "-c", "print('anything')")

    assert outcome.request.targets == ()

    # The command still executes, because nothing was denied. This test exists
    # to record that limitation honestly: closing it needs sandboxing, not more
    # revalidation.
    result = executor.execute(outcome, request_for("-c", "print('anything')"))
    assert result.state is ExecutionState.COMPLETED


def test_execution_uses_the_approved_workspace(
    executor: Executor, classifier: CommandClassifier, workspace_dir: Path
) -> None:
    result = run(executor, classifier, "-c", "import os; print(os.getcwd())")

    assert result.stdout.strip() == str(workspace_dir)


def test_missing_workspace_fails_closed_rather_than_raising(
    tmp_path: Path, classifier: CommandClassifier
) -> None:
    """An expected refusal is a result, not a traceback.

    A caller that forgot to handle something should get something it cannot
    mistake for a completed run, not an exception.
    """
    root = tmp_path / "gone"
    root.mkdir()
    executor = Executor(Workspace(root))
    outcome = approve(classifier, "-c", "print('x')")
    _rmtree(root)

    result = executor.execute(outcome, request_for("-c", "print('x')"))

    assert isinstance(result, ExecutionResult_type())
    assert result.state is ExecutionState.INVALID_APPROVAL


# ==========================================================================
# Audit metadata
# ==========================================================================


def test_event_carries_no_output(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "print('UNIQUE_MARKER')")
    event = result.event()

    assert "UNIQUE_MARKER" not in json.dumps(event.to_public_dict(), default=str)


def test_event_carries_no_argv(executor: Executor, classifier: CommandClassifier) -> None:
    """Arguments carry secrets too: `curl -H "Authorization: Bearer ..."`."""
    result = run(
        executor, classifier, "-c", "import sys; print(sys.argv[1])", "SECRET_ARG_VALUE"
    )
    body = json.dumps(result.event().to_public_dict(), default=str)

    assert "SECRET_ARG_VALUE" not in body
    assert result.event().arg_count == 3


def test_event_carries_no_environment_values(
    executor: Executor, classifier: CommandClassifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGE_TEST_SENTINEL", "super-secret-value")
    result = run(executor, classifier, "-c", "print('x')")

    assert "super-secret-value" not in json.dumps(result.event().to_public_dict(), default=str)


def test_event_records_byte_counts_not_content(
    workspace: Workspace, classifier: CommandClassifier
) -> None:
    small = Executor(workspace, default_timeout=30, max_output_bytes=4096)
    result = run(small, classifier, "-c", "print('a' * 100000)")

    assert result.event().stdout_bytes == 100001
    assert result.event().stdout_truncated is True


def test_event_records_the_fingerprint_for_audit_join(
    executor: Executor, classifier: CommandClassifier
) -> None:
    result = run(executor, classifier, "-c", "print('x')")

    assert result.event().fingerprint == result.fingerprint
    assert len(result.fingerprint) == 64


def test_event_records_timing_and_state(executor: Executor, classifier: CommandClassifier) -> None:
    result = run(executor, classifier, "-c", "print('x')")
    event = result.event()

    assert event.state is ExecutionState.COMPLETED
    assert event.child_exit_code == 0
    assert event.finished_at >= event.started_at
    assert event.duration >= 0


def test_event_marks_a_timeout(executor: Executor, classifier: CommandClassifier) -> None:
    event = run(executor, classifier, "-c", "import time; time.sleep(30)", timeout=1.0).event()

    assert event.timed_out is True
    assert event.terminated is True


def test_result_has_no_environment_field() -> None:
    assert "env" not in ExecutionResult_fields()


# ==========================================================================
# Architecture guards
# ==========================================================================


def test_executor_never_passes_a_shell() -> None:
    """No shell, and no parameter through which one could be requested."""
    import forge.tools.execution as module

    source = Path(str(module.__file__)).read_text()

    assert "shell=True" not in source
    assert "os.system" not in source
    assert "os.popen" not in source


def test_executor_contains_no_classifier_policy() -> None:
    """The executor holds no rules.

    It decides nothing about danger. If a rule ever appeared here, the executor
    would be able to refuse something the classifier allowed -- or the reverse --
    and two layers would disagree about the same operation.
    """
    import forge.tools.execution as module

    source = Path(str(module.__file__)).read_text()

    for banned in (
        "Classification",
        "DENIED_PROGRAMS",
        "APPROVAL_PROGRAMS",
        "cmd.sudo",
        "cmd.git_force_push",
    ):
        assert banned not in source, banned


def test_executor_never_calls_the_approver_or_a_classifier() -> None:
    import forge.tools.execution as module

    source = Path(str(module.__file__)).read_text()

    assert "Approver(" not in source
    assert "CommandClassifier(" not in source
    assert "SecretClassifier(" not in source


def test_executor_does_not_import_the_cli() -> None:
    """An execution layer that knew about presentation would not be thin."""
    import forge.tools.execution as module

    source = Path(str(module.__file__)).read_text()

    assert "forge.cli" not in source
    assert "from .cli" not in source


def test_executor_has_exactly_one_public_entry_point() -> None:
    """No convenience wrapper that could skip the approval checks."""
    public = [
        name
        for name in dir(Executor)
        if not name.startswith("_") and callable(getattr(Executor, name))
    ]

    assert public.count("execute") == 1
    assert set(public) <= {"execute"}


def test_executor_only_claims_after_validation(
    executor: Executor, classifier: CommandClassifier
) -> None:
    """A mismatch leaves the approval spendable.

    Validation failure is a caller bug, not an operator decision. Spending the
    approval on one would be a second, silent refusal of something the operator
    did agree to.
    """
    outcome = approve(classifier, "-c", "print('approved')")

    refused = executor.execute(outcome, request_for("-c", "print('different')"))

    assert refused.state is ExecutionState.INVALID_APPROVAL
    assert outcome.claimed is False


def test_process_output_is_not_claimed_to_be_redacted() -> None:
    """The module states the policy rather than implying a guarantee.

    Process output is untrusted and may contain sensitive data. Bounded, not
    redacted, and this test fails if someone later documents a redaction
    guarantee the code does not implement.
    """
    import forge.tools.execution as module

    doc = module.__doc__ or ""

    assert "not a sandbox" in doc.lower()
    assert "untrusted" in doc.lower()


# --------------------------------------------------------------------------
# Local helpers
# --------------------------------------------------------------------------


def _rmtree(path: Path) -> None:
    """Remove a tree without importing shutil.rmtree into the module under test."""
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir() and not child.is_symlink():
            child.rmdir()
        else:
            child.unlink()
    path.rmdir()


def ExecutionResult_type():  # noqa: N802 - test helper, reads as a type at the call site
    from forge.tools.execution import ExecutionResult

    return ExecutionResult


def ExecutionResult_fields():  # noqa: N802 - test helper
    from forge.tools.execution import ExecutionResult

    return ExecutionResult.__dataclass_fields__


def _unused(*_args: object) -> None:  # pragma: no cover - keeps linters quiet
    ApprovalOutcome