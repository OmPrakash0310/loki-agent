"""Error signals from Sentry. Read-only.

Sentry already groups errors into issues; we turn each issue's events in the
window into (time, service, line) tuples, so telemetry.cluster_logs and the
onset logic in context.py work exactly as they do for Grafana Loki logs.

Env: SENTRY_TOKEN, SENTRY_ORG, SENTRY_PROJECTS ("project-slug=service,..."),
SENTRY_BASE_URL (default https://sentry.io; set it for self-hosted or EU).
"""
import os
from datetime import datetime, timezone

import httpx

from redact import redact
from sources import parse_mapping
from sources.base import ErrorsSource
from sources.httpkit import RateLimited, get, next_url

MAX_ISSUES = 20  # per project per query: the top ones by frequency carry the signal
MAX_EVENT_PAGES = 2  # 100 events each; enough for counts and onset, cheap on rate limits


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def issue_line(issue: dict) -> str:
    """One stable text per issue, so all its events cluster into one pattern.
    Shaped like a log line ("ERROR <title> (<culprit>)") for the same reason."""
    level = (issue.get("level") or "error").upper()
    culprit = f" ({issue['culprit']})" if issue.get("culprit") else ""
    return redact(f"{level} [{issue.get('shortId', issue['id'])}] {issue.get('title', '')}{culprit}")


class SentryErrors(ErrorsSource):
    def __init__(self, org: str, projects: dict[str, str], client: httpx.Client):
        self.org = org
        self.projects = projects  # project slug -> service name
        self.client = client
        self._notes: list[str] = []

    @classmethod
    def from_env(cls):
        token, org = os.environ.get("SENTRY_TOKEN", ""), os.environ.get("SENTRY_ORG", "")
        projects = parse_mapping(os.environ.get("SENTRY_PROJECTS", ""))
        if not (token and org and projects):
            raise ValueError("LOKI_ERRORS_SOURCE=sentry needs SENTRY_TOKEN, SENTRY_ORG and SENTRY_PROJECTS")
        client = httpx.Client(base_url=(os.environ.get("SENTRY_BASE_URL") or "https://sentry.io").rstrip("/"),
                              headers={"Authorization": f"Bearer {token}"}, timeout=15)
        return cls(org, projects, client)

    def notes(self):
        return list(self._notes)

    def error_lines(self, service, start, end):
        self._notes = []
        lines = []
        for slug, svc in self.projects.items():
            if service and svc != service:
                continue
            try:
                for issue in self._issues(slug, start):
                    lines += self._issue_lines(issue, svc, start, end)
            except RateLimited as e:
                self._notes.append(f"Sentry {e}: error data is partial, stopped at project {slug}")
                break
            except httpx.HTTPError as e:
                code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
                self._notes.append(f"Sentry: could not read project {slug}: HTTP {code}")
        lines.sort(key=lambda x: x[0])
        return lines

    def _issues(self, slug, start):
        # Issues seen since start, most frequent first. Older-but-quiet issues aren't part of this incident.
        resp = get(self.client, f"/api/0/projects/{self.org}/{slug}/issues/",
                   {"query": f"lastSeen:>={_iso(start)}", "sort": "freq", "limit": MAX_ISSUES})
        return resp.json()[:MAX_ISSUES]

    def _issue_lines(self, issue, svc, start, end):
        line = issue_line(issue)
        url = f"/api/0/organizations/{self.org}/issues/{issue['id']}/events/"
        params = {"start": _iso(start), "end": _iso(end), "full": "false"}
        times, truncated = [], False
        for page in range(MAX_EVENT_PAGES):
            resp = get(self.client, url, params if page == 0 else None)
            times += [_ts(e["dateCreated"]) for e in resp.json() if e.get("dateCreated")]
            url = next_url(resp)
            if not url:
                break
        else:
            truncated = url is not None
        times = [t for t in times if start <= t <= end]
        first_seen = _ts(issue["firstSeen"]) if issue.get("firstSeen") else None
        if truncated:
            self._notes.append(f"Sentry: {issue.get('shortId', issue['id'])} has more than "
                               f"{len(times)} events in the window; count is a lower bound")
            # Events come newest first, so the earliest ones were cut. Keep the onset right.
            if first_seen and start <= first_seen <= end:
                times.append(first_seen)
        if not times and issue.get("lastSeen"):
            # No events listed (sampling, retention): fall back to the issue's own timestamps.
            times = [t for t in {first_seen, _ts(issue["lastSeen"])} if t and start <= t <= end]
        return [(t, svc, line) for t in times]
