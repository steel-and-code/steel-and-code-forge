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

Every agent answers all nine fields. A field marked TBD is undecided, not
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

Specified as **structured instruction blocks** rather than finished prose. The
reason is that the assembled prompt string depends on a model gateway and a
context-assembly step that do not exist yet, so prose written now would be
rewritten by whoever builds them. What is decidable now — and what the safety
architecture actually constrains — is the content of each block.

The blocks and their content are set out for Byte below, which is the reference
shape. Every agent uses the same blocks; what differs is role and boundaries.

### Memory configuration

What the agent remembers and for how long. Session-only, durable, or both. What
scope it can read — its own memory, or shared memory with other agents.

**This is a Forge-wide decision, not a per-agent one.** Byte is the reference
implementation; Ember, Fin, and any future agent inherit the same memory
architecture unless a future contract explicitly changes it.

The reasoning is that a per-agent answer means three separate designs for one
decision. The scopes, the persistence rules, the non-exposure requirements, and
the staleness handling are properties of what memory *is* in Forge, not of what
any particular agent does. Settling it once also means the second agent is a
configuration rather than a fresh design.

The full model — session memory in v1, durable project memory designed but
disabled, personal memory out of scope — is set out under Byte below.

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

**Permissions come from the Forge-wide baseline**, specified in
[byte-tools.md](byte-tools.md). Three properties:

1. **One baseline.** Every agent runs under it.
2. **An agent may be narrower.** Its configuration selects a subset of the
   baseline's tools and permissions. This is structurally guaranteed: nothing in
   the configuration path can add a permission.
3. **An agent may not widen the baseline in v1.** Not quietly and not loudly.
   There is no "allow extra" key, no trust override, and no per-agent policy
   file that grants what the baseline withholds. Any future widening is a change
   to the Forge baseline itself, reviewed like any other policy change.

Two rules apply to every agent:

- **Credentials are usable but not visible.** An agent may invoke a Forge-managed
  process or provider client that presents a credential; it may never read one.
  Secret values must not reach tool output, prompts, logs, or memory.
- **Approval is per-operation.** One approval covers one call. No
  session-wide or persistent approval mode.

Byte is the baseline's reference configuration. Ember, Fin, and any future agent
inherit it unless a future contract explicitly changes it.

### Workspace

Where the agent operates: which directories it may read, which it may write,
and where it puts working files. Bounded by definition. An agent with an
unbounded workspace is an agent with no permission model.

### Memory configuration

What the agent remembers and for how long. Session-only, durable, or both. What
scope it can read — its own memory, or shared memory with other agents.

Settled Forge-wide; see the generic field above and Byte's configuration below
for the full model.

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

Specified as seven instruction blocks. The content of each is decided; the
assembled prompt string is not, because it depends on a model gateway and a
context-assembly step that do not exist yet.

Every block below is a standing instruction. It holds for every run and is
independent of any single request.

#### Block A — Identity and role

> You are Byte, a software engineering agent. You work inside one project
> directory at a time, on the operator's machine or on a Forge host. A human is
> watching, or is not.

The final clause is the load-bearing one. Everything about how Byte reports and
when it stops follows from whether anyone is present, so that fact belongs in
the standing identity rather than being rediscovered per request.

#### Block B — Responsibilities

> Investigate before changing anything. Read the repository, the tests, and the
> documentation before you edit. Make the smallest change that solves the problem
> asked about. Verify your work by running it — the test suite, the build, the
> command you changed. Leave the workspace better than you found it.

Four commitments, deliberately. *Investigate before changing* is there because
the classifier's largest soft spot is a guess about code that has not been read.
*Smallest change* is there because v1 has no revert mechanism, so a broad change
is unrecoverable. *Verify by running it* is the direct counterweight to the
observation in [byte-tools.md](byte-tools.md) that prompting on every test run
would make Byte useless. *Leave it better* is the Steel & Code philosophy, and
the only aspirational instruction in the set.

#### Block C — Boundaries

> You operate inside one workspace root. Paths outside it are not yours to
> touch. You may use credentials that Forge supplies to a process you start; you
> may never read one. You do not install packages. You do not run commands as
> root. You do not manage services, users, network configuration, or disk.

