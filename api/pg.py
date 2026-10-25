"""PostgreSQL access layer (Windows, Linux, macOS).

Backend: psycopg 3 if installed (recommended: `pip install "psycopg[binary]"`, works on
Windows out of the box); otherwise a ctypes wrapper around libpq (on Windows it looks for
libpq.dll in PF_LIBPQ_DIR, PATH, and C:\\Program Files\\PostgreSQL\\*\\bin).
Force one with PF_PG_DRIVER=psycopg|ctypes.

Both expose the same tiny API: Connection.execute(sql, params) with $1,$2 placeholders,
.one(), .scalar(), .tx(), Pool, PgError(sqlstate, message), and dsn(role).
"""
import json
import os
import re
import sys
import threading
from contextlib import contextmanager


class PgError(Exception):
    def __init__(self, sqlstate, message):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message


# ---------------------------------------------------------------- connection string
def _q(v):
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def dsn(role=None):
    """libpq connection string from the environment.

    PGHOST (default localhost; on Linux a socket dir like /tmp also works), PGPORT (5432),
    PFDB (portfolio). role=None -> superuser PFSUPERUSER (default postgres), password from
    PGPASSWORD. role in {app_role, worker_role, admin_role} -> that role, password from
    PF_ROLE_PASSWORD (omitted if unset, which only works with trust auth).
    """
    parts = {"host": os.environ.get("PGHOST", "localhost"),
             "port": os.environ.get("PGPORT", "5432"),
             "dbname": os.environ.get("PFDB", "portfolio"),
             "user": role or os.environ.get("PFSUPERUSER", "postgres"),
             "client_encoding": "UTF8"}
    if role and os.environ.get("PF_ROLE_PASSWORD"):
        parts["password"] = os.environ["PF_ROLE_PASSWORD"]
    return " ".join(f"{k}={_q(v)}" for k, v in parts.items())


# ---------------------------------------------------------------- shared helpers
_PARAM = re.compile(r"\$(\d+)")


def to_named(sql, params):
    """'... $1 ... $1 ... $2' + (a, b) -> psycopg named style, literal % escaped."""
    q = _PARAM.sub(lambda m: f"%(p{m.group(1)})s", sql.replace("%", "%%"))
    p = {f"p{i}": (json.dumps(v) if isinstance(v, (dict, list)) else v)
         for i, v in enumerate(params, start=1)}
    return q, p


class _Base:
    rowcount = 0

    def one(self, sql, params=()):
        r = self.execute(sql, params)
        return r[0] if r else None

    def scalar(self, sql, params=()):
        r = self.one(sql, params)
        return next(iter(r.values())) if r else None

    @contextmanager
    def tx(self, user_id=None, role=None, request_id=None):
        """One transaction; sets the per-request identity used by RLS and the audit log."""
        self.execute("BEGIN")
        try:
            if user_id is not None:
                self.execute("SELECT set_config('app.user_id', $1, true)", (str(user_id),))
            if role is not None:
                self.execute("SELECT set_config('app.role', $1, true)", (role,))
            if request_id is not None:
                self.execute("SELECT set_config('app.request_id', $1, true)", (request_id,))
            yield self
            self.execute("COMMIT")
        except BaseException:
            try:
                self.execute("ROLLBACK")
            except PgError:
                pass
            raise


# ---------------------------------------------------------------- backend 1: psycopg 3
def _psycopg_backend():
    import psycopg
    from psycopg.rows import dict_row

    class PsycopgConnection(_Base):
        def __init__(self, conninfo):
            try:
                self._c = psycopg.connect(conninfo, autocommit=True, prepare_threshold=None,
                                          row_factory=dict_row)
            except psycopg.Error as e:
                raise PgError(e.sqlstate or "08001", str(e))

        def execute(self, sql, params=()):
            q, p = to_named(sql, params)
            try:
                cur = self._c.execute(q, p)
            except psycopg.Error as e:
                msg = getattr(getattr(e, "diag", None), "message_primary", None) or str(e)
                raise PgError(e.sqlstate or "XX000", msg)
            self.rowcount = cur.rowcount
            return cur.fetchall() if cur.description else []

        def close(self):
            self._c.close()

    return PsycopgConnection


# ---------------------------------------------------------------- backend 2: ctypes + libpq
def _load_libpq():
    import ctypes
    import ctypes.util
    import glob
    if sys.platform == "win32":
        dirs = [os.environ.get("PF_LIBPQ_DIR")] + os.environ.get("PATH", "").split(os.pathsep) + \
               sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
        for d in filter(None, dirs):
            dll = os.path.join(d, "libpq.dll")
            if os.path.exists(dll):
                os.add_dll_directory(d)          # its OpenSSL/ICU dependencies live next to it
                return ctypes.CDLL(dll)
        raise ImportError("libpq.dll not found: set PF_LIBPQ_DIR to your PostgreSQL bin folder "
                          "or `pip install \"psycopg[binary]\"`")
    return ctypes.CDLL(ctypes.util.find_library("pq") or "libpq.so.5")


