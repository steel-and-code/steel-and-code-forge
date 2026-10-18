# Forge 2.0 — AI Engineering Platform Roadmap

**Steel & Code | Planning baseline: October 18, 2026**  
**Status:** Proposed roadmap; implementation begins **after Outpost v1**.  
**Decision:** **Build Byte as one capable lead engineer first; add delegation later.**

## Vision

Forge 2.0 turns the existing Forge runtime into a conversation-first, persistent AI engineering platform. **Byte** should be able to understand a project, inspect its repository, propose an approach, edit code, execute bounded commands, test and repair failures, request approvals, and resume an engineering conversation after interruption. We want the experience of collaborating with a real engineering partner, informed by working with Hermes, not merely a one-shot chatbot.

> The human directs the engineering. Byte executes the work. Forge enforces the boundaries. Outpost makes the collaboration visible.

Forge remains a usable standalone CLI/TUI runtime. **Outpost** is the graphical/terminal command station, not a required dependency of Forge. The human is **Chief Architect** and retains authority over architecture, scope, permissions, and delivery. **Byte** is the lead AI engineer. **Hermes** remains an external collaborator; **Neighbor**, **Ember**, and **Fin** are optional later specialists, not day-one dependencies.

## Sequencing and scope

1. **Complete Outpost v1 first.** Stabilize the Konsole-based command center, normal terminal behavior, workspace navigation, graphical AI stations, and keyboard-first experience.
2. **Return to Forge and audit the code as it exists.** Reuse its tested tooling, safety controls, model routing, TUI, and Workbench. Avoid a rewrite.
3. **Build Byte's single-agent engineering workflow.**
4. **Connect Forge to Outpost.**
5. **Develop optional research instrumentation and, later, delegation.**

This roadmap is a plan, **not a claim that the milestones below are already implemented**. Existing documentation may be behind development branches, so Phase 0 establishes a verified baseline.

## Architecture and ownership

```text
Human Chief Architect
   │ requirements, decisions, approvals
   ▼
Outpost v1 (optional UI)
   ├── ordinary terminal, workspaces
   ├── conversation and specialist station
   └── model / tokens / time / costs / approvals dashboards
   │
   ▼
Forge 2.0 (standalone runtime + CLI/TUI)
   ├── agent session lifecycle and persistence
   ├── Byte engineering agent (single-agent first)
   ├── model discovery, routing, fallback and budgets
   ├── repository tools, execution, tests, Git
   ├── permissions, approval gates, audit events
   └── opt-in research telemetry and exports
   │
   ▼
Models: OpenRouter providers and supported local inference
```

- Forge owns execution policy, session records, model telemetry, and engineering event records.
- Outpost renders status and controls, but does not duplicate safety enforcement.
- The underlying tool/safety boundary applies equally to CLI/TUI, Outpost, and future APIs.
- Model changes must not silently discard conversation or engineering state.
- Commits, pushes, destructive commands, and scope expansions remain subject to the established explicit approval policy.

## Phased delivery

Time estimates are **rough effort ranges**, subject to the Phase 0 audit and available working time. They are not calendar promises.

### Phase 0 — Forge 1.x audit and baseline (about 1 week)

- Inventory current CLI/TUI, Workbench, agent runtime, tool APIs, Git controls, tests, model registry/router, logging, persistence, and known bugs.
- Run baseline checks and record exact working commands, supported environments and test results.
- Reconcile documentation with actual code; identify what to retain and what is missing.
- Specify safety invariants and create a short gap analysis for Byte versus observed Hermes behavior.

**Exit criterion:** repeatable baseline and prioritized implementation backlog; no needless redesign.

### Phase 1 — Byte Engineering Console (about 2–3 weeks)

- Persistent-feeling, conversation-first engineering CLI/TUI with streamed dialogue and visible actions.
- Display exact active model/provider, input/output/cumulative tokens, context usage (distinct from cumulative tokens), session elapsed time, and estimated versus confirmed cost.
- Show tool-call status and clear approval prompts; support interrupt, cancel, resume and readable errors.
- Preserve keyboard-first navigation and sensible terminal behavior.
- Start a structured session event schema here, rather than retrofitting telemetry later.

