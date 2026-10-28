"""General analysis: Kubernetes, configuration, network and CI errors, plus the fallback for unknown errors."""
import re

from .base import Plugin
from ..log_analyzer import dedupe_errors


class GeneralPlugin(Plugin):
    name = "general_analysis"
    label = "General analysis"
    description = ("Covers errors outside Docker and databases: missing environment variables, config parse "
                   "errors, crash loops, failing health probes, scheduling, DNS, TLS, permissions and CI test "
                   "failures. Also lists error lines no rule recognized so the agent can reason about them.")
    kb_plugin = "general"
    signals = re.compile(r"(?i).")  # always applicable as the fallback

    def can_handle(self, parsed):
        return 1.0 if parsed["errors"] or parsed["warnings"] else 0.0

    def unmatched(self, parsed: dict, matched_ids: set[int]) -> list[dict]:
        """Error groups that no knowledge base rule matched."""
        out = []
        for g in dedupe_errors(parsed["errors"]):
            ev = g["first"]
            if ev.id not in matched_ids:
                out.append({"line": ev.id, "t": ev.t, "component": ev.component,
                            "message": ev.message[:300], "count": g["count"]})
        return out[:15]
