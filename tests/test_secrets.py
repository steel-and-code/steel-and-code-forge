"""Tests for secret-source classification.

Covers the two tiers from ``docs/byte-security-classification.md``:

    location or structure proves sensitivity  -> deny
    filename merely suggests sensitivity      -> approval_required

and one content-proven rule: a PEM ``PRIVATE KEY`` block is denied whatever the
filename.

Name-guess rules are tested separately from structural rules, because they are
separate tiers and a regression in one should not mask a regression in the other.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from forge.tools.secrets import SecretClassifier
from forge.tools.workspace import Classification, Workspace

# A realistic RSA private key header block. The body is filler: the marker is
# what proves private-key material.
PRIVATE_KEY_BODY = """-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAx0Z0P1vQ2mR8kL3nT5yU7wX9aB1cD3eF5gH7jK9lM2nO4pQ6rS8
tU0vW2xY4zA6bC8dE0fG2hI4jK6lM8nO0pQ2rS4tU6vW8xY0zA2bC4dE6fG8hI0jK
2lM4nO6pQ8rS0tU2vW4xY6zA8bC0dE2fG4hI6jK8lM0nO2pQ4rS6tU8vW0xY2zA4bC
6dE8fG0hI2jK4lM6nO8pQ0rS2tU4vW6xY8zA0bC2dE4fG6hI8jK0lM2nO4pQ6rS8tU
-----END RSA PRIVATE KEY-----
"""

OPENSSH_KEY_BODY = """-----BEGIN OPENSSH PRIVATE KEY-----
b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW
-----END OPENSSH PRIVATE KEY-----
"""

ORDINARY_CERT = """-----BEGIN CERTIFICATE-----
MIIDazCCAlOgAwIBAgIUAhM2N9nQ6y0d8Zr3vJ7VqLp4k9m2XhR8sT1uW3yZ0aBcD
EfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz01234567
89AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrStUvWxYz0123
-----END CERTIFICATE-----
"""


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


@pytest.fixture()
def classifier(workspace: Workspace) -> SecretClassifier:
    return SecretClassifier(workspace)


def _write(root: Path, name: str, content: str = "x") -> Path:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ==========================================================================
# Explicitly required cases
# ==========================================================================


def test_env_example_is_allowed(classifier: SecretClassifier, workspace: Workspace) -> None:
    """``.env.example`` holds no values -- it is the file documenting the vars.

    Blocking it would deny Byte the one file that tells it what a project wants.
    """
    target = _write(workspace.root, ".env.example", "API_KEY=\nDEBUG=true\n")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW
    assert verdict.allowed
    assert verdict.rule == "none"


def test_secrets_json_is_approval_required(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """A name guess: approval_required, never denied."""
    target = _write(workspace.root, "secrets.json", '{"placeholder": null}')

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.requires_approval
    assert verdict.rule.startswith("guess.")
    assert verdict.tier == "name_guess"


def test_id_rsa_is_denied(classifier: SecretClassifier, workspace: Workspace) -> None:
    """Structural: the default SSH private key name proves it."""
    target = _write(workspace.root, "id_rsa", PRIVATE_KEY_BODY)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.DENY
    assert verdict.denied
    assert verdict.rule == "secret.ssh.id"
    assert verdict.tier == "structural"


def test_id_ed25519_is_denied(classifier: SecretClassifier, workspace: Workspace) -> None:
    target = _write(workspace.root, "id_ed25519", PRIVATE_KEY_BODY)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "secret.ssh.id"


def test_ordinary_crt_is_allowed(classifier: SecretClassifier, workspace: Workspace) -> None:
    """A public certificate is not a secret.

    This is the false positive the spec is explicit about avoiding.
    """
    target = _write(workspace.root, "server.crt", ORDINARY_CERT)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW
    assert verdict.allowed


def test_private_key_material_is_denied_regardless_of_filename(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """Content-proven: no path list can catch a key under any name."""
    innocent_name = _write(workspace.root, "definitely_not_a_key.txt", PRIVATE_KEY_BODY)

    verdict = classifier.classify(innocent_name)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "secret.private_key_material"


def test_openssh_private_key_inert_filename_is_denied(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    target = _write(workspace.root, "notes.txt", OPENSSH_KEY_BODY)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "secret.private_key_material"


def test_private_key_beyond_prefix_is_not_inspected(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """Inspection is bounded, so a marker past the prefix is not found.

    Documents the bound honestly rather than pretending to scan whole files.
    """
    padding = "filler line\n" * 20_000  # well past MAX_CONTENT_INSPECTION_BYTES
    target = _write(workspace.root, "big_notes.txt", padding + PRIVATE_KEY_BODY)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW


# ==========================================================================
# Tier 1: structural secret sources -- denied
# ==========================================================================


@pytest.mark.parametrize(
    "filename",
    [
        ".env",
        ".env.production",
        "production.env",
        ".envrc",
        "id_dsa",
        "id_ecdsa",
        "server.ppk",
        "bundle.p12",
        "bundle.pfx",
        "keystore.jks",
        "app.keystore",
        "vault.kdbx",
        "export.1pif",
        "legacy.agilekeychain",
        "http.keytab",
        ".netrc",
        "_netrc",
        ".authinfo",
        ".git-credentials",
        "service-account-prod.json",
        "application_default_credentials.json",
        ".htpasswd",
        ".vault-token",
    ],
)
def test_structural_patterns_are_denied(
    classifier: SecretClassifier, workspace: Workspace, filename: str
) -> None:
    """Every structural pattern denies, and reports a structural tier."""
    target = _write(workspace.root, filename, "content")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.DENY, f"{filename} should be denied"
    assert verdict.rule.startswith("secret."), f"{filename} matched a non-secret rule"
    assert verdict.tier == "structural"


@pytest.mark.parametrize(
    "filename",
    [
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secret.json",
        "secret.yaml",
        "app.secrets",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "db.password",
        "session.token",
        "kubeconfig",
        "kubeconfig.dev",
        "prod.kubeconfig",
        "k8s-secret-app.yaml",
    ],
)
def test_name_guess_patterns_are_approval_required(
    classifier: SecretClassifier, workspace: Workspace, filename: str
) -> None:
    """Every name-guess pattern gates, and never denies.

    This is the tier that keeps the deny list honest: a false positive here
    costs a prompt, not a blocked read.
    """
    target = _write(workspace.root, filename, "content")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.APPROVAL_REQUIRED, (
        f"{filename} should be approval_required, not denied"
    )
    assert verdict.rule.startswith("guess.")
    assert verdict.tier == "name_guess"


def test_structural_wins_over_name_guess(workspace: Workspace) -> None:
    """A structural match on the same path outranks a name guess."""
    target = _write(workspace.root, "id_rsa", "x")

    verdict = SecretClassifier(workspace).classify(target)

    assert verdict.classification is Classification.DENY
    assert verdict.tier == "structural"


# ==========================================================================
# Containment takes precedence
# ==========================================================================


def test_path_outside_workspace_is_denied_even_if_innocuous(
    classifier: SecretClassifier, tmp_path: Path
) -> None:
    outside = _write(tmp_path, "totally_normal.txt", "hello")

    verdict = classifier.classify(outside)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_lexical"
    assert verdict.containment is not None
    assert not verdict.containment.allowed


def test_path_outside_workspace_is_denied_not_approval_gated(
    classifier: SecretClassifier, tmp_path: Path
) -> None:
    """Containment failure denies, even for a name-guess filename.

    A path outside the workspace must never reach an approval prompt: the
    operator cannot meaningfully approve access to somewhere else entirely.
    """
    outside = _write(tmp_path, "secrets.json", '{"a":1}')

    verdict = classifier.classify(outside)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_lexical"


def test_symlink_escape_is_denied_even_if_innocuous(
    classifier: SecretClassifier, workspace: Workspace, tmp_path: Path
) -> None:
    outside = _write(tmp_path, "target.txt", "hello")
    link = workspace.root / "looks_local.txt"
    link.symlink_to(outside)

    verdict = classifier.classify(link)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "workspace.outside_symlink"


# ==========================================================================
# Content-key detection
# ==========================================================================


def test_credential_key_name_in_content_is_approval_required(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """Key names are weaker evidence than a key block: gate, do not deny."""
    target = _write(workspace.root, "settings.yaml", "api_key: placeholder\n")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "guess.content_key_name"
    assert verdict.tier == "name_guess"


def test_ordinary_text_content_is_allowed(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    target = _write(workspace.root, "main.py", "def handler():\n    return 1\n")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW


def test_binary_content_is_not_key_scanned(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """A compiled artefact contains the substring ``token`` by coincidence."""
    target = workspace.root / "app.bin"
    target.write_bytes(b"\x00\x01\x02token\x00\xff" * 100)

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW


def test_content_check_can_be_disabled(workspace: Workspace) -> None:
    """Path-only mode must not deny a key by its contents."""
    target = _write(workspace.root, "notes.txt", PRIVATE_KEY_BODY)

    classifier = SecretClassifier(workspace, inspect_content=False)
    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW
    assert verdict.content_checked is False


def test_credential_key_scan_can_be_disabled_independently(
    workspace: Workspace
) -> None:
    target = _write(workspace.root, "settings.yaml", "api_key: placeholder\n")

    classifier = SecretClassifier(workspace, inspect_content_keys=False)
    verdict = classifier.classify(target)

    assert verdict.classification is Classification.ALLOW


# ==========================================================================
# Fail-closed on unreadable content
# ==========================================================================


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root bypasses file permission checks",
)
def test_unreadable_file_fails_closed(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """A file Forge cannot read is a file Forge cannot clear -> DENY.

    The alternative, treating an unreadable file as clean, would mean any
    unreadable file is a secret-free file -- the wrong default for a security
    check.
    """
    target = _write(workspace.root, "locked.txt", "content")
    target.chmod(0o000)
    try:
        verdict = classifier.classify(target)
    finally:
        target.chmod(0o600)

    assert verdict.classification is Classification.DENY
    assert verdict.rule == "secret.content_unreadable"
    assert verdict.content_failure is not None
    assert verdict.content_checked is False


def test_non_existent_path_is_not_a_content_failure(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """Absent is not unreadable. An absent path cannot hold a private key."""
    verdict = classifier.classify(workspace.root / "not_created_yet.txt")

    assert verdict.classification is Classification.ALLOW
    assert verdict.content_failure is None
    assert verdict.content_checked is False


# ==========================================================================
# Result structure -- what the approval UX will consume
# ==========================================================================


def test_verdict_exposes_structured_fields(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """A verdict carries enough for a real prompt, not just a boolean."""
    target = _write(workspace.root, "secrets.json", "plain text")

    verdict = classifier.classify(target)

    assert verdict.classification is Classification.APPROVAL_REQUIRED
    assert verdict.rule == "guess.secrets_json"
    assert "secrets" in verdict.reason.lower()
    assert verdict.resolved == target
    assert verdict.exists is True
    assert verdict.matched_rules == ("guess.secrets_json",)
    assert verdict.containment is not None
    assert verdict.containment.allowed


def test_verdict_reports_all_matched_rules(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """``app.secrets`` matches one glob; ``kubeconfig.dev`` matches two patterns.

    The approval prompt may want to name every match, not only the deciding one.
    """
    single = _write(workspace.root, "app.secrets", "x")
    assert classifier.classify(single).matched_rules == ("guess.secrets_ext",)

    # `kubeconfig.*` matches; `kubeconfig` alone does not (fnmatch is
    # whole-string, not prefix).
    target = _write(workspace.root, "kubeconfig.dev", "x")
    verdict = classifier.classify(target)

    assert len(verdict.matched_rules) == 1
    assert verdict.matched_rules == ("guess.kubeconfig",)
    assert verdict.classification is Classification.APPROVAL_REQUIRED


def test_verdict_reports_multiple_matched_rules(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    """A name matching two globs reports both, not just the deciding one."""
    # `.env` matches both `.env*` and `*.env` (fnmatch's `*` matches empty).
    target = _write(workspace.root, ".env", "SECRET=1")

    verdict = classifier.classify(target)

    assert len(verdict.matched_rules) == 2
    assert set(verdict.matched_rules) == {"secret.env"}
    assert verdict.classification is Classification.DENY


def test_verdict_is_deterministic(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    target = _write(workspace.root, "id_ed25519", PRIVATE_KEY_BODY)

    first = classifier.classify(target)
    second = classifier.classify(target)

    assert first.rule == second.rule
    assert first.classification is second.classification


def test_verdict_string_is_operator_readable(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    target = _write(workspace.root, "id_rsa", PRIVATE_KEY_BODY)

    rendered = str(classifier.classify(target))

    assert rendered.startswith("deny: secret.ssh.id:")
    assert str(target) in rendered


def test_directory_is_allowed_when_itself_innocuous(
    classifier: SecretClassifier, workspace: Workspace
) -> None:
    (workspace.root / "src").mkdir()

    verdict = classifier.classify(workspace.root / "src")

    assert verdict.classification is Classification.ALLOW


def test_classification_enum_values_are_stable() -> None:
    """The approval and HTTP surfaces depend on these string values."""
    assert Classification.ALLOW.value == "allow"
    assert Classification.APPROVAL_REQUIRED.value == "approval_required"
    assert Classification.DENY.value == "deny"