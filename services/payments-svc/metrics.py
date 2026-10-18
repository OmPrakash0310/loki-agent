"""Shared Prometheus metrics for every service.

Every HTTP request gets counted (with its status code) and timed.
Prometheus scrapes these numbers from GET /metrics every few seconds.
"""
import resource
import sys
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["service", "method", "path", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

MEMORY = Gauge("service_memory_rss_bytes", "Resident memory of the service process", ["service"])


def rss_bytes() -> float:
    """Current resident memory (Linux /proc), or the peak where /proc is missing."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * resource.getpagesize()
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak if sys.platform == "darwin" else peak * 1024


def instrument(app: FastAPI, service: str) -> None:
    MEMORY.labels(service).set_function(rss_bytes)

    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        path = request.url.path
        if path in ("/metrics", "/chaos"):  # the demo's fault switch is not app traffic
            return await call_next(request)
        start = time.perf_counter()
        status = 500  # if the handler crashes, count it as a 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            REQUESTS.labels(service, request.method, path, str(status)).inc()
            LATENCY.labels(service, path).observe(time.perf_counter() - start)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