def _ctypes_backend():
    import ctypes
    from decimal import Decimal
    lib = _load_libpq()
    vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    lib.PQconnectdb.restype, lib.PQconnectdb.argtypes = vp, [cp]
    lib.PQstatus.argtypes = [vp]
    lib.PQerrorMessage.restype, lib.PQerrorMessage.argtypes = cp, [vp]
    lib.PQfinish.argtypes = [vp]
    lib.PQexecParams.restype = vp
    lib.PQexecParams.argtypes = [vp, cp, ci, vp, ctypes.POINTER(cp), vp, vp, ci]
    lib.PQresultStatus.argtypes = [vp]
    lib.PQresultErrorField.restype, lib.PQresultErrorField.argtypes = cp, [vp, ci]
    lib.PQntuples.argtypes = [vp]
    lib.PQnfields.argtypes = [vp]
    lib.PQfname.restype, lib.PQfname.argtypes = cp, [vp, ci]
    lib.PQftype.argtypes = [vp, ci]
    lib.PQgetisnull.argtypes = [vp, ci, ci]
    lib.PQgetvalue.restype, lib.PQgetvalue.argtypes = cp, [vp, ci, ci]
    lib.PQclear.argtypes = [vp]
    lib.PQcmdTuples.restype, lib.PQcmdTuples.argtypes = cp, [vp]
    lib.PQsetClientEncoding.argtypes = [vp, cp]
    ok_status = {1, 2, 6}
    ints, nums, jsons = {20, 21, 23, 26}, {700, 701, 1700}, {114, 3802}

    def conv(oid, raw):
        if oid in ints:
            return int(raw)
        if oid in nums:
            return Decimal(raw)
        if oid == 16:
            return raw == "t"
        if oid in jsons:
            return json.loads(raw)
        return raw

    def enc(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return b"t" if v else b"f"
        if isinstance(v, (dict, list)):
            return json.dumps(v).encode()
        return str(v).encode()

    class CtypesConnection(_Base):
        def __init__(self, conninfo):
            self._c = lib.PQconnectdb(conninfo.encode())
            if lib.PQstatus(self._c) != 0:
                msg = lib.PQerrorMessage(self._c).decode(errors="replace")
                lib.PQfinish(self._c)
                self._c = None
                raise PgError("08001", msg)
            lib.PQsetClientEncoding(self._c, b"UTF8")

        def execute(self, sql, params=()):
            n = len(params)
            arr = (cp * max(n, 1))(*[enc(p) for p in params])
            res = lib.PQexecParams(self._c, sql.encode(), n, None, arr, None, None, 0)
            try:
                if lib.PQresultStatus(res) not in ok_status:
                    code = lib.PQresultErrorField(res, ord("C"))
                    msg = lib.PQresultErrorField(res, ord("M"))
                    raise PgError(code.decode() if code else "XX000",
                                  msg.decode(errors="replace") if msg else
                                  lib.PQerrorMessage(self._c).decode(errors="replace"))
                nt, nf = lib.PQntuples(res), lib.PQnfields(res)
                names = [lib.PQfname(res, j).decode() for j in range(nf)]
                oids = [lib.PQftype(res, j) for j in range(nf)]
                rows = [{names[j]: None if lib.PQgetisnull(res, i, j) else
                         conv(oids[j], lib.PQgetvalue(res, i, j).decode()) for j in range(nf)}
                        for i in range(nt)]
                ct = lib.PQcmdTuples(res)
                self.rowcount = int(ct) if ct else len(rows)
                return rows
            finally:
                lib.PQclear(res)

        def close(self):
            if self._c:
                lib.PQfinish(self._c)
                self._c = None

    return CtypesConnection


def _pick():
    want = os.environ.get("PF_PG_DRIVER", "").lower()
    if want != "ctypes":
        try:
            return _psycopg_backend(), "psycopg"
        except ImportError:
            if want == "psycopg":
                raise
    return _ctypes_backend(), "ctypes"


Connection, DRIVER = _pick()


class Pool:
    """Thread-safe pool of independent connections."""

    def __init__(self, conninfo, size=8):
        self._conninfo, self._items, self._lock = conninfo, [], threading.Lock()
        self._sem = threading.BoundedSemaphore(size)

    @contextmanager
    def connection(self):
        self._sem.acquire()
        with self._lock:
            c = self._items.pop() if self._items else None
        try:
            if c is None:
                c = Connection(self._conninfo)
            yield c
        finally:
            with self._lock:
                self._items.append(c)
            self._sem.release()
