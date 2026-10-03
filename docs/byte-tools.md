# Byte v1 — Tools and Permissions

Status: specification, implemented in `forge/tools/` and `forge/cli/`. Several
rules in this document were changed by the permission-model pass; each is marked
where it changed.

Scope: the Forge-wide safety baseline, with Byte as its reference configuration.
This is the pattern Ember and Fin inherit. The baseline is not Byte-specific and
does not become so.

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
one case that might warrant a prompt.

> **Ruled: no special dirty-file gate in v1.** The permission mechanism is
> deferred. `write_file` and `patch_file` remain automatic in the workspace,
> exactly as documented above.
>
> What is *not* deferred is the behavioural requirement. Byte's instructions
> should still require inspecting repository state before editing and avoiding
> silent overwrites of work it did not write — that is a prompt to good conduct
> in the system instructions, not a permission Forge enforces, and the two are
> kept distinct rather than conflated. A permission gate here would need to
> decide what "the operator cares about" means, which is a workflow judgement
> rather than a safety property; see the open questions.
>
> Deferred with the reasoning: in-place edits to dirty files.

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

`run_tests` runs an already-provisioned environment. It does not install
anything to get there.

> **Changed by the permission-model pass. `run_tests` is approval-required,
> not automatic.** The earlier draft made it automatic, reasoning that tests
> "may legitimately do things that look destructive" and that prompting on them
> would make Byte useless. That reasoning was sound about the *destructive* part
> and wrong about the *automatic* part, because running a test suite is running
> repository code: `pytest` executes `conftest.py`, loads installed plugins,
> imports fixtures, and runs arbitrary test code. The same pass ruled that
> `./scripts/check.sh` requires approval, and those two rulings cannot both be
> right — a shell script in the repository and a test runner in the repository
> are the same trust proposition. Gated.
>
> See [Repository code is not a trust
> boundary](byte-security-classification.md#repository-code-is-not-a-trust-boundary).
>
> Reading a test file is unaffected. `cat tests/test_x.py`, `grep`,
> `search_content`, and `read_file` on a test file stay automatic; the gate is
> on executing repository code, not on looking at it.

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

This list is about **tool availability** — whether Byte has a dedicated tool —
and is separate from the git action policy below. A dedicated `git_branch` tool
does not exist in v1; the underlying `git branch` invocation is still classified
by the classifier, and its listing form is automatic while its mutating forms are
approval-required or denied. The two layers are independent by design: a
permission ruling does not create a tool.

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
| `git branch` / `git tag` listing forms | Automatic |
| `git stash list` | Automatic |
| `git add` | Requires explicit human approval |
| `git commit` | Requires explicit human approval |
| `git push` | Requires explicit human approval |
| `git branch` create, `git switch`, `git tag` create | Requires explicit human approval |
| `git stash push` / `pop` / `drop` | Requires explicit human approval |
| force push (`--force`, `--force-with-lease`) | **Denied by default** |
| branch delete (`-d`, `-D`), tag delete (`-d`) | **Denied by default** |
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

### On branch, tag, and stash

These are three families with an automatic listing form and a mutating form, and
they are **classified per operation, not per program**. `git branch` is not one
permission level; neither is `git tag` or `git stash`.

| Command | Policy |
| --- | --- |
| `git branch`, `--list`, `-a -v` | Automatic |
| `git branch <name>`, `-c`, `git switch` | Approval-required |
| `git branch -d`, `-D` | **Denied** |
| `git tag`, `git tag --list` | Automatic |
| `git tag <name>`, `git tag -a` | Approval-required |
| `git tag -d` | **Denied** — same reasoning as branch deletion |
| `git stash list` | Automatic |
| `git stash push`, `pop`, `drop`, `clear` | Approval-required; `drop` and `clear` destroy state |

Tag deletion is denied because it is the same operation as branch deletion under a
different noun, and denying one while approving the other would be arbitrary.

### On `git add`

Approval-required. It is the one git write that does not reach the network, so
the argument for it is not publication — it is that staging is a state change to
the operator's index, made on the agent's judgement about what belongs in a
commit, and the operator reviewing that is the point of the gate.

**Approval is per invocation.** v1 does not introduce per-path approval or
batching semantics: one `git add` invocation is one approval decision, whatever
it stages. Batching would be a UX refinement of the same gate, not a safety
one, and it is not designed here.

## The Forge-wide baseline

**Decision: the permission model below is Forge's, not Byte's. Byte is its
reference configuration.**

This is a naming and ownership correction as much as a policy one. The
classifier, the workspace boundary, the secret tiers, the approval layer, and the
executor are all agent-agnostic already — `commands.py` cites this
specification, not Byte — so describing the model as Byte's would invite reading
a Forge-wide rule as a per-agent special case.

Three properties, all settled:

1. **One baseline.** Every agent runs under it.
2. **An agent may be narrower.** An agent's configuration selects a *subset* of
   the tools and permissions. This is structurally guaranteed: nothing in the
   configuration path can add a permission.
3. **An agent may not widen the baseline in v1.** Not quietly and not loudly.
   There is no "allow extra", no trust override, and no per-agent policy file
   that grants anything the baseline withholds.

Property 3 is deliberately stronger than "may not *silently* widen". "Silently"
would imply a loud widening exists. It does not. **Any future widening is a
change to the Forge baseline itself**, which goes through code review like any
other policy change and is visible in the diff. That is the only route.

The agent contract reached the same conclusion independently for memory scope,
and the two should read the same way.

## Permission model

### Automatic inside the workspace

No prompt, no delay.

- **Read** any file in the workspace that is not a secret source.
- **Edit** files in the workspace through Forge's structured tools:
  `write_file` and `patch_file`.
- **Search** filenames and contents in the workspace.
- **Read-only inspection commands**: `ls`, `pwd`, `find` (read forms), `grep`,
  `rg`, `cat`, `head`, `tail`, `wc`, `stat`, `file`, `diff`, and version probes.
- **Inspect** git status, diff, log, and the listing forms of `git branch`,
  `git tag`, and `git stash`.

> **The allowlist is deliberately small.** The governing principle:
>
>     if we are not confident it belongs in automatic,
>     leave it at APPROVAL_REQUIRED
>
> It is not broadened to reduce prompt count. A missing entry costs one prompt;
> a wrongly-added one costs a silent execution, and that asymmetry sets the size
> of the table. Coverage grows one reviewed row at a time.

> **Changed by the permission-model pass.** The earlier draft also listed "normal
> development command execution" and "run tests" as automatic. Both are now
> approval-required:
>
> - **"Normal development command execution" was not a rule.** It was defined by
>   intent rather than by a list of programs, on the reasoning that an allowlist
>   expressing `python -m pytest` "cannot express the project it is standing in".
>   With an explicit allowlist this objection dissolves: the allowlist is data,
>   so `python -m <module>` is one row, and a repository that needs a command
>   covered gets a v2 declaration rather than a standing exemption.
> - **"Run tests" executes repository code** and is gated with everything else
>   that does. See the `run_tests` note above.

Automatic remains an **explicit rule**, not a fall-through. A program not on
List 3 is approval-required.

### Requires human approval

- **Destructive filesystem operations**: deleting or overwriting outside the
  workspace, recursive deletes, bulk rewrites, `rm -rf` on any explicit path.
- **General command-based filesystem writes**: `mkdir`, `touch`, `cp`, `mv`,
  `ln`, `chmod`, `chown`, `sed -i`, and any redirection Forge does not perform
  itself.
- **`git add`**, **`git commit`**, **`git push`**, and every other git mutation
  not denied above.
- **Executing repository code**: `pytest`, `python -m pytest`, `make`, `npm
  test`, `npm run`, `cargo test`/`build`/`run`, `go test`/`build`/`run`, `tox`,
  `nox`, and `run_tests`.
- **Any executable that resolves inside the workspace**: `./configure`,
  `./scripts/check.sh`, `./tool`, `/workspace/bin/lint`.
- **Any unrecognised argv invocation.** Not automatic by default — see
  [Unknown commands](byte-security-classification.md#unknown-commands).
- **Inline code in any interpreter**: `python -c`, `bash -c`, `node -e`.
- Operations that leave the machine or cost real money: contacting paid APIs,
  sending messages, publishing, opening pull requests, deploying.
- Anything the operator's project marks as protected.

### Denied by default

- **System-level and `sudo` operations**: installing system packages, editing
  files outside the workspace root, changing system services, touching `/etc`,
  managing users, changing firewall or network config.
- **Access outside the assigned workspace**: any path that resolves outside via
  `..` or symlink, including a workspace-local executable that resolves out.
- **Credential access**: reading `.env`, SSH keys, tokens, or any secret file,
  even inside the workspace. See [Credential handling](#credential-handling).
- **Dependency installation**. Excluded from v1 entirely, not gated. See
  [Dependency installation](#dependency-installation).
- **Force push**, and remote history destruction generally.
- **Branch and tag deletion**, `git reset --hard`, `git clean -f`,
  `git checkout -- .`, `git filter-branch`, `git filter-repo`,
  `git reflog expire`.
- **Wildcard destructive deletes**: any destructive operation whose targets
  cannot be enumerated before execution.
- **Destructive SQL** reaching a database client.

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
2. **Program-token resolution** — where does the executable come from? If it
   resolves inside the workspace, it is approval-required. This sits next to
   containment because it uses the same resolution and the same boundary.
3. **Secret-source check** — if the target is a credential source, deny. Not
   reached for operations that only *use* credentials through Forge.
4. **Tool availability** — does the tool exist?
5. **Tool permission** — is the agent allowed this tool? An agent may be
   narrower than the baseline; it may never be wider.
6. **Operation classification** — automatic, approval-gated, or denied, per
   [Command execution and classification](#command-execution-and-classification).
   **An unrecognised operation resolves to approval-gated.** Automatic is an
   explicit rule, never an absence.
7. **Prompt** — if approval-gated, present the operation for review and wait.
   No interactive channel means the operation fails closed.
8. **Execute** — only after the above.

Denied at any step stops there. Nothing below a denial runs.

Approvals apply to one operation. They do not persist to the next call, and
there is no session-wide or persistent allow mode in v1.

The secret-source check sits at step 3, before tool permission, because it does
not depend on what the agent is allowed to do — a secret is a secret regardless
of configuration.

### Verdicts are ranked together, and one approval covers the operation

Steps 2 and 5 both produce a classification for the same operation, and they do
not agree by default. An ordinary command reading a name-guess file is `ALLOW`
from the command rules and `APPROVAL_REQUIRED` from the secret rules. Neither
verdict is wrong; they describe different properties of one action.

**The rule: every verdict participating in an operation is ranked together and
the strictest wins.**

    DENY  >  APPROVAL_REQUIRED  >  ALLOW

Consequences, all of which are enforced in `Approver.submit_command` rather than
in any caller:

| Command | Secret(s) | Outcome |
| --- | --- | --- |
| allow | allow | Allowed. No prompt. |
| allow | approval_required | **Approval required.** The command is ordinary; the file is not. |
| allow | deny | Denied by policy. |
| approval_required | allow | Approval required, for the command's own reason. |
| approval_required | approval_required | Approval required. |
| approval_required | deny | Denied by policy. |
| any | any deny | Denied by policy, whatever the command said. |

Two properties follow from "together" and both matter:

**One approval decision per operation, not one per verdict.** A command reading
three gated files produces one prompt, not three. The operator is approving a
single command; a prompt per target trains them to answer without reading, which
defeats review-then-confirm.

**A secret denial outranks a command allow.** `cat .env` is not a dangerous
command. It is a read of a structurally secret file, which no approval prompt
can make safe.

The rule lives in the approval layer so that every future caller inherits it. A
caller that ranked verdicts itself would eventually rank them differently, which
is precisely how this was wrong before: `submit_command` consulted only secret
*denials*, so a name-guess read through `exec_command` was approved without
asking while the same file through `read_file` prompted.

When a secret verdict is what made an otherwise-automatic operation gated, the
prompt names the escalating secret rule rather than the command rule. An
operator shown `APPROVAL REQUIRED` for `cat secrets.json` with no stated reason
would be reading a prompt without information.

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

> **Accepted: an unattended run that reaches an unknown or unapproved command
> fails closed.** With approval-required as the default for unrecognised
> invocations, this now applies to ordinary-looking commands on a toolchain List
> 3 does not cover — not only to commands that were always gated.
>
> That cost is intentional. It is the direct consequence of ruling that a
> classifier miss must produce a prompt, applied to a run where there is nobody
> to answer one. The alternative — unattended auto-approval or persistent
> command trust — is rejected outright, and the same reasoning that forbids a
> persistent allowlist for an operator forbids it more strongly for a run with
> no operator at all.
>
> The operational implication is a precondition rather than a surprise: an
> unattended run is viable only for a project whose toolchain is fully covered
> by List 3.

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
- **Read-only subpaths — DEFERRED, no mechanism in v1.** Should there be paths
  inside the workspace Byte can never write, even though it may read them?
  `LICENSE`, or generated output? Ruled out for v1; see [Closed by the
  permission-model pass](#closed-by-the-permission-model-pass).
- The credential-key check cannot read the candidate → **DENY**,
  `rule="secret.content_unreadable"`. Settled and implemented. A file Forge
  cannot read is a file Forge cannot clear; treating an unreadable file as
  clean would mean any unreadable file is secret-free, which is the wrong
  default for a security check.
- **Read-only subpaths.** Should there be paths inside the workspace Byte can
  never write, even though it may read them? `LICENSE`, or generated output?
- **Database reachability.** The classifier has destructive-database patterns, but
  whether Byte can reach a database in v1 is still undecided.

### Execution path, and why there is no `forge exec`

**Decision: `forge exec` does not exist in v1.** No human-facing execute
subcommand, and no bypass surface of any kind.

Two things get conflated here, so they are separated:

**A human running a command.** The operator has a shell. They can already type
`rm -rf build`, `git push --force`, `sudo make install`, or `curl` an internal
address. A Forge command surface would add no capability they lack and would
remove one they want — their own shell, with their own judgement and their own
undo history. **The v1 answer for human execution is the human's own shell,
outside Forge.** There is nothing to decide.

**An agent requesting execution.** That is `exec_command` inside the agent
runtime, and its shape is already fixed by what exists:

```
    classify CommandClassifier
      -> secret checks
      -> approval Approver
      -> Executor.execute
```

`forge check` already performs the first three and stops. Wiring it to the
executor belongs to the agent runtime, not to a new CLI verb.

**Why this is not a missing feature but a closed question.** A human-facing
`forge exec` becomes a bypass the moment it does any of five things:

| Bypass route | Why it is fatal |
| --- | --- |
| Classifies with a laxer policy | A second policy system, and the two drift |
| Skips the approval step | The gate is the product |
| Accepts shell strings | Unparseable, and already approval-gated for that reason |
| Inherits the parent environment | Hands every executed command every secret the session held |
| Offers `--yes` / `--force` | Inverts approval into a rubber stamp |

The `--yes` route is the tempting one, and it is exactly the "approve
everything" mode `byte-tools.md` already forbids — arriving through a flag
instead of a documented policy. If a human wants no prompt, the answer is to
type the command themselves.

There is also a structural argument: two entry points to execution means two
policies eventually, because the second accumulates the exceptions the first is
too slow to take. That is precisely the "second independent policy system" the
design forbids, and `forge exec` is how it would get built.

**If one is added later**, the only acceptable shape is a front end over the
*identical* pipeline: classify, prompt, and execute through the same `Approver`
and `Executor`, with no flag that skips a stage and no separate policy table.
Then it is not a bypass, merely a different entry point. That is a v2 question
and belongs to whoever has the actual need for it.

## Implementation status

### What the permission-model pass changed

The policy above is now the specification of record. The classifier in
`forge/tools/commands.py` still implements the **previous** default — an
unrecognised argv invocation classifies ALLOW — and has no ALLOW rule table yet.
That gap is intentional and is the next code change, not a documentation defect.
Concretely, the following documented rules are **specified but not yet
implemented**:

| Rule | Specified | Shipped |
| --- | --- | --- |
| Unrecognised argv → approval-required | Yes | **No** — still ALLOW |
| Explicit ALLOW rule table (List 3) | Yes | **No** — table does not exist |
| Workspace-local executable → approval-required | Yes | **No** |
| `pytest` / `make` / `npm` / `cargo` → approval-required | Yes | **No** — falls to the old default |
| `git branch`/`tag`/`stash` per-operation | Yes | **No** — no ALLOW rules, so all fall through |
| `git add` → approval-required | Yes | **No** |
| `git tag -d` → denied | Yes | **No** |

Everything else in this document is implemented and tested. The implementation
work is one focused change to `commands.py`: add `ALLOWED_PROGRAMS`, invert the
fall-through, add the program-token resolution check, and add the missing git and
runner rules. No new module and no second policy table.

### Existing implementation

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
`tests/test_commands.py` (261), `tests/test_approval.py` (101),
`tests/test_cli.py` (80), and `tests/test_execution.py` (86). 610 passing.

Implemented: workspace containment, secret classification, command
classification, terminal approval, the terminal interface, and command execution.

Not implemented, by decision: `shell=True`, credential injection, agent
runtime, CLI/TUI, HTTP surface, memory.

**CLI status.** A terminal entry point now exists in `forge/cli/`:
`main.py` owns argv, wiring, and exit codes; `render.py` is pure presentation.
It classifies, it asks, and it executes nothing.

    forge                                          status, exit 0
    forge check command --workspace DIR [-- PROG ARG...]
    forge check path    --workspace DIR PATH [PATH...]

Shared options (`--workspace`, `--agent`, `--json`, `--classify-only`,
`--unattended`) are attached to the two leaf parsers through a shared argparse
parent, so the supported placement is the only placement. The `--` separator is
required on `check command`: Forge's options go before it, the command's own
arguments after it.

The requester identity defaults to `forge-cli`, not `byte`. There is no Byte
runtime yet, so claiming Byte requested an operation would be a fabrication an
operator could act on. `forge byte` will pass `byte` explicitly when that
runtime exists.

Six states are distinguished: `ALLOW`, `APPROVAL REQUIRED`, `APPROVED`,
`REJECTED`, `DENIED BY POLICY`, `DENIED UNATTENDED`. The last three are all
refusals and share exit code 1, but they are different events and are labelled
and reported distinctly. Exit codes: 0 success, 1 refused, 2 malformed
invocation, 3 approval would be required in `--classify-only` mode, 4 internal
failure.

Two behaviours worth recording. An approved operation prints "execution is not
implemented yet, so nothing ran" — approving something that then silently did
nothing would be worse than no approval at all. And a relative path given to
`check path` resolves against the named workspace rather than the shell's
directory, because over SSH the shell directory is often unrelated and
classifying a different file than the operator named is a silent wrong answer.

The CLI holds no policy. It builds a `Workspace`, calls the classifiers, hands
verdicts to `Approver`, and renders what comes back. It never constructs an
`ApprovalRequest`, so a denial cannot become a prompt by any route, and
`tests/test_cli.py` asserts each of these properties rather than trusting them.

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

### Three policy rulings the classifier could not infer

Closing the gaps between the shipped rule tables and
[byte-security-classification.md](byte-security-classification.md) required
three decisions the specification did not settle on its own. They are recorded
here rather than left implicit in the rule tables.

#### `cp`, `mv`, and `ln` are approval-required inside the workspace

The permission model above says reads, edits, searches, tests, and git
inspection are automatic inside the workspace, and separately that `write_file`
and `patch_file` are automatic. That language covers the *structured tools*.

It does not cover an arbitrary `cp`/`mv`/`ln` invocation, which is a command
carrying whatever arguments the caller chose. v1 treats the three as
approval-required without distinguishing a safe `cp` from an unsafe one.

Containment is unaffected: any target resolving outside the workspace is still
denied outright, and that denial is the stronger of the two.

#### `git rebase` is approval-required, not denied

**In v1, `git rebase` is APPROVAL_REQUIRED.** It is not denied.

The classification spec denies `git rebase` conditionally — "rewriting published
history" — while listing rebase under approval as "everyday development". The
condition is about whether the history being rewritten has already been shared,
and **a static classifier cannot determine whether a ref is published or
shared**: that depends on the remote's state, which Forge does not query and
v1 has no mechanism to query. So the denial condition cannot be evaluated, and
an unevaluable condition must not be treated as satisfied.

Approval is the conservative reading: it does not pretend to know, and it leaves
the judgement with the operator, who can see the ref in the prompt. Denying all
rebases would be stricter than the spec requires and would block ordinary work.

#### `git checkout -- .` is denied by argument shape, not by rule

The spec names this exact form. A rule-table entry cannot express it, because a
rule matching "any `git checkout`" would deny everyday branch switching and a
rule matching "any `--`" would match unrelated flags.

It is therefore matched on shape in `_is_checkout_discard`: a `checkout`
subcommand, a `--` separator, and only tree-wide paths (`.` or `./`) after it.
`git checkout main`, `git checkout -b feature`, and `git checkout -- file.txt`
all stay allowed.

### Rules added to match the spec

The following operations appear in the classification spec but had no entry in
the shipped tables, so they fell through to the convenience case and classified
ALLOW. Each is now covered:

| Rule | Treatment | Form |
| --- | --- | --- |
| `cmd.mkfs` (`mkfs.<fstype>`) | denied | any `mkfs.` dispatcher, dot-prefixed so `mkfsext4` is unaffected |
| `cmd.git_reset_hard` | denied | `git reset --hard` |
| `cmd.git_clean` | denied | `git clean -f`/`--force`, with or without `-x`/`-d` |
| `cmd.git_checkout_discard` | denied | `git checkout -- .` and `-- ./` only |
| `cmd.git_branch_delete` | denied | `git branch -d`/`-D` |
| `cmd.git_update_ref_delete` | denied | `git update-ref -d`/`--delete` |
| `cmd.git_push_delete` | denied | `git push --delete` |
| `cmd.git_push_refspec_delete` | denied | `git push origin :branch` |
| `cmd.sysctl_write` | denied | `sysctl -w` and `sysctl -p`; reads stay allowed |
| `cmd.service_sc` | denied | `sc` |
| `cmd.fs_repair` | denied | extended to `setfattr` |
| `cmd.credential_inspect` | denied | extended to `docker login` |
| `cmd.git_rebase` | approval | `git rebase` |
| `cmd.file_move` | approval | `cp`, `mv`, `ln` |
| `cmd.shred` | approval | `shred` of a file; device targets stay denied |
| `cmd.dd_file_write` | approval | `dd of=<file>`; device outputs stay denied |
| `cmd.chmod` | approval | extended to `umask` |

#### `shred` and `dd` are split by target, not by program

The classification spec lists `shred` and `dd` in both its denied table and its
approval-required table without stating how the two coexist. v1 resolves it by
**where the bytes land**:

| Form | Classification | Reasoning |
| --- | --- | --- |
| `shred /dev/sda`, `dd of=/dev/sda` | **DENY** | Raw device erasure. Irrecoverable, and no approval makes it safe. |
| `shred notes.txt`, `dd if=a of=b.bin` | **APPROVAL_REQUIRED** | Destructive but scoped and knowable. The operator sees the exact target. |

The device test runs first, so a device target is denied before the file-target
gate is reached. The distinction is made by the `of=` operand for `dd` and by
the target path for `shred`, not by the program name — the same program is
gated or denied depending on what it is aimed at.

Nothing was loosened. `docker system prune` remains denied even though the spec
lists it under approval-required: that over-blocking predates this pass and a
stricter existing denial is left alone rather than relaxed to match a table.

**Resolved by the permission-model pass.** Arbitrary repository executables
(`./configure`, `./scripts/check.sh`) previously classified ALLOW under the
documented convenience case. That was
[byte-security-classification.md](byte-security-classification.md)'s own item 9
— the largest soft spot in the design — and narrowing it was a policy change
rather than a defect fix.

They are now **approval-required**, and so is any executable resolving inside the
workspace. The test is where the *resolved* path lands, not whether the token
contains a `/`: `/usr/bin/git` is an ordinary system program classified by its
normal rules, while `./tool` and `/workspace/bin/lint` are gated. The reasoning,
and the consistency argument with `cmd.inline_code`, are in [Workspace-local
executables](byte-security-classification.md#workspace-local-executables).

### Command execution

Status: **implemented** in `forge/tools/execution.py`. It is the only place Forge
launches a process.

The layer holds no policy. It does not classify, does not approve, and cannot
widen a permission. It runs an operation that was already approved, or returns a
result saying it did not run.

#### Approvals are single-spend capabilities

An approved outcome is a capability: `claim()` spends it, exactly once, for
exactly one operation. The state lives in a `ClaimToken` held inside the frozen
`ApprovalOutcome`, not in a field the caller threads forward.

That placement is the guarantee. Copying an outcome — by assignment,
`dataclasses.replace()`, `copy.copy()`, or `copy.deepcopy()` — copies a
*reference* to the same token, so every copy shares one claim and only one can
win. A per-executor "already spent" set would have let a second executor launch
the same approved command again; a plain dataclass flag would have been copied
rather than shared.

`ClaimToken.__deepcopy__` returns `self` for the same reason: the default path
would try to pickle the lock and raise, and an implementation that *cloned* the
token would mint a second spendable capability.

`consume()` remains as a compatibility alias and delegates to `claim()`, so the
two cannot disagree. The old `consumed` field is gone; `claimed` is computed
from the token and cannot go stale.

#### Approvals are structurally bound to the invocation

An approval for `rm -rf build` does not authorise `rm -rf src`. Forge recomputes
a digest from both the approved request and the requested execution, and refuses
unless they match. The caller is not trusted to assert the two correspond.

The digest covers:

| Field | Note |
| --- | --- |
| Program token | **Exact**, as supplied — not the classifier's normalised basename. |
| Argv | Exact tokens, boundaries preserved. |
| Workspace | Resolved. |
| Cwd | Resolved. |
| Invocation form | argv or shell. |

The program token is bound verbatim on purpose. The classifier lowercases a
basename to match rules against, and reusing that normalisation here would make
`/usr/bin/rm` and `rm` one approval — approving one would authorise whichever
file the other resolved to. Classification answers *which rules apply*;
execution binding answers *which operation runs*.

argv boundaries matter for the same reason: `["-rf", "a b"]` and `["-rf", "a",
"b"]` render identically in the human-readable detail string and are different
operations. Binding cannot be built from that string.

No environment and no value of any kind enters the digest, so it cannot serve as
an offline check against a guessable secret. That is also why a future
credential design needs opaque handles rather than raw values.

**A shell-string approval cannot be spent by an argv execution.** A shell string
is not an argv invocation, so there is nothing to bind; the executor refuses.
This is a structural reason `shell=True` is out of scope, not only a policy one.

#### Claim timing

    validation → atomic claim → launch

Validation failure does **not** spend the approval. A mismatched or malformed
request leaves the operator's yes intact for the operation they actually
approved; a caller bug must not become a second, silent refusal.

Once the claim succeeds the approval is spent, **even if the launch then fails**.
One approval authorises one launch attempt. A retry after the environment
changed must go back through approval, because the executor cannot know what
changed.

#### Execution states

| State | Meaning | `exit_code` |
| --- | --- | --- |
| `COMPLETED` | Ran, exit 0. | 0 |
| `FAILED` | Ran, non-zero. | the child's |
| `TIMED_OUT` | Forge signalled it. | `None` |
| `LAUNCH_FAILED` | Could not start. | `None` |
| `INVALID_APPROVAL` | Capability or binding problem. | `None` |
| `INVALID_REQUEST` | Malformed input: bad timeout, bad cwd, unsupported option. | `None` |

Forge state and child exit code are separate. A failing test suite is `FAILED`,
which is a normal result to report rather than an error.

Every refusal is a result, never an exception. A caller that forgot to handle
something gets something it cannot mistake for a completed run, not a traceback.

#### Environment

Forge-owned and fixed. **The parent environment is not inherited.** It holds
whatever the operator's shell had — tokens, SSH agents, database URLs — and
passing it through would hand every executed command every secret the session
held.

| Variable | Value |
| --- | --- |
| `PATH` | `/usr/local/bin:/usr/bin:/bin` |
| `HOME` | The workspace root |
| `LANG` / `LC_ALL` | `C.UTF-8` |
| `PWD` | The resolved cwd |

`HOME` points at the workspace so tools do not scatter state into the operator's
real home directory. A test runner writing `~/.cache` is normal, and it should
not reach outside.

There is **no caller-supplied environment** in v1 — not defaulted-to-empty,
absent. There is no argument through which `PYTHONPATH`, `LD_PRELOAD`, or a
credential could be passed. Binding environment *names* would not have been
sufficient anyway: `PYTHONPATH=/safe` and `PYTHONPATH=/attacker` have identical
key sets and radically different behaviour.

stdin is `DEVNULL`: the child sees EOF immediately rather than waiting for input.

#### Output is bounded while the child runs

One drain thread per stream, reading **to EOF continuously** and retaining only
the first N bytes.

Reading does not stop at the cap. A drainer that returned once it had its N
bytes would let the child fill the pipe buffer and block forever — turning a
truncation into a hang, which is worse than the unbounded output it was meant to
prevent. Bytes beyond the cap are counted and discarded, never stored.

| Limit | Value |
| --- | --- |
| Retained per stream | 1 MiB default, 16 MiB hard maximum |
| Byte accounting | Total **seen** reported separately from **retained** |

The retained head is kept, because a traceback's useful part is at the top and
finding the tail would require holding everything. Truncation is always flagged:
a truncated result that did not say so would be silently wrong.

stdout and stderr are separate, capped independently, and decoded once with
`errors="replace"` so malformed bytes cannot raise.

#### Secret-bearing output

**Process output is untrusted and may contain sensitive data.** It is bounded,
not redacted, and no redaction guarantee is made.

The secret classifier classifies paths and reads candidate files for PEM markers
and key names. Applying it to a stream of unbounded process output would be a
different problem with different failure modes, and claiming otherwise would be
the false assurance the rest of this document avoids.

So v1's posture is: bounded capture, byte counts recorded rather than content,
output never written to a log or memory, and the policy stated once rather than
tracked in a field that is always true. A redaction design is real work and
belongs to its own pass, after an audit trail exists to review what was redacted.

#### Timeout and the process tree

| Setting | Value |
| --- | --- |
| Default | 120s |
| Hard maximum | 900s |
| SIGTERM grace | 5s, then SIGKILL |

A request timeout may only **lower** the executor's configured default. A timeout
above it is `INVALID_REQUEST` rather than silently clamped — clamping would run
the command for less time than asked while reporting success, which is a
bounded-but-wrong answer. Trusted configuration may set the default anywhere up
to the hard ceiling.

Every child is started with `start_new_session=True`, making it a process-group
leader. On timeout Forge sends SIGTERM to the **group**, waits 5 seconds, then
SIGKILLs the group and reaps.

Signalling only the direct child would leave its descendants running — a command
that backgrounds work would keep holding the workspace and a process slot long
after the timeout. SIGKILL is sent unconditionally after the grace window rather
than polling, because a process that traps SIGTERM would otherwise survive.

Linux-first, like the rest of the security layer. Windows job objects are out of
scope.

#### Workspace revalidation — and its limit

Immediately before launch, Forge re-proves the workspace root's identity, cwd
containment, and containment of every approved target, re-resolving symlinks.
Detected drift fails closed.

> **This is not a sandbox and does not eliminate TOCTOU.** A race necessarily
> remains between the final validation and whatever an arbitrary child opens
> afterwards. Targets that were never enumerable — a path named inside inline
> source, for instance — cannot be re-proved at all, because nothing about them
> was ever known. True isolation would require an OS-level primitive —
> namespaces, containerisation, or capability restriction — which is a separate
> design and not part of this layer.

Nor is the resolved executable path bound: it is resolved at launch, and a file
can be replaced between resolution and `execve`. The digest proves the
*invocation* matches the approval; the resolved path recorded in metadata is
evidence of what ran, not a guarantee about it.

#### Audit metadata

`ExecutionResult.event()` produces an `ExecutionEvent` carrying agent, program,
arg **count**, workspace, cwd, fingerprint, approval rule, timings, child exit
code, state, byte counts, and truncation flags.

No stdout, no stderr, **no argv**, no environment. Arguments carry secrets as
readily as files do — `curl -H "Authorization: Bearer ..."` — so raw argv appears
in neither the result nor the event. Byte *counts* rather than content, so a
reviewer can tell a quiet run from a noisy one without the record holding what
was said.

The fingerprint is the audit join key: it links an execution to the exact
approval that authorised it without recording either.

Nothing durable is recorded yet. Writing these events somewhere reviewable is the
audit pass, and it should exist before durable memory is switched on.

Not implemented by decision: `shell=True`, credentials or environment injection,
stdin, and any second entry point to execution.

### Needed before Byte is usable

- **Implement the permission-model changes.** The gap table above is the
  work: `ALLOWED_PROGRAMS`, the inverted default, program-token resolution, and
  the missing git and runner rules. Until this lands, the code and this document
  disagree about unknown commands.
- **In-place edits to dirty files — DEFERRED, no gate in v1.** `write_file` and
  `patch_file` stay automatic. The behavioural requirement (inspect state,
  avoid silent overwrites) belongs in Byte's system instructions rather than in
  the permission model. Deferred because deciding what "the operator cares
  about" is a workflow judgement, not a safety property.
- **Read-only subpaths — DEFERRED, no mechanism in v1.** No protection for
  `LICENSE` or generated directories. A new mechanism rather than a permission
  question, and v1 does not build one.
- **Test detection — DEFERRED to v2**, along with project-declared workflow
  commands. The declared route is the only sane one for picking a runner, and
  that mechanism is not in v1.
- **Command timeout.** Settled in `execution.py` at 120s default, 900s ceiling.
  Whether a long-running process belongs in v1 at all is still open.
- **Provisioning story.** v1 assumes the environment is already provisioned.
  What happens when it is not — clear failure telling the operator to provision
  it, or something more helpful? A stub environment is the one remaining way
  Byte can be useless through no fault of the permission model.

### Closed by the permission-model pass

Recorded so they are not reopened as though they were still undecided:

- **Automatic allowlist scope — ruled: deliberately small.** The principle is
  "if we are not confident it belongs in automatic, leave it at
  APPROVAL_REQUIRED", and the list is not broadened to reduce prompt count.
- **`git add` batching — ruled: per invocation.** No per-path approval or
  batching semantics in v1.
- **Unattended cost — accepted.** An unattended run reaching an unknown command
  fails closed. No unattended auto-approval, no persistent command trust.
- **Test runner detection and project declarations — deferred to v2.** No
  project-declared widening mechanism in v1.
- **`env` / `printenv` / `set` — ruled: stay denied.** No name-only environment
  reads until the classifier can structurally distinguish a safe form. No new
  matcher capability in the current implementation pass.

### Project-declared workflow commands — v2

A repository will need to declare its normal commands (`make test`, `npm test`,
`cargo test`) so an agent is not prompted for a project's whole test cycle.

**Deferred to v2. No configuration format, no config file, and no code in v1.**

The constraints are recorded so it is not designed afresh later, and they are
deliberately restrictive because this would be the first mechanism able to move
an operation from approval-required to automatic without a human present:

1. May narrow approval-required → automatic only. Never affects a denial.
2. Never applies to a workspace-local executable, inline code, shell strings,
   or network egress.
3. Static data in the repository, reviewed through the normal code-review path.
4. Program plus exact argv prefix, matched with the same structured matcher.
   No globs, no shell.
5. `./scripts/check.sh` is not eligible.
6. Availability is a Forge-level operator setting, not a per-repository switch,
   so a repository cannot opt itself in.

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