# FORGE-22 — Hermes ACP engineering adapter (experimental, parked)

**Status:** experimental adapter + tests, parked by the Chief Architect (PORCH) on 2026-10-26. Not wired to any live execution path, and live execution is refused by construction.

**Files:**
- `forge/external/acp.py` — the adapter (toolset allowlist, sandbox/profile/credential verification, engineering gate, allow-once/deny approval bridge, session lifecycle).
- `tests/test_external_acp.py` — 20 tests, all passing; `forge/external/acp.py` imports nothing beyond the standard library at import time (the `acp` client is lazy).

## Decision (parked, do not drift)

1. Preserve the adapter and its 20 tests as experimental work.
2. Do not build a credential proxy or a new gateway.
3. Do not enable live ACP engineering execution.
4. Keep the working text-only Hermes integration (`forge/external/hermes.py`, `forge/textonly`) unchanged.
5. Byte remains Forge's engineering agent.
6. The credential-isolation blocker and revisit requirements are documented here.

## The credential-isolation blocker

Live engineering execution is refused because the inference credential is not provably inaccessible to the model's tools:

- The in-process ACP server runs Hermes's own `read_file` in the **same process** that holds the provider key.
- Hermes's `file` toolset does **not** confine reads to a workspace, so a model can `read_file` the credential — the `.env` in the session's home, or the process environment via `/proc/self/environ`.
- With `file` the only enabled toolset the exfiltration channel (network/shell) is closed, but the key is still *readable*, which fails the "inaccessible" bar this module demands.

OS isolation itself is fine and was proven (bwrap mount namespace hiding the operator home). The missing piece is the *credential* boundary, not the *filesystem* boundary.

## What is already enforced (no live execution needed)

- `ENGINEERING_TOOLSETS == ("file",)` — no terminal/code_execution/browser/web/delegation.
- `ALLOWED_PERMISSION_OPTIONS == ("allow_once", "deny")` — never `allow_always`/`allow_session`/auto-accept.
- `EngineeringGate` — disabled by default; sandbox, profile, and credential refusals all fail closed.
- `verify_profile` — refuses any effective tool inventory other than exactly the four file tools.
- session lifecycle, event streaming, cancellation, and session recovery, all against an injectable transport.

## Requirements to safely revisit (re-enable live execution)

1. **Credential boundary:** the inference key must be provably unreachable by `read_file`/`write_file`/`patch`/`search_files` and by `/proc` reads. Candidate designs: a gateway-held key with sandboxed tool execution, or a credential-injecting proxy that never materializes the key in the session's filesystem/environment.
2. **Re-verify the sandbox recipe** (borrowed-checkout data-root, epoll/SelectSelector under the user namespace, systemd-resolved DNS symlinks) against the Hermes version in use when revisited.
3. **Prove the boundary by an attempt, not a claim:** issue a read of the key and assert it is refused at the boundary.
4. **Re-run the full suite** and add a live end-to-end test behind `FORGE_RUN_E2E` (the same fail-closed gate the text-only adapter uses).
5. **Re-check the FORGE-17 decision gates** before treating engineering execution as more than an experiment.