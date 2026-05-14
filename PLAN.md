# Portfolio Hub — Multi-Broker Holdings Dashboard

## Context

A lightweight, mobile-friendly portfolio dashboard for a personal trading setup spanning **four brokers** (IBKR, Futu/MooMoo, Tiger, Longbridge) and multiple Asia/US markets. Hosted on a spare laptop, exposed over Tailscale. The user is currently flying blind across these accounts — no single view of total exposure, no resolved English names for Asian numeric tickers, no awareness of when markets (with their lunch breaks and holidays) are actually open.

**Day-1 outcome**: a single page that shows every IBKR position with English names, native + USD values, P&L in USD, plus a market-status panel that tells the user which markets are open right now and when they next open/close. The architecture is designed so that adding Futu, Tiger, and Longbridge adapters later costs ~150 lines each with no changes to the UI or core logic.

---

## Domain language (canonical terms)

| Term | Definition | Notes |
|---|---|---|
| **Broker** | A vendor providing the user account (IBKR, Futu/MooMoo, Tiger, Longbridge). | One Protocol implementation per broker. |
| **Account** | A logical account at a broker — IBKR can expose multiple linked accounts under one login. | Each `Position` is tied to exactly one `(broker, account_id)`. |
| **Exchange** | A canonical exchange code (`HKEX`, `NYSE`, `NASDAQ`, `TSE`, `SSE`, `SZSE`, `TWSE`, `KSE`, `KOSDAQ`, `ASX`, `LSE`, `SGX`, `EBS`, `TSX`). | Derived from IB's `primaryExchange` via the suffix mapping. **HKEX, TWSE, SSE, SZSE are all distinct** — HK and Taiwan are never grouped with mainland China. |
| **Market** | Used in the UI as a synonym for "exchange's trading session". `MarketStatus` is per-`exchange`. | Avoid the bare word "market" in code paths; prefer `exchange` (the venue) or `market_session` (the time-window the venue is in). |
| **Instrument** | A canonical financial instrument identified by `(broker, native_key)`. | Different listings of the same underlying (`9988.HK` vs `BABA.US`) are distinct instruments. |
| **Position** | A row in `reqPositions`: `(broker, account_id, native_key, quantity, avg_cost)`. | Filtered to STK + CASH only in v1. Identical positions in different accounts are separate rows. |
| **CASH position** | An FX cash balance returned by IB with `secType="CASH"`. | Rendered with a 💵 indicator; no P&L in v1; never contributes to the market-status panel. |
| **Canonical symbol** | The Longbridge-style display key: `<native_symbol>.<country_suffix>`. | Used for human inspection and cross-broker dedup; **not** the primary cache key. |
| **Native key** | The broker's most stable instrument identifier (IB `conId`; Longbridge symbol; etc.). | Primary key for `name_cache`. |
| **Quote** | A live tick from the broker (`last`, `bid`, `ask`, `volume`). v1 uses only `last`. |  |
| **Snapshot** (live) | The in-memory `dict[(broker, account_id, canonical_symbol), Position]` the SSE diff is computed from. | Distinct from "equity snapshot". |
| **Equity snapshot** | A row in the `equity_snapshots` table — historical account-level net liquidation at a captured moment. | Persistent. Different concept from the live snapshot. |
| **Fill** | A row in the `fills` table — a single execution from `execDetailsEvent` / `reqExecutions`. |  |
| **FX pair / FX rate** | A currency pair `XXXUSD` and its quoted rate. | Sourced from IB primary, `open.er-api.com` fallback. CNH and CNY are distinct. |

## Decisions (locked in)

