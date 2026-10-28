import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent import TroubleshootingAgent
from app.llm import GroqClient
from app.log_analyzer import mask_secrets, parse_log
from app.main import app
from app.safe_fix import DESTRUCTIVE, READ_ONLY, REVERSIBLE, REVIEW, classify, dry_run

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def read(name):
    return (SAMPLES / name).read_text()


# ---------- log analyzer ----------
def test_masks_secrets_before_analysis():
    text = "DB_PASSWORD=hunter2 token: abc123 postgres://app:s3cret@db:5432/x mail ops@example.com from 10.1.2.3"
    out = mask_secrets(text)
    for secret in ("hunter2", "abc123", "s3cret", "ops@example.com", "10.1.2.3"):
        assert secret not in out


def test_parses_mixed_formats():
    log = "\n".join([
        '{"time":"2026-10-28T10:00:00Z","level":"error","msg":"query failed","error":"boom"}',
        "2026-10-28T10:00:01.5Z ERROR [api] Error: connect ECONNREFUSED",
        "2026-10-28T10:00:02.0000000Z ##[error]Process completed with exit code 1.",
        "#7 9.80 npm ERR! code ERESOLVE",
        "E1028 10:00:03.000000       1 main.go:42] something failed",
    ])
    p = parse_log(log)
    assert set(p["formats"]) >= {"json", "timestamped", "github-actions", "docker-buildkit", "klog"}
    assert all(e.level == "ERROR" for e in p["events"])
    assert p["events"][1].message.startswith("Error: connect")  # "Error:" is not mistaken for a logger name
    assert p["events"][0].t == 0 and p["events"][1].t == 1.5


def test_line_order_used_without_timestamps():
    p = parse_log("starting\nERROR: first\nERROR: second")
    assert p["time_source"] == "line order"
    assert [e.t for e in p["events"]] == [0.0, 1.0, 2.0]


# ---------- safe-fix validator ----------
def test_risk_classification():
    assert classify("kubectl get pods -n x")[0] == READ_ONLY
    assert classify("kubectl set env deployment/a --list | grep X")[0] == READ_ONLY
    assert classify("kubectl set resources deployment/a --limits=memory=1Gi")[0] == REVERSIBLE
    assert classify("kubectl delete pod a")[0] == DESTRUCTIVE
    assert classify("psql -h db -c 'DROP TABLE users;'")[0] == DESTRUCTIVE
    assert classify("docker system prune -af")[0] == DESTRUCTIVE
    assert classify("curl-something-weird --go")[0] == REVIEW
    assert classify("lsof -i :80 && docker stop abc")[0] == REVERSIBLE  # riskiest segment decides


def test_dry_run_previews():
    assert dry_run("kubectl scale deployment/a --replicas=3").endswith("--dry-run=server -o yaml")
    assert dry_run("terraform apply").startswith("terraform plan")
    assert "ROLLBACK" in dry_run("psql -h db -U app -c 'CREATE DATABASE x;'")


# ---------- agent, rule mode ----------
def test_root_cause_is_cause_not_symptom():
    r = TroubleshootingAgent(GroqClient(api_key="")).analyze(read("k8s_oomkilled.log"))
    assert r["root_cause"]["kb_id"] == "container_oom_killed"
    assert r["confidence"]["level"] == "high"
    assert r["timeline"]["root_cause_line"] == r["root_cause"]["first_line"]
    assert any(n["root_cause"] and n["id"] == "container" for n in r["timeline"]["map"]["nodes"])
    assert "checkout-api" in r["fixes"][0]["command"] and "payments-staging" in r["fixes"][0]["command"]


def test_earliest_cause_wins_in_chain():
    r = TroubleshootingAgent(GroqClient(api_key="")).analyze(read("db_missing_secret.log"))
    assert r["root_cause"]["kb_id"] == "env_var_missing"
    assert any(l["to"] == "db_auth_failed" for l in r["timeline"]["links"])


def test_diff_finds_version_change():
    r = TroubleshootingAgent(GroqClient(api_key="")).analyze(read("docker_build_failed.log"), read("docker_build_success.log"))
    assert {"name": "node", "before": "18.19.0", "after": "20.11.1"} in r["diff"]["version_changes"]


def test_unknown_error_is_low_confidence():
    r = TroubleshootingAgent(GroqClient(api_key="")).analyze(read("unknown_error.log"))
    assert r["root_cause"]["kb_id"] is None
    assert r["confidence"]["level"] == "low"


