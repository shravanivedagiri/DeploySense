"""Follow-up questions about one analysis report. The server is stateless: the browser sends the
question, recent messages and a compact context built from the report, and nothing is stored.
Facts stay in code: cited line ids are checked against the context and every command is risk-checked."""
import json
import re

from pydantic import BaseModel, Field

from .llm import GroqClient, LLMError, parse_json
from .safe_fix import validate_fix

MAX_WORDS = 150
MAX_COMMANDS = 4
UNAVAILABLE = "Follow-up questions need the AI agent, which is off right now."

SYSTEM_PROMPT = """You are DeploySense, answering follow-up questions about one failed deployment.
The user message contains the analysis report and log lines from this deployment. Each log line starts with its id, for example L12.

Rules:
- Any claim about this deployment must come from the report or the log lines given. Cite the lines you rely on as L12. Never cite a line id that is not listed.
- If the question is general and not about this deployment, answer in one line, set "general" to true, and say it is not based on this log.
- If the log does not contain the answer, say so and name the command that would show it.
- Keep the answer under %d words, in plain sentences.
- You cannot run commands and have not run any. Never say or imply that you ran, checked or executed something.
- Put shell commands only in "commands", never in "answer". Prefer read-only commands. Never suggest deleting data or resources unless the user asks how.

Reply with one JSON object and nothing else:
{"answer": "text citing lines like L12", "commands": [{"command": "single shell command", "purpose": "one short sentence"}], "general": false}""" % MAX_WORDS


class Line(BaseModel):
    id: int
    text: str = Field(max_length=600)


class Finding(BaseModel):
    title: str = Field(max_length=200)
    symptom: bool = False
    first_line: int | None = None
    evidence_ids: list[int] = Field(default_factory=list, max_length=20)


class Fix(BaseModel):
    title: str = Field(max_length=300)
    command: str = Field(max_length=1000)
    risk: str = Field(max_length=20)


class Context(BaseModel):
    summary: str = Field("", max_length=3000)
    root_cause: dict = Field(default_factory=dict)
    confidence: dict = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list, max_length=30)
    fixes: list[Fix] = Field(default_factory=list, max_length=12)
    links: list[str] = Field(default_factory=list, max_length=20)
    lines: list[Line] = Field(default_factory=list, max_length=150)


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    history: list[Message] = Field(default_factory=list)
    context: Context


def _context_text(ctx: Context) -> str:
    rc = ctx.root_cause
    parts = [f"Summary: {ctx.summary}",
             "Root cause: " + json.dumps({k: rc.get(k) for k in ("title", "kb_id", "explanation", "component",
                                                                  "first_line", "evidence_ids")})[:2000],
             "Confidence: " + json.dumps(ctx.confidence)[:1500]]
    if ctx.findings:
        parts.append("Findings (rule engine):\n" + "\n".join(
            f"- {f.title}{' (symptom)' if f.symptom else ''}, first at L{f.first_line}, evidence "
            + ", ".join(f"L{i}" for i in f.evidence_ids) for f in ctx.findings))
    if ctx.fixes:
        parts.append("Fix plan:\n" + "\n".join(f"- [{x.risk}] {x.title}: {x.command}" for x in ctx.fixes))
    if ctx.links:
        parts.append("Timeline links:\n" + "\n".join(f"- {x}" for x in ctx.links))
    parts.append("Log lines:\n" + "\n".join(f"L{x.id} {x.text}" for x in ctx.lines))
    return "\n\n".join(parts)


_LINE_REF = re.compile(r"\bL(\d+)\b")
_FENCED = re.compile(r"```[a-z]*\n?(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]+)`")


def _limit_words(text: str) -> str:
    words = text.split()
    if len(words) <= MAX_WORDS:
        return text
    cut = " ".join(words[:MAX_WORDS])
    end = max(cut.rfind(". "), cut.rfind(".\n"))
    return cut[:end + 1] if end > len(cut) // 2 else cut + " ..."


def clean_answer(raw: dict, valid_ids: set[int]) -> dict:
    """Check cited line ids, move commands out of the prose and risk-check every command."""
    text = str(raw.get("answer") or "").strip()
    commands = [c for c in raw.get("commands") or [] if isinstance(c, dict) and str(c.get("command") or "").strip()]

    # Commands written into the prose are moved to the command list so they get a risk label too.
    for block in _FENCED.findall(text):
        commands += [{"command": ln.strip(), "purpose": ""} for ln in block.splitlines() if ln.strip()]
    text = _FENCED.sub("", text)
    for span in _INLINE.findall(text):
        if " " in span.strip():
            commands.append({"command": span.strip(), "purpose": ""})
    text = _INLINE.sub(lambda m: "" if " " in m.group(1).strip() else m.group(1), text)

    removed = sorted({int(n) for n in _LINE_REF.findall(text) if int(n) not in valid_ids})
    if removed:
        text = _LINE_REF.sub(lambda m: m.group(0) if int(m.group(1)) in valid_ids else "", text)
        text = re.sub(r"\(\s*(?:,\s*)*\)", "", text)
        text = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"(,\s*){2,}", ", ", text))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    cited = sorted({int(n) for n in _LINE_REF.findall(text)})

    seen, checked = set(), []
    for c in commands:
        cmd = str(c["command"]).strip()
        if cmd in seen or len(checked) >= MAX_COMMANDS:
            continue
        seen.add(cmd)
        checked.append(validate_fix({"title": str(c.get("purpose") or "").strip(), "command": cmd, "verify": ""}))

    note = None
    if removed:
        n = len(removed)
        note = (f"Removed {n} line reference{'s' if n != 1 else ''} not found in this log: "
                + ", ".join(f"L{i}" for i in removed) + ".")
    return {"available": True, "answer": _limit_words(text), "cited": cited, "removed": removed, "note": note,
            "general": raw.get("general") is True, "commands": checked}


def answer(llm: GroqClient, req: AskRequest) -> dict:
    """Ask the model one follow-up question. Never raises for model or network problems."""
    if not llm.available:
        return {"available": False, "message": UNAVAILABLE, "reason": "GROQ_API_KEY is not set"}
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Report and log for this deployment:\n\n" + _context_text(req.context)},
                {"role": "assistant", "content": "Understood. I will answer only from this report and log."}]
    messages += [{"role": m.role, "content": m.content} for m in req.history[-6:]]
    messages.append({"role": "user", "content": req.question})
    try:
        msg = llm.chat(messages, json_mode=True, max_tokens=1024)
    except LLMError as exc:
        return {"available": False, "message": UNAVAILABLE, "reason": str(exc)}
    raw = parse_json(msg.get("content"))
    if not isinstance(raw, dict) or not str(raw.get("answer") or "").strip():
        return {"available": True, "error": "The AI agent's answer could not be read. Try asking again."}
    return {**clean_answer(raw, {x.id for x in req.context.lines}), "model": llm.model}
