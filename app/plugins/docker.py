"""Docker plugin: image build, registry and container runtime failures."""
import re

from .base import Plugin

_FROM = re.compile(r"(?:^|\]\s+)FROM\s+([\w.\-/]+(?::[\w.\-]+)?)")
_NPM_PATH = re.compile(r"npm ERR! path \S*node_modules/(@?[\w.-]+(?:/[\w.-]+)?)")
_STEP = re.compile(r"^#\d+\s+(\[[^\]]+\]\s+.+)$|^(Step \d+/\d+ : .+)$")
_EXIT = re.compile(r"(?:exit code:?|exited with code|non-zero code:)\s*(\d+)", re.I)


class DockerPlugin(Plugin):
    name = "docker_plugin"
    label = "Docker plugin"
    description = ("Diagnoses image build failures (npm, pip, native modules, COPY), registry pull and auth "
                   "errors, and container runtime failures (OOMKilled, exit codes, port conflicts, disk full).")
    kb_plugin = "docker"
    signals = re.compile(r"(?i)(docker|dockerfile|build-step|step \d+/\d+|npm err|gyp err|pip|copy failed|"
                         r"oomkilled|exit code|exited with|image|registry|manifest|pull|eaddrinuse|"
                         r"address already in use|no space left|container)")

    def enrich(self, parsed, values):
        details, extra = {}, {}
        failing_step = None
        for ev in parsed["events"]:
            m = _FROM.search(ev.raw)
            if m and "base_image" not in details:
                details["base_image"] = extra["base_image"] = m.group(1)
            s = _STEP.match(ev.raw.strip())
            if s:
                failing_step = (s.group(1) or s.group(2)).strip()
            if ev.level in ("ERROR", "FATAL") and failing_step and "failing_step" not in details:
                details["failing_step"] = failing_step
            p = _NPM_PATH.search(ev.raw)
            if p and "package" not in extra:
                extra["package"] = details["package"] = p.group(1)
            e = _EXIT.search(ev.raw)
            if e and e.group(1) != "0" and "exit_code" not in details:
                details["exit_code"] = int(e.group(1))
                extra["code"] = e.group(1)
        if details.get("exit_code") == 137:
            details["exit_code_meaning"] = "137 means the process received SIGKILL, most often from the out-of-memory killer."
        elif details.get("exit_code") == 143:
            details["exit_code_meaning"] = "143 means the process received SIGTERM and shut down."
        elif details.get("exit_code") == 1:
            details["exit_code_meaning"] = "1 is a general application error; the preceding lines hold the reason."
        return {"values": extra, "details": details}

    def adapt_command(self, command, values):
        base = values.get("base_image")
        return command.replace("<base-image>", base) if base else command
