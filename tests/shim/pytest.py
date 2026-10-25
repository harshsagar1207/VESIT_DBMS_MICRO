"""Tiny stand-in for pytest (fixture, raises) used only when real pytest is not installed."""
from contextlib import contextmanager


def fixture(*a, **kw):
    scope = kw.get("scope", "function")
    def deco(fn):
        fn._fixture_scope = scope
        return fn
    if a and callable(a[0]):
        return deco(a[0])
    return deco


class _Info:
    value = None


@contextmanager
def raises(exc):
    info = _Info()
    try:
        yield info
    except exc as e:
        info.value = e
        return
    raise AssertionError(f"DID NOT RAISE {exc.__name__}")