| Concern | Choice |
|---|---|
| **Stack** | FastAPI + HTMX + Alpine.js + TradingView Lightweight Charts (server-rendered, one Python process, React islands deferred) |
| **Project location** | `/Users/admin/Documents/Work/portfolio-hub/` |
| **IBKR connection** | Live trading, port 4001, via `gnzsnz/ib-gateway-docker` |
| **IB library** | `ib_async` (maintained fork of `ib_insync`) |
| **Name resolution** | Broker-native (IB's `reqContractDetails.longName`), fallback to local override map in SQLite |
| **FX → USD (primary)** | IB Gateway `reqMktData` on FX pairs (HKD.USD, JPY.USD, etc.), cached in memory. Subscribed pairs cover `SUPPORTED_FX = {HKD, JPY, KRW, TWD, CNH, AUD, GBP, EUR, SGD, CHF, CAD}`. HKD is subscribed live despite peg, for code-path uniformity. |
| **FX → USD (fallback)** | Free public API (`open.er-api.com/v6/latest/USD`, no key, daily updates) used when IB subscription fails or returns no quotes within 30s of startup. Refreshed hourly when active. Limitation: API exposes `CNY` not `CNH` — if IB's CNH subscription fails, CNH-denominated rows fall back to native-only USD column = `—` (we do not silently use CNY for CNH). |
| **FX currency strictness** | `CNY` raises a clear validation error at adapter boundary (never silently substituted with CNH). Both `CNH` (offshore, used by IB for Stock Connect A-shares) and `CNY` (onshore, mainland) are recognized as distinct. |
| **FX staleness rules** | IB-sourced rate: ⚠️ on USD columns when older than 60s **during FX market hours** (Sunday 22:00 UTC → Friday 22:00 UTC). No warning outside FX market hours. API-fallback rate: no staleness ⚠️ but persistent `📡 Fallback FX` row badge. Switch from IB to API automatically if IB rate becomes stale and API has a fresher value. |
| **Live updates** | Server-Sent Events via HTMX `hx-sse`. **Row-level deltas** (only changed rows in each push). **Tick-driven with 500ms min-interval** per client (not fixed 2s). **Hash-based change detection** over display-relevant fields. **Full snapshot on every fresh connection**; no `Last-Event-ID` complexity. **15s heartbeat** comment line on idle to survive iOS Safari's cellular timeout. Per-client snapshot state held in `dict[client_id, dict[row_key, content_hash]]`, freed on disconnect. |
| **Persistence** | SQLite (`data/portfolio.db`) for: `name_cache`, `fx_cache`, `name_overrides`, `equity_snapshots` (seed), `fills` (seed). Backed by `aiosqlite`. |
| **Equity snapshots (seed)** | Daily background captures of `(snapshot_at, snapshot_session, broker, account_id, net_liquidation_usd, gross_position_value_usd, cash_usd)`. Triggered per-market-close for each exchange held — gives sub-daily resolution to the future equity curve. Each capture records the **whole account's** net liquidation at that moment (not just that exchange's positions); `snapshot_session` labels what triggered it (`NYSE_CLOSE`, `HKEX_CLOSE`, etc.). Closed-market days produce no rows. Half-day close times come from `exchange_calendars`. **No UI in v1.** |
| **Fills log (seed)** | `IbkrAdapter` subscribes to `execDetailsEvent` at startup; each fill is inserted into `fills` (idempotent on `(broker, execution_id)` PK). End-of-day reconciliation calls `reqExecutions(filter=last_24h)` and `INSERT OR IGNORE` to catch fills missed during disconnect windows. **No UI in v1.** |
| **Future-features explicitly seeded** | Equity curve, TWR, XIRR, realized-P&L log, trade journal — all read from these two tables when their UIs are built. Tax-lot accounting, journals, alerts: **not** seeded (need UI design first). |
| **Market hours** | `exchange_calendars` library as primary source. IB `tradingHours` (from `reqContractDetails`) used as a startup sanity check; warning logged on discrepancy and IB trusted for "today only" if they disagree. Lunch breaks for HKEX/TSE/SSE/SZSE handled natively. |
| **Market status states** | Five states: `🟢 OPEN`, `🌒 EXTENDED` (US pre/post-market only — NYSE/NASDAQ/ARCA/AMEX), `🟡 LUNCH`, `🔴 CLOSED`, `⚫ HOLIDAY`. Extended hours are hand-coded (pre 04:00–09:30 ET, post 16:00–20:00 ET) since `exchange_calendars` doesn't model them. Non-US exchanges only ever show OPEN/LUNCH/CLOSED/HOLIDAY. |
| **Half-day sessions** | No special badge; the early-close time itself ("Closes at 13:00 ET in 23m") communicates it. `exchange_calendars.schedule` returns correct close times on half-days. |
| **Lunch tick handling** | Ticks accepted normally during lunch (off-exchange prints update `last_price`). Per-row subtext: "Last @ 12:23 HKT (lunch break)" when the row's exchange is in lunch. No tick gating. |
| **Time display** | Exchange-local time + relative offset ("Closes at 16:00 HKT · in 1h 23m"). No server/client TZ juggling. Optional v1.1: toggle to also show user-local time. |
| **CASH and market hours** | CASH positions (FX balances) never contribute exchanges to the market-status panel. Panel is driven by `{p.exchange for p in positions if p.asset_class == "STK"}`. |
| **Auth** | None — Tailscale handles network-level access control. **Tailscale Funnel explicitly disabled** (the app has no app-layer auth and must never be reachable outside the Tailnet). Dashboard binds to the Tailnet interface only. |
| **IBKR 2FA** | `TWOFA_DEVICE=mobile` — IBKR Mobile push, one tap per day on phone. TOTP migration deferred to later iteration. |
| **IBKR daily restart** | `RELOGIN_AFTER_TWOFA_TIMEOUT=yes`. `IbkrAdapter` registers `disconnectedEvent` handler → reconnect with exponential backoff (5s, 15s, 60s) → re-subscribe all `reqMktData` lines. UI shows `🔴 IBKR reconnecting...` badge during the gap; SSE keeps pushing last-known prices with a stale-data flag. |
| **Read-Only API mode** | `READ_ONLY_API=yes` in gateway config for v1. Prevents accidental order placement from a misconfigured adapter. Disabled when order entry is added later. |
| **Trusted IPs** | `TRUSTED_IPS=127.0.0.1,172.16.0.0/12` in gateway env so the `dashboard` container can reach `ib-gateway` over the Docker bridge network. |
| **Volumes** | Named Docker volume `ib-gateway-config` for `/root/Jts` (preserves 2FA registration). Bind mount `./data` for SQLite. Both survive container rebuilds. |
| **Restart policy** | `restart: unless-stopped` on both services. |
| **Mobile UX — column priorities** | Portrait shows 5 columns: `[🇭🇰 Name + symbol subtext] · Qty · Last · MV USD · P&L USD`. Hidden in portrait, shown landscape/desktop: avg cost, broker, account_id, exchange, native MV/P&L. Row long-press → detail card. |
| **Mobile UX — country flag** | A country/territory flag emoji appears inline at the start of the Name column for every STK row. **Hong Kong (🇭🇰) and Taiwan (🇹🇼) are always rendered separately from mainland China (🇨🇳)** — this is a hard requirement, not a default. Mapping in `app/core/symbols.py`: `HKEX→🇭🇰`, `TWSE→🇹🇼`, `SSE/SZSE→🇨🇳`, `NYSE/NASDAQ/ARCA/AMEX→🇺🇸`, `TSE/OSE→🇯🇵`, `KSE/KOSDAQ→🇰🇷`, `ASX→🇦🇺`, `LSE/IOB→🇬🇧`, `SGX→🇸🇬`, `EBS/SIX→🇨🇭`, `TSX→🇨🇦`. CASH rows show the currency flag instead (e.g., 🇭🇰 for HKD). |
| **Mobile UX — filters** | Broker chips, account chips, asset-class toggle. **No separate exchange filter chip** — the country flag in the name column conveys this dimension visually. Filter state persists to URL query string. |
| **Mobile UX — sort** | Default `market_value_usd` descending. Tappable headers cycle asc/desc/unsorted. Preference persists to `localStorage`. |
| **Mobile UX — header strip** | Sticky top bar: total exposure USD, total unrealized P&L USD (color-coded), P&L %, broker connection badges, last-update timestamp. |
| **Mobile UX — market drawer** | Collapsible. Collapsed shows per-exchange state emoji row (`🇭🇰 🟡 · 🇺🇸 🟢 · 🇯🇵 🔴`). Tap to expand into full per-exchange cards. Default: collapsed on portrait, expanded on desktop. |
| **Mobile UX — dark mode** | Auto via `prefers-color-scheme` (Pico.css). P&L colors use Pico variables (`--pico-color-green-500` / `--pico-color-red-500`) for accessible contrast in both modes. |
| **Mobile UX — gestures** | Min 44×44px touch targets. Long-press row → detail card (Alpine.js). Pull-to-refresh → reconnect SSE + force full snapshot. Swipe-left actions deferred but row markup designed to support them. |
| **Broker extensibility** | `Broker` Protocol + per-broker adapters; only IBKR adapter built day 1 |
| **Asset classes in scope** | **STK, ETF, CASH only** (ETF is returned by IB as `STK`, so the runtime enum is effectively `STK \| CASH`). OPT, FUT, BOND, FUND, CRYPTO are filtered out of `reqPositions()` results with a UI note "N non-equity/cash positions hidden". |
| **Account scope** | Multiple linked IBKR accounts. Each `(account_id, canonical_symbol)` is a distinct row (no merging across accounts — preserves cost-basis honesty for tax lots). Account ID is a `Position` field, rendered as a small pill badge (hidden on narrow mobile by default; surfaced via an "Accounts" filter in the header). |
| **Canonical symbol dialect** | Longbridge-style `<native_symbol>.<country_suffix>` — `.HK`, `.US`, `.JP`, `.SH`, `.SZ`, `.KR`, `.TW`, `.AU`, `.UK`, `.SG`, etc. Derived from IB's `primaryExchange` (fetched via `reqContractDetails`, cached) via a single mapping table in `app/core/symbols.py`. Never trust `Contract.exchange` from a raw `Position`. |
| **Dual-listed instruments** | Always shown as separate rows (e.g., `9988.HK` and `BABA.US` for Alibaba). Different ADR ratios, currencies, market hours, and tax treatment make merging actively misleading. A future "instrument group" feature could provide an aggregated view; explicitly deferred. |
| **`name_cache` key** | `(broker, native_key)` where `native_key` is the broker's most stable instrument identifier — IB's `conId` (as string) for the IBKR adapter; broker-specific equivalents for others. Survives ticker renames (e.g., FB → META, 2022-06-09). `canonical_symbol` denormalized into the row for human inspection. TTL of 30 days so corporate-action renames eventually propagate. |

