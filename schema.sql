-- Run once: psql "$DATABASE_URL" -f schema.sql

CREATE TABLE IF NOT EXISTS transactions (
    id          SERIAL PRIMARY KEY,
    ref         TEXT UNIQUE,            -- REF# from the statement, dedup key
    date        DATE NOT NULL,
    amount      NUMERIC(12,2) NOT NULL, -- negative = money out, positive = money in
    name        TEXT NOT NULL,          -- cleaned merchant/payee name
    currency    TEXT NOT NULL DEFAULT 'USD',
    category    TEXT NOT NULL,
    account     TEXT NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cache: raw merchant string -> LLM-assigned clean name + category.
-- Once a merchant is seen once, every future statement reuses this for free.
CREATE TABLE IF NOT EXISTS merchant_cache (
    raw_key     TEXT PRIMARY KEY,       -- normalized raw merchant string
    clean_name  TEXT NOT NULL,
    category    TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
