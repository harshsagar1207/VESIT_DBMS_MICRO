"""Runs the test modules with real pytest if installed, else a minimal runner."""
import importlib, inspect, os, sys, time, traceback

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
try:
    import pytest as _p
    if hasattr(_p, "main"):
        sys.exit(_p.main([here, "-q"] + sys.argv[1:]))
except ImportError:
    sys.path.insert(0, os.path.join(here, "shim"))

import conftest  # noqa

fixtures = {n: f for n, f in vars(conftest).items() if getattr(f, "_fixture_scope", None)}
session = {}
teardowns = []


def get(name, local):
    if name in local:
        return local[name]
    fn = fixtures[name]
    if fn._fixture_scope == "session" and name in session:
        return session[name]
    args = [get(a, local) for a in inspect.signature(fn).parameters]
    val = fn(*args)
    if inspect.isgenerator(val):
        gen = val
        val = next(gen)
        (teardowns if fn._fixture_scope == "session" else local.setdefault("_td", [])).append(gen)
    (session if fn._fixture_scope == "session" else local)[name] = val
    return val


want = [a for a in sys.argv[1:] if not a.startswith("-")]
passed = failed = 0
for fname in sorted(os.listdir(here)):
    if not (fname.startswith("test_") and fname.endswith(".py")):
        continue
    mod = importlib.import_module(fname[:-3])
    for name, fn in vars(mod).items():
        if not name.startswith("test_") or (want and not any(w in name for w in want)):
            continue
        local, t0 = {}, time.time()
        try:
            fn(*[get(a, local) for a in inspect.signature(fn).parameters])
            passed += 1
            print(f"PASS {fname}::{name} ({time.time()-t0:.1f}s)")
        except BaseException:
            failed += 1
            print(f"FAIL {fname}::{name}")
            traceback.print_exc(limit=4)
        for g in local.get("_td", []):
            try: next(g)
            except StopIteration: pass
for g in teardowns:
    try: next(g)
    except StopIteration: pass
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