---

## Architecture

```
portfolio-hub/
├── docker-compose.yml                # ib-gateway (gnzsnz) + dashboard service; TRUSTED_IPS, READ_ONLY_API=yes, TWOFA_DEVICE=mobile, RELOGIN_AFTER_TWOFA_TIMEOUT=yes, named volume for /root/Jts
├── .env.example                      # IB_USER, IB_PASS, TRADING_MODE=live, TZ, BROKERS_ENABLED=ibkr
├── pyproject.toml                    # ib_async, fastapi, uvicorn, exchange_calendars, jinja2, sse-starlette, aiosqlite, pyxirr (future)
├── app/
│   ├── main.py                       # FastAPI app, routes, SSE endpoint
│   ├── core/
│   │   ├── broker.py                 # Broker Protocol + Position/AccountSummary dataclasses
│   │   ├── registry.py               # Loads enabled adapters from .env, exposes BrokerRegistry
│   │   ├── fx.py                     # FxService: subscribes to IB FX pairs, caches last quote, converts amounts
│   │   ├── names.py                  # NameResolver: looks up cache → override map → broker SDK
│   │   ├── markets.py                # MarketHours: wraps exchange_calendars, returns status per exchange (incl. lunch)
│   │   └── symbols.py                # IB_EXCHANGE_TO_SUFFIX mapping + canonical_symbol(broker, raw) helper
│   ├── adapters/
│   │   └── ibkr.py                   # IbkrAdapter implementing Broker Protocol
│   ├── db/
│   │   ├── schema.sql                # Tables: name_cache, fx_cache, name_overrides, equity_snapshots, fills
│   │   └── store.py                  # Thin aiosqlite wrapper
│   ├── jobs/
│   │   ├── snapshot.py               # Per-market-close equity snapshot scheduler (uses exchange_calendars for next-close)
│   │   └── fills_reconcile.py        # End-of-day reqExecutions backstop for execDetailsEvent stream
│   ├── templates/
│   │   ├── base.html                 # Layout, viewport meta, Pico.css for mobile-first styling
│   │   ├── index.html                # Holdings table + market-status panel
│   │   └── partials/
│   │       ├── holdings_row.html     # One <tr>, swapped in via SSE
│   │       └── market_card.html      # One market status card
│   └── static/
│       ├── app.css                   # Light overrides on Pico.css
│       └── app.js                    # Alpine.js components; ~30 lines
└── data/                             # SQLite file lives here (gitignored)
```

