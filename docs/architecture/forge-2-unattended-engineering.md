# Forge 2.0 — Approved Unattended Engineering Workflow

**Status:** Architecture approved by the Chief Architect on 2026-10-20. **Implementation not authorized by this document alone.**  
**Scope:** Forge 2.0 orchestrator, Byte engineer, Jira intake and decisions, GitHub delivery, and approved deployment automation.

> **The Chief Architect's absence is not a blocker. The Chief Architect's approval is a requirement.**

## Purpose

The Chief Architect and Neighbor hold a chalkboard conversation to shape a feature into requirements, constraints, acceptance criteria, and risk boundaries. Only with the Chief Architect's approval is the work recorded in Jira. Forge automatically picks up **authorized** tickets marked **Ready for Forge**, schedules durable jobs, and assigns engineering work to Byte. The Chief Architect can teach class or work with students while Byte executes bounded, safe work unattended. Questions, approvals, and evidence survive chat closure, host reboot, and agent restart.

This is a **target workflow**; not a claim that these integrations are already operational.

## Roles and authority

- **Chief Architect (human):** Product vision; approves requirements, ready-for-engineering state, scope changes, sensitive operations, PR merges, and production releases. Can pause or cancel jobs.
- **Neighbor (design/review collaborator):** Chalkboard requirements and acceptance criteria; creates/updates Jira tickets when authorized; interprets engineering questions and helps review evidence. Does not unilaterally approve on behalf of the Chief Architect.
- **Jira (durable coordination):** Authoritative record of requested work, decisions, questions, human approvals, blockers, and high-level status. A backlog ticket alone never authorizes execution.
- **Forge (orchestrator):** Watches for explicit Ready for Forge transitions, validates authorization, claims jobs idempotently, enforces scope/budgets, records audit history, routes work to agents, and pauses safely on uncertainty.
- **Byte (lead AI engineer):** Works in isolated workspaces/feature branches; edits code, runs tests and checks, supplies progress and evidence, and opens reviewable GitHub PRs. No unilateral merge or production deploy.
- **Hermes (independent engineering collaborator):** Optional specialized collaborator under a scoped assignment and the same approval/permission controls; not the source of authorization.
- **GitHub:** Source control, branch/PR review, CI evidence, release artifacts. Work is committed to a feature branch and exposed in a PR **before** the Chief Architect's final merge approval.
- **DigitalOcean:** Public production hosting. Forge may prepare deployment; requires separate explicit human approval to deploy.

## End-to-end state flow

1. **Chalkboard → Backlog:** Chief Architect and Neighbor design requirements, constraints, acceptance criteria, definition of done, and risks. Neighbor creates/updates Jira work item *only after explicit approval*.
2. **Backlog → Ready for Forge:** Human explicitly authorizes the ticket for engineering, including approved repo, branch policy, available tools, budget, and prohibited actions.
3. **Ready → Claimed/In Engineering:** Forge detects a Jira event or polls, de-duplicates delivery, checks authorization, and durably claims a single job before dispatch. Byte prepares an isolated workspace and a feature branch.
4. **Implementation:** Byte codes, tests, checks security, updates job progress, and stores artifacts. All actions stay within per-job permissions, time/token/cost budgets, and approved repos.
5. **Needs Architect Input:** If requirements conflict, risk or costs exceed bounds, unexpected destructive changes are needed, or an unapproved product decision arises, Forge **suspends the job** and posts a concise question with options and impacts to Jira. No silent guessing or repeated risky execution. Human response is recorded in Jira, validated, and delivered back to the same durable job.
6. **Ready for Review:** Byte commits changes to its feature branch, runs appropriate checks, and opens a PR with a summary, acceptance-criteria mapping, test results, known limitations, and any security or operational implications. Forge updates Jira.
7. **Review/Approval:** Chief Architect reviews the PR; changes can cycle back to Engineering. **No automatic merge.** Approval must be tied to a specific revision/commit; material changes after approval require renewed review.
8. **Release preparation:** After human-authorized merge, Forge may build and stage release artifacts, check environment compatibility, establish backups, run migrations plan and health checks, and produce a rollback procedure.
9. **Release approval → Deploy:** Production deployment requires its **own** explicit approval for a specific artifact/environment and the applicable backup, test, and health-check evidence. Forge performs only the approved deployment, verifies outcome, and executes safe rollback or stops/escalates on failure.
10. **Released/Closed:** Forge publishes PR/deployment links, test evidence, approval trace, and final status to Jira; user receives a concise briefing.

## Suggested Jira statuses

