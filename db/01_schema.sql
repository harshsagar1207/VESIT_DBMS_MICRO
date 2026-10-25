-- Stock Portfolio Management System v1 -- schema (PostgreSQL 16)
-- Sections refer to Technical Report v1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------- roles
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_role') THEN
    CREATE ROLE app_role LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- ---------------------------------------------------------------- identity
CREATE TABLE app_user (
  user_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email           text NOT NULL,
  password_hash   text NOT NULL,
  status          text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','LOCKED')),
  failed_attempts int  NOT NULL DEFAULT 0,
  locked_until    timestamptz,
  mfa_secret_enc  bytea,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX app_user_email_uq ON app_user (lower(email));

CREATE TABLE role (
  role_id smallint PRIMARY KEY,
  name    text UNIQUE NOT NULL CHECK (name IN ('USER','ADMIN','AUDITOR'))
);
INSERT INTO role VALUES (1,'USER'),(2,'ADMIN'),(3,'AUDITOR');

CREATE TABLE user_role (
  user_id bigint NOT NULL REFERENCES app_user,
  role_id smallint NOT NULL REFERENCES role,
  PRIMARY KEY (user_id, role_id)
);

CREATE TABLE auth_session (
  session_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id          uuid NOT NULL DEFAULT gen_random_uuid(),
  user_id            bigint NOT NULL REFERENCES app_user,
  refresh_token_hash text NOT NULL UNIQUE,
  expires_at         timestamptz NOT NULL,
  revoked_at         timestamptz,
  ip                 inet,
  user_agent         text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_session_user_ix ON auth_session (user_id);
CREATE INDEX auth_session_family_ix ON auth_session (family_id);

-- ---------------------------------------------------------------- accounts
CREATE TABLE portfolio (
  portfolio_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       bigint NOT NULL REFERENCES app_user,
  name          text NOT NULL,
  base_currency char(3) NOT NULL DEFAULT 'USD',
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, name)
);

CREATE TABLE cash_account (
  account_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portfolio_id bigint NOT NULL REFERENCES portfolio,
  currency     char(3) NOT NULL,
  balance      numeric(18,4) NOT NULL DEFAULT 0,
  reserved     numeric(18,4) NOT NULL DEFAULT 0,
  version      bigint NOT NULL DEFAULT 0,
  UNIQUE (portfolio_id, currency),
  CHECK (reserved >= 0),
  CHECK (balance - reserved >= 0)                       -- I1
);

CREATE TABLE cash_ledger (
  entry_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id    bigint NOT NULL REFERENCES cash_account,
  entry_type    text NOT NULL CHECK (entry_type IN
                 ('DEPOSIT','WITHDRAW','BUY_COST','SELL_PROCEEDS','FEE','DIVIDEND','REVERSAL')),
  amount        numeric(18,4) NOT NULL,                  -- signed
  ref_type      text,
  ref_id        bigint,
  balance_after numeric(18,4) NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cash_ledger_acct_ix ON cash_ledger (account_id, created_at);

-- ---------------------------------------------------------------- market
CREATE TABLE instrument (
  instrument_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  symbol        text NOT NULL,
  exchange      text NOT NULL,
  currency      char(3) NOT NULL DEFAULT 'USD',
  lot_size      numeric(18,6) NOT NULL DEFAULT 1,
  tick_size     numeric(18,6) NOT NULL DEFAULT 0.01,
  status        text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','DELISTED')),
  UNIQUE (symbol, exchange)
);

CREATE TABLE price_snapshot (
  instrument_id bigint NOT NULL REFERENCES instrument,
  ts            timestamptz NOT NULL,
  open numeric(18,6) NOT NULL, high numeric(18,6) NOT NULL,
  low  numeric(18,6) NOT NULL, close numeric(18,6) NOT NULL,
  volume bigint NOT NULL DEFAULT 0,
  source text NOT NULL DEFAULT 'csv',
  loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (instrument_id, ts),
  CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
  CHECK (low <= high)
) PARTITION BY RANGE (ts);
CREATE TABLE price_snapshot_default PARTITION OF price_snapshot DEFAULT;

CREATE TABLE latest_price (
  instrument_id bigint PRIMARY KEY REFERENCES instrument,
  price         numeric(18,6) NOT NULL CHECK (price > 0),
  ts            timestamptz NOT NULL
);

CREATE TABLE fee_schedule (
  exchange   text PRIMARY KEY,
  pct        numeric(9,6) NOT NULL DEFAULT 0.001,
  min_fee    numeric(18,4) NOT NULL DEFAULT 1.00
);

-- ---------------------------------------------------------------- trading
CREATE TABLE orders (
  order_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portfolio_id    bigint NOT NULL REFERENCES portfolio,
  instrument_id   bigint NOT NULL REFERENCES instrument,
  side            text NOT NULL CHECK (side IN ('BUY','SELL')),
  type            text NOT NULL CHECK (type IN ('MARKET','LIMIT')),
  qty             numeric(18,6) NOT NULL CHECK (qty > 0),
  limit_price     numeric(18,6),
  status          text NOT NULL DEFAULT 'NEW' CHECK (status IN
                    ('NEW','ACCEPTED','PARTIALLY_FILLED','FILLED','CANCELLED','REJECTED','EXPIRED')),
  filled_qty      numeric(18,6) NOT NULL DEFAULT 0,
  reserved_cash   numeric(18,4) NOT NULL DEFAULT 0,      -- cash still held for this order
  client_order_id uuid NOT NULL,
  version         bigint NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (portfolio_id, client_order_id),
  CHECK (filled_qty >= 0 AND filled_qty <= qty),          -- I4 (first half)
  CHECK ((type = 'LIMIT') = (limit_price IS NOT NULL))
);
CREATE INDEX orders_open_ix ON orders (portfolio_id, created_at DESC)
  WHERE status IN ('ACCEPTED','PARTIALLY_FILLED');
CREATE INDEX orders_match_ix ON orders (instrument_id, side, limit_price, created_at)
  WHERE status IN ('ACCEPTED','PARTIALLY_FILLED');

CREATE TABLE trade (
  trade_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id    bigint NOT NULL REFERENCES orders,
  qty         numeric(18,6) NOT NULL CHECK (qty > 0),
  price       numeric(18,6) NOT NULL CHECK (price > 0),
  fee         numeric(18,4) NOT NULL CHECK (fee >= 0),
  executed_at timestamptz NOT NULL DEFAULT now(),
  settle_date date NOT NULL DEFAULT current_date
);
CREATE INDEX trade_order_ix ON trade (order_id);

-- ---------------------------------------------------------------- holdings
CREATE TABLE position (
  portfolio_id  bigint NOT NULL REFERENCES portfolio,
  instrument_id bigint NOT NULL REFERENCES instrument,
  qty           numeric(18,6) NOT NULL DEFAULT 0,
  reserved_qty  numeric(18,6) NOT NULL DEFAULT 0,
  avg_cost      numeric(18,6) NOT NULL DEFAULT 0,
  version       bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (portfolio_id, instrument_id),
  CHECK (qty >= 0),                                       -- I2
  CHECK (reserved_qty >= 0 AND reserved_qty <= qty)       -- I2
);

CREATE TABLE tax_lot (
  lot_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  portfolio_id  bigint NOT NULL,
  instrument_id bigint NOT NULL,
  trade_id      bigint REFERENCES trade,
  open_qty      numeric(18,6) NOT NULL CHECK (open_qty > 0),
  remaining_qty numeric(18,6) NOT NULL CHECK (remaining_qty >= 0 AND remaining_qty <= open_qty),
  cost_per_share numeric(18,6) NOT NULL CHECK (cost_per_share >= 0),
  opened_at     timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (portfolio_id, instrument_id) REFERENCES position
);
CREATE INDEX tax_lot_fifo_ix ON tax_lot (portfolio_id, instrument_id, opened_at, lot_id)
  WHERE remaining_qty > 0;

CREATE TABLE lot_disposal (
  disposal_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  lot_id       bigint NOT NULL REFERENCES tax_lot,
  trade_id     bigint NOT NULL REFERENCES trade,
  qty          numeric(18,6) NOT NULL CHECK (qty > 0),
  proceeds     numeric(18,4) NOT NULL,
  cost         numeric(18,4) NOT NULL,
  fee_alloc    numeric(18,4) NOT NULL DEFAULT 0,
  realized_pnl numeric(18,4) NOT NULL
);

-- ---------------------------------------------------------------- events
CREATE TABLE corporate_action (
  ca_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  instrument_id bigint NOT NULL REFERENCES instrument,
  kind          text NOT NULL CHECK (kind IN ('SPLIT','CASH_DIVIDEND')),
  ratio_num     numeric(18,6),          -- split: new shares per ratio_den old
  ratio_den     numeric(18,6),
  amount_per_share numeric(18,6),       -- dividend
  record_date   date NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ca_application (
  ca_id        bigint NOT NULL REFERENCES corporate_action,
  portfolio_id bigint NOT NULL REFERENCES portfolio,
  applied_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ca_id, portfolio_id)
);

-- ---------------------------------------------------------------- control
CREATE TABLE idempotency_key (
  key           text NOT NULL,
  user_id       bigint NOT NULL REFERENCES app_user,
  request_hash  text NOT NULL,
  response      jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);

CREATE TABLE audit_log (
  audit_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor      bigint,
  action     text NOT NULL,
  entity     text NOT NULL,
  entity_id  bigint,
  before     jsonb,
  after      jsonb,
  request_id text,
  ts         timestamptz NOT NULL DEFAULT clock_timestamp(),
  prev_hash  text,
  hash       text
);
CREATE INDEX audit_entity_ix ON audit_log (entity, entity_id, ts);
