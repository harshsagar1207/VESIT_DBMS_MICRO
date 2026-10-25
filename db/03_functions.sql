-- Business rules as stored functions (report sections 5, 7, 10.3).
--
-- Global lock order everywhere: orders -> cash_account -> position -> tax_lot.
-- Error codes: PF001 insufficient funds, PF002 insufficient shares, PF003 order state,
--              PF004 not found / not yours, PF005 validation, PF006 permission.
--
-- Functions are SECURITY DEFINER with a pinned search_path and check ownership against
-- app.user_id explicitly (the definer bypasses RLS). app_role has no table write access
-- on money tables, so these functions are the only way in.

SET search_path = public;

-- ---------------------------------------------------------------- pricing helpers
CREATE FUNCTION instrument_currency(p_i bigint) RETURNS char(3)
LANGUAGE sql STABLE SET search_path = public, pg_temp AS
$$ SELECT currency FROM instrument WHERE instrument_id = p_i $$;

CREATE FUNCTION fee_for(p_i bigint, p_qty numeric, p_price numeric) RETURNS numeric
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT GREATEST(fs.min_fee, round_he(fs.pct * p_qty * p_price, 4))
  FROM instrument i JOIN fee_schedule fs USING (exchange)
  WHERE i.instrument_id = p_i
$$;

-- Price used to size a reservation: the limit, or latest price + 5% buffer for market orders.
CREATE FUNCTION reservation_price(p_i bigint, p_type text, p_limit numeric) RETURNS numeric
LANGUAGE plpgsql STABLE SET search_path = public, pg_temp AS $$
DECLARE v numeric;
BEGIN
  IF p_type = 'LIMIT' THEN RETURN p_limit; END IF;
  SELECT price INTO v FROM latest_price WHERE instrument_id = p_i;
  IF v IS NULL THEN
    RAISE EXCEPTION 'no price for instrument %', p_i USING ERRCODE = 'PF005';
  END IF;
  RETURN round_he(v * 1.05, 6);
END $$;

-- ---------------------------------------------------------------- deposit / withdraw
CREATE FUNCTION deposit(p_portfolio bigint, p_currency char(3), p_amount numeric)
RETURNS numeric LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE a cash_account; v_new numeric;
BEGIN
  IF app_uid() IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = 'PF006'; END IF;
  IF p_amount IS NULL OR p_amount <= 0 THEN
    RAISE EXCEPTION 'amount must be positive' USING ERRCODE = 'PF005';
  END IF;
  PERFORM 1 FROM portfolio WHERE portfolio_id = p_portfolio AND user_id = app_uid();
  IF NOT FOUND THEN RAISE EXCEPTION 'portfolio not found' USING ERRCODE = 'PF004'; END IF;

  INSERT INTO cash_account (portfolio_id, currency) VALUES (p_portfolio, p_currency)
    ON CONFLICT (portfolio_id, currency) DO NOTHING;
  SELECT * INTO a FROM cash_account
   WHERE portfolio_id = p_portfolio AND currency = p_currency FOR UPDATE;

  v_new := a.balance + round_he(p_amount, 4);
  INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, balance_after)
    VALUES (a.account_id, 'DEPOSIT', round_he(p_amount, 4), 'DEPOSIT', v_new);
  UPDATE cash_account SET balance = v_new, version = version + 1 WHERE account_id = a.account_id;
  PERFORM audit_write('DEPOSIT', 'cash_account', a.account_id, NULL,
                      jsonb_build_object('amount', p_amount, 'balance', v_new));
  RETURN v_new;
END $$;

CREATE FUNCTION withdraw(p_portfolio bigint, p_currency char(3), p_amount numeric)
RETURNS numeric LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE a cash_account; v_new numeric; v_amt numeric := round_he(p_amount, 4);
BEGIN
  IF app_uid() IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = 'PF006'; END IF;
  IF p_amount IS NULL OR p_amount <= 0 THEN
    RAISE EXCEPTION 'amount must be positive' USING ERRCODE = 'PF005';
  END IF;
  PERFORM 1 FROM portfolio WHERE portfolio_id = p_portfolio AND user_id = app_uid();
  IF NOT FOUND THEN RAISE EXCEPTION 'portfolio not found' USING ERRCODE = 'PF004'; END IF;

  SELECT * INTO a FROM cash_account
   WHERE portfolio_id = p_portfolio AND currency = p_currency FOR UPDATE;
  IF NOT FOUND OR a.balance - a.reserved < v_amt THEN
    RAISE EXCEPTION 'insufficient funds' USING ERRCODE = 'PF001';
  END IF;

  v_new := a.balance - v_amt;
  INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, balance_after)
    VALUES (a.account_id, 'WITHDRAW', -v_amt, 'WITHDRAW', v_new);
  UPDATE cash_account SET balance = v_new, version = version + 1 WHERE account_id = a.account_id;
  PERFORM audit_write('WITHDRAW', 'cash_account', a.account_id, NULL,
                      jsonb_build_object('amount', v_amt, 'balance', v_new));
  RETURN v_new;