### Broker Protocol (`app/core/broker.py`)

```python
class Broker(Protocol):
    name: str  # "IBKR" | "Futu" | "Tiger" | "Longbridge"

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def is_connected(self) -> bool: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_account_summary(self) -> list[AccountSummary]: ...  # one per linked account
    async def get_company_name(self, symbol: str, exchange: str) -> str | None: ...
    # Order methods added later when needed
```

```python
@dataclass
class Position:
    broker: str                  # "IBKR" | "Futu" | "Tiger" | "Longbridge"
    account_id: str              # "U1234567" — IBKR account number; broker-specific format
    native_key: str              # IB conId (str), Longbridge symbol, etc. — broker-stable PK for name_cache
    canonical_symbol: str        # "700.HK", "AAPL.US", "7203.JP" (Longbridge dialect)
    native_symbol: str           # "700"
    exchange: str                # "HKEX" | "NASDAQ" | "TSE" — canonical exchange names
    currency: str                # "HKD" | "USD" | "JPY"
    name_en: str                 # "Tencent Holdings Ltd"
    asset_class: str             # "STK" | "CASH" (ETF arrives as STK from IB)
    quantity: float
    avg_cost: float              # native currency
    last_price: float            # native currency
    market_value_native: float
    market_value_usd: float
    unrealized_pnl_native: float
    unrealized_pnl_usd: float

# AccountSummary is per (broker, account_id) — get_account_summary() returns a list
@dataclass
class AccountSummary:
    broker: str
    account_id: str
    base_currency: str           # IB lets each account set its own base currency
    net_liquidation_usd: float
    cash_usd: float
    buying_power_usd: float
```

