-- Helpers, triggers, audit chain, row-level security, privileges.
-- Run after 01_schema.sql, as the table owner (superuser in dev).

SET search_path = public;

-- ---------------------------------------------------------------- db roles
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'worker_role') THEN
    CREATE ROLE worker_role LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'admin_role') THEN
    CREATE ROLE admin_role LOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END $$;

-- ---------------------------------------------------------------- helpers
CREATE FUNCTION app_uid() RETURNS bigint LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::bigint $$;

CREATE FUNCTION app_claim_role() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.role', true), '') $$;

-- Round half to even (report 7.2 "Rounding"); numeric round() is half-away-from-zero.
CREATE FUNCTION round_he(x numeric, d int) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s numeric := x * power(10::numeric, d);
        f numeric := floor(s);
        diff numeric := s - f;
BEGIN
  IF diff < 0.5 THEN RETURN f / power(10::numeric, d);
  ELSIF diff > 0.5 THEN RETURN (f + 1) / power(10::numeric, d);
  ELSIF mod(f, 2) = 0 THEN RETURN f / power(10::numeric, d);
  ELSE RETURN (f + 1) / power(10::numeric, d);
  END IF;
END $$;

-- ---------------------------------------------------------------- append-only guards
CREATE FUNCTION forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
    USING ERRCODE = '42501';
END $$;

CREATE TRIGGER cash_ledger_append_only BEFORE UPDATE OR DELETE ON cash_ledger
  FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER cash_ledger_no_truncate BEFORE TRUNCATE ON cash_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION forbid_change();
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_change();

-- ---------------------------------------------------------------- audit hash chain (9.1)
CREATE FUNCTION audit_hash(r audit_log) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT encode(digest(
    concat_ws('|', coalesce(r.prev_hash,''), r.audit_id::text, coalesce(r.actor::text,''),
              r.action, r.entity, coalesce(r.entity_id::text,''),
              coalesce(r.before::text,''), coalesce(r.after::text,''),
              coalesce(r.request_id,''), extract(epoch FROM r.ts)::text),
    'sha256'), 'hex')
$$;

CREATE FUNCTION audit_before_insert() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_prev text;
BEGIN
  PERFORM pg_advisory_xact_lock(727001);               -- serialize the chain; no-op if already held
  SELECT hash INTO v_prev FROM audit_log ORDER BY audit_id DESC LIMIT 1;
  NEW.actor      := COALESCE(NEW.actor, app_uid());
  NEW.request_id := COALESCE(NEW.request_id, current_setting('app.request_id', true));
  NEW.ts         := clock_timestamp();
  NEW.prev_hash  := v_prev;
  NEW.hash       := audit_hash(NEW);
  RETURN NEW;
END $$;
CREATE TRIGGER audit_chain BEFORE INSERT ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_before_insert();

