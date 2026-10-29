"""Session-scoped approval grants.

``Approver`` is stateless by design -- no remembered approvals, no allowlist,
no session mode. That absence is what guarantees "approval narrows a
permission, it never widens one": the only thing that can approve an operation
is a human, deciding one operation at a time.

A session grant is the single, explicit exception. The operator may, once per
session, authorise a *narrow named scope* -- for the first implementation, the
repository test-runner scope -- and for the rest of that session, operations
that fall under the scope are authorised without a per-operation prompt.

The contract that keeps this from becoming a blanket approval:

*   **It is opt-in and explicit.** A grant is built with a concrete set of
    ``ScopeKind`` values and a finite budget. There is no default grant and no
    code path that builds one on its own.
*   **It never widens a denial.** The grant is consulted only after the
    classifier has returned ``approval_required``. ``DENY`` is returned before
    the grant is ever seen, and ``ALLOW`` needs no grant. A granted scope
    replaces a human prompt; it can never replace a refusal.
*   **It is structural, not textual.** A scope matches on the classifier's
    rule id and the structured program/argument tokens -- never on a command
    string, never on an operator's description, never on a remote hostname
    alone.
*   **It is bounded and expiring.** Every authorisation decrements the budget;
    at zero the grant stops matching and ordinary prompting resumes. A
    wall-clock expiry may cap it further. The grant lives in one
    :class:`~forge.tools.dispatcher.ToolDispatcher` run and dies with it -- it
    is never persisted and cannot survive into another run.
*   **Remote scopes are declared, not enabled.** ``REMOTE_READONLY`` (a
    forced-command SSH identity) and ``REMOTE_CODING`` (a bounded headless
    assignment on the Space Monkey task contract) exist in the type only. The
    no code path authorises them: remote execution stays disabled until its
    dedicated identities and server-side enforcement are separately verified.

Nothing here imports the approval, command, or secret modules. A grant consumes
the *primitives* a request already exposes (rule id, program, args), so the
approval layer stays the only place that owns the request object.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "ScopeKind",
    "SessionGrant",
    "Authorization",
    "REMOTE_SCOPES",
    "DEFAULT_SESSION_GRANT_BUDGET",
    "DEFAULT_SESSION_GRANT_EXPIRY_SECONDS",
    "build_test_grant",
]

#: Ceiling on session-authorized operations, matching the runtime's own turn
#: ceiling so a grant can never outlast the run it belongs to. A caller may
#: lower this, never raise the effective bound upward in practice -- it is a
#: ceiling, not an entitlement.
DEFAULT_SESSION_GRANT_BUDGET = 25

#: Default wall-clock lifetime of a session grant, in seconds. The budget and
#: the run's teardown already bound every grant; this adds a monotonic ceiling
#: so a single long run cannot hold a grant indefinitely, and it is the one
#: number the CLI and the Workbench share for the ``tests`` scope.
DEFAULT_SESSION_GRANT_EXPIRY_SECONDS = 60 * 60


class ScopeKind(str, Enum):
    """A named, structural class of operations a session may authorise.

    ``TESTS`` is the only authorising scope in the first implementation. The
    two remote scopes are declared so the future shape is explicit, but they
    are never granted.
    """

    TESTS = "tests"
    REMOTE_READONLY = "remote_readonly"
    REMOTE_CODING = "remote_coding"


#: Scopes that require dedicated remote identities and server-side enforcement
#: before they may authorise anything. Combined with the structural matcher
#: below (which returns no scope for these), there is no path from a grant
#: containing a remote scope to an authorised operation.
REMOTE_SCOPES = frozenset({ScopeKind.REMOTE_READONLY, ScopeKind.REMOTE_CODING})

# The classifier rule ids that gate repository test runners. Kept as literals
# (this module imports nothing from ``commands``) and pinned against the real
# table by ``tests/test_session_grant.py`` so a rename there is caught here.
_TEST_RUNNER_RULES = frozenset({"cmd.repo_runner", "cmd.repo_runner_module"})

# Programs whose *entire* purpose is running repository tests, safe to scope
# regardless of further arguments.
_BARE_TEST_PROGRAMS = frozenset({"pytest", "tox", "nox"})

# Programs that carry a test subcommand alongside build/run forms. Only the
# literal ``test`` subcommand is scoped -- ``make``, ``npm run``, ``cargo
# build``, ``go run`` and the like execute arbitrary repository code and stay
# approval-required.
_TEST_SUBCOMMAND_PROGRAMS = frozenset({"npm", "pnpm", "yarn", "cargo", "go"})
_TEST_SUBCOMMAND = "test"


@dataclass(frozen=True)
class Authorization:
    """A structured answer: *which* scope matched, and against which rule."""

    scope: ScopeKind
    rule: str


@dataclass
class SessionGrant:
    """A bounded, explicit, structural permission for one session.

    Mutable by design: ``authorize`` decrements the budget. It is held by one
    ``Approver`` for one run and never shared or persisted, so its mutation is
    concurrent-safe by construction (dispatch is sequential).

    Args:
        scopes: The granted scopes. Empty means "authorise nothing". An
            explicit, rather than defaulted, value -- a grant with no scopes is
            a no-op, not an error.
        budget: Maximum number of operations this grant may authorise. Each
            authorisation decrements it; at zero, matching stops.
        expires_at: Optional ``time.monotonic()`` wall-clock. Once reached the
            grant stops matching regardless of remaining budget. ``None`` means
            no wall-clock cap (the budget and session teardown remain the
            bounds).
    """

    scopes: frozenset[ScopeKind]
    budget: int
    expires_at: float | None = None

    def authorize(
        self,
        *,
        rule: str,
        program: str | None,
        args: tuple[str, ...],
        is_shell: bool,
    ) -> Authorization | None:
        """Return an :class:`Authorization` if ``rule``/``program``/``args``
        fall under a granted scope and the grant still has authority, else
        ``None`` (caller falls through to the ordinary prompt).

        A shell form is never scoped: a scope matches structured argv only.
        Expiry and budget are checked first so a matching operation does not
        decrement a spent or expired grant.
        """
        if is_shell:
            return None
        if self.expires_at is not None and time.monotonic() >= self.expires_at:
            return None
        if self.budget <= 0:
            return None

        scope = self._match(rule, program, args)
        if scope is None:
            return None

        self.budget -= 1
        return Authorization(scope=scope, rule=rule)

    def _match(
        self, rule: str, program: str | None, args: tuple[str, ...]
    ) -> ScopeKind | None:
        if ScopeKind.TESTS in self.scopes and _is_test_form(rule, program, args):
            return ScopeKind.TESTS
        # Remote scopes, even if present, never match. Keeping the decision here
        # (and not merely in the caller) means a grant containing a remote scope
        # can never become an authorising grant through any construction path.
        return None

    @property
    def remaining(self) -> int:
        """Authorisations left. Non-authorising matches do not count."""
        return self.budget


def _is_test_form(rule: str, program: str | None, args: tuple[str, ...]) -> bool:
    """Whether this invocation is a repository test runner, not a build/run.

    ``python -m pytest`` arrives as ``cmd.repo_runner_module`` (the classifier
    already required ``-m`` plus ``pytest``), so the rule alone is decisive.
    Otherwise the deciding rule must be ``cmd.repo_runner`` *and* the program
    must be a test runner -- either a bare test program, or a program whose
    first non-flag argument is the literal ``test`` subcommand.
    """
    if rule == "cmd.repo_runner_module":
        return True
    if rule != "cmd.repo_runner":
        return False

    base = Path(program).name.casefold() if program else ""
    if base in _BARE_TEST_PROGRAMS:
        return True

    non_flags = [a.casefold() for a in args if not a.startswith("-")]
    sub = non_flags[0] if non_flags else None
    return base in _TEST_SUBCOMMAND_PROGRAMS and sub == _TEST_SUBCOMMAND


def build_test_grant(
    *,
    budget: int = DEFAULT_SESSION_GRANT_BUDGET,
    expires_after: float | None = DEFAULT_SESSION_GRANT_EXPIRY_SECONDS,
) -> SessionGrant:
    """Build the one run's ``tests``-scope grant.

    The single construction point both surfaces share, so the CLI and the
    Workbench authorise exactly the same thing: the ``TESTS`` scope, a bounded
    budget, and a monotonic expiry counted from *now*. The returned grant is
    always fresh -- it is built per run by the caller and handed to that run's
    :class:`~forge.tools.approval.Approver`, so it cannot survive into another
    run.

    ``expires_after`` is in seconds from now; pass ``None`` for no wall-clock
    cap (the budget and session teardown remain the bounds). Tests pass a small
    value to exercise expiry without waiting an hour.
    """
    return SessionGrant(
        scopes=frozenset({ScopeKind.TESTS}),
        budget=budget,
        expires_at=(time.monotonic() + expires_after)
        if expires_after is not None
        else None,
    )