The rest of the app (UI templates, FX service, market hours, SSE broadcaster) operates **only** on the normalized `Position` type. Adapters are the sole place where vendor SDKs are touched.

### Market hours panel

For every distinct `exchange` in the current portfolio (from STK positions only — CASH excluded), render a card showing:

- **State badge**: `🟢 OPEN` / `🌒 EXTENDED` (US only) / `🟡 LUNCH` / `🔴 CLOSED` / `⚫ HOLIDAY`
- **Time line in exchange-local time with relative offset**: `Closes at 16:00 HKT · in 1h 23m` or `Reopens at 13:00 HKT · in 30m`
- **Extended-hours subtext for US** when state is `EXTENDED`: "Pre-market until 09:30 ET" or "After-hours until 20:00 ET"

`exchange_calendars` handles HKEX (12:00–13:00 lunch), TSE (11:30–12:30 lunch), SSE/SZSE (11:30–13:00 lunch), half-days, and per-exchange holidays natively. Extended hours are hand-rolled in `markets.py` (pre 04:00–09:30 ET, post 16:00–20:00 ET) for `NYSE`, `NASDAQ`, `ARCA`, `AMEX`. On startup, `IbkrAdapter` cross-checks `exchange_calendars` against each exchange's IB `tradingHours` for the current trading day; mismatches log a warning and IB wins for that day only.

```python
@dataclass
class MarketStatus:
    exchange: str                  # "HKEX" | "NYSE" | "TSE" | ...
    state: Literal["OPEN", "EXTENDED", "LUNCH", "CLOSED", "HOLIDAY"]
    extended_session: Literal["PRE", "POST"] | None  # only when state == "EXTENDED"
    next_transition_local: str     # "16:00 HKT"
    next_transition_iso: str       # "2026-05-13T08:00:00+00:00" — for JS countdown
    next_transition_label: str     # "Closes" | "Opens" | "Reopens" | "Pre-market ends" | ...
```

### CASH position handling

IBKR returns FX cash balances (e.g., 50,000 HKD sitting idle in the account) as `Position` rows with `secType="CASH"`. These have important differences from STK positions:

- **No `reqContractDetails` call needed** — no company name to resolve. The "name" displayed is the currency code itself (e.g., "Hong Kong Dollar"; a small static `CURRENCY_NAMES` map handles this).
- **No `reqMktData` subscription needed for the instrument** — the instrument *is* the currency. The USD value comes directly from the FX service (which is already subscribing to HKD.USD for other reasons).
- **No P&L in the traditional sense** — `unrealized_pnl_native = 0` always; `unrealized_pnl_usd` is `0` for v1 (computing it correctly requires FX cost basis, which IB does not track reliably). Display as `—` in the P&L column rather than `$0.00`.
- **Rendered in a distinct visual section or with a `💵` badge** to avoid being mistaken for a stock position.
- **No market hours card** — CASH positions don't pin an exchange in the market-status panel.

