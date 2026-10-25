"""Stock Portfolio Management System - HTTP API (Starlette).

The API holds no business rules: every money or share movement is a call to a stored
function that runs in one transaction under row-level security. Handlers only authenticate,
validate shape, translate errors, and set the per-request identity.
"""
import csv
import io
import json
import os
import re
import time
import uuid
from collections import defaultdict, deque
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
load_dotenv()
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import pg
import security as sec

HOST, PORT, DB = (os.environ.get("PGHOST", "/tmp"), os.environ.get("PGPORT", "5433"),
                  os.environ.get("PFDB", "portfolio"))
APP = pg.Pool(f"host={HOST} port={PORT} dbname={DB} user=app_role", 8)
ADMIN = pg.Pool(f"host={HOST} port={PORT} dbname={DB} user=admin_role", 2)

# SQLSTATE -> (HTTP status, public message). Anything else becomes a generic 500.
ERRORS = {"PF001": 409, "PF002": 409, "PF003": 409, "PF004": 404, "PF005": 422, "PF006": 403,
          "23505": 409, "23514": 409}


class Json(JSONResponse):
    def render(self, content):
        return json.dumps(content, default=lambda o: format(o, "f") if isinstance(o, Decimal) else str(o)).encode()


def err(status, msg, **extra):
    return Json({"error": msg, **extra}, status_code=status)


def money(v, field, places=4, positive=True):
    if isinstance(v, bool) or not re.fullmatch(r"\d+(\.\d+)?|\.\d+", str(v).strip()) and not (
            positive is False and re.fullmatch(r"-?\d+(\.\d+)?", str(v).strip())):
        raise HTTPException(422, f"{field} must be a plain decimal number")
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f"{field} must be a decimal number")
    if not d.is_finite() or (positive and d <= 0) or -d.as_tuple().exponent > places:
        raise HTTPException(422, f"{field} must be positive with at most {places} decimals")
    return d


async def body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(b, dict):
        raise HTTPException(400, "JSON object expected")
    return b


# ---------------------------------------------------------------- login throttle (per IP)
_hits = defaultdict(deque)


def throttle(ip, limit=int(os.environ.get("PF_LOGIN_LIMIT", "20")), window=60):
    q, now = _hits[ip], time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "too many attempts")
    q.append(now)


# ---------------------------------------------------------------- auth helpers
def claims(request: Request, need=None):
    h = request.headers.get("authorization", "")
    c = sec.read_access(h[7:]) if h.lower().startswith("bearer ") else None
    if not c:
        raise HTTPException(401, "authentication required")
    if need and need not in c["roles"]:
        raise HTTPException(403, "forbidden")
    return c


def run(pool, c, fn):
    """Run fn(conn) in one transaction with the caller's identity; map DB errors to HTTP."""
    rid = uuid.uuid4().hex
    with pool.connection() as conn:
        try:
            with conn.tx(user_id=c["sub"] if c else None,
                         role="AUDITOR" if c and "AUDITOR" in c["roles"] else None, request_id=rid):
                return fn(conn)
        except pg.PgError as e:
            status = ERRORS.get(e.sqlstate)
            if status:
                raise HTTPException(status, e.message)
            raise


def tokens(conn, user_id, roles, request):
    refresh, rhash = sec.new_refresh()
    ip = request.client.host if request.client else None
    conn.execute("SELECT session_issue($1,$2,$3,$4::inet,$5)",
                 (user_id, rhash, sec.REFRESH_TTL, ip, request.headers.get("user-agent", "")[:200]))
    return {"access_token": sec.make_access(user_id, roles), "token_type": "bearer",
            "expires_in": sec.ACCESS_TTL, "refresh_token": refresh}


# ---------------------------------------------------------------- auth endpoints
def register(request, b):
    email, pw = str(b.get("email", "")).strip(), str(b.get("password", ""))
    if "@" not in email or len(email) > 254:
        raise HTTPException(422, "valid email required")
    if (e := sec.password_ok(pw)):
        raise HTTPException(422, e)
    h = sec.hash_password(pw)
    try:
        with APP.connection() as conn:
            with conn.tx():
                uid = conn.scalar("SELECT register_user($1,$2)", (email, h))
    except pg.PgError as e:
        if e.sqlstate == "23505":
            raise HTTPException(409, "could not register with these details")   # no enumeration detail
        raise
    return Json({"user_id": uid}, status_code=201)


