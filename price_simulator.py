#!/usr/bin/env python3
"""Price simulator: moves every instrument's price, so the whole system runs without a market feed.

What it does each cycle (default 1 s):
  * equities and listed IPOs follow a one-factor random walk (shared market shock + own noise),
    with a +/-10% circuit limit against the reference close, rounded to the instrument's tick size
  * indices are the weight-averaged return of their members (index_membership); GIFT Nifty is
    Nifty 50 plus a slowly drifting basis
  * mutual-fund NAVs and bonds move rarely and very little; IPOs that are OPEN gain subscription
  * one batched upsert into latest_price, one NOTIFY on channel 'price_tick' (payload "id:price,..."),
    a heartbeat row in price_feed_state, all in one transaction
  * 1-minute candles are accumulated in memory and written to price_snapshot (source='sim') when
    the minute rolls over

A Postgres advisory lock keeps a single simulator running, so two copies can't fight over the same
rows. Run:  DATABASE_URL=postgresql://... python price_simulator.py [--interval 1] [--vol-mult 25]

Assumptions about the schema (adjust the SQL below if yours differs): latest_price(instrument_id PK,
price, ts); price_snapshot(instrument_id, ts, open, high, low, close, volume, source) with a unique
key that ON CONFLICT DO NOTHING can use; tables from 03b_market_extension.sql.
"""
import argparse
import json
import math
import os
import random
import signal
import time
import zlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LOCK_KEY = 7770001
TRADING_SECONDS_PER_YEAR = 252 * 6.25 * 3600     # 252 days x 09:15-15:30
CIRCUIT = 0.10

# asset_class -> (annual volatility, update every N cycles, market beta range)
PROFILE = {
    "EQUITY": (0.26, 1, (0.6, 1.4)),
    "IPO": (0.55, 1, (0.8, 1.8)),
    "MUTUAL_FUND": (0.10, 30, (0.0, 1.0)),
    "BOND": (0.02, 10, (0.0, 0.1)),
}
MARKET_VOL = 0.14          # annual vol of the common market factor


class Inst:
    __slots__ = ("id", "symbol", "cls", "tick", "price", "ref", "sigma", "every", "beta", "ipo_status")

    def __init__(self, id, symbol, cls, tick, price, ipo_status=None):
        self.id, self.symbol, self.cls, self.tick = id, symbol, cls, float(tick)
        self.price = self.ref = float(price)
        self.ipo_status = ipo_status
        prof = PROFILE.get(cls)
        if prof:
            self.sigma, self.every, (lo, hi) = prof
            r = random.Random(zlib.crc32(symbol.encode()))     # stable beta per instrument
            self.beta = lo + (hi - lo) * r.random()
        else:
            self.sigma, self.every, self.beta = 0.0, 1, 0.0


