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
tests/        pytest suite (522 passing)
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
it yet, so `forge check` still executes nothing. Next steps, in order:

1. Wire a `forge exec` subcommand to the executor, and decide what Byte is
   allowed to run without asking.
2. Define the model gateway contract in `forge/models/`.
3. Implement the agent contract in `forge/agents/`.
4. Implement session memory, the one memory scope v1 enables.
5. Record execution and approval events somewhere reviewable. Nothing is
   recorded durably yet, and this should exist before durable memory is enabled.
6. Define the secondary HTTP surface n8n and the website will call.
7. Wire n8n trigger handling.
8. Write the remaining tools.

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

This works. There is no application code yet — no agent runtime, no CLI, no
interfaces, and no command execution. What exists is the security classification
layer, the terminal approval step that consumes it, and their tests.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.