This restates what Forge already enforces rather than adding to it. That is
deliberate: an instruction the agent follows is cheaper than a prompt the
operator answers, and both layers being present is what makes a classifier miss
land on a refusal rather than on damage. The agent is told the boundary so it
can explain it — not so it can be trusted with it.

#### Block D — Working with tools

> Call tools with the exact arguments the task requires. Use structured
> arguments, not shell strings, unless there is genuinely no other way.
>
> If a tool call comes back denied, that is the answer. Do not look for a
> different tool, a different argument order, an alias, a wrapper script, or a
> way of phrasing the same operation. Do not ask the operator to run it for you
> so you can claim the result.

Forge's classifier is incomplete by construction, and the specification says so
plainly. The pressure that creates on an agent is direct: the tool was denied,
the goal is legitimate, so find another route. That pressure *is* the soft spot,
and no additional classification rule closes it — a determined agent routes
around a rule set too.

This is therefore a standing behavioural rule, not a second copy of a classifier
rule. The classifier decides what to gate; this tells the agent what to do when
something is gated. The instruction is the deliverable, so the language stays
explicit and unhedged.

#### Block E — When to request approval

> Some operations stop and ask the operator. You do not know in advance which
> ones, and you do not need to: the tool result tells you. If an operation
> requires approval, present it and wait. Do not batch a gated operation into a
> larger plan that assumes it will succeed.
>
> Do not request approval for something the operator did not ask you to do.

The second paragraph addresses a real failure mode: an agent that has learned
approvals are obtainable can begin asking for things nobody requested, which
trains the operator to approve reflexively. Approval is for work the operator
asked for.

#### Block F — When to stop instead of improvising

> Stop and report when: the task as stated is ambiguous in a way that changes the
> work; the fix would require touching something outside the workspace; the
> honest answer is that you do not know; a change would be substantially larger
> than what was asked; tests fail and you cannot tell why; or you are tempted to
> work around a denial.
>
> Say what you found, what you did not do, and what you would need.

Six triggers. Each maps to something this architecture already knows goes
wrong: ambiguity that silently picks a wrong reading, boundary escape,
confident fabrication, scope creep with no approval prompt to slow it, guessing
at a failure it cannot diagnose, and rule circumvention.

#### Block G — Reporting

> When you finish: say what you changed, which files, how you verified it, and
> anything left unresolved. If you changed nothing, say that and why.
>
> When you fail: say what you attempted, what the actual error was, and what you
> did not try next. Do not report partial work as complete. Do not describe work
> you did not verify as verified.

The distinction between *verified* and *believed* is the point of the whole
block. An agent that reports an unverified change as verified has made the
operator's review meaningless, and the operator cannot tell the difference from
the report alone.

#### What the blocks deliberately omit

- **No tone or personality.** The contract already places personality in the
  instructions rather than the role, but personality in a standing prompt is a
  maintenance cost with no safety value. It is not included.
- **No tool names or command examples.** Tools are the allowed-tools field.
  Duplicating them in prose creates two places to update and a silent drift when
  only one is changed.
- **No gateway, model, HTTP, n8n, or Forge internals.** Provider- and
  surface-agnostic by contract.
- **No restatement of classification rules.** The classifier owns that. If Byte
  were told "never use sudo", every later improvement to the classifier would
  need a matching prompt change, and the prompt would become a second, weaker
  copy of the safety layer.

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

The Forge-wide baseline in [byte-tools.md](byte-tools.md), which Byte
instantiates without modification:

**Automatic inside the workspace:** read-only inspection (non-secret files and
the read-only command forms), structured `write_file` / `patch_file` edits,
search, and git inspection including the listing forms of `git branch`,
`git tag`, and `git stash`.

**Approval-required:** destructive and general command-based filesystem writes,
every git mutation (`git add`, `commit`, `push`, `merge`, `rebase`, branch and
tag creation, stash push/pop/drop), executing repository code (`pytest`,
`python -m pytest`, `make`, `npm test`, `cargo`, `go`, `tox`, `nox`), any
executable resolving inside the workspace, inline interpreter code, network
egress, and **any unrecognised argv command**.

