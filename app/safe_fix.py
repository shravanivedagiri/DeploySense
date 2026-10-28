"""Classify every proposed fix by risk and attach a dry-run preview and pre-run checks.
DeploySense never executes commands. It only analyses the command text."""
import re
import shlex

READ_ONLY, REVERSIBLE, DESTRUCTIVE, REVIEW = "read-only", "reversible", "destructive", "review"

_DESTRUCTIVE = [
    (r"\brm\s+-[a-z]*[rf]", "Deletes files recursively or forcefully."),
    (r"\bkubectl\s+delete\b", "Deletes Kubernetes resources."),
    (r"\bhelm\s+(uninstall|delete)\b", "Removes a Helm release."),
    (r"\bterraform\s+destroy\b", "Destroys infrastructure."),
    (r"\bdocker\s+(system|volume|image|container)\s+prune\b(?!.*--filter)", "Removes Docker data without a filter."),
    (r"\bdocker\s+(rm|rmi)\b", "Removes containers or images."),
    (r"(?i)\b(drop\s+(table|database|schema)|truncate\s+table|delete\s+from)\b", "Removes database data."),
    (r"\bgit\s+push\b.*(--force|-f\b)", "Rewrites remote history."),
    (r"\bgit\s+reset\s+--hard\b", "Discards local changes."),
    (r"\bkubectl\s+drain\b", "Evicts all pods from a node."),
    (r"\bmkfs\b|\bdd\s+if=", "Overwrites a disk."),
]
_READ_ONLY = [
    r"^kubectl\s+(get|describe|logs|top|explain|auth\s+can-i|api-resources|version|events)\b",
    r"^kubectl\s+set\s+env\b.*--list\b",
    r"^kubectl\s+(apply|create|delete|patch|set|scale)\b.*--dry-run",
    r"^kubectl\s+rollout\s+(status|history)\b",
    r"^kubectl\s+run\s+\S+\s+--rm\b.*\s--\s+(nslookup|nc|wget|curl|psql|mysql|dig)\b(?!.*(create|drop|delete|insert|update)\b)",
    r"^kubectl\s+exec\b.*\s--\s+(sh -c 'id|wget -qO-|curl|cat|ls|env|printenv|npx prisma migrate status|alembic current)",
    r"^docker\s+(ps|logs|inspect|images|manifest\s+inspect|system\s+df|version|info|history)\b",
    r"^docker\s+run\s+--rm\s+\S+\s+(node|python|java|go)\s+(--version|-v|version)\b",
    r"^(lsof|netstat|ss|df|du|ls|cat|grep|head|tail|env|printenv|id|whoami|nslookup|dig|ping|nc|openssl|"
    r"yamllint|npm\s+ls|pip\s+index|pip\s+install\s+--dry-run|pip\s+show|pg_isready|terraform\s+plan|"
    r"helm\s+(status|list|get|diff|template)|pytest|npm\s+test|git\s+(log|diff|status|show))\b",
    r"^(psql|mysql)\b.*(-l\b|select\s|show\s)(?!.*(create|drop|delete|insert|update|alter))",
    r"^docker\s+build\b.*-t\s+\S+:test\b",
    r"^docker\s+build\s+--progress=plain",
]
_REVERSIBLE = [
    (r"^kubectl\s+(set|scale|patch|apply|annotate|label|edit)\b", "Changes cluster state. Roll back with kubectl rollout undo."),
    (r"^kubectl\s+rollout\s+(restart|undo|pause|resume)\b", "Restarts or rolls back pods. Short disruption is possible."),
    (r"^kubectl\s+create\s+secret\b", "Creates a secret. Remove it with kubectl delete if wrong."),
    (r"^kubectl\s+exec\b.*migrate\s+deploy", "Applies schema migrations. Take a backup first; down-migrations may not exist."),
    (r"^helm\s+(upgrade|install|rollback)\b", "Changes a release. Roll back with helm rollback."),
    (r"^docker\s+(stop|restart|start|run|build|pull|tag|push)\b", "Changes local containers or images."),
    (r"^docker\s+builder\s+prune\b.*--filter", "Removes old build cache only."),
    (r"^(npm|yarn|pnpm)\s+(install|ci|update)\b", "Changes installed packages and the lockfile."),
    (r"^pip\s+install\b", "Changes installed packages."),
    (r"^sed\s+-i\b", "Edits a file in place. Commit before running so it can be reverted."),
    (r"^(psql|mysql)\b.*create\s+database", "Creates a database."),
    (r"^git\s+(add|commit|checkout|switch|revert)\b", "Changes the working tree. Reversible with git."),
    (r"^terraform\s+apply\b", "Changes infrastructure. Review terraform plan first."),
]
_PROD = re.compile(r"(?i)(-n|--namespace)[ =](prod|production|live)\b|\bprod(uction)?\b")
_PLACEHOLDER = re.compile(r"<[a-z][a-z0-9_-]*>", re.I)


