# Forge Architecture

Status: initial draft. Describes boundaries only. Nothing here is implemented.

## Purpose

Forge is the orchestration layer of the Steel & Code platform. It sits between
external clients and models, decides which agent handles a request, gathers the
tools and context that agent needs, and returns a result.

Forge is not a model provider, not an automation scheduler, and not a website.

## Layers

```
        +---------------------------+
        | Steel & Code website      |   external client
        | (consumer of Forge)       |
        +-------------+-------------+
                      |  HTTP request / event
                      v
        +---------------------------+
        | Forge                     |   orchestration layer
        |  workflows/               |
        |  agents/  ->  tools/      |
        |      \->  memory/         |
        |      \->  models/         |
        +------+-------------+------+
               |             |
    triggers   |             |  LLM requests
               v             v
        +-------------+  +-------------+
        | n8n         |  | OpenRouter  |
        | automation  |  | model       |
        | & triggers  |  | gateway     |
        +-------------+  +-------------+
```

### Forge — orchestration layer

Owns:

- Request intake and validation.
- Agent selection and delegation.
- Tool assembly and permissioning per agent.
- Step sequencing for multi-step work.
- Returned results and errors to the caller.

Does not own: model hosting, scheduling, or the customer-facing UI.

Code lives under `forge/`; `forge/workflows/` is the top-level entry point for
orchestration logic, `forge/agents/` for agent definitions.

### OpenRouter — model gateway

Single point of access to language models. Forge talks to OpenRouter and to
nothing else when it needs an LLM call.

Why a gateway: one API surface, one place to switch or fall back between models,
and centralized usage and cost tracking.

No OpenRouter client exists yet. It will be introduced behind the interface in
`forge/models/` so that the rest of Forge never depends on a vendor SDK.

### n8n — automation and triggers

Owns *when* things happen. Scheduled jobs, inbound webhooks, third-party events.

n8n does not run agents and does not hold business logic. It decides that
something should be triggered and hands the payload to Forge. Forge does not
schedule its own work.

Integration direction is inbound (n8n calls Forge). Outbound notification from
Forge back into n8n is possible later but undecided.

### Byte, Ember, Fin — agents

Agents are the workers Forge orchestrates. Each is a role, not a separate
service.

| Agent | Role | Surface |
| --- | --- | --- |
| Byte | Engineering worker. Builds and maintains code. | Internal, Steel & Code |
| Ember | TBD. | TBD |
| Fin | TBD. | TBD |

All three run on the same Forge runtime and differ by configuration: model,
tools, instructions, and memory scope. Byte, Ember, and Fin are planned names
only; no agent logic, prompt, or definition is implemented.

### Steel & Code website — external client

The public-facing site. It consumes Forge; Forge does not own it and does not
depend on its internals.

The website is an external client of a public Forge interface. That interface is
not defined yet — defining it is one of the next steps, because it fixes the
contract everything else is built behind.

## Data flow

Illustrative, for the intended shape. None of this exists yet.

1. A trigger fires (n8n schedule, webhook, or website request).
2. Forge receives the request and picks an agent.
3. The agent requests a model call; Forge calls OpenRouter.
4. The model returns; Forge may let the agent call tools or continue looping.
5. Memory and workflow state are updated.
6. The result returns to the caller.

## Explicitly out of scope for this pass

- OpenRouter integration.
- n8n wiring.
- Agent logic, prompts, or execution.
- User interface.
- Database or persistence layer.
- Public API and its schema.

## Open questions

- What is the public Forge interface the website calls: REST, or something else?
- Are Ember and Fin single agents or families of agents?
- Does each agent get an isolated memory scope, or shared memory with
  permissions?
- Which models does each agent default to?
- Does Forge ever call out to n8n, or only receive from it?