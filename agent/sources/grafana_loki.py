"""Error lines from Grafana Loki (the default): a thin wrapper over telemetry.fetch_logs."""
import telemetry as tm
from sources.base import ErrorsSource


class LokiErrors(ErrorsSource):
    def error_lines(self, service, start, end):
        return tm.fetch_logs(service, start, end, errors_only=True)

    def logs_present(self, start, end):
        return bool(tm.fetch_logs(None, start, end, errors_only=False, limit=1))
