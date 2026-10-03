"""Terminal approval.

Implements step 6 of the enforcement pipeline in ``docs/byte-tools.md``: when a
classification comes back ``approval_required``, present the operation for review
and wait for a human to decide.

This module sits between classification and future execution. Four properties
hold to, and each of them exists because a decision in the spec depends on it:

*   **It consumes verdicts, it does not produce them.** Nothing here re-runs
    containment, secret-source, or command classification. The classifiers have
    already done the work, and re-deriving it here would be a second
    implementation of the same rules that could disagree with the first.
*   **It does not execute.** An :class:`ApprovalOutcome` is a handoff. The
    execution layer is future work and is deliberately absent.
*   **Approval narrows a permission, it never widens one.** A ``deny`` from the
    command verdict or from any supplied secret verdict produces
    ``DENIED_BY_POLICY`` without a prompt ever being rendered and without the
    approval channel being consulted. There is no code path by which an operator
    can approve a denial.
*   **It fails closed.** An approval-required operation with no interactive
    channel is ``DENIED_UNATTENDED`` -- not auto-approved, not queued, not
    silently skipped, and distinguishable from a human saying no, because a
    failure that says "needed approval, nobody was there" is a reportable
    condition and a silent skip is not.

Nothing here re-checks whether a target is inside the workspace or whether a
file is a credential source. Those checks belong to the classifiers, and this
module trusts their verdicts exactly as the spec orders them: containment first,
secret sources second, classification later, prompt last.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from .commands import CommandVerdict
from .secrets import SecretVerdict
from .workspace import Classification, strictest

__all__ = [
    "ApprovalDecision",
    "OperationKind",
    "ApprovalRequest",
    "ApprovalOutcome",
    "ApprovalChannel",
    "TerminalApprovalChannel",
    "ScriptedApprovalChannel",
    "Approver",
    "DeniedByPolicy",
    "ApprovalConsumed",
    "ClaimToken",
    "render_prompt",
    "parse_response",
    "effective_classification",
    "escalating_secret",
    "AFFIRMATIVE_RESPONSES",
    "NOT_SUPPLIED",
]


def effective_classification(
    command: Classification, *secrets: Classification
) -> Classification:
    """The strictest classification across every verdict in one operation.

    The shared ranking rule, in one place:

        DENY > APPROVAL_REQUIRED > ALLOW

    Exposed as a module function so that a caller which reports a classification
    *without* asking anyone -- a ``--classify-only`` mode, say -- reports the same
    answer as the full approval path. A caller that ranked verdicts itself
    would eventually rank them differently, which is how the same file came to
    require approval through one entry point and not another.

    Args:
        command: The command verdict's classification.
        secrets: Classifications of every target's secret verdict.

    Returns:
        The strictest of them all.
    """
    return strictest(command, *secrets)


def escalating_secret(
    secrets: Sequence["SecretVerdict"], level: Classification
) -> "SecretVerdict | None":
    """The first secret verdict at ``level``, or ``None``.

    Used to name *which* rule made the operation gated, so a prompt can say why
    an ordinary-looking command needs a decision.
    """
    for secret in secrets:
        if secret.classification is level:
            return secret
    return None

# The only responses that approve. Exact word, case-insensitive, after trimming.
# Everything else rejects, including an empty line, a bare newline, "yeah",
# "yolo", and anything the operator typed without meaning it as a decision.
#
# This is deliberately stricter than it needs to be for a cooperative operator.
# The operator is approving an action they may not have requested, and an inline
# `y` on a half-understood operation trains the reader to type `y` without
# reading -- which defeats the control entirely. The spec asks for
# review-then-confirm; the parse is the last half of that.
AFFIRMATIVE_RESPONSES = frozenset({"y", "yes"})

# Rendered where a prompt field has no value. The spec is explicit that an
# operator seeing "not determinable" can make a different decision from one
# seeing nothing, so absent fields are stated rather than omitted.
NOT_SUPPLIED = "not supplied"
NOT_DETERMINABLE = "not determinable"


class ApprovalDecision(str, Enum):
    """Outcome of routing one operation through approval.

    Inherits from :class:`str` for the same reason
    :class:`~forge.tools.workspace.Classification` does: results serialise
    cleanly for a terminal or a log line.

    The four values are not interchangeable. ``REJECTED`` means a human was
    present and said no. ``DENIED_UNATTENDED`` means nobody was there to say
    anything. Both stop the operation, but only one of them is a decision a
    person made, and conflating them would hide the unattended case that the
    spec calls reportable.
    """

    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED_BY_POLICY = "denied_by_policy"
    DENIED_UNATTENDED = "denied_unattended"

    @property
    def permitted(self) -> bool:
        """Whether the operation may proceed.

        Only :attr:`APPROVED`. A denial is not a permission, and an
        unattended failure is not a permission.
        """
        return self is ApprovalDecision.APPROVED

    @property
    def human_decided(self) -> bool:
        """Whether a human made this call.

        False for both policy denials and for an unattended failure. A denial
        is not something a human declined -- no, because the answer could not be
        anything but no.
        """
        return self in (ApprovalDecision.APPROVED, ApprovalDecision.REJECTED)


class OperationKind(str, Enum):
    """What sort of operation an approval request is about.

    Present so a prompt can say which kind of thing it is showing, and so a
    future surface can present git context only for the kind that has one.
    """

    COMMAND = "command"
    SECRET = "secret"


class DeniedByPolicy(Exception):
    """Raised when asked to build a promptable request from a denial.

    A denial has no approval path. Rather than return a request the operator
    could approve -- which would be the exact failure the permission model
    exists to prevent -- the request constructors refuse. :class:`Approver`
    catches this and converts it to ``DENIED_BY_POLICY``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClaimToken:
    """Single-spend state shared by every copy of one :class:`ApprovalOutcome`.

    An approval authorises one operation. That has to be an intrinsic property
    of the approval itself, not a bookkeeping detail of whoever spends it: a
    per-executor "already used" set would let a second executor spend the same
    approval, and a plain dataclass flag would be copied rather than shared.

    So the state lives here, in a small mutable object held by an otherwise
    frozen dataclass. Copying the outcome -- by assignment, ``replace()``,
    ``copy.copy()``, or ``copy.deepcopy()`` -- copies the *reference* to this
    object, so every copy shares one claim and only one of them can ever win.

    The check-and-set happens under a lock, so concurrent claims resolve to
    exactly one winner rather than racing.

    ``__copy__`` and ``__deepcopy__`` return ``self`` deliberately. The default
    deepcopy path would try to pickle the lock and raise ``TypeError``; and any
    implementation that *cloned* the token would hand out a second spendable
    capability, which is precisely the hole this class exists to close.
    """

    __slots__ = ("_lock", "_claimed")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        """Atomically transition unclaimed -> claimed.

        Returns:
            ``True`` for exactly one caller over this token's lifetime, and
            ``False`` for every later one.
        """
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True

    @property
    def claimed(self) -> bool:
        """Whether this approval has been spent."""
        with self._lock:
            return self._claimed

    def __copy__(self) -> "ClaimToken":
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> "ClaimToken":
        # Sharing, not cloning: a deepcopy of an outcome must not be a second
        # capability to spend the same approval.
        return self


