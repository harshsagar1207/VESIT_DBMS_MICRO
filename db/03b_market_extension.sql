-- Market-universe extension (run after 01-03 schema files, before 04_seed.sql).
-- Safe to re-run. Adds asset classes (equity, index, index future, mutual fund, bond, IPO) to
-- `instrument`, plus per-class detail tables and index membership.
SET search_path = public;

ALTER TABLE instrument ADD COLUMN IF NOT EXISTS asset_class text NOT NULL DEFAULT 'EQUITY';
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS sector text;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS tradable boolean NOT NULL DEFAULT true;
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS underlying_id bigint;      -- e.g. GIFT Nifty -> Nifty 50
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS feed_symbol text;          -- provider-side ticker for live prices
ALTER TABLE instrument ADD COLUMN IF NOT EXISTS data_source text NOT NULL DEFAULT 'seed-synthetic';

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'instrument_asset_class_chk') THEN
    ALTER TABLE instrument ADD CONSTRAINT instrument_asset_class_chk
      CHECK (asset_class IN ('EQUITY','INDEX','INDEX_FUTURE','MUTUAL_FUND','BOND','IPO'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS instrument_asset_class_idx ON instrument (asset_class);

-- index constituents (Nifty 50, Nifty Bank, Sensex)
CREATE TABLE IF NOT EXISTS index_membership (
  index_id   bigint NOT NULL,
  member_id  bigint NOT NULL,
  weight_pct numeric(6,3),
  as_of      date NOT NULL DEFAULT date '2026-10-24',
  PRIMARY KEY (index_id, member_id)
);

CREATE TABLE IF NOT EXISTS mf_scheme (
  instrument_id bigint PRIMARY KEY,
  amc           text NOT NULL,
  category      text NOT NULL,
  scheme_group  text NOT NULL CHECK (scheme_group IN ('Equity','Debt','Index')),
  expense_ratio numeric(5,2),
  min_investment numeric(12,2)
);

CREATE TABLE IF NOT EXISTS bond (
  instrument_id bigint PRIMARY KEY,
  issuer        text NOT NULL,
  bond_type     text NOT NULL CHECK (bond_type IN ('GSEC','SDL','TBILL','CORPORATE')),
  coupon_pct    numeric(6,3) NOT NULL,
  face_value    numeric(12,2) NOT NULL,
  maturity_date date NOT NULL,
  coupons_per_year smallint NOT NULL,
  rating        text
);

CREATE TABLE IF NOT EXISTS ipo (
  instrument_id bigint PRIMARY KEY,
  company       text NOT NULL,
  status        text NOT NULL CHECK (status IN ('UPCOMING','OPEN','CLOSED','LISTED')),
  issue_open    date,
  issue_close   date,
  allotment_date date,
  price_band_low  numeric(12,2),
  price_band_high numeric(12,2),
  issue_price   numeric(12,2),
  lot_size      integer NOT NULL,
  subscription_x numeric(8,2)
);

-- live price feed bookkeeping (populated by the price-updater service; see price-feed discussion)
CREATE TABLE IF NOT EXISTS price_feed_state (
  source          text PRIMARY KEY,
  last_success_at timestamptz,
  last_error_at   timestamptz,
  last_error      text
);
