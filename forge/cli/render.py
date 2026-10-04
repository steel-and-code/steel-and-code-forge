"""Terminal rendering for the Forge CLI.

Pure presentation. Every function here takes an object the existing Forge stack
already produced and returns text. Nothing in this module decides anything,
classifies anything, or builds a request.

That separation is the point. The CLI's claim is that it is a thin adapter over
the safety stack, and the way to keep that claim honest is to make the place
where output is produced incapable of influencing the outcome. If a future
change needed a rule here to make the output sensible, that would be a sign the
rule belonged in a classifier and the CLI was compensating.

Six states are distinguished, because an operator reading a terminal needs to
tell them apart:

    ALLOW                  classified automatic; no prompt was shown
    APPROVAL REQUIRED      classified gated; not asked, because classifying only
    APPROVED               a human said yes
    REJECTED               a human was asked and said no
    DENIED BY POLICY       a classifier refused; not something a human can allow
    DENIED UNATTENDED      gated, and nobody was there to ask

The last three are all refusals and share an exit code, but they are different
events. An operator, or a later audit, needs to tell "the operator declined"
from "a rule refused" from "there was no operator". Only ``--json`` carries all
three unambiguously; the human-readable form carries them as distinct labels
with distinct reasons underneath.

No ANSI escape sequences are emitted. Forge is expected to run over SSH on a VPS
operator's terminal, and a colour code that renders as literal escape text on
some hosts is worse than no colour. A ``--color`` flag is a later decision.
"""

from __future__ import annotations

import json
from typing import Any

from ..tools.approval import (
    NOT_DETERMINABLE,
    ApprovalDecision,
    ApprovalOutcome,
    ApprovalRequest,
)
from ..tools.commands import CommandVerdict
from ..tools.secrets import SecretVerdict
from ..tools.workspace import Classification

__all__ = [
    "EXIT_OK",
    "EXIT_MODEL_UNAVAILABLE",
    "EXIT_REFUSED",
    "EXIT_USAGE",
    "EXIT_NEEDS_APPROVAL",
    "EXIT_INTERNAL",
    "STATE_ALLOW",
    "STATE_APPROVAL_REQUIRED",
    "STATE_APPROVED",
    "STATE_REJECTED",
    "STATE_DENIED_BY_POLICY",
    "STATE_DENIED_UNATTENDED",
    "EXIT_FOR_STATE",
    "state_for_verdict",
    "state_for_classification",
    "state_for_outcome",
    "render_verdict",
    "render_outcome",
    "render_json",
    "render_route",
    "render_route_json",
    "render_model_status",
    "render_model_status_json",
    "UNVERIFIED_NOTE",
]

# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------
#
# A small, stable contract. Four values a script can branch on, plus one the
# CLI only uses to report its own failure.
#
# ``EXIT_USAGE`` is 2 because that is what argparse already uses for a usage
# error, so malformed invocation needs no special handling. ``EXIT_NEEDS_APPROVAL``
# is deliberately a *different* code rather than a variant of a failure: in
# ``--classify-only`` mode an approval-required operation is not a refusal, and
# a caller needs to distinguish "this would have run" from "this would have
# asked".
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_NEEDS_APPROVAL = 3
EXIT_INTERNAL = 4

#: No model in the requested capability tier could serve the request. A sixth
#: code rather than a reuse of EXIT_REFUSED, because "the policy said no" and
#: "there was nothing to run" are different facts: the first is a decision, the
#: second is a capability gap. A script branching on either would silently
#: conflate them if they shared a code. Shares EXIT_REFUSED's *class* -- both
#: mean "this did not happen" -- while staying distinguishable.
EXIT_MODEL_UNAVAILABLE = 5

# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------
#
# Two spellings, deliberately. The label form is for a person reading a
# terminal; the token form is for ``--json`` and is stable enough to script
# against. Keeping them separate means the human-facing wording can be reworded
# later without breaking anything that parses the JSON.
STATE_ALLOW = "ALLOW"
STATE_APPROVAL_REQUIRED = "APPROVAL REQUIRED"
STATE_APPROVED = "APPROVED"
STATE_REJECTED = "REJECTED"
STATE_DENIED_BY_POLICY = "DENIED BY POLICY"
STATE_DENIED_UNATTENDED = "DENIED UNATTENDED"