class ApprovalConsumed(Exception):
    """Raised when an already-claimed approval is claimed again.

    An approval covers one operation and is not remembered. An approval that
    cannot be spent twice is enforced by the shared :class:`ClaimToken` inside
    :class:`ApprovalOutcome`, so the guarantee holds across copies and across
    concurrent callers rather than depending on any single caller behaving.
    """


def _realpath_or_empty(path: Path | None) -> str:
    """Resolve ``path`` for fingerprinting, or yield an empty string.

    A path that cannot be resolved still contributes a stable, distinct value,
    which is the safe direction: an unresolvable approval cannot match a
    resolvable execution.
    """
    if path is None:
        return ""
    try:
        return os.path.realpath(str(path))
    except (OSError, ValueError):
        return f"<unresolvable:{path}>"


@dataclass(frozen=True)
class ApprovalRequest:
    """A single operation, rendered for review.

    Built from a classification verdict and never from a re-run of any check.
    Every field here exists because the prompt in ``docs/byte-tools.md``
    displays it, or because a caller supplied it and the spec says to say so
    when it is absent.

    Attributes:
        kind: Command or secret-source operation.
        agent: Which agent wants this. Displayed first, so the operator knows
            who is asking before they read what it wants.
        tool: The tool being called, e.g. ``"exec_command"``. Availability and
            permission are settled before this point; this is the operation
            layer only.
        operation: One-line description of what the operation would do, phrased
            as intent rather than as a verdict.
        rule: The classifier rule that required approval, e.g.
            ``"cmd.delete_recursive"``. Stable identifier, so a decision is
            explainable rather than mysterious.
        classification: The verdict being routed. Kept on the request so
            :meth:`Approver.resolve` can defend against a hand-built request
            whose classification would not survive review.
        reason: The classifier's explanation. Operator-safe by construction --
            the classifiers never put file contents or environment values in
            it.
        intent: Why the agent wants this, in the agent's words. Optional here
            and rendered as ``not supplied`` when absent: the approval layer
            should not invent a rationale it was not given. A future agent
            runtime may require it before it constructs a request, but that is
            the runtime's policy, not this module's.
        workspace: The resolved workspace root the operation would run in.
        detail: The exact thing being approved. A program and its arguments, or
            a resolved path. Rendered verbatim; quoting here is deliberately
            absent because a shell-quoted line invites the reader to trust the
            quoting rather than the arguments.
        targets: Resolved affected paths, when knowable. Empty means not
            knowable, which is rendered explicitly rather than omitted.
        is_shell: Whether the invocation is a shell string. Surfaced because a
            shell string is never parsed, and an operator reading one deserves
            to know that nothing checked inside it.
        git_context: Caller-supplied git context, or ``None``. This module
            performs no git lookup; per the current scope it carries what it is
            given and says so when it is given nothing.
    """

    kind: OperationKind
    agent: str
    tool: str
    operation: str
    rule: str
    classification: Classification
    reason: str
    workspace: Path
    detail: str
    intent: str | None = None
    targets: tuple[Path, ...] = ()
    is_shell: bool = False
    git_context: str | None = None
    matched_rules: tuple[str, ...] = ()

    # -- execution binding ------------------------------------------------
    #
    # The exact structural identity of the invocation, kept separately from
    # `detail`, which is a human-readable join and loses token boundaries.
    # `detail` cannot prove what will be launched; these can.
    program: str | None = None
    args: tuple[str, ...] | None = None
    cwd: Path | None = None
    is_shell_form: bool = False

    @property
    def executable_bindable(self) -> bool:
        """Whether this request carries a command-shaped identity.

        A secret-source request is a file access, not a command, so it has no
        program to bind. False there rather than half-filled.
        """
        return self.program is not None

    @property
    def targets_determinable(self) -> bool:
        """Whether the affected targets are known before execution."""
        return bool(self.targets)

    # -- constructors ---------------------------------------------------

    @classmethod
    def from_command(
        cls,
        verdict: CommandVerdict,
        *,
        agent: str,
        workspace: Path,
        tool: str = "exec_command",
        intent: str | None = None,
        git_context: str | None = None,
    ) -> "ApprovalRequest":
        """Build a request from a :class:`CommandVerdict`.

        Args:
            workspace: The Forge workspace root. **Required, with no default.**

                It was once optional and fell back to ``verdict.cwd``, which is
                how a defect shipped: the fallback is correct for a root-level
                command and wrong for a nested one, so the approval
                fingerprinted a subdirectory as the workspace, the executor
                fingerprinted the real root, the digests disagreed, and a
                correctly-approved operation was refused with
                ``INVALID_APPROVAL``. ``CommandVerdict`` carries the resolved cwd
                but not the root, so there was never a safe fallback to infer.

                Making it mandatory removes the failure mode rather than
                documenting it. A caller that forgets now gets a ``TypeError`` at
                the call site instead of a subtly mis-bound approval that only
                misbehaves for commands run outside the root.

        Raises:
            DeniedByPolicy: ``verdict`` is a denial. A denied command has no
                approval path and must never reach a prompt.
        """
        if verdict.classification is Classification.DENY:
            raise DeniedByPolicy(
                f"{verdict.rule}: {verdict.reason}"
            )

        detail = " ".join([verdict.program, *verdict.args])
        return cls(
            kind=OperationKind.COMMAND,
            agent=agent,
            tool=tool,
            operation=f"run `{verdict.program}` with the arguments shown",
            rule=verdict.rule,
            classification=verdict.classification,
            reason=verdict.reason,
            workspace=workspace,
            detail=detail,
            intent=intent,
            targets=verdict.targets_resolved,
            is_shell=verdict.is_shell,
            git_context=git_context,
            matched_rules=verdict.matched_rules,
            program=verdict.program_token or verdict.program,
            args=tuple(verdict.args),
            cwd=verdict.cwd,
            is_shell_form=verdict.is_shell,
        )

    @classmethod
    def from_secret(
        cls,
        verdict: SecretVerdict,
        *,
        agent: str,
        tool: str = "read_file",
        intent: str | None = None,
        git_context: str | None = None,
    ) -> "ApprovalRequest":
        """Build a request from a :class:`SecretVerdict`.

        Raises:
            DeniedByPolicy: ``verdict`` is a denial. This is the structural
                tier -- a file whose location or content proves it holds
                credentials is denied with no approval path, so it is not
                something an operator can wave through.
        """
        if verdict.classification is Classification.DENY:
            raise DeniedByPolicy(f"{verdict.rule}: {verdict.reason}")

        target = verdict.resolved
        detail = str(target) if target is not None else NOT_DETERMINABLE
        return cls(
            kind=OperationKind.SECRET,
            agent=agent,
            tool=tool,
            operation=f"access `{detail}`",
            rule=verdict.rule,
            classification=verdict.classification,
            reason=verdict.reason,
            workspace=target.parent if target is not None else Path("."),
            detail=detail,
            intent=intent,
            targets=(target,) if target is not None else (),
            git_context=git_context,
            matched_rules=verdict.matched_rules,
        )

    # -- execution binding ------------------------------------------------

    @staticmethod
    def fingerprint_for(
        program: str,
        args: tuple[str, ...],
        workspace: Path,
        cwd: Path,
        is_shell_form: bool = False,
    ) -> str:
        """The binding digest for a launch identity, without building a request.

        Exists so a caller that has an invocation but not an approval -- the
        execution layer, computing what it is about to ask permission for --
        can derive the same digest without fabricating an ApprovalRequest and
        inventing a classification it did not produce. The request method below
        delegates here, so there is one implementation.
        """
        parts = (
            "forge-approval-v1",
            program,
            "\x00".join(args),
            _realpath_or_empty(workspace),
            _realpath_or_empty(cwd),
            "shell" if is_shell_form else "argv",
        )
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def execution_fingerprint(self) -> str:
        """A digest binding this approval to one exact invocation.

        Covers, and only covers, the things that decide what gets launched:

            the exact program token
            the exact argv tokens, with boundaries preserved
            the resolved workspace
            the resolved cwd
            whether the invocation is a shell string

        The program token is bound **verbatim**. The classifier lowercases a
        basename to match rules against, but that normalisation is a
        classification convenience and reusing it here would be wrong: it would
        make ``/usr/bin/rm`` and ``rm`` the same approval, so approving one
        would authorise whichever file the other happened to resolve to.
        Execution binding answers a different question -- *which operation is
        being launched* -- and needs the exact token the operator saw.

        Nothing secret enters this digest. There is no environment and no value
        of any kind in the input, so the result cannot be used as an offline
        check against a guessable secret. That is also why a future credential
        design needs opaque handles rather than raw values: a digest of a
        low-entropy value would be a verification oracle for it.

        Deliberately *not* included: the resolved executable path. Resolving it
        is a launch-time act, and a digest of it would still not close the
        window between resolution and execve. The resolved path is recorded in
        execution metadata as evidence of what ran, not bound as a guarantee.
        """
        return ApprovalRequest.fingerprint_for(
            self.program or "",
            tuple(self.args or ()),
            self.workspace,
            self.cwd or self.workspace,
            self.is_shell_form,
        )

    def to_public_dict(self) -> dict[str, object]:
        """A serialisable projection with no private or non-public field."""
        body: dict[str, object] = {
            "kind": self.kind.value,
            "agent": self.agent,
            "tool": self.tool,
            "operation": self.operation,
            "rule": self.rule,
            "classification": self.classification.value,
            "reason": self.reason,
            "workspace": str(self.workspace),
            "detail": self.detail,
            "is_shell": self.is_shell,
        }
        if self.intent is not None:
            body["intent"] = self.intent
        if self.targets:
            body["targets"] = [str(t) for t in self.targets]
        if self.git_context is not None:
            body["git_context"] = self.git_context
        if self.matched_rules:
            body["matched_rules"] = list(self.matched_rules)
        if self.program is not None:
            body["program"] = self.program
            body["arg_count"] = len(self.args or ())
        return body

    # -- rendering ------------------------------------------------------

    def render(self) -> str:
        """Render this request as a review-then-confirm prompt.

        See :func:`render_prompt`, which is the module-level entry point. This
        method is the same rendering, available where a caller already holds a
        request.
        """
        return render_prompt(self)