def login(request, b):
    throttle(request.client.host if request.client else "?")
    email, pw = str(b.get("email", "")), str(b.get("password", ""))
    with APP.connection() as conn:
        with conn.tx():
            u = conn.one("SELECT * FROM auth_lookup($1)", (email,))
        if not u:
            sec.verify_password(pw, sec.DUMMY_HASH)                # equalise timing
            raise HTTPException(401, "invalid credentials")
        if u["locked"]:
            sec.verify_password(pw, sec.DUMMY_HASH)
            raise HTTPException(401, "invalid credentials")        # same message while locked
        if not sec.verify_password(pw, u["password_hash"]):
            with conn.tx():
                conn.execute("SELECT auth_failure($1)", (u["user_id"],))
            raise HTTPException(401, "invalid credentials")
        with conn.tx():
            conn.execute("SELECT auth_success($1)", (u["user_id"],))
            roles = _roles(u["roles"])
            return Json(tokens(conn, u["user_id"], roles, request))


def _roles(v):
    return v if isinstance(v, list) else [x for x in str(v).strip("{}").split(",") if x]


def refresh(request, b):
    old = str(b.get("refresh_token", ""))
    new, nhash = sec.new_refresh()
    with APP.connection() as conn:
        with conn.tx():
            r = conn.one("SELECT * FROM session_rotate($1,$2,$3)", (sec.hash_refresh(old), nhash, sec.REFRESH_TTL))
        if r["outcome"] != "ok":
            raise HTTPException(401, "invalid refresh token")
        return Json({"access_token": sec.make_access(r["user_id"], _roles(r["roles"])), "token_type": "bearer",
                     "expires_in": sec.ACCESS_TTL, "refresh_token": new})


def logout(request, b):
    with APP.connection() as conn:
        with conn.tx():
            conn.execute("SELECT session_revoke($1)", (sec.hash_refresh(str(b.get("refresh_token", ""))),))
    return Json({"ok": True})


# ---------------------------------------------------------------- idempotent money operations
def idempotent(conn, c, request, scope, payload, op):
    """Replay-safe wrapper: same key + same payload returns the stored response."""
    key = request.headers.get("idempotency-key")
    if not key or len(key) > 128:
        raise HTTPException(400, "Idempotency-Key header required (max 128 chars)")
    rh = sec.request_hash(scope, json.dumps(payload, sort_keys=True))
    k = f"{scope}:{key}"
    conn.execute("INSERT INTO idempotency_key (key, user_id, request_hash) VALUES ($1,$2,$3) "
                 "ON CONFLICT (user_id, key) DO NOTHING", (k, c["sub"], rh))
    row = conn.one("SELECT request_hash, response FROM idempotency_key WHERE user_id=$1 AND key=$2",
                   (c["sub"], k))
    if row["request_hash"] != rh:
        raise HTTPException(422, "Idempotency-Key reused with a different request")
    if row["response"] is not None:
        return row["response"]
    out = op()
    conn.execute("UPDATE idempotency_key SET response=$3::jsonb WHERE user_id=$1 AND key=$2",
                 (c["sub"], k, json.dumps(out, default=str)))
    return out


# ---------------------------------------------------------------- portfolio endpoints
def list_portfolios(c):
    return Json(run(APP, c, lambda cn: cn.execute(
        "SELECT portfolio_id, name, base_currency FROM portfolio ORDER BY portfolio_id")))