END $$;

-- ---------------------------------------------------------------- place_order (10.3)
-- Differences from the report's listing: duplicates are serialized with an advisory lock and
-- answered BEFORE any reservation is attempted, so a retry of an order that consumed all the
-- cash returns the stored order instead of "insufficient funds".
CREATE FUNCTION place_order(p_portfolio bigint, p_instrument bigint, p_side text, p_type text,
                            p_qty numeric, p_limit numeric, p_client_id uuid)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_id bigint; v_hold numeric := 0; v_price numeric; ins instrument;
BEGIN
  IF app_uid() IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = 'PF006'; END IF;
  PERFORM 1 FROM portfolio WHERE portfolio_id = p_portfolio AND user_id = app_uid();
  IF NOT FOUND THEN RAISE EXCEPTION 'portfolio not found' USING ERRCODE = 'PF004'; END IF;

  PERFORM pg_advisory_xact_lock(hashtextextended(p_portfolio::text || ':' || p_client_id::text, 0));
  SELECT order_id INTO v_id FROM orders
   WHERE portfolio_id = p_portfolio AND client_order_id = p_client_id;
  IF FOUND THEN RETURN v_id; END IF;

  IF p_side NOT IN ('BUY','SELL') OR p_type NOT IN ('MARKET','LIMIT') THEN
    RAISE EXCEPTION 'invalid side or type' USING ERRCODE = 'PF005';
  END IF;
  IF p_qty IS NULL OR p_qty <= 0 THEN
    RAISE EXCEPTION 'quantity must be positive' USING ERRCODE = 'PF005';
  END IF;
  SELECT * INTO ins FROM instrument WHERE instrument_id = p_instrument;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown instrument' USING ERRCODE = 'PF005'; END IF;
  IF ins.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'instrument is %', ins.status USING ERRCODE = 'PF005';
  END IF;
  IF mod(p_qty, ins.lot_size) <> 0 THEN
    RAISE EXCEPTION 'quantity violates lot size %', ins.lot_size USING ERRCODE = 'PF005';
  END IF;
  IF p_type = 'LIMIT' THEN
    IF p_limit IS NULL OR p_limit <= 0 OR mod(p_limit, ins.tick_size) <> 0 THEN
      RAISE EXCEPTION 'limit price violates tick size %', ins.tick_size USING ERRCODE = 'PF005';
    END IF;
  ELSIF p_limit IS NOT NULL THEN
    RAISE EXCEPTION 'market order cannot carry a limit price' USING ERRCODE = 'PF005';
  END IF;

  IF p_side = 'BUY' THEN
    v_price := reservation_price(p_instrument, p_type, p_limit);
    v_hold  := round_he(p_qty * v_price, 4) + fee_for(p_instrument, p_qty, v_price);
    INSERT INTO cash_account (portfolio_id, currency) VALUES (p_portfolio, ins.currency)
      ON CONFLICT (portfolio_id, currency) DO NOTHING;
    -- atomic claim: check and reserve in one statement (5.3)
    UPDATE cash_account SET reserved = reserved + v_hold, version = version + 1
     WHERE portfolio_id = p_portfolio AND currency = ins.currency
       AND balance - reserved >= v_hold;
    IF NOT FOUND THEN RAISE EXCEPTION 'insufficient funds' USING ERRCODE = 'PF001'; END IF;
  ELSE
    UPDATE position SET reserved_qty = reserved_qty + p_qty, version = version + 1
     WHERE portfolio_id = p_portfolio AND instrument_id = p_instrument
       AND qty - reserved_qty >= p_qty;
    IF NOT FOUND THEN RAISE EXCEPTION 'insufficient shares' USING ERRCODE = 'PF002'; END IF;
  END IF;

  INSERT INTO orders (portfolio_id, instrument_id, side, type, qty, limit_price,
                      status, reserved_cash, client_order_id)
  VALUES (p_portfolio, p_instrument, p_side, p_type, p_qty, p_limit,
          'ACCEPTED', v_hold, p_client_id)
  RETURNING order_id INTO v_id;

  PERFORM audit_write('ORDER_PLACED', 'orders', v_id, NULL,
    jsonb_build_object('side', p_side, 'type', p_type, 'qty', p_qty,
                       'limit', p_limit, 'reserved_cash', v_hold));
  RETURN v_id;
