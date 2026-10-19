"""Pluggable data sources: where deploy history and error signals come from.

Picked by env vars, so the same agent can investigate the demo stack or a real
team's stack:

    LOKI_DEPLOYS_SOURCE = file (default) | github
    LOKI_ERRORS_SOURCE  = loki (default) | sentry | datadog

Metrics always come from Prometheus (telemetry.py).
"""
import os
import threading

_instances: dict[tuple, object] = {}
_lock = threading.Lock()


def _cached(key: tuple, make):
    # One instance per configuration: providers keep small caches (diff ranges,
    # default branches) and an HTTP connection pool worth reusing.
    with _lock:
        if key not in _instances:
            _instances[key] = make()
        return _instances[key]


def deploys():
    kind = os.environ.get("LOKI_DEPLOYS_SOURCE", "file").strip().lower() or "file"
    if kind == "file":
        from sources.files import FileDeploys
        return _cached(("deploys", "file"), FileDeploys)
    if kind == "github":
        from sources.github import GitHubDeploys
        return _cached(("deploys", "github", os.environ.get("GITHUB_REPOS", "")), GitHubDeploys.from_env)
    raise ValueError(f"unknown LOKI_DEPLOYS_SOURCE {kind!r}; use file or github")


def errors():
    kind = os.environ.get("LOKI_ERRORS_SOURCE", "loki").strip().lower() or "loki"
    if kind == "loki":
        from sources.grafana_loki import LokiErrors
        return _cached(("errors", "loki"), LokiErrors)
    if kind == "sentry":
        from sources.sentry import SentryErrors
        key = ("errors", "sentry", os.environ.get("SENTRY_ORG", ""), os.environ.get("SENTRY_PROJECTS", ""))
        return _cached(key, SentryErrors.from_env)
    if kind == "datadog":
        from sources.datadog import DatadogErrors
        key = ("errors", "datadog", os.environ.get("DATADOG_SITE", ""), os.environ.get("DATADOG_LOG_QUERY", ""))
        return _cached(key, DatadogErrors.from_env)
    raise ValueError(f"unknown LOKI_ERRORS_SOURCE {kind!r}; use loki, sentry or datadog")


def reset() -> None:
    """Forget provider instances (tests, or after changing env vars)."""
    with _lock:
        _instances.clear()


def parse_mapping(value: str) -> dict[str, str]:
    """"owner/repo=svc,owner/repo2=svc2" -> {"owner/repo": "svc", ...}.
    A bare "owner/repo" maps to its own short name."""
    out = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        key, _, service = item.partition("=")
        out[key.strip()] = service.strip() or key.strip().rsplit("/", 1)[-1]
    return out