class Market:
    """Pure price model: no database in here, so it can be tested on its own."""

    def __init__(self, insts, members, interval, vol_mult, seed=None):
        self.rng = random.Random(seed)
        self.interval = interval
        self.vol_mult = vol_mult
        self.cycle = 0
        self.by_sym = {i.symbol: i for i in insts}
        self.by_id = {i.id: i for i in insts}
        self.movers = [i for i in insts if i.cls in PROFILE and not (i.cls == "IPO" and i.ipo_status != "LISTED")]
        self.indices = [i for i in insts if i.cls == "INDEX"]
        self.gift = self.by_sym.get("GIFTNIFTY")
        self.members = {}                                   # index id -> [(member Inst, weight)]
        for idx_id, mem_id, w in members:
            if w and idx_id in self.by_id and mem_id in self.by_id:
                self.members.setdefault(idx_id, []).append((self.by_id[mem_id], float(w)))
        self.basis = 0.0015
        self.open_ipos = [i for i in insts if i.cls == "IPO" and i.ipo_status == "OPEN"]
        self.subscription = {}                              # ipo id -> current subscription (x)

    def _round(self, inst, p):
        t = inst.tick
        return max(t, round(round(p / t) * t, 4))

    def step(self):
        """Advance one cycle. Returns {instrument_id: new_price} for instruments that changed."""
        self.cycle += 1
        scale = self.vol_mult
        dt_base = self.interval / TRADING_SECONDS_PER_YEAR
        sys_shock = self.rng.gauss(0, 1)
        rets, out = {}, {}
        for i in self.movers:
            if self.cycle % i.every:
                continue
            dt = dt_base * i.every
            sd = math.sqrt(dt) * scale
            r = i.beta * MARKET_VOL * sd * sys_shock \
                + math.sqrt(max(i.sigma ** 2 - (i.beta * MARKET_VOL) ** 2, 0.0)) * sd * self.rng.gauss(0, 1)
            new = i.price * math.exp(r - 0.5 * i.sigma ** 2 * dt)
            new = min(max(new, i.ref * (1 - CIRCUIT)), i.ref * (1 + CIRCUIT))
            new = self._round(i, new)
            rets[i.id] = (new / i.price) - 1.0
            if new != i.price:
                i.price = new
                out[i.id] = new
        for ix in self.indices:                              # weight-averaged member returns
            mem = [(m, w) for m, w in self.members.get(ix.id, []) if m.id in rets]
            if not mem:
                continue
            r = sum(w * rets[m.id] for m, w in mem) / sum(w for _, w in mem)
            new = self._round(ix, ix.price * (1 + r))
            if new != ix.price:
                ix.price = new
                out[ix.id] = new
        if self.gift is not None:
            nifty = self.by_sym.get("NIFTY")
            if nifty is not None:
                self.basis = min(max(self.basis + self.rng.gauss(0, 0.00002) * scale ** 0.5, 0.0003), 0.003)
                new = self._round(self.gift, nifty.price * (1 + self.basis))
                if new != self.gift.price:
                    self.gift.price = new
                    out[self.gift.id] = new
        return out

    def step_ipo_subscriptions(self):
        """OPEN IPOs gain subscription over time. Returns {ipo instrument id: new subscription_x}."""
        out = {}
        for i in self.open_ipos:
            cur = self.subscription.get(i.id)
            if cur is None:
                continue
            cur += abs(self.rng.gauss(0, 0.05)) * self.vol_mult / 25
            self.subscription[i.id] = round(cur, 2)
            out[i.id] = round(cur, 2)
        return out


class Candles:
    """Accumulates 1-minute OHLCV per instrument; flush() returns the finished minutes."""

    def __init__(self):
        self.cur = {}                                        # id -> [minute_ts, o, h, l, c, vol]

    def add(self, inst_id, ts, price, vol):
        m = ts.replace(second=0, microsecond=0)
        c = self.cur.get(inst_id)
        if c is None or c[0] != m:
            self.cur[inst_id] = [m, price, price, price, price, vol]
            return [(inst_id, c)] if c else []
        c[2], c[3], c[4], c[5] = max(c[2], price), min(c[3], price), price, c[5] + vol
        return []

    def drain(self):
        done = list(self.cur.items())
        self.cur = {}
        return done


# --------------------------------------------------------------------------- database layer
SQL_LOAD = """
SELECT i.instrument_id, i.symbol, i.asset_class, i.tick_size, lp.price, o.status
FROM instrument i
JOIN latest_price lp ON lp.instrument_id = i.instrument_id
LEFT JOIN ipo o ON o.instrument_id = i.instrument_id"""
SQL_MEMBERS = "SELECT index_id, member_id, weight_pct FROM index_membership"
SQL_SUBS = "SELECT instrument_id, subscription_x FROM ipo WHERE status = 'OPEN'"
SQL_UPSERT_PRICES = """
INSERT INTO latest_price (instrument_id, price, ts)
SELECT * FROM unnest(%s::bigint[], %s::numeric[], %s::timestamptz[])
ON CONFLICT (instrument_id) DO UPDATE SET price = EXCLUDED.price, ts = EXCLUDED.ts
WHERE latest_price.ts <= EXCLUDED.ts"""
SQL_CANDLES = """
INSERT INTO price_snapshot (instrument_id, ts, open, high, low, close, volume, source)
SELECT id, ts, o, h, l, c, v, 'sim'
FROM unnest(%s::bigint[], %s::timestamptz[], %s::numeric[], %s::numeric[], %s::numeric[], %s::numeric[], %s::bigint[])
     AS t(id, ts, o, h, l, c, v)
ON CONFLICT DO NOTHING"""
SQL_SUBS_UPDATE = """
UPDATE ipo SET subscription_x = t.x FROM unnest(%s::bigint[], %s::numeric[]) AS t(id, x)
WHERE ipo.instrument_id = t.id"""
SQL_HEARTBEAT = """
INSERT INTO price_feed_state (source, last_success_at) VALUES ('simulator', now())
ON CONFLICT (source) DO UPDATE SET last_success_at = now(), last_error = NULL"""

