# Forge 2.0 — Engineering Roadmap (October 2026)

> Status: proposed architecture; implementation not complete. See GitHub issues [#2](https://github.com/steel-and-code/steel-and-code-forge/issues/2) and [#3](https://github.com/steel-and-code/steel-and-code-forge/issues/3).

## Vision
Conversation-first, open-model engineering system. Chief Architect authorizes work; Neighbor helps plan/review on demand; Forge dispatches; Byte executes in a sandbox; GitHub CI verifies; the human reviews and approves. Hermes stays independent.

## First proof of concept
An approved Outpost Jira ticket enters Forge, generates one restart-safe job, invokes Byte with a model through the existing gateway, performs permitted edits and tests in an isolated Git worktree, posts bounded progress to Jira, and opens a draft PR. Jira gets links, tests, usage, and outcome. No automatic merging.

## Phases
1. **A — Contract and safety**: inventory the existing runtime. Model job status and authorization, webhook authentication, deduplication, per-repo allowlists, budget gates, and audit logs. Preserve current permission defaults.
2. **B — Intake and persistence**: Jira trigger/adapter with durable queue, restart-safe retries, no duplicate execution.
3. **C — Byte worker**: resumable task context, isolated worktree, approved tool/test commands, model routing through OpenRouter initially, structured updates.
4. **D — PR and review**: GitHub draft PR, CI tests, review feedback routed back to the same Byte task, human merge approval.
5. **E — Release**: staging automation; separate human production authorization; health checks/rollback. Not in initial MVP.

## Safety invariants
No unapproved ticket execution; no direct protected-branch writes; no self-merge; no silent privilege expansion; model costs metered and capped; no production secrets in agent jobs. Work remains auditable and interruptible. Human intervention for privileged operations.

## Infrastructure concept
- Byte laptop: always-on orchestration server (proposed, not yet deployed).
- Command laptop (i9, 64 GB, RTX 5090 Mobile): optional local inference after NVIDIA support is validated; not a blocker.
- Katana: hands-on development and testing.
- Initial experiment budget ceiling: $20/month of model API spend, enforced before autonomous execution (actual price varies).
- Provider-agnostic local/cloud/hybrid routing planned.

## Pilot exit criteria
One harmless Outpost task: approved Jira ticket → durable Forge job → Byte executes/tests → draft GitHub PR → Jira update → human decision. Demonstrate deduplication, restart recovery, safe failure, and a hard cost stop.

## Deferred
Outpost GUI integration; production auto-deploy; mobile clients; parallel autonomous fleets; STEELCAST development through Forge.

## Open-source licensing decision
The README now states Steel & Code's intent to make Forge open source, with the license pending. Choose and approve a standard open-source license, then commit its full text as `LICENSE`. Public visibility alone does not grant open-source reuse rights.
