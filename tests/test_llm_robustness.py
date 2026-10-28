"""The real model can return odd tool arguments, wrong JSON types or rate limits. None of that may become a 500."""
import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent import TroubleshootingAgent
from app.llm import GroqClient, LLMError

LOG = (Path(__file__).resolve().parent.parent / "samples" / "k8s_oomkilled.log").read_text()


class Script(GroqClient):
    """Returns the scripted assistant messages in order, then repeats the last one."""
    def __init__(self, *replies):
        super().__init__(api_key="test", model="scripted")
        self.replies = list(replies)

    def chat(self, messages, tools=None, json_mode=False, **kw):
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_call(name, arguments):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": name, "arguments": arguments}}]}


def answer(obj):
    return {"content": json.dumps(obj)}


GOOD = {"summary": "Out of memory.", "root_cause": {"title": "OOM", "kb_id": "container_oom_killed",
                                                     "component": "container", "evidence_lines": [12, 13]}}


# ---------- tool arguments ----------
def test_line_ids_with_prefix_are_accepted():
    r = TroubleshootingAgent(Script(tool_call("get_log_lines", '{"start": "L10", "end": "L12"}'), answer(GOOD))).analyze(LOG)
    assert r["mode"] == "llm"
    assert any(t["step"] == "get_log_lines" and t["detail"] == "lines 10 to 12" for t in r["trace"])


@pytest.mark.parametrize("arguments", ['{"start": "abc"}', '{"start": null}', "not json", "[1, 2]"])
def test_bad_tool_arguments_do_not_crash(arguments):
    r = TroubleshootingAgent(Script(tool_call("get_log_lines", arguments), answer(GOOD))).analyze(LOG)
    assert r["mode"] == "llm" and r["root_cause"]["kb_id"] == "container_oom_killed"


# ---------- badly typed model output ----------
@pytest.mark.parametrize("final", [
    {"summary": "x", "root_cause": "OOM"},
    {"summary": "x"},
    ["not", "an", "object"],
])
def test_unusable_answer_falls_back_to_rules(final):
    r = TroubleshootingAgent(Script(answer(final))).analyze(LOG)
    assert r["mode"] == "rules"
    assert r["root_cause"]["kb_id"] == "container_oom_killed"
    assert "format" in r["llm_error"] or "JSON" in r["llm_error"]


def test_wrongly_typed_fields_are_coerced():
    final = {"summary": "x", "root_cause": {"kb_id": ["container_oom_killed"], "evidence_lines": "L12, L13",
                                            "component": 7},
             "fixes": ["kubectl get pods", {"title": "Top", "command": "kubectl top pod -n payments-staging"}],
             "contributing_factors": "memory", "timeline_explanations": [None], "open_questions": "why?"}
    r = TroubleshootingAgent(Script(answer(final))).analyze(LOG)
    assert r["mode"] == "llm"
    assert {12, 13} <= set(r["root_cause"]["evidence_ids"])
    assert [f["command"] for f in r["fixes"] if f["source"] == "ai"] == ["kubectl top pod -n payments-staging"]


def test_unexpected_error_in_agent_loop_falls_back():
    r = TroubleshootingAgent(Script(KeyError("choices"))).analyze(LOG)
    assert r["mode"] == "rules" and "unexpected model output" in r["llm_error"]


def test_api_returns_200_on_bad_model_output(monkeypatch):
    monkeypatch.setattr(main, "agent", TroubleshootingAgent(Script(answer({"root_cause": "x"}))))
    r = TestClient(main.app).post("/api/analyze", data={"failed_log": LOG})
    assert r.status_code == 200 and r.json()["mode"] == "rules"


# ---------- Groq client: retries and reasons ----------
def fake_responses(monkeypatch, *responses):
    sent = []

    def post(url, **kw):
        sent.append(url)
        status, body, headers = responses[min(len(sent), len(responses)) - 1]
        return httpx.Response(status, text=body, headers=headers, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx, "post", post)
    return sent