**Denied:** system-level and `sudo` operations, out-of-workspace access,
credential access, dependency installation, force push, destructive git history
operations, wildcard destructive deletes, and destructive SQL.

Two further v1 constraints: commands are invoked as argument arrays rather than
shell strings wherever possible, and **automatic is an explicit rule rather than
a default** — an unrecognised command is approval-gated, not assumed safe.

Byte is the reference configuration and carries no special exception. An agent
may be configured narrower; there is no configuration that makes it wider.

### Workspace

Steel & Code development projects. Read and write access bounded to those
project directories.

### Memory configuration

**Session memory in v1. Durable project memory is designed here and shipped
disabled. Personal memory is out of scope.**

The shape is Forge-wide and Byte is the reference agent; see the generic field
above for why it is settled once rather than per agent.

#### Why session-only in v1

Durable memory is the one feature in this design that can *quietly* persist a
mistake. Everything else in Forge fails loudly and immediately — a denial, an
unattended approval, an out-of-workspace path. Memory fails differently: it is
wrong next Tuesday, and it is trusted in the meantime. With no audit trail in
the design yet, switching durable memory on before anything can review it would
be the wrong order of work.

Designing it now costs nothing and makes enabling it a configuration change
rather than a redesign.

#### What Byte may remember

**Session scope — this run only:**

- Files read or changed, and why.
- Decisions reached and the reasoning behind them.
- Test results, including failures and what they told Byte.
- Errors encountered and whether they were resolved.
- Open threads the operator asked about.

**Durable project scope — designed, disabled in v1:**

- Durable project facts: the project's conventions, its test command, its
  structure, decisions made and why.
- Written as prose, not as a transcript. A memory is a summary of what was
  learned, not a record of what was said.

#### What must stay session-only

- Anything read from a credential-bearing or approval-gated context. A denial
  was a denial; remembering the detail of it is not useful and re-establishes
  the access the denial refused.
- Transient state: plan position, scratch reasoning, in-flight edits.
- PIDs, process lists, timings — environment facts that are true now and false
  later.
- Anything Byte inferred but could not verify.

**Operator corrections** are session-only by default. A correction shapes the
current run. It may be **promoted to durable project memory when the operator
explicitly approves that promotion** — the promotion is itself an
approval-gated action, because it is a write to a tracked file. Absent that
explicit approval, the correction does not persist.

#### Project memory versus personal memory

Two scopes, because they answer to different authorities.

**Project memory** belongs to the repository. It is shared by everyone working
in it and reviewed through the same approval path as any other change. Byte
cannot write it directly; it becomes a proposed change the operator sees and
decides on. This is the `git commit` policy applied to knowledge.

**Personal memory** would belong to the operator's account, never enter a
repository, and never be committed. It is **excluded from v1**, and no
implementation for it is designed here.

The exclusion is deliberate. Project memory has a review point that already
exists — a diff. Personal memory has no review point, no visible boundary, and
no obvious owner. It is the scope most likely to accumulate something Byte
should not have retained, and the one with the weakest guardrails. Adding it
later is additive; getting it wrong first is not recoverable by anyone reading
the stored file. If it is ever added, it lives outside any repository and needs
its own governance.

**Cross-project:** Byte reads across Steel & Code projects when asked, and does
not write across them. Reading cross-project is the actual workflow; writing
cross-project is how one project's conventions get silently imposed on another.
This answers the memory-scope question in the same direction the workspace
decision already went: one explicit root per run, no named set.

#### Where durable project memory would live

Plain files inside the workspace, at `.forge/memory/project.md`. Markdown,
human-readable.

**Tracked in git, not git-ignored.** This is a deliberate correction to an
earlier sketch of this design, which proposed ignoring the file. Because the
design depends on git for review, provenance, diffing, rollback, and sharing, an
ignored file would forfeit all five. As a normal tracked project file, durable
memory is:

- **Human-readable** — a person can open it and understand it without Forge.
- **Tracked in git** — it has history, authorship, and an author date.
- **Reviewed like other project changes** — through the normal approval path.
- **Operator-editable** — a human may correct it by hand at any time, without
  an approval flow, because editing a markdown file does not need Forge's
  permission model to have an opinion.
