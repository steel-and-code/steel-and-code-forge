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
  tools/      tools an agent is allowed to call
  memory/     agent memory and state
  workflows/  multi-step orchestration, trigger handling
tests/        pytest suite (not yet written)
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
containment, secret-source classification, and command classification. Next
steps, in order:

1. Draft Byte's system instructions (`docs/agent-contract.md`).
2. Settle Byte's memory configuration.
3. Define the terminal entry point and the approval prompt. This is the
   primary interface, and the classifiers are ready for it.
4. Define the model gateway contract in `forge/models/`.
5. Implement the agent contract in `forge/agents/`.
6. Define the secondary HTTP surface n8n and the website will call.
7. Wire n8n trigger handling.
8. Write the remaining tools.

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
interfaces. What exists is the security classification layer and its tests.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.