# ---------- agent, LLM mode with a scripted model ----------
class ScriptedLLM(GroqClient):
    """Replays tool calls and a final answer so the agent loop is tested without network access."""
    def __init__(self, final):
        super().__init__(api_key="test", model="scripted")
        self.final, self.calls = final, []

    def chat(self, messages, tools=None, json_mode=False, **kw):
        self.calls.append(messages)
        tool_turns = sum(1 for m in messages if m["role"] == "tool")
        if tools and tool_turns == 0:
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "docker_plugin", "arguments": "{}"}},
                {"id": "c2", "type": "function", "function": {"name": "general_analysis", "arguments": "{}"}},
                {"id": "c3", "type": "function", "function": {"name": "get_log_lines", "arguments": '{"start": 10, "end": 13}'}}]}
        return {"content": json.dumps(self.final)}


def test_llm_agent_loop_and_verification():
    final = {"summary": "The pod ran out of memory while loading the catalog cache.",
             "root_cause": {"title": "Heap exhausted", "kb_id": "container_oom_killed", "component": "container",
                            "explanation": "Line 12 shows the heap limit; line 13 the OOM kill.", "evidence_lines": [12, 13, 999]},
             "fixes": [{"title": "Profile memory", "command": "kubectl top pod -n payments-staging", "verify": "usage shown"}],
             "timeline_explanations": [{"from": "container_oom_killed", "to": "crashloop_backoff",
                                        "explanation": "Each OOM kill restarts the pod until Kubernetes backs off."}],
             "open_questions": []}
    llm = ScriptedLLM(final)
    r = TroubleshootingAgent(llm).analyze(read("k8s_oomkilled.log"))
    assert r["mode"] == "llm"
    assert [t["step"] for t in r["trace"]][1:4] == ["docker_plugin", "general_analysis", "get_log_lines"]
    assert 999 not in r["root_cause"]["evidence_ids"]
    assert any("did not exist" in x for x in r["confidence"]["reasons"])
    assert any(f["source"] == "ai" for f in r["fixes"])
    link = next(l for l in r["timeline"]["links"] if l["to"] == "crashloop_backoff")
    assert link["explained_by"] == "ai"


def test_llm_disagreement_lowers_confidence():
    final = {"summary": "x", "root_cause": {"title": "Probe misconfigured", "kb_id": "probe_failed",
                                             "component": "container", "evidence_lines": [14]}, "fixes": []}
    r = TroubleshootingAgent(ScriptedLLM(final)).analyze(read("k8s_oomkilled.log"))
    assert r["root_cause"]["kb_id"] == "probe_failed"
    assert r["confidence"]["level"] == "medium"


# ---------- API ----------
def test_api_end_to_end():
    c = TestClient(app)
    assert c.get("/api/health").json()["status"] == "ok"
    s = c.get("/api/samples/docker_build_failed").json()
    r = c.post("/api/analyze", data={"failed_log": s["failed_log"], "success_log": s["success_log"], "use_llm": "false"})
    assert r.status_code == 200 and r.json()["root_cause"]["kb_id"] == "native_module_build_failed"
    up = c.post("/api/analyze", files={"failed_file": ("x.log", read("compose_port_conflict.log").encode(), "text/plain")},
                data={"use_llm": "false"})
    assert up.json()["root_cause"]["kb_id"] == "port_already_in_use"
    assert c.post("/api/analyze", data={"failed_log": "  "}).status_code == 400
    assert c.post("/api/analyze", files={"failed_file": ("x.png", b"\x89PNG\x00", "image/png")}).status_code == 400
    assert c.get("/privacy").status_code == 200 and c.get("/terms").status_code == 200


def test_feedback_counts_each_analysis_once(tmp_path, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "FEEDBACK_FILE", tmp_path / "fb.jsonl")
    c = TestClient(app)
    assert c.get("/api/feedback/stats").json() == {"rated": 0, "useful": 0, "rate": None}
    c.post("/api/feedback", json={"analysis_id": "a1", "useful": False})
    c.post("/api/feedback", json={"analysis_id": "a1", "useful": True})
    s = c.post("/api/feedback", json={"analysis_id": "a2", "useful": False}).json()
    assert s == {"rated": 2, "useful": 1, "rate": 0.5}
