# Stock Portfolio Management System — v1 prototype

PostgreSQL 16 holds every business rule; the API is a thin, authenticated shell around stored functions.

## Layout
- `db/01_schema.sql` tables + CHECK constraints (invariants I1, I2, I4)
- `db/02_security.sql` roles, RLS, append-only ledger/audit, audit hash chain, order state machine
- `db/03_functions.sql` deposit, withdraw, place_order, cancel_order, fill_order (FIFO lots), load_price, apply_corporate_action, `reconcile()`
- `db/04_seed.sql` 20 instruments, 3 months of prices, 5 demo users
- `db/05_auth.sql` register/login/refresh-rotation functions, matcher queue (SKIP LOCKED)
- `api/` Starlette app (`app.py`), Argon2id + JWT (`security.py`), libpq driver (`pg.py`)
- `worker/matcher.py` matching simulator (run several in parallel)
- `tests/` 32 tests: concurrency, invariants, security, API

## Run
```bash
export PGHOST=/tmp PGPORT=5433                      # your Postgres
for f in db/0*.sql; do psql -v ON_ERROR_STOP=1 -d portfolio -f $f; done
PF_DEMO_PASSWORD='something-long-12+' python3 api/seed_passwords.py
PF_JWT_SECRET="$(openssl rand -hex 32)" uvicorn app:app --app-dir api --port 8000
python3 worker/matcher.py                           # PF_PARTIAL=1 for random partial fills
python3 tests/run_tests.py                          # uses real pytest if installed
```
Set `PF_JWT_SECRET` in any real deployment; without it a random per-process secret is used and tokens die on restart.

## Not done / deliberate v1 limits
- No MFA/TOTP, no email verification, no password reset.
- Login throttling is in-memory per process (use a shared store with several API instances).
- Split handling cancels open orders on that instrument instead of rescaling them.
- Single currency per instrument, no FX. Prices come from CSV upload only (`POST /admin/prices`).
- `psycopg`/FastAPI/pytest could not be installed in the build sandbox (no PyPI), so the code uses a ctypes libpq wrapper and Starlette. Swapping in psycopg is mechanical; `tests/shim/pytest.py` is only a stand-in.
- Development Postgres runs with `trust` auth; use password/cert auth and TLS for anything else.