END $$;

-- ---------------------------------------------------------------- cancel (shared core)
CREATE FUNCTION _cancel_core(p_order bigint, p_new_status text) RETURNS text
LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE o orders; cur char(3);
BEGIN
  SELECT * INTO o FROM orders WHERE order_id = p_order FOR UPDATE;      -- 1. order
  IF NOT FOUND THEN RAISE EXCEPTION 'order not found' USING ERRCODE = 'PF004'; END IF;
  IF o.status NOT IN ('ACCEPTED','PARTIALLY_FILLED') THEN
    RAISE EXCEPTION 'order % is %, cannot cancel', o.order_id, o.status USING ERRCODE = 'PF003';
  END IF;
  IF o.side = 'BUY' THEN
    cur := instrument_currency(o.instrument_id);
    PERFORM 1 FROM cash_account WHERE portfolio_id = o.portfolio_id AND currency = cur FOR UPDATE; -- 2. cash
    UPDATE cash_account SET reserved = reserved - o.reserved_cash, version = version + 1
     WHERE portfolio_id = o.portfolio_id AND currency = cur;
  ELSE
    PERFORM 1 FROM position WHERE portfolio_id = o.portfolio_id
       AND instrument_id = o.instrument_id FOR UPDATE;                  -- 3. position
    UPDATE position SET reserved_qty = reserved_qty - (o.qty - o.filled_qty), version = version + 1
     WHERE portfolio_id = o.portfolio_id AND instrument_id = o.instrument_id;
  END IF;
  UPDATE orders SET status = p_new_status, reserved_cash = 0, version = version + 1
   WHERE order_id = o.order_id;
  PERFORM audit_write('ORDER_' || p_new_status, 'orders', o.order_id,
    jsonb_build_object('status', o.status, 'filled_qty', o.filled_qty),
    jsonb_build_object('status', p_new_status));
  RETURN p_new_status;
END $$;

CREATE FUNCTION cancel_order(p_order bigint) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF app_uid() IS NULL THEN RAISE EXCEPTION 'no user context' USING ERRCODE = 'PF006'; END IF;
  PERFORM 1 FROM orders o JOIN portfolio p USING (portfolio_id)
   WHERE o.order_id = p_order AND p.user_id = app_uid();
  IF NOT FOUND THEN RAISE EXCEPTION 'order not found' USING ERRCODE = 'PF004'; END IF;
  RETURN _cancel_core(p_order, 'CANCELLED');
END $$;

-- ---------------------------------------------------------------- fill (5.4) -- worker only
CREATE FUNCTION fill_order(p_order bigint, p_qty numeric, p_price numeric) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  o orders; ins instrument; a cash_account; pos "position"; lot tax_lot;
  v_remaining numeric; v_fee numeric; v_gross numeric; v_release numeric;
  v_trade bigint; v_bal numeric; v_new_qty numeric; v_new_status text;
  v_need numeric; v_take numeric; v_alloc_proc numeric := 0; v_alloc_fee numeric := 0;
  v_proc numeric; v_cost numeric; v_feea numeric; v_realized numeric := 0; v_total_cost numeric := 0;