_TOKENS = {
    STATE_ALLOW: "ALLOW",
    STATE_APPROVAL_REQUIRED: "APPROVAL_REQUIRED",
    STATE_APPROVED: "APPROVED",
    STATE_REJECTED: "REJECTED",
    STATE_DENIED_BY_POLICY: "DENIED_BY_POLICY",
    STATE_DENIED_UNATTENDED: "DENIED_UNATTENDED",
}

# Every refusal shares one code. The rendered state and the JSON token tell them
# apart; expanding the exit-code contract per failure mode would turn a stable
# four-value contract into a documented enum that grows every time a rule does.
EXIT_FOR_STATE = {
    STATE_ALLOW: EXIT_OK,
    STATE_APPROVAL_REQUIRED: EXIT_NEEDS_APPROVAL,
    STATE_APPROVED: EXIT_OK,
    STATE_REJECTED: EXIT_REFUSED,
    STATE_DENIED_BY_POLICY: EXIT_REFUSED,
    STATE_DENIED_UNATTENDED: EXIT_REFUSED,
}

# Shown when an operation was approved. This line exists to prevent the most
# damaging possible misreading of this CLI: that approving something meant it
# ran. Nothing here executes. The wording says so plainly rather than implying
# a dry run, because "would run" invites the reader to assume the effects landed.
NO_EXECUTION_NOTE = (
    "approved by requester; execution is not implemented yet, so nothing ran"
)


def state_for_verdict(verdict: CommandVerdict | SecretVerdict) -> str:
    """The label for a classification, before any approval is attempted.

    Reports what the classifier decided. It does not decide anything itself --
    the mapping from ``Classification`` to a label is a rendering concern, and
    an ``ALLOW`` verdict stays ``ALLOW`` here even when a caller intends to go
    on to ask about it.
    """
    if verdict.classification is Classification.ALLOW:
        return STATE_ALLOW
    if verdict.classification is Classification.DENY:
        return STATE_DENIED_BY_POLICY
    return STATE_APPROVAL_REQUIRED


def state_for_classification(classification: Classification) -> str:
    """The label for a bare :class:`Classification`.

    Used where the effective classification has already been ranked -- by the
    approval layer, or by :func:`effective_classification` -- and only the label
    is needed. The verdict-level helper below stays the entry point for callers
    holding a single verdict.
    """
    if classification is Classification.ALLOW:
        return STATE_ALLOW
    if classification is Classification.DENY:
        return STATE_DENIED_BY_POLICY
    return STATE_APPROVAL_REQUIRED


def state_for_outcome(outcome: ApprovalOutcome) -> str:
    """The label for a settled approval outcome.

    An outcome that was approved *without* a prompt is reported as ``ALLOW``,
    because that is what happened: the classification allowed it and no human
    was involved. An outcome approved by an operator is ``APPROVED``. The two
    are different events and both are worth distinguishing.
    """
    if outcome.decision is ApprovalDecision.APPROVED:
        if outcome.operator_response is None:
            return STATE_ALLOW
        return STATE_APPROVED
    if outcome.decision is ApprovalDecision.REJECTED:
        return STATE_REJECTED
    if outcome.decision is ApprovalDecision.DENIED_UNATTENDED:
        return STATE_DENIED_UNATTENDED
    return STATE_DENIED_BY_POLICY


def _command_text(verdict: CommandVerdict | None, detail: str | None) -> str | None:
    if verdict is not None:
        return " ".join([verdict.program, *verdict.args]).rstrip()
    return detail


def _targets_text(targets: tuple[Any, ...]) -> str:
    if not targets:
        return NOT_DETERMINABLE
    return ", ".join(str(t) for t in targets)


