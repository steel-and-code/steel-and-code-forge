# Steel & Code Forge

Forge is the orchestration layer for Steel & Code's agent platform.

This repository holds the **orchestration layer and its safety stack**, plus the
first agent runtime. There is no n8n wiring, no UI, no database, and no HTTP API
yet.

## What lives here

```
forge/
  agents/     agent profiles and the runtime (Byte, Ember, Fin)
  models/     model client abstractions (OpenRouter gateway)
  cli/        terminal entry point (the `forge` command)
  tools/      tools an agent is allowed to call
  memory/     agent memory and state
  workflows/  multi-step orchestration, trigger handling
tests/        pytest suite (847 passing)
docs/         architecture and design notes
```

## Layer responsibilities

| Layer | Role |
| --- | --- |
| Steel & Code website | External client. Consumes Forge over a public interface. |
| Forge | Orchestration layer. Coordinates agents, tools, and workflows. |
| OpenRouter | Model gateway. Single provider entry point for LLM access. |
| n8n | Automation and triggers. Schedules and events enter Forge here. |
| Byte, Ember, Fin | Agents. Distinct roles running on Forge. |

See [docs/architecture.md](docs/architecture.md) for detail and
[docs/agent-contract.md](docs/agent-contract.md) for the per-agent fields.

Forge is **terminal-first**. The primary human interface is CLI/TUI; HTTP and
APIs are secondary integration surfaces for n8n, the Steel & Code website, and
future services. The core is provider-agnostic.

## Agent roles (working definitions)

- **Byte** — software engineering agent. Builds and maintains code.
- **Ember** — TBD. Role not yet decided.
- **Fin** — TBD. Role not yet decided.

Byte is the only agent specified so far. See
[docs/agent-contract.md](docs/agent-contract.md).

## Status

The agent runtime works. `forge byte` sends a task to a model, routes every tool
call through the Forge safety stack, and executes only what Forge decided was
permitted.

The enforcement pipeline is complete for v1: workspace containment, two-tier
secret-source classification, the explicit deny/approval/allow command tables
with approval-required as the default, the terminal approval step, and command
execution. The model gateway is provider-neutral, with an offline scripted
implementation used by every test and OpenRouter as the live provider.

Next steps, in order:

1. Define the secondary HTTP surface n8n and the website will call.
2. Wire n8n trigger handling. Approval-required operations fail closed over HTTP.
3. Record execution and approval events somewhere reviewable. Nothing is
   recorded durably yet, and this should exist before durable memory is enabled.
4. Implement durable project memory — designed, still disabled.
5. Decide whether an interactive chat session is worth building over the
   one-shot runtime.

## Permission model

One Forge-wide safety baseline, with Byte as its reference configuration. Every
agent runs under it; an agent may be configured narrower, and nothing in v1 can
widen it.

| | Examples |
| --- | --- |
| **Automatic** | Read-only inspection in the workspace (`ls`, `pwd`, `find` read forms, `grep`, `rg`, `cat`, `head`, `tail`, `wc`, `stat`, `file`, `diff`); `git status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`, and the listing forms of `git branch`, `tag`, `stash`; structured `write_file` / `patch_file` edits. |
| **Approval required** | Every git mutation including `add`, `commit`, `push`; general filesystem writes (`mkdir`, `touch`, `cp`, `mv`, `ln`, `chmod`, `rm`); executing repository code (`pytest`, `make`, `npm test`, `cargo`); any executable resolving inside the workspace (`./scripts/check.sh`); inline interpreter code; network egress; **any unrecognised command**. |
| **Denied** | `sudo` and system-level changes; anything outside the workspace; executables outside the workspace that are not in a trusted system root; credential access and environment dumps (`env`, `printenv`); dependency installation; force push and destructive git history; wildcard destructive deletes. |

Automatic is an explicit rule, never a default, and the allowlist is
deliberately small: an entry is added only when there is confidence it belongs
there. An unrecognised command is approval-required, so a classifier miss
produces a prompt rather than silent execution.

Executables are classified by where they *resolve*, not by the shape of the
token. Inside the workspace is repository code and is gated; in
`/usr/local/bin`, `/usr/bin`, or `/bin` — the directories Forge's executor
searches — it is classified normally; anywhere else it is denied.

## Terminal interface

```
forge                                          status and exit code
forge check command --workspace DIR [-- PROG ARG...]
forge check path    --workspace DIR PATH [PATH...]
```

Shared options go after the leaf subcommand: `--workspace`, `--agent`, `--json`,
`--classify-only`, `--unattended`. The `--` separator is required on
`check command` so a command's own flags are never read as Forge's.

Exit codes: 0 success, 1 refused, 2 malformed invocation, 3 approval would be
required, 4 internal failure.

```bash
forge check command --workspace . -- pytest tests/ -q
forge check command --workspace . -- rm -rf build      # prompts; runs nothing
forge check path --workspace . secrets.json
```

`forge check` classifies and asks. It never executes, and an approved operation
says so. It holds no policy: command and secret verdicts are ranked together by
the approval layer, so `forge check command -- cat secrets.json` reports
`APPROVAL REQUIRED` even though `cat` is ordinary. See
[docs/byte-tools.md](docs/byte-tools.md) for the full behaviour.

`forge byte` runs the agent and does execute — through the same pipeline, with no
second path to a process. It needs `FORGE_OPENROUTER_API_KEY` in the environment;
a missing key fails before any request is attempted.

```bash
forge byte --workspace . "fix the failing tests"
forge byte --workspace . --task-file task.md
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — layers, surfaces, data flow.
- [docs/agent-contract.md](docs/agent-contract.md) — the nine agent fields and
  Byte as the first concrete example.
- [docs/byte-tools.md](docs/byte-tools.md) — Byte v1 tool set, git action
  policy, and permission model.
- [docs/byte-security-classification.md](docs/byte-security-classification.md) —
  secret-source and known-dangerous command patterns, workspace boundary.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

This works. There is no HTTP surface, no n8n wiring, no durable memory, and no
model gateway *client* beyond OpenRouter. What exists is the security
classification layer, the approval step that consumes it, the command execution
layer, the agent runtime, the model gateway, the `forge` CLI, and their tests.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.