def _segments(command: str) -> list[str]:
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", command.strip())
    return [p.split("|")[0].strip() for p in parts if p.strip()]


def classify(command: str) -> tuple[str, str]:
    """Return (risk, reason) for a whole command. The riskiest segment decides."""
    for rx, reason in _DESTRUCTIVE:
        if re.search(rx, command, re.I):
            return DESTRUCTIVE, reason
    worst, reason = READ_ONLY, "Only reads state. Safe to run."
    for seg in _segments(command):
        if any(re.search(rx, seg, re.I) for rx in _READ_ONLY):
            continue
        for rx, why in _REVERSIBLE:
            if re.search(rx, seg, re.I):
                worst, reason = REVERSIBLE, why
                break
        else:
            return REVIEW, "Command not recognized by the safety rules. Review it before running."
    return worst, reason


def dry_run(command: str) -> str | None:
    """A preview command that shows the effect without applying it, when the tool supports one."""
    c = command.strip()
    if re.match(r"^kubectl\s+(set|scale|patch|apply|create|delete|annotate|label)\b", c) and "--dry-run" not in c:
        return f"{c} --dry-run=server -o yaml"
    if re.match(r"^kubectl\s+rollout\s+restart\b", c):
        return f"{c} --dry-run=server"
    if re.match(r"^helm\s+upgrade\b", c):
        return re.sub(r"^helm\s+upgrade", "helm diff upgrade", c)
    if re.match(r"^terraform\s+(apply|destroy)\b", c):
        return re.sub(r"^terraform\s+destroy", "terraform plan -destroy", re.sub(r"^terraform\s+apply", "terraform plan", c))
    if re.match(r"^(npm)\s+install\b", c) and "--dry-run" not in c:
        return f"{c} --dry-run"
    if re.match(r"^pip\s+install\b", c) and "--dry-run" not in c:
        return c.replace("pip install", "pip install --dry-run", 1)
    if re.match(r"^sed\s+-i\b", c):
        return re.sub(r"^sed\s+-i\s+", "sed ", c) + " | diff Dockerfile -"
    m = re.search(r"(?i)(-c|-e)\s+'((?:create|drop|alter|delete|update|insert)[^']*)'", c)
    if m:
        return c.replace(m.group(0), f"{m.group(1)} 'BEGIN; {m.group(2).rstrip(';')}; ROLLBACK;'")
    if re.match(r"^kubectl\s+exec\b.*prisma\s+migrate\s+deploy", c):
        return c.replace("migrate deploy", "migrate status")
    return None


def checks(command: str, risk: str) -> list[dict]:
    """Static pre-run checks. These analyse the command text; nothing is executed."""
    out = []
    try:
        shlex.split(command)
        out.append({"name": "Command syntax", "status": "pass", "detail": "Quotes and arguments are balanced."})
    except ValueError as exc:
        out.append({"name": "Command syntax", "status": "fail", "detail": f"Could not parse: {exc}."})
    holes = sorted(set(_PLACEHOLDER.findall(command)))
    if holes:
        out.append({"name": "Placeholders", "status": "warn",
                    "detail": f"Replace {', '.join(holes)} with real values before running."})
    else:
        out.append({"name": "Placeholders", "status": "pass", "detail": "All values were filled from the log."})
    if _PROD.search(command):
        out.append({"name": "Target environment", "status": "warn",
                    "detail": "Targets a production namespace. Run the dry-run first and follow change control."})
    if risk == DESTRUCTIVE:
        out.append({"name": "Data safety", "status": "fail",
                    "detail": "Destructive. DeploySense will not mark this as recommended. Take a backup and get approval."})
    return out


def validate_fix(fix: dict) -> dict:
    risk, reason = classify(fix["command"])
    preview = dry_run(fix["command"]) if risk in (REVERSIBLE, DESTRUCTIVE, REVIEW) else None
    return {**fix, "risk": risk, "risk_reason": reason, "dry_run": preview,
            "checks": checks(fix["command"], risk), "recommended": risk != DESTRUCTIVE}


_ORDER = {READ_ONLY: 0, REVERSIBLE: 1, REVIEW: 2, DESTRUCTIVE: 3}


def validate_all(fixes: list[dict]) -> list[dict]:
    """Validate and order fixes: diagnose first (read-only), then changes, destructive last. Original order is kept within a tier."""
    validated = [validate_fix(f) for f in fixes]
    return sorted(validated, key=lambda f: _ORDER[f["risk"]])
