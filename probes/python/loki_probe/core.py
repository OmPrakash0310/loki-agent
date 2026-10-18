"""Non-breaking line probes for a running Python process.

A probe watches ONE file:line. Each time that line is about to run it records
the local variables (redacted, size-capped), the time, the thread and a short
stack, then lets the line run as normal. Probes remove themselves after their
time limit or hit limit, whichever comes first.

Python 3.12+: sys.monitoring LINE events enabled only on the code objects that
contain the line, so nothing else in the process is slowed down and with no
probe active there is no hook at all. Older Pythons fall back to
sys.settrace/threading.settrace (slower, and only threads that call into the
probed code after install, plus the installing thread, are traced).

Safety: every callback is wrapped so nothing can raise into the host app;
failures are counted on the probe. No expression is evaluated and no variable
is ever written.
"""
import gc
import json
import os
import sys
import threading
import time
import traceback
import types
import uuid

from .capture import capture_locals, scalar_equals

DEFAULT_SECONDS = 60
MAX_SECONDS = 300
DEFAULT_HITS = 20
MAX_HITS = 100
HIT_BYTES = 4096        # one hit, serialized
TOTAL_BYTES = 64 * 1024  # all hits of one probe
MAX_ACTIVE = 5
KEEP_FINISHED = 20
STACK_DEPTH = 5

HAS_MONITORING = hasattr(sys, "monitoring")


class ProbeError(ValueError):
    """A probe request was rejected (bad path, line not found, limits)."""


def _roots() -> list[str]:
    raw = os.environ.get("LOKI_PROBE_ROOTS") or os.getcwd()
    return [os.path.realpath(r) for r in raw.split(os.pathsep) if r.strip()]


def resolve_file(file: str, roots: list[str] | None = None) -> str:
    """Map a requested file to a real path inside an allowed root, or raise ProbeError."""
    roots = roots or _roots()
    if not isinstance(file, str) or not file or "\0" in file:
        raise ProbeError("file must be a non-empty path")
    if os.path.isabs(file):
        raise ProbeError("file must be relative to the app directory")
    if not file.endswith(".py"):
        raise ProbeError("only .py files can be probed")
    for root in roots:
        path = os.path.realpath(os.path.join(root, file))
        if os.path.commonpath([root, path]) != root:
            continue  # ../ escape
        parts = path.split(os.sep)
        if "site-packages" in parts or "dist-packages" in parts:
            raise ProbeError("library code can't be probed, only the app's own files")
        if os.path.isfile(path):
            return path
    raise ProbeError(f"{file!r} is not a file under the allowed app directories")


def _code_objects_at(path: str, line: int) -> list[types.CodeType]:
    """Code objects of live functions in `path` whose body includes `line`."""
    seen, found = set(), []

    def visit(code):
        if id(code) in seen:
            return
        seen.add(id(code))
        if any(ln == line for _, _, ln in code.co_lines()):
            found.append(code)
        for c in code.co_consts:
            if isinstance(c, types.CodeType):
                visit(c)

    real: dict[str, bool] = {}  # co_filename -> is it `path`? (realpath is slow; filenames repeat)
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            code = obj.__code__
            name = code.co_filename
            match = real.get(name)
            if match is None:
                try:
                    match = real[name] = os.path.realpath(name) == path
                except Exception:
                    match = real[name] = False
            if match:
                visit(code)
    return found


