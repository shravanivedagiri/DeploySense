"""Compare a failed run with the last successful run of the same deployment."""
import re

from .log_analyzer import message_template

_VERSION = re.compile(r"(?i)\b([a-z][\w.\-/]*?)(?:@|:|\s+v(?:ersion)?\s*|==|\s)v?(\d+\.\d+(?:\.\d+)?(?:[-.][\w]+)?)\b")
_NOISE = re.compile(r"(?i)(took \d|elapsed|duration|\d+ms\b|\d+\.\d+s\b|sha256:|digest)")


def _versions(events) -> dict[str, str]:
    found = {}
    for e in events:
        for name, ver in _VERSION.findall(e.raw):
            key = name.lower().strip("/:")
            if len(key) >= 2 and not key.isdigit() and key not in ("at", "line", "step", "in", "v"):
                found.setdefault(key, ver)
    return found


def compare(failed: dict, success: dict, limit: int = 25) -> dict:
    f_tpl = {}
    for e in failed["events"]:
        f_tpl.setdefault(message_template(e.message), e)
    s_tpl = {}
    for e in success["events"]:
        s_tpl.setdefault(message_template(e.message), e)

    new_lines = [e for k, e in f_tpl.items() if k not in s_tpl and not _NOISE.search(e.message)]
    new_lines.sort(key=lambda e: (e.level not in ("ERROR", "FATAL", "WARN"), e.id))
    missing = [e for k, e in s_tpl.items() if k not in f_tpl and not _NOISE.search(e.message)]
    missing.sort(key=lambda e: e.id)

    fv, sv = _versions(failed["events"]), _versions(success["events"])
    changed = [{"name": n, "before": sv[n], "after": fv[n]} for n in fv if n in sv and fv[n] != sv[n]]

    return {
        "new_in_failed": [{"line": e.id, "level": e.level, "message": e.message[:240]} for e in new_lines[:limit]],
        "missing_from_failed": [{"line": e.id, "message": e.message[:240]} for e in missing[:limit]],
        "version_changes": changed[:15],
        "counts": {"new": len(new_lines), "missing": len(missing), "versions": len(changed)},
    }
