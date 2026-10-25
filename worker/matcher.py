"""Matching simulator (report 5.4 / 10.3): fills marketable open orders at the latest price.

Run several copies in parallel: next_marketable_order() uses FOR UPDATE SKIP LOCKED, so two
workers never take the same order, and fill_order() does the whole fill in one transaction.

    PF_PARTIAL=1 python3 worker/matcher.py      # fill in random-sized chunks to exercise partial fills
"""
import os
import random
import sys
import time
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import pg  # noqa: E402

DSN = (f"host={os.environ.get('PGHOST', '/tmp')} port={os.environ.get('PGPORT', '5433')} "
       f"dbname={os.environ.get('PFDB', 'portfolio')} user=worker_role")


def step(conn, partial=False):
    """Fill one order. Returns True if work was done."""
    try:
        with conn.tx():
            o = conn.one("SELECT * FROM next_marketable_order()")
            if not o:
                return False
            qty = o["remaining"]
            if partial and qty > 1:
                qty = Decimal(random.randint(1, int(qty)))
            conn.execute("SELECT fill_order($1,$2,$3)", (o["order_id"], qty, o["price"]))
            return True
    except pg.PgError as e:
        # Business rejection (e.g. instrument suspended between pick and fill): the transaction
        # rolled back, the order is untouched. Log and carry on.
        print(f"fill failed [{e.sqlstate}]: {e.message}", file=sys.stderr)
        return False


def main():
    partial = os.environ.get("PF_PARTIAL") == "1"
    conn = pg.Connection(DSN)
    n = 0
    while True:
        if step(conn, partial):
            n += 1
            continue
        if os.environ.get("PF_ONCE"):
            print(f"filled {n}")
            return
        time.sleep(0.5)


if __name__ == "__main__":
    main()
