# Byte v1 — Tools and Permissions

Status: specification. No tools implemented. No code exists for any of this.

Scope: Byte only, version 1. This is the first agent to get a concrete tool
list, and the pattern here is intended to become the reference for Ember and
Fin.

## Purpose

Define what Byte can do in v1, what Byte can do without asking, and what Byte
must never do. Also define the three separate ideas that are easy to conflate:
a tool existing, Byte being allowed to call it, and an operation being serious
enough to require a human.

## The three layers

These are distinct and are checked in order. A tool can be available and still
denied. An operation can be permitted and still require approval.

### 1. Tool availability

Does the tool exist in Forge at all? Availability is a property of the
runtime, not of Byte.

A tool that does not exist cannot be called regardless of Byte's permissions.
Byte's config selects from the tools that exist; it cannot invent one.

Availability answers: "could this be called, if everything else allowed it?"

### 2. Permission to use the tool

Is Byte allowed to call this tool at all? This is the `allowed tools` and
`permissions` pair from the [agent contract](agent-contract.md).

An agent can be denied a tool it could otherwise use — Byte might be
configured without shell execution on a restricted workspace, for instance.
Permission is about Byte's configuration.

Permission answers: "is Byte cleared to call this, in this workspace?"

### 3. Operations requiring human approval

Within a permitted tool, is this *specific call* serious enough to stop and ask?

Approval is about the operation, not the tool. Byte running tests needs no
approval. Byte running a command containing `rm -rf` does, even though both go
through the same shell tool.

Approval answers: "may this particular call proceed right now, or must a human
confirm?"

The rule that ties it together: **approval narrows a permission, it never
widens one.** A denied tool stays denied no matter who asks. Approval only
resolves operations that were already permitted.

## Tool set

Eight tools in v1. Grouped by what they do.

### Files — read

**`read_file`** — read a file within the workspace, with line ranges and a
byte budget.

Within the workspace: automatic. No approval.

### Files — write

**`write_file`** — create a file or replace its contents entirely.

**`patch_file`** — targeted edits to an existing file, replacing an exact
region.

Within the workspace: automatic for both. No approval.

An existing file that the operator has open or uncommitted changes in is the
one case that might warrant a prompt. Not decided here — see open questions.

### Search

**`search_files`** — find files by name or glob within the workspace.

**`search_content`** — regex search across file contents within the workspace.

Both automatic. No approval. Read-only.

### Shell

**`exec_command`** — run a command with the workspace as the working
directory.

This is the tool that carries most of the risk, because "run a command" is
unbounded in what it can touch. Its permissioning is specified separately
below rather than summarized with the rest.

