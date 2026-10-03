# Steel & Code Forge

Forge is the orchestration layer for Steel & Code's agent platform.

This repository is currently a **skeleton**: it defines the project structure
and documented boundaries only. No OpenRouter integration, no n8n wiring, no
agent logic, no UI, no database, and no API exist yet.

## What lives here

```
forge/
  agents/     agent definitions (Byte, Ember, Fin)
  models/     model client abstractions (OpenRouter gateway)
  cli/        terminal entry point (the `forge` command)
  tools/      tools an agent is allowed to call
  memory/     agent memory and state
  workflows/  multi-step orchestration, trigger handling
tests/        pytest suite (610 passing)
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

Initial scaffold. The enforcement pipeline is complete for v1: workspace
containment, secret-source classification, command classification, and the
terminal approval step that sits between classification and execution. A
terminal entry point exists and drives all of it. Byte's agent contract is
fully specified — system instructions and memory included.

The execution layer exists and runs approved operations; the CLI does not call
it yet, so `forge check` still executes nothing. There is **no `forge exec` in
v1** — that is a decision, not a gap; the reasoning is in
[docs/byte-tools.md](docs/byte-tools.md). Next steps, in order:

1. Implement the permission-model changes: an explicit ALLOW rule table,
   approval-required as the default for unrecognised commands, and gating for
   workspace-local executables and repository-code execution. The gap between
   the spec and the code is tabulated in
   [docs/byte-tools.md](docs/byte-tools.md).
2. Define the model gateway contract in `forge/models/`.
3. Implement the agent contract in `forge/agents/`, and wire the runtime to the
   existing executor through the classify → secret checks → approval → execute
   pipeline.
4. Implement session memory, the one memory scope v1 enables.
5. Record execution and approval events somewhere reviewable. Nothing is
   recorded durably yet, and this should exist before durable memory is enabled.
6. Define the secondary HTTP surface n8n and the website will call.
7. Wire n8n trigger handling.
8. Write the remaining tools.

## Permission model

One Forge-wide safety baseline, with Byte as its reference configuration. Every
agent runs under it; an agent may be configured narrower, and nothing in v1 can
widen it.

| | Examples |
| --- | --- |
| **Automatic** | Read-only inspection in the workspace (`ls`, `pwd`, `find` read forms, `grep`, `rg`, `cat`, `head`, `tail`, `wc`, `stat`, `file`, `diff`); `git status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`, and the listing forms of `git branch`, `tag`, `stash`; structured `write_file` / `patch_file` edits. |
| **Approval required** | Every git mutation including `add`, `commit`, `push`; general filesystem writes (`mkdir`, `touch`, `cp`, `mv`, `ln`, `chmod`, `rm`); executing repository code (`pytest`, `make`, `npm test`, `cargo`); any executable resolving inside the workspace (`./scripts/check.sh`); inline interpreter code; network egress; **any unrecognised command**. |
| **Denied** | `sudo` and system-level changes; anything outside the workspace; credential access and environment dumps (`env`, `printenv`); dependency installation; force push and destructive git history; wildcard destructive deletes. |

Automatic is an explicit rule, never a default, and the allowlist is
deliberately small: an entry is added only when there is confidence it belongs
there. An unrecognised command is approval-required, so a classifier miss
produces a prompt rather than silent execution.

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

The CLI classifies and asks. It never executes, and an approved operation says
so. It holds no policy: command and secret verdicts are ranked together by the
approval layer, so `forge check command -- cat secrets.json` reports
`APPROVAL REQUIRED` even though `cat` is ordinary. See
[docs/byte-tools.md](docs/byte-tools.md) for the full behaviour.

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

This works. There is no agent runtime yet, no model gateway, no HTTP surface, and
no memory layer. What exists is the security classification layer, the terminal
approval step that consumes it, the command execution layer, the `forge` CLI, and
their tests. `forge check` classifies and asks; it executes nothing.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.