NOTIFY_CHUNK = 80          # keeps each payload far below Postgres' 8000-byte NOTIFY limit


def in_session(now):
    n = now.astimezone(IST)
    return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) < (15, 30)


def flush_candles(cur, finished):
    rows = [(i, c) for i, c in finished]
    if not rows:
        return
    cur.execute(SQL_CANDLES, (
        [i for i, _ in rows], [c[0] for _, c in rows], [c[1] for _, c in rows], [c[2] for _, c in rows],
        [c[3] for _, c in rows], [c[4] for _, c in rows], [int(c[5]) for _, c in rows]))


def main():
    import psycopg                                           # imported late so the model can be tested without a driver

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--interval", type=float, default=1.0, help="seconds per cycle")
    ap.add_argument("--vol-mult", type=float, default=25.0, help="volatility multiplier so moves are visible in a demo")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds (0 = run until stopped)")
    ap.add_argument("--sessions-only", action="store_true", help="only move prices 09:15-15:30 IST, Mon-Fri")
    args = ap.parse_args()
    if not args.dsn:
        ap.error("set DATABASE_URL or pass --dsn")

    stop = {"flag": False}
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: stop.update(flag=True))

    with psycopg.connect(args.dsn) as conn:
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
        if not cur.fetchone()[0]:
            raise SystemExit("another price simulator is already running (advisory lock held)")
        cur.execute(SQL_LOAD)
        insts = [Inst(r[0], r[1], r[2], r[3], r[4], r[5]) for r in cur.fetchall()]
        cur.execute(SQL_MEMBERS)
        members = cur.fetchall()
        market = Market(insts, members, args.interval, args.vol_mult, args.seed)
        cur.execute(SQL_SUBS)
        market.subscription = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
        conn.commit()
        candles, vrng = Candles(), random.Random(args.seed)
        started = time.monotonic()
        print(f"simulating {len(insts)} instruments, interval={args.interval}s, vol-mult={args.vol_mult}")

        while not stop["flag"]:
            t0 = time.monotonic()
            now = datetime.now(timezone.utc)
            if not args.sessions_only or in_session(now):
                changed = market.step()
                finished = []
                for iid, price in changed.items():
                    inst = market.by_id[iid]
                    vol = 0 if inst.cls == "INDEX" else vrng.randint(10, 2000)
                    finished += candles.add(iid, now, price, vol)
                if changed:
                    ids = list(changed)
                    cur.execute(SQL_UPSERT_PRICES, (ids, [changed[i] for i in ids], [now] * len(ids)))
                    for k in range(0, len(ids), NOTIFY_CHUNK):
                        payload = ",".join(f"{i}:{changed[i]}" for i in ids[k:k + NOTIFY_CHUNK])
                        cur.execute("SELECT pg_notify('price_tick', %s)", (payload,))
                subs = market.step_ipo_subscriptions()
                if subs:
                    cur.execute(SQL_SUBS_UPDATE, (list(subs), list(subs.values())))
                flush_candles(cur, [(i, c) for i, c in finished])
                cur.execute(SQL_HEARTBEAT)
                conn.commit()
            if args.duration and time.monotonic() - started >= args.duration:
                break
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))

        flush_candles(cur, candles.drain())                  # write the partial minute on shutdown
        conn.commit()
        print("stopped")


if __name__ == "__main__":
    main()