def portfolio_view(c, pid):
    def go(cn):
        p = cn.one("SELECT portfolio_id, name, base_currency FROM portfolio WHERE portfolio_id=$1", (pid,))
        if not p:
            raise HTTPException(404, "portfolio not found")
        cash = cn.execute("SELECT currency, balance, reserved, balance - reserved AS available "
                          "FROM cash_account WHERE portfolio_id=$1", (pid,))
        pos = cn.execute(
            "SELECT i.symbol, p.qty, p.reserved_qty, p.avg_cost, lp.price AS last_price, "
            "       round(p.qty * lp.price, 4) AS market_value, "
            "       round(p.qty * (lp.price - p.avg_cost), 4) AS unrealized_pnl "
            "FROM position p JOIN instrument i USING (instrument_id) "
            "LEFT JOIN latest_price lp USING (instrument_id) "
            "WHERE p.portfolio_id=$1 AND p.qty > 0 ORDER BY i.symbol", (pid,))
        realized = cn.scalar(
            "SELECT COALESCE(sum(d.realized_pnl),0) FROM lot_disposal d "
            "JOIN trade t USING (trade_id) JOIN orders o USING (order_id) WHERE o.portfolio_id=$1", (pid,))
        return {**p, "cash": cash, "positions": pos, "realized_pnl": realized}
    return Json(run(APP, c, go))


def cash_op(request, c, pid, b, fn):
    amt, cur = money(b.get("amount"), "amount"), str(b.get("currency", "USD")).upper()
    if len(cur) != 3:
        raise HTTPException(422, "currency must be a 3-letter code")

    def go(cn):
        return idempotent(cn, c, request, f"{fn}:{pid}", {"a": str(amt), "c": cur},
                          lambda: {"balance": cn.scalar(f"SELECT {fn}($1,$2,$3)", (pid, cur, amt))})
    return Json(run(APP, c, go))


# ---------------------------------------------------------------- orders
def create_order(request, c, b):
    try:
        cid = str(uuid.UUID(request.headers.get("idempotency-key", "")))
    except ValueError:
        raise HTTPException(400, "Idempotency-Key header must be a UUID")
    pid, iid = b.get("portfolio_id"), b.get("instrument_id")
    if not isinstance(pid, int) or not isinstance(iid, int):
        raise HTTPException(422, "portfolio_id and instrument_id must be integers")
    side, typ = str(b.get("side", "")).upper(), str(b.get("type", "")).upper()
    qty = money(b.get("qty"), "qty", 6)
    limit = money(b["limit_price"], "limit_price", 6) if b.get("limit_price") is not None else None

    def go(cn):
        oid = cn.scalar("SELECT place_order($1,$2,$3,$4,$5,$6,$7::uuid)", (pid, iid, side, typ, qty, limit, cid))
        return cn.one("SELECT * FROM orders WHERE order_id=$1", (oid,))
    return Json(run(APP, c, go), status_code=201)


def cancel(c, oid):
    def go(cn):
        cn.execute("SELECT cancel_order($1)", (oid,))
        return cn.one("SELECT order_id, status FROM orders WHERE order_id=$1", (oid,))
    return Json(run(APP, c, go))


def list_orders(request, c):
    pid = request.query_params.get("portfolio_id")
    st = request.query_params.get("status")
    return Json(run(APP, c, lambda cn: cn.execute(
        "SELECT * FROM orders WHERE ($1::bigint IS NULL OR portfolio_id=$1::bigint) "
        "AND ($2::text IS NULL OR status=$2) ORDER BY order_id DESC LIMIT 200", (pid, st))))


# ---------------------------------------------------------------- market data
def instruments(request):
    return Json(run(APP, None, lambda cn: cn.execute(
        "SELECT i.instrument_id, i.symbol, i.exchange, i.currency, lp.price AS last_price "
        "FROM instrument i LEFT JOIN latest_price lp USING (instrument_id) ORDER BY symbol")))


# ---------------------------------------------------------------- admin
def load_prices(c, text):
    ok, bad = 0, []
    with ADMIN.connection() as cn:
        sym = {r["symbol"]: r["instrument_id"] for r in cn.execute("SELECT symbol, instrument_id FROM instrument")}
        for n, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            try:
                with cn.tx(user_id=c["sub"]):
                    cn.execute("SELECT load_price($1,$2::timestamptz,$3,$4,$5,$6,$7,'csv')", (
                        sym[row["symbol"].strip().upper()], row["ts"], money(row["open"], "open", 6),
                        money(row["high"], "high", 6), money(row["low"], "low", 6),
                        money(row["close"], "close", 6), int(row.get("volume") or 0)))
                ok += 1
            except (pg.PgError, KeyError, ValueError, HTTPException) as e:
                bad.append({"line": n, "reason": getattr(e, "message", None) or getattr(e, "detail", None) or str(e)})
    return Json({"loaded": ok, "rejected": bad})


