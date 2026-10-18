"""Control endpoints for Starlette/FastAPI apps.

    from loki_probe.http import mount
    mount(app)   # no-op unless LOKI_PROBE_TOKEN is set

POST   /_loki/probes        {"file": "main.py", "line": 68, "seconds": 30, "max_hits": 5,
                             "variables": ["payload"], "condition": {"variable": "x", "equals": 1}}
GET    /_loki/probes        list (without hits)
GET    /_loki/probes/{id}   status and hits
DELETE /_loki/probes/{id}   remove now

Every request needs the header X-Loki-Probe-Token: <LOKI_PROBE_TOKEN>.
"""
import hmac
import json
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import ProbeError, manager

HEADER = "x-loki-probe-token"
PREFIX = "/_loki/probes"
MAX_BODY = 4096


def mount(app, token: str | None = None, probe_manager=None) -> bool:
    """Add the probe routes to `app`. Returns False (and adds nothing) when no token is configured."""
    token = token if token is not None else os.environ.get("LOKI_PROBE_TOKEN", "")
    if not token:
        return False
    mgr = probe_manager or manager
    expected = token.encode()

    def denied(request: Request):
        got = request.headers.get(HEADER, "").encode()
        if not hmac.compare_digest(got, expected):
            return JSONResponse({"error": "missing or wrong probe token"}, status_code=401)
        return None

    async def create(request: Request):
        if (resp := denied(request)) is not None:
            return resp
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "request too large"}, status_code=413)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        allowed = {"file", "line", "seconds", "max_hits", "variables", "condition"}
        if set(body) - allowed:
            return JSONResponse({"error": f"unknown fields {sorted(set(body) - allowed)}"}, status_code=400)
        try:
            probe = mgr.add(**{k: body.get(k) for k in allowed if k in body})
        except (ProbeError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(probe, status_code=201)

    async def listing(request: Request):
        if (resp := denied(request)) is not None:
            return resp
        return JSONResponse({"probes": mgr.list(), "active_hooks": mgr.active_hooks()})

    async def one(request: Request):
        if (resp := denied(request)) is not None:
            return resp
        pid = request.path_params["probe_id"]
        probe = mgr.remove(pid) if request.method == "DELETE" else mgr.get(pid)
        if probe is None:
            return JSONResponse({"error": "no such probe"}, status_code=404)
        return JSONResponse(probe)

    app.add_route(PREFIX, create, methods=["POST"], include_in_schema=False)
    app.add_route(PREFIX, listing, methods=["GET"], include_in_schema=False)
    app.add_route(PREFIX + "/{probe_id}", one, methods=["GET", "DELETE"], include_in_schema=False)
    return True
