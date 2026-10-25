"""Password hashing (Argon2id), JWT access tokens, opaque refresh tokens."""
import base64
import hashlib
import hmac
import os
import secrets
import time

import jwt
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

# OWASP-recommended minimum profile: 19 MiB, t=2, p=1
_MEM_KIB, _ITERS, _LANES, _LEN = 19456, 2, 1, 32

JWT_SECRET = os.environ.get("PF_JWT_SECRET") or secrets.token_urlsafe(48)   # per-process if unset
ACCESS_TTL = 15 * 60
REFRESH_TTL = 7 * 24 * 3600
_ISS = "portfolio-api"


def _kdf(salt):
    return Argon2id(salt=salt, length=_LEN, iterations=_ITERS, lanes=_LANES, memory_cost=_MEM_KIB)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = _kdf(salt).derive(pw.encode())
    b = lambda x: base64.b64encode(x).decode()
    return f"$argon2id$m={_MEM_KIB},t={_ITERS},p={_LANES}${b(salt)}${b(h)}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, algo, _params, salt, h = stored.split("$")
        if algo != "argon2id":
            return False
        _kdf(base64.b64decode(salt)).verify(pw.encode(), base64.b64decode(h))
        return True
    except (ValueError, InvalidKey):
        return False


# Used to spend equal time when the account does not exist (no user enumeration by timing).
DUMMY_HASH = hash_password("dummy-password-for-timing")


def password_ok(pw: str):
    """Policy: 12-128 chars, not all one class. Returns an error string or None."""
    if not 12 <= len(pw) <= 128:
        return "password must be 12-128 characters"
    if len({c.isalpha() for c in pw} | {c.isdigit() for c in pw}) < 2 and pw.isalpha():
        return "password must mix letters with digits or symbols"
    return None


def make_access(user_id: int, roles: list) -> str:
    now = int(time.time())
    return jwt.encode({"sub": str(user_id), "roles": roles, "iat": now, "exp": now + ACCESS_TTL,
                       "iss": _ISS, "jti": secrets.token_hex(8)}, JWT_SECRET, algorithm="HS256")


def read_access(token: str):
    """Returns claims or None. Algorithm is pinned, expiry and issuer enforced."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], issuer=_ISS,
                          options={"require": ["exp", "sub", "iss"]})
    except jwt.PyJWTError:
        return None


def new_refresh() -> tuple:
    """(token given to the client, hash stored in the DB)."""
    t = secrets.token_urlsafe(48)
    return t, hash_refresh(t)


def hash_refresh(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def request_hash(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
