# Forge Architecture

Status: initial draft. Describes boundaries only. Nothing here is implemented.

## Purpose

Forge is the orchestration layer of the Steel & Code platform. It sits between
external clients and models, decides which agent handles a request, gathers the
tools and context that agent needs, and returns a result.

Forge is not a model provider, not an automation scheduler, and not a website.

Forge is **terminal-first**. The primary human interface is the command line,
with a terminal UI as the richer form of it. Everything else Forge exposes is
secondary and exists to serve a machine, not a person.

## Interaction surfaces

Ordered by priority, highest first.

### 1. Terminal — primary human interface

The primary human interface for Forge is CLI and TUI. A human operating an
agent should be sitting at a terminal, either on their own machine or attached
to a host where Forge runs.

A terminal-first Forge implies:

- Every action available in a TUI is also reachable from a plain command line,
  and vice versa. No capability lives only in the rich interface.
- Output is designed to be read by a person first and piped second. Human
  output stays readable; machine-readable output goes behind an explicit flag.
- Interactive prompting works. Tools can ask the operator a question and wait.
- Sessions are resumable and state is inspectable from a shell.
- No browser, no daemon rendering HTML, no login wall is required for core use.

### 2. Host — VPS and SSH

Forge is expected to run on a VPS and be operated over SSH. A dedicated
server, reachable by SSH, is a normal deployment target rather than an
exception.

This means:

- Forge must behave under a plain shell session: no assumption that a display,
  browser, or desktop environment exists.
- Long-running work must survive a dropped SSH connection, or at minimum fail
  loudly and leave recoverable state rather than dying silently.
- Configuration and credentials come from the environment on that host.
- A local workstation is the same product in a different place, not a special
  mode.

### 3. HTTP / API — secondary integration surfaces

HTTP and API interfaces are **secondary**. They exist so that other software can
drive Forge, not so that a person can.

Known consumers:

| Consumer | Why it needs HTTP |
| --- | --- |
| n8n | Schedules and webhooks are HTTP by nature. |
| Steel & Code website | A website cannot hold an SSH session. |
| Future services | Anything not in this repo. |

Because these are secondary, they carry no feature the terminal lacks. If
something is possible over HTTP, it is possible in the terminal first. An HTTP
surface is a remote control, never the place a feature is born.

One consequence is specific to the secondary path. Approval of a sensitive
operation needs an interactive human, and an HTTP caller is not one. So
approval-required operations **fail closed** when driven over HTTP — not
auto-approved, not queued. Deferred approval with later resume is reserved for a
future version. See [byte-tools.md](byte-tools.md) for the full reasoning.

This is a deliberate limit on the secondary surface, not an oversight. An
unattended trigger can do the work that needs no approval and nothing else.

### 4. Provider-agnostic core

The core stays provider-agnostic. Forge does not embed vendor assumptions in
agent logic, workflows, or tools.

- Model access sits behind one interface in `forge/models/`. Swapping the
  gateway is a configuration change, not a rewrite.
- Agents declare what they need from a model, not which vendor supplies it.
- Tools do not call model providers directly.
- Workflows do not branch on a vendor name.
- No vendor SDK types appear in agent or workflow definitions.

This is what keeps a provider change cheap, and it is why the gateway is an
internal boundary rather than a set of scattered call sites.

## Layers

```
  PRIMARY                                        SECONDARY (machine-driven)
                                                
   human @ terminal / TUI                             n8n           website
          |                                             |              |
          | SSH                                         |  HTTP/JSON   |
          v                                             v              v
   +---------------------+                    +---------------------------+
   | Forge host          |  <---- triggers -- | secondary HTTP surfaces  |
   |  (VPS or workstation)|                    +-------------+-------------+
   |                     |                                  |
   |  workflows/         |                                  |
   |  agents/ -> tools/  |                                  |
   |     \-> memory/     |                                  |
   |     \-> models/  <---- LLM requests -------------------+
   +---------------------+
                                  |
                                  v
                         +---------------------+
                         | OpenRouter          |
                         | model gateway       |
                         +---------------------+
```

### Forge — orchestration layer

Owns:

- Request intake and validation.
- Agent selection and delegation.
- Tool assembly and permissioning per agent.
- Step sequencing for multi-step work.
- Returned results and errors to the caller.
- **Credential handling.** Forge loads credentials and injects an allowlisted
  environment into processes it starts or provider clients it builds. Agents may
  use a credential through Forge-managed execution but may never read one, and
  secret values must not reach tool output, prompts, logs, memory, or any HTTP
  response.

Does not own: model hosting, scheduling, or the customer-facing UI.

The terminal is the reference implementation. If an orchestration behaviour is
hard to express at a shell prompt, the design is wrong, not the terminal.

Code lives under `forge/`; `forge/workflows/` is the top-level entry point for
orchestration logic, `forge/agents/` for agent definitions. The interface layer
that a person drives has no home yet — no `forge/cli/` directory exists.

### OpenRouter — model gateway

Single point of access to language models. Forge talks to a model gateway and
to nothing else when it needs an LLM call.

Why a gateway: one API surface, one place to switch or fall back between models,
and centralized usage and cost tracking. It also gives Forge one place to
enforce provider-agnosticism — nothing above this boundary knows a vendor name.