BEGIN
  SELECT * INTO o FROM orders WHERE order_id = p_order FOR UPDATE;                 -- 1. order
  IF NOT FOUND THEN RAISE EXCEPTION 'order not found' USING ERRCODE = 'PF004'; END IF;
  IF o.status NOT IN ('ACCEPTED','PARTIALLY_FILLED') THEN
    RAISE EXCEPTION 'order % is %, cannot fill', o.order_id, o.status USING ERRCODE = 'PF003';
  END IF;
  v_remaining := o.qty - o.filled_qty;
  IF p_qty IS NULL OR p_qty <= 0 OR p_qty > v_remaining THEN
    RAISE EXCEPTION 'fill quantity % outside remainder %', p_qty, v_remaining USING ERRCODE = 'PF005';
  END IF;
  SELECT * INTO ins FROM instrument WHERE instrument_id = o.instrument_id;
  IF ins.status <> 'ACTIVE' THEN
    RAISE EXCEPTION 'instrument is %', ins.status USING ERRCODE = 'PF005';
  END IF;
  IF o.type = 'LIMIT' AND ((o.side = 'BUY'  AND p_price > o.limit_price)
                        OR (o.side = 'SELL' AND p_price < o.limit_price)) THEN
    RAISE EXCEPTION 'price % violates limit %', p_price, o.limit_price USING ERRCODE = 'PF005';
  END IF;

  v_fee   := fee_for(o.instrument_id, p_qty, p_price);
  v_gross := round_he(p_qty * p_price, 4);

  INSERT INTO cash_account (portfolio_id, currency) VALUES (o.portfolio_id, ins.currency)
    ON CONFLICT (portfolio_id, currency) DO NOTHING;
  SELECT * INTO a FROM cash_account
   WHERE portfolio_id = o.portfolio_id AND currency = ins.currency FOR UPDATE;      -- 2. cash

  IF o.side = 'BUY' THEN
    INSERT INTO position (portfolio_id, instrument_id) VALUES (o.portfolio_id, o.instrument_id)
      ON CONFLICT DO NOTHING;
  END IF;
  SELECT * INTO pos FROM position
   WHERE portfolio_id = o.portfolio_id AND instrument_id = o.instrument_id FOR UPDATE; -- 3. position

  INSERT INTO trade (order_id, qty, price, fee) VALUES (o.order_id, p_qty, p_price, v_fee)
    RETURNING trade_id INTO v_trade;

  IF o.side = 'BUY' THEN
    -- reservation consumed in proportion; the last fill releases the exact remainder
    v_release := CASE WHEN p_qty = v_remaining THEN o.reserved_cash
                      ELSE LEAST(o.reserved_cash, round_he(o.reserved_cash * p_qty / v_remaining, 4)) END;
    v_bal := a.balance - v_gross;
    INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, ref_id, balance_after)
      VALUES (a.account_id, 'BUY_COST', -v_gross, 'TRADE', v_trade, v_bal);
    v_bal := v_bal - v_fee;
    INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, ref_id, balance_after)
      VALUES (a.account_id, 'FEE', -v_fee, 'TRADE', v_trade, v_bal);
    UPDATE cash_account SET balance = v_bal, reserved = reserved - v_release, version = version + 1
     WHERE account_id = a.account_id;   -- CHECK (balance - reserved >= 0) guards a shortfall

    v_new_qty := pos.qty + p_qty;
    UPDATE position SET qty = v_new_qty,
           avg_cost = round_he((pos.qty * pos.avg_cost + p_qty * p_price) / v_new_qty, 6),
           version = version + 1
     WHERE portfolio_id = pos.portfolio_id AND instrument_id = pos.instrument_id;
    INSERT INTO tax_lot (portfolio_id, instrument_id, trade_id, open_qty, remaining_qty, cost_per_share)
      VALUES (o.portfolio_id, o.instrument_id, v_trade, p_qty, p_qty, p_price);
  ELSE
    v_release := 0;
    v_bal := a.balance + v_gross;
    INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, ref_id, balance_after)
      VALUES (a.account_id, 'SELL_PROCEEDS', v_gross, 'TRADE', v_trade, v_bal);
    v_bal := v_bal - v_fee;
    INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, ref_id, balance_after)
      VALUES (a.account_id, 'FEE', -v_fee, 'TRADE', v_trade, v_bal);
    UPDATE cash_account SET balance = v_bal, version = version + 1 WHERE account_id = a.account_id;

    UPDATE position SET qty = qty - p_qty, reserved_qty = reserved_qty - p_qty,
           avg_cost = CASE WHEN qty - p_qty = 0 THEN 0 ELSE avg_cost END,
           version = version + 1
     WHERE portfolio_id = pos.portfolio_id AND instrument_id = pos.instrument_id;

    -- FIFO lot consumption (4. tax_lot is last in the lock order)
    v_need := p_qty;
    FOR lot IN SELECT * FROM tax_lot
                WHERE portfolio_id = o.portfolio_id AND instrument_id = o.instrument_id
                  AND remaining_qty > 0
                ORDER BY opened_at, lot_id FOR UPDATE
    LOOP
      EXIT WHEN v_need = 0;
      v_take := LEAST(lot.remaining_qty, v_need);
      IF v_take = v_need THEN               -- last lot absorbs rounding remainder
        v_proc := v_gross - v_alloc_proc;
        v_feea := v_fee - v_alloc_fee;
      ELSE
        v_proc := round_he(v_gross * v_take / p_qty, 4);
        v_feea := round_he(v_fee * v_take / p_qty, 4);
      END IF;
      v_cost := round_he(v_take * lot.cost_per_share, 4);
      INSERT INTO lot_disposal (lot_id, trade_id, qty, proceeds, cost, fee_alloc, realized_pnl)
        VALUES (lot.lot_id, v_trade, v_take, v_proc, v_cost, v_feea, v_proc - v_feea - v_cost);
      UPDATE tax_lot SET remaining_qty = remaining_qty - v_take WHERE lot_id = lot.lot_id;
      v_alloc_proc := v_alloc_proc + v_proc;
      v_alloc_fee  := v_alloc_fee + v_feea;
      v_need := v_need - v_take;
    END LOOP;
    IF v_need <> 0 THEN
      RAISE EXCEPTION 'lot shortfall % for order % (invariant I5)', v_need, o.order_id;
    END IF;
  END IF;

  v_new_status := CASE WHEN o.filled_qty + p_qty = o.qty THEN 'FILLED' ELSE 'PARTIALLY_FILLED' END;
  UPDATE orders SET filled_qty = filled_qty + p_qty, reserved_cash = reserved_cash - v_release,
         status = v_new_status, version = version + 1
   WHERE order_id = o.order_id;

  PERFORM audit_write('ORDER_FILLED', 'orders', o.order_id,
    jsonb_build_object('status', o.status, 'filled_qty', o.filled_qty),
    jsonb_build_object('status', v_new_status, 'trade_id', v_trade, 'qty', p_qty,
                       'price', p_price, 'fee', v_fee));
  RETURN v_trade;
