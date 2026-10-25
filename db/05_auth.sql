-- Authentication functions and matcher queue. Credentials and sessions are reachable only
-- through these SECURITY DEFINER functions; app_role loses direct access to both tables.

SET search_path = public;

REVOKE ALL ON app_user, auth_session, user_role FROM app_role;

-- ---------------------------------------------------------------- users
CREATE OR REPLACE FUNCTION register_user(p_email text, p_hash text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE v_id bigint;
BEGIN
  INSERT INTO app_user (email, password_hash) VALUES (trim(p_email), p_hash)
    RETURNING user_id INTO v_id;                    -- unique(lower(email)) -> 23505 on duplicate
  INSERT INTO user_role (user_id, role_id) VALUES (v_id, 1);        -- always plain USER
  INSERT INTO portfolio (user_id, name) VALUES (v_id, 'Main');
  PERFORM pg_advisory_xact_lock(727001);
  INSERT INTO audit_log (actor, action, entity, entity_id) VALUES (v_id, 'USER_REGISTERED', 'app_user', v_id);
  RETURN v_id;
END $$;

CREATE OR REPLACE FUNCTION auth_lookup(p_email text)
RETURNS TABLE (user_id bigint, password_hash text, locked boolean, roles text[])
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT u.user_id, u.password_hash,
         (u.locked_until IS NOT NULL AND u.locked_until > now()),
         COALESCE((SELECT array_agg(r.name) FROM user_role ur JOIN role r USING (role_id)
                    WHERE ur.user_id = u.user_id), '{}')
    FROM app_user u WHERE lower(u.email) = lower(trim(p_email))
$$;

CREATE OR REPLACE FUNCTION auth_failure(p_user bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE app_user SET failed_attempts = failed_attempts + 1,
         locked_until = CASE WHEN failed_attempts + 1 >= 5 THEN now() + interval '15 minutes'
                             ELSE locked_until END
   WHERE user_id = p_user;
  PERFORM pg_advisory_xact_lock(727001);
  INSERT INTO audit_log (actor, action, entity, entity_id) VALUES (p_user, 'LOGIN_FAILED', 'app_user', p_user);
END $$;

CREATE OR REPLACE FUNCTION auth_success(p_user bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  UPDATE app_user SET failed_attempts = 0, locked_until = NULL WHERE user_id = p_user;
  PERFORM pg_advisory_xact_lock(727001);
  INSERT INTO audit_log (actor, action, entity, entity_id) VALUES (p_user, 'LOGIN_OK', 'app_user', p_user);
END $$;

-- ---------------------------------------------------------------- refresh-token sessions
CREATE OR REPLACE FUNCTION session_issue(p_user bigint, p_hash text, p_ttl_seconds int, p_ip inet, p_ua text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  INSERT INTO auth_session (user_id, refresh_token_hash, expires_at, ip, user_agent)
  VALUES (p_user, p_hash, now() + make_interval(secs => p_ttl_seconds), p_ip, p_ua)
$$;

-- Rotation with reuse detection: presenting an already-rotated token revokes the whole family.
-- Returns (user_id, roles, outcome) where outcome is 'ok' | 'invalid' | 'reuse'.
CREATE OR REPLACE FUNCTION session_rotate(p_old_hash text, p_new_hash text, p_ttl_seconds int)
RETURNS TABLE (user_id bigint, roles text[], outcome text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE s auth_session;
BEGIN
  SELECT * INTO s FROM auth_session WHERE refresh_token_hash = p_old_hash FOR UPDATE;
  IF NOT FOUND THEN RETURN QUERY SELECT NULL::bigint, '{}'::text[], 'invalid'; RETURN; END IF;
  IF s.revoked_at IS NOT NULL THEN
    UPDATE auth_session SET revoked_at = now() WHERE family_id = s.family_id AND revoked_at IS NULL;
    PERFORM pg_advisory_xact_lock(727001);
    INSERT INTO audit_log (actor, action, entity, entity_id)
      VALUES (s.user_id, 'REFRESH_REUSE_DETECTED', 'auth_session', s.user_id);
    RETURN QUERY SELECT s.user_id, '{}'::text[], 'reuse'; RETURN;
  END IF;
  IF s.expires_at <= now() THEN RETURN QUERY SELECT NULL::bigint, '{}'::text[], 'invalid'; RETURN; END IF;
  UPDATE auth_session SET revoked_at = now() WHERE session_id = s.session_id;
  INSERT INTO auth_session (family_id, user_id, refresh_token_hash, expires_at, ip, user_agent)
    VALUES (s.family_id, s.user_id, p_new_hash, now() + make_interval(secs => p_ttl_seconds), s.ip, s.user_agent);
  RETURN QUERY SELECT s.user_id,
     COALESCE((SELECT array_agg(r.name) FROM user_role ur JOIN role r USING (role_id)
                WHERE ur.user_id = s.user_id), '{}'), 'ok'::text;
END $$;

CREATE OR REPLACE FUNCTION session_revoke(p_hash text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  UPDATE auth_session SET revoked_at = now()
   WHERE family_id = (SELECT family_id FROM auth_session WHERE refresh_token_hash = p_hash)
     AND revoked_at IS NULL
$$;

-- ---------------------------------------------------------------- matcher queue
-- One marketable open order, locked with SKIP LOCKED so parallel workers never collide.
-- Caller runs fill_order in the same transaction.
CREATE OR REPLACE FUNCTION next_marketable_order()
RETURNS TABLE (order_id bigint, remaining numeric, price numeric)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT o.order_id, o.qty - o.filled_qty,
         CASE o.type WHEN 'MARKET' THEN lp.price
                     WHEN 'LIMIT'  THEN lp.price END
    FROM orders o
    JOIN instrument i USING (instrument_id)
    JOIN latest_price lp USING (instrument_id)
   WHERE o.status IN ('ACCEPTED','PARTIALLY_FILLED') AND i.status = 'ACTIVE'
     AND (o.type = 'MARKET'
          OR (o.side = 'BUY'  AND lp.price <= o.limit_price)
          OR (o.side = 'SELL' AND lp.price >= o.limit_price))
   ORDER BY o.created_at
   LIMIT 1
   FOR UPDATE OF o SKIP LOCKED
$$;

-- expire stale open orders (releases reservations through the shared cancel core)
CREATE OR REPLACE FUNCTION expire_old_orders(p_max_age interval DEFAULT interval '1 day') RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE r record; n integer := 0;
BEGIN
  FOR r IN SELECT order_id FROM orders
            WHERE status IN ('ACCEPTED','PARTIALLY_FILLED') AND created_at < now() - p_max_age
            ORDER BY order_id
  LOOP
    BEGIN
      PERFORM _cancel_core(r.order_id, 'EXPIRED'); n := n + 1;
    EXCEPTION WHEN sqlstate 'PF003' THEN NULL;   -- raced with a fill/cancel: fine
    END;
  END LOOP;
  RETURN n;
END $$;

-- ---------------------------------------------------------------- privileges
REVOKE ALL ON FUNCTION register_user(text, text), auth_lookup(text), auth_failure(bigint),
  auth_success(bigint), session_issue(bigint, text, int, inet, text),
  session_rotate(text, text, int), session_revoke(text),
  next_marketable_order(), expire_old_orders(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION register_user(text, text), auth_lookup(text), auth_failure(bigint),
  auth_success(bigint), session_issue(bigint, text, int, inet, text),
  session_rotate(text, text, int), session_revoke(text) TO app_role;
GRANT EXECUTE ON FUNCTION next_marketable_order(), expire_old_orders(interval) TO worker_role;
