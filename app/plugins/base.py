"""Plugin contract shared by every troubleshooting plugin."""
import re

from .. import knowledge_base as kb


class Plugin:
    name = "base"            # tool name the agent calls
    label = "Base"
    description = ""
    kb_plugin = ""           # which knowledge base entries this plugin owns
    signals = re.compile(r"$^")

    def can_handle(self, parsed: dict) -> float:
        """Relevance score 0..1 from error and warning lines. 0 means the plugin has nothing to say."""
        lines = parsed["errors"] + parsed["warnings"]
        if not lines:
            return 0.0
        hits = sum(1 for e in lines if self.signals.search(e.raw))
        return round(min(1.0, hits / max(3, len(lines) / 4)), 2)

    def enrich(self, parsed: dict, values: dict) -> dict:
        """Hook for plugin-specific extraction. Returns extra placeholder values and details."""
        return {}

    def adapt_command(self, command: str, values: dict) -> str:
        return command

    def analyze(self, parsed: dict, context: dict) -> list[dict]:
        findings = []
        extra = self.enrich(parsed, context)
        for entry in kb.entries_for(self.kb_plugin):
            hits, captured = kb.match_entry(entry, parsed["events"])
            if not hits:
                continue
            values = {**context, **extra.get("values", {}), **captured}
            fixes = []
            for f in entry["fixes"]:
                cmd = self.adapt_command(kb.fill(f["command"], values), values)
                fixes.append({"title": kb.fill(f["title"], values), "command": cmd,
                              "verify": kb.fill(f["verify"], values), "source": "knowledge_base"})
            first = min(hits, key=lambda e: (e.t, e.id))
            findings.append({
                "id": f"{self.name}:{entry['id']}",
                "plugin": self.name,
                "kb_id": entry["id"],
                "title": entry["title"],
                "cause": entry["cause"],
                "symptom": entry["symptom"],
                "severity": entry["severity"],
                "component": entry["component"],
                "evidence_ids": [e.id for e in hits[:8]],
                "evidence_count": len(hits),
                "first_line": first.id,
                "first_t": first.t,
                "fixes": fixes,
                "prevention": entry["prevention"],
                "values": {k: v for k, v in values.items() if v},
                "details": extra.get("details", {}),
            })
        return findings