def _field(label: str, value: str) -> str:
    return f"  {label:<18} {value}"


def render_prompt(request: ApprovalRequest) -> str:
    """Render ``request`` for operator review.

    The field set is the one ``docs/byte-tools.md`` specifies: requesting agent,
    operation, workspace, command or action, affected targets, reason, git
    context. Every field is always present.

    Two rendering decisions are load-bearing rather than cosmetic:

    *   **Absent fields are stated.** No intent, no git context, no determinable
        targets -- each renders an explicit "not supplied" or "not determinable"
        line. The spec's reasoning is that an operator who sees
        "Affected targets: not determinable" can decide differently from one
        who sees nothing, and a missing line is indistinguishable from an
        absent field to anyone skimming.
    *   **A shell invocation says so.** ``cmd.shell_string`` is approval-gated
        because Forge does not parse shell. An operator reading a shell string
        needs to know that nothing inside it was checked, or the prompt implies
        a level of analysis that does not exist.

    No file contents and no environment values appear here. The only text
    sourced from elsewhere is the classifier's ``reason`` and ``rule``, which the
    classifiers build to be operator-safe.
    """
    lines: list[str] = [
        "APPROVAL REQUIRED",
        "The following operation has not run. Review it, then decide.",
        "",
        _field("Requesting agent", request.agent),
        _field("Operation", request.operation),
        _field("Tool", request.tool),
        _field("Workspace", str(request.workspace)),
        _field("Command or action", request.detail),
    ]

    if request.targets_determinable:
        targets = ", ".join(str(t) for t in request.targets)
        lines.append(_field("Affected targets", targets))
    else:
        lines.append(
            _field("Affected targets", NOT_DETERMINABLE)
        )

    if request.is_shell:
        lines.append(
            _field("Invocation form", "shell string; not parsed, not inspected")
        )

    lines.extend(
        [
            _field("Rule", request.rule),
            _field("Classification", request.classification.value),
            _field("Why it is gated", request.reason),
            _field("Reason", request.intent or NOT_SUPPLIED),
            _field("Git context", request.git_context or NOT_SUPPLIED),
            "",
            f"Approve request from {request.agent}? "
            "Type 'y'/'yes' to approve or 'n'/'no' to reject:",
        ]
    )
    return "\n".join(lines)