END $$;

-- Queue pick for matcher workers: one open order, skipping rows other workers hold.
-- The row lock lives as long as the caller's transaction, which must also run fill_order.
CREATE FUNCTION next_open_order(p_instrument bigint DEFAULT NULL)
RETURNS TABLE (order_id bigint, instrument_id bigint, side text, type text,
               remaining numeric, limit_price numeric)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT o.order_id, o.instrument_id, o.side, o.type, o.qty - o.filled_qty, o.limit_price
    FROM orders o
   WHERE o.status IN ('ACCEPTED','PARTIALLY_FILLED')
     AND (p_instrument IS NULL OR o.instrument_id = p_instrument)
   ORDER BY o.created_at
   LIMIT 1
   FOR UPDATE SKIP LOCKED
$$;

-- ---------------------------------------------------------------- admin: prices
CREATE FUNCTION load_price(p_instrument bigint, p_ts timestamptz, p_open numeric, p_high numeric,
                           p_low numeric, p_close numeric, p_volume bigint, p_source text DEFAULT 'csv')
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_last numeric;
BEGIN
  IF LEAST(p_open, p_high, p_low, p_close) <= 0 OR p_low > p_high
     OR p_close > p_high OR p_close < p_low THEN
    RAISE EXCEPTION 'rejected price row (non-positive or inconsistent)' USING ERRCODE = 'PF005';
  END IF;
  SELECT price INTO v_last FROM latest_price WHERE instrument_id = p_instrument;
  IF v_last IS NOT NULL AND (p_close > v_last * 1.5 OR p_close < v_last * 0.5) THEN
    RAISE EXCEPTION 'rejected price % : more than 50%% away from last %', p_close, v_last
      USING ERRCODE = 'PF005';
  END IF;
  INSERT INTO price_snapshot (instrument_id, ts, open, high, low, close, volume, source)
    VALUES (p_instrument, p_ts, p_open, p_high, p_low, p_close, p_volume, p_source)
    ON CONFLICT (instrument_id, ts) DO UPDATE
      SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
          close = EXCLUDED.close, volume = EXCLUDED.volume,
          source = EXCLUDED.source, loaded_at = now();
  INSERT INTO latest_price (instrument_id, price, ts) VALUES (p_instrument, p_close, p_ts)
    ON CONFLICT (instrument_id) DO UPDATE SET price = EXCLUDED.price, ts = EXCLUDED.ts
    WHERE latest_price.ts <= EXCLUDED.ts;
  RETURN true;
