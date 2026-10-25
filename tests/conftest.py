import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
import pg  # noqa: E402

HOST = os.environ.get("PGHOST", "/tmp")
PORT = os.environ.get("PGPORT", "5433")
DB = os.environ.get("PFDB", "portfolio")


def dsn(user):
    return f"host={HOST} port={PORT} dbname={DB} user={user}"


@pytest.fixture(scope="session")
def admin():
    c = pg.Connection(dsn("postgres"))   # superuser: setup and verification only
    yield c
    c.close()


@pytest.fixture()
def make_user(admin):
    """A fresh user with one portfolio and funded cash. Returns (user_id, portfolio_id)."""
    def _make(cash="10000", shares=None):
        email = f"t{uuid.uuid4().hex[:10]}@test.local"
        uid = admin.scalar("INSERT INTO app_user (email, password_hash) VALUES ($1,'x') RETURNING user_id", (email,))
        pid = admin.scalar("INSERT INTO portfolio (user_id, name) VALUES ($1,'Main') RETURNING portfolio_id", (uid,))
        app = pg.Connection(dsn("app_role"))
        try:
            if float(cash) > 0:
                with app.tx(user_id=uid):
                    app.execute("SELECT deposit($1,'USD',$2)", (pid, cash))
        finally:
            app.close()
        if shares:
            iid, qty, price = shares
            seed_shares(admin, pid, iid, qty, price)
        return uid, pid
    return _make


def seed_shares(admin, pid, iid, qty, price):
    """Give a portfolio shares by running a real buy + fill (so lots and ledger stay consistent)."""
    uid = admin.scalar("SELECT user_id FROM portfolio WHERE portfolio_id=$1", (pid,))
    app = pg.Connection(dsn("app_role"))
    wrk = pg.Connection(dsn("worker_role"))
    try:
        with app.tx(user_id=uid):
            app.execute("SELECT deposit($1,'USD',$2)", (pid, str(float(qty) * float(price) * 1.01 + 5)))
            oid = app.scalar("SELECT place_order($1,$2,'BUY','LIMIT',$3,$4,$5::uuid)",
                             (pid, iid, qty, price, str(uuid.uuid4())))
        with wrk.tx():
            wrk.execute("SELECT fill_order($1,$2,$3)", (oid, qty, price))
    finally:
        app.close()
        wrk.close()


@pytest.fixture(scope="session")
def instrument(admin):
    return admin.scalar("SELECT instrument_id FROM instrument WHERE symbol='AAPL'")


def as_user(uid):
    c = pg.Connection(dsn("app_role"))
    return c


def invariants(admin):
    return admin.execute("SELECT * FROM reconcile()")


@pytest.fixture(scope="session")
def api():
    import subprocess, time, requests
    port = "8099"
    env = {**os.environ, "PGHOST": HOST, "PGPORT": PORT, "PFDB": DB,
           "PF_JWT_SECRET": "test-secret-" + "x" * 40, "PF_LOGIN_LIMIT": "10000"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--app-dir",
                             os.path.join(os.path.dirname(__file__), "..", "api"),
                             "--port", port, "--log-level", "warning"], env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if requests.get(base + "/health", timeout=1).status_code == 200:
                break
        except requests.RequestException:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("API did not start")
    yield base
    proc.terminate()
    proc.wait(5)
