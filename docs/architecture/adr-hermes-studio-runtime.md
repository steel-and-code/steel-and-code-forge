# ADR: Hermes-based Steel & Code Studio Workbench

- **Date:** 2026-10-24
- **Status:** Proposed for Chief Architect review; not an implementation authorization
- **Jira:** FORGE-17 (decision), FORGE-6 (Byte), FORGE-9 (Workbench), FORGE-14 (paused)
- **Decision authority:** Chief Architect

## Purpose

Steel & Code's product goal is a small human-led software and game studio supported by specialized AI teammates, not an independent agent-framework business. Apply KISS / the Porch Protocol: reuse reliable capabilities before building competing infrastructure.

## Proposed decision

1. **Hermes is the preferred runtime candidate** for agent conversations, delegated workers, memory, Kanban execution, and coordination, subject to security/isolation and integration tests.
2. **Forge becomes the Studio Workbench**: keyboard-first operator experience for projects, agent identities, Jira decisions, task visibility, review evidence, budgets, and approval requests.
3. **Jira remains the authority for human-approved scope and decisions.** A Backlog item is not permission to work. Only explicit Chief authorization (Ready for Forge) may initiate an engineering job. A Hermes task status is not a substitute for this permission.
4. **Preserve existing Forge implementation and tests.** Do not delete or replace Forge's classifier, secret detection, digest-bound approvals, JobStore, idempotency records, checkpoints, or gateway while the new architecture is evaluated.
5. **Do not maintain two competing dispatchers** without evidence that a specific missing safety or workflow guarantee requires a narrowly scoped bridge.

This ADR requests review of direction only. It does **not** authorize changing runtime code, replacing Byte, auto-merging a PR, issuing production credentials, or deploying.

## Evidence: Katana Hermes experiments, 2026-10-24

| Experiment | Observed | Not established |
|---|---|---|
| 001: Inspect Forge | Hermes read repository, reported model/provider and a passing local test run | Production workflow |
| 002: Parallel delegation | Two independent subagents completed analysis; one structured result was empty due to output schema | Robust structured handoff in all cases |
| 003A/B: Persistence | SQLite sessions, memory and marker persisted; explicit session resume recovered prior context | Continuing an interrupted tool operation |
| 003D: Kanban execution | Worker claimed a disposable task, heartbeated, completed, recorded history | Exactly-once side effects |
| 003D: Crash retry | Dispatcher recognized crash and retried; second attempt completed; START marker repeated | Safe retries for consequential operations and stale-worker fencing |
| 004A: Security inspection | Smart approvals, tool profiles, logs, and existing safety settings inspected | Equivalent Forge-level execution safeguards |
| 004B/C: Restricted profile | CLI and Kanban worker respected disabled terminal/code toolsets; file toolset permitted read **and** write | Read-only filesystem enforcement; container isolation |

Evidence is based on locally reported experiments. Reproduce critical observations in integration tests before implementation.

## Boundaries and safeguards

- **Human-only authority:** Chief approves scope, consequential changes, PR merge, and production release separately; workers may request but never self-grant these approvals.
- **Enforced isolation:** A model instruction such as 'do not modify files' is not containment. Hermes file tools bundle read/write and Kanban workers receive their assigned profile's available tools plus lifecycle tools. Run engineering workers in a restricted OS/container identity with explicit mounts, limited network/credentials and disposable workspaces; validate *all* execution paths, not merely terminal.
- **Secrets and cost:** Do not expose production credentials to coding workers. Define provider access and per-task budget limits before unattended work. Distinguish estimated from billed costs.
- **Side effects:** Crash retries may re-run work. A pending record or fencing token does not itself provide exactly-once external side effects; protected resources must validate authorization/fencing or support idempotency.
- **Observability:** Capture Jira key, worker/run ID, model, branch/commit, tests, approval actor, output, retry, cost and decisions. Avoid secrets in logs.
- **Release safety:** Feature branches and PRs before review; no automatic merge. Deployment requires its own approval, backups, health checks and rollback plan.
- **Runtime authority:** Decide which system owns actual worker dispatch. Jira is the human authorization record; reconcile state after outages. Treat Hermes Kanban as candidate operational dispatcher, not yet as trusted authorization source.

## What Forge retains

- Studio interface and operator experience on Katana / Outpost, potentially connecting to the always-on Forge host.
- Human/Jira authorization mapping, clarification and explicit decisions.
- The existing safety and durability code as validated components or fallback until their necessity is resolved.
- Integration glue only where justified by a measured gap.

## Decision gates before replacing runtime ownership

1. Validate Hermes license/upgrade story and stable CLI/API integration surface.
2. Demonstrate a disposable, scoped worker inside OS-level isolation; prove prohibited file writes, outbound network and credential access fail **at the boundary**.
3. Demonstrate start/status/cancel/result and reliable, bounded replay of one authorized job without unsafe duplicate effects.
4. Confirm task ownership, approval audit, and Jira/PR status reconciliation after a simulated interruption.
5. Verify actual cost tracking and per-job budget enforcement.
6. Present evidence, maintenance comparison and go/no-go decision to Chief. No cutover without separate approval.

## Minimal proof of concept (future ticket only)

For an explicitly authorized, low-risk Jira ticket, launch one isolated Hermes-managed Byte worker on a disposable repository branch. Show status and structured results in Forge Workbench, produce tests/evidence and a draft PR; stop before merge or deployment. No additional scheduler, new LLM gateway, or game engine is part of the spike.

## Work tracking

- **FORGE-14:** move back to Backlog. Preserve feature branch and unfinished checkpoint work.
- **FORGE-17:** architecture feasibility, evidence, integration boundary, threat model and explicit go/no-go.
- **FORGE-6:** evaluate Byte under Hermes profile/Kanban with real enforced isolation.
- **FORGE-9:** Workbench UX, approval queue, project/agent switching and return-to-work briefing.
- **FORGE-18:** model/cost optimization stays deferred.
- Other Forge 2.0 Jira issues remain unchanged pending dependencies review.

## Decision

**Proposed, not yet accepted as a technical migration.** The Chief Architect approved documenting this architectural pivot for review on 2026-10-24. Existing code stays intact; no runtime implementation, automatic merge or deployment is authorized.

> Build games. Build software. Create worlds. Use existing tools where they work. **KISS or PORCH.**