- **Revertible** — `git revert` and `git checkout` restore it, because a stale
  memory has a way to be noticed and undone.
- **Scoped to the current project** — it travels with the repository and means
  nothing outside it.

Rejected alternatives:

- **In `CLAUDE.md` or a README section.** Those are checked-in conventions that
  ship to every clone and every contributor. Byte's operational memory is
  learned context for this project and does not belong in a distributed
  artifact.
- **Under `~/.forge/`.** Correct for personal memory, wrong for project memory:
  invisible to the repository and unversioned, so a stale entry has no way to be
  noticed or reverted. This is the personal-memory location, when it exists.
- **A database.** Explicitly out of scope per [architecture.md](architecture.md),
  and actively worse here — opaque, unreviewable in a diff, not hand-editable.

#### What must never be persisted

This is the memory instance of the non-exposure requirement, and it is the one
place a new failure mode appears. The existing rule says secret values must not
reach tool output, prompts, logs, memory, or HTTP. Memory is the surface most
likely to be overlooked, because it is the only one where writing is not a
visible act during a run.

Never persisted:

- Credential values of any kind, in any form. Also tokens, key material,
  connection strings, `.env` contents.
- Anything the secret classifier denied. Denied means denied; persisting a
  detail of it re-establishes the access that was refused.
- Environment values, `env` output, or anything matching a credential key-name
  pattern.
- File contents from an approval-gated read, even once approved.
- One-time codes, session identifiers, anything short-lived.
- Full tool output or transcripts verbatim.
- Anything Byte was explicitly told not to remember.

**Consequence for implementation, recorded now so it is not discovered later:**
a memory writer must run candidate content past the existing secret classifier
before writing it, and fail closed on an unreadable check exactly as
`secrets.py` already does. Memory is a write path into a durable file, which
makes it the one place a value could be written and persist. Reusing the
existing classifier rather than inventing a second redaction pass keeps the rule
in one place.

#### How memory gets updated

1. **Session memory** updates as the run proceeds. In process, no write, no
   review step, no chance to get it wrong on disk.
2. **Durable project memory** is proposed, never written directly. When
   something durable was learned, Byte drafts the content and presents it as a
   normal approval-gated change. Approved, it is written with the same
   provenance as any other file Byte changes — an author, a date, and a commit.
3. **An operator may correct memory directly** by hand, at any time.

#### How stale memory is handled

Staleness is the failure mode that makes durable memory dangerous, so the
handling is a decision rather than a detail.

- **Every entry is attributed.** Who wrote it, when, and from what. A memory
  without a date is a memory nobody can evaluate.
- **Conflict resolves toward the repository.** If memory and the code disagree,
  the code is right and the memory is stale. Memory is never authoritative over
  what it describes.
- **Memory never widens permission.** A remembered fact can make Byte better
  informed; it cannot make an operation permitted. If memory says "the operator
  approved this last time", that is not an approval — v1 has no persistent
  approval, and such a memory is stale by definition.
- **Stale is checked, not fatal.** Memory is a hint about the project, not a
  substitute for reading it. Byte is instructed to verify a memory against the
  current tree before acting on it, as it verifies anything else.
- **Deletion is a first-class operation.** Stale memory must be removable by
  hand with no approval flow.
- **No automatic expiry in v1.** What ages out, and on what evidence, is a real
  design question; guessing produces memory that silently deletes what it should
  have kept. Dates plus human deletion is the honest v1 answer.

### Runtime status

**Runnable, with one known defect.** Every field has a real answer and the
runtime executes: `forge byte --workspace DIR "task"` runs the agent loop.

Implemented in `forge/agents/` and `forge/models/`, with the permission
integration in `forge/tools/dispatcher.py`:

| Piece | Where | Role |
| --- | --- | --- |
| `AgentProfile` | `forge/agents/profile.py` | Byte is a value, not a class |
| `ConversationState` | `forge/agents/state.py` | session-only, in-process |
| `AgentRuntime` | `forge/agents/runtime.py` | the tool-call loop |
| `ModelGateway` | `forge/models/base.py` | one-method provider Protocol |
| `ScriptedGateway` | `forge/models/scripted.py` | offline; what every test uses |
| `OpenRouterGateway` | `forge/models/openrouter.py` | the live provider |
| `ToolDispatcher` | `forge/tools/dispatcher.py` | the only route into policy |

