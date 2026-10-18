"""Secret scrubbing for captured values.

PATTERNS, CARD and _luhn_ok are copied from agent/redact.py so the probe can run
inside a service without the agent's code. Keep them in sync: the agent's test
suite (tests/test_probe_tool.py) fails if the two drift apart.
"""
import re

PATTERNS = [
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+\b"), "[JWT]"),
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]+\b"), "[API_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer [TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)(\s*[=:]\s*)\S+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"postgres(ql)?://[^\s\"']+"), "postgresql://[REDACTED]"),
]


CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(number: str) -> bool:
    """Real card numbers pass the Luhn checksum; timestamps and IDs almost never do."""
    digits = [int(d) for d in number if d.isdigit()]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact(text: str) -> str:
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return CARD.sub(lambda m: "[CARD]" if _luhn_ok(m.group()) else m.group(), text)


# Probe-only: a variable or dict key with one of these names is never captured.
SECRET_NAME = re.compile(r"(?i)(card|cvv|cvc|^pan$|password|passwd|secret|token|api[_-]?key|auth|ssn|iban|cookie|session)")