def parse_response(raw: str | None) -> bool:
    """Whether ``raw`` is an approval.

    Exact affirmative, case-insensitive, after trimming. ``None`` -- an
    unreachable prompt, EOF, or an interrupt -- declines.
    """
    if raw is None:
        return False
    return raw.strip().casefold() in AFFIRMATIVE_RESPONSES


@dataclass(frozen=True)
class ApprovalOutcome:
    """The settled result of routing one operation through approval.

    An approved outcome is a **single-spend capability**. :meth:`claim` spends
    it, exactly once, for exactly one operation.

    Attributes:
        decision: What happened.
        request: The request that was settled, or ``None`` when the
            classification was a denial and no request was ever built.
        detail: An operator-safe explanation, suitable for a log line or an
            error message. Never contains file contents or credential values.
        operator_response: What the operator actually typed, when a human was
            present. Retained so a later audit surface can show what was
            answered; v1 records no durable audit trail, and this is an
            in-memory field that dies with the outcome.
    """

    decision: ApprovalDecision
    detail: str
    request: ApprovalRequest | None = None
    operator_response: str | None = None

    # The single-spend state. `repr=False` and `compare=False` keep it out of
    # reprs and equality; the field is private and read only through
    # `claimed`/`claim`, so no caller can mutate it directly.
    #
    # `default_factory` gives every *distinct* outcome its own token. Copies of
    # one outcome share the reference, which is what makes the guarantee hold
    # across assignment, replace(), copy.copy(), and copy.deepcopy().
    _token: ClaimToken = field(
        default_factory=ClaimToken, repr=False, compare=False
    )

    @property
    def approved(self) -> bool:
        return self.decision is ApprovalDecision.APPROVED

    @property
    def denied(self) -> bool:
        return self.decision in (
            ApprovalDecision.DENIED_BY_POLICY,
            ApprovalDecision.DENIED_UNATTENDED,
        )

    @property
    def claimed(self) -> bool:
        """Whether this approval has been spent.

        Computed from the shared token rather than stored, so it cannot go stale
        and cannot disagree with the gate :meth:`claim` actually uses.
        """
        return self._token.claimed

    @property
    def human_decided(self) -> bool:
        """Whether a human made this call.

        False for both policy denials and for an unattended failure. A denial
        is not something a human declined -- no, because the answer could not be
        anything but no.
        """
        return self.decision.human_decided

    @property
    def requires_human(self) -> bool:
        """Whether the absence of a decision came from nobody being present.

        The reportable unattended case, kept separate from an ordinary
        rejection so a caller can tell "the operator said no" from "there was
        no operator".
        """
        return self.decision is ApprovalDecision.DENIED_UNATTENDED

    def claim(self) -> "ApprovalOutcome":
        """Spend this approval. Authorises exactly one operation, once.

        Returns ``self``. The return value exists for callers that want to bind
        the claimed outcome to whatever they do next -- the execution layer
        passes the returned outcome to the launcher -- but it carries no
        additional state: the spend has already happened, whether or not
        anything is done with it.

        Raises:
            ApprovalConsumed: Already claimed. A second claim would silently
                widen a per-operation approval into a reusable one.
        """
        if not self._token.claim():
            raise ApprovalConsumed(
                "this approval has already been claimed; approval is "
                "per-operation and is not reusable"
            )
        return self

    def consume(self) -> "ApprovalOutcome":
        """Compatibility alias for :meth:`claim`.

        Kept because ``consume`` was the original name and callers may still
        read it. The semantics are identical -- both delegate to the shared
        token -- so the two cannot disagree.
        """
        return self.claim()

    def to_public_dict(self) -> dict[str, object]:
        """A token-free projection, safe to serialise.

        The claim token is deliberately absent: it is not state worth
        recording, it holds no information an audit needs, and omitting it here
        gives one obvious place that decides what leaves an outcome.

        Note that ``dataclasses.asdict`` is *not* this method -- asdict copies
        every field including the private one, and the result is not
        serialisable. Callers wanting a safe projection use this.
        """
        body: dict[str, object] = {
            "decision": self.decision.value,
            "detail": self.detail,
            "claimed": self.claimed,
        }
        if self.operator_response is not None:
            body["operator_response"] = self.operator_response
        if self.request is not None:
            body["request"] = self.request.to_public_dict()
        return body

    def __str__(self) -> str:  # pragma: no cover - operator convenience
        target = self.request.detail if self.request is not None else "-"
        return f"{self.decision.value}: {target}: {self.detail}"


