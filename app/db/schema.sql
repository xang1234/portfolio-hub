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