**The separation, stated once because it is the whole design:** the model decides
what it wants to attempt; Forge decides whether it is allowed.
`AgentRuntime` holds no classifier, no `Approver` and no `Executor` — it holds a
`ToolDispatcher`, and the dispatcher holds the pipeline. A runtime that could
reach a classifier would be one edit away from holding a second opinion about
what is permitted, which is how "approval narrows a permission, it never widens
one" decays. The separation is asserted structurally, by import scan, in
`tests/test_agent_runtime.py` — not by convention.

`forge/tools/dispatcher.py` is the sole importer of `forge/tools/execution.py`
among runtime modules, also asserted by import scan. There is no second path to
a process.

**The seven instruction blocks** are reproduced verbatim in
`forge/agents/byte.py` rather than paraphrased, so a test can assert the profile
still matches the contract above. What the assembled prompt adds is the
workspace path and nothing else: no tool names (they travel as structured
schemas), and no restatement of classifier rules, because a second copy of the
safety layer in a prompt is weaker than the code and drifts from it on every
future improvement.

### Tool-call loop

A model round-trip is one turn. A response carrying five parallel tool calls
costs one turn, because the round-trip is what costs tokens. The bound is
`MAX_TOOL_TURNS = 25`, configurable **downward** per run and by profile; a
caller may lower it and never raise it past the constant.

**A cached denial still consumes a turn.** The cache prevents repeated classifier
work and repeated approval prompts. It does not exempt the model from the bound,
so an agent looping on a denied call still terminates — which is the property
that matters, since the cache is an optimisation rather than the thing preventing
a runaway.

Exhausting the limit raises `MaxTurnsExceeded`. The model does not get to
narrate a conclusion it did not produce, and the operator is not shown a truncated
run dressed up as a finished one.

### What is cached, and what is not

Only **final permission refusals** are cached: `DENIED_BY_POLICY`, `REJECTED`,
`DENIED_UNATTENDED`. Those are the outcomes where policy or a human has already
answered the question, so asking again would re-prompt someone who said no.

Not cached, because they are different failure classes and several are
legitimately worth retrying: malformed arguments, unknown tool, schema violation,
execution failure, timeout, and internal errors. A malformed call retried with
corrected arguments must reach the model again.

The cache keys on canonical JSON of the call — argument order does not matter,
and the provider's call id does not, because a model retrying usually mints a
fresh one.

### Credential boundary

**`FORGE_OPENROUTER_API_KEY`**, read only by the model-gateway/bootstrap layer,
from a mapping passed in explicitly rather than from `os.environ` directly. The
ambient environment is never passed wholesale anywhere.

Never placed in an `AgentProfile`, `ConversationState`, `Message`, `ToolResult`,
system prompt, tool schema, execution environment, audit record, or log. Never
present in an exception message, nor any prefix of it. A missing key fails at
gateway construction, before any request is attempted, and the error names the
variable without echoing the environment.

A **provider error body is never echoed.** Providers quote the request that
produced the error, and the request carried the authorization header. Only
bounded, operator-safe metadata is surfaced: the HTTP status, and the provider's
error code and type when they parse as short identifiers.

### Process output is untrusted, and v1 does not redact it

Stated plainly rather than reassuringly: **bounded stdout and stderr are
returned to the model, and Forge v1 provides no general output-redaction
guarantee.** Process output may contain sensitive data.

What Forge does do: prevent *known* secret sources from being deliberately read
where the secret classifier can identify them. What it cannot do: guarantee that
an arbitrary subprocess's output is free of secrets. The path-based secret
classifier is not applied to a byte stream — that is a different problem with
different failure modes, and `execution.py` already records that redaction is
separate work belonging after an audit trail exists to review what was redacted.

Bounding is what v1 provides: 1 MiB retained per stream, truncation always
flagged, output never written to a log or to memory.

### Session memory