**Exit criterion:** Byte can sustain a usable natural engineering conversation with accurate live session status.

### In-session human decision gates (Phase 1 foundation; Phase 2 integration)

**Requirement from observing Hermes (October 18, 2026):** Byte must recognize when an engineering choice belongs to the Chief Architect, present a concise question with selectable options, explain trade-offs, indicate a recommendation where appropriate, and **pause within the current running engineering conversation**. The user answers without exiting the TUI, spawning a separate questionnaire, restarting Byte, or losing task context. Byte then resumes precisely the suspended workflow with the chosen answer recorded as an explicit decision.

- Support arrow-key/number selection and Enter confirmation; offer free-text/Other where useful. Respect the existing keyboard-first interface.
- Distinguish **engineering decision gates** (architecture, UX, conventions, implementation trade-offs) from **safety approval gates** (permission to run tools, change files, mutate Git, spend money). A design choice never substitutes for a safety authorization.
- Preserve pending decision, selected answer, original task, execution checkpoint, and relevant context across recoverable interruptions and application restarts.
- Clearly show *waiting for human decision* versus *working*; never silently choose a default on timeout or unavailable input.
- Ask only for genuinely human-owned or materially ambiguous choices; avoid burdening the user with routine implementation decisions.
- Emit structured, privacy-conscious events for when a gate was raised, the options provided, human response, and elapsed wait time. This supports later research into human oversight and engineering role changes.
- Test that the original TUI session remains alive, tool execution is paused safely, input is applied exactly once, and the agent resumes without replaying unsafe operations.

**Acceptance scenario:** During an engineering task Byte encounters competing keyboard shortcut designs, displays numbered choices with a recommended option, waits for the Chief Architect's selection within the terminal application, then continues implementation without leaving the session or forgetting prior work.

### Phase 2 — Reliable single-agent engineering loop (about 3–4 weeks)

- Read/search repository, inspect history, edit and patch files, execute approved commands, build, test, diagnose failures, retry and summarize.
- Use bounded plans and verification checkpoints; expose actions and results without pretending unverified work succeeded.
- Prefer structured file operations; enforce workspace containment and approval requirements.
- Keep Git commit/push behind appropriate explicit approval.
- Test failure recovery, cancellation, tool limits, partial edits, unexpected output, and restart behavior.

**Exit criterion:** Byte independently completes a bounded engineering task, validates it, reports limitations, and waits for required approval before delivery.

### Phase 3 — Durable memory and project continuity (about 2 weeks)

- Associate sessions with project/workspace, Git branch, task ID, decisions, and checkpoints.
- Resume conversations and partial task state after exit, host restart or model failure.
- Add explicit summarization/compaction with provenance and boundaries; never imply forgotten context is still fully available.
- Store credentials outside logs and memory, with sensible retention and deletion controls.

**Exit criterion:** Byte can continue an interrupted task without requiring the entire original assignment to be pasted again.

### Phase 4 — Intelligent model discovery and routing (about 2 weeks)

- Query and cache OpenRouter catalog metadata: capabilities, context, pricing, availability, and supported parameters.
- Define Byte's model requirements, score candidate models for task type, cost, latency, capability and measured reliability.
- Support a configurable preferred DeepSeek model (exact identifier to confirm), supported local models, fallback candidates, and manual override.
- Recover gracefully from provider 402/429/503 or similar failures while preserving session state; **never** bypass budgets or approvals automatically.
- Show model-switch history, reasons, live estimated spend and configurable hard/soft caps.

**Exit criterion:** model selection and fallback are transparent, cost-conscious, and do not lose engineering continuity.

### Phase 5 — Outpost integration (about 2–3 weeks)

- Expose a stable Forge interface to Outpost for conversations, workspace/session selection, tools, approvals and telemetry.
- Render Byte's engineering station in Outpost's existing command-center design.
- Support persistent specialist sessions without breaking ordinary Konsole terminal sessions.
- Keep Forge usable independently from Outpost, including on headless or remote machines.

**Exit criterion:** Byte works through Outpost with the same enforcement and continuity as the standalone console.

