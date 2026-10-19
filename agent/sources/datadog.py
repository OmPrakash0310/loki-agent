"""Error lines and logs from Datadog. Read-only.

Uses the Logs Search API (POST /api/v2/logs/events/search). Each log becomes a
(time, service, line) tuple, so telemetry.cluster_logs and the onset logic in
context.py work as they do for Grafana Loki logs. Datadog tags each log with its
own `service`, so no mapping is needed.

Env:
  DATADOG_API_KEY + DATADOG_APP_KEY   classic keys (app key needs logs_read_data), or
  DATADOG_TOKEN                       a personal / service access token (Bearer)
  DATADOG_SITE                        datadoghq.com (default), datadoghq.eu, us3.datadoghq.com,
                                      us5.datadoghq.com, ap1.datadoghq.com ...
  DATADOG_LOG_QUERY                   optional extra filter, e.g. "env:production"
  DATADOG_BASE_URL                    optional full API base URL (overrides the site)
"""
import os
import re
from datetime import datetime, timezone

import httpx

from redact import redact
from sources.base import ErrorsSource
from sources.httpkit import RateLimited

PAGE_LIMIT = 1000
MAX_PAGES = 5  # 5000 logs per query; ascending order keeps the earliest (the onset) when it truncates
ERROR_STATUSES = "status:(error OR critical OR alert OR emergency)"
_SITE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_SERVICE = re.compile(r"^[\w.:/-]+$")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _base_url() -> str:
    override = os.environ.get("DATADOG_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    site = (os.environ.get("DATADOG_SITE") or "datadoghq.com").strip().lower()
    if not _SITE.match(site):
        raise ValueError(f"DATADOG_SITE {site!r} is not a valid Datadog site, e.g. datadoghq.com or datadoghq.eu")
    return f"https://api.{site}"


def auth_headers() -> dict:
    """Classic API + application key, or a bearer token. Raises ValueError when neither is set."""
    env = os.environ
    api, app, token = (env.get(k, "").strip() for k in ("DATADOG_API_KEY", "DATADOG_APP_KEY", "DATADOG_TOKEN"))
    if token:
        return {"Authorization": f"Bearer {token}"}
    if api and app:
        return {"DD-API-KEY": api, "DD-APPLICATION-KEY": app}
    raise ValueError("Datadog needs DATADOG_API_KEY and DATADOG_APP_KEY, or DATADOG_TOKEN")


def log_line(attrs: dict) -> str:
    """One text line per log. Prefers the message; falls back to the error attributes APM adds."""
    msg = (attrs.get("message") or "").strip()
    if not msg:
        err = (attrs.get("attributes") or {}).get("error") or {}
        msg = " ".join(str(err[k]) for k in ("kind", "message") if err.get(k))
    status = (attrs.get("status") or "").upper()
    if status in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY") and not msg.upper().startswith(status):
        msg = f"{status} {msg}"
    return redact(msg)


class DatadogErrors(ErrorsSource):
    def __init__(self, client: httpx.Client, extra_query: str = ""):
        self.client = client
        self.extra_query = extra_query.strip()
        self._notes: list[str] = []

    @classmethod
    def from_env(cls):
        headers = auth_headers()
        client = httpx.Client(base_url=_base_url(), headers={**headers, "Content-Type": "application/json"}, timeout=20)
        return cls(client, os.environ.get("DATADOG_LOG_QUERY", ""))

    def notes(self):
        return list(self._notes)

    def _query(self, service, errors_only):
        parts = [ERROR_STATUSES] if errors_only else []
        if service:
            if not _SERVICE.match(service):
                raise ValueError(f"unusual service name {service!r}")
            parts.append(f"service:{service}")
        if self.extra_query:
            parts.append(f"({self.extra_query})")
        return " ".join(parts) or "*"

    def _search(self, query, start, end, max_pages=None, limit=PAGE_LIMIT):
        max_pages = max_pages or MAX_PAGES
        body = {"filter": {"query": query, "from": _iso(start), "to": _iso(end)},
                "sort": "timestamp", "page": {"limit": limit}}
        logs = []
        for _ in range(max_pages):
            resp = self.client.post("/api/v2/logs/events/search", json=body)
            if resp.status_code == 429:
                reset = resp.headers.get("x-ratelimit-reset", "")
                raise RateLimited(int(reset) if reset.isdigit() and int(reset) < 3600 else None)
            resp.raise_for_status()
            data = resp.json()
            logs += data.get("data", [])
            for w in (data.get("meta") or {}).get("warnings") or []:
                self._notes.append(f"Datadog: {w.get('title') or w.get('code') or 'warning'}")
            cursor = ((data.get("meta") or {}).get("page") or {}).get("after")
            if not cursor or not data.get("data"):
                return logs, False
            body["page"] = {"limit": limit, "cursor": cursor}
        return logs, True

    def _lines(self, query, start, end, **kw):
        self._notes = []
        try:
            logs, truncated = self._search(query, start, end, **kw)
        except RateLimited as e:
            self._notes.append(f"Datadog {e}: log data is partial or missing")
            return []
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError):
                code = e.response.status_code
                hint = " (check the keys, and that the app key has logs_read_data)" if code in (401, 403) else ""
                self._notes.append(f"Datadog: could not read logs: HTTP {code}{hint}")
            else:
                self._notes.append(f"Datadog: could not read logs: {type(e).__name__}")
            return []
        if truncated:
            self._notes.append(f"Datadog: more than {len(logs)} matching logs in the window; "
                               "counts are a lower bound, the earliest are kept")
        out = []
        for item in logs:
            attrs = item.get("attributes") or {}
            if not attrs.get("timestamp"):
                continue
            out.append((_ts(attrs["timestamp"]), attrs.get("service") or "?", log_line(attrs)))
        out.sort(key=lambda x: x[0])
        return out

    def error_lines(self, service, start, end):
        return self._lines(self._query(service, True), start, end)

    def all_lines(self, service, start, end):
        return self._lines(self._query(service, False), start, end, max_pages=1, limit=500)

    def logs_present(self, start, end):
        try:
            logs, _ = self._search(self._query(None, False), start, end, max_pages=1, limit=1)
        except (httpx.HTTPError, RateLimited):
            return True  # an unreachable API isn't evidence that the log pipeline is broken
        return bool(logs)