OK = (200, json.dumps({"choices": [{"message": {"content": "{}"}}]}), {})
TPM = (429, '{"error":{"message":"Rate limit reached for model `m` in organization `org_secret` on tokens per minute (TPM): '
            'Limit 8000. Please try again in 4.74s."}}', {})


def test_rate_limit_is_retried_with_the_wait_groq_asks_for(monkeypatch):
    sent = fake_responses(monkeypatch, TPM, TPM, OK)
    llm, waits = GroqClient(api_key="k", model="m"), []
    llm.sleep = waits.append
    assert llm.chat([]) == {"content": "{}"}
    assert waits == [4.74, 4.74] and len(sent) == 3 and llm.last_error is None


def test_long_waits_fail_at_once_with_a_short_reason(monkeypatch):
    tpd = (429, '{"error":{"message":"Rate limit reached in organization `org_secret` on tokens per day (TPD). '
                'Please try again in 12m3.5s."}}', {})
    sent = fake_responses(monkeypatch, tpd)
    llm = GroqClient(api_key="k", model="m")
    llm.sleep = lambda s: pytest.fail("should not wait")
    with pytest.raises(LLMError) as exc:
        llm.chat([])
    assert str(exc.value) == "rate limited by Groq (tokens per day)" and len(sent) == 1
    assert "org_secret" not in llm.last_error


def test_retries_stop_after_the_limit(monkeypatch):
    sent = fake_responses(monkeypatch, (503, "busy", {"retry-after": "1"}))
    llm, waits = GroqClient(api_key="k", model="m"), []
    llm.sleep = waits.append
    with pytest.raises(LLMError, match="server error"):
        llm.chat([])
    assert len(sent) == 5 and waits == [1.0] * 4


def test_missing_model_is_not_retried(monkeypatch):
    sent = fake_responses(monkeypatch, (404, '{"error":{"code":"model_not_found"}}', {}))
    with pytest.raises(LLMError, match="model m is not available"):
        GroqClient(api_key="k", model="m").chat([])
    assert len(sent) == 1


def test_empty_key_never_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert not GroqClient(api_key="").available
    assert GroqClient().available


# ---------- health ----------
def test_health_reports_a_failing_model(monkeypatch):
    llm = main.agent.llm
    monkeypatch.setattr(llm, "api_key", "k")
    monkeypatch.setattr(llm, "_check", None)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(
        404, text='{"error":{"code":"model_not_found"}}', request=httpx.Request("GET", url)))
    h = TestClient(main.app).get("/api/health").json()
    assert h["llm"] == "error" and "not available" in h["llm_error"]


def test_health_reports_the_last_failed_request(monkeypatch):
    llm = main.agent.llm
    monkeypatch.setattr(llm, "api_key", "k")
    monkeypatch.setattr(llm, "last_error", "rate limited by Groq (tokens per minute)")
    h = TestClient(main.app).get("/api/health").json()
    assert h["llm"] == "error" and h["llm_error"] == "last request failed: rate limited by Groq (tokens per minute)"


def test_health_off_without_key():
    h = TestClient(main.app).get("/api/health").json()
    assert h["llm"] == "off" and h["llm_error"] is None


# ---------- concurrency ----------
def test_analysis_does_not_block_other_requests(monkeypatch):
    started, release = threading.Event(), threading.Event()

    class Slow(Script):
        def chat(self, messages, tools=None, json_mode=False, **kw):
            started.set()
            release.wait(10)
            return answer(GOOD)

    monkeypatch.setattr(main, "agent", TroubleshootingAgent(Slow(None)))
    with TestClient(main.app) as c:
        result = {}
        worker = threading.Thread(target=lambda: result.update(r=c.post("/api/analyze", data={"failed_log": LOG})))
        worker.start()
        assert started.wait(5)
        other = threading.Thread(target=lambda: result.update(s=c.get("/api/samples")))
        other.start()
        other.join(3)
        answered_while_busy = not other.is_alive()
        release.set()
        worker.join(10)
        other.join(5)
    assert answered_while_busy, "/api/samples waited for the running analysis"
    assert result["r"].status_code == 200 and result["r"].json()["mode"] == "llm"
