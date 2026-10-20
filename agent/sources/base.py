"""What every source must provide. Plain classes, no framework.

Deploy dicts: {id, service, version, author, message, deployed_at (ISO),
deployed_at_dt, diff}. `diff` may be None for remote sources; get the change
with get_diff(), which fetches (and truncates) it on demand.

Error lines: (datetime, service, line) tuples, oldest first, the same shape
telemetry.fetch_logs returns, so cluster_logs and the onset logic in
context.py work unchanged.
"""
from datetime import datetime


class DeploysSource:
    def list_deploys(self, start: datetime | None, end: datetime | None) -> list[dict]:
        """Deploys in [start, end], oldest first. None means unbounded (as far as the source allows)."""
        raise NotImplementedError

    def get_diff(self, deploy_id: str) -> dict:
        """{deploy_id, service, version, diff}. Raises ValueError for an unknown id."""
        raise NotImplementedError

    def notes(self) -> list[str]:
        """Caveats from the last call (rate limited, partial data), for the model to see."""
        return []


class ErrorsSource:
    def error_lines(self, service: str | None, start: datetime, end: datetime) -> list[tuple]:
        raise NotImplementedError

    def all_lines(self, service: str | None, start: datetime, end: datetime) -> list[tuple] | None:
        """Every log line (not only errors), same shape as error_lines. None = this source can't
        search full logs, so the caller falls back to Grafana Loki."""
        return None

    def logs_present(self, start: datetime, end: datetime) -> bool:
        """False only when the pipeline looks broken (no data at all), not when there are no errors."""
        return True

    def notes(self) -> list[str]:
        return []
