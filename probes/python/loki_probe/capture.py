"""Turn live objects into small, redacted, JSON-safe values.

Never calls code from the host app: no user __repr__, __eq__, properties or
__getattr__. Built-in scalars are rendered directly, containers are walked,
and other objects show their type plus their instance __dict__ (read with
object.__getattribute__, which bypasses any override).
"""
from .redact import SECRET_NAME, redact

MAX_DEPTH = 3
MAX_ITEMS = 20
MAX_STR = 200

_SCALARS = (bool, int, float, type(None))


def _str(s: str) -> str:
    s = redact(s)
    return s if len(s) <= MAX_STR else s[:MAX_STR] + f"...[+{len(s) - MAX_STR} chars]"


def _key(k) -> str:
    return _str(k) if type(k) is str else (repr(k) if type(k) in _SCALARS else f"<{type(k).__name__}>")


def safe_value(obj, depth: int = 0):
    t = type(obj)
    if t in _SCALARS:
        if t is int and abs(obj) > 10**30:
            return f"<int {obj.bit_length()} bits>"
        return obj
    if t is str:
        return _str(obj)
    if t in (bytes, bytearray):
        return f"<{t.__name__} len={len(obj)}>"
    if depth >= MAX_DEPTH:
        return f"<{t.__name__} ...>"
    if t is dict:
        out = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= MAX_ITEMS:
                out["..."] = f"+{len(obj) - MAX_ITEMS} more"
                break
            ks = _key(k)
            out[ks] = "[REDACTED]" if SECRET_NAME.search(ks) else safe_value(v, depth + 1)
        return out
    if t in (list, tuple, set, frozenset):
        items = list(obj)[:MAX_ITEMS] if t in (set, frozenset) else obj[:MAX_ITEMS]
        out = [safe_value(v, depth + 1) for v in items]
        if len(obj) > MAX_ITEMS:
            out.append(f"...+{len(obj) - MAX_ITEMS} more")
        return out
    # Any other object: its type and its plain attributes, without running its code.
    try:
        attrs = object.__getattribute__(obj, "__dict__")
    except Exception:
        attrs = None
    name = f"<{t.__module__}.{t.__qualname__}>" if t.__module__ != "builtins" else f"<{t.__name__}>"
    if type(attrs) is dict:
        return {"__type__": name, **safe_value(attrs, depth + 1)} if attrs else {"__type__": name}
    return name


def capture_locals(f_locals: dict, wanted: list[str] | None) -> dict:
    names = wanted if wanted else [n for n in f_locals if not n.startswith("__")]
    out = {}
    for n in names:
        if n not in f_locals:
            if wanted:
                out[n] = "<not defined at this line>"
            continue
        out[n] = "[REDACTED]" if SECRET_NAME.search(n) else safe_value(f_locals[n])
    return out


def scalar_equals(value, expected) -> bool:
    """The condition filter: equality on built-in scalars only, so no user __eq__ runs."""
    if type(value) not in (*_SCALARS, str) or type(expected) not in (*_SCALARS, str):
        return False
    if type(value) is bool or type(expected) is bool:
        return type(value) is type(expected) and value == expected
    return value == expected