# ---------------------------------------------------------------- routing
async def _call(fn, request, c, b, **path):
    import anyio
    names = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    kw = {k: v for k, v in {"request": request, "c": c, "b": b, **path}.items() if k in names}
    return await anyio.to_thread.run_sync(lambda: fn(**kw))


async def admin_prices_h(request: Request):
    try:
        c = claims(request, "ADMIN")
        raw = (await request.body())[:5_000_000].decode("utf-8", "replace")
        import anyio
        return await anyio.to_thread.run_sync(lambda: load_prices(c, raw))
    except HTTPException as e:
        return err(e.status_code, e.detail)


def reconcile_view(c):
    with ADMIN.connection() as cn:
        with cn.tx(user_id=c["sub"]):
            return Json({"violations": cn.execute("SELECT * FROM reconcile()")})


def apply_ca(c, ca_id):
    with ADMIN.connection() as cn:
        with cn.tx(user_id=c["sub"]):
            return Json({"holders_updated": cn.scalar("SELECT apply_corporate_action($1)", (ca_id,))})


def audit_view(c):
    return Json(run(APP, c, lambda cn: cn.execute(
        "SELECT audit_id, actor, action, entity, entity_id, ts FROM audit_log ORDER BY audit_id DESC LIMIT 200")))


def health(request):
    return Json({"status": "ok"})


async def _wrapped(fn, request, need=None, with_body=False, public=False):
    try:
        c = None if public else claims(request, need)
        b = await body(request) if with_body else None
        return await _call(fn, request, c, b, **request.path_params)
    except HTTPException as e:
        return err(e.status_code, e.detail)


def route(path, fn, methods, **kw):
    async def h(request):
        return await _wrapped(fn, request, **kw)
    return Route(path, h, methods=methods)


async def deposit_h(request):
    return await _wrapped(lambda request, c, b, id: cash_op(request, c, id, b, "deposit"), request, with_body=True)


async def withdraw_h(request):
    return await _wrapped(lambda request, c, b, id: cash_op(request, c, id, b, "withdraw"), request, with_body=True)


async def security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
                         "Content-Security-Policy": "default-src 'none'", "X-Frame-Options": "DENY"})
    return resp


from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

app = Starlette(routes=[
    route("/health", health, ["GET"], public=True),
    route("/auth/register", register, ["POST"], public=True, with_body=True),
    route("/auth/login", login, ["POST"], public=True, with_body=True),
    route("/auth/refresh", refresh, ["POST"], public=True, with_body=True),
    route("/auth/logout", logout, ["POST"], public=True, with_body=True),
    route("/instruments", instruments, ["GET"], public=True),
    route("/portfolios", list_portfolios, ["GET"]),
    route("/portfolios/{pid:int}", portfolio_view, ["GET"]),
    Route("/portfolios/{id:int}/deposit", deposit_h, methods=["POST"]),
    Route("/portfolios/{id:int}/withdraw", withdraw_h, methods=["POST"]),
    route("/orders", create_order, ["POST"], with_body=True),
    route("/orders", list_orders, ["GET"]),
    route("/orders/{oid:int}", cancel, ["DELETE"]),
    route("/audit", audit_view, ["GET"], need="AUDITOR"),
    route("/admin/reconcile", reconcile_view, ["GET"], need="ADMIN"),
    route("/admin/corporate-actions/{ca_id:int}/apply", apply_ca, ["POST"], need="ADMIN"),
    Route("/admin/prices", admin_prices_h, methods=["POST"]),
], middleware=[__import__("starlette.middleware", fromlist=["Middleware"]).Middleware(
    BaseHTTPMiddleware, dispatch=security_headers)])