END $$;

-- ---------------------------------------------------------------- admin: corporate actions (7.2)
-- One application row per holder is inserted first; the unique (ca_id, portfolio_id) makes a
-- repeat or a concurrent second run a no-op for that holder.
-- v1 simplification: open orders on the instrument are cancelled at split time (reservations
-- released) instead of being rescaled.
CREATE FUNCTION holdings_as_of(p_portfolio bigint, p_instrument bigint, p_date date) RETURNS numeric
LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT COALESCE(SUM(CASE o.side WHEN 'BUY' THEN t.qty ELSE -t.qty END), 0)
    FROM trade t JOIN orders o USING (order_id)
   WHERE o.portfolio_id = p_portfolio AND o.instrument_id = p_instrument AND t.settle_date <= p_date
$$;

CREATE FUNCTION apply_corporate_action(p_ca bigint) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE ca corporate_action; h record; n integer := 0; v_ins integer; v_ratio numeric;
        v_qty numeric; v_amt numeric; a cash_account; v_bal numeric; ord record;
BEGIN
  SELECT * INTO ca FROM corporate_action WHERE ca_id = p_ca;
  IF NOT FOUND THEN RAISE EXCEPTION 'corporate action not found' USING ERRCODE = 'PF004'; END IF;

  FOR h IN SELECT DISTINCT portfolio_id FROM (
             SELECT portfolio_id FROM position WHERE instrument_id = ca.instrument_id
             UNION
             SELECT o.portfolio_id FROM orders o JOIN trade t USING (order_id)
              WHERE o.instrument_id = ca.instrument_id) s
           ORDER BY portfolio_id
  LOOP
    INSERT INTO ca_application (ca_id, portfolio_id) VALUES (ca.ca_id, h.portfolio_id)
      ON CONFLICT (ca_id, portfolio_id) DO NOTHING;
    GET DIAGNOSTICS v_ins = ROW_COUNT;
    CONTINUE WHEN v_ins = 0;                                   -- already applied

    IF ca.kind = 'SPLIT' THEN
      v_ratio := ca.ratio_num / ca.ratio_den;
      FOR ord IN SELECT order_id FROM orders
                  WHERE portfolio_id = h.portfolio_id AND instrument_id = ca.instrument_id
                    AND status IN ('ACCEPTED','PARTIALLY_FILLED') ORDER BY order_id
      LOOP
        PERFORM _cancel_core(ord.order_id, 'CANCELLED');
      END LOOP;
      PERFORM 1 FROM position WHERE portfolio_id = h.portfolio_id
         AND instrument_id = ca.instrument_id FOR UPDATE;
      UPDATE tax_lot SET open_qty = round_he(open_qty * v_ratio, 6),
             remaining_qty = round_he(remaining_qty * v_ratio, 6),
             cost_per_share = round_he(cost_per_share / v_ratio, 6)
       WHERE portfolio_id = h.portfolio_id AND instrument_id = ca.instrument_id;
      UPDATE position SET
             qty = COALESCE((SELECT SUM(remaining_qty) FROM tax_lot t
                              WHERE t.portfolio_id = position.portfolio_id
                                AND t.instrument_id = position.instrument_id), 0),
             avg_cost = round_he(avg_cost / v_ratio, 6), reserved_qty = 0, version = version + 1
       WHERE portfolio_id = h.portfolio_id AND instrument_id = ca.instrument_id;
      PERFORM audit_write('CA_SPLIT', 'portfolio', h.portfolio_id, NULL,
                          jsonb_build_object('ca_id', ca.ca_id, 'ratio', v_ratio));
    ELSE
      v_qty := holdings_as_of(h.portfolio_id, ca.instrument_id, ca.record_date);
      CONTINUE WHEN v_qty <= 0;
      v_amt := round_he(v_qty * ca.amount_per_share, 4);
      INSERT INTO cash_account (portfolio_id, currency)
        VALUES (h.portfolio_id, instrument_currency(ca.instrument_id))
        ON CONFLICT (portfolio_id, currency) DO NOTHING;
      SELECT * INTO a FROM cash_account WHERE portfolio_id = h.portfolio_id
         AND currency = instrument_currency(ca.instrument_id) FOR UPDATE;
      v_bal := a.balance + v_amt;
      INSERT INTO cash_ledger (account_id, entry_type, amount, ref_type, ref_id, balance_after)
        VALUES (a.account_id, 'DIVIDEND', v_amt, 'CORPORATE_ACTION', ca.ca_id, v_bal);
      UPDATE cash_account SET balance = v_bal, version = version + 1 WHERE account_id = a.account_id;
      PERFORM audit_write('CA_DIVIDEND', 'cash_account', a.account_id, NULL,
                          jsonb_build_object('ca_id', ca.ca_id, 'qty', v_qty, 'amount', v_amt));
    END IF;
    n := n + 1;
  END LOOP;
  RETURN n;
