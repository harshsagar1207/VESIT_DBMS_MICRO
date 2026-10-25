import base64
import json
import os
import subprocess
import sys
import threading
import uuid

import pytest
import requests

import pg
from conftest import dsn, invariants

PW = "correct-horse-battery-9"


def new_user(api):
    email = f"u{uuid.uuid4().hex[:10]}@test.local"
    r = requests.post(api + "/auth/register", json={"email": email, "password": PW})
    assert r.status_code == 201, r.text
    t = requests.post(api + "/auth/login", json={"email": email, "password": PW}).json()
    return email, t, {"Authorization": "Bearer " + t["access_token"]}


def portfolio_id(api, h):
    return requests.get(api + "/portfolios", headers=h).json()[0]["portfolio_id"]


def deposit(api, h, pid, amt, key=None):
    return requests.post(f"{api}/portfolios/{pid}/deposit", json={"amount": amt, "currency": "USD"},
                         headers={**h, "Idempotency-Key": key or str(uuid.uuid4())})


def run_worker():
    env = {**os.environ, "PF_ONCE": "1"}
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "worker", "matcher.py")],
                   env=env, check=True, capture_output=True)


def test_register_login_and_auth_required(api):
    assert requests.get(api + "/portfolios").status_code == 401
    assert requests.get(api + "/portfolios", headers={"Authorization": "Bearer garbage"}).status_code == 401
    email, t, h = new_user(api)
    assert requests.get(api + "/portfolios", headers=h).status_code == 200
    # duplicate registration (case-insensitive) is refused without saying why
    assert requests.post(api + "/auth/register", json={"email": email.upper(), "password": PW}).status_code == 409
    assert requests.post(api + "/auth/register", json={"email": "a@b.co", "password": "short"}).status_code == 422


