"""Which metric and label names Loki queries: the demo's, or a real team's.

Every PromQL and LogQL query telemetry.py sends is built here. Pick a preset with
LOKI_METRICS_PRESET, and override single fields with a JSON (or YAML, when
PyYAML is installed) file at LOKI_METRICS_CONFIG:

    {"preset": "spring", "service_label": "job", "dependency_histogram": null}

Presets:
    default       the demo: http_requests_total{service,status}, http_request_duration_seconds,
                  dependency_duration_seconds{dependency}; Grafana Loki label "service"
    otel          OpenTelemetry semantic conventions exported to Prometheus:
                  http_server_request_duration_seconds{service_name,http_response_status_code}
    spring        Spring Boot / Micrometer: http_server_requests_seconds{application,status}
    express-prom  Node.js prom-client via express-prom-bundle: http_request_duration_seconds{job,status_code}

A metric set to null is "not available": its briefing section is skipped with a
note and the tools say so, instead of failing.
"""
import json
import os
import re
from dataclasses import dataclass, fields, replace

METRIC_NAMES = ("error_rate", "p95_latency_seconds", "request_rate")

_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_METRIC = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")


@dataclass(frozen=True)
class MetricsConfig:
    # Prometheus label that names the service (service, job, app, service_name, ...).
    service_label: str = "service"
    # HTTP status label and the regex that means "server error". None: no error rate.
    status_label: str | None = "status"
    status_5xx: str = "5.."
    # A series that counts requests: a counter, or a histogram's _count. None: no error or request rate.
    requests: str | None = "http_requests_total"
    # Request latency histogram, base name (without _bucket). None: no p95 latency.
    latency_histogram: str | None = "http_request_duration_seconds"
    # Outgoing-call latency histogram (database, external APIs) and the label naming the callee.
    dependency_histogram: str | None = "dependency_duration_seconds"
    dependency_label: str = "dependency"
    # Grafana Loki stream label that names the service.
    log_service_label: str = "service"
    # Loki's own service name, never investigated.
    self_service: str = "agent"
    # List services from the request metric only (a real Prometheus has many unrelated jobs).
    # The demo lists every value of the service label, as it always did.
    services_from_requests: bool = False

    def __post_init__(self):
        for name in ("service_label", "dependency_label", "log_service_label"):
            if not _LABEL.match(getattr(self, name) or ""):
                raise ValueError(f"metrics config: {name} {getattr(self, name)!r} is not a valid label name")
        if self.status_label is not None and not _LABEL.match(self.status_label):
            raise ValueError(f"metrics config: status_label {self.status_label!r} is not a valid label name")
        for name in ("requests", "latency_histogram", "dependency_histogram"):
            v = getattr(self, name)
            if v is not None and not _METRIC.match(v):
                raise ValueError(f"metrics config: {name} {v!r} is not a valid metric name")

    # ---------- PromQL ----------

    def _sel(self, service: str, extra: str = "") -> str:
        return "{" + f'{self.service_label}="{_esc(service)}"' + extra + "}"

    def missing_reason(self, metric: str) -> str | None:
        """Why a metric can't be built from this config, or None when it can."""
        if metric not in METRIC_NAMES:
            raise ValueError(f"unknown metric {metric!r}; use one of {list(METRIC_NAMES)}")
        if metric == "p95_latency_seconds":
            return None if self.latency_histogram else "no latency metric configured"
        if not self.requests:
            return "no request counter configured"
        if metric == "error_rate" and not self.status_label:
            return "no status-code label configured"
        return None

    def promql(self, metric: str, service: str) -> str:
        reason = self.missing_reason(metric)
        if reason:
            raise ValueError(f"{metric}: {reason}")
        if metric == "error_rate":
            err = self._sel(service, f',{self.status_label}=~"{_esc(self.status_5xx)}"')
            return (f"(sum(rate({self.requests}{err}[1m])) or vector(0))"
                    f" / sum(rate({self.requests}{self._sel(service)}[1m]))")
        if metric == "p95_latency_seconds":
            return ("histogram_quantile(0.95, sum by (le) "
                    f"(rate({self.latency_histogram}_bucket{self._sel(service)}[1m])))")
        return f"sum(rate({self.requests}{self._sel(service)}[1m]))"

    def dependency_promql(self, service: str) -> str:
        if not self.dependency_histogram:
            raise ValueError("no dependency metric configured")
        return (f"histogram_quantile(0.95, sum by (le, {self.dependency_label}) "
                f"(rate({self.dependency_histogram}_bucket{self._sel(service)}[1m])))")

    def services_match(self) -> str | None:
        """match[] for listing services, or None for every value of the label."""
        if not self.services_from_requests:
            return None
        return self.requests or (f"{self.latency_histogram}_count" if self.latency_histogram else None)

    def families(self) -> dict[str, str | None]:
        """The series whose existence is checked in Prometheus, per section."""
        return {
            "requests": self.requests,
            "latency": f"{self.latency_histogram}_bucket" if self.latency_histogram else None,
            "dependency": f"{self.dependency_histogram}_bucket" if self.dependency_histogram else None,
        }

    # ---------- LogQL ----------

    def log_selector(self, service: str | None) -> str:
        lbl = self.log_service_label
        if service:
            return "{" + f'{lbl}="{_esc(service)}"' + "}"
        return "{" + f'{lbl}=~".+", {lbl}!="{_esc(self.self_service)}"' + "}"


