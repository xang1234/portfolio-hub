# portfolio-hub

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

A lightweight, mobile-friendly multi-broker portfolio dashboard. Hosted on a spare laptop, exposed over Tailscale.

## Status

Planning / pre-implementation. See [`PLAN.md`](./PLAN.md) for the full design and [the issue tracker](../../issues) for the v1 implementation slices.

## Operations

- **Before going live**, work through [`docs/HITL-GOLIVE.md`](./docs/HITL-GOLIVE.md) — the human-in-the-loop verification checklist that gates "ready for daily use." It covers Tailscale boundary, read-only API enforcement, data-vs-TWS spot checks, market-hours panel accuracy, reconnect resilience, privacy logging, and seed-job sanity.
- Supporting scripts live in [`scripts/`](./scripts):
  - `verify_read_only_api.py` — confirms the gateway rejects order placement.
  - `audit_privacy_log.sh` — greps logs for dollar-amount leaks at non-DEBUG levels.
  - `dump_seeds.py` — prints recent `equity_snapshots` / `fills` rows + live account-summary for TWS comparison.

## Scope

| Concern | v1 |
|---|---|
| Brokers | IBKR (via `gnzsnz/ib-gateway-docker`) |
| Asset classes | STK, ETF, CASH |
| Stack | FastAPI + HTMX + Alpine.js + TradingView Lightweight Charts |
| Persistence | SQLite |
| Live updates | Server-Sent Events with row-level deltas |
| Auth | None (Tailnet is the boundary) |

Adapters for Futu/MooMoo, Tiger, and Longbridge are out of scope for v1 but the `Broker` Protocol is designed so each can be added later in ~150 lines without touching the UI, market-hours, FX, or seed-job code.

## Key design rules

- **HK and Taiwan are always rendered separately from mainland China** — `🇭🇰` for HKEX, `🇹🇼` for TWSE, `🇨🇳` only for SSE/SZSE. This is a hard requirement, not a default.
- **Read-only API** at the IBKR gateway for v1 — accidental order placement is impossible at the gateway level until order entry is explicitly added.
- **No public exposure** — Tailscale Funnel disabled; the dashboard binds to the Tailnet interface only.
- **Dual-listed instruments stay separate** — `9988.HK` and `BABA.US` are different rows with different cost bases.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE) for details.

You are free to use, modify, and redistribute this code under the terms of the Apache 2.0 license. The license includes an explicit patent grant and requires attribution and a copy of the license to be preserved in derivative works.