### Update flow

1. On startup, `IbkrAdapter` connects to IB Gateway, calls `reqPositions()`.
2. For each unique contract: check `name_cache` table → if miss, call `reqContractDetails()`, store `longName` in cache.
3. For each unique non-USD currency: `FxService` subscribes via `reqMktData()` on the FX pair (e.g., `Forex("HKDUSD")`).
4. IB streams ticks; an asyncio task aggregates them into a `dict[canonical_symbol, Position]` snapshot.
5. SSE endpoint `/stream/holdings` emits a recomputed snapshot every ~2s while clients are connected.
6. HTMX swaps `<tbody>` rows by `id` on each event — only changed rows re-paint.

---

## Files to create

All new — `portfolio-hub/` is a fresh directory. No existing files modified.

### Critical files (highest design density)

| File | Why |
|---|---|
| `app/core/broker.py` | The Protocol is the keystone. Get this right and the rest of the codebase stays decoupled. |
| `app/core/markets.py` | Lunch breaks + timezone correctness is the trickiest UX detail. |
| `app/core/fx.py` | FX caching + IB subscription lifecycle is the trickiest async detail. |
| `app/adapters/ibkr.py` | The only concrete adapter day 1 — sets the pattern future adapters copy. |
| `app/main.py` + `templates/index.html` | SSE wiring, mobile layout, market-status panel. |

### Reuse from existing projects

