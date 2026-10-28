"""/api/ask: follow-up questions about one report, answered by a scripted model."""
import json
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.agent import TroubleshootingAgent
from app.llm import GroqClient, LLMError


class Scripted(GroqClient):
    def __init__(self, reply):
        super().__init__(api_key="test", model="scripted")
        self.reply, self.calls = reply, []

    def chat(self, messages, tools=None, json_mode=False, **kw):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"content": self.reply if isinstance(self.reply, str) else json.dumps(self.reply)}


CONTEXT = {
    "summary": "Login to the database failed because DB_PASSWORD was not set.",
    "root_cause": {"title": "Required environment variable is missing", "kb_id": "env_var_missing", "first_line": 5,
                   "evidence_ids": [5]},
    "confidence": {"level": "high", "reasons": ["Matched knowledge base rule"]},
    "findings": [{"title": "Database login rejected", "symptom": True, "first_line": 7, "evidence_ids": [7, 9]}],
    "fixes": [{"title": "List env vars", "command": "kubectl set env deployment/orders --list", "risk": "read-only"}],
    "links": ["Database login rejected appears after the missing variable."],
    "lines": [{"id": 5, "text": "WARN Environment variable DB_PASSWORD is not set"},
              {"id": 7, "text": "ERROR FATAL: password authentication failed for user \"orders_app\""}],
}


def ask(client, question="Why is this the root cause?", **extra):
    return client.post("/api/ask", json={"question": question, "history": [], "context": CONTEXT, **extra})


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_ask_times", defaultdict(main.deque))
    return TestClient(main.app)


def use_model(monkeypatch, reply):
    llm = Scripted(reply)
    monkeypatch.setattr(main, "agent", TroubleshootingAgent(llm))
    return llm


def test_invalid_line_ids_are_removed_and_reported(client, monkeypatch):
    use_model(monkeypatch, {"answer": "The variable is unset at L5 (L999), so the login at L7 fails, see L42.",
                            "commands": [], "general": False})
    r = ask(client).json()
    assert r["available"] and r["cited"] == [5, 7] and r["removed"] == [42, 999]
    assert "L999" not in r["answer"] and "L42" not in r["answer"] and "()" not in r["answer"]
    assert r["note"] == "Removed 2 line references not found in this log: L42, L999."


def test_commands_are_risk_checked(client, monkeypatch):
    use_model(monkeypatch, {"answer": "Check the secret first with `kubectl get secret orders -n prod`.",
                            "commands": [{"command": "kubectl set env deployment/orders --list", "purpose": "List vars"},
                                         {"command": "kubectl delete pod orders-1", "purpose": "Restart"}]})
    r = ask(client).json()
    by_cmd = {c["command"]: c for c in r["commands"]}
    assert by_cmd["kubectl set env deployment/orders --list"]["risk"] == "read-only"
    assert by_cmd["kubectl delete pod orders-1"]["risk"] == "destructive"
    assert by_cmd["kubectl delete pod orders-1"]["recommended"] is False
    assert by_cmd["kubectl get secret orders -n prod"]["risk"] == "read-only"  # moved out of the prose
    assert "kubectl get secret" not in r["answer"]


def test_no_key_returns_the_off_message(client, monkeypatch):
    monkeypatch.setattr(main, "agent", TroubleshootingAgent(GroqClient(api_key="")))
    r = ask(client).json()
    assert r == {"available": False, "message": "Follow-up questions need the AI agent, which is off right now.",
                 "reason": "GROQ_API_KEY is not set"}


def test_groq_failure_returns_the_off_message_with_reason(client, monkeypatch):
    use_model(monkeypatch, LLMError("rate limited by Groq (tokens per minute)"))
    r = ask(client).json()
    assert r["available"] is False and r["reason"] == "rate limited by Groq (tokens per minute)"


def test_unreadable_answer_is_reported(client, monkeypatch):
    use_model(monkeypatch, "not json at all")
    r = ask(client)
    assert r.status_code == 200 and "could not be read" in r.json()["error"]


def test_oversized_body_is_refused(client, monkeypatch):
    llm = use_model(monkeypatch, {"answer": "x"})
    big = {**CONTEXT, "lines": [{"id": i, "text": "x" * 600} for i in range(1, 151)] * 3}
    r = client.post("/api/ask", content=json.dumps({"question": "q", "context": big}),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and not llm.calls


def test_too_many_lines_or_bad_shape_is_refused(client, monkeypatch):
    use_model(monkeypatch, {"answer": "x"})
    many = {**CONTEXT, "lines": [{"id": i, "text": "x"} for i in range(151)]}
    assert client.post("/api/ask", json={"question": "q", "context": many}).status_code == 400
    assert client.post("/api/ask", json={"question": "q"}).status_code == 400
    assert ask(client, question="   ").status_code == 400


def test_rate_limit_per_ip(client, monkeypatch):
    use_model(monkeypatch, {"answer": "ok"})
    codes = [ask(client).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    assert "Too many questions" in ask(client).json()["error"]


def test_prompt_contains_context_history_and_question(client, monkeypatch):
    llm = use_model(monkeypatch, {"answer": "ok", "general": True})
    history = [{"role": "user", "content": f"q{i}"} for i in range(8)]
    r = ask(client, question="What is a pod?", history=history).json()
    msgs = llm.calls[0]
    assert r["general"] is True
    assert "L5 WARN Environment variable DB_PASSWORD is not set" in msgs[1]["content"]
    assert [m["content"] for m in msgs[3:-1]] == [f"q{i}" for i in range(2, 8)]  # only the last 6 messages
    assert msgs[-1] == {"role": "user", "content": "What is a pod?"}


def test_long_answers_are_cut_to_150_words(client, monkeypatch):
    use_model(monkeypatch, {"answer": "word " * 400})
    assert len(ask(client).json()["answer"].split()) <= 151