### Phase 6 — Engineering research telemetry (about 2 weeks for initial research-ready layer)

Collect **opt-in, local-first** task and session events with export to CSV/JSON. Include:

- Session and task IDs, elapsed time and explicit active/idle distinctions.
- Model/provider and switches, token counts, context pressure, price estimates and billing where available.
- Repository/tool actions, test outcomes, reruns, failures, rework, and interventions.
- Human-supplied annotations of time spent on architecture, planning, prompting, coding, review, debugging, and approvals.
- Versioned instrumentation definitions and consent/retention/export controls.

**Research track A — Productivity:** time to completion, accepted changes, defects, quality, rework, tool/model expense, and human effort.

**Research track B — Engineer role transformation:** movement from hands-on implementation toward architecture, task design, supervision, evaluation, and accountability.

Telemetry alone does not prove productivity gains or causation. Plan comparable tasks, defined quality measures, human-only/AI-assisted baselines where feasible, and ethical/privacy review before formal studies. Do not upload proprietary source code, student records, credentials, or private conversations into research exports by default.

**Exit criterion:** reproducible, privacy-conscious observations suitable for exploratory research and later controlled studies.

### Phase 7 — Multi-agent delegation (later, not part of initial Byte milestone; about 3–4 weeks exploratory)

Only after the single-engineer loop is trustworthy:

- Allow Byte to propose bounded handoffs to optional agents such as Hermes, Neighbor, Ember, and Fin.
- Define task ownership, shared evidence, review gates, cancellation, permissions and budget limits.
- Log delegation decisions and outcomes; prevent uncontrolled recursive delegation.
- Compare single-agent versus delegated workflows using the research instrumentation.

**Exit criterion:** measurable improvement without losing human control or traceability.

## Release gates

| Milestone | Scope | Gate |
| --- | --- | --- |
| Forge 2.0 Alpha | Phases 0–2 | Byte reliably handles bounded engineering tasks alone. |
| Forge 2.0 Beta | Phases 3–5 | Session continuity, model handling, and Outpost integration work end to end. |
| Forge 2.0 Stable | Reliability hardening and telemetry foundation | Security, documented test suite, recovery and cost controls verified. |
| Forge 2.1+ | Richer research dashboards and Phase 7 delegation | Single-agent baseline established first. |

Phase 6 design starts in Phase 1 via a common event schema even though full research features come later.

## Initial acceptance scenario

Using a safe Steel & Code test repository, ask Byte to:

1. Restore the project, current branch, and previous task checkpoint.
2. Inspect relevant files and devise an implementation plan.
3. Make focused changes using approved tools.
4. Run targeted tests and diagnose/retry failures.
5. Report changed files, results, unresolved concerns, exact model, token counts, session time, and estimated costs.
6. Request approval before Git commit/push where required.
7. Survive a simulated provider interruption and resume with context intact.

A useful comparison is the Hermes-supported Outpost Phase 4A.2b/4A.2c workflow, but Byte should implement an independently designed, testable system rather than clone Hermes internals.

## Non-goals for the initial release

- Autonomous multi-agent delegation from day one.
- Rewriting the functioning Forge runtime.
- Making Outpost necessary for Forge.
- Silent destructive operations or automatic pushes.
- Claiming a research causal result from unstructured logs alone.

## Decisions and open questions

**Decided October 18, 2026**
- Finish Outpost v1 before returning to major Forge 2.0 development.
- Byte is a **single lead engineer first**; delegation comes after reliability.
- Conversation-first interaction and transparent tools are essential.
- Active model, token usage, session duration, and costs should be visible.
- Research covers **both engineering productivity and changing engineering roles**.
- Forge must remain terminal-first and usable without Outpost.

**To confirm during Phase 0**
- Exact preferred DeepSeek model identifier and alternatives.
- Current test/build baseline and outstanding technical debt.
- Session persistence format, event schema, retention and consent defaults.
- Benchmarks, spending budgets and release quality thresholds.
- Exact interface contract between Forge and Outpost.

---
*Planning document. Revise as evidence from implementation, user testing, and research accumulates.*
