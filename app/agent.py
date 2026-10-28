"""The single troubleshooting agent. It decides which plugins to call, combines their findings,
and returns one validated report. Facts (parsing, ordering, risk) come from code; the LLM reasons and explains."""
import json
import re
import time
import uuid

from . import knowledge_base as kb
from .llm import GroqClient, LLMError, parse_json
from .log_analyzer import compact_for_llm, parse_log
from .log_diff import compare
from .plugins import PLUGINS, PLUGIN_BY_NAME
from .safe_fix import validate_all
from .timeline import COMPONENT_LABELS, build_timeline

MAX_AGENT_STEPS = 6
COMPONENTS = ["pipeline", "build", "registry", "orchestrator", "container", "config", "app", "database", "load-balancer"]

SYSTEM_PROMPT = """You are DeploySense, a deployment troubleshooting agent for DevOps and IT maintenance teams.
You receive error lines from a failed deployment log. Each line starts with its line id, for example L42.

Work like this:
1. Call the plugin tools that are relevant to the errors. Call general_analysis for anything outside Docker and databases.
2. Use search_knowledge_base or get_log_lines only if you need more information.
3. Decide the single most probable root cause. The root cause is the EARLIEST event that explains the later errors. Errors such as CrashLoopBackOff, failed health probes, 502/503 and non-zero exit codes are usually symptoms, not causes.
4. Reply with one JSON object and nothing else.

Rules:
- Every claim must be supported by line ids that appear in the log excerpt or tool results. Never invent line ids.
- If a plugin finding matches the root cause, set root_cause.kb_id to its kb_id and reuse its fixes.
- Fixes must be specific: exact commands with names, ports and values taken from the log. Do not write generic advice such as "check your configuration".
- Prefer read-only diagnostic commands first, then the change that fixes it. Never propose deleting data or resources as the first fix.
- If the evidence is not enough to be sure, say so in open_questions and give the commands that would confirm it.

JSON format:
{
  "summary": "2 to 4 plain sentences: what failed, where, the probable cause, and the impact",
  "root_cause": {"title": "short title", "explanation": "why this caused the failure, citing lines", "kb_id": "kb id or null", "component": "one of: %s", "evidence_lines": [line ids]},
  "contributing_factors": [{"description": "...", "evidence_lines": [line ids]}],
  "fixes": [{"title": "...", "command": "single shell command", "verify": "how to confirm it worked", "why": "one sentence"}],
  "timeline_explanations": [{"from": "kb id", "to": "kb id", "explanation": "one sentence on how the earlier event led to the later one"}],
  "prevention": "one sentence",
  "open_questions": ["..."]
}""" % ", ".join(COMPONENTS)


