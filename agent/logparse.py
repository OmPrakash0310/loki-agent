"""Language-aware reading of error logs: Java/JVM, Node.js/TypeScript, Go and Python.

Log shippers deliver a stack trace either as one multi-line entry or as one
entry per line. Either way we fold a trace back into one event, so a
500-line Java trace counts as one error, not 500. Each event yields:
  - lead: the log line that announced the error ("ERROR ... charge failed"), if any
  - outer / root: the outermost exception and the deepest cause ("Caused by" chains)
  - location: the first application frame (vendor frames skipped), e.g. "src/charge.ts:42"

Plain regexes only: this runs on every error line of every investigation.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime

# Lines of one service arriving this close together can belong to the same trace.
CONTINUATION_GAP_S = 2.0

# ---------- frames: where a trace says the error happened ----------
JAVA_FRAME = re.compile(r"^\s+at\s+(?P<fn>[\w$.<>/\[\]-]+)\((?:(?P<file>[^:()]+):(?P<line>\d+)|[^)]*)\)\s*$")
NODE_FRAME = re.compile(r"^\s+at\s+(?:async\s+)?(?:[^()]*?\()?(?P<path>(?:file://)?[^\s()]+?):(?P<line>\d+):\d+\)?(?:\s*\{)?\s*$")
NODE_FRAME_NO_LOC = re.compile(r"^\s+at\s+\S.*$")  # "at new Promise (<anonymous>)"
PY_FRAME = re.compile(r'^[\s|]*File "(?P<path>[^"]+)", line (?P<line>\d+)')
GO_FILE = re.compile(r"^\s+(?P<path>/\S+\.go):(?P<line>\d+)(?:\s+\+0x[0-9a-f]+)?\s*$")
GO_FUNC = re.compile(r"^(?:created by\s+)?[\w./*()\[\]{}-]+\(.*\)(?:\s+in goroutine \d+)?\s*$|^created by \S+")

# Lines that only ever continue a trace: never the start of a new error.
UNCAUGHT_THREAD = re.compile(r"^Exception in thread ")
JAVA_MORE = re.compile(r"^\s*\.\.\. \d+ (more|common frames omitted)\s*$")
CHAIN = re.compile(r"^\s*(Caused by:|caused by:|\[cause\]:|Suppressed:)\s*")
PY_CHAIN = re.compile(r"^\s*(During handling of the above exception|The above exception was the direct cause)")
PY_TRACEBACK = re.compile(r"^[\s|+]*(Exception Group )?Traceback \(most recent call last\):")
GO_GOROUTINE = re.compile(r"^goroutine \d+ \[[^\]]*\]:\s*$")
GO_MISC = re.compile(r"^(\[signal |exit status \d+$|\s+panic: )")
CARET = re.compile(r"^\s*[\^~]+\s*$")  # Python / Node source pointers
NODE_SRC_HEADER = re.compile(r"^(/\S+|node:\S+):\d+\s*$")  # "/app/dist/charge.js:42" before an uncaught error
NODE_VERSION = re.compile(r"^Node\.js v\d")

# ---------- exception summaries: "Type: message" ----------
EXC = re.compile(
    r"^\s*(?:Caused by:\s*|caused by:\s*|\[cause\]:\s*|Suppressed:\s*|Exception in thread \"[^\"]*\"\s+|Uncaught\s+)?"
    r"(?P<type>(?:[A-Za-z_$][\w$]*[.$])*[\w$]*(?:Error|Exception|Throwable|Rejection|Fault))"
    r"(?:\s*\[[\w-]+\])?(?::\s*(?P<msg>.*?))?\s*\{?\s*$"
)
NODE_REJECTION = re.compile(r"^\[(?P<type>\w+(?:Error|Rejection)): (?P<msg>.*?)\]\s*\{?\s*$")
# Node's rejection text is boilerplate around the one part that matters.
NODE_REASON = re.compile(r'The promise rejected with the reason "(?P<reason>.*)"\.?$')
GO_PANIC = re.compile(r"^\s*(?P<type>panic|fatal error): (?P<msg>.*?)(?:\s*\[recovered\])?\s*$")
GO_HTTP_PANIC = re.compile(r"\bhttp: panic serving \S+: (?P<msg>.*?)\s*$")
PY_SUMMARY = re.compile(r"^(?P<type>(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)(?::\s(?P<msg>.*)|:?)$")
PY_EXC_NAME = re.compile(r"(Error|Exception|Exit|Interrupt|Iteration|Warning|Group|Timeout|Cancelled)$")

# A JVM logging header that is followed by the exception it logs.
JAVA_HEADER = re.compile(r"\s---\s+\[|\bwith root cause\s*$|^\S+\s+\S+\s+\[[^\]]+\]\s+(ERROR|WARN)\s+\S+\s+-\s")

# ---------- vendor code: never the location ----------
JAVA_VENDOR = re.compile(r"^(java|javax|jdk|sun|com\.sun|jakarta|kotlin|kotlinx|scala|org\.springframework|org\.apache"
                         r"|org\.hibernate|org\.postgresql|com\.zaxxer|io\.netty|reactor|com\.fasterxml|ch\.qos"
                         r"|org\.slf4j|org\.junit|io\.micrometer|org\.eclipse|com\.mysql)\.")
PATH_VENDOR = re.compile(r"node_modules|^node:|^internal/|<anonymous>|site-packages|dist-packages|/lib/python\d"
                         r"|<frozen|<string>|^/usr/local/go/|^/usr/lib/go|/go/pkg/mod/|/vendor/|/src/runtime/")
APP_ROOT = re.compile(r"^(?:file://)?(?:/usr/src/app|/home/[^/]+/app|/opt/app|/srv/app|/workspace|/app|/go/src|/src)/")

# ---------- language hints ----------
LANG_MARKERS = {
    "java": re.compile(r"^\s+at [\w$.<>/]+\([\w$]*\.(java|kt|scala|groovy):\d+\)|^Caused by: |^Exception in thread "
                       r"|\s---\s+\[|^\s*\.\.\. \d+ (more|common frames omitted)|\bjava\.(lang|io|net|util)\.\w+"),
    "node": re.compile(r"^\s+at .*\.(m?[jt]sx?|cjs):\d+:\d+\)?|node:internal|UnhandledPromiseRejection|node_modules/"
                       r"|\"level\":\s*[1-6]0\b|^Node\.js v\d|\[cause\]:"
                       # V8 / libuv wording survives even when the filter has dropped every frame
                       r"|Cannot read propert(y|ies) of (undefined|null)|is not a function\b|\bE(CONNREFUSED|CONNRESET|TIMEDOUT)\b"),
    "go": re.compile(r"^goroutine \d+ \[|^panic: |^\s+/\S+\.go:\d+|\.go:\d+\b|\bhttp: panic serving"),
    "python": re.compile(r"Traceback \(most recent call last\)|^[\s|]*File \".*\", line \d+|site-packages/"
                         r"|^(ERROR|INFO|WARNING):\s{2,}"),
}


def detect_language(lines, sample: int = 500) -> str | None:
    """The likeliest language of a stream, from cheap marker regexes. None when nothing says."""
    scores = dict.fromkeys(LANG_MARKERS, 0)
    for text in lines[:sample]:
        for part in text.split("\n"):
            for lang, rx in LANG_MARKERS.items():
                if rx.search(part):
                    scores[lang] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] else None


def short_path(path: str) -> str:
    """'/app/src/charge.ts' -> 'src/charge.ts': drop the deploy root, keep at most 3 parts."""
    path = APP_ROOT.sub("", path).removeprefix("file://")
    return "/".join(path.split("/")[-3:]).lstrip("/")


def frame_location(line: str) -> tuple[str, bool] | None:
    """(file:line, is_application_code) for a stack-frame line, else None."""
    if m := JAVA_FRAME.match(line):
        if not m["file"]:
            return None  # "(Native Method)", "(Unknown Source)": nothing to point at
        fn = m["fn"].rsplit("/", 1)[-1]  # "java.base/java.lang.Thread.run" -> "java.lang.Thread.run"
        return f"{m['file']}:{m['line']}", not JAVA_VENDOR.match(fn)
    for rx in (NODE_FRAME, PY_FRAME, GO_FILE):
        if m := rx.match(line):
            return f"{short_path(m['path'])}:{m['line']}", not PATH_VENDOR.search(m["path"])
    return None


def is_frame(line: str) -> bool:
    return bool(JAVA_FRAME.match(line) or NODE_FRAME_NO_LOC.match(line) or PY_FRAME.match(line) or GO_FILE.match(line))


def summary(line: str, python: bool = False) -> tuple[str, str] | None:
    """(type, "Type: message") for an exception summary line, else None."""
    if python:
        body = re.sub(r"^\s*\|\s?", "", line)  # exception-group blocks are prefixed with "| "
        m = PY_SUMMARY.match(body)
        if m and (m["msg"] is not None or PY_EXC_NAME.search(m["type"])):
            return m["type"], body.strip()
        return None
    for rx in (EXC, NODE_REJECTION, GO_PANIC):
        if m := rx.match(line):
            msg = m["msg"]
            if msg and (r := NODE_REASON.search(msg)):
                msg = r["reason"]
            return m["type"], f"{m['type']}: {msg}" if msg else m["type"]
    if m := GO_HTTP_PANIC.search(line):
        return "panic", f"panic: {m['msg']}"
    return None


# ---------- folding entries into events ----------

@dataclass
class Event:
    t: datetime
    svc: str
    lines: list[str]
    last: datetime
    python: bool = False  # inside a Python traceback: summaries come after frames
    frames: bool = False  # has seen a frame, so indented lines are part of the trace
    awaiting: bool = False  # a header that announces an exception on the next lines
    goroutine: bool = False
    closed: bool = False  # Python summary seen: only a chained traceback may follow
    chained: bool = False  # "The above exception was the direct cause..." just seen


def _continues(ev: Event, line: str) -> bool:
    """Does this line belong to the open trace `ev`? Only explicit trace syntax counts,
    so separate one-line errors (the Python demo's) never merge."""
    if not line.strip() or CARET.match(line):
        return True
    if ev.python:
        if PY_CHAIN.match(line) or (PY_TRACEBACK.match(line) and (ev.chained or not ev.closed)):
            return True
        if ev.closed:
            return False
        return bool(PY_FRAME.match(line) or line[:1].isspace() or line.lstrip().startswith(("|", "+"))
                    or summary(line, python=True))
    if CHAIN.match(line) or JAVA_MORE.match(line) or GO_GOROUTINE.match(line) or GO_MISC.match(line):
        return True
    if NODE_VERSION.match(line) or is_frame(line):
        return True
    if ev.goroutine and GO_FUNC.match(line):
        return True
    if ev.frames and (line[:1].isspace() or line.startswith("}")):
        return True  # Node error properties: "  code: 'ECONNREFUSED'", "}"
    # After a header, the exception it announces (and, for Node, the source excerpt).
    # An uncaught "Exception in thread" is never something a header announced.
    return ev.awaiting and (line[:1].isspace() or (summary(line) is not None and not UNCAUGHT_THREAD.match(line)))


def _add(ev: Event, line: str):
    ev.lines.append(line)
    if line.strip():
        ev.chained = bool(PY_CHAIN.match(line))
    if PY_TRACEBACK.match(line):
        ev.python, ev.closed = True, False
    elif ev.python and not ev.closed and not line[:1].isspace() and not line.startswith("|") and summary(line, python=True):
        ev.closed = True
    if is_frame(line) or GO_GOROUTINE.match(line) or line.rstrip().endswith("{"):
        ev.frames = True  # "{": Node prints the error's properties after it
    if GO_GOROUTINE.match(line):
        ev.goroutine = True
    if summary(line):
        ev.awaiting = False


def _new(t, svc, lines: list[str]) -> Event:
    ev = Event(t, svc, [], t)
    for line in lines:
        _add(ev, line)
    first = lines[0]
    ev.awaiting = len(lines) == 1 and bool(JAVA_HEADER.search(first) or NODE_SRC_HEADER.match(first)) and not summary(first)
    return ev


def fold(lines) -> list[Event]:
    """(time, service, text) entries, oldest first -> events. Consecutive entries of one
    service that continue a trace are folded into it; multi-line entries are one event."""
    events: list[Event] = []
    open_: dict[str, Event] = {}
    for t, svc, text in lines:
        if "\n" in text.strip("\n"):  # a whole trace in one entry (multiline shipping)
            events.append(_new(t, svc, text.strip("\n").split("\n")))
            open_.pop(svc, None)
            continue
        ev = open_.get(svc)
        if ev and (t - ev.last).total_seconds() <= CONTINUATION_GAP_S and _continues(ev, text):
            _add(ev, text)
            ev.last = t
            continue
        ev = _new(t, svc, [text])
        events.append(ev)
        open_[svc] = ev
    return events


# ---------- what an event says ----------

@dataclass
class Parsed:
    lead: str | None = None  # the announcing log line, when it isn't itself the exception
    outer: str | None = None
    root: str | None = None
    error_type: str | None = None
    location: str | None = None


def _sections(lines: list[str], python: bool):
    """[(summary_text, type, [(loc, is_app), ...]), ...] outermost first.
    Python prints frames before each summary and the root cause first; others print
    the summary first and the root cause last."""
    out, pending = [], []
    for line in lines:
        if not line.strip():
            continue
        loc = frame_location(line)
        s = None if loc or (python and line[:1].isspace() and not line.lstrip().startswith("|")) else summary(line, python)
        if python:
            if loc:
                pending.append(loc)
            elif s:
                out.append((s[1], s[0], pending))
                pending = []
            continue
        if s and not (out and out[-1][0] == s[1] and not out[-1][2]):  # "panic: X [recovered]" then "panic: X"
            out.append((s[1], s[0], []))
        elif loc:
            if out:
                out[-1][2].append(loc)
            else:
                pending.append(loc)
    if not python and pending and out:
        out[0][2][:0] = pending
    if python:
        out.reverse()  # outermost first, like the others
        out = [(s, ty, list(reversed(fr))) for s, ty, fr in out]  # innermost frame first
    elif pending and not out:
        out.append((None, None, pending))
    return out


def _first_app(sections) -> str | None:
    for _, _, frames in reversed(sections):  # deepest cause first, then outward
        for loc, app in frames:
            if app:
                return loc
    return None


def parse(lines: list[str], python: bool = False) -> Parsed:
    sections = _sections(lines, python)
    named = [s for s in sections if s[0]]
    p = Parsed()
    if named:
        p.outer, p.root, p.error_type = named[0][0], named[-1][0], named[-1][1]
    p.location = _first_app(sections)
    first = next((l for l in lines if l.strip()), "")
    marker = (is_frame(first) or PY_TRACEBACK.match(first) or GO_GOROUTINE.match(first) or NODE_SRC_HEADER.match(first)
              or CARET.match(first))
    if not marker and not (p.outer and p.outer in first):
        p.lead = first
    return p


# ---------- structured (JSON) log lines ----------

LEVELS = {10: "TRACE", 20: "DEBUG", 30: "INFO", 40: "WARN", 50: "ERROR", 60: "FATAL"}  # pino / bunyan


def json_lines(text: str) -> list[str] | None:
    """A pino / winston / zap / logrus JSON line as plain lines: "LEVEL msg" plus the
    error's own stack, so it goes through the same trace parsing. None if not JSON."""
    if not text.startswith("{"):
        return None
    try:
        rec = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    level = rec.get("level", rec.get("severity", rec.get("lvl", "")))
    level = LEVELS.get(level, str(level)) if isinstance(level, int) else str(level)
    msg = rec.get("msg") or rec.get("message") or ""
    out = [f"{level.upper()} {msg}".strip()]
    err = rec.get("err") or rec.get("error") or rec.get("exception")
    stack = rec.get("stack") or rec.get("stacktrace") or ""
    if isinstance(err, dict):
        stack = err.get("stack") or stack
        if not stack and (err.get("type") or err.get("name")):
            stack = f"{err.get('type') or err.get('name')}: {err.get('message', '')}"
    elif isinstance(err, str) and err:
        out[0] += f": {err}"
    if stack and isinstance(stack, str):
        out += stack.split("\n")
    if rec.get("caller") and not any(frame_location(l) for l in out):
        out.append(f"\t/{rec['caller']}")  # zap's "caller": "payments/charge.go:47"
    return out
