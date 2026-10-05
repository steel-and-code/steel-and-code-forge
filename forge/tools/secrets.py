"""Secret-source classification.

Implements List 1 of ``docs/byte-security-classification.md``: which files
Forge treats as secret-bearing, at which tier, and what the resulting
classification is.

The governing principle, from the spec:

    Location or structure proves sensitivity  -> deny.
    Filename merely suggests sensitivity       -> approval_required.

Plus one content-proven rule: a file containing a PEM ``PRIVATE KEY`` block is
denied regardless of its filename, because no path list can catch a private key
under an arbitrary name.

This module classifies paths. It does not execute anything, and it never
returns file contents to a caller.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .workspace import Classification, Resolution, Workspace, strictest

__all__ = [
    "SecretVerdict",
    "SecretClassifier",
    "Rule",
    "STRUCTURAL_RULES",
    "NAME_GUESS_RULES",
    "PRIVATE_KEY_MARKERS",
    "CREDENTIAL_KEY_NAMES",
    "MAX_CONTENT_INSPECTION_BYTES",
]

# PEM headers that prove private-key material. Matched on the first
# ``MAX_CONTENT_INSPECTION_BYTES`` of a candidate file.
#
# ``BEGIN CERTIFICATE`` is deliberately absent: a certificate is public and
# blocking it would be a false positive on a large class of ordinary files.
PRIVATE_KEY_MARKERS = (
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)

# Credential-looking words in ordinary file content are intentionally not a
# classification signal. Filename rules and PEM markers below are the stronger,
# reviewable protections; scanning arbitrary prose and source creates approval
# fatigue without proving that a file contains credentials.

# Kept as an empty compatibility constant for callers that imported the old
# evidence list; it is deliberately not consulted during classification.
CREDENTIAL_KEY_NAMES: tuple[str, ...] = ()

# How much of a candidate file to read for content checks. Bounded so that
# classification never becomes an unbounded read of an arbitrarily large file.
MAX_CONTENT_INSPECTION_BYTES = 64 * 1024

# NUL byte in the prefix strongly implies binary. Binary files are not scanned
# for credential key names -- a compiled artefact will match anything.
_BINARY_NUL = b"\x00"


@dataclass(frozen=True)
class Rule:
    """A single classification rule.

    Attributes:
        rule_id: Stable identifier, surfaced in results and used by the approval
            UX to explain a decision.
        pattern: The glob this rule matches against the candidate path.
        reason: Human-readable explanation. Safe for an operator.
        tier: ``"structural"`` (deny) or ``"name_guess"`` (approval_required).
        paths_are: Which path component set the pattern is evaluated against --
            the full absolute path, or just the basename. Splitting these keeps
            ``~/.ssh/id_rsa`` from matching on a project-local ``id_rsa`` by
            accident while still denying both where appropriate.
    """

    rule_id: str
    pattern: str
    reason: str
    tier: str
    paths_are: str = "basename"

    @property
    def classification(self) -> Classification:
        return (
            Classification.DENY
            if self.tier == "structural"
            else Classification.APPROVAL_REQUIRED
        )


def _structural(
    rule_id: str, pattern: str, reason: str, paths_are: str = "basename"
) -> Rule:
    return Rule(rule_id, pattern, reason, "structural", paths_are)


def _guess(rule_id: str, pattern: str, reason: str, paths_are: str = "basename") -> Rule:
    return Rule(rule_id, pattern, reason, "name_guess", paths_are)


# --------------------------------------------------------------------------
# Structural rules. Location or format proves these hold credentials. Denied.
# --------------------------------------------------------------------------
# Filenames that match an ``.env*`` shape but hold no values. These are checked
# into repositories precisely because they document variables rather than define
# them, and they are the one file that tells an agent what a project expects.
ENV_EXAMPLE_SUFFIXES = ("example", "template", "sample", "dist")


def _is_env_example(name: str) -> bool:
    """Whether ``name`` is a non-secret ``.env``-family documentation file."""
    lowered = name.casefold()
    if not lowered.startswith(".env"):
        return False
    tail = lowered[4:].lstrip(".")
    return tail in ENV_EXAMPLE_SUFFIXES


STRUCTURAL_RULES: tuple[Rule, ...] = (
    # .env family. Matching is `.env*` rather than `.env`, because `.env.local`,
    # `.env.staging` and `.env.production` are all secret-bearing and a bare
    # `.env` glob would miss every one of them. `.env.example` and friends are
    # excluded -- see `_is_env_example`.
    _structural("secret.env", ".env*", "local environment file, conventionally secret-bearing"),
    _structural("secret.env", "*.env", "environment file with a non-standard name"),
    _structural("secret.envrc", ".envrc", "direnv loader, frequently exports secrets"),
    # SSH
    _structural("secret.ssh.id", "id_rsa", "SSH private key, default name"),
    _structural("secret.ssh.id", "id_dsa", "SSH private key, default name"),
    _structural("secret.ssh.id", "id_ecdsa", "SSH private key, default name"),
    _structural("secret.ssh.id", "id_ed25519", "SSH private key, default name"),
    _structural("secret.ssh.ppk", "*.ppk", "PuTTY private key"),
    # Keystore / bundle formats. These always contain private material or a key
    # protected by one.
    _structural("secret.store.p12", "*.p12", "PKCS#12 bundle, contains private material"),
    _structural("secret.store.pfx", "*.pfx", "PKCS#12 bundle, contains private material"),
    _structural("secret.store.jks", "*.jks", "Java keystore, contains private material"),
    _structural("secret.store.keystore", "*.keystore", "Java keystore, contains private material"),
    _structural("secret.store.kdbx", "*.kdbx", "KeePass database"),
    _structural("secret.store.1pif", "*.1pif", "1Password export"),
    _structural("secret.store.agile", "*.agilekeychain", "1Password export"),
    _structural("secret.keytab", "*.keytab", "Kerberos keytab, contains key material"),
    # Authentication files
    _structural("secret.netrc", ".netrc", "machine-wide login credentials"),
    _structural("secret.netrc", "_netrc", "machine-wide login credentials"),
    _structural("secret.authinfo", ".authinfo", "machine-wide login credentials"),
    _structural("secret.authinfo", ".authinfo-gpg", "encrypted machine-wide credentials"),
    _structural("secret.git_credentials", ".git-credentials", "plaintext git credential store"),
    _structural("secret.docker_config", "config.json", "Docker registry auth file", "docker_dir"),
    _structural("secret.gh_hosts", "hosts.yml", "GitHub CLI auth token", "gh_dir"),
    # Credential directories. `full_path` rules are constrained to a specific
    # directory suffix so a project-local `.git/config` is not swept up by the
    # `.git` rule -- only the user-level one is denied here.
    _structural("secret.dir.aws", "*", "file under the AWS credentials directory", "aws_dir"),
    _structural("secret.dir.azure", "*", "file under the Azure credentials directory", "azure_dir"),
    _structural("secret.dir.gcloud", "*", "file under the GCP credentials directory", "gcloud_dir"),
    _structural("secret.dir.kube", "config", "kubeconfig, location proves it holds credentials", "kube_dir"),
    _structural("secret.dir.gnupg", "*", "file under the GnuPG keyring", "gnupg_dir"),
    _structural("secret.dir.password_store", "*", "file under the pass credential store", "password_store_dir"),
    _structural("secret.dir.ssh", "*", "file under the SSH directory", "ssh_dir"),
    # Structurally proven single files
    _structural("secret.service_account", "service-account*.json", "GCP service account key"),
    _structural("secret.gcp_adc", "application_default_credentials.json", "GCP application default credentials"),
    _structural("secret.htpasswd", ".htpasswd", "HTTP basic auth hashes"),
    _structural("secret.vault_token", "vault-token", "HashiCorp Vault token"),
    _structural("secret.vault_token", ".vault-token", "HashiCorp Vault token"),
)

# --------------------------------------------------------------------------
# Name-guess rules. The filename suggests sensitivity but does not prove it.
# approval_required. Per the spec's principle, denying these would block
# legitimate reads and teach the operator that the rule is arbitrary.
# --------------------------------------------------------------------------
NAME_GUESS_RULES: tuple[Rule, ...] = (
    _guess("guess.secrets_json", "secrets.json", "filename suggests stored secrets"),
    _guess("guess.secrets_yaml", "secrets.yaml", "filename suggests stored secrets"),
    _guess("guess.secrets_yml", "secrets.yml", "filename suggests stored secrets"),
    _guess("guess.secret_json", "secret.json", "filename suggests stored secrets"),
    _guess("guess.secret_yaml", "secret.yaml", "filename suggests stored secrets"),
    _guess("guess.secrets_ext", "*.secrets", "filename suggests stored secrets"),
    _guess("guess.credentials_json", "credentials.json", "filename suggests stored credentials"),
    _guess("guess.credentials_yaml", "credentials.yaml", "filename suggests stored credentials"),
    _guess("guess.credentials_yml", "credentials.yml", "filename suggests stored credentials"),
    _guess("guess.password_ext", "*.password", "filename suggests a stored password"),
    _guess("guess.token_ext", "*.token", "filename suggests a stored token"),
    # A kubeconfig outside ~/.kube/ is a name guess: the convention is real but
    # an application kubeconfig may hold no credentials at all.
    _guess("guess.kubeconfig", "kubeconfig", "filename suggests cluster credentials"),
    _guess("guess.kubeconfig", "kubeconfig.*", "filename suggests cluster credentials"),
    _guess("guess.kubeconfig", "*.kubeconfig", "filename suggests cluster credentials"),
    _guess("guess.k8s_secret", "k8s-secret*.yaml", "manifest with inline secret data"),
)


@dataclass(frozen=True)
class SecretVerdict:
    """The structured result of classifying a path.

    Carries more than a boolean so the approval UX can render a real prompt
    later: which rule fired, why, which path it fired on.

    Attributes:
        classification: Final verdict after all rules and strictest-match-wins.
        rule: Identifier of the deciding rule, e.g. ``"secret.ssh.id"``, or
            ``"none"``.
        reason: Human-readable explanation of the deciding rule.
        resolved: The real resolved path, when resolution succeeded.
        exists: Whether the resolved path exists.
        tier: ``"structural"``, ``"name_guess"``, or ``None``.
        matched_rules: Every rule that matched, not only the deciding one. Useful
            for the approval prompt, which may want to name more than one.
        containment: The underlying workspace containment result, so callers can
            inspect why a path was rejected for location rather than content.
        content_checked: Whether a content inspection was attempted and
            succeeded.
        content_failure: Populated when content inspection could not be
            performed safely. Per spec this fails closed, so
            ``classification`` will be ``DENY``.
    """

    classification: Classification
    rule: str
    reason: str
    resolved: Path | None
    exists: bool
    tier: str | None = None
    matched_rules: tuple[str, ...] = ()
    containment: Resolution | None = None
    content_checked: bool = False
    content_failure: str | None = None

    @property
    def allowed(self) -> bool:
        return self.classification is Classification.ALLOW

    @property
    def denied(self) -> bool:
        return self.classification is Classification.DENY

    @property
    def requires_approval(self) -> bool:
        return self.classification is Classification.APPROVAL_REQUIRED

    def __str__(self) -> str:  # pragma: no cover - operator convenience
        target = self.resolved if self.resolved is not None else "<unresolved>"
        return f"{self.classification.value}: {self.rule}: {target}"


class SecretClassifier:
    """Classifies paths against the v1 secret-source lists.

    Args:
        workspace: The single explicit workspace root. Containment is checked
            before content, because containment is the check that can be
            relied on absolutely.
        inspect_content: Whether to read candidate files looking for PEM private
            key markers. Content inspection requires the file to exist and be
            readable. Set ``False`` to run a pure path-based classification.
        inspect_content_keys: Retained for API compatibility. Ordinary content
            words are not classification signals.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        inspect_content: bool = True,
        inspect_content_keys: bool = True,
    ) -> None:
        self.workspace = workspace
        self.inspect_content = inspect_content
        self.inspect_content_keys = inspect_content_keys

    # -- rule matching ---------------------------------------------------

    @staticmethod
    def _match(rule: Rule, resolved: Path) -> bool:
        """Whether ``rule`` matches ``resolved``.

        ``paths_are`` selects the comparison target so that a directory-scoped
        rule does not accidentally match a same-named file elsewhere.
        """
        name = resolved.name
        full = str(resolved)

        if rule.paths_are == "basename":
            # `.env.example` and friends are documented, not secret. Checked
            # before the glob so the exclusion cannot be lost in a pattern
            # change, and so it applies to the whole `.env*` family at once.
            if rule.rule_id == "secret.env" and _is_env_example(name):
                return False
            return fnmatch.fnmatch(name.casefold(), rule.pattern.casefold())

        # Directory-scoped rules. Matched against the real path with a
        # separator-terminated suffix so `/work/.ssh` does not match
        # `/work/.sshx`.
        def in_dir(suffix: str) -> bool:
            needle = suffix.replace("~", str(Path.home()))
            needle = needle.rstrip("/")
            return full == needle or full.startswith(needle + os.sep)

        if rule.paths_are == "full_path":
            # Generic `full_path`: currently unused by shipped rules but kept so
            # adding one is a data change.
            return fnmatch.fnmatch(full.casefold(), rule.pattern.casefold())
        if rule.paths_are == "ssh_dir":
            return in_dir("~/.ssh")
        if rule.paths_are == "aws_dir":
            return in_dir("~/.aws")
        if rule.paths_are == "azure_dir":
            return in_dir("~/.azure")
        if rule.paths_are == "gcloud_dir":
            return in_dir("~/.gcloud") or in_dir("~/.config/gcloud")
        if rule.paths_are == "kube_dir":
            return in_dir("~/.kube") and name == rule.pattern
        if rule.paths_are == "gnupg_dir":
            return in_dir("~/.gnupg")
        if rule.paths_are == "password_store_dir":
            return in_dir("~/.password-store")
        if rule.paths_are == "docker_dir":
            return in_dir("~/.docker") and name == rule.pattern
        if rule.paths_are == "gh_dir":
            return in_dir("~/.config/gh") and name == rule.pattern

        # Unknown scope: fail closed rather than silently not matching.
        raise ValueError(f"unknown paths_are scope: {rule.paths_are!r}")

    def _path_matches(self, resolved: Path) -> list[Rule]:
        matched = [
            rule
            for rule in (*STRUCTURAL_RULES, *NAME_GUESS_RULES)
            if self._match(rule, resolved)
        ]
        # Deterministic order so a verdict is reproducible. Structural first, so
        # the deciding rule is the strongest.
        matched.sort(key=lambda r: (r.classification.severity * -1, r.rule_id))
        return matched

    # -- content checks --------------------------------------------------

    def _read_prefix(self, path: Path) -> bytes:
        with path.open("rb") as handle:
            return handle.read(MAX_CONTENT_INSPECTION_BYTES)

    def _check_content(
        self, resolved: Path
    ) -> tuple[str | None, str | None, str | None, bool]:
        """Inspect a candidate file for secret material.

        Returns ``(deny_rule, approval_rule, detail, checked)``. The approval
        result is retained in the return shape for compatibility, but ordinary
        credential-looking words are not inspected.

        Fails closed: any inability to read the file safely returns a denial
        rule rather than a clean result. The classification that produces is
        ``DENY`` with ``rule="secret.content_unreadable"``, because a file Forge
        cannot read is a file Forge cannot clear. The alternative -- treating it
        as clean -- would mean any unreadable file is a secret-free file, which
        is exactly the wrong default for a security check.

        A path that does not exist is *not* a failure: it cannot contain
        private-key material. It returns no match with ``checked=False``.
        """
        unreadable = "secret.content_unreadable"

        if not resolved.exists() or resolved.is_dir():
            return None, None, None, False

        try:
            prefix = self._read_prefix(resolved)
        except PermissionError as exc:
            return unreadable, None, exc.strerror or "permission denied", False
        except OSError as exc:
            return unreadable, None, str(exc), False

        if not prefix:
            return None, None, None, True

        # Private key markers: content-proven, denied, filename irrelevant.
        for marker in PRIVATE_KEY_MARKERS:
            if marker.encode() in prefix:
                detail = f"file contains private-key material ({marker.strip('- ')})"
                return "secret.private_key_material", None, detail, True


        return None, None, None, True

    # -- public API ------------------------------------------------------

    def classify(self, candidate: str | os.PathLike[str]) -> SecretVerdict:
        """Classify ``candidate`` against the v1 secret-source lists.

        Order of evaluation:

        1. Workspace containment. A path outside the workspace is denied
           regardless of what it is called.
        2. Path-based rules, both tiers.
        3. Content inspection, if enabled and the file exists.

        The verdict is the strictest of everything that matched.
        """
        resolution = self.workspace.resolve(candidate)

        if not resolution.allowed:
            return SecretVerdict(
                classification=Classification.DENY,
                rule=resolution.rule,
                reason=resolution.reason,
                resolved=resolution.resolved,
                exists=resolution.exists,
                tier="structural",
                matched_rules=(resolution.rule,),
                containment=resolution,
            )

        resolved = resolution.resolved
        assert resolved is not None  # guaranteed by the allow above

        matched = self._path_matches(resolved)
        matched_ids = tuple(rule.rule_id for rule in matched)

        candidates: list[Classification] = [Classification.ALLOW]
        deciding: Rule | None = None
        reason = "no secret-source rule matched"

        for rule in matched:
            candidates.append(rule.classification)
            if rule.classification.severity > Classification.ALLOW.severity:
                # strictest wins; keep the first rule at the winning severity
                if (
                    deciding is None
                    or rule.classification.severity > deciding.classification.severity
                ):
                    deciding = rule
                    reason = rule.reason

        content_checked = False
        content_failure: str | None = None

        if self.inspect_content:
            deny_rule, approval_rule, detail, checked = self._check_content(resolved)
            content_checked = checked

            if deny_rule is not None:
                candidates.append(Classification.DENY)
                if deny_rule == "secret.content_unreadable":
                    # A read failure is the only content result that may
                    # displace an existing path-based decision, because it means
                    # the path rules were never fully evaluated.
                    content_failure = detail
                    deciding = Rule(deny_rule, "", detail or "", "structural")
                    reason = detail or "content check failed closed"
                elif deciding is None or deciding.classification is not Classification.DENY:
                    # Otherwise a structural path match already denies, and its
                    # rule name is the more useful thing to show an operator
                    # than "this file contains private key material".
                    deciding = Rule(deny_rule, "", detail or "", "structural")
                    reason = detail or "content check denied the operation"

            elif approval_rule is not None:
                candidates.append(Classification.APPROVAL_REQUIRED)
                if deciding is None or deciding.classification is Classification.ALLOW:
                    deciding = Rule(approval_rule, "", detail or "", "name_guess")
                    reason = detail or "credential-looking key name in content"

        final = strictest(*candidates)

        # A structural path match already set the tier; fall back to the
        # deciding rule's tier, then None for a clean file.
        tier = deciding.tier if deciding is not None and deciding.classification is final else None

        return SecretVerdict(
            classification=final,
            rule=deciding.rule_id if deciding is not None else "none",
            reason=reason,
            resolved=resolved,
            exists=resolution.exists,
            tier=tier,
            matched_rules=matched_ids,
            containment=resolution,
            content_checked=content_checked,
            content_failure=content_failure,
        )
