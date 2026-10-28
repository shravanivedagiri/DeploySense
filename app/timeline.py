"""Deterministic failure timeline and component propagation map. No AI is used here."""
import re

_MILESTONE = re.compile(r"(?i)(step \d+/\d+|^\[\d+/\d+\]|\bbuilding\b|pulling image|pulled|created container|"
                        r"started container|starting|started|listening|ready|deploying|rollout|scheduled|"
                        r"successfully|connected|migrat|health ?check|finished:|##\[group\]|run |exporting)")

# Fixed deployment topology. Only components present in the log are drawn.
TOPOLOGY_EDGES = [
    ("pipeline", "build"), ("build", "registry"), ("registry", "orchestrator"), ("orchestrator", "container"),
    ("config", "app"), ("container", "app"), ("app", "database"), ("app", "load-balancer"),
]
COMPONENT_LABELS = {
    "pipeline": "CI pipeline", "build": "Image build", "registry": "Registry", "orchestrator": "Scheduler",
    "container": "Container", "config": "Config and secrets", "app": "Application", "database": "Database",
    "load-balancer": "Load balancer",
}
_STATE_RANK = {"ok": 0, "degraded": 1, "failed": 2}


def build_timeline(parsed: dict, findings: list[dict], root: dict | None, max_events: int = 60) -> dict:
    events = parsed["events"]
    finding_by_line: dict[int, dict] = {}
    for f in findings:
        for lid in f["evidence_ids"]:
            finding_by_line.setdefault(lid, f)

    picked = []
    for ev in events:
        significant = (ev.level in ("ERROR", "FATAL", "WARN") or ev.id in finding_by_line
                       or (root is not None and ev.id == root.get("first_line")))
        if significant or _MILESTONE.search(ev.message):
            picked.append(ev)
    # keep every anomaly; thin out milestones if the log is long
    anomalies = [e for e in picked if e.level != "INFO" or e.id in finding_by_line]
    milestones = [e for e in picked if e not in anomalies]
    room = max(0, max_events - len(anomalies))
    if len(milestones) > room:
        step = len(milestones) / room if room else 0
        milestones = [milestones[int(i * step)] for i in range(room)] if room else []
    ordered = sorted(anomalies[:max_events] + milestones, key=lambda e: (e.t, e.id))

    first_anomaly = next((e for e in sorted(events, key=lambda e: (e.t, e.id)) if e.level in ("WARN", "ERROR", "FATAL")), None)
    failures = [e for e in events if e.level in ("ERROR", "FATAL")]
    final_failure = max(failures, key=lambda e: (e.t, e.id)) if failures else None
    root_line = root["first_line"] if root else None

    items = []
    for ev in ordered:
        f = finding_by_line.get(ev.id)
        marks = []
        if root_line == ev.id:
            marks.append("root_cause")
        if first_anomaly and first_anomaly.id == ev.id:
            marks.append("first_anomaly")
        if final_failure and final_failure.id == ev.id:
            marks.append("final_failure")
        items.append({"line": ev.id, "t": ev.t, "level": ev.level, "component": ev.component,
                      "message": ev.message[:240], "finding": f["kb_id"] if f else None,
                      "symptom": f["symptom"] if f else None, "marks": marks})

    return {
        "time_source": parsed["time_source"],
        "duration": round(max((e.t for e in events), default=0) - min((e.t for e in events), default=0), 3),
        "items": items,
        "first_anomaly_line": first_anomaly.id if first_anomaly else None,
        "final_failure_line": final_failure.id if final_failure else None,
        "root_cause_line": root_line,
        "links": _causal_links(findings, root),
        "map": _component_map(events, findings, root),
    }


def _causal_links(findings: list[dict], root: dict | None) -> list[dict]:
    """Order findings by first occurrence. A later symptom is linked to the closest earlier cause."""
    seq = sorted(findings, key=lambda f: (f["first_t"], f["first_line"]))
    links = []
    for i, later in enumerate(seq):
        earlier = [f for f in seq[:i] if not f["symptom"]]
        if not earlier:
            continue
        cause = next((f for f in earlier if root and f["kb_id"] == root.get("kb_id")), earlier[-1])
        links.append({"from": cause["kb_id"], "to": later["kb_id"],
                      "from_line": cause["first_line"], "to_line": later["first_line"],
                      "basis": "known downstream symptom" if later["symptom"] else "occurred later",
                      "explanation": None})
    return links


def _component_map(events, findings, root) -> dict:
    present = {e.component for e in events}
    if root and root.get("component"):
        present.add(root["component"])
    present.add("app")
    states: dict[str, list] = {c: [] for c in present}
    current = {c: "ok" for c in present}
    for ev in sorted(events, key=lambda e: (e.t, e.id)):
        new = {"WARN": "degraded", "ERROR": "failed", "FATAL": "failed"}.get(ev.level)
        if new and _STATE_RANK[new] > _STATE_RANK[current[ev.component]]:
            current[ev.component] = new
            states[ev.component].append({"t": ev.t, "state": new, "line": ev.id})
    nodes = [{"id": c, "label": COMPONENT_LABELS.get(c, c), "changes": states[c],
              "root_cause": bool(root and root["component"] == c),
              "line_count": sum(1 for e in events if e.component == c)} for c in present]
    edges = [{"from": a, "to": b} for a, b in TOPOLOGY_EDGES if a in present and b in present]
    linked = {x for e in edges for x in (e["from"], e["to"])}
    edges += [{"from": c, "to": "app"} for c in sorted(present) if c != "app" and c not in linked]
    return {"nodes": nodes, "edges": edges}
