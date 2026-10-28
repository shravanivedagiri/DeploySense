"""Parse, normalize and mask deployment logs into a single event schema."""
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

MAX_LINES = 20000

# ---------- secret masking (runs before anything is stored or sent to the LLM) ----------
_MASKS = [
    (re.compile(r"(?i)\b(\w*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key))(\s*[:=]\s*)(\"?)[^\s\"',;]+"), r"\1\2\3[MASKED]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9\-._~+/]+=*"), "Bearer [MASKED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[MASKED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[MASKED_GITHUB_TOKEN]"),
    (re.compile(r"(?i)(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://([^:@/\s]+):([^@/\s]+)@"), r"\1://\2:[MASKED]@"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[MASKED_EMAIL]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
]


def mask_secrets(text: str) -> str:
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


# ---------- event schema ----------
@dataclass
class Event:
    id: int                      # 1-based line number in the uploaded log
    raw: str
    message: str
    level: str                   # FATAL | ERROR | WARN | INFO | DEBUG
    source: str
    format: str
    component: str
    timestamp: str | None = None
    t: float | None = None       # seconds since first event (filled later)
    level_inferred: bool = False

    def to_dict(self):
        return asdict(self)


_LEVELS = {
    "FATAL": "FATAL", "CRITICAL": "FATAL", "CRIT": "FATAL", "PANIC": "FATAL", "EMERG": "FATAL",
    "ERROR": "ERROR", "ERR": "ERROR", "E": "ERROR",
    "WARN": "WARN", "WARNING": "WARN", "W": "WARN",
    "INFO": "INFO", "NOTICE": "INFO", "I": "INFO",
    "DEBUG": "DEBUG", "TRACE": "DEBUG", "D": "DEBUG",
}

_ERROR_HINTS = re.compile(
    r"(?i)(exception|traceback|\bfailed\b|failure|refused|denied|oomkilled|out of memory|crashloopbackoff|"
    r"imagepullbackoff|errimagepull|backoff|panic:|segmentation fault|cannot |can't |unable to|not found|"
    r"no such|timed out|timeout|exit code [1-9]|exited with code [1-9]|non-zero|fatal:|npm err!|gyp err!|"
    r"\berror\b|err_|unhealthy|does not exist|invalid|unauthorized|forbidden|x509|eaddrinuse|already in use)"
)
_PROMOTE = re.compile(r"(?i)(oomkilled|crashloopbackoff|imagepullbackoff|errimagepull|failed to pull|"
                      r"exceeded its progress deadline|panic:)")
_WARN_HINTS = re.compile(r"(?i)(\bwarn(ing)?\b|deprecated|retrying|retry \d|back-off|probe failed|slow )")

