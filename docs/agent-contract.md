# Forge Agent Contract

Status: conceptual. No Python classes implement this. Nothing here runs.

## What this document is

A description of the shape every Forge agent takes. An agent in Forge is a
configuration, not a class and not a service. This document fixes the fields
that configuration must answer.

## What this document is not

- Not a class definition. There is no `Agent` base class, no dataclass, no
  schema. That comes when the first agent is built.
- Not an implementation. No code in this repository reads this document.
- Not the runtime. Nothing here describes how an agent executes, only what it
  is configured with.

## The contract

Every agent answers all eight fields. A field marked TBD is undecided, not
optional — an agent is not complete until every field has a real answer.

| Field | Answers |
| --- | --- |
| Name | What the agent is called, and how it is invoked. |
| Role | What the agent is for, in one line. |
| System instructions | What the agent is told it is, every run. |
| Model configuration | What model capability it needs. |
| Allowed tools | What it may do. |
| Permissions | What it may do without asking. |
| Workspace | Where it operates. |
| Memory configuration | What it remembers, and for how long. |
| Runtime status | Whether it can run at all yet. |

The last field is the honest one. It is what stops a planned agent from
looking like a working agent.

### Name

The agent's identifier, used at the command line and in configuration. Must be
unique across Forge. Chosen to be typeable — an agent you cannot name quickly
is an agent you will not use.

### Role

One sentence. What this agent is for, and by what means. Not a description of
personality or tone; that belongs in the instructions.

### System instructions

The standing prompt. What the agent knows it is, how it should work, what it
should refuse, and how it should report. Stable across runs and independent of
any single request.

Not implemented. Not drafted. No prompt text exists yet for any agent.

### Model configuration

What the agent needs from a model: capability level, context size, and
reasoning depth where it matters. Provider-agnostic by design — this field
describes requirements, never a vendor or a specific gateway model name.

Each agent should be able to run on a different model without changing its
logic. See provider-agnosticism in [architecture.md](architecture.md).

### Allowed tools

The set of tools the agent may call. An empty set is a valid, useful answer: a
text-only agent that reasons and reports is still an agent.

Allowed is not the same as permitted. A tool can be allowed but still gated by
the permissions field.

Byte's v1 tool set is specified in [byte-tools.md](byte-tools.md).

### Permissions

What the agent may do without stopping to ask a human. The rest requires
approval. A third category is denied outright — not a prompt a human can
approve, which matters for unattended runs where nobody is present.

Three things are kept distinct: whether a tool exists in Forge at all
(availability), whether this agent may call it (permission), and whether this
particular call needs a human (approval). Approval narrows a permission, it
never widens one.

Two rules apply to every agent:

- **Credentials are usable but not visible.** An agent may invoke a Forge-managed
  process or provider client that presents a credential; it may never read one.
  Secret values must not reach tool output, prompts, logs, or memory.
- **Approval is per-operation.** One approval covers one call. No
  session-wide or persistent approval mode.

Byte's v1 permission model is specified in [byte-tools.md](byte-tools.md).

### Workspace

Where the agent operates: which directories it may read, which it may write,
and where it puts working files. Bounded by definition. An agent with an
unbounded workspace is an agent with no permission model.

### Memory configuration

What the agent remembers and for how long. Session-only, durable, or both. What
scope it can read — its own memory, or shared memory with other agents.

This is the same decision as the memory scope question in
[architecture.md](architecture.md): isolated per agent, or shared with
permissions. It is unresolved, and it affects Ember and Fin as much as Byte.

### Runtime status

Whether the agent can actually run.

| Status | Meaning |
| --- | --- |
| Specified | Fields are defined, nothing is implemented. |
| Partial | Some fields work, others block a run. |
| Runnable | Every field is satisfied and the agent can be invoked. |
| Deprecated | Still exists, should not be used. |

No agent is above Specified.

## Agent: Byte

The first concrete example. Byte is the reference shape — the other agents
will follow whatever pattern Byte settles.

### Name

Byte.

Invoked by name from the terminal, on a local machine or a Forge host over
SSH.

### Role

Software engineering agent. Builds and maintains code.

### System instructions

TBD. No prompt text drafted. What Byte is told about the Steel & Code way of
working — building carefully, testing work, documenting decisions — belongs
here once decided.

### Model configuration

Configurable. Byte has no fixed model.

Byte needs strong tool-use and code reasoning, and enough context to hold a
codebase. Those are requirements; which gateway model satisfies them is a
configuration decision made at runtime, not baked into Byte.

### Allowed tools

TBD as a final list. The v1 candidate set is specified in
[byte-tools.md](byte-tools.md): file read, file write, file patch, filename
search, content search, shell execution, test running, and read-only git
inspection.

This was the largest open item for Byte. The list is settled in the linked
document; whether all of it ships in v1 is not.

### Permissions

Configurable per the v1 permission model in [byte-tools.md](byte-tools.md): read,
edit, search, tests, and git inspection are automatic inside the workspace;
destructive filesystem operations and `git commit`/`git push` require approval;
system-level, `sudo`, out-of-workspace, and credential access are denied by
default; force push is denied outright.

Two further v1 constraints: commands are invoked as argument arrays rather than
shell strings wherever possible, and unclassifiable commands are treated as
ambiguous and approval-gated rather than assumed safe. Dependency installation
is excluded from v1 entirely.

No per-project overrides decided.

### Workspace

Steel & Code development projects. Read and write access bounded to those
project directories.

### Memory configuration

TBD. Session memory is the safe starting assumption; durable memory is a
separate decision.

Byte should probably read across projects, since Steel & Code work is
cross-project, but that is a scoping decision to make rather than assume.

### Runtime status

**Specified.** Name, role, model configuration, workspace, permissions, and a
candidate v1 tool set are all defined. System instructions and memory
configuration are TBD. Byte still cannot run.

## Agent: Ember

TBD. No role, no fields, no scope. Held open.

## Agent: Fin

TBD. No role, no fields, no scope. Held open.

## Open questions

Byte-specific open questions are in [byte-tools.md](byte-tools.md), including
the two pattern lists that need review before the enforcement pipeline can be
built. Those remaining here are contract-level:

- Does Byte get a human approval prompt, a config gate, or both? v1 assumes a
  prompt, with review-then-confirm presentation.
- Is an empty allowed-tools set a supported agent, or does every agent need at
  least one tool to be useful?
- Does memory scope differ per agent, or is it one Forge-wide decision?
- What does a run look like when the agent lacks something it needs: fail on
  startup, or fail when the gap is hit?
- Should Byte's permission model become the shared default for all agents, or
  stay a per-agent decision?
- Do the credential and per-operation-approval rules hold for every agent, or
  does an agent with a different trust profile need an exception?
- When does Ember and Fin stop being placeholders?