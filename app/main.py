"""DeploySense AI web server: API plus the static dashboard."""
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from . import ask
from .agent import TroubleshootingAgent
from .llm import load_env_file
from .plugins import PLUGINS

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples"
FEEDBACK_FILE = Path(os.getenv("FEEDBACK_FILE", ROOT / "data" / "feedback.jsonl"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "5")) * 1024 * 1024
ALLOWED_SUFFIXES = {".log", ".txt", ".json", ".jsonl", ".out", ""}
MAX_ASK_BYTES = 200 * 1024
ASK_LIMIT_PER_MINUTE = 10

# Samples offered in the dashboard. A success log is attached where a comparison makes sense.
SAMPLE_SETS = [
    {"id": "k8s_oomkilled", "title": "Kubernetes pod killed for memory", "failed": "k8s_oomkilled.log"},
    {"id": "db_missing_secret", "title": "Missing secret breaks database login", "failed": "db_missing_secret.log"},
    {"id": "docker_build_failed", "title": "Docker build after a Node upgrade", "failed": "docker_build_failed.log",
     "success": "docker_build_success.log"},
    {"id": "github_actions_image_pull", "title": "GitHub Actions deploy with a missing image tag",
     "failed": "github_actions_image_pull.log"},
    {"id": "db_migration_missing", "title": "Release without its database migration", "failed": "db_migration_missing.log"},
    {"id": "compose_port_conflict", "title": "Docker Compose port conflict", "failed": "compose_port_conflict.log"},
    {"id": "unknown_error", "title": "Error no rule recognizes", "failed": "unknown_error.log"},
]

app = FastAPI(title="DeploySense AI", version="1.0.0", docs_url="/api/docs", redoc_url=None)
agent = TroubleshootingAgent()


load_env_file(ROOT / ".env")
agent.llm.api_key = os.getenv("GROQ_API_KEY", "")
agent.llm.model = os.getenv("GROQ_MODEL", agent.llm.model)


async def _read_upload(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    if Path(upload.filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"{upload.filename}: upload a plain-text log (.log, .txt, .json).")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{upload.filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    if b"\x00" in data[:4096]:
        raise HTTPException(400, f"{upload.filename} looks like a binary file, not a text log.")
    return data.decode("utf-8", errors="replace")


@app.get("/api/health")
def health():
    """llm is "off" without a key, "error" when the key is set but the model check or the last request failed."""
    llm, model, reason = "off", None, None
    if agent.llm.available:
        model = agent.llm.model
        if agent.llm.last_error:
            reason = f"last request failed: {agent.llm.last_error}"
        elif check := agent.llm.check():
            reason = check
        llm = "error" if reason else "groq"
    return {"status": "ok", "llm": llm, "model": model, "llm_error": reason,
            "plugins": [{"name": p.name, "label": p.label, "description": p.description} for p in PLUGINS]}


@app.post("/api/analyze")
async def analyze(failed_log: str = Form(""), success_log: str = Form(""), use_llm: bool = Form(True),
                  failed_file: UploadFile | None = File(None), success_file: UploadFile | None = File(None)):
    failed = await _read_upload(failed_file) or failed_log
    success = await _read_upload(success_file) or success_log
    if not failed.strip():
        raise HTTPException(400, "Add the log from the failed deployment, as a file or pasted text.")
    if len(failed.encode()) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"The pasted log is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    # The agent makes blocking network calls; run it in a worker thread so other requests are not held up.
    return await run_in_threadpool(agent.analyze, failed, success or None, use_llm=use_llm)


_ask_times: dict[str, deque] = defaultdict(deque)


def _ask_allowed(ip: str) -> bool:
    """At most ASK_LIMIT_PER_MINUTE questions per client IP in any 60 second window. Kept in memory only."""
    now, times = time.monotonic(), _ask_times[ip]
    while times and now - times[0] > 60:
        times.popleft()
    if len(times) >= ASK_LIMIT_PER_MINUTE:
        return False
    times.append(now)
    return True


@app.post("/api/ask")
async def ask_question(request: Request):
    """Answer a follow-up question about one report. Stateless: the browser sends the context, nothing is stored."""
    too_big = HTTPException(413, "The question and its context are larger than 200 KB.")
    if int(request.headers.get("content-length") or 0) > MAX_ASK_BYTES:
        raise too_big
    body = await request.body()
    if len(body) > MAX_ASK_BYTES:
        raise too_big
    try:
        req = ask.AskRequest.model_validate_json(body)
    except ValidationError:
        raise HTTPException(400, "The question or its context is not in the expected format.")
    if not req.question.strip():
        raise HTTPException(400, "Type a question first.")
    if not _ask_allowed(request.client.host if request.client else "unknown"):
        raise HTTPException(429, "Too many questions. Wait a minute and try again.")
    # The model call blocks on the network; run it in a worker thread so other requests are not held up.
    return await run_in_threadpool(ask.answer, agent.llm, req)


@app.get("/api/samples")
def samples():
    return [{"id": s["id"], "title": s["title"], "has_success_log": "success" in s} for s in SAMPLE_SETS]


@app.get("/api/samples/{sample_id}")
def sample(sample_id: str):
    s = next((x for x in SAMPLE_SETS if x["id"] == sample_id), None)
    if not s:
        raise HTTPException(404, "Sample not found.")
    return {"id": s["id"], "title": s["title"], "failed_log": (SAMPLES / s["failed"]).read_text(),
            "success_log": (SAMPLES / s["success"]).read_text() if "success" in s else ""}


class Feedback(BaseModel):
    analysis_id: str
    useful: bool
    root_cause: str | None = None
    comment: str | None = None


@app.post("/api/feedback")
def feedback(item: Feedback):
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": int(time.time()), "analysis_id": item.analysis_id[:32], "useful": item.useful,
              "root_cause": (item.root_cause or "")[:80], "comment": (item.comment or "")[:500]}
    with FEEDBACK_FILE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return feedback_stats()


@app.get("/api/feedback/stats")
def feedback_stats():
    """Usefulness from real ratings only. Each analysis counts once, using its latest rating."""
    latest = {}
    if FEEDBACK_FILE.exists():
        for line in FEEDBACK_FILE.read_text().splitlines():
            try:
                r = json.loads(line)
                latest[r["analysis_id"]] = r["useful"]
            except (ValueError, KeyError):
                continue
    total = len(latest)
    useful = sum(1 for v in latest.values() if v)
    return {"rated": total, "useful": useful, "rate": round(useful / total, 3) if total else None}


for page in ("privacy", "terms"):
    app.add_api_route(f"/{page}", (lambda p=page: FileResponse(FRONTEND / f"{p}.html")), methods=["GET"],
                      include_in_schema=False)


@app.exception_handler(HTTPException)
async def http_error(_, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
