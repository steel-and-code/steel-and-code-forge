# Steel & Code Forge

Forge is the orchestration layer for Steel & Code's agent platform.

This repository holds the **orchestration layer and its safety stack**, the
first agent runtime, and the local Workbench browser surface. There is no n8n
wiring, no database, and no public HTTP API yet.

## What lives here

```
forge/
  agents/     agent profiles and the runtime (Byte, Ember, Fin)
  models/     model client abstractions (OpenRouter gateway)
  cli/        terminal entry point (the `forge` command)
  tools/      tools an agent is allowed to call
  events/     the observational event contract (bounded payloads)
  ui/         Workbench: loopback server, SSE broker, approval bridge
  memory/     agent memory and state
  workflows/  multi-step orchestration, trigger handling
tests/        pytest suite (985 passing)
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

The Workbench browser surface is implemented: `forge workbench` serves one Byte
run on loopback, with live SSE tool activity, browser approvals over the same
policy layer, and no second path from the UI to a process.

Next steps, in order:

1. Define the secondary HTTP surface n8n and the website will call.
2. Wire n8n trigger handling. Approval-required operations fail closed over HTTP.
3. Record execution and approval events somewhere reviewable. Nothing is
   recorded durably yet, and this should exist before durable memory is enabled.
4. Implement durable project memory — designed, still disabled.
5. Decide whether a multi-run or chat-style session is worth building over the
   one-run-at-a-time Workbench.

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
forge workbench --workspace DIR [--model ID] [--port 8765]
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

## Workbench (local browser surface)

`forge workbench` serves a single-page view of one Byte run on loopback: the
task, the model's turns, live tool activity, and any approval the run needs. It
is the same runtime, the same safety stack, and the same approval decision as
`forge byte` — only the surface differs. An approval-required operation asks
the same question, through a browser instead of a terminal.

```bash
forge workbench --workspace .
forge workbench --workspace . --model anthropic/claude-sonnet-4
forge workbench --workspace . --port 9000
```

It needs `FORGE_OPENROUTER_API_KEY`, exactly as `forge byte` does, and its
optional dependencies:

```bash
pip install -e '.[workbench]'    # or '.[dev]', which includes them
```

Open the printed URL (`http://127.0.0.1:8765` by default). Nothing is launched
for you: starting a browser is a side effect nobody asked for, and you are
already at a terminal with the URL in front of you.

What it is, precisely:

- **Loopback only.** The bind address is fixed at `127.0.0.1` and there is no
  `--host` flag. Adding one would delete a named safety property rather than
  supply an argument, so it is absent rather than discouraged. `--port` is a
  port, not a host.
- **One run at a time.** A second task submitted while a run is in flight is a
  `409` conflict, not a queue. Queueing would imply the second task eventually
  runs.
- **Approvals are single-use capabilities.** Each prompt gets an unguessable
  id; the first valid answer claims it and any second answer is told it is
  stale. A refresh recovers the same pending approval, so the page keeps no
  approval state of its own.
- **Per-process token, exact Origin.** Every capability-bearing request must
  present the token minted by this process *and* match `Origin` exactly. The
  SSE stream uses a separate single-use ticket rather than the main token, and
  a refused request does not spend it.
- **No Stop button.** Nothing in the runtime or the executor supports
  cooperative cancellation, and a Stop button that cannot reliably stop is worse
  than no button. An unanswered prompt at shutdown is a rejection, not a hang.
- **Observations are bounded.** Events carry metadata and sizes, never file
  contents or process output. A slow client drops events and is told it dropped
  them, rather than being shown a stream with a silent hole in it.

The provider credential is read in the server process and is never threaded
into the session, the app, or a response body. A page that could read the
provider key would be a page that could spend your money.

The Workbench is a **local operator surface, not an API**. It is not the
integration surface n8n or the website will call, and it is not authenticated
for anything beyond the one loopback tab that minted its token. See
[docs/architecture.md](docs/architecture.md) for where it sits relative to the
surfaces still to come.

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

This works. There is no n8n wiring, no public HTTP API, no durable memory, and
no model gateway *client* beyond OpenRouter. What exists is the security
classification layer, the approval step that consumes it, the command execution
layer, the agent runtime, the model gateway, the `forge` CLI, the loopback
Workbench, and their tests.

The suite makes no network calls. Every test that drives a run uses the offline
`ScriptedGateway`, so `pytest` needs no credential and spends nothing.

## Configuration

Copy `.env.example` to `.env` and fill in real values locally. `.env` is
git-ignored and must never be committed.

## License

Proprietary — Steel & Code.