# ---------- format parsers ----------
_ISO = r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
_P_GHA = re.compile(r"^" + _ISO + r"\s+##\[(error|warning|notice|debug|group|endgroup)\](.*)$", re.I)
_P_ISO_LEVEL = re.compile(r"^\[?" + _ISO + r"\]?\s+\[?([A-Za-z]+)\]?\s*(?:\[([^\]]+)\]|([\w.\-/]+):)?\s*(.*)$")
_P_ISO_ONLY = re.compile(r"^\[?" + _ISO + r"\]?\s+(.*)$")
_P_SYSLOG = re.compile(r"^([A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+(\S+)\s+([\w\-./]+)(?:\[\d+\])?:\s*(.*)$")
_P_K8S_KLOG = re.compile(r"^([IWEF])(\d{4} \d{2}:\d{2}:\d{2}\.\d+)\s+\d+\s+([\w.]+:\d+)\]\s*(.*)$")
_P_BUILDKIT = re.compile(r"^#(\d+)\s+(\d+\.\d+)\s+(.*)$")
_P_BUILDKIT_STEP = re.compile(r"^#(\d+)\s+(\[.*?\].*|DONE.*|ERROR.*|CACHED.*)$")
_P_DOCKER_STEP = re.compile(r"^(Step \d+/\d+ :.*)$")
_P_JENKINS = re.compile(r"^(\[Pipeline\].*|ERROR: .*|Finished: (SUCCESS|FAILURE|UNSTABLE|ABORTED))$")
_P_K8S_EVENT = re.compile(r"^\s*(Normal|Warning)\s+(\w+)\s+(\S+)\s+(\S+)\s+(.*)$")
_P_LEVEL_PREFIX = re.compile(r"^\[?(FATAL|CRITICAL|ERROR|ERR|WARN|WARNING|INFO|DEBUG|TRACE)\]?[:\s]\s*(.*)$", re.I)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace(",", ".")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    # trim fractional seconds to 6 digits for fromisoformat
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)
    try:
        dt = datetime.fromisoformat(v.replace(" ", "T", 1))
    except ValueError:
        for fmt in ("%b %d %H:%M:%S", "%m%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(v, fmt).replace(year=2000)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _norm_level(token: str | None) -> str | None:
    if not token:
        return None
    return _LEVELS.get(token.upper())


def _parse_line(line: str):
    """Return (timestamp_str, level, source, message, format, elapsed_seconds)."""
    s = line.rstrip()

    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            ts = obj.get("time") or obj.get("timestamp") or obj.get("ts") or obj.get("@timestamp")
            lvl = obj.get("level") or obj.get("severity") or obj.get("lvl")
            msg = obj.get("msg") or obj.get("message") or obj.get("error") or s
            src = obj.get("logger") or obj.get("service") or obj.get("component") or obj.get("caller") or "app"
            if obj.get("error") and obj.get("msg"):
                msg = f"{obj['msg']}: {obj['error']}"
            return (str(ts) if ts is not None and not isinstance(ts, (int, float)) else None,
                    _norm_level(str(lvl)) if lvl else None, str(src), str(msg), "json", None)
        except (ValueError, TypeError):
            pass

    m = _P_GHA.match(s)
    if m:
        kind = m.group(2).lower()
        lvl = {"error": "ERROR", "warning": "WARN"}.get(kind, "INFO")
        return m.group(1), lvl, "github-actions", m.group(3).strip(), "github-actions", None

    m = _P_K8S_KLOG.match(s)
    if m:
        return None, _norm_level(m.group(1)), m.group(3), m.group(4), "klog", None

    m = _P_ISO_LEVEL.match(s)
    if m and _norm_level(m.group(2)):
        src, msg = m.group(3) or m.group(4) or "app", m.group(5)
        if m.group(4) and re.search(r"(?i)(error|exception|warning)$", m.group(4)):
            src, msg = "app", f"{m.group(4)}: {msg}"  # "Error: ..." is part of the message, not a logger name
        return m.group(1), _norm_level(m.group(2)), src, msg, "timestamped", None

    m = _P_ISO_ONLY.match(s)
    if m:
        rest = m.group(2)
        lp = _P_LEVEL_PREFIX.match(rest)
        if lp:
            return m.group(1), _norm_level(lp.group(1)), "app", lp.group(2), "timestamped", None
        return m.group(1), None, "app", rest, "timestamped", None

    m = _P_SYSLOG.match(s)
    if m:
        return m.group(1), None, m.group(3), m.group(4), "syslog", None

    m = _P_BUILDKIT.match(s)
    if m:
        return None, None, f"build-step-{m.group(1)}", m.group(3), "docker-buildkit", float(m.group(2))
    m = _P_BUILDKIT_STEP.match(s)
    if m:
        lvl = "ERROR" if m.group(2).startswith("ERROR") else None
        return None, lvl, f"build-step-{m.group(1)}", m.group(2), "docker-buildkit", None

    m = _P_DOCKER_STEP.match(s)
    if m:
        return None, "INFO", "docker-build", m.group(1), "docker-build", None

    m = _P_JENKINS.match(s)
    if m:
        lvl = "ERROR" if s.startswith("ERROR") or s.endswith("FAILURE") else "INFO"
        return None, lvl, "jenkins", s, "jenkins", None

    m = _P_K8S_EVENT.match(s)
    if m:
        lvl = "WARN" if m.group(1) == "Warning" else "INFO"
        return None, lvl, m.group(4), f"{m.group(2)}: {m.group(5)}", "k8s-events", None

    lp = _P_LEVEL_PREFIX.match(s)
    if lp:
        return None, _norm_level(lp.group(1)), "app", lp.group(2), "plain", None

    return None, None, "app", s, "plain", None


# ---------- component classification (drives the 3D map) ----------
_COMPONENT_RULES = [
    ("database", r"(?i)(postgres|psql|mysql|mariadb|mongo|redis|sqlalchemy|prisma|sequelize|typeorm|knex|jdbc|"
                 r"\bdb\b|database|:5432|:3306|:27017|:6379|migration|relation \"|no such table|pg_hba)"),
    ("registry", r"(?i)(imagepull|errimagepull|pull access denied|manifest unknown|registry|docker\.io|ghcr\.io|ecr|gcr\.io|"
                 r"repository does not exist|pulling image|failed to pull)"),
    ("build", r"(?i)(build-step|docker-build|step \d+/\d+|dockerfile|npm err|gyp err|node-gyp|pip install|"
              r"\bcopy\b.*failed|failed to compute cache key|did not complete successfully|webpack|compil|"
              r"no matching distribution|could not find a version|eresolve|(?-i:\bRUN\b))"),
    ("config", r"(?i)(environment variable|env var|\benv\b|\bconfig(?:map|uration)?\b|\bsecrets?\b|yaml|\.env\b|missing required|"
               r"keyerror|undefined variable|not set)"),
    ("load-balancer", r"(?i)(ingress|load ?balancer|\belb\b|\balb\b|nginx|502|503|504|bad gateway|upstream|"
                      r"no healthy|target group|traffic)"),
    ("container", r"(?i)(oomkilled|out of memory|exit code|exited|crashloopbackoff|back-off restarting|container|"
                  r"liveness|readiness|probe|restart|killed|sigterm|sigkill|eaddrinuse|address already in use|"
                  r"port is already allocated|bind for )"),
    ("orchestrator", r"(?i)(kubelet|kube-scheduler|default-scheduler|insufficient (cpu|memory)|nodes are available|failedscheduling|"
                     r"replicaset|deployment \"|rollout|helm|scheduled|evicted|nodes? (?:is|are) |node/)"),
]
_COMPILED_COMPONENTS = [(c, re.compile(p)) for c, p in _COMPONENT_RULES]


def classify_component(text: str, source: str, fmt: str) -> str:
    if fmt in ("docker-buildkit", "docker-build"):
        # registry problems can still appear during a build pull
        if _COMPILED_COMPONENTS[1][1].search(text):
            return "registry"
        return "build"
    probe = f"{source} {text}"
    for name, rx in _COMPILED_COMPONENTS:
        if rx.search(probe):
            return name
    if fmt in ("github-actions", "jenkins"):
        return "pipeline"
    return "app"


# ---------- public API ----------
def parse_log(text: str) -> dict:
    """Parse a raw log into normalized events plus extracted errors and stats."""
    text = mask_secrets(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = text.split("\n")
    truncated = len(lines) > MAX_LINES
    lines = lines[:MAX_LINES]

    events: list[Event] = []
    formats: dict[str, int] = {}
    elapsed_base = 0.0
    last_elapsed = None
    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        ts, lvl, src, msg, fmt, elapsed = _parse_line(line)
        inferred = False
        if lvl is None:
            if _ERROR_HINTS.search(msg):
                lvl, inferred = "ERROR", True
            elif _WARN_HINTS.search(msg):
                lvl, inferred = "WARN", True
            else:
                lvl = "INFO"
        elif lvl in ("INFO", "WARN") and _PROMOTE.search(msg):
            lvl, inferred = "ERROR", True
        formats[fmt] = formats.get(fmt, 0) + 1
        ev = Event(id=idx, raw=line, message=msg.strip(), level=lvl, source=src.strip(), format=fmt,
                   component=classify_component(msg, src, fmt), timestamp=ts, level_inferred=inferred)
        if elapsed is not None:
            # buildkit elapsed seconds restart per step; keep them monotonic
            if last_elapsed is not None and elapsed < last_elapsed:
                elapsed_base += last_elapsed
            last_elapsed = elapsed
            ev.t = elapsed_base + elapsed
        events.append(ev)

    time_source = "timestamps" if any(e.timestamp for e in events) else (
        "build elapsed time" if any(e.t is not None for e in events) else "line order")
    _assign_relative_time(events)
    errors = [e for e in events if e.level in ("ERROR", "FATAL")]
    warnings = [e for e in events if e.level == "WARN"]
    return {
        "events": events,
        "errors": errors,
        "warnings": warnings,
        "formats": formats,
        "line_count": len(lines),
        "truncated": truncated,
        "masked_text": text,
        "time_source": time_source,
    }


def _assign_relative_time(events: list[Event]) -> None:
    """Give every event a relative time in seconds. Uses real timestamps when present,
    then build elapsed time, then line order. Gaps are filled from the nearest known neighbour."""
    parsed = [(_parse_ts(e.timestamp) if e.timestamp else None) for e in events]
    known = [p for p in parsed if p]
    if known:
        t0 = min(known)
        for e, p in zip(events, parsed):
            if p:
                e.t = round((p - t0).total_seconds(), 3)
    if all(e.t is None for e in events):
        for i, e in enumerate(events):
            e.t = float(i)
        return
    last = None
    for e in events:
        if e.t is None:
            e.t = last if last is not None else 0.0
        last = e.t


_TEMPLATE_SUBS = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)*\b"), "<n>"),
    (re.compile(r"-[a-z0-9]{5}\b"), "-<id>"),
    (re.compile(r"\s+"), " "),
]


