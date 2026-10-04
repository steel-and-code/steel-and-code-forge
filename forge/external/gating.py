"""Gate the task text before any external agent is spawned.

**Forge cannot classify what it cannot see.** An external agent's tool calls
happen inside another process; Forge is not in that loop and never will be. So
the task text is the *only* thing available to gate, and this module exists to
gate it honestly rather than pretend the problem away.

The ruling this implements, from the Stage 3 MVP decision:

=============== ==========================================================
Gate verdict    What Forge does
=============== ==========================================================
``allow``       spawn the agent
``approval``    refuse, and say the capability is not supported yet
``deny``        refuse
classification  refuse -- fail closed, spawn nothing
failure
=============== ==========================================================

Failing closed is the whole point. An external agent granted no toolsets is a
much smaller risk than one that was spawned because a gate threw.

How the text is classified, and why it is not just handed to
:class:`~forge.tools.commands.CommandClassifier`
---------------------------------------------------------------------

Feeding prose to the command classifier does not work, and the reason is worth
recording because it looks like it should. ``CommandClassifier``'s default for
an unrecognised program is ``approval_required`` -- deliberately, so an unknown
command prompts rather than executing. Run a sentence through it as though the
first word were a program name and *every* task classifies as
approval-required, including "what does this project do?". A gate that refuses
everything is not a gate; it is a wall with a door that is painted on.

So the gate is built from the same rules, applied honestly to text:

*   **Credential-shaped text is denied outright.** A path-looking token that
    names a secret source -- ``.env``, ``id_rsa``, ``*.pem``, ``.aws/`` -- is
    handed to :class:`~forge.tools.secrets.SecretClassifier`, which is a real
    Forge classifier, and its verdict is used directly. Deny is final.
*   **Shell-escaping text is refused.** Backticks, ``$(``, and ``|`` in a task
    mean the operator is asking for command execution. That is a capability the
    external-agent MVP does not grant, so it is refused with an explanation
    rather than passed through to a child that would run it.
*   **Prose is prose.** Everything else is ``allow``.

That last rule is the deliberate asymmetry with :mod:`forge.tools.commands`: a
native agent's *tool calls* are all classified, because Forge sees every one.
An external agent's *task* is the only artefact, and refusing ordinary English
would make the command unusable without making it any safer -- the capability
being withheld is withheld by the empty toolset either way.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..tools.secrets import SecretClassifier
from ..tools.workspace import Classification, Workspace

__all__ = ["GateDecision", "TaskGate", "gate_task"]


#: Tokens whose presence means the task is asking for command execution. These
#: are *not* secrets and are not commands Forge would run -- they are a signal
#: that the request is of a kind this MVP refuses, so the operator gets an
#: explanation instead of a silent capability grant.
SHELL_ESCAPE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"`"), "backtick command substitution"),
    (re.compile(r"\$\("), "shell command substitution $(...)"),
    (re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|k|)sh\b"), "pipe into a shell"),
    (re.compile(r"\bsudo\b"), "a request to run as root"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f\b"), "a recursive force delete"),
)


#: Path-looking tokens, extracted for secret classification. Deliberately
#: conservative: it must catch credential filenames without matching ordinary
#: prose.
#:
#: The extension is part of the credential stem on purpose. Forge's rules match
#: ``credentials.json`` and ``kubeconfig.yaml`` but not the bare words
#: ``credentials`` or ``kubeconfig``, so capturing only the stem produced a token
#: the classifier then answered ``allow`` for -- a gate that extracted the
#: evidence and then threw it away.
SECRET_PATH_PATTERN = re.compile(
    r"(?<![\w/.-])"
    r"(?:~|\.{1,2})?/?"
    r"(?:[\w.-]+/)*"
    r"\.env(?:\.[\w-]+)?"
    r"(?![\w-])"
    r"|(?<![\w/.-])"
    r"(?:~|\.{1,2})?/?(?:[\w.-]+/)*"
    r"[\w.-]*"
    r"(?:id_rsa|id_dsa|id_ecdsa|id_ed25519|\.pem|\.key|\.p12|\.pfx|"
    r"credentials|secrets?|service[-_]?account|"
    r"kubeconfig|\.netrc|\.pgpass|\.htpasswd|\.npmrc|"
    r"\.pypirc)"
    r"(?:\.(?:json|ya?ml|toml|ini|cfg|conf|txt))?"
    r"(?![\w-])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateDecision:
    """Whether Forge may spawn an external agent, and why.

    Attributes:
        classification: The gate's verdict, one of Forge's
            :class:`~forge.tools.workspace.Classification` values. Reusing the
            enum rather than inventing a parallel one is deliberate: an operator
            reading ``deny`` should mean the same thing here as everywhere else
            in Forge.
        reason: Why, in operator-readable terms. Never empty, so a refusal can
            always be explained.
        rule: A stable identifier for the rule that fired, for tests and for any
            future audit surface.
        detail: The offending fragment, when there is one. Bounded -- this is
            operator-facing text, not a transcript.
    """

    classification: Classification
    reason: str = ""
    rule: str = ""
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """Whether Forge may spawn.

        Strictly ``allow``. Both other verdicts refuse, including
        ``approval_required``: an external agent's capabilities cannot be
        negotiated at runtime, so an "ask the human" answer has nowhere to go.
        That refusal is the MVP's honest position -- see the package docstring.
        """
        return self.classification is Classification.ALLOW


class TaskGate:
    """Classifies task text for an external-agent spawn.

    Args:
        workspace: The run's explicit root. Passed to
            :class:`~forge.tools.secrets.SecretClassifier`, whose containment
            check is what makes an out-of-workspace path a denial rather than a
            guess.
    """

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._secrets = SecretClassifier(workspace)

    def classify(self, task: str) -> GateDecision:
        """Classify ``task``. Never raises for a bad verdict -- see ``gate_task``."""
        text = task or ""

        for pattern, description in SHELL_ESCAPE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return GateDecision(
                    classification=Classification.DENY,
                    reason=(
                        f"the task asks for shell execution ({description}), and "
                        f"the external-agent MVP grants no command capability. "
                        f"forge hermes runs text-only; use forge byte or forge "
                        f"neighbor for work that needs tools."
                    ),
                    rule="external.shell_escape",
                    detail=match.group(0)[:80],
                )

        for candidate in self._secret_candidates(text):
            try:
                verdict = self._secrets.classify(Path(candidate))
            except Exception:  # noqa: BLE001 - fail closed below
                return GateDecision(
                    classification=Classification.DENY,
                    reason=(
                        "the secret classifier could not evaluate the task, so "
                        "forge declined to spawn an external agent. Failing "
                        "closed is deliberate: an ungated spawn is worse than a "
                        "refused one."
                    ),
                    rule="external.classifier_failure",
                    detail=candidate[:80],
                )
            if verdict.classification is Classification.ALLOW:
                continue

            # Anything other than allow is a refusal here, *including*
            # approval_required. That is not the gate being strict -- it is the
            # gate refusing to answer a question it cannot put to anyone. A
            # native agent's read of `kubeconfig` would raise an approval prompt
            # the operator can answer; an external agent has no such moment, so
            # an approval-required secret source resolves the same way a denied
            # one does: forge does not pass it on.
            return GateDecision(
                classification=(
                    Classification.DENY
                    if verdict.classification is Classification.DENY
                    else Classification.APPROVAL_REQUIRED
                ),
                reason=(
                    f"the task references a secret source ({verdict.rule}), "
                    f"which forge will not pass to an external agent. This "
                    f"capability is not supported by the external-agent MVP yet."
                ),
                rule="external.secret_source",
                detail=candidate[:80],
            )

        return GateDecision(
            classification=Classification.ALLOW,
            reason="no denied capability or secret source in the task text",
            rule="external.text_only",
        )

    def _secret_candidates(self, text: str) -> tuple[str, ...]:
        """Path-looking tokens in ``text``, deduplicated, order preserved.

        ``.env.example``-style documentation references are dropped: the
        classifier is asked about the *real* name and would otherwise deny a
        question about a template file. This is a precision choice, and it is
        the only place the gate is lenient -- a template contains no secret.

        A bare secret-ish word is **anchored to the workspace root** before
        classification. That is not a policy of this module's own: it hands the
        classifier a path so its own containment rule applies. Without it,
        ``credentials`` classifies ``allow`` (Forge's rules match
        ``credentials.json``, not the bare stem) while ``.aws/credentials``
        classifies ``deny`` purely for being outside the workspace -- so the
        bare word would slip past precisely when the qualified one would not.
        Anchoring keeps the decision in one place, in the classifier.
        """
        root = self._workspace.root
        found: list[str] = []
        for match in SECRET_PATH_PATTERN.finditer(text):
            token = match.group(0).strip()
            if not token or token in found:
                continue
            # A suffixed .env.<example|sample|template|dist> is documentation.
            if re.search(r"\.env\.(?:example|sample|template|dist)$", token, re.I):
                continue
            if not os.path.isabs(token) and not token.startswith("~"):
                token = str(root / token)
            found.append(token)
        return tuple(found)


def gate_task(task: str, workspace: Workspace) -> GateDecision:
    """Classify ``task`` for an external-agent spawn, failing closed.

    The single entry point the CLI uses. It wraps :class:`TaskGate` so that an
    unexpected failure becomes a refusal rather than an exception escaping at the
    spawn site -- the one place where an error would otherwise be most damaging,
    because it is immediately before a subprocess starts.
    """
    try:
        return TaskGate(workspace).classify(task)
    except Exception as exc:  # noqa: BLE001 - deliberate boundary
        return GateDecision(
            classification=Classification.DENY,
            reason=(
                f"forge could not classify the task ({exc.__class__.__name__}), "
                f"so it declined to spawn an external agent"
            ),
            rule="external.gate_failure",
        )