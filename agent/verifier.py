"""Loki's verifier: prove a blamed deploy by reproducing the failure.

After Loki blames a deploy, run the image that deploy shipped and the image it
replaced side by side in a throwaway sandbox, replay a request that failed in
production against both, and compare:

    CONFIRMED       the new version fails with the production error, the previous one works
    NOT_REPRODUCED  the versions don't differ that way (both work, or both fail)
    INCONCLUSIVE    the sandbox couldn't answer (no image, no request, timeout, docker error)

The sandbox is its own Docker network created with --internal (no internet and no
route to the live stack), a fresh Postgres seeded with db/init.sql, and the two
images. No secrets go in. It is always torn down, even on errors and timeouts.

Opt-in with LOKI_VERIFY=1. It needs the docker CLI and access to the Docker
socket, which is root-equivalent on the host (see README "Verification").

Run by hand:
    python3 agent/verifier.py --bad IMG --previous IMG --request '{"method": "POST", ...}'
"""
import argparse
import json
import logging
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from redact import redact

log = logging.getLogger("agent")

CONFIRMED = "CONFIRMED"
NOT_REPRODUCED = "NOT_REPRODUCED"
INCONCLUSIVE = "INCONCLUSIVE"
UNVERIFIED = "UNVERIFIED"  # nothing to reproduce (not a deploy)

TIMEOUT_S = float(os.environ.get("LOKI_VERIFY_TIMEOUT_S", "120"))
POSTGRES_IMAGE = os.environ.get("LOKI_VERIFY_POSTGRES_IMAGE", "postgres:16")
INIT_SQL = Path(os.environ.get("LOKI_VERIFY_INIT_SQL") or Path(__file__).resolve().parent.parent / "db" / "init.sql")
# Used when a deploy record names no image, e.g. "loki-demo/{service}:{version}".
IMAGE_TEMPLATE = os.environ.get("LOKI_VERIFY_IMAGE_TEMPLATE", "")
APP_PORT = 8000
# The sandbox's own throwaway database. Never the live one, never a real secret.
APP_ENV = {"DATABASE_URL": "postgresql://app:app@postgres:5432/shop"}
TEARDOWN_TIMEOUT_S = 20
POLL_S = 0.5

# payments-svc logs one of these when a request fails (see its FailedRequestSampler).
SAMPLE_MARKER = "failed request sample: "

LABELS = {
    CONFIRMED: "✅ Verified by reproduction",
    NOT_REPRODUCED: "⚠️ Not reproduced",
    INCONCLUSIVE: "Unverified",
    UNVERIFIED: "Unverified",
}


def enabled() -> bool:
    # Read on every call so tests (and operators) can flip it without a restart.
    return os.environ.get("LOKI_VERIFY", "").strip() == "1"


# ---------- talking to Docker ----------

class DockerError(Exception):
    pass


class SandboxTimeout(Exception):
    pass