def test_wrong_password_and_unknown_user_look_identical(api):
    email, _, _ = new_user(api)
    a = requests.post(api + "/auth/login", json={"email": email, "password": "wrong-password-123"})
    b = requests.post(api + "/auth/login", json={"email": "nobody@test.local", "password": "wrong-password-123"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_lockout_after_five_failures(api):
    email, _, _ = new_user(api)
    for _ in range(5):
        assert requests.post(api + "/auth/login", json={"email": email, "password": "wrong-password-123"}).status_code == 401
    r = requests.post(api + "/auth/login", json={"email": email, "password": PW})   # right password, still locked
    assert r.status_code == 401


def test_forged_and_tampered_tokens_rejected(api):
    _, t, h = new_user(api)
    head, payload, sig = t["access_token"].split(".")
    pl = json.loads(base64.urlsafe_b64decode(payload + "=="))
    pl["roles"] = ["ADMIN"]
    forged = base64.urlsafe_b64encode(json.dumps(pl).encode()).rstrip(b"=").decode()
    r = requests.get(api + "/admin/reconcile", headers={"Authorization": f"Bearer {head}.{forged}.{sig}"})
    assert r.status_code == 401
    none_hdr = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    r = requests.get(api + "/portfolios", headers={"Authorization": f"Bearer {none_hdr}.{forged}."})
    assert r.status_code == 401
    assert requests.get(api + "/admin/reconcile", headers=h).status_code == 403     # honest USER token


def test_refresh_rotation_and_reuse_detection(api):
    _, t, _ = new_user(api)
    r1 = requests.post(api + "/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r1.status_code == 200
    new_rt = r1.json()["refresh_token"]
    # replaying the old token = theft signal: refused AND the whole family is revoked
    assert requests.post(api + "/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    assert requests.post(api + "/auth/refresh", json={"refresh_token": new_rt}).status_code == 401


def test_logout_revokes_refresh(api):
    _, t, _ = new_user(api)
    requests.post(api + "/auth/logout", json={"refresh_token": t["refresh_token"]})
    assert requests.post(api + "/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_idor_other_users_portfolio_and_orders(api, instrument):
    _, _, ha = new_user(api)
    _, _, hb = new_user(api)
    pa = portfolio_id(api, ha)
    assert requests.get(f"{api}/portfolios/{pa}", headers=hb).status_code == 404
    assert deposit(api, hb, pa, "100").status_code == 404
    deposit(api, ha, pa, "1000")
    o = requests.post(api + "/orders", headers={**ha, "Idempotency-Key": str(uuid.uuid4())},
                      json={"portfolio_id": pa, "instrument_id": instrument, "side": "BUY", "type": "LIMIT",
                            "qty": "1", "limit_price": "10.00"})
    assert o.status_code == 201
    assert requests.delete(f"{api}/orders/{o.json()['order_id']}", headers=hb).status_code == 404
    assert requests.get(api + "/orders", headers=hb).json() == []


def test_deposit_idempotency(api):
    _, _, h = new_user(api)
    pid = portfolio_id(api, h)
    k = str(uuid.uuid4())
    a, b = deposit(api, h, pid, "250", k), deposit(api, h, pid, "250", k)
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert requests.get(f"{api}/portfolios/{pid}", headers=h).json()["cash"][0]["balance"] == "250.0000"
    assert deposit(api, h, pid, "999", k).status_code == 422               # same key, different body
    assert requests.post(f"{api}/portfolios/{pid}/deposit", json={"amount": "5"}, headers=h).status_code == 400


def test_concurrent_identical_deposits_apply_once(api):
    _, _, h = new_user(api)
    pid = portfolio_id(api, h)
    k, out = str(uuid.uuid4()), []
    ts = [threading.Thread(target=lambda: out.append(deposit(api, h, pid, "100", k).status_code)) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == [200] * 6
    assert requests.get(f"{api}/portfolios/{pid}", headers=h).json()["cash"][0]["balance"] == "100.0000"


def test_input_validation(api, instrument):
    _, _, h = new_user(api)
    pid = portfolio_id(api, h)
    for bad in ("-5", "0", "abc", "1e3", "1.23456", "NaN", "Infinity"):
        assert deposit(api, h, pid, bad).status_code == 422, bad
    base = {"portfolio_id": pid, "instrument_id": instrument, "side": "BUY", "type": "LIMIT", "qty": "1", "limit_price": "10"}
    for patch in ({"qty": "0"}, {"qty": "-1"}, {"side": "HOLD"}, {"type": "STOP"}, {"limit_price": None},
                  {"limit_price": "10.001"}, {"portfolio_id": "1"}, {"instrument_id": 99999}):
        r = requests.post(api + "/orders", headers={**h, "Idempotency-Key": str(uuid.uuid4())}, json={**base, **patch})
        assert r.status_code in (404, 422), (patch, r.status_code, r.text)
    r = requests.post(api + "/orders", headers={**h, "Idempotency-Key": "not-a-uuid"}, json=base)
    assert r.status_code == 400


def test_sql_injection_is_just_a_string(api):
    r = requests.post(api + "/auth/login", json={"email": "' OR 1=1; DROP TABLE app_user; --", "password": "x" * 14})
    assert r.status_code == 401
    r = requests.post(api + "/auth/register", json={"email": f"x{uuid.uuid4().hex[:8]}'; DROP TABLE orders;--@t.co", "password": PW})
    assert r.status_code in (201, 422)
    assert requests.get(api + "/instruments").status_code == 200


def test_end_to_end_trade_via_api(api, admin, instrument):
    _, _, h = new_user(api)
    pid = portfolio_id(api, h)
    deposit(api, h, pid, "100000")
    price = requests.get(api + "/instruments").json()
    last = next(i for i in price if i["instrument_id"] == instrument)["last_price"]
    key = str(uuid.uuid4())
    body = {"portfolio_id": pid, "instrument_id": instrument, "side": "BUY", "type": "MARKET", "qty": "10"}
    o1 = requests.post(api + "/orders", headers={**h, "Idempotency-Key": key}, json=body)
    o2 = requests.post(api + "/orders", headers={**h, "Idempotency-Key": key}, json=body)   # client retry
    assert o1.status_code == o2.status_code == 201 and o1.json()["order_id"] == o2.json()["order_id"]
    run_worker()
    v = requests.get(f"{api}/portfolios/{pid}", headers=h).json()
    assert [p["symbol"] for p in v["positions"]] == ["AAPL"] and v["positions"][0]["qty"] == "10.000000"
    assert v["cash"][0]["reserved"] == "0.0000"
    # sell half, realized P&L visible
    s = requests.post(api + "/orders", headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                      json={**body, "side": "SELL", "qty": "4"})
    assert s.status_code == 201
    run_worker()
    v = requests.get(f"{api}/portfolios/{pid}", headers=h).json()
    assert v["positions"][0]["qty"] == "6.000000"
    # cannot sell what is not there
    big = requests.post(api + "/orders", headers={**h, "Idempotency-Key": str(uuid.uuid4())},
                        json={**body, "side": "SELL", "qty": "7"})
    assert big.status_code == 409
    assert invariants(admin) == []


def test_admin_and_auditor_access(api, admin):
    email, _, _ = new_user(api)
    admin.execute("INSERT INTO user_role SELECT user_id, 2 FROM app_user WHERE email=$1 ON CONFLICT DO NOTHING", (email,))
    h = {"Authorization": "Bearer " + requests.post(api + "/auth/login", json={"email": email, "password": PW}).json()["access_token"]}
    r = requests.get(api + "/admin/reconcile", headers=h)
    assert r.status_code == 200 and r.json() == {"violations": []}
    assert requests.get(api + "/audit", headers=h).status_code == 403     # ADMIN is not AUDITOR


def test_price_upload_rejects_bad_rows(api, admin):
    email, _, _ = new_user(api)
    admin.execute("INSERT INTO user_role SELECT user_id, 2 FROM app_user WHERE email=$1 ON CONFLICT DO NOTHING", (email,))
    h = {"Authorization": "Bearer " + requests.post(api + "/auth/login", json={"email": email, "password": PW}).json()["access_token"]}
    last = admin.scalar("SELECT price FROM latest_price lp JOIN instrument i USING (instrument_id) WHERE symbol='KO'")
    csvdata = ("symbol,ts,open,high,low,close,volume\n"
               f"KO,2030-01-01T21:00:00Z,{last},{last},{last},{last},10\n"      # good
               f"KO,2030-01-02T21:00:00Z,1,2,1,{float(last)*3},10\n"            # >50% jump and close>high
               "NOPE,2030-01-02T21:00:00Z,1,1,1,1,1\n"                          # unknown symbol
               f"KO,2030-01-03T21:00:00Z,-1,1,1,1,1\n")                         # negative
    r = requests.post(api + "/admin/prices", headers=h, data=csvdata).json()
    assert r["loaded"] == 1 and [x["line"] for x in r["rejected"]] == [3, 4, 5]
    admin.execute("DELETE FROM price_snapshot WHERE ts >= '2030-01-01'")
    admin.execute("UPDATE latest_price SET price=$1, ts=(SELECT max(ts) FROM price_snapshot WHERE instrument_id=latest_price.instrument_id) "
                  "WHERE instrument_id=(SELECT instrument_id FROM instrument WHERE symbol='KO')", (last,))
