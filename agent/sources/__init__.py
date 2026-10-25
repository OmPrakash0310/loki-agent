"""Pluggable data sources: where deploy history and error signals come from.

Picked per tenant (tenant.setting: env vars for the default tenant), so the same
agent can investigate the demo stack or real teams' stacks:

    LOKI_DEPLOYS_SOURCE = file (default tenant's default) | github | none
    LOKI_ERRORS_SOURCE  = loki (default) | sentry | datadog

Instances are cached per tenant and configuration, so tenants never share a client
(and its token). Other tenants have no "file" source: that file is the operator's demo.

Metrics always come from Prometheus (telemetry.py).
"""
import threading

import tenant
from sources.base import DeploysSource

_instances: dict[tuple, object] = {}
_lock = threading.Lock()


class NoDeploys(DeploysSource):
    """A tenant with no deploy history configured."""

    def list_deploys(self, start, end):
        return []

    def get_diff(self, deploy_id):
        raise ValueError(f"no deploy with id {deploy_id!r} (no deploy source configured)")


def _cached(key: tuple, make):
    # One instance per tenant and configuration: providers keep small caches (diff ranges,
    # default branches) and an HTTP connection pool worth reusing.
    key = (tenant.current_id(), *key)
    with _lock:
        if key not in _instances:
            _instances[key] = make()
        return _instances[key]


def deploys():
    default = "file" if tenant.current().is_default else "none"
    kind = tenant.setting("LOKI_DEPLOYS_SOURCE", default).strip().lower() or default
    if kind == "none" or (kind == "file" and not tenant.current().is_default):
        return _cached(("deploys", "none"), NoDeploys)
    if kind == "file":
        from sources.files import FileDeploys
        return _cached(("deploys", "file"), FileDeploys)
    if kind == "github":
        from sources.github import GitHubDeploys
        return _cached(("deploys", "github", tenant.setting("GITHUB_REPOS", "")), GitHubDeploys.from_env)
    raise ValueError(f"unknown LOKI_DEPLOYS_SOURCE {kind!r}; use file, github or none")


def errors():
    kind = tenant.setting("LOKI_ERRORS_SOURCE", "loki").strip().lower() or "loki"
    if kind == "loki":
        from sources.grafana_loki import LokiErrors
        return _cached(("errors", "loki"), LokiErrors)
    if kind == "sentry":
        from sources.sentry import SentryErrors
        key = ("errors", "sentry", tenant.setting("SENTRY_ORG", ""), tenant.setting("SENTRY_PROJECTS", ""))
        return _cached(key, SentryErrors.from_env)
    if kind == "datadog":
        from sources.datadog import DatadogErrors
        key = ("errors", "datadog", tenant.setting("DATADOG_SITE", ""), tenant.setting("DATADOG_LOG_QUERY", ""))
        return _cached(key, DatadogErrors.from_env)
    raise ValueError(f"unknown LOKI_ERRORS_SOURCE {kind!r}; use loki, sentry or datadog")


def reset(tenant_id: int | None = None) -> None:
    """Forget provider instances: all (tests, env changes) or one tenant's (its settings changed)."""
    with _lock:
        for key in [k for k in _instances if tenant_id is None or k[0] == tenant_id]:
            del _instances[key]


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