def message_template(msg: str) -> str:
    """Normalize a message so repeated lines with different numbers or ids compare equal."""
    out = msg
    for rx, repl in _TEMPLATE_SUBS:
        out = rx.sub(repl, out)
    return out.strip().lower()


def dedupe_errors(errors: list[Event]) -> list[dict]:
    """Group repeated errors by message template, keeping the first occurrence."""
    groups: dict[str, dict] = {}
    for e in errors:
        key = message_template(e.message)
        g = groups.get(key)
        if g:
            g["count"] += 1
            g["last_id"] = e.id
        else:
            groups[key] = {"first": e, "count": 1, "last_id": e.id}
    return list(groups.values())


def context_window(events: list[Event], line_id: int, radius: int = 5) -> list[Event]:
    idx = next((i for i, e in enumerate(events) if e.id == line_id), None)
    if idx is None:
        return []
    return events[max(0, idx - radius): idx + radius + 1]


def compact_for_llm(parsed: dict, max_groups: int = 25, radius: int = 3) -> str:
    """Build the error excerpt sent to the LLM: deduplicated errors with nearby context, line ids kept."""
    events = parsed["events"]
    groups = dedupe_errors(parsed["errors"])[:max_groups]
    chosen: dict[int, Event] = {}
    for g in groups:
        for ev in context_window(events, g["first"].id, radius):
            chosen[ev.id] = ev
    for w in parsed["warnings"][:10]:
        chosen[w.id] = w
    counts = {g["first"].id: g["count"] for g in groups}
    out = []
    for ev in sorted(chosen.values(), key=lambda x: x.id):
        rep = f"  (repeated {counts[ev.id]}x)" if counts.get(ev.id, 1) > 1 else ""
        out.append(f"L{ev.id} [{ev.level}] [{ev.component}] {ev.message[:400]}{rep}")
    return "\n".join(out)
