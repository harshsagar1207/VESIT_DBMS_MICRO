import threading
import uuid
from decimal import Decimal

import pytest

import pg
from conftest import dsn, invariants, seed_shares


def run_parallel(fns):
    out = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def wrap(i, fn):
        barrier.wait()
        try:
            out[i] = ("ok", fn())
        except pg.PgError as e:
            out[i] = ("err", e.sqlstate)
    ts = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def place(uid, pid, iid, side, qty, limit, cid=None):
    c = pg.Connection(dsn("app_role"))
    try:
        with c.tx(user_id=uid):
            return c.scalar("SELECT place_order($1,$2,$3,'LIMIT',$4,$5,$6::uuid)",
                            (pid, iid, side, qty, limit, cid or str(uuid.uuid4())))
    finally:
        c.close()


def fill(oid, qty, price):
    c = pg.Connection(dsn("worker_role"))
    try:
        with c.tx():
            return c.scalar("SELECT fill_order($1,$2,$3)", (oid, qty, price))
    finally:
        c.close()


def cancel(uid, oid):
    c = pg.Connection(dsn("app_role"))
    try:
        with c.tx(user_id=uid):
            return c.scalar("SELECT cancel_order($1)", (oid,))
    finally:
        c.close()


def test_double_spend_only_one_wins(admin, make_user, instrument):
    uid, pid = make_user(cash="1000")
    # each order needs ~600 -> only one can be reserved
    res = run_parallel([lambda: place(uid, pid, instrument, "BUY", 6, "100.00") for _ in range(8)])
    assert sum(r[0] == "ok" for r in res) == 1
    assert all(r[1] == "PF001" for r in res if r[0] == "err")
    assert invariants(admin) == []


def test_oversell_only_one_wins(admin, make_user, instrument):
    uid, pid = make_user(cash="0", shares=(instrument, 10, "50.00"))
    res = run_parallel([lambda: place(uid, pid, instrument, "SELL", 6, "50.00") for _ in range(6)])
    assert sum(r[0] == "ok" for r in res) == 1
    assert all(r[1] == "PF002" for r in res if r[0] == "err")
    assert invariants(admin) == []


def test_duplicate_client_id_is_one_order(admin, make_user, instrument):
    uid, pid = make_user(cash="700")
    cid = str(uuid.uuid4())
    res = run_parallel([lambda: place(uid, pid, instrument, "BUY", 6, "100.00", cid) for _ in range(8)])
    assert all(r[0] == "ok" for r in res)                # retries get the stored order back
    assert len({r[1] for r in res}) == 1
    assert admin.scalar("SELECT count(*) FROM orders WHERE client_order_id=$1::uuid", (cid,)) == 1
    assert invariants(admin) == []


def test_cancel_vs_fill_race(admin, make_user, instrument):
    for _ in range(10):
        uid, pid = make_user(cash="1000")
        oid = place(uid, pid, instrument, "BUY", 5, "100.00")
        res = run_parallel([lambda: cancel(uid, oid), lambda: fill(oid, 5, "100.00")])
        statuses = admin.scalar("SELECT status FROM orders WHERE order_id=$1", (oid,))
        assert statuses in ("CANCELLED", "FILLED")
        assert sum(r[0] == "ok" for r in res) == 1        # exactly one transition wins
        assert all(r[1] == "PF003" for r in res if r[0] == "err")
    assert invariants(admin) == []


def test_partial_fills_release_exact_reservation(admin, make_user, instrument):
    uid, pid = make_user(cash="2000")
    oid = place(uid, pid, instrument, "BUY", 10, "100.00")
    fill(oid, 3, "99.00")
    fill(oid, 3, "98.50")
    fill(oid, 4, "97.00")
    assert admin.scalar("SELECT status FROM orders WHERE order_id=$1", (oid,)) == "FILLED"
    a = admin.one("SELECT balance, reserved FROM cash_account WHERE portfolio_id=$1", (pid,))
    assert a["reserved"] == 0
    assert a["balance"] < 2000
    assert invariants(admin) == []