See [Command execution and classification](#command-execution-and-classification)
for how Byte invokes commands and how they are classified.

### Tests

**`run_tests`** — discover and run a project's test suite.

A deliberate convenience over raw `exec_command`. It knows how to find the test
runner in a given project, so Byte does not have to guess the command.

Automatic inside the workspace. No approval. Tests may legitimately do things
that look destructive — write to a temp dir, build, delete a cache — and
prompting on that would make Byte useless.

`run_tests` runs an already-provisioned environment. It does not install
anything to get there.

### Git — inspect

**`git_status`** — working tree state.

**`git_diff`** — unstaged and staged changes, optionally against a ref.

**`git_log`** — commit history.

All three automatic. No approval. Read-only, and they cannot alter the
repository.

### Deliberately not in v1

- Branch create, checkout, merge, rebase, stash.
- Remote operations of any kind (`fetch`, `pull`, `remote` management).
- Tagging and release.
- Submodules.
- Package installation. Excluded from v1, not gated — see
  [Dependency installation](#dependency-installation).
- Network fetch tools other than those tests and search perform themselves.
- GitHub or forge API operations.

Branch and remote operations are absent because each one has a plausible
accident and no v1 need. Adding them later is additive.

Commands are invoked as argument arrays rather than shell strings wherever
possible. See
[Command execution and classification](#command-execution-and-classification).

## Git action policy

Separate from tool availability, because the read tools are automatic and the
write actions are not.

| Action | v1 policy |
| --- | --- |
| `git status` | Automatic |
| `git diff` | Automatic |
| `git log` | Automatic |
| `git commit` | Requires explicit human approval |
| `git push` | Requires explicit human approval |
| force push (`--force`, `--force-with-lease`) | **Denied by default** |
| branch, merge, rebase, stash, fetch, pull, tag | Not implemented in v1 |

### On commit

Approval is required every time. The point of a human in the loop here is that
commit messages record intent, and intent is the operator's to state. Byte
drafts the message; the operator decides whether it is committed.

### On push

Approval is required, and the approval must show what is about to be pushed:
remote name, current branch, commits ahead. A push is not reversible by a
local operation.

### On force push

Denied by default rather than approval-gated. An approval-gated force push is
one misclick away from losing other people's commits, and the value of force
push in normal development is low.

`--force-with-lease` is safer but is still a force push and is covered by the
same denial until someone decides otherwise.

## Permission model

### Automatic inside the workspace

No prompt, no delay.

- **Read** any file in the workspace.
- **Edit** files in the workspace: create, write, patch.
- **Search** filenames and contents in the workspace.
- **Normal development command execution** inside the workspace.
- **Run tests** inside the workspace.
- **Inspect** git status, diff, and log.

"Normal development command execution" means the ordinary work of building
software: build, test, lint, format, typecheck, run, install into a project
virtualenv. It is defined by intent rather than by an allowlist of binaries,
because a command allowlist that can express `python -m pytest` cannot express
the project it is standing in.

### Requires human approval

- **Destructive filesystem operations**: deleting or overwriting outside the
  workspace, recursive deletes, bulk rewrites, `rm -rf` on any path, force
  removal of files with changes.
- **`git commit`** and **`git push`**, per the policy above.
- Operations that leave the machine or cost real money: contacting paid APIs,
  sending messages, publishing, opening pull requests, deploying.
- Anything the operator's project marks as protected.

### Denied by default

- **System-level and `sudo` operations**: installing system packages, editing
  files outside the workspace root, changing system services, touching `/etc`,
  managing users, changing firewall or network config.
- **Access outside the assigned workspace**: paths outside the Steel & Code
  project directories. Includes `~`, `/tmp` as a default, and any path that
  resolves outside via `..` or symlink.
- **Credential access**: reading `.env`, SSH keys, tokens, or any secret file,
  even inside the workspace. See [Credential handling](#credential-handling) —
  the rule is that Byte may use credentials it is given, never read one.
- **Dependency installation**. Excluded from v1 entirely, not gated. See
  [Dependency installation](#dependency-installation).
- **Force push.**
- **Destructive git history operations** on a shared remote.

Denied means the call does not execute and is reported as denied. It is not a
prompt that a human can approve.

### On approval-gated vs denied

The difference is intentional and matters.

An approval-gated operation assumes a human is there to say no. That holds when
someone is watching Byte at a terminal, and it fails when Byte runs unattended
from an n8n schedule with nobody present.

Denied operations hold in both cases. So the split is roughly:

- Safe to ask about, or unsafe to do without asking → approval-gated.
- Cannot be made safe by a human being careful → denied.

Credential reading is in the second group even though it is a single call,
because an approval prompt cannot distinguish "Byte needs the token for this
task" from "Byte is being talked into printing it".

## Enforcement points

Conceptually, a Byte operation passes through these checks in order. Order
matters: cheapest and most decisive first.

1. **Workspace containment** — resolve the real path, confirm it is inside the
   workspace. Symlinks resolved before the check, not after.
2. **Secret-source check** — if the target is a credential source, deny. Not
   reached for operations that only *use* credentials through Forge.
3. **Tool availability** — does the tool exist?
4. **Tool permission** — is Byte allowed this tool?
5. **Operation classification** — automatic, approval-gated, or denied, per
   [Command execution and classification](#command-execution-and-classification).
   Ambiguous resolves to approval-gated.
6. **Prompt** — if approval-gated, present the operation for review and wait.
   No interactive channel means the operation fails closed.
7. **Execute** — only after the above.

Denied at any step stops there. Nothing below a denial runs.

Approvals apply to one operation. They do not persist to the next call, and
there is no session-wide or persistent allow mode in v1.

The secret-source check sits at step 2, before tool permission, because it does
not depend on what Byte is allowed to do — a secret is a secret regardless of
configuration.

## Command execution and classification

### The problem

The permission model above is only as good as the ability to tell a safe command
from a destructive one. That ability is limited, and v1 is designed around that
limit rather than pretending it away.

### Decision: no complete shell parser

Forge does not attempt to build a complete shell parser in v1. Deciding whether
an arbitrary shell command is safe requires understanding control flow,
expansion, subshells, redirection, `eval`, and the aliasing and wrapper scripts
already on the host. Getting that wrong in the permissive direction is a
security failure, not a bug.

### Decision: prefer structured invocation

`exec_command` takes an argument array, not an arbitrary shell string:

```
exec_command(program="pytest", args=["tests/", "-q"], cwd=<workspace>)
```

where a single string may be passed only as an explicit escape hatch, and doing
so marks the operation ambiguous and therefore approval-gated.

Structured invocation is not a security control on its own — nothing stops Byte
passing `program="rm"` — but it removes the shell as a hiding place and makes
the operation visible in the approval prompt as a program and arguments rather
than a line of text to be read carefully.

### Decision: classification is conservative

Classification inputs, in order of reliability:

1. **Resolved workspace containment.** Real paths, symlinks resolved before
   the check. This is the one input Forge can rely on absolutely.
2. **Structured command information**, where the invocation form provides it.
   Program name and arguments are known before execution.
3. **A conservative set of known-dangerous operations and patterns.** A short
   list, biased toward over-approximating danger.
4. **Anything ambiguous defaults to approval-gated.**

When classification is uncertain, the answer is a prompt. Forge does not guess
"probably safe."

The concrete patterns for steps 4 and 5, and the full classifier including its
stated limits, are in
[byte-security-classification.md](byte-security-classification.md).

### What this means in practice

- Arbitrary shell strings cannot be perfectly classified, and v1 does not
  pretend otherwise. They are treated as ambiguous.
- A command that cannot be classified is not automatically dangerous; it is
  automatically *not automatic*.
- Byte is permitted to run development commands, and it may well need a shell
  string for some of them. The cost is a prompt, not a refusal.

This is a deliberate trade: prompts in exchange for not shipping a hand-rolled
shell parser that would eventually be wrong in the unsafe direction.

## Credential handling

### Rule

**Agents may USE approved credentials through Forge-managed execution, but may
not SEE credential values.**

Use without sight. An agent can invoke a provider client, a deploy step, or a
test suite that presents a token. It cannot read the token.

### Denied to agents, always

Agents may not read, and Forge must not expose through any agent-facing surface:

- `.env` files, or any file matching secret-file patterns.
- Credential and key files: SSH private keys, `.pem`, `.key`, `.p12`, `.pfx`,
  `.netrc`, cloud credential files, kubeconfig secrets.
- System keyrings, password stores, or secret managers.
- Environment dumps that would include credential variables.
- Any file whose declared purpose is holding a secret.

This is denied rather than approval-gated, for the reason already stated: an
approval prompt cannot distinguish a legitimate need from being talked into
printing a value.

### Permitted: Forge-managed injection

Forge itself may load credentials from the environment or a secret store and
inject an explicitly allowlisted environment into processes it starts, or into
provider clients it constructs. This is Forge acting as the operator's deputy,
not the agent gaining access.

The allowlist is explicit. The ambient environment is not passed through
wholesale.

### Non-exposure requirement

Credential values must not appear in:

- Tool output returned to the agent.
- Approval prompt content.
- Logs, at any level including debug.
- Memory — session or durable.
- Error messages and tracebacks.
- Any secondary HTTP surface response.

Where a command needs a secret and might echo it, Forge treats the output as
secret-bearing and does not return it verbatim.

**Memory is the surface most likely to be missed here.** Every other item above
is a surface something writes to deliberately, during a run, where the write is
visible. Memory is the one where writing is silent and the value persists. The
memory model in [agent-contract.md](agent-contract.md) therefore carries a
concrete implementation requirement: a memory writer must classify candidate
content with the existing secret classifier before writing it, and must fail
closed on an unreadable check exactly as `secrets.py` does. Reusing that
classifier rather than adding a second redaction pass is what keeps the rule in
one place.

### Consequence

Byte cannot read `.env` to learn what a project needs. It runs the project, and
Forge supplies the environment. A project that cannot be run without the agent
first discovering its variable names is a limitation of v1, accepted knowingly.

## Approval UX: inspect-then-confirm

### Decision

An approval request is not an inline yes/no. It is a **review-then-confirm**
step: the operator inspects the operation, then decides.

The rationale is that the operator is approving an action they may not have
requested. An inline `Allow? [y/N]` on a half-understood operation trains the
reader to type `y` without reading, which defeats the approval entirely.

### What the prompt displays

Every approval request shows:

| Field | Content |
| --- | --- |
| Requesting agent | Which agent wants this. |
| Operation | Which tool, and what it is trying to do. |
| Workspace | The resolved workspace the operation runs in. |
| Command or action | The exact program and arguments, or the action detail. |
| Affected targets | Paths, files, or refs affected — when knowable. |
| Reason | Why the agent wants to do this, in the agent's words. |
| Git context | For git operations: remote, current branch, commits ahead or behind. |

When affected targets are not knowable before execution, the prompt says so
rather than omitting the field. An operator seeing "targets: not determinable"
can make a different decision than one seeing nothing.

### Scope

Approval applies to **that operation only**. It does not extend to the next
similar operation, and it is not remembered after the call returns.

### No session-wide approve-all

There is no "approve everything" or "allow for this session" mode in v1. A
blanket approval voids the permission model, and the value of the control is
that it is consulted every time.

There is no persistent allowlist of "always allowed" commands in v1 either. An
operator who approves the same command repeatedly has a case for adding one;
adding it in advance removes the control for every operator who does not know
about it.

## Unattended and HTTP operation

Byte may be triggered by n8n, where no human is present.

### Decision: fail closed

Approval-required operations **fail closed** when no interactive human approval
channel is available. The operation does not execute, and the failure states
that approval was required and unavailable.

It is not auto-approved, not queued, and not silently skipped. A failure that
says "needed approval, nobody was there" is a reportable condition; a silent
skip is not.

This makes the approved-versus-denied distinction concrete. Approved means a
human could have said yes. Denied means no, not because a human declined, but
because the answer could not be anything but no.

### No asynchronous approval in v1

Deferred approval — request now, human approves later, run resumes — is **not**
implemented in v1. It is a legitimate future capability, and it is deliberately
not designed here.

It is not designed now because it changes the shape of a run: a suspended run
holds a workspace, credentials, and a process slot across an unbounded wait,
and it introduces a second interface for approval that does not exist. Deciding
that under time pressure, before anything runs, would be premature.

### Which operations are permitted unattended

v1 assumes **none**. Any Byte run that requires approval fails if triggered
unattended.

An explicit unattended permission policy is separate work and is not designed
here. When it is designed, it should be a per-project allowlist rather than a
loosening of the approval model.

## Dependency installation

**Excluded from v1.**

Forge v1 assumes the project's runtime environment has already been provisioned:
dependencies installed, virtualenv created, project tooling configured. Byte
runs within an environment someone set up, and does not create one.

### Rationale

Installing packages is a privileged operation with poor failure modes. It
reaches a package index, runs install-time code from a third party, and changes
the environment that every later command depends on. A partial or malicious
install corrupts the workspace in ways that are hard to detect and hard to
recover from.

### Reserved

Dependency installation is a **possible future approval-required capability**.
It is reserved, not rejected — approving an install with a review-then-confirm
prompt showing the packages and the index is a reasonable future control.

It is not in v1, and no part of v1 should be built in anticipation of it.

## Open questions

The four blocking questions are resolved: command classification, credential
handling, terminal approval UX, and unattended approval. Those decisions are
recorded above and are no longer open.

What remains is implementation-scoped. None of these block starting work, but
each needs an answer before the corresponding piece is finished.

### Needed before the enforcement pipeline

The two pattern lists are now drafted in
[byte-security-classification.md](byte-security-classification.md), with all
classification decisions settled. The decisions that shape the pipeline:

- **Secret sources are two-tier.** Structural or content-proven sensitivity is
  denied. A filename that merely suggests sensitivity is approval-required.
- **Destructive operations with unresolved wildcards are denied.** Byte resolves
  the target list and re-requests with explicit paths.
- **Inline-code execution is approval-required**, with no attempt to analyse
  whether the source is safe.
- **Network commands are approval-required**, and `curl` does not become Byte's
  web interface.
- **Platform scope is Linux.** Windows and PowerShell are outside v1, and the
  classifier will not protect a Windows host.

Still needed, none of which block starting implementation:

- **Workspace boundary precision.** Is it the project root, or a named set of
  project directories? A multi-project workspace changes what "outside" means
  and therefore what containment checks.
- **Redaction policy.** How are secret values detected in output for the
  non-exposure requirement? Exact-value matching against known secrets catches
  the known ones and nothing else. Deciding the approach is needed before the
  output path exists.
- **Read-only subpaths.** Should there be paths inside the workspace Byte can
  never write, even though it may read them? `LICENSE`, or generated output?
- The credential-key check cannot read the candidate → **DENY**,
  `rule="secret.content_unreadable"`. Settled and implemented. A file Forge
  cannot read is a file Forge cannot clear; treating an unreadable file as
  clean would mean any unreadable file is secret-free, which is the wrong
  default for a security check.
- **Read-only subpaths.** Should there be paths inside the workspace Byte can
  never write, even though it may read them? `LICENSE`, or generated output?
- **Database reachability.** The classifier has destructive-database patterns, but
  whether Byte can reach a database in v1 is still undecided.

## Implementation status

Four pieces of the pipeline are implemented, in `forge/tools/`:

- `workspace.py` — path resolution and containment. Fails closed.
- `secrets.py` — the two-tier secret-source classification.
- `commands.py` — command classification for structured invocations.
- `approval.py` — the terminal approval step. Consumes `CommandVerdict` and
  `SecretVerdict`; does not re-classify and does not execute.

The first three return a structured result rather than a boolean, carrying
`classification`, `rule`, `reason`, resolved paths, `tier`/`matched_rules`, and
where applicable the underlying containment result, so the approval UX can be
built on top without re-running any of these checks. `approval.py` builds on
exactly those fields and adds nothing of its own.

Tested by `tests/test_workspace.py` (18), `tests/test_secrets.py` (64),
`tests/test_commands.py` (174), and `tests/test_approval.py` (70). 326 passing.

Not implemented, by decision: command execution, agent runtime, CLI/TUI, HTTP
surface, memory.

Both remaining agent-contract decisions are now settled in
[agent-contract.md](agent-contract.md): Byte's system instructions are specified
as seven instruction blocks, and the memory model is session memory in v1 with
durable project memory designed and disabled. Byte's runtime status is
**Partial** — every field answered, nothing implemented.

### How the approval step behaves

`Approver` is the entry point. It is constructed with an `ApprovalChannel` and
holds no state between operations, so there is no session-wide allow and no
allowlist — v1 has neither, and a stateless class is what guarantees it.

Routing, in order:

1. A `deny` from the command verdict, or from any secret verdict supplied
   alongside it, produces `DENIED_BY_POLICY`. No prompt is rendered and the
   channel is never consulted. `ApprovalRequest.from_command` and
   `.from_secret` raise `DeniedByPolicy` on a denial rather than return
   something an operator could wave through, and `Approver.resolve` re-checks
   the classification so a hand-built request cannot turn a denial into a
   prompt. This is "approval narrows a permission, it never widens one" made
   structural rather than conventional.
2. An `allow` produces `APPROVED` without consulting the channel. A routine
   development command should not need a human to say yes.
3. Anything `approval_required` goes to the channel. No channel, or a channel
   that cannot reach anyone, produces `DENIED_UNATTENDED` — not auto-approved,
   not queued, not silently skipped. The detail says the operation was not run
   and not queued, so the unattended case is reportable.
4. Otherwise the prompt is rendered and the answer parsed.

`REJECTED` and `DENIED_UNATTENDED` both stop the operation, but only the first
means a person was there and said no. They stay distinct so a caller can tell
"the operator declined" from "there was no operator".

Response parsing is an exact affirmative: `y` or `yes`, case-insensitive, after
trimming. Everything else declines, including an empty line, EOF, and an
interrupt.

`ApprovalOutcome.consume()` claims an approved outcome for one execution and
raises on a second claim. Approval is per-operation; making that a flag rather
than a convention is what stops it quietly becoming per-session.

Two prompt behaviours worth recording because they are decisions rather than
formatting:

- **Absent fields are stated, not omitted.** No agent-supplied reason renders
  `Reason: not supplied`; no caller-supplied git context renders
  `Git context: not supplied`; an unresolvable target list renders
  `Affected targets: not determinable`. The spec's reasoning is that an
  operator seeing "not determinable" can decide differently from one seeing
  nothing, and a missing line is indistinguishable from an absent field.
- **A shell invocation is labelled.** The prompt says the invocation is a shell
  string and was not parsed, because `cmd.shell_string` is gated precisely
  because nothing inside it was checked. A prompt that omitted that would imply
  a level of analysis that does not exist.

The agent-supplied reason is optional at this layer by design. The approval
module should not invent a rationale it was not given; a future agent runtime
may require one before it constructs a request, and that is the runtime's
policy rather than this module's.

`TerminalApprovalChannel` is stdlib `print` and `input` and nothing else. It is
deliberately thin — no colours, no key handling, no TUI. All of that is
interface design belonging to the CLI layer, and none of it is needed to ask a
question and record the answer. A channel that has no TTY reports itself
unavailable and the operation fails closed. `ScriptedApprovalChannel` replays
answers for tests and non-interactive driving; it goes through the same
availability check and the same strict parse, so it is not a bypass.

Two things this step deliberately does not do. It performs no git lookup, so
git context is only what a caller supplies. And it executes nothing — an
`ApprovalOutcome` is a handoff to a future execution layer.

### Needed before Byte is usable

- **In-place edits to dirty files.** If Byte patches a file the operator has
  uncommitted changes in, does that need approval?
- **Test detection.** How does `run_tests` decide which runner a project uses?
  Declared in project config, or guessed?
- **Command timeout.** What is the ceiling for one command, and does a
  long-running process belong in v1 at all?
- **Provisioning story.** v1 assumes the environment is already provisioned.
  What happens when it is not — clear failure telling the operator to provision
  it, or something more helpful? A stub environment is the one remaining way
  Byte can be useless through no fault of the permission model.

### Deferred, not blocking

- **Async approval and resume.** Reserved for a future version. Needs its own
  design: what suspends, what expires, and how a second approval interface
  works.
- **Dependency installation.** Reserved as a possible future approval-required
  capability. Not designed.
- **Unattended permission policy.** v1 permits no approval-required operation
  unattended. When designed, it should be a per-project allowlist.
- **Persistent allowlist for repeatedly-approved operations.** Deliberately
  absent in v1.
- **Audit trail.** Nothing records approvals and denials yet. An approval nobody
  can review afterwards is weaker than it looks, and this gets harder to add
  once runs happen.