def docker_cli(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The one place that touches Docker. Tests swap it for a fake."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


# Runs inside a sandbox container, so nothing needs a published port.
REPLAY_SCRIPT = r"""
import json, sys, urllib.error, urllib.request
method, url, body = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(url, data=body.encode() if body else None, method=method,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        code, text = r.status, r.read()
except urllib.error.HTTPError as e:
    code, text = e.code, e.read()
except Exception as e:
    code, text = 0, repr(e).encode()
print(json.dumps({"status": code, "body": text[:300].decode(errors="replace")}))
"""


class Sandbox:
    """One isolated network with a Postgres and the app containers. Every name
    starts with loki-verify-<run id> and carries the label loki.verify.run=<id>."""

    def __init__(self, docker, deadline: float, run_id: str | None = None):
        self.docker = docker
        self.deadline = deadline
        self.run_id = run_id or secrets.token_hex(4)
        self.prefix = f"loki-verify-{self.run_id}"
        self.network = f"{self.prefix}-net"
        self.containers: list[str] = []
        self.network_created = False

    def _docker(self, *args: str, timeout: float = 30, check: bool = True) -> subprocess.CompletedProcess:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise SandboxTimeout(f"out of time before docker {args[0]}")
        try:
            proc = self.docker(list(args), timeout=min(timeout, left))
        except subprocess.TimeoutExpired:
            raise SandboxTimeout(f"docker {' '.join(args[:2])} did not finish in time")
        if check and proc.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return proc

    def _sleep(self):
        if time.monotonic() + POLL_S >= self.deadline:
            raise SandboxTimeout("gave up waiting")
        time.sleep(POLL_S)

    def require_images(self, *images: str):
        # Never pull: a missing image means we can't say which code ran.
        for image in images:
            if self._docker("image", "inspect", "--format", "{{.Id}}", image, check=False).returncode != 0:
                raise DockerError(f"image {image} is not available on this Docker host")

    def create_network(self):
        self.network_created = True  # set first: a timed-out create may still have made it
        self._docker("network", "create", "--internal", "--label", f"loki.verify.run={self.run_id}", self.network)

    def _common(self, name: str) -> list[str]:
        self.containers.append(name)
        return ["--name", name, "--network", self.network, "--label", f"loki.verify.run={self.run_id}",
                "--memory", "256m", "--cpus", "1"]

    def start_postgres(self, init_sql: Path) -> str:
        name = f"{self.prefix}-db"
        self._docker("create", *self._common(name), "--network-alias", "postgres", "--pull", "never",
                     "-e", "POSTGRES_USER=app", "-e", "POSTGRES_PASSWORD=app", "-e", "POSTGRES_DB=shop",
                     POSTGRES_IMAGE)
        # Copied in rather than bind-mounted, so it also works when the agent runs in a container.
        self._docker("cp", str(init_sql), f"{name}:/docker-entrypoint-initdb.d/init.sql")
        self._docker("start", name)
        return name

    def wait_postgres(self, name: str):
        # -h 127.0.0.1: during init Postgres only listens on its unix socket, so
        # TCP readiness means init.sql has run and the real server is up.
        while self._docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "app", "-d", "shop",
                           timeout=10, check=False).returncode != 0:
            self._sleep()

    def start_app(self, role: str, image: str) -> str:
        name = f"{self.prefix}-{role}"
        env = [arg for k, v in APP_ENV.items() for arg in ("-e", f"{k}={v}")]
        self._docker("run", "-d", *self._common(name), "--pull", "never", *env, image, timeout=60)
        return name

    def call(self, name: str, method: str, path: str, body: str = "") -> dict:
        proc = self._docker("exec", name, "python3", "-c", REPLAY_SCRIPT, method,
                            f"http://127.0.0.1:{APP_PORT}{path}", body, timeout=20, check=False)
        if proc.returncode != 0:
            return {"status": 0, "body": (proc.stderr or proc.stdout).strip()[-300:]}
        try:
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"status": 0, "body": proc.stdout.strip()[-300:]}

    def running(self, name: str) -> bool:
        proc = self._docker("inspect", "--format", "{{.State.Running}}", name, timeout=10, check=False)
        return proc.stdout.strip() == "true"

    def wait_healthy(self, name: str, image: str):
        while self.call(name, "GET", "/health")["status"] != 200:
            if not self.running(name):
                tail = " | ".join(self.logs(name).strip().splitlines()[-3:])
                raise DockerError(f"{image} exited during startup: {tail[-300:]}")
            self._sleep()

    def logs(self, name: str) -> str:
        proc = self._docker("logs", name, timeout=15, check=False)
        return (proc.stdout or "") + (proc.stderr or "")

    def teardown(self):
        """Remove everything this sandbox made. Runs past the deadline on purpose:
        cleaning up matters more than the time cap."""
        def run(*args):
            try:
                self.docker(list(args), timeout=TEARDOWN_TIMEOUT_S)
            except Exception as e:  # keep going: the next step may still succeed
                log.warning("verifier cleanup: docker %s failed: %s", args[0], e)
        if self.containers:
            run("rm", "-f", "-v", *self.containers)
        if self.network_created:
            run("network", "rm", self.network)


# ---------- the verdict ----------

EXCEPTION_LINE = re.compile(r"^[\s|+]*([A-Za-z_][\w.]*(?:Error|Exception)\b(?::.*)?)$")


def error_lines(logs: str) -> list[str]:
    """Exception summaries ("KeyError: 'currency'") and ERROR lines from container logs."""
    out = []
    for line in logs.splitlines():
        m = EXCEPTION_LINE.match(line)
        if m and not m.group(1).startswith(("ExceptionGroup", "BaseExceptionGroup")):
            out.append(m.group(1).strip())
        elif re.search(r"\bERROR\b", line) and SAMPLE_MARKER not in line:
            out.append(line.strip())
    return out


def _norm(text: str) -> str:
    # Ids and amounts vary between requests; the error's shape doesn't.
    return re.sub(r"\d+(\.\d+)?", "<n>", text).strip()


def _first_exception(errors: list[str]) -> str | None:
    return next((e for e in errors if EXCEPTION_LINE.match(e)), errors[0] if errors else None)


def judge(bad: dict, prev: dict, expected_error: str | None = None) -> tuple[str, str]:
    """bad/prev: {"status": HTTP status (0 = no response), "errors": [...]}."""
    bad_failed = bad["status"] == 0 or bad["status"] >= 500
    prev_ok = 0 < prev["status"] < 400
    codes = f"new version HTTP {bad['status']}, previous version HTTP {prev['status']}"
    if bad_failed and prev_ok:
        if expected_error and not any(_norm(expected_error) in _norm(e) for e in bad["errors"]):
            seen = _first_exception(bad["errors"]) or "no error logged"
            return NOT_REPRODUCED, f"the new version fails, but with {seen!r}, not the production error {expected_error!r}"
        what = f"with {expected_error!r}" if expected_error else "with a server error"
        return CONFIRMED, f"the new version fails {what} and the previous version handles the same request ({codes})"
    if 0 < bad["status"] < 400 and prev_ok:
        return NOT_REPRODUCED, (f"both versions handle the request ({codes}): the failure needs something the "
                                "sandbox doesn't have, such as production data, load or a dependency")
    if bad_failed:
        return NOT_REPRODUCED, f"the previous version fails too ({codes}), so this deploy didn't introduce it"
    return NOT_REPRODUCED, f"the new version doesn't fail with a server error ({codes})"


def _request_parts(request: dict) -> tuple[str, str, str]:
    path = request["path"] + (f"?{request['query']}" if request.get("query") else "")
    body = json.dumps(request["body"]) if request.get("body") is not None else ""
    return request["method"].upper(), path, body


def _side(image: str, who: str, out: dict) -> str:
    err = _first_exception(out["errors"]) if out["status"] == 0 or out["status"] >= 500 else None
    return f"{who} {image}: HTTP {out['status'] or 'no response'}" + (f", {err}" if err else "")


def verify(bad_image: str | None, previous_image: str | None, request: dict | None,
           expected_error: str | None = None, *, docker=docker_cli, timeout_s: float = TIMEOUT_S,
           bad_name: str = "new version", previous_name: str = "previous version") -> dict:
    """Reproduce in a sandbox. Never raises: anything that goes wrong is INCONCLUSIVE."""
    started = time.monotonic()

    def result(status, reason, evidence=()):
        return {"status": status, "reason": reason, "evidence": [redact(e)[:400] for e in evidence],
                "seconds": round(time.monotonic() - started, 1),
                "bad_image": bad_image, "previous_image": previous_image}

    if not bad_image or not previous_image:
        missing = bad_name if not bad_image else previous_name
        return result(INCONCLUSIVE, f"no image recorded for the {missing}")
    if not request or not request.get("method") or not str(request.get("path", "")).startswith("/"):
        return result(INCONCLUSIVE, "no failed request to replay")
    if not INIT_SQL.is_file():
        return result(INCONCLUSIVE, f"database seed {INIT_SQL} not found")

    method, path, body = _request_parts(request)
    box = Sandbox(docker, started + timeout_s)
    outcome, failure = None, None
    try:
        box.require_images(bad_image, previous_image, POSTGRES_IMAGE)
        box.create_network()
        db = box.start_postgres(INIT_SQL)
        # The apps boot while Postgres initialises; they only connect per request.
        apps = {"bad": (box.start_app("bad", bad_image), bad_image),
                "prev": (box.start_app("prev", previous_image), previous_image)}
        box.wait_postgres(db)
        for name, image in apps.values():
            box.wait_healthy(name, image)
        outcome = {}
        for role, (name, _) in apps.items():
            out = box.call(name, method, path, body)
            out["errors"] = error_lines(box.logs(name))
            outcome[role] = out
    except SandboxTimeout as e:
        failure = f"sandbox timed out after {timeout_s:.0f}s ({e})"
    except DockerError as e:
        failure = f"sandbox failed: {e}"
    except Exception as e:  # a bug here must not take the investigation down
        log.exception("verifier crashed")
        failure = f"verifier error: {type(e).__name__}: {e}"
    finally:
        box.teardown()

    request_text = f"{method} {path} {body}".strip()
    if failure:
        return result(INCONCLUSIVE, failure, [f"Tried to replay {request_text} in an isolated sandbox"])
    replayed = f"Replayed {request_text} against both versions in an isolated sandbox"
    status, reason = judge(outcome["bad"], outcome["prev"], expected_error)
    return result(status, reason, [replayed, _side(bad_image, bad_name, outcome["bad"]),
                                   _side(previous_image, previous_name, outcome["prev"])])


