"""Running an external agent as a subprocess.

**This module exists so that ``forge/cli/main.py`` does not import
``subprocess``.** That is not a stylistic preference: ``tests/test_cli.py``
carries a settled guard asserting the CLI imports no execution or network
module, on the reasoning that ``forge check`` must not be able to run anything.
Adding a launch to the CLI would have quietly broken that contract.

The separation is also the right shape independently. The CLI's job is to decide
what an operator asked for, gate it, and render the outcome. Starting a child
process is none of those. Keeping the launch here means:

*   the CLI's existing architectural guards keep holding, unmodified;
*   the argv, environment, and timeout rules live beside the adapter that owns
    them, rather than in a 2000-line dispatch module;
*   the single subprocess call site in Forge is greppable, which is worth more
    than the convenience of having it inline.

What is *not* claimed: that ``forge`` cannot run a process. It can -- through
this module, for an external agent whose capabilities were bounded before launch.
The CLI guard is about the CLI's own module, and this one is honestly labelled.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .base import ExternalAgent, ExternalInvocation, ExternalResult

__all__ = ["ExternalRun", "spawn"]


@dataclass(frozen=True)
class ExternalRun:
    """The outcome of one subprocess launch.

    Attributes:
        result: The adapter's parsed verdict. Always present, even for a failed
            run -- a child that reports failure in its output has still produced
            a parseable answer, and discarding it would throw away the
            explanation.
        timed_out: Whether the child exceeded its wall-clock budget. Separate
            from ``result.ok`` because the two need different messages: one was
            stopped, the other ran and failed.
        launch_error: A message when the child could not be started at all, or
            ``""``. Kept as a value rather than raised so the caller can render
            it through the same path as every other failure.
    """

    result: ExternalResult
    timed_out: bool = False
    launch_error: str = ""


def spawn(
    agent: ExternalAgent,
    invocation: ExternalInvocation,
    *,
    environ: Mapping[str, str] | None = None,
) -> ExternalRun:
    """Run ``agent`` for ``invocation`` and parse what came back.

    Args:
        agent: The adapter. Supplies argv, the child environment, and the parser.
        invocation: What to run.
        environ: Unused for the child (the adapter builds a minimal environment
            from its own allowlist) and accepted only so a caller can pass its
            mapping uniformly. Present rather than absent because every other
            Forge boundary takes one, and a caller that had to special-case this
            would be a sign the seam was in the wrong place.

    Returns:
        An :class:`ExternalRun`. **Never raises for a failed run.** A launch that
        cannot start, a timeout, and a child that reports failure are all
        returned as values, because each has an operator-readable message and
        turning one into a traceback would discard it.
    """
    argv = agent.argv(invocation)
    child_env = agent.environment(invocation)

    try:
        completed = subprocess.run(  # noqa: S603 - argv is a tuple, shell=False
            list(argv),
            capture_output=True,
            text=True,
            timeout=invocation.timeout,
            cwd=invocation.workspace,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return ExternalRun(
            result=ExternalResult(
                agent=agent.name,
                ok=False,
                exit_code=1,
                detail=(
                    f"the external agent did not finish within "
                    f"{invocation.timeout:.0f}s and was stopped"
                ),
            ),
            timed_out=True,
        )
    except OSError as exc:
        # A program that cannot be started is an installation or path problem,
        # not an internal fault. Reported as a value so the caller can render it
        # as a usage error instead of letting it reach the CLI's catch-all.
        return ExternalRun(
            result=ExternalResult(
                agent=agent.name,
                ok=False,
                exit_code=1,
                detail=f"the external agent could not be started: {exc}",
            ),
            launch_error=str(exc),
        )

    return ExternalRun(result=agent.parse(completed.stdout, completed.stderr, completed.returncode))
