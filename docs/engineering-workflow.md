# Forge Engineering Workflow

> **One integration branch. Small feature branches. Tested pull requests. Finish what we start.**

This document is the shared working agreement for the Steel & Code Forge team, including the Captain, Neighbor, Byte, and Hermes.

## Rules

1. **One active integration branch.** Designate one shared, tested integration target for current development. Record its name in the active integration plan; do not assume that `main`, `feature/forge-2.0`, or `feature/workbench` already contains every feature.
2. **Small, focused feature branches.** Each feature or repair gets a clearly scoped branch and, when parallel work requires it, a separate worktree. Byte and Hermes must avoid editing the same worktree concurrently.
3. **Pull requests and passing tests.** Integrate completed features through reviewed pull requests with relevant automated tests passing. Run the combined test suite on the integration result before promoting it.
4. **Protect `main`.** Require appropriate PR review and status checks and prevent accidental force pushes or deletions. Promote tested, reviewed integration work into `main`; do not make ad hoc direct changes there.
5. **Preserve before reconciling.** Inspect `git status` and diffs in *every* affected worktree before merging, rebasing, cherry-picking, cleaning, or deleting branches. Commit or otherwise securely preserve staged, unstaged, and untracked work in its own proper scope. Never discard unknown work.
6. **Finish before expanding.** Favor validating and integrating existing Forge features over starting another feature. Build new tooling only when a real game or software task exposes a concrete gap.

## Practical feature flow

1. Identify the current integration target, owning agent, and acceptance criteria.
2. Create a focused feature branch/worktree from the correct base; capture the initial worktree status.
3. Implement the smallest useful change, keeping approval boundaries and protected operations intact.
4. Run focused tests, then the appropriate full suite. Report test failures honestly.
5. Open a pull request against the designated integration branch; review changes and required checks.
6. Merge after review and passing checks, confirm the integrated behavior, then retire superseded branches/worktrees **only after verifying no unique work remains**.
7. Promote the tested integration branch into protected `main` via the agreed review process.

## Current reconciliation note (2026-10-29)

Hermes' read-only audit found separate **job orchestration** (`feature/forge-2.0`) and **Workbench/UI** lineages, and unfinished work in several local worktrees. **No final unified integration branch has been selected yet.** Preserve FORGE-14 progress, FORGE-20 Jira/model tuning, and FORGE-22 ACP work before attempting cross-track integration. The ACP execution path stays deferred pending safety review. Do not infer that the existence of a branch means its feature is tested or merged.

## Agent handoff checklist

For each task, record: **branch/worktree, base/integration target, scope, files changed, tests and results, commit(s), PR, blockers, and remaining uncommitted work**. Reading files and routine in-scope engineering actions should not require constant interruption; destructive, secret-bearing, out-of-scope, and protected actions retain their approval safeguards.

**KISS / PORCH:** Keep the workflow lightweight. Do not create new infrastructure to manage the workflow unless a concrete delivery need justifies it.
