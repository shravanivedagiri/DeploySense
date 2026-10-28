"""Curated error-to-fix knowledge base, pattern matching and placeholder filling."""
import json
import re
from functools import lru_cache
from pathlib import Path

KB_PATH = Path(__file__).parent / "data" / "knowledge_base.json"
PLACEHOLDERS = ("deployment", "namespace", "image", "port", "host", "db_name", "user", "env_var",
                "path", "package", "table", "column", "code", "line")
_PLACEHOLDER_RX = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


@lru_cache(maxsize=1)
def load_kb() -> list[dict]:
    entries = json.loads(KB_PATH.read_text())
    for e in entries:
        e["_compiled"] = [re.compile(p, re.I) for p in e["patterns"]]
    return entries


def public_entry(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def entries_for(plugin: str) -> list[dict]:
    return [e for e in load_kb() if e["plugin"] == plugin]


def match_entry(entry: dict, events) -> tuple[list, dict]:
    """Return (matching events, captured named groups) for one KB entry."""
    hits, captured = [], {}
    required = entry.get("require_component")
    for ev in events:
        if ev.level == "DEBUG":
            continue
        if required and ev.component != required:
            continue
        for rx in entry["_compiled"]:
            m = rx.search(ev.message) or rx.search(ev.raw)
            if m:
                hits.append(ev)
                for k, v in m.groupdict().items():
                    if v and k not in captured:
                        captured[k] = v.strip("\"'")
                break
    return hits, captured


def fill(text: str, values: dict) -> str:
    """Replace known {placeholders}; unknown values become <placeholder> so the user sees what to fill in."""
    return _PLACEHOLDER_RX.sub(lambda m: str(values.get(m.group(1)) or f"<{m.group(1)}>"), text)


def search(query: str, limit: int = 5) -> list[dict]:
    """Keyword search used by the agent's lookup tool."""
    words = [w for w in re.findall(r"[a-z0-9_]{3,}", query.lower())]
    scored = []
    for e in load_kb():
        hay = " ".join([e["id"], e["title"], e["cause"], " ".join(e["patterns"])]).lower()
        score = sum(hay.count(w) for w in words)
        if any(rx.search(query) for rx in e["_compiled"]):
            score += 10
        if score:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [{"id": e["id"], "title": e["title"], "cause": e["cause"], "symptom": e["symptom"],
             "fixes": e["fixes"], "prevention": e["prevention"]} for _, e in scored[:limit]]


_CONTEXT_RULES = {
    "namespace": [r"(?:^|\s)(?:-n|--namespace)[ =]([a-z0-9][a-z0-9-]{0,62})\b", r"namespace[=: /]\"?([a-z0-9][a-z0-9-]{0,62})\"?"],
    "deployment": [r"deployment(?:\.apps)?[ ./]\"?([a-z0-9][a-z0-9-]{1,62})\"?",
                   r"\b(?:pod/)?([a-z][a-z0-9-]*?)-[a-f0-9]{8,10}-[a-z0-9]{5}\b"],
    "image": [r"(?:image|Pulling image|pull image)[ =:]+\"?([\w.\-/]+(?::[\w.\-]+|@sha256:[a-f0-9]{12,}))\"?"],
    "host": [r"(?:DB_HOST|DATABASE_HOST|PGHOST|MYSQL_HOST|REDIS_HOST)[=: ]+\"?([\w.-]+)",
             r"\b([a-z][\w-]*(?:\.[\w-]+)*):(?:5432|3306|27017|6379|1433)\b",
             r"getaddrinfo (?:ENOTFOUND|EAI_AGAIN) ([\w.-]+)"],
    "port": [r"(?:listening|listen|bind|port)[^\n]{0,20}?[: ](\d{2,5})\b"],
    "db_name": [r"(?:DB_NAME|DATABASE_NAME|PGDATABASE|MYSQL_DATABASE)[=: ]+\"?([a-z][\w-]{1,62})\"?",
                r"\"database\"\s*:\s*\"([a-z][\w-]{1,62})\"", r"database \"([a-z][\w-]{1,62})\""],
    "user": [r"(?:DB_USER|PGUSER|MYSQL_USER|for user)[=: ]+\"?'?([\w-]{2,})"],
}
_COMPILED_CONTEXT = {k: [re.compile(p, re.I if k != "deployment" else 0) for p in v] for k, v in _CONTEXT_RULES.items()}
_STOPWORDS = {"default", "the", "is", "not", "does", "has", "rollout", "status", "apps", "exceeded", "progressing"}


def extract_context(events) -> dict:
    """Pull deployment-wide values (names, ports, images) used to make fix commands specific."""
    ctx: dict[str, str] = {}
    for ev in events:
        for key, rules in _COMPILED_CONTEXT.items():
            if key in ctx:
                continue
            for rx in rules:
                m = rx.search(ev.raw)
                if m and m.group(1).lower() not in _STOPWORDS and "[" not in m.group(1):
                    ctx[key] = m.group(1)
                    break
    return ctx