- **`/Users/admin/Documents/Work/StocksIBKR/ib_connection_manager.py`** — connection state machine + asyncio loop pattern. Borrow the structure, simplify (we don't need stop-loss logic).
- **`/Users/admin/Documents/Work/StocksIBKR/position_tracker.py`** — `reqPositionsAsync()` usage pattern.
- **`/Users/admin/Documents/Work/daily_stock_analysis/requirements.txt`** — confirms `exchange_calendars` as the right choice; check that project for any helper functions worth borrowing for multi-market hour logic.
- **`/Users/admin/Documents/Work/ibkr_order_panel/ib_connector.py`** — simple synchronous reference for understanding `ib.positions()` shape.

---

## Build sequence

1. **Scaffold** — `pyproject.toml`, `docker-compose.yml` (ib-gateway + dashboard, all env vars from the Decisions table), `.env.example`, FastAPI hello-world on `:8080`. Verify Tailscale reachability from phone.
2. **Broker Protocol + dataclasses** — `app/core/broker.py`, `app/core/symbols.py` (incl. `IB_EXCHANGE_TO_SUFFIX`, `EXCHANGE_TO_FLAG` mappings; **HKEX → 🇭🇰, TWSE → 🇹🇼, SSE/SZSE → 🇨🇳 strictly separate**). Pure types, no I/O.
3. **SQLite layer** — `app/db/schema.sql` (all 5 tables: `name_cache`, `fx_cache`, `name_overrides`, `equity_snapshots`, `fills`), `app/db/store.py` (aiosqlite wrapper).
4. **IBKR adapter v1 — bare positions** — `app/adapters/ibkr.py`. Connect to gateway with disconnectedEvent → backoff reconnect handler. Implement `get_positions()` filtered to STK + CASH only. Capture `account_id`, `native_key=str(conId)`. Leave `name_en`/USD fields blank for now.
5. **Name resolver** — `app/core/names.py`. `reqContractDetails` → cache by `(broker, native_key)`, denormalize `canonical_symbol` + `primaryExchange`. Verify "700" (SEHK) → "TENCENT HOLDINGS LTD" and canonical_symbol = `"700.HK"`.
6. **FX service** — `app/core/fx.py`. IB subscriptions for `SUPPORTED_FX` currencies seen in current positions; `open.er-api.com` fallback wired with `📡 Fallback FX` flag propagation; staleness logic (60s during FX hours; weekend-aware).
7. **Holdings page, static fetch** — `templates/base.html` + `templates/index.html` + `app/static/app.css`. 5-column portrait layout, country flag inline with name, sticky header strip (totals + broker badges), sort by `market_value_usd` desc. No live updates yet — page-load fetch only. Verify renders correctly on phone in portrait *and* landscape.
8. **Market hours panel** — `app/core/markets.py` (5 states incl. extended for US, IB `tradingHours` sanity check on startup) + `templates/partials/market_card.html` + collapsed-drawer markup. Verify HKEX lunch transition, NYSE extended-hours display, half-day correctness, holiday handling.
9. **SSE live updates** — `/stream/holdings` endpoint, row-level deltas with hash-based change detection, 500ms tick-driven, 15s heartbeat. HTMX `hx-sse` on `<tbody>` and totals strip. Per-client snapshot state. Verify full snapshot on reconnect (background phone, then foreground).
10. **Seed jobs** — `app/jobs/snapshot.py` (per-market-close equity capture using `exchange_calendars` next-close lookup) + `app/jobs/fills_reconcile.py` (end-of-day `reqExecutions` backstop). `execDetailsEvent` subscription wired into adapter at startup with `INSERT OR IGNORE` upserts to `fills`.
11. **Filters + Alpine.js polish** — broker/account/asset-class filter chips, URL query-string persistence, sort cycling, row long-press detail card, drawer expand/collapse, `localStorage` for sort + drawer state.
12. **Operational polish** — disconnected-gateway badge with last-known-prices flagged stale, FX fallback banner, "no positions" empty state, structured logs.

Adapters for Futu / MooMoo / Tiger / Longbridge are **explicitly out of scope for v1** — the Protocol exists so they can be added later as ~150-line files each with no changes to UI, market-hours, FX, or seed-job code.

---

## Verification

After build:
1. `docker compose up -d` — IB Gateway + dashboard come up.
2. `curl http://localhost:8080/healthz` returns `{"ibkr": "connected"}`.
3. Open `http://<laptop-hostname>:8080` from phone via Tailscale — page loads under 1s, table is readable without horizontal scroll.
4. Open the same URL on desktop — layout adapts (Pico.css responsive grid).
5. Pick one HK position (e.g., 700) — verify `name_en = "TENCENT HOLDINGS LTD"`, `market_value_usd` ≈ `quantity × last_price × HKD-USD rate`, P&L matches IB TWS.
6. During HK trading hours, verify market card shows "🟢 Open · Closes in Xh Ym (lunch break 12:00–13:00 HKT)".
7. At 12:00 HKT, verify the card transitions to "🟡 Lunch break · Reopens at 13:00 HKT".
8. Manually edit a row in `name_overrides` table (`UPDATE name_overrides SET name_en='Tencent' WHERE canonical_symbol='700.HK'`) — refresh, verify override takes effect.
9. Stop IB Gateway — verify the page shows a clear "🔴 IBKR disconnected" badge without crashing.
10. Restart everything — verify name cache hits avoid re-calling `reqContractDetails` (check logs).

---

## Deferred (explicitly out of scope for v1)

- **Futu / Tiger / Longbridge adapters** — Broker Protocol is ready; adapters added later as ~150-line files each, no UI changes needed.
- **Price charts** — TradingView Lightweight Charts library chosen; not wired up.
- **Equity curve over time UI** — `equity_snapshots` table is being populated from day 1, so the curve will have history when the UI lands.
- **TWR / XIRR returns** — `pyxirr` is the chosen library; needs `equity_snapshots` + deposits/withdrawals (Flex query later).
- **Trading journal / thesis journal** — UI design needed; SQLite schema can extend with `journal_entries` table.
- **Order entry** — Broker Protocol method left undefined until needed. Read-Only API mode in gateway will be disabled at that point.
- **Per-broker authentication flows** beyond IB.
- **App-layer authentication** — Tailnet is the boundary; Tailscale Funnel deliberately disabled.
- **Tax-lot accounting** — needs explicit FIFO/LIFO/SpecificID choice; deferred.
- **Alerts / price targets / watchlists** — out of scope; needs UI design.
- **OPT / FUT / BOND / FUND / CRYPTO asset classes** — filtered from `reqPositions` with a UI note. Add when actually needed.
- **CNY (onshore renminbi)** — explicitly errors at v1; only CNH supported (IB's actual currency for Stock Connect A-shares).
- **Optional user-local time toggle** on market hours panel — exchange-local is the v1 display.
- **Swipe-action shortcuts** on rows — markup designed to support, not implementing.
- **Tailscale Funnel / public exposure** — never. App has no auth.