END $$;

-- ---------------------------------------------------------------- reconciliation (9.2)
-- Any returned row is a defect.
CREATE FUNCTION reconcile() RETURNS TABLE (invariant text, detail text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT 'I1', 'cash_account ' || account_id || ' available < 0'
    FROM cash_account WHERE balance - reserved < 0
  UNION ALL
  SELECT 'I2', 'position ' || portfolio_id || '/' || instrument_id || ' qty or reserved invalid'
    FROM position WHERE qty < 0 OR reserved_qty < 0 OR reserved_qty > qty
  UNION ALL
  SELECT 'I3', 'cash_account ' || a.account_id || ' balance <> ledger'
    FROM cash_account a
   WHERE a.balance <> (SELECT COALESCE(SUM(l.amount), 0) FROM cash_ledger l WHERE l.account_id = a.account_id)
  UNION ALL
  SELECT 'I4', 'order ' || o.order_id || ' filled_qty <> sum(trades) or > qty'
    FROM orders o
   WHERE o.filled_qty > o.qty
      OR o.filled_qty <> (SELECT COALESCE(SUM(t.qty), 0) FROM trade t WHERE t.order_id = o.order_id)
  UNION ALL
  SELECT 'I5', 'position ' || p.portfolio_id || '/' || p.instrument_id || ' qty <> open lots'
    FROM position p
   WHERE p.qty <> (SELECT COALESCE(SUM(t.remaining_qty), 0) FROM tax_lot t
                    WHERE t.portfolio_id = p.portfolio_id AND t.instrument_id = p.instrument_id)
  UNION ALL   -- reservation accounting (extra checks beyond I1-I5)
  SELECT 'I6', 'cash_account ' || a.account_id || ' reserved <> open BUY reservations'
    FROM cash_account a
   WHERE a.reserved <> (SELECT COALESCE(SUM(o.reserved_cash), 0) FROM orders o
                         WHERE o.portfolio_id = a.portfolio_id AND o.side = 'BUY'
                           AND o.status IN ('ACCEPTED','PARTIALLY_FILLED')
                           AND instrument_currency(o.instrument_id) = a.currency)
  UNION ALL
  SELECT 'I7', 'position ' || p.portfolio_id || '/' || p.instrument_id || ' reserved_qty <> open SELL remainder'
    FROM position p
   WHERE p.reserved_qty <> (SELECT COALESCE(SUM(o.qty - o.filled_qty), 0) FROM orders o
                             WHERE o.portfolio_id = p.portfolio_id AND o.instrument_id = p.instrument_id
                               AND o.side = 'SELL' AND o.status IN ('ACCEPTED','PARTIALLY_FILLED'))
  UNION ALL
  SELECT 'AUDIT', 'hash chain broken at audit_id ' || verify_audit_chain()
   WHERE verify_audit_chain() IS NOT NULL
$$;

-- ---------------------------------------------------------------- privileges on functions
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_uid(), app_claim_role(), round_he(numeric, int),
      instrument_currency(bigint), fee_for(bigint, numeric, numeric),
      reservation_price(bigint, text, numeric), holdings_as_of(bigint, bigint, date),
      audit_hash(audit_log) TO app_role, worker_role, admin_role;
GRANT EXECUTE ON FUNCTION deposit(bigint, char, numeric), withdraw(bigint, char, numeric),
      place_order(bigint, bigint, text, text, numeric, numeric, uuid),
      cancel_order(bigint) TO app_role;
GRANT EXECUTE ON FUNCTION fill_order(bigint, numeric, numeric),
      next_open_order(bigint) TO worker_role;
GRANT EXECUTE ON FUNCTION load_price(bigint, timestamptz, numeric, numeric, numeric, numeric, bigint, text),
      apply_corporate_action(bigint), reconcile(), verify_audit_chain() TO admin_role;