class Probe:
    def __init__(self, path, rel, line, seconds, max_hits, variables, condition):
        self.id = uuid.uuid4().hex[:12]
        self.path, self.file, self.line = path, rel, line
        self.seconds, self.max_hits = seconds, max_hits
        self.variables = variables
        self.condition = condition
        self.created = time.time()
        self.deadline = time.monotonic() + seconds
        self.hits: list[dict] = []
        self.bytes = 0
        self.errors = 0
        self.skipped = 0  # condition did not match
        self.dropped = 0  # over the byte cap
        self.status = "active"
        self.ended_reason = None
        self.codes: list[types.CodeType] = []
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "id": self.id, "file": self.file, "line": self.line, "status": self.status,
                "ended_reason": self.ended_reason, "seconds": self.seconds, "max_hits": self.max_hits,
                "variables": self.variables, "condition": self.condition,
                "remaining_s": max(0.0, round(self.deadline - time.monotonic(), 1)) if self.status == "active" else 0,
                "hit_count": len(self.hits), "skipped": self.skipped, "dropped": self.dropped,
                "errors": self.errors, "bytes": self.bytes, "hits": list(self.hits),
            }

    # Called on the host app's thread, inside its request. Must be fast and never raise.
    def on_hit(self, frame) -> bool:
        """Record one hit. Returns True when the probe is finished."""
        if self.status != "active":
            return True
        if time.monotonic() >= self.deadline:
            self._end("expired")
            return True
        f_locals = frame.f_locals
        if self.condition is not None:
            name, expected = self.condition["variable"], self.condition["equals"]
            if name not in f_locals or not scalar_equals(f_locals[name], expected):
                with self.lock:
                    self.skipped += 1
                return False
        hit = {
            "ts": time.time(),
            "thread": threading.current_thread().name,
            "locals": capture_locals(f_locals, self.variables),
            "stack": [
                f"{os.path.basename(fs.filename)}:{fs.lineno} in {fs.name}"
                for fs in traceback.extract_stack(frame, limit=STACK_DEPTH)[::-1]
            ],
        }
        data = json.dumps(hit, default=str)
        if len(data) > HIT_BYTES:
            hit = {**hit, "locals": {"__truncated__": data[:HIT_BYTES]}}
            data = json.dumps(hit, default=str)
        with self.lock:
            if self.status != "active":
                return True
            if self.bytes + len(data) > TOTAL_BYTES:
                self.dropped += 1
                self._end_locked("byte_cap")
                return True
            self.hits.append(hit)
            self.bytes += len(data)
            if len(self.hits) >= self.max_hits:
                self._end_locked("max_hits")
                return True
        return False

    def _end(self, reason):
        with self.lock:
            self._end_locked(reason)

    def _end_locked(self, reason):
        if self.status == "active":
            self.status = "done" if reason in ("max_hits", "byte_cap") else reason
            self.ended_reason = reason


