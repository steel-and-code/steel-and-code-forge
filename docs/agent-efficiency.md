# Forge Agent Efficiency and Cost Discipline

Date established: 2026-10-31
Status: Proposed team operating guidance (review before merging)
Related: [Forge Engineering Workflow](engineering-workflow.md)

## Principle

**Agents optimize for completed, verified work—not maximum model turns, tool calls, or lines changed.** Keep the workshop supporting Steel & Code game/software delivery. **PORCH:** stop expanding infrastructure when it no longer serves a concrete project.

## Before an assignment

- State the specific deliverable, explicit acceptance checks, branch/worktree, and what is excluded.
- Estimate whether paid inference is justified. Prefer available local models for suitable work, but do not silently change providers or quality requirements.
- Agree on a per-task spending ceiling when paid inference is involved; a suggested initial human-controlled target for small investigations is $1–$2, **not an enforced Forge limit**. Stop and ask before exceeding the agreed ceiling.
- Preserve current interfaces and compatibility unless the task expressly requires a breaking change. Review the expected test surface before implementation.

## During an assignment

- **Read narrowly:** use search/grep and line ranges before ingesting entire source files or long design documents. Avoid repeatedly loading the same large files.
- **Edit narrowly:** make the smallest coherent patch. If the change unexpectedly spreads to many files or changes public output formats, stop and explain why.
- **Test narrowly first:** run affected tests, fix failures, then one appropriate broader suite before requesting review. Do not repeatedly paste huge test outputs into model context; summarize failures and retain concise artifacts.
- Maintain task and context boundaries. Repeated long transcripts and tool outputs can inflate billed prompt tokens, including when prompt caching reduces their unit cost.
- At checkpoints report progress, blockers, remaining work, and actual usage **when provider telemetry is available**. Never present guessed tokens, model calls, or cost as measurements.
- Never spawn subagents, escalate to more expensive models, or exceed the authorized spend without approval.

## Completion and handoff

- A model's DONE claim is **not objective proof of task completion**. Report changed files, validation commands/results, remaining gaps, branch and PR.
- BLOCKED or INCOMPLETE must not be presented as successful delivery.
- Include observed token usage, cache use, request count, model, and cost when available. Label estimates explicitly.
- No automatic merge; protect main and the designated integration branch.

## Case study — 2026-10-31 Hermes completion protocol

Hermes reported implementing explicit DONE/BLOCKED/INCOMPLETE semantics in `feature/explicit-completion` (PR #34). It reported 2,281 tests passing and 83 skipped; these are developer-reported results until independently verified. A key inefficiency was altering `RunResult.text` in a way that caused many existing UI/CLI/TUI tests to need changes. Future work should retain raw display text and store machine-facing outcome separately unless a change of interface is explicitly approved.

Hermes also reported repeatedly reading large files, multiple full-suite runs, and patch remediation. Its self-estimated usage was *not measured*. An OpenRouter activity export reviewed on October 31 showed substantial repeated prompt/context processing across Hermes requests, but **do not equate the whole exported period with this single assignment** without per-request and per-run correlation. Do not commit raw billing exports, API keys, or other sensitive usage/account data to the repository.

## Follow-up engineering need (not authorized for implementation)

Track cost/usage observability and context budgeting as an issue: collect provider usage per request/run, show input/output/cached tokens and cost when supported, prevent unbounded transcript/tool-output growth, and support optional task spend limits. **Do not build this as part of documenting the rule.**