def _esc(value: str) -> str:
    """Escape a value for a double-quoted PromQL/LogQL string (a no-op for normal names)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


PRESETS: dict[str, MetricsConfig] = {
    "default": MetricsConfig(),
    "otel": MetricsConfig(
        service_label="service_name",
        status_label="http_response_status_code",
        requests="http_server_request_duration_seconds_count",
        latency_histogram="http_server_request_duration_seconds",
        dependency_histogram="http_client_request_duration_seconds",
        dependency_label="server_address",
        log_service_label="service_name",
        services_from_requests=True,
    ),
    "spring": MetricsConfig(
        service_label="application",
        status_label="status",
        requests="http_server_requests_seconds_count",
        latency_histogram="http_server_requests_seconds",  # needs percentiles-histogram enabled
        dependency_histogram="http_client_requests_seconds",
        dependency_label="client_name",
        log_service_label="application",
        services_from_requests=True,
    ),
    "express-prom": MetricsConfig(
        service_label="job",
        status_label="status_code",
        requests="http_request_duration_seconds_count",
        latency_histogram="http_request_duration_seconds",
        dependency_histogram=None,
        log_service_label="job",
        services_from_requests=True,
    ),
}

_cache: dict[tuple, MetricsConfig] = {}


def _read_file(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as e:
            raise ValueError("LOKI_METRICS_CONFIG is YAML but PyYAML isn't installed; use a .json file") from e
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LOKI_METRICS_CONFIG must hold an object of settings")
    return data


def load(preset: str | None = None, path: str | None = None) -> MetricsConfig:
    """Build a config from a preset plus optional file overrides."""
    data = _read_file(path) if path else {}
    name = (data.pop("preset", None) or preset or "default").strip().lower()
    if name not in PRESETS:
        raise ValueError(f"unknown metrics preset {name!r}; use one of {', '.join(PRESETS)}")
    known = {f.name for f in fields(MetricsConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown metrics config keys: {', '.join(sorted(unknown))}")
    return replace(PRESETS[name], **data)


def current() -> MetricsConfig:
    """The active config, from LOKI_METRICS_PRESET and LOKI_METRICS_CONFIG (re-read when they change)."""
    preset = os.environ.get("LOKI_METRICS_PRESET", "").strip() or None
    path = os.environ.get("LOKI_METRICS_CONFIG", "").strip() or None
    key = (preset, path, os.path.getmtime(path) if path else None)
    if key not in _cache:
        _cache[key] = load(preset, path)
    return _cache[key]
