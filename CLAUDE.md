# Botcoin Project Instructions — botcoin-dash

## Purpose

This project is part of myBotCoin, a Bitcoin-only product focused on stacking sats in a simple, understandable way. This folder is the **`botcoin-dash`** repo — the dashboard and user-facing interface.

## Repo structure

Botcoin currently has two separate codebases, treated as different working contexts unless direct inspection proves otherwise:

- `botcoin-dash` (this repo) — the dashboard and user-facing interface
- `botcoin-bot` — the bot and related trading/runtime behavior, in a sibling folder with its own `CLAUDE.md`

Do not assume that changes, commands, or deploy behavior here also apply to `botcoin-bot`, or vice versa.

## Required behavior

- Read the docs in `docs/` before making changes.
- Explain findings in plain English before editing.
- Wait for approval before changing trading logic, deployment behavior, credentials, or automation scope.
- Keep changes small and easy to review.
- After every change, report exactly which files changed and what should be tested.

## Safety rules

- Treat this as a live-money project.
- Cold storage and playable exchange balance are separate risk tiers and must never be mentally merged.
- Flag anything that changes buy/sell logic, wallet-tier behavior, re-entry rules, or bot automation.
- Preserve the Bitcoin-only identity.
- Keep the dashboard simple for normal users; advanced jargon belongs in expert surfaces.
- Never place secrets, API keys, tokens, or sensitive credentials into chat, markdown docs, commits, or code comments.
- Treat any push to an auto-deploy branch or production-connected branch as a production event requiring extra care.

## Strategy v3 program (started 2026-09-21)

The bot's v2 strategy is being replaced by Strategy v3, the "one-knob engine", built in `botcoin-bot`. See that repo's `CLAUDE.md` and `docs/` for its principles, rails and phases.

What matters for this repo:
- **Phase 4 is the dashboard work.** Nothing here changes before Phase 4 is approved.
- **The v3 dashboard renders from the bot's API:** the engine's name/version, its settings/controls, attribution, and its events. It does not keep its own copy of strategy definitions; the dash's copy of `AGGRESSION_LEVELS` was the drift risk v3 removes.
- **The only user controls will be the risk knob and DCA.** Big events (large sells, the pool going all-cash or all-BTC, rails firing, halts, knob changes) must be impossible to miss.
- **Superseded:** `feature/aggression-level-tracking` (`12c0851`, v2.8.7) is closed unmerged. Kept for reference; do not merge.

## Docs

- `docs/product-context.md`
- `docs/risk-guardrails.md`
- `docs/deploy-notes.md` (restored 2026-09-21; see the note at its end)
- `docs/eos-protocol.md`
- `docs/repo-map.md`
- `docs/commands.md`

## Default workflow

1. Inspect first.
2. Explain in plain English.
3. Propose the smallest safe next step.
4. Wait for approval.
5. Work on a feature branch — never commit directly to `main`.
6. Make one small change.
7. Explain exactly what changed.
8. Before proposing a merge to `main`, run the app locally (see `docs/commands.md`) so the change can be checked in a browser first.
9. Only merge to `main` once explicitly approved — merging to `main` triggers a live production rebuild.