@runtime_checkable
class ApprovalChannel(Protocol):
    """A way to ask a human.

    Two things are separate on purpose. :attr:`available` answers "is anybody
    there", and is checked *before* a prompt is rendered, so an unattended run
    produces a clear failure rather than a prompt that goes nowhere.
    :meth:`present` answers "what did they say". Splitting them is what lets a
    test drive a scripted channel and a terminal drive a real one through the
    same policy.
    """

    @property
    def available(self) -> bool:
        """Whether an interactive human can be asked right now."""
        ...

    def present(self, request: ApprovalRequest, prompt: str) -> str | None:
        """Show ``prompt`` and return the raw response.

        Returns ``None`` when the operator could not be reached -- EOF, a closed
        stream, an interrupt. ``None`` is not an approval.
        """
        ...


class TerminalApprovalChannel:
    """The interactive channel: stdlib ``print`` and ``input``, nothing more.

    This is the primary human interface, because Forge is terminal-first, so the
    default channel is the real one. It is deliberately thin: no colours, no
    key handling, no cursor tricks, no TUI. Every one of those is interface
    design belonging to the CLI layer, and none of them is needed to ask a
    question and record the answer.

    Args:
        prompt: Sink for the rendered prompt. Defaults to stdout.
        read: Reads one line of input. Defaults to :func:`input`. Injectable so
            the policy can be tested without a TTY.
        available: Overrides the TTY probe. ``None`` (the default) means "ask
            the streams", which is the correct behaviour for the real terminal
            and a TTY-free value for tests.
    """

    def __init__(
        self,
        *,
        prompt_stream: object | None = None,
        read: Callable[[str], str] | None = None,
        available: bool | None = None,
    ) -> None:
        self._prompt_stream = prompt_stream if prompt_stream is not None else sys.stdout
        self._read = read if read is not None else input
        self._available_override = available

    @property
    def available(self) -> bool:
        if self._available_override is not None:
            return self._available_override
        stdin, stdout = sys.stdin, sys.stdout
        try:
            return bool(stdin.isatty() and stdout.isatty())
        except (AttributeError, ValueError):
            # A detached or replaced stream has no usable TTY notion. Fail
            # closed: no probe means no operator.
            return False

    def present(self, request: ApprovalRequest, prompt: str) -> str | None:
        """Print ``prompt`` and read one line.

        Returns:
            The raw line, or ``None`` on EOF, a closed stream, or an interrupt.
            A keyboard interrupt at the prompt is a decision to stop, not an
            approval, and it is reported as "no answer" rather than propagating
            an exception into the caller's flow.
        """
        try:
            print(prompt, file=self._prompt_stream)  # type: ignore[arg-type]
            return self._read("")
        except (EOFError, KeyboardInterrupt):
            return None
        except (ValueError, OSError):
            # A closed or unwritable stream. No operator was reached.
            return None