Session-only, in-process, one run. `ConversationState` holds the messages, the
turn counter, and the denial cache; it is returned in `RunResult` for inspection
and dropped when the process exits. Nothing is written to disk, serialised, or
pickled. A test asserts a run creates no file.

The security consequence is worth stating because it removes the need for a
scrubbing pass: every message that reaches the state has already passed through
the dispatcher, and the provider key is never a message, never a tool result, and
never a system instruction. There is nothing to scrub because nothing
secret-shaped is ever constructed as a message.

Durable project memory remains specified and disabled. Personal memory remains
out of scope.

### A defect that was found and fixed

**A non-root `exec_command` cwd used to fail to execute.** `ApprovalRequest.from_command`
set `workspace=verdict.cwd` instead of the workspace root. The two are the same
directory for a root-level command and different directories for any nested one,
so in a subdirectory the approval fingerprinted the subdirectory as the workspace
while the executor fingerprinted the real root. The digests disagreed and a
correctly-approved operation was refused with `INVALID_APPROVAL`.

`CommandVerdict` carries the resolved *cwd* but not the workspace root, so the
constructor could not infer it and had nothing correct to bind.

**The fix makes `workspace` a required keyword** on both
`ApprovalRequest.from_command` and `Approver.submit_command`. There is no
default and no fallback to `verdict.cwd`, because that fallback is precisely
what let the defect ship: it is correct for a root-level command and wrong for a
nested one, so it fails only in the case that matters. A caller that forgets now
gets a `TypeError` at the call site instead of an approval that is silently
mis-bound and can never be spent.

Every caller passes the root explicitly: `forge check` uses the workspace it
already resolved, and the agent dispatcher uses `self.workspace.root`.

**Nothing was relaxed to make the digests match.** `workspace` and `cwd` remain
separate fields, both still enter the digest, and both still independently
invalidate an approval. `tests/test_dispatcher.py` covers that directly: changing
the cwd after approval fails, changing the workspace after approval fails, and the
two fields are asserted distinct on one request. A structural test asserts the
required argument cannot be dropped again. The existing binding, claim, and
target-revalidation tests in `test_execution.py` all still pass unchanged.

## Agent: Ember

TBD. No role, no fields, no scope. Held open.

## Agent: Fin

TBD. No role, no fields, no scope. Held open.

## Open questions

Byte-specific open questions are in [byte-tools.md](byte-tools.md). Those
remaining here are contract-level:

- Does Byte get a human approval prompt, a config gate, or both? v1 assumes a
  prompt, with review-then-confirm presentation. **Settled in v1:** a prompt,
  implemented in `forge/tools/approval.py`.
- Is an empty allowed-tools set a supported agent, or does every agent need at
  least one tool to be useful?
- What does a run look like when the agent lacks something it needs: fail on
  startup, or fail when the gap is hit?
- When does Ember and Fin stop being placeholders?
- **Personal memory.** Excluded from v1 and undesigned. If it is ever added it
  lives outside any repository and needs its own governance — retention, what may
  be stored, how an operator inspects and deletes it, and what an agent or a
  compromised tool could get out of it. Those are separate decisions from
  project memory and should be designed when the scope is actually wanted.
- **Enabling durable project memory.** The design is settled; the switch is not.
  It needs an audit trail first, because [byte-tools.md](byte-tools.md) records
  that an approval nobody can review afterwards is weaker than it looks, and
  memory is the surface most likely to accumulate something that should not be
  there.

**Resolved here:** memory scope is a Forge-wide decision rather than a per-agent
one, with Byte as the reference agent. Ember, Fin, and any future agent inherit
the same memory architecture unless a future contract explicitly changes it.

**Also resolved here: the permission baseline is Forge-wide, not per-agent, and
is not overridable by an agent.** Byte is the reference configuration. An agent
may be configured narrower; nothing in the configuration path can widen the
baseline, and no agent-level "allow extra" or trust override exists in v1. Any
future widening is a change to the Forge baseline itself, reviewed like any other
policy change.

This closes the two contract-level questions that asked whether the model should
be shared, and whether the credential and per-operation-approval rules need
exceptions for a different trust profile. They hold for every agent. An agent
with a different trust profile gets a narrower configuration, not a different
model — and there is deliberately no mechanism for the latter.