-- The only way to write an audit row. It takes the chain lock BEFORE the identity value
-- is drawn, so audit_id order equals chain order.
CREATE FUNCTION audit_write(p_action text, p_entity text, p_entity_id bigint,
                            p_before jsonb, p_after jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(727001);
  INSERT INTO audit_log (action, entity, entity_id, before, after)
  VALUES (p_action, p_entity, p_entity_id, p_before, p_after);
END $$;

-- Returns the audit_id of the first broken link, or NULL if the chain is intact.
CREATE FUNCTION verify_audit_chain() RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE r audit_log; v_prev text := NULL;
BEGIN
  FOR r IN SELECT * FROM audit_log ORDER BY audit_id LOOP
    IF r.prev_hash IS DISTINCT FROM v_prev OR r.hash <> audit_hash(r) THEN
      RETURN r.audit_id;
    END IF;
    v_prev := r.hash;
  END LOOP;
  RETURN NULL;
END $$;

-- ---------------------------------------------------------------- order state machine (7.1)
CREATE FUNCTION orders_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status NOT IN ('NEW','ACCEPTED') THEN
      RAISE EXCEPTION 'new order must start NEW or ACCEPTED, not %', NEW.status USING ERRCODE='PF003';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.status = OLD.status THEN
    IF NEW.status NOT IN ('ACCEPTED','PARTIALLY_FILLED') AND
       (NEW.filled_qty <> OLD.filled_qty OR NEW.reserved_cash <> OLD.reserved_cash) THEN
      RAISE EXCEPTION 'order % is final (%)', OLD.order_id, OLD.status USING ERRCODE='PF003';
    END IF;
    RETURN NEW;
  END IF;
  IF NOT ( (OLD.status = 'NEW'              AND NEW.status IN ('ACCEPTED','REJECTED'))
        OR (OLD.status = 'ACCEPTED'         AND NEW.status IN ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED'))
        OR (OLD.status = 'PARTIALLY_FILLED' AND NEW.status IN ('PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED')) )
  THEN
    RAISE EXCEPTION 'illegal order transition % -> %', OLD.status, NEW.status USING ERRCODE='PF003';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER orders_state BEFORE INSERT OR UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION orders_state_guard();

-- ---------------------------------------------------------------- row-level security (6.2)
ALTER TABLE portfolio     ENABLE ROW LEVEL SECURITY;
ALTER TABLE cash_account  ENABLE ROW LEVEL SECURITY;
ALTER TABLE cash_ledger   ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders        ENABLE ROW LEVEL SECURITY;
ALTER TABLE trade         ENABLE ROW LEVEL SECURITY;
ALTER TABLE position      ENABLE ROW LEVEL SECURITY;
ALTER TABLE tax_lot       ENABLE ROW LEVEL SECURITY;
ALTER TABLE lot_disposal  ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_key ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log     ENABLE ROW LEVEL SECURITY;

CREATE POLICY owner_only ON portfolio
  USING (user_id = app_uid()) WITH CHECK (user_id = app_uid());
CREATE POLICY owner_only ON cash_account
  USING (portfolio_id IN (SELECT portfolio_id FROM portfolio));
CREATE POLICY owner_only ON cash_ledger
  USING (account_id IN (SELECT account_id FROM cash_account));
CREATE POLICY owner_only ON orders
  USING (portfolio_id IN (SELECT portfolio_id FROM portfolio));
CREATE POLICY owner_only ON trade
  USING (order_id IN (SELECT order_id FROM orders));
CREATE POLICY owner_only ON position
  USING (portfolio_id IN (SELECT portfolio_id FROM portfolio));
CREATE POLICY owner_only ON tax_lot
  USING (portfolio_id IN (SELECT portfolio_id FROM portfolio));
CREATE POLICY owner_only ON lot_disposal
  USING (lot_id IN (SELECT lot_id FROM tax_lot));
CREATE POLICY owner_only ON idempotency_key
  USING (user_id = app_uid()) WITH CHECK (user_id = app_uid());
CREATE POLICY own_or_auditor ON audit_log FOR SELECT
  USING (actor = app_uid() OR app_claim_role() = 'AUDITOR');

-- ---------------------------------------------------------------- privileges
-- app_role may READ business tables (through RLS) but has no write access to money
-- tables: every money or share movement goes through a stored function.
GRANT USAGE ON SCHEMA public TO app_role, worker_role, admin_role;

GRANT SELECT ON portfolio, cash_account, cash_ledger, orders, trade, position,
                tax_lot, lot_disposal, instrument, price_snapshot, latest_price,
                fee_schedule, corporate_action, ca_application, role, user_role
      TO app_role;
GRANT SELECT ON audit_log TO app_role;                      -- no UPDATE/DELETE/INSERT
GRANT INSERT ON portfolio TO app_role;                      -- WITH CHECK ties it to the caller
GRANT SELECT, INSERT, UPDATE ON idempotency_key TO app_role;
-- Authentication tables are touched only by the API's auth service.
GRANT SELECT, INSERT, UPDATE ON app_user, auth_session TO app_role;

GRANT SELECT ON instrument, latest_price, fee_schedule TO worker_role;
GRANT SELECT ON instrument, price_snapshot, latest_price, corporate_action,
                ca_application, app_user TO admin_role;
GRANT SELECT, INSERT, UPDATE ON app_user TO admin_role;
GRANT INSERT, UPDATE ON instrument, fee_schedule, corporate_action TO admin_role;