def test_overfill_and_limit_violation_rejected(admin, make_user, instrument):
    uid, pid = make_user(cash="2000")
    oid = place(uid, pid, instrument, "BUY", 5, "100.00")
    with pytest.raises(pg.PgError) as e:
        fill(oid, 6, "100.00")
    assert e.value.sqlstate == "PF005"
    with pytest.raises(pg.PgError) as e:
        fill(oid, 5, "100.01")
    assert e.value.sqlstate == "PF005"
    assert invariants(admin) == []


def test_fifo_lots_and_realized_pnl(admin, make_user, instrument):
    uid, pid = make_user(cash="5000")
    b1 = place(uid, pid, instrument, "BUY", 10, "10.00")
    fill(b1, 10, "10.00")
    b2 = place(uid, pid, instrument, "BUY", 10, "20.00")
    fill(b2, 10, "20.00")
    s = place(uid, pid, instrument, "SELL", 15, "30.00")
    fill(s, 15, "30.00")
    lots = admin.execute("SELECT remaining_qty FROM tax_lot WHERE portfolio_id=$1 ORDER BY lot_id", (pid,))
    assert [l["remaining_qty"] for l in lots] == [0, 5]       # oldest lot consumed first
    pnl = admin.scalar("SELECT sum(d.realized_pnl) FROM lot_disposal d JOIN trade t USING (trade_id) "
                       "WHERE t.order_id=$1", (s,))
    fee = admin.scalar("SELECT fee FROM trade WHERE order_id=$1", (s,))
    assert pnl == Decimal("10") * 20 + Decimal("5") * 10 - fee   # 450 gross gain - fee
    assert invariants(admin) == []


def test_sell_without_position_rejected(admin, make_user, instrument):
    uid, pid = make_user(cash="100")
    with pytest.raises(pg.PgError) as e:
        place(uid, pid, instrument, "SELL", 1, "10.00")
    assert e.value.sqlstate == "PF002"


def test_cannot_touch_someone_elses_portfolio(admin, make_user, instrument):
    uid_a, pid_a = make_user(cash="1000")
    uid_b, _ = make_user(cash="0")
    with pytest.raises(pg.PgError) as e:
        place(uid_b, pid_a, instrument, "BUY", 1, "10.00")
    assert e.value.sqlstate == "PF004"
    oid = place(uid_a, pid_a, instrument, "BUY", 1, "10.00")
    with pytest.raises(pg.PgError) as e:
        cancel(uid_b, oid)
    assert e.value.sqlstate == "PF004"


def test_rls_hides_other_users_rows(admin, make_user, instrument):
    uid_a, pid_a = make_user(cash="1000")
    uid_b, _ = make_user(cash="500")
    c = pg.Connection(dsn("app_role"))
    try:
        with c.tx(user_id=uid_b):
            assert c.scalar("SELECT count(*) FROM cash_account WHERE portfolio_id=$1", (pid_a,)) == 0
            assert c.scalar("SELECT count(*) FROM cash_ledger") >= 1
            assert c.scalar("SELECT count(DISTINCT portfolio_id) FROM cash_account") == 1
        with c.tx():                                        # no identity -> sees nothing
            assert c.scalar("SELECT count(*) FROM cash_account") == 0
    finally:
        c.close()


def test_app_role_cannot_write_money_tables(admin, make_user):
    uid, pid = make_user(cash="100")
    c = pg.Connection(dsn("app_role"))
    try:
        for sql in ("UPDATE cash_account SET balance = 1e9",
                    "INSERT INTO cash_ledger (account_id, entry_type, amount, balance_after) VALUES (1,'DEPOSIT',1,1)",
                    "DELETE FROM orders", "UPDATE position SET qty = 1e9",
                    "DELETE FROM audit_log"):
            with pytest.raises(pg.PgError) as e:
                with c.tx(user_id=uid):
                    c.execute(sql)
            assert e.value.sqlstate == "42501", sql
    finally:
        c.close()