OpenRouter is the current intended gateway. The abstraction is the point, not
the vendor: no OpenRouter client exists yet, and when one is added it goes
behind the interface in `forge/models/` so the rest of Forge never depends on
that SDK. A different provider should be reachable by changing configuration.

### n8n — automation and triggers

Owns *when* things happen. Scheduled jobs, inbound webhooks, third-party events.

n8n does not run agents and does not hold business logic. It decides that
something should be triggered and hands the payload to Forge. Forge does not
schedule its own work.

n8n reaches Forge over a secondary HTTP surface, because webhooks and schedules
are HTTP by nature. That surface carries no capability the terminal lacks.

Integration direction is inbound (n8n calls Forge). Outbound notification from
Forge back into n8n is possible later but undecided.

### Byte, Ember, Fin — agents

Agents are the workers Forge orchestrates. Each is a role, not a separate
service.

| Agent | Role | Surface |
| --- | --- | --- |
| Byte | Software engineering agent. Builds and maintains code. | Terminal, primary |
| Ember | TBD. | TBD |
| Fin | TBD. | TBD |

All three run on the same Forge runtime and differ by configuration: model,
tools, instructions, permissions, workspace, and memory scope. Byte is the only
one specified so far; Ember and Fin are planned names with no agreed scope.

The per-agent fields are defined in [agent-contract.md](agent-contract.md). No
agent logic, prompt, or executable definition exists.

### Steel & Code website — external client

The public-facing site. It consumes Forge; Forge does not own it and does not
depend on its internals.

The website is an external client of a secondary Forge interface. It cannot
hold an SSH session, so it arrives over HTTP — but it gets no capability that
the terminal does not also expose. The interface is not defined yet; defining
it is one of the next steps, because it fixes the contract everything else is
built behind.

## Data flow

Illustrative, for the intended shape. None of this exists yet.

Primary path, a person at a terminal:

1. The operator invokes an agent from the command line, or starts a TUI
   session, on a local machine or a Forge host over SSH.
2. Forge resolves the agent contract: model, tools, permissions, workspace,
   memory scope.
3. The agent requests a model call; Forge calls the model gateway.
4. The model returns; the agent may call a permitted tool or continue looping.
5. Memory and workflow state are updated; output is rendered for the operator.

Secondary path, software driving Forge over HTTP:

1. A trigger fires (n8n schedule, webhook, or website request).
2. Forge receives the request and picks an agent from its contract.
3. Steps 3 through 5 proceed as above, minus the human rendering step.
4. The result returns to the caller as structured data.

The two paths converge on the same orchestration core. Only entry and rendering
differ.

## Explicitly out of scope for this pass

- OpenRouter integration, or any model provider client.
- n8n wiring.
- Agent logic, prompts, or execution.
- CLI, TUI, or any interface implementation.
- Database or persistence layer.
- Public API and its schema.
- Python classes for the agent contract.
- Tool implementations.
- Dependency installation by agents. Forge v1 assumes the project environment
  is already provisioned.
- Asynchronous approval and resume.

## Consequences worth stating

These are consequences of terminal-first, not preferences.

- Testing favours plain functions over framework objects. The contract can be
  validated from a shell without standing up a server.
- Debugging favours readable logs and inspectable session state over dashboards.
- Deployment stays small. A VPS running a CLI is a smaller operational surface
  than a service with a web tier.
- Network access is optional, not assumed. A Forge host with no inbound HTTP
  reachable is still fully usable over SSH.
- Secrets live at the orchestration layer, not in the agent's reach. An agent
  gets capability without getting values, which is what makes it safe to run
  unattended.

## Design decisions recorded

Decisions taken during design that are not obvious from the surface list, and
would otherwise be re-litigated:

- Terminal-first, HTTP secondary (see Interaction surfaces).
- Model access behind one gateway interface; no vendor names above it.
- Agents are configuration, not classes or services.
- Agents may use credentials but never see them.
- Approval is per-operation; no blanket approval in v1.
- Approval-required operations fail closed when unattended.
- Memory is a Forge-wide architecture, not a per-agent choice.
- Memory is session-only in v1; durable project memory designed, disabled.
- The project environment is provisioned by a human, not by the agent.

## Open questions

- What does the terminal entry point look like: one command per agent, or a
  single command that takes an agent name?
- What is the session model: persistent resumable sessions, or one-shot runs?
- Does the website get a different interface from n8n, or one shared secondary
  surface with per-consumer credentials?
- Are Ember and Fin single agents or families of agents?
- Which model capability levels should the gateway expose as configuration, and
  which model does each agent default to?
- Does Forge ever call out to n8n, or only receive from it?
- Are the security classification lists in
  [byte-security-classification.md](byte-security-classification.md) accepted
  as written? All five classification decisions are settled: two-tier secret
  sources, wildcard deletes denied, inline code approval-gated without analysis,
  network commands approval-gated, Linux-only scope.

**Resolved:** memory scope is a single Forge-wide decision, not a per-agent one,
with Byte as the reference agent. Session memory in v1; durable project memory
designed and disabled; personal memory out of scope. Ember, Fin, and future
agents inherit the architecture unless a future contract explicitly changes it.
The full model is in [agent-contract.md](agent-contract.md).