class ProbeManager:
    """Owns the hook. One per process (see `manager`)."""

    TOOL_NAME = "loki_probe"

    def __init__(self):
        self._lock = threading.RLock()
        self._probes: dict[str, Probe] = {}
        self._active: dict[str, Probe] = {}
        self._by_code: dict[types.CodeType, list[Probe]] = {}
        self._tool_id = None
        self._timers: dict[str, threading.Timer] = {}
        self.internal_errors = 0

    # ---------- public API ----------

    def add(self, file, line, seconds=None, max_hits=None, variables=None, condition=None, roots=None) -> dict:
        seconds = DEFAULT_SECONDS if seconds is None else seconds
        max_hits = DEFAULT_HITS if max_hits is None else max_hits
        if type(line) is not int or line < 1:
            raise ProbeError("line must be a positive integer")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= MAX_SECONDS:
            raise ProbeError(f"seconds must be between 1 and {MAX_SECONDS}")
        if type(max_hits) is not int or not 0 < max_hits <= MAX_HITS:
            raise ProbeError(f"max_hits must be between 1 and {MAX_HITS}")
        if variables is not None:
            if not isinstance(variables, list) or not all(isinstance(v, str) and v.isidentifier() for v in variables):
                raise ProbeError("variables must be a list of variable names")
            variables = variables[:20] or None
        if condition is not None:
            if (not isinstance(condition, dict) or set(condition) != {"variable", "equals"}
                    or not isinstance(condition["variable"], str) or not condition["variable"].isidentifier()
                    or type(condition["equals"]) not in (str, int, float, bool, type(None))):
                raise ProbeError('condition must be {"variable": <local name>, "equals": <string|number|bool|null>}')
        path = resolve_file(file, roots)
        with self._lock:
            if len(self._active) >= MAX_ACTIVE:
                raise ProbeError(f"at most {MAX_ACTIVE} probes can be active at once")
            codes = _code_objects_at(path, line)
            if not codes:
                raise ProbeError(f"no executable code at {file}:{line} inside a function")
            probe = Probe(path, file, line, seconds, max_hits, variables, condition)
            probe.codes = codes
            self._install(probe)
            self._probes[probe.id] = probe
            self._trim()
        timer = threading.Timer(seconds, self._expire, args=(probe.id,))
        timer.daemon = True
        self._timers[probe.id] = timer
        timer.start()
        return probe.snapshot()

    def get(self, probe_id) -> dict | None:
        probe = self._probes.get(probe_id)
        if probe is None:
            return None
        if probe.status == "active" and time.monotonic() >= probe.deadline:
            self._finish(probe, "expired")
        return probe.snapshot()

    def remove(self, probe_id) -> dict | None:
        with self._lock:
            probe = self._probes.pop(probe_id, None)
        if probe is None:
            return None
        self._finish(probe, "removed")
        return probe.snapshot()

    def list(self) -> list[dict]:
        return [{k: v for k, v in p.snapshot().items() if k != "hits"} for p in list(self._probes.values())]

    def active_hooks(self) -> int:
        """How many code objects currently have a probe hook (0 when idle)."""
        return len(self._by_code)

    def remove_all(self):
        for pid in list(self._probes):
            self.remove(pid)

    # ---------- hook plumbing ----------

    def _install(self, probe):
        self._active[probe.id] = probe
        for code in probe.codes:
            self._by_code.setdefault(code, []).append(probe)
        if HAS_MONITORING:
            mon = sys.monitoring
            if self._tool_id is None:
                self._tool_id = self._claim_tool_id()
                mon.register_callback(self._tool_id, mon.events.LINE, self._mon_line)
            for code in probe.codes:
                mon.set_local_events(self._tool_id, code, mon.events.LINE)
            mon.restart_events()  # re-arm lines an earlier probe DISABLEd
        else:
            threading.settrace(self._global_trace)
            sys.settrace(self._global_trace)

    def _claim_tool_id(self):
        mon = sys.monitoring
        for tid in (mon.DEBUGGER_ID, 5, 4, 3, mon.PROFILER_ID, mon.OPTIMIZER_ID):
            if mon.get_tool(tid) is None:
                mon.use_tool_id(tid, self.TOOL_NAME)
                return tid
        raise ProbeError("no free sys.monitoring tool id (another debugger or profiler is attached)")

    def _finish(self, probe, reason):
        probe._end(reason)
        timer = self._timers.pop(probe.id, None)
        if timer:
            timer.cancel()
        with self._lock:
            if self._active.pop(probe.id, None) is None:
                return
            for code in probe.codes:
                lst = self._by_code.get(code, [])
                if probe in lst:
                    lst.remove(probe)
                if not lst:
                    self._by_code.pop(code, None)
                    if HAS_MONITORING and self._tool_id is not None:
                        try:
                            sys.monitoring.set_local_events(self._tool_id, code, 0)
                        except Exception:
                            self.internal_errors += 1
            if not self._by_code:
                self._release_hook()

    def _release_hook(self):
        if HAS_MONITORING:
            if self._tool_id is not None:
                try:
                    sys.monitoring.register_callback(self._tool_id, sys.monitoring.events.LINE, None)
                    sys.monitoring.free_tool_id(self._tool_id)
                except Exception:
                    self.internal_errors += 1
                self._tool_id = None
        else:
            threading.settrace(None)
            sys.settrace(None)

    def _expire(self, probe_id):
        try:
            probe = self._probes.get(probe_id)
            if probe is not None:
                self._finish(probe, "expired")
        except Exception:
            self.internal_errors += 1

    def _trim(self):
        finished = [p for p in self._probes.values() if p.status != "active"]
        for p in finished[:-KEEP_FINISHED] if len(finished) > KEEP_FINISHED else []:
            self._probes.pop(p.id, None)

    def _dispatch(self, code, line, frame):
        """Shared by both backends. Returns False when this location can be switched off."""
        probes = self._by_code.get(code)
        if not probes:
            return False
        relevant = False
        for probe in list(probes):
            if probe.line != line:
                continue
            relevant = True
            try:
                done = probe.on_hit(frame)
            except Exception:
                with probe.lock:
                    probe.errors += 1
                done = probe.errors >= 10  # a probe that keeps failing removes itself
                if done:
                    probe._end("errors")
            if done:
                try:
                    self._finish(probe, probe.ended_reason or "done")
                except Exception:
                    self.internal_errors += 1
        return relevant

    # sys.monitoring backend
    def _mon_line(self, code, line):
        try:
            if not self._dispatch(code, line, sys._getframe(1)):
                return sys.monitoring.DISABLE  # this line isn't probed: never call back for it again
        except Exception:
            self.internal_errors += 1
        return None

    # settrace fallback
    def _global_trace(self, frame, event, arg):
        try:
            if event == "call" and frame.f_code in self._by_code:
                return self._local_trace
        except Exception:
            self.internal_errors += 1
        return None

    def _local_trace(self, frame, event, arg):
        try:
            if event == "line":
                self._dispatch(frame.f_code, frame.f_lineno, frame)
        except Exception:
            self.internal_errors += 1
        return self._local_trace if self._by_code else None


manager = ProbeManager()
