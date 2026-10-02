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

See [docs/architecture.md](docs/architecture.md) for detail.

## Agent roles (working definitions)

- **Byte** — engineering worker. Builds and maintains code.
- **Ember** — TBD (documentation/pipeline role, to be defined).
- **Fin** — TBD (analysis/accounting role, to be defined).

## Status

Initial scaffold. Next steps, in order:

1. Decide the language/runtime dependency set and add a `pyproject.toml`.
2. Define the OpenRouter client contract in `forge/models/`.
3. Define the agent interface in `forge/agents/`.
4. Define the Forge public interface the website will call.
5. Wire n8n trigger handling.
6. Write tests.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Those commands will not work yet — `pyproject.toml` is not part of this
first pass. They document the intended shape.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.