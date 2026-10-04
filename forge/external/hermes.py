"""The Hermes adapter: Forge invoking an existing Hermes Agent installation.

Hermes is not a Forge agent. It is a separate, already-running-as-itself agent
runtime with its own model configuration, its own credentials, its own tool
implementations, and its own safety decisions. This adapter *invokes* it. It does
not reimplement it, wrap its personality, or make it look like an
:class:`~forge.agents.profile.AgentProfile`.

What was verified about Hermes on this machine before writing this, because every
decision below rests on it:

*   ``hermes chat -q <task> --format stream-json`` emits newline-delimited JSON
    events, including a final ``result`` event carrying ``exit_code``, ``text``,
    ``model``, and ``session_id``.
*   **The process exit code is always 0**, even when the run failed -- verified by
    asking it to run a command that itself exited 7. So ``returncode`` is
    meaningless as a success signal and the ``result`` event is authoritative.
    This is why :meth:`HermesAdapter.parse` reads the stream.
*   ``-z/--oneshot`` states in its own help that *approvals are auto-bypassed*,
    and it demonstrably runs shell commands with no prompt. Combined with
    ``terminal``, ``file``, ``code_execution``, and ``computer_use`` being enabled
    by default, an unrestricted Hermes has strictly more authority than Byte or
    Neighbor -- including out-of-workspace reads that Forge's classifier denies.
*   ``-t/--toolsets`` is a *real* restriction: with ``-t file`` the terminal tool
    is genuinely absent. This is the lever that makes a safe MVP possible.

Model ownership
---------------

**Hermes owns its model, and this adapter never overrides it.** Hermes reported
``stealth/space-bunny-alpha`` on provider ``nous`` from its own config, knows
nothing about Forge's registry, and exposes its own ``--model``/``--provider``.
Passing a Forge-routed model through would address a model to an endpoint whose
provider pairing Forge does not control -- precisely the mistake
``docs/model-registry.md`` forbids. So there is no ``--model`` here, Forge's
registry is never consulted for this agent, and the model is *reported* from the
stream events instead.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Mapping

from .base import (
    ExternalAgent,
    ExternalInvocation,
    ExternalResult,
    ExternalUnavailable,
)

__all__ = ["HERMES", "HermesAdapter"]


#: Where to look for the Hermes executable. Not hardcoded to one absolute path:
#: the launcher is a shell script that moves with the installation, and ``PATH``
#: is where an operator's own environment says it is.
HERMES_PROGRAM = "hermes"

#: The toolset value that grants Hermes **nothing**.
#:
#: **This is the load-bearing safety constant of the whole external-agent MVP,
#: and it was established by adversarial testing rather than by reading the
#: help text.** What that testing found:
#:
#: *   **Omitting ``-t`` grants everything.** Hermes falls back to the operator's
#:     configured toolsets, which on this machine include ``terminal``, ``file``,
#:     ``code_execution``, ``browser``, and ``computer_use``.
#: *   **``-t ""`` grants everything too.** An empty string is silently ignored,
#:     which is the most dangerous possible reading: it *looks* like a
#:     restriction and is not one.
#: *   ``-t none`` genuinely blocks. Verified by asking Hermes to *do* the thing
#:     rather than to report its tools: with ``none`` it could not run a shell
#:     command, could not read ``/etc/hostname``, and could not fetch a URL.
#:     Adding ``web`` to it granted network and nothing else.
#:
#: Note that asking the model "do you have tools?" is worthless as evidence --
#: with ``-t ""`` it answered ``NO-TOOLS`` and then ran the command anyway. Only
#: an attempt to act is a test. See ``tests/test_external_hermes.py``.
NO_CAPABILITIES_TOOLSET = "none"

#: Environment variables passed through to the child.
#:
#: A *minimal* environment, not an overlay. Inheriting the operator's ambient
#: environment would hand the child every credential in the session, which is the
#: opposite of a capability boundary. Only what Hermes needs to find its own
#: configuration is forwarded, and Forge's own variables are never among them.
#:
#: ``HERMES_HOME`` is forwarded because it selects the Hermes profile, and a
#: Forge-initiated run should use the same profile the operator uses
#: interactively -- silently running against a different profile would be a
#: surprise with a different model and different credentials behind it.
PASSTHROUGH_ENV: tuple[str, ...] = (
    "HOME",
    "HERMES_HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
)


class HermesAdapter:
    """Invoke a real Hermes Agent as an external Forge agent.

    Args:
        program: The executable name or path. Resolved from ``PATH`` unless it
            contains a separator.
        extra_env: Additional environment entries, for a caller that must supply
            something specific. Merged over the passthrough set; never a
            replacement for it.
    """

    def __init__(
        self,
        program: str = HERMES_PROGRAM,
        *,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._program = program
        self._extra_env = dict(extra_env or {})

    # -- ExternalAgent ---------------------------------------------------

    @property
    def name(self) -> str:
        return "hermes"

    @property
    def capabilities(self) -> tuple[str, ...]:
        """**Empty, and that is the MVP's central safety decision.**

        No toolset is granted, so Hermes receives no ``web``, ``terminal``,
        ``file``, ``code_execution``, or ``computer_use`` tool. It can read the
        task and answer in prose; it cannot touch the filesystem, run a command,
        or reach the network.

        This is deliberately not configurable per invocation. Forge cannot
        classify a tool call it cannot see, so granting a capability is a change
        to the safety architecture and belongs in a reviewed code change -- not
        in a flag an operator can widen from a shell. ``--toolsets`` is therefore
        not offered on ``forge hermes``.

        The enforcement is :data:`NO_CAPABILITIES_TOOLSET` passed as ``-t`` in
        :meth:`argv`, and its absence would be a silent grant of *everything* --
        see the constant's comment for the measurements behind that.
        """
        return ()

    def available(self) -> bool:
        """Whether the Hermes program can be found. Opens nothing."""
        return self._resolve() is not None

    def describe(self) -> str:
        return (
            "the Hermes Agent, invoked as an external runtime; text-only in "
            "the Forge MVP (no tools, no network, no filesystem). Hermes "
            "chooses its own model."
        )

    def _resolve(self) -> str | None:
        if os.sep in self._program:
            return self._program if os.path.isfile(self._program) else None
        return shutil.which(self._program)

    def argv(self, invocation: ExternalInvocation) -> tuple[str, ...]:
        """The exact argv for one run.

        Every element is a separate list item, so a task containing quotes,
        ``$(...)``, or backticks is passed as inert data. No shell is involved.

        The flags, and why each is here:

        ``chat``/``-q``            one query, one answer.
        ``--oneshot``              answer and exit rather than seeding a
                                   interactive session -- required for a
                                   non-interactive subprocess.
        ``--format stream-json``   the structured interface. Chosen over ``-z``
                                   because it reports the run's own exit code,
                                   model, and session, none of which the plain
                                   text mode exposes.
        ``--in <workspace>``       the child's working directory. **Not** the
                                   process cwd: a subprocess inherits whatever
                                   directory Forge was launched from, which is
                                   not a safety decision. Verified necessary --
                                   a plain ``cd`` did not move Hermes.
        ``-t none``               **grants no tools at all.** The MVP's safety
                                   boundary. Measured, not assumed: omitting the
                                   flag grants the operator's full configured
                                   toolsets, and ``-t ""`` grants them too
                                   because an empty value is ignored. Only
                                   ``none`` blocks, verified by having Hermes
                                   attempt a shell command, a file read, and a
                                   fetch with all three refused.
        ``--source tool``          tags the session so Forge-driven runs do not
                                   clutter the operator's own session list.
        ``--max-turns``            the invocation's advisory turn ceiling.

        **Absent, deliberately:** ``--yolo`` (never -- it bypasses Hermes's own
        approvals), ``--model``/``--provider`` (Hermes owns those),
        ``--toolsets`` (capability is fixed, not selectable), ``--tui`` (no
        terminal), ``--accept-hooks`` (auto-approves shell hooks).
        """
        program = self._resolve()
        if program is None:
            raise ExternalUnavailable(
                f"the Hermes Agent was not found on PATH as {self._program!r}. "
                f"Install Hermes, or pass an absolute path."
            )

        argv: tuple[str, ...] = (
            program,
            "chat",
            "--query",
            invocation.task,
            "--oneshot",
            "--format",
            "stream-json",
            "--in",
            invocation.workspace,
            "--source",
            "tool",
            "--toolsets",
            NO_CAPABILITIES_TOOLSET,
        )
        if invocation.max_turns is not None:
            argv += ("--max-turns", str(int(invocation.max_turns)))
        return argv

    def environment(self, invocation: ExternalInvocation) -> dict[str, str]:
        """A minimal, explicit environment for the child.

        Built from an allowlist over the *current* environment rather than from
        scratch, so the child still has ``HOME`` and ``PATH`` and can find its own
        installation -- but nothing else. In particular Forge's
        ``FORGE_WORKSPACE``, ``FORGE_MODEL``, and any provider credential are
        absent, and ``HERMES_*`` is forwarded only for ``HERMES_HOME``.
        """
        env = {
            key: os.environ[key]
            for key in PASSTHROUGH_ENV
            if key in os.environ
        }
        env.update(self._extra_env)
        # A child with no HOME cannot find a profile; fall back rather than let
        # Hermes invent a location.
        env.setdefault("HOME", os.path.expanduser("~"))
        # Forge is the caller, and saying so is useful in a transcript.
        env["FORGE_INVOKED_HERMES"] = "1"
        return env

    def parse(
        self, stdout: str, stderr: str, returncode: int
    ) -> ExternalResult:
        """Read the JSONL stream into a result.

        The ``result`` event is authoritative for success, because the process
        exit code is not -- verified: a run whose work failed still exits 0. A
        missing or unparseable ``result`` event is therefore a failure, and
        reported as one, rather than being optimistically read as success.
        """
        events, malformed = _parse_events(stdout)

        result_event: dict[str, object] | None = None
        model = ""
        session_id = ""
        texts: list[str] = []

        for event in events:
            kind = event.get("type")
            if kind == "text" and isinstance(event.get("text"), str):
                texts.append(str(event["text"]))
            elif kind == "system":
                # The init event announces the model the run will use. Reported,
                # never chosen.
                model = str(event.get("model") or model)
                session_id = str(event.get("session_id") or session_id)
            elif kind == "result":
                result_event = event
                model = str(event.get("model") or model)
                session_id = str(event.get("session_id") or session_id)

        if result_event is None:
            detail = _failure_detail(events, stdout, stderr, malformed, returncode)
            return ExternalResult(
                agent=self.name,
                text="\n".join(texts).strip(),
                ok=False,
                exit_code=1,
                model=model,
                provider=_provider_of(result_event, events),
                session_id=session_id,
                events=tuple(events),
                detail=detail,
            )

        exit_code = _as_int(result_event.get("exit_code"), 1)
        text = result_event.get("text")
        return ExternalResult(
            agent=self.name,
            text=text if isinstance(text, str) and text.strip() else "\n".join(texts).strip(),
            ok=exit_code == 0,
            exit_code=exit_code,
            model=model,
            provider=_provider_of(result_event, events),
            session_id=session_id,
            events=tuple(events),
            detail="" if exit_code == 0 else _result_detail(result_event),
        )


#: The single Hermes adapter instance. A module-level constant because the
#: adapter is stateless -- it holds configuration, not a run -- so sharing one
#: is safe and keeps the CLI's lookup trivial.
HERMES = HermesAdapter()


# --------------------------------------------------------------------------
# Stream parsing
# --------------------------------------------------------------------------


def _parse_events(stdout: str) -> tuple[list[dict[str, object]], int]:
    """Parse JSONL events from ``stdout``.

    Returns ``(events, malformed_count)``.

    A line that is not JSON is counted and skipped rather than raising. Hermes
    may print a warning banner before the stream begins, and one unparseable
    line should not discard every event after it -- but the count is kept so a
    result assembled from partial output can say so.
    """
    events: list[dict[str, object]] = []
    malformed = 0
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            if stripped:
                malformed += 1
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed += 1
    return events, malformed


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _provider_of(
    result_event: dict[str, object] | None, events: list[dict[str, object]]
) -> str:
    """The provider Hermes reported, if it reported one."""
    for event in (result_event, *reversed(events)):
        if event is None:
            continue
        provider = event.get("provider")
        if isinstance(provider, str) and provider:
            return provider
    return ""


def _result_detail(result_event: dict[str, object]) -> str:
    """Why Hermes reported a nonzero exit."""
    for key in ("error", "detail", "reason", "turn_exit_reason"):
        value = result_event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    code = _as_int(result_event.get("exit_code"), 1)
    return f"Hermes reported a failed run (exit_code {code})"


def _failure_detail(
    events: list[dict[str, object]],
    stdout: str,
    stderr: str,
    malformed: int,
    returncode: int,
) -> str:
    """Explain a run that produced no ``result`` event.

    The most common cause is the program failing to start or dying before its
    first event, so the child's own stderr is surfaced -- bounded, because it is
    untrusted third-party text.
    """
    if not events:
        tail = (stderr or "").strip()[-400:]
        if tail:
            return f"Hermes produced no events; its stderr was: {tail}"
        return (
            f"Hermes produced no events (process exit {returncode}). forge "
            f"cannot tell whether it started."
        )
    if malformed:
        return (
            f"Hermes emitted no result event; {malformed} line(s) of its "
            f"output were not valid events"
        )
    return "Hermes emitted no result event, so forge cannot confirm the run succeeded"


# Re-exported for the protocol's sake; ``ExternalAgent`` is structural, so this
# import is documentation rather than a base class.
_ = ExternalAgent