# ---------- what to verify, from the deploy history and the logs ----------

def parse_sample(line: str) -> dict | None:
    """The request in a 'failed request sample: {...}' log line, or None."""
    i = line.find(SAMPLE_MARKER)
    if i < 0:
        return None
    try:
        sample = json.loads(line[i + len(SAMPLE_MARKER):].strip())
    except ValueError:
        return None
    if not isinstance(sample, dict) or not sample.get("method") or not str(sample.get("path", "")).startswith("/"):
        return None
    return sample


def find_sample(service: str, since, until=None) -> dict | None:
    """The most recent failed-request sample the service logged since `since`."""
    import telemetry as tm  # lazy: the CLI works without the agent's settings
    for _, _, line in reversed(tm.fetch_logs(service, since, until or tm.now(), errors_only=True)):
        sample = parse_sample(line)
        if sample:
            return sample
    return None


def image_for(deploy: dict | None) -> str | None:
    if not deploy:
        return None
    if deploy.get("image"):
        return deploy["image"]
    if IMAGE_TEMPLATE and deploy.get("version"):
        return IMAGE_TEMPLATE.format(service=deploy["service"], version=deploy["version"])
    return None


def find_suspect(deploys: list[dict], deploy_id: str) -> tuple[dict | None, dict | None]:
    """The blamed deploy and the deploy of the same service just before it."""
    deploy = next((d for d in deploys if d["id"] == deploy_id), None)
    if not deploy:
        return None, None
    earlier = [d for d in deploys if d["service"] == deploy["service"]
               and d["deployed_at_dt"] < deploy["deployed_at_dt"]]
    return deploy, max(earlier, key=lambda d: d["deployed_at_dt"], default=None)


def _skip(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "evidence": [], "seconds": 0}


def verify_report(report: dict, docker=docker_cli) -> dict | None:
    """The verification block for a finished report, or None when verification is off."""
    if not enabled():
        return None
    if report.get("trigger") != "deploy" or not report.get("deploy_id"):
        return _skip(UNVERIFIED, "not blamed on a deploy, so there's no version to reproduce")
    try:
        import telemetry as tm
        deploy, previous = find_suspect(tm.load_deploys(), report["deploy_id"])
        if not deploy:
            return _skip(INCONCLUSIVE, f"deploy {report['deploy_id']} isn't in the deploy history")
        if not previous:
            return _skip(INCONCLUSIVE, f"no earlier {deploy['service']} deploy to compare with")
        sample = find_sample(deploy["service"], deploy["deployed_at_dt"])
    except Exception as e:
        log.warning("verifier: couldn't gather what to replay: %s", e)
        return _skip(INCONCLUSIVE, f"couldn't gather what to replay: {type(e).__name__}: {e}")
    log.warning("verifying deploy %s: %s vs %s", deploy["id"], image_for(deploy), image_for(previous))
    return verify(image_for(deploy), image_for(previous), sample, (sample or {}).get("error"), docker=docker,
                  bad_name=f"deploy {deploy['id']}", previous_name=f"previous deploy {previous['id']}")


# ---------- showing it ----------

def label(v: dict | None) -> str:
    """Short status for a footer. No verdict at all keeps the old wording."""
    if not v:
        return "not verified by reproduction yet"
    return LABELS.get(v.get("status"), "Unverified")


def headline(v: dict) -> str:
    text = label(v)
    if v.get("status") in (CONFIRMED, NOT_REPRODUCED):
        return f"{text} in {v.get('seconds', 0):.0f}s: {v.get('reason', '')}"
    return f"{text}: {v.get('reason', '')}"


def main():
    parser = argparse.ArgumentParser(description="Reproduce a failure on two images in a sandbox.")
    parser.add_argument("--bad", required=True, help="image the suspect deploy shipped")
    parser.add_argument("--previous", required=True, help="image it replaced")
    req = parser.add_mutually_exclusive_group(required=True)
    req.add_argument("--request", help='JSON: {"method": "POST", "path": "/charge", "body": {...}}')
    req.add_argument("--request-log", help="a 'failed request sample: {...}' log line")
    parser.add_argument("--expect", help="error signature, e.g. \"KeyError: 'currency'\" (default: the sample's)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    args = parser.parse_args()
    request = json.loads(args.request) if args.request else parse_sample(args.request_log)
    expected = args.expect or (request or {}).get("error")
    print(json.dumps(verify(args.bad, args.previous, request, expected, timeout_s=args.timeout), indent=2,
                     ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