class ScriptedApprovalChannel:
    """A channel that replays answers. For tests and non-interactive driving.

    Not an allowlist and not a bypass: it still goes through the same
    availability check and the same strict parse as the terminal channel. The
    only difference is where the answer comes from.
    """

    def __init__(
        self,
        responses: list[str | None] | tuple[str | None, ...],
        *,
        available: bool = True,
    ) -> None:
        self._responses = list(responses)
        self._available = available
        self.presented: list[ApprovalRequest] = []

    @property
    def available(self) -> bool:
        return self._available

    def present(self, request: ApprovalRequest, prompt: str) -> str | None:
        self.presented.append(request)
        if not self._responses:
            return None
        return self._responses.pop(0)


class Approver:
    """Routes classified operations to a human, or fails closed without one.

    The single entry point an execution layer would sit behind. It is
    constructed with a channel and holds no state between operations: no
    remembered approvals, no allowlist, no session mode. That absence is the
    spec's decision, and keeping this class stateless is what guarantees it.

    Args:
        channel: Where approval questions go. ``None`` means unattended, which
            is the correct configuration for an n8n-triggered run and produces
            ``DENIED_UNATTENDED`` for anything approval-required.
    """

    def __init__(self, channel: ApprovalChannel | None = None) -> None:
        self.channel = channel

    # -- routing --------------------------------------------------------

    def submit_command(
        self,
        verdict: CommandVerdict,
        *,
        agent: str,
        workspace: Path,
        secret_verdicts: tuple[SecretVerdict, ...] = (),
        tool: str = "exec_command",
        intent: str | None = None,
        git_context: str | None = None,
    ) -> ApprovalOutcome:
        """Route a classified command together with the secret verdicts for its targets.

        Every verdict that participates in the operation is ranked together and
        the strictest wins:

            DENY > APPROVAL_REQUIRED > ALLOW

        so a command that is itself ordinary is still gated when it would read a
        name-guess secret, and denied outright when it would read a structural
        one. The rule lives here rather than in any caller, because every future
        caller inherits it and none of them can forget it.

        Ranking the whole set together also means **one** approval decision for
        the operation. Several gated targets produce one prompt, not one per
        target: the operator is approving a single command, and a prompt per
        target would train them to approve without reading.

        Args:
            verdict: The command classification result. Not re-run here.
            agent: Which agent is asking. Displayed in the prompt.
            secret_verdicts: Secret classifications of the paths this command
                would touch, if the caller performed them. Every one
                participates in the ranking.
            tool: The tool being called.
            intent: The agent's own reason, in its words. Optional.
            git_context: Caller-supplied git context. No lookup is performed
                here.
            workspace: The Forge workspace root, forwarded to
                :meth:`ApprovalRequest.from_command`. **Required.** The command
                verdict carries only a resolved cwd, and conflating the two
                mis-binds every command run outside the root.
        """
        effective = effective_classification(
            verdict.classification,
            *(secret.classification for secret in secret_verdicts),
        )

        # A secret denial is reported in its own terms. "secret.env denied this"
        # tells an operator something actionable; "cmd.none denied this" would
        # point them at a rule that never fired.
        denying_secret = escalating_secret(secret_verdicts, Classification.DENY)
        if denying_secret is not None:
            return ApprovalOutcome(
                decision=ApprovalDecision.DENIED_BY_POLICY,
                detail=(
                    f"secret source denied the operation before approval: "
                    f"{denying_secret.rule}: {denying_secret.reason}"
                ),
            )

        try:
            request = ApprovalRequest.from_command(
                verdict,
                agent=agent,
                tool=tool,
                intent=intent,
                git_context=git_context,
                workspace=workspace,
            )
        except DeniedByPolicy as denial:
            return ApprovalOutcome(
                decision=ApprovalDecision.DENIED_BY_POLICY,
                detail=f"denied by classification: {denial.reason}",
            )

        # The request carries the command's own verdict. When a secret verdict is
        # what made the operation gated, the request has to say so -- otherwise
        # `resolve` would see ALLOW, approve without asking, and the escalation
        # would be lost. The prompt names the escalating secret so the operator
        # can see *why* an ordinary-looking command needs a decision.
        if effective is not verdict.classification:
            escalating = escalating_secret(secret_verdicts, effective)
            if escalating is not None:
                request = replace(
                    request,
                    classification=effective,
                    rule=escalating.rule,
                    reason=(
                        f"secret source requires approval: {escalating.rule}: "
                        f"{escalating.reason}"
                    ),
                    matched_rules=request.matched_rules + escalating.matched_rules,
                )

        return self.resolve(request)

    def submit_secret(
        self,
        verdict: SecretVerdict,
        *,
        agent: str,
        tool: str = "read_file",
        intent: str | None = None,
        git_context: str | None = None,
    ) -> ApprovalOutcome:
        """Route a classified secret-source access."""
        try:
            request = ApprovalRequest.from_secret(
                verdict, agent=agent, tool=tool, intent=intent, git_context=git_context
            )
        except DeniedByPolicy as denial:
            return ApprovalOutcome(
                decision=ApprovalDecision.DENIED_BY_POLICY,
                detail=f"denied by classification: {denial.reason}",
            )

        return self.resolve(request)

    def resolve(self, request: ApprovalRequest) -> ApprovalOutcome:
        """Settle one request.

        Three outcomes, in the order they are decided:

        1. A denial in the request's own classification is refused here as
           well as in the constructors. :class:`ApprovalRequest` is a frozen
           dataclass a caller can build directly, and this check keeps a
           hand-built request from turning a denial into a prompt.
        2. An ``allow`` is approved without consulting the channel. There is
           nothing to ask about, and a routine development command that needs a
           human to say yes to running a test is the prompt fatigue that makes
           operators stop reading prompts.
        3. Anything approval-required needs the channel. No channel, or a
           channel that cannot reach anyone, fails closed.
        """
        if request.classification is Classification.DENY:
            return ApprovalOutcome(
                decision=ApprovalDecision.DENIED_BY_POLICY,
                detail=f"denied by classification: {request.rule}: {request.reason}",
                request=request,
            )

        if request.classification is Classification.ALLOW:
            return ApprovalOutcome(
                decision=ApprovalDecision.APPROVED,
                detail="classification allowed this operation; no approval needed",
                request=request,
            )

        if self.channel is None or not self.channel.available:
            return ApprovalOutcome(
                decision=ApprovalDecision.DENIED_UNATTENDED,
                detail=(
                    "approval was required and no interactive approval channel "
                    "was available; the operation was not run and was not queued"
                ),
                request=request,
            )

        prompt = request.render()
        raw = self.channel.present(request, prompt)

        if parse_response(raw):
            return ApprovalOutcome(
                decision=ApprovalDecision.APPROVED,
                detail=f"approved by operator ({request.rule})",
                request=request,
                operator_response=raw,
            )

        return ApprovalOutcome(
            decision=ApprovalDecision.REJECTED,
            detail=(
                "no approval was given"
                if raw is None
                else f"declined by operator ({request.rule})"
            ),
            request=request,
            operator_response=raw,
        )
