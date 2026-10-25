"""Give the seeded demo users a real Argon2id password hash.

    PF_DEMO_PASSWORD='something-long-12+' python3 api/seed_passwords.py
Without the variable a random password is generated and printed once.
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(__file__))
import pg  # noqa: E402
import security as sec  # noqa: E402

pw = os.environ.get("PF_DEMO_PASSWORD") or secrets.token_urlsafe(12) + "-9"
c = pg.Connection(f"host={os.environ.get('PGHOST', '/tmp')} port={os.environ.get('PGPORT', '5433')} "
                  f"dbname={os.environ.get('PFDB', 'portfolio')} user=postgres")
n = c.execute("UPDATE app_user SET password_hash=$1 WHERE email LIKE '%@example.test'", (sec.hash_password(pw),))
print(f"updated {c.rowcount} demo users; password: {pw}")