def _lines(label: str, pairs: list[tuple[str, str]]) -> list[str]:
    """State label, then indented fields.

    The first column is fixed width so repeated invocations line up and an
    operator scanning two results vertically reads one column, not ragged text.
    """
    out = [label]
    for name, value in pairs:
        out.append(f"  {name:<12} {value}")
    return out


def render_verdict(
    verdict: CommandVerdict | SecretVerdict,
    *,
    command: str | None = None,
    workspace: str | None = None,
    extra_reason: str | None = None,
    state: str | None = None,
    secret_verdicts: tuple[SecretVerdict, ...] = (),
) -> str:
    """Render a classification on its own, with no approval attempted.

    Used by ``--classify-only``. ``state`` is supplied by the caller when the
    effective classification came from ranking several verdicts together; the
    caller passes it rather than having it recomputed here, so this module never
    ranks anything.

    ``secret_verdicts`` names the rule that escalated the operation, so an
    operator sees why an ordinary-looking command needs a decision. Showing
    "APPROVAL REQUIRED" on `cat secrets.json` with no stated reason would be a
    prompt without information.
    """
    if state is None:
        state = state_for_verdict(verdict)

    targets: tuple[Any, ...] = ()
    if isinstance(verdict, CommandVerdict):
        targets = verdict.targets_resolved
    elif verdict.resolved is not None:
        targets = (verdict.resolved,)

    resolved_workspace = workspace
    if resolved_workspace is None:
        resolved_workspace = (
            str(verdict.cwd) if isinstance(verdict, CommandVerdict) else ""
        )

    pairs: list[tuple[str, str]] = []

    text = _command_text(verdict if isinstance(verdict, CommandVerdict) else None, command)
    if text:
        pairs.append(("command", text))
    if resolved_workspace:
        pairs.append(("workspace", resolved_workspace))

    escalating = next(
        (
            s
            for s in secret_verdicts
            if state == STATE_APPROVAL_REQUIRED
            and s.classification is Classification.APPROVAL_REQUIRED
        ),
        None,
    )

    if escalating is not None:
        pairs.append(("rule", escalating.rule))
    else:
        pairs.append(("rule", verdict.rule))

    pairs.append(("targets", _targets_text(targets)))

    if escalating is not None:
        pairs.append(
            (
                "reason",
                f"secret source requires approval: {escalating.rule}: "
                f"{escalating.reason}",
            )
        )
    else:
        pairs.append(("reason", extra_reason or verdict.reason))

    if state == STATE_APPROVAL_REQUIRED:
        pairs.append(
            (
                "note",
                "approval required; run without --classify-only to ask",
            )
        )
    elif state == STATE_ALLOW:
        pairs.append(("note", "automatic; no approval required"))

    return "\n".join(_lines(state, pairs))


def render_outcome(
    outcome: ApprovalOutcome,
    *,
    verdict: CommandVerdict | SecretVerdict | None = None,
    command: str | None = None,
) -> str:
    """Render a settled approval outcome.

    For ``APPROVED`` the note states plainly that nothing ran. That is the single
    most important sentence this CLI prints, and it is not decoration: an
    operator who approves ``rm -rf build`` and sees only ``APPROVED`` has every
    reason to believe the directory is gone.
    """
    state = state_for_outcome(outcome)
    request = outcome.request

    targets: tuple[Any, ...] = ()
    if verdict is not None and isinstance(verdict, CommandVerdict):
        targets = verdict.targets_resolved
    if request is not None and request.targets:
        targets = request.targets

    pairs: list[tuple[str, str]] = []

    text = _command_text(
        verdict if isinstance(verdict, CommandVerdict) else None,
        command or (request.detail if request is not None else None),
    )
    if text:
        pairs.append(("command", text))
    if request is not None:
        pairs.append(("workspace", str(request.workspace)))
        pairs.append(("agent", request.agent))

    rule = request.rule if request is not None else None
    if rule is None and verdict is not None:
        rule = verdict.rule
    if rule:
        pairs.append(("rule", rule))

    if targets:
        pairs.append(("targets", _targets_text(targets)))

    pairs.append(("reason", outcome.detail))

    if state == STATE_APPROVED:
        pairs.append(("note", NO_EXECUTION_NOTE))
    elif state == STATE_ALLOW:
        pairs.append(("note", "automatic; no approval required"))

    return "\n".join(_lines(state, pairs))


