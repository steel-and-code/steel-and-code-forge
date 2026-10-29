# Architecture decision: Pause Outpost; use Konsole with Forge Workbench

- **Date:** 2026-10-24
- **Decision:** Outpost terminal development is paused indefinitely. **Do not delete or archive its repository** as part of this decision.
- **Decision maker:** Chief Architect, Steel & Code
- **Status:** Accepted direction; implementation/configuration changes not authorized by this documentation alone
- **Related:** [Hermes-based Studio Workbench ADR](adr-hermes-studio-runtime.md), FORGE-20

## Context

Outpost was envisioned as a keyboard-first terminal and AI workstation based on a Konsole fork, offering stations for AI agents and an integrated studio command center. We now have a functional Steel OS desktop using KDE Konsole, and are building Forge Studio Workbench as the graphical operator interface to a Hermes-backed AI team.

Maintaining an entire Konsole fork to duplicate terminal, agent conversation, or Workbench functionality adds code, packaging, security, and long-term update burden without a demonstrated unmet need.

## Decision

1. **Steel OS** is the workstation operating environment.
2. **Konsole**, unmodified where possible, is the everyday terminal for shell, SSH, Git, Neovim, troubleshooting, and direct CLI interaction with Hermes.
3. **Forge Studio Workbench** owns the GUI for projects, studio conversations, AI activity, task visibility, and Chief Architect approvals.
4. **Hermes** is the preferred candidate agent coordination/runtime behind the Workbench, subject to proven execution isolation and integration safeguards.
5. **Outpost** is on indefinite hold, not cancelled or deleted. Its existing code and UX explorations remain available if a concrete unmet use case emerges.
6. **No Outpost-to-Forge integration is on the active roadmap.** If desired later, begin with simple desktop launchers, keyboard shortcuts, window placement, or Konsole profiles rather than a fork or embedded terminal.

## What this does NOT mean

- No removal of Outpost's GitHub repository, history, existing code or experiments.
- No requirement to eliminate Konsole or change Steel OS defaults.
- No claim that Forge/Hermes integration is completed; FORGE-20 is the small first text-only GUI round-trip.
- No bundled remote-control endpoints or relaxation of Forge's human authorization and security boundaries.
- No new implementation work is automatically authorized by this decision.

## Revisit criteria

Reconsider custom terminal development **only if** normal Konsole plus Steel OS plus Forge demonstrably cannot deliver a specific, frequently needed operator workflow, and simpler mechanisms (Konsole profiles, shortcuts, desktop integration, existing plugins) have been tested first. Record a scoped problem, alternatives, maintenance cost, and a Chief-approved implementation ticket before reactivating Outpost.

## Guiding principle

> **KISS / PORCH:** Build and ship games and software. Do not maintain a second terminal when the one we already use works.
