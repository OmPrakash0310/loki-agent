"""Code the probe tests attach to. Line numbers matter: tests look them up by marker comment."""


class Opaque:
    def __init__(self):
        self.visible = 1

    def __repr__(self):
        raise RuntimeError("user __repr__ must never run")


def charge(payload, n=0):
    total = payload.get("amount", 0) * 2  # PROBE_CHARGE
    return total + n


def other(x):
    y = x + 1  # PROBE_OTHER
    return y


def secrets(user):
    password = "hunter2"
    api_token = "abc"
    note = "Bearer abc.def.ghi to ops@example.com"
    return len(password + api_token + note + user)  # PROBE_SECRETS


def big(n):
    blob = "x" * 5000
    nested = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    many = list(range(n))
    obj = Opaque()
    return len(blob) + len(many) + len(nested) + obj.visible  # PROBE_BIG


def line_of(marker):
    with open(__file__) as f:
        for i, text in enumerate(f, 1):
            if text.rstrip().endswith("# " + marker):
                return i
    raise LookupError(marker)