**Backlog → Ready for Forge → In Engineering → In Review → Approved → Released**

Side states: **Needs Architect Input**, **Blocked**, **Failed**, **Cancelled**. Paused tickets resume their previous permissible stage only after an authorized answer or retry decision. Jira is the human-readable project record; Forge's durable JobStore is the operational source for precise claims, transitions, and execution state. Reconcile the two after outages instead of assuming exactly-once webhook delivery.

## Non-negotiable engineering controls

1. **Human approval gates.** Backlog creation ≠ execution authorization. Starting work requires Ready for Forge. Merging and production deployment require distinct, explicit human approvals. No agent may self-approve or weaken a gate.
2. **Durable jobs and idempotency.** Persist state, assigned owner, attempts, checkpoints, approvals, and evidence before dispatch or side effects. Handle duplicate Jira events and reboot recovery. Do **not** claim exactly-once execution without durable storage and appropriate external idempotency.
3. **Least privilege and isolation.** Per-repository/job credentials, segregated worktrees or sandboxed execution, no production secrets in prompts or logs. Deployment authority separate from coding authority. Never allow unrestricted shell/network access by default.
4. **Bounded autonomy.** Enforce per-job/global spending and time budgets, retry ceilings, and concurrency quotas; fail closed when budget or permission checks cannot be completed.
5. **No silent product choices.** Questions go through Jira with context, choices, and consequences. Await human instruction when outside approved scope.
6. **Evidence and traceability.** Log actions with relevant job IDs, Jira issue keys, branch/PR references, tests, model/cost usage, approval actors, artifact hashes, and deployment results; protect credentials and private data.
7. **Safe rollout and rollback.** Verify target configuration, backup existence and restoration strategy, asset delivery, database migration plan, staging smoke tests, health checks, and rollback before production release.
8. **Interruption-safe.** Forge can stop and resume after SSH loss, browser closure, restart, or power loss without forgetting decisions or creating duplicate PRs/side effects.

## Operator experience

- **Katana:** Primary cockpit; Outpost or browser GUI shows chat, job statuses, questions, tests, PRs, and approval queue. A conversational UI is useful but **not a dependency** of the first functional Jira-driven workflow.
- **Forge laptop:** Always-on headless Linux engineering host running orchestration and workers; secure SSH and private authenticated web access; no need to use its desktop.
- **Command laptop:** RTX 5090 optional GPU inference worker. Forge's correctness must not depend on Command always being powered on.
- **DigitalOcean VPS:** Public production workload, logically separate from Forge's execution environment. Never expose Forge's operator controls publicly.

## Briefing when the Chief Architect returns

Show completed jobs, blocked items and precise Jira questions, PRs requiring review, tests and evidence, spending/time summaries, failures/retries, and pending deployment requests. Distinguish **work complete** from **approved for release** and **deployed**.

## Recommended phased implementation

- **A — Job contract (already reported locally):** State machine, idempotency keys, claim/budget contracts. Earlier reported commit `360bb44`; verify repository branch/commit before assuming it is merged or remote.
- **B — Durability:** Persistent JobStore, atomic claim/transition, resume/reconciliation, audit trail, and duplicate Jira event handling.
- **C — Jira intake:** Approved Ready for Forge gate, issue state mapping, structured questions, and response/resume handling.
- **D — Byte engineering:** Isolated workspace, branch operations, allowed tools, tests, budget and approval enforcement.
- **E — GitHub delivery:** Draft PR with evidence, review cycle, immutable approval binding, no auto merge.
- **F — Staging/deployment:** Backups, dry runs, release-specific authorization, deploy health checks and rollback. Use Steel & Code website as a concrete acceptance test.
- **G — Operator UX:** Katana/Outpost dashboard, notifications and return-to-work briefing; expand the conversational interface when orchestration is proven.

## Acceptance scenario

Given an approved Jira ticket for a Steel & Code website change:
- Forge picks it up while the Chief Architect is teaching, claims it once, and records its state;
- Byte uses a scoped feature branch, edits code, and runs tests without needing the chat open;
- if a requirement or security issue arises, the job pauses with an actionable Jira question;
- after approved clarification, Byte resumes and creates a reviewable PR with evidence;
- no merge occurs without human review, and no production deployment occurs without a **separate** human release approval;
- after approval, Forge validates backup/readiness, deploys, verifies healthy HTTP/static endpoints, and records outcomes; on failure it follows the approved rollback policy.

**Design boundary:** This document records approved product behavior and gates. Hermes/Byte implementation assignments will be created separately after the Chief Architect approves an actionable engineering ticket.
