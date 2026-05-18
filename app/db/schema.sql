-- Slice 2 schema.
--
-- name_cache: caches the English company name resolved via reqContractDetails
-- so we don't re-query on every restart. Keyed by (broker, native_key) — for
-- IBKR, native_key is the conId-as-string. This survives ticker renames
-- (e.g., FB → META, 2022-06-09) because conId is stable.

CREATE TABLE IF NOT EXISTS name_cache (
    broker            TEXT      NOT NULL,
    native_key        TEXT      NOT NULL,
    canonical_symbol  TEXT      NOT NULL,
    name_en           TEXT      NOT NULL,
    -- Slice 3 follow-up: IB's price-unit divisor (1 for most, 100 for LSE
    -- pence-quoted equities like IQE). Captured at contract-details lookup
    -- so we don't have to re-fetch on every restart. Default 1 keeps older
    -- cached rows from breaking the math.
    price_magnifier   INTEGER   NOT NULL DEFAULT 1,
    updated_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (broker, native_key)
);

-- Slice 3: fx_cache holds the latest usable FX rate per pair.
--
-- Survives restart: on boot, FxService loads this table so the UI shows
-- last-known USD values immediately, before the IB FX subscription warms up.
-- `source` distinguishes IB-streamed quotes from API-fallback values so the
-- 📡 badge correctly reappears on rows that were on the fallback when we shut
-- down.

CREATE TABLE IF NOT EXISTS fx_cache (
    pair              TEXT      NOT NULL,
    rate              REAL      NOT NULL,
    source            TEXT      NOT NULL,  -- "IB" | "API_FALLBACK"
    quoted_at         TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL,
    PRIMARY KEY (pair)
);

-- Slice 11: fills records every order execution captured from the broker's
-- execDetailsEvent stream (live) and the EOD reconcile job (backstop). Feeds
-- future realized-P&L, trade journal, and XIRR UIs in v1.1+.
--
-- PK (broker, execution_id): IB's execId is globally unique per broker, so
-- INSERT OR IGNORE makes both the live stream and the daily reconcile safely
-- re-runnable without duplicates. fx_rate_at_fill is NULL for USD-denominated
-- fills since there's nothing to convert.

CREATE TABLE IF NOT EXISTS fills (
    broker            TEXT      NOT NULL,
    account_id        TEXT      NOT NULL,
    execution_id      TEXT      NOT NULL,
    canonical_symbol  TEXT      NOT NULL,
    native_key        TEXT      NOT NULL,
    asset_class       TEXT      NOT NULL,
    side              TEXT      NOT NULL,    -- "BUY" | "SELL"
    quantity          REAL      NOT NULL,
    price             REAL      NOT NULL,
    currency          TEXT      NOT NULL,
    fx_rate_at_fill   REAL,                  -- NULL for USD trades
    fees_native       REAL,
    fees_usd          REAL,
    filled_at         TIMESTAMP NOT NULL,    -- UTC
    captured_at       TIMESTAMP NOT NULL,    -- UTC, when we observed the fill
    PRIMARY KEY (broker, execution_id)
);

CREATE INDEX IF NOT EXISTS idx_fills_account_filled
    ON fills(broker, account_id, filled_at);
CREATE INDEX IF NOT EXISTS idx_fills_symbol
    ON fills(canonical_symbol, filled_at);