def test_ledger_and_audit_are_append_only(admin, make_user):
    make_user(cash="100")
    for sql in ("UPDATE cash_ledger SET amount = 0", "DELETE FROM cash_ledger",
                "UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log",
                "TRUNCATE cash_ledger"):
        with pytest.raises(pg.PgError) as e:
            admin.execute(sql)                               # even the superuser is blocked by triggers
        assert e.value.sqlstate == "42501", sql


def test_audit_chain_detects_tampering(admin, make_user):
    make_user(cash="100")
    assert admin.scalar("SELECT verify_audit_chain()") is None
    target = admin.scalar("SELECT audit_id FROM audit_log ORDER BY audit_id DESC LIMIT 1")
    admin.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_append_only")
    try:
        admin.execute("UPDATE audit_log SET after = '{\"amount\": 999999}' WHERE audit_id=$1", (target,))
        assert admin.scalar("SELECT verify_audit_chain()") == target
        admin.execute("DELETE FROM audit_log WHERE audit_id >= $1", (target,))   # drop tampered tail
    finally:
        admin.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_append_only")
    assert admin.scalar("SELECT verify_audit_chain()") is None


def test_illegal_state_transition_blocked(admin, make_user, instrument):
    uid, pid = make_user(cash="1000")
    oid = place(uid, pid, instrument, "BUY", 1, "10.00")
    cancel(uid, oid)
    with pytest.raises(pg.PgError) as e:
        admin.execute("UPDATE orders SET status='ACCEPTED' WHERE order_id=$1", (oid,))
    assert e.value.sqlstate == "PF003"


def test_withdraw_cannot_touch_reserved_cash(admin, make_user, instrument):
    uid, pid = make_user(cash="1000")
    place(uid, pid, instrument, "BUY", 9, "100.00")          # ~900 + fee reserved
    c = pg.Connection(dsn("app_role"))
    try:
        with pytest.raises(pg.PgError) as e:
            with c.tx(user_id=uid):
                c.execute("SELECT withdraw($1,'USD',500)", (pid,))
        assert e.value.sqlstate == "PF001"
    finally:
        c.close()
    assert invariants(admin) == []


def test_concurrent_fills_by_many_workers_stay_consistent(admin, make_user, instrument):
    uid, pid = make_user(cash="100000")
    oids = [place(uid, pid, instrument, "BUY", 10, "100.00") for _ in range(10)]
    res = run_parallel([(lambda o=o: fill(o, 10, "99.00")) for o in oids] +
                       [(lambda o=o: fill(o, 10, "99.00")) for o in oids])   # every order filled twice
    assert sum(r[0] == "ok" for r in res) == 10
    assert all(r[1] == "PF003" for r in res if r[0] == "err")
    assert invariants(admin) == []


def test_skip_locked_queue_gives_each_worker_a_different_order(admin, make_user, instrument):
    uid, pid = make_user(cash="100000")
    [place(uid, pid, instrument, "BUY", 1, "100.00") for _ in range(6)]
    got, lock = [], threading.Lock()
    barrier = threading.Barrier(4)

    def worker():
        c = pg.Connection(dsn("worker_role"))
        try:
            c.execute("BEGIN")
            r = c.one("SELECT * FROM next_open_order($1)", (instrument,))
            barrier.wait()                                   # all hold their row at the same time
            with lock:
                got.append(r["order_id"] if r else None)
            c.execute("ROLLBACK")
        finally:
            c.close()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert None not in got and len(set(got)) == 4


def test_money_never_floats(admin, make_user, instrument):
    uid, pid = make_user(cash="1000.10")
    oid = place(uid, pid, instrument, "BUY", 3, "0.10")
    fill(oid, 3, "0.10")
    bal = admin.scalar("SELECT balance FROM cash_account WHERE portfolio_id=$1", (pid,))
    fee = admin.scalar("SELECT fee FROM trade WHERE order_id=$1", (oid,))
    assert bal == Decimal("1000.10") - Decimal("0.30") - fee
    assert invariants(admin) == []
