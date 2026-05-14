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