def _tool_defs() -> list[dict]:
    tools = [{"type": "function", "function": {
        "name": p.name, "description": p.description,
        "parameters": {"type": "object", "properties": {}, "required": []}}} for p in PLUGINS]
    tools.append({"type": "function", "function": {
        "name": "search_knowledge_base",
        "description": "Search the curated error-to-fix knowledge base with an error message or keywords.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}})
    tools.append({"type": "function", "function": {
        "name": "get_log_lines",
        "description": "Read raw log lines by line id range (at most 40 lines) to see more context.",
        "parameters": {"type": "object", "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}},
                       "required": ["start", "end"]}}})
    return tools


def _finding_for_llm(f: dict) -> dict:
    return {"kb_id": f["kb_id"], "title": f["title"], "cause": f["cause"], "is_symptom": f["symptom"],
            "first_line": f["first_line"], "evidence_lines": f["evidence_ids"], "occurrences": f["evidence_count"],
            "details": f["details"], "fixes": [{"title": x["title"], "command": x["command"]} for x in f["fixes"]]}


def rank_findings(findings: list[dict]) -> list[dict]:
    """Deterministic ranking: real causes before symptoms, then earliest first, then most evidence."""
    return sorted(findings, key=lambda f: (f["symptom"], f["first_t"], f["first_line"], -f["evidence_count"]))


def _fmt_t(t) -> str:
    return f"{round(t, 1):g}s" if t is not None else "unknown time"


def _line_arg(value) -> int | None:
    """Accept a line id as 12, 12.0, "12" or "L12". Anything else is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.fullmatch(r"\s*[Ll]?(\d+)\s*", str(value)) if isinstance(value, str) else None
    return int(m.group(1)) if m else None


def clean_ai_answer(raw) -> dict | None:
    """Coerce the model's JSON into the expected shape. Returns None if it cannot be used,
    so the rule engine answers instead. Line ids are still checked against the log later."""
    if not isinstance(raw, dict) or not isinstance(raw.get("root_cause"), dict):
        return None

    def text(v) -> str:
        return str(v).strip() if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""

    def lines(v) -> list:
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            return re.findall(r"\d+", str(v))
        return [x for x in v if isinstance(x, (str, int)) and not isinstance(x, bool)] if isinstance(v, list) else []

    def dicts(v) -> list[dict]:
        return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    rc = raw["root_cause"]
    kb_id = text(rc.get("kb_id"))
    return {
        "summary": text(raw.get("summary")),
        "root_cause": {"title": text(rc.get("title")), "explanation": text(rc.get("explanation")),
                       "kb_id": kb_id if kb_id.lower() not in ("", "null", "none") else None,
                       "component": text(rc.get("component")), "evidence_lines": lines(rc.get("evidence_lines"))},
        "contributing_factors": [{"description": text(c.get("description")), "evidence_lines": lines(c.get("evidence_lines"))}
                                 for c in dicts(raw.get("contributing_factors"))],
        "fixes": [{"title": text(f.get("title")), "command": text(f.get("command")), "verify": text(f.get("verify")),
                   "why": text(f.get("why")) or None} for f in dicts(raw.get("fixes"))],
        "timeline_explanations": [{"from": text(x.get("from")), "to": text(x.get("to")),
                                   "explanation": text(x.get("explanation"))} for x in dicts(raw.get("timeline_explanations"))],
        "prevention": text(raw.get("prevention")) or None,
        "open_questions": [text(q) for q in raw.get("open_questions") or [] if text(q)]
        if isinstance(raw.get("open_questions"), list) else [],
    }


class TroubleshootingAgent:
    def __init__(self, llm: GroqClient | None = None):
        self.llm = llm or GroqClient()

    # ---------- entry point ----------
    def analyze(self, failed_log: str, success_log: str | None = None, use_llm: bool = True) -> dict:
        started = time.perf_counter()
        parsed = parse_log(failed_log)
        success = parse_log(success_log) if success_log and success_log.strip() else None
        context = kb.extract_context(parsed["events"])
        trace = [{"step": "log_analyzer", "detail": f"{len(parsed['events'])} lines parsed, "
                  f"{len(parsed['errors'])} errors, {len(parsed['warnings'])} warnings, "
                  f"formats: {', '.join(parsed['formats']) or 'none'}"}]

        relevance = {p.name: p.can_handle(parsed) for p in PLUGINS}
        cache: dict[str, list[dict]] = {}

        def run_plugin(name: str) -> list[dict]:
            if name not in cache:
                cache[name] = PLUGIN_BY_NAME[name].analyze(parsed, context)
            return cache[name]

        diff = compare(parsed, success) if success else None
        base = {
            "id": uuid.uuid4().hex[:12],
            "stats": {"lines": parsed["line_count"], "events": len(parsed["events"]), "errors": len(parsed["errors"]),
                      "warnings": len(parsed["warnings"]), "formats": parsed["formats"], "truncated": parsed["truncated"],
                      "time_source": parsed["time_source"]},
            "context": context, "plugin_relevance": relevance, "diff": diff,
            "events": [e.to_dict() for e in parsed["events"]],
        }

        if not parsed["errors"] and not parsed["warnings"]:
            return {**base, "status": "no_errors", "mode": "rules", "trace": trace,
                    "summary": "No error or warning lines were found in this log. Upload the log from the failed run, "
                               "or include the container logs with kubectl logs --previous.",
                    "root_cause": None, "findings": [], "fixes": [], "confidence": None,
                    "timeline": build_timeline(parsed, [], None), "elapsed_ms": _ms(started)}

        # Run every applicable plugin deterministically. This is the ground truth the LLM answer is checked against.
        for p in PLUGINS:
            run_plugin(p.name)
        rule_findings = rank_findings([f for fs in cache.values() for f in fs])
        matched_ids = {lid for f in rule_findings for lid in f["evidence_ids"]}
        unmatched = PLUGIN_BY_NAME["general_analysis"].unmatched(parsed, matched_ids)
        llm_used, llm_error, ai = False, None, None

        if use_llm and self.llm.available:
            try:
                raw, agent_trace = self._run_llm_agent(parsed, diff, run_plugin, unmatched)
                ai = clean_ai_answer(raw)
                if ai is not None:
                    trace += agent_trace
                    llm_used = True
                elif raw is None:
                    llm_error = "the model did not return valid JSON"
                else:
                    llm_error = "the model's answer did not match the required format"
            except LLMError as exc:
                llm_error = str(exc)
            except Exception as exc:  # malformed model output must never turn into a 500; the rule engine answers instead
                llm_error = f"unexpected model output ({type(exc).__name__})"
        if not llm_used:
            for p in PLUGINS:
                if p.name in cache:
                    trace.append({"step": p.name, "detail": f"{len(cache[p.name])} finding(s)", "by": "rules"})
            if llm_error:
                trace.append({"step": "llm", "detail": f"LLM unavailable, used rule-based reasoning ({llm_error[:160]})"})
            elif use_llm:
                trace.append({"step": "llm", "detail": "No GROQ_API_KEY set, used rule-based reasoning"})

        report = self._assemble(parsed, rule_findings, unmatched, ai, context)
        rc = report["root_cause"]
        timeline = build_timeline(parsed, rule_findings, rc if rc.get("first_line") else None)
        self._explain_links(timeline, rule_findings, ai)

        return {**base, **report, "status": "ok", "mode": "llm" if llm_used else "rules",
                "llm_model": self.llm.model if llm_used else None, "llm_error": llm_error,
                "trace": trace, "timeline": timeline, "unmatched_errors": unmatched, "elapsed_ms": _ms(started)}

    # ---------- LLM agent loop ----------
    def _run_llm_agent(self, parsed, diff, run_plugin, unmatched):
        excerpt = compact_for_llm(parsed)
        user = [f"Detected log formats: {', '.join(parsed['formats'])}. Time source: {parsed['time_source']}.",
                f"Available plugins and relevance scores: " + ", ".join(f"{p.name}={p.can_handle(parsed)}" for p in PLUGINS),
                "Error excerpt (deduplicated, with context):", excerpt]
        if diff:
            user.append("Compared with the last successful run:\n" + json.dumps(
                {"new_in_failed": diff["new_in_failed"][:12], "missing_from_failed": diff["missing_from_failed"][:8],
                 "version_changes": diff["version_changes"]}, indent=1))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(user)}]
        tools = _tool_defs()
        events_by_id = {e.id: e for e in parsed["events"]}
        trace = []

        for _ in range(MAX_AGENT_STEPS):
            t0 = time.perf_counter()
            msg = self.llm.chat(messages, tools=tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                result = parse_json(msg.get("content"))
                if result is None:
                    messages.append({"role": "assistant", "content": msg.get("content") or ""})
                    messages.append({"role": "user", "content": "Reply with the JSON object only, in the required format."})
                    retry = self.llm.chat(messages, json_mode=True)
                    result = parse_json(retry.get("content"))
                trace.append({"step": "final_answer", "detail": "Agent produced its diagnosis", "by": "llm",
                              "ms": _ms(t0)})
                return result, trace
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = str((call.get("function") or {}).get("name"))
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                if name in PLUGIN_BY_NAME:
                    found = run_plugin(name)
                    payload = {"findings": [_finding_for_llm(f) for f in found]}
                    if name == "general_analysis":
                        payload["unmatched_errors"] = unmatched
                    detail = f"{len(found)} finding(s)"
                elif name == "search_knowledge_base":
                    payload = {"results": kb.search(str(args.get("query", "")))}
                    detail = f"query \"{str(args.get('query', ''))[:60]}\", {len(payload['results'])} result(s)"
                elif name == "get_log_lines":
                    s, e = _line_arg(args.get("start")), _line_arg(args.get("end"))
                    if s is None:
                        payload, detail = {"error": "start must be a line id such as 12 or L12"}, "invalid line range"
                    else:
                        e = min(e if e is not None and e >= s else s, s + 40)
                        payload = {"lines": [f"L{i} {events_by_id[i].raw[:300]}" for i in range(s, e + 1) if i in events_by_id]}
                        detail = f"lines {s} to {e}"
                else:
                    payload, detail = {"error": f"unknown tool {name}"}, "unknown tool"
                trace.append({"step": name, "detail": detail, "by": "llm", "ms": _ms(t0)})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(payload)[:12000]})

        messages.append({"role": "user", "content": "Stop calling tools. Reply with the JSON object now."})
        final = self.llm.chat(messages, json_mode=True)
        trace.append({"step": "final_answer", "detail": "Agent produced its diagnosis after the step limit", "by": "llm"})
        return parse_json(final.get("content")), trace

    # ---------- combine, verify, score ----------
    def _assemble(self, parsed, findings, unmatched, ai, context) -> dict:
        valid_ids = {e.id for e in parsed["events"]}
        by_kb = {f["kb_id"]: f for f in findings}
        rule_root = findings[0] if findings else None
        dropped = 0

        def clean_lines(lines):
            nonlocal dropped
            out = []
            for x in lines or []:
                try:
                    n = int(str(x).lstrip("Ll"))
                except ValueError:
                    dropped += 1
                    continue
                if n in valid_ids:
                    out.append(n)
                else:
                    dropped += 1
            return sorted(set(out))

        reasons = []
        if ai:
            rc = ai.get("root_cause") or {}
            ai_lines = clean_lines(rc.get("evidence_lines"))
            chosen = by_kb.get(rc.get("kb_id"))
            if chosen:
                root = {**chosen, "explanation": rc.get("explanation") or chosen["cause"], "source": "knowledge_base + ai",
                        "evidence_ids": sorted(set(chosen["evidence_ids"]) | set(ai_lines))[:12]}
            else:
                first = min(ai_lines) if ai_lines else None
                root = {"kb_id": None, "title": rc.get("title") or "Probable root cause",
                        "explanation": rc.get("explanation") or "", "cause": rc.get("explanation") or "",
                        "component": rc.get("component") if rc.get("component") in COMPONENTS else "app",
                        "evidence_ids": ai_lines, "first_line": first, "symptom": False,
                        "severity": "blocked", "prevention": ai.get("prevention"), "fixes": [], "source": "ai",
                        "first_t": next((e.t for e in parsed["events"] if e.id == first), None)}
            summary = ai.get("summary") or ""
            fixes = list(root.get("fixes") or [])
            known_cmds = {f["command"].strip() for f in fixes}
            for f in ai.get("fixes") or []:
                cmd = str(f.get("command") or "").strip()
                if cmd and cmd not in known_cmds and len(fixes) < 8:
                    fixes.append({"title": str(f.get("title") or "Suggested step"), "command": cmd,
                                  "verify": str(f.get("verify") or ""), "why": f.get("why"), "source": "ai"})
                    known_cmds.add(cmd)
            contributing = [{"description": c.get("description", ""), "evidence_ids": clean_lines(c.get("evidence_lines"))}
                            for c in (ai.get("contributing_factors") or []) if c.get("description")]
            open_q = [str(q) for q in (ai.get("open_questions") or [])][:5]
            agrees = bool(rule_root and root.get("kb_id") == rule_root["kb_id"])
            if rule_root and not agrees:
                reasons.append(f"The rule engine ranked \"{rule_root['title']}\" first; the AI chose differently.")
            elif agrees:
                reasons.append("The AI agent and the rule engine agree on the root cause.")
        else:
            agrees = True
            if rule_root:
                root = {**rule_root, "explanation": rule_root["cause"], "source": "knowledge_base"}
                fixes = list(rule_root["fixes"])
                symptoms = [f for f in findings[1:] if f["symptom"]]
                causes = [f for f in findings[1:] if not f["symptom"]]
                where = f" in the {COMPONENT_LABELS.get(root['component'], root['component']).lower()}" if root["component"] != "app" else ""
                summary = (f"The deployment failed{where}. Probable root cause: {root['title'].lower()}, first seen "
                           f"at line {root['first_line']} ({_fmt_t(root['first_t'])} into the log), backed by "
                           f"{root['evidence_count']} matching log line{'s' if root['evidence_count'] != 1 else ''}.")
                if symptoms:
                    summary += " Later errors (" + ", ".join(s["title"].lower() for s in symptoms[:3]) + ") are downstream effects."
                contributing = [{"description": f"{c['title']}: {c['cause']}", "evidence_ids": c["evidence_ids"]} for c in causes[:3]]
            else:
                first = unmatched[0] if unmatched else None
                root = {"kb_id": None, "title": "Unrecognized error", "component": first["component"] if first else "app",
                        "explanation": f"No rule matched. The earliest error is at line {first['line']}: {first['message']}" if first else "",
                        "cause": "", "evidence_ids": [first["line"]] if first else [], "first_line": first["line"] if first else None,
                        "first_t": first["t"] if first else None, "symptom": False, "severity": "blocked",
                        "prevention": None, "source": "fallback", "fixes": []}
                fixes = [{"title": "Read the logs of the previous container run",
                          "command": kb.fill("kubectl logs deployment/{deployment} -n {namespace} --previous --tail=200", context),
                          "verify": "Find the first error before the process exits.", "source": "fallback"},
                         {"title": "Check recent cluster events for the deployment",
                          "command": kb.fill("kubectl get events -n {namespace} --sort-by=.lastTimestamp", context),
                          "verify": "Look for Warning events near the failure time.", "source": "fallback"}]
                summary = ("The log contains errors that no rule recognizes. The earliest one is shown as the probable "
                           "cause. AI reasoning is off; set GROQ_API_KEY on the server to analyse unknown errors.")
                contributing = []
            open_q = []

        # Confidence is computed from verifiable signals, never taken from the model.
        has_kb = bool(root.get("kb_id"))
        has_evidence = bool(root.get("evidence_ids"))
        if has_kb:
            reasons.append(f"Matched knowledge base rule \"{root['kb_id']}\" on {len(root['evidence_ids'])} log line(s).")
        if not has_evidence:
            reasons.append("No log line could be confirmed as evidence.")
        if dropped:
            reasons.append(f"{dropped} cited line id(s) did not exist in the log and were removed.")
        if root.get("symptom"):
            reasons.append("The top match is a symptom; the underlying cause may be outside this log.")
        if not has_kb and has_evidence:
            reasons.append("No knowledge base rule matched; the diagnosis comes from AI reasoning over the cited lines."
                           if root.get("source") == "ai" else "No knowledge base rule matched.")
        if has_kb and has_evidence and not root.get("symptom") and agrees:
            level = "high"
        elif has_evidence and (has_kb or root.get("source") == "ai"):
            level = "medium"
        else:
            level = "low"

        root_public = {k: root.get(k) for k in ("kb_id", "title", "explanation", "component", "evidence_ids",
                                                 "first_line", "first_t", "symptom", "severity", "source", "prevention")}
        root_public["details"] = root.get("details", {})
        prevention = (ai or {}).get("prevention") or root.get("prevention")
        return {
            "summary": summary.strip(),
            "root_cause": root_public,
            "confidence": {"level": level, "reasons": reasons},
            "severity": root.get("severity") or "blocked",
            "fixes": validate_all(fixes),
            "findings": [{k: f[k] for k in ("id", "kb_id", "plugin", "title", "cause", "symptom", "severity",
                                             "component", "evidence_ids", "evidence_count", "first_line", "first_t")}
                         | {"fixes": validate_all(f["fixes"])} for f in findings],
            "contributing_factors": contributing,
            "open_questions": open_q,
            "prevention": prevention,
        }

    def _explain_links(self, timeline, findings, ai):
        titles = {f["kb_id"]: f["title"] for f in findings}
        ai_map = {}
        for x in (ai or {}).get("timeline_explanations") or []:
            if isinstance(x, dict) and x.get("from") and x.get("to"):
                ai_map[(x["from"], x["to"])] = x.get("explanation")
        for link in timeline["links"]:
            ai_text = ai_map.get((link["from"], link["to"]))
            if ai_text:
                link["explanation"], link["explained_by"] = ai_text, "ai"
            else:
                a, b = titles.get(link["from"], link["from"]), titles.get(link["to"], link["to"])
                rel = "a known downstream symptom" if link["basis"] == "known downstream symptom" else "a later error"
                link["explanation"] = (f"{b} (line {link['to_line']}) appears after {a.lower()} (line {link['from_line']}) "
                                       f"and is {rel} of it.")
                link["explained_by"] = "rules"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