def _payload(
    state: str,
    *,
    verdict: CommandVerdict | SecretVerdict | None,
    outcome: ApprovalOutcome | None,
    command: str | None,
    workspace: str | None,
    extra_reason: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body.

    Deliberately small and flat. Every field is either a state the existing
    stack produced or an identifier it supplied; nothing here is computed by the
    CLI. A key is omitted rather than set to null when the stack supplied nothing,
    so a consumer can tell "no target" from "no field".
    """
    request: ApprovalRequest | None = outcome.request if outcome is not None else None

    rule: str | None = None
    reason: str | None = None
    classification: str | None = None
    resolved: str | None = None
    matched: tuple[str, ...] = ()
    targets: tuple[Any, ...] = ()

    if verdict is not None:
        rule = verdict.rule
        reason = verdict.reason
        classification = verdict.classification.value
        matched = verdict.matched_rules
        if isinstance(verdict, CommandVerdict):
            targets = verdict.targets_resolved
        elif verdict.resolved is not None:
            resolved = str(verdict.resolved)
            targets = (verdict.resolved,)

    if outcome is not None:
        reason = outcome.detail
        if outcome.request is not None:
            rule = outcome.request.rule
            classification = outcome.request.classification.value
            matched = outcome.request.matched_rules or matched
            targets = outcome.request.targets or targets
            resolved = resolved or (
                str(outcome.request.workspace) if outcome.request.workspace else None
            )

    body: dict[str, Any] = {"state": _TOKENS[state]}

    if classification is not None:
        body["classification"] = classification
    if rule is not None:
        body["rule"] = rule
    if reason is not None:
        body["reason"] = reason

    text = _command_text(verdict if isinstance(verdict, CommandVerdict) else None, command)
    if text:
        body["command"] = text
    if resolved_workspace := (workspace or ""):
        body["workspace"] = resolved_workspace

    if targets:
        body["targets"] = [str(t) for t in targets]
    if matched:
        body["matched_rules"] = list(matched)
    if outcome is not None:
        body["exit_code"] = EXIT_FOR_STATE[state]
        if outcome.operator_response is not None:
            body["requester_response"] = outcome.operator_response
        if request is not None:
            body["requester"] = request.agent

    return body


def render_json(
    state: str,
    *,
    verdict: CommandVerdict | SecretVerdict | None = None,
    outcome: ApprovalOutcome | None = None,
    command: str | None = None,
    workspace: str | None = None,
) -> str:
    """Render one JSON object for ``--json``.

    Key order is insertion order and is stable, because a scripted consumer
    comparing output should not be defeated by a dict reshuffle.
    """
    return json.dumps(
        _payload(
            state,
            verdict=verdict,
            outcome=outcome,
            command=command,
            workspace=workspace,
        ),
        sort_keys=False,
    )

# --------------------------------------------------------------------------
# Model routing
# --------------------------------------------------------------------------
#
# Presentation only, like everything above: these take a decision the registry
# and router already produced and format it. Neither module is imported here, so
# the reasoning about what is healthy cannot leak into how it is spelled, and a
# reworded label cannot change a health value.
#
# Field widths follow the rest of this module rather than being chosen per
# function, so a `forge route` block and a `forge check` block read as the same
# interface to the same operator.

#: Said plainly when nobody checked. A GREEN tier printed next to the word
#: "unverified" must not read as a confirmed-working model, because nothing
#: contacted the provider. Deliberately a whole word rather than a symbol.
UNVERIFIED_NOTE = "unverified"


def render_route(decision, *, source: str | None = None) -> str:
    """Render one routing decision.

    The first line is the health state, matching ``forge check``'s convention
    that the thing an operator scans for comes first. A routing failure renders
    the same way -- RED, reason underneath -- so a RED capability and a refused
    command look like the same kind of event: something is unavailable, and here
    is why.

    "selected" names primary or fallback and, for a fallback, its position.
    Position is worth showing: the first fallback and the third are different
    cost and behaviour, and an operator reading this after an incident wants to
    know which one was used.
    """
    lines = [decision.health.value]

    pairs: list[tuple[str, str]] = [("agent", decision.agent), ("tier", decision.tier)]

    if decision.usable:
        pairs.append(("model", decision.model))
        pairs.append(("provider", decision.provider))
        selected = decision.selection.value
        if decision.fallback_index:
            selected = f"{selected} #{decision.fallback_index}"
        pairs.append(("selected", selected))
    else:
        pairs.append(("model", "none selected"))

    pairs.append(("reason", decision.reason))
    if decision.tier_reason:
        pairs.append(("routing", decision.tier_reason))
    if source:
        pairs.append(("config", source))

    lines.extend(f"  {name:<12} {value}" for name, value in pairs)
    return "\n".join(lines)


def render_route_json(decision) -> str:
    """One JSON object for ``forge route --json``."""
    return json.dumps(decision.to_json(), sort_keys=False)


#: Column widths for the status table. Fixed and generous enough for the longest
#: shipped model id, so the provider column stays aligned rather than wrapping
#: into a shape an operator has to re-read per row.
_STATUS_TIER = 18
_STATUS_HEALTH = 7
_STATUS_MODEL = 34
_STATUS_PROVIDER = 12
_STATUS_FALLBACKS = 30


def render_model_status(
    *,
    tiers: list,
    source: str,
    verified: bool,
) -> str:
    """Render every capability tier's health as a table.

    A table rather than a set of labelled blocks because the question an
    operator actually has is "which of my three capabilities is red", and that
    is a column-reading task. Each row carries the model that would be used, so
    a GREEN next to an unexpected id is visible without a second command.

    ``tiers`` is the router's status list. This function decides nothing about
    it -- not the health, not the model, not the slot position.
    """
    header = (
        f"{'TIER':<{_STATUS_TIER}} {'HEALTH':<{_STATUS_HEALTH}} "
        f"{'MODEL':<{_STATUS_MODEL}} {'PROVIDER':<{_STATUS_PROVIDER}} "
        f"{'SELECTED':<11} FALLBACKS"
    )

    rows = [header]
    for entry in tiers:
        model = entry["model"] or "-"
        provider = entry["provider"] or "-"
        rows.append(
            f"{entry['tier']:<{_STATUS_TIER}} "
            f"{entry['health']:<{_STATUS_HEALTH}} "
            f"{model:<{_STATUS_MODEL}} "
            f"{provider:<{_STATUS_PROVIDER}} "
            f"{entry['slot']:<11} {_fallback_list(entry)}"
        )

    lines = ["\n".join(rows), ""]
    lines.append(f"  config        {source}")

    if verified:
        lines.append(
            "  availability  reported by an injected source; health is verified"
        )
    else:
        lines.append(
            f"  availability  {UNVERIFIED_NOTE}: no availability source was "
            "supplied, so no provider was contacted. Health reflects "
            "configuration and gateway availability only."
        )

    return "\n".join(lines)


def _fallback_list(entry: dict) -> str:
    """The tier's fallback models, in declaration order.

    Shown even when the primary is healthy. That is the whole point of the
    column: when the primary does go down, the operator wants to see what is
    standing behind it without running a second command to find out.
    """
    fallbacks = [
        slot
        for slot in entry.get("slots", [])
        if slot.get("position", 0) > 0
    ]
    if not fallbacks:
        return "-"
    return ", ".join(
        f"{slot['model']}{'' if slot.get('enabled', True) else ' (disabled)'}"
        for slot in fallbacks
    )


def render_model_status_json(entries: list) -> str:
    """One JSON object per tier, newline-delimited.

    Newline-delimited rather than a list under a wrapper key: ``forge check path
    --json`` already emits one object per line, so a consumer learns one format
    for the CLI. Insertion order is preserved, matching ``render_json``.
    """
    return "\n".join(json.dumps(entry, sort_keys=False) for entry in entries)
