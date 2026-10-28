"""Minimal client for Groq's OpenAI-compatible chat completions API."""
import json
import os
import time

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(RuntimeError):
    pass


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 45.0):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.model = model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             temperature: float = 0.1, max_tokens: int = 2048) -> dict:
        """Return the assistant message dict. Retries once on rate limits and server errors."""
        if not self.available:
            raise LLMError("GROQ_API_KEY is not set")
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error = None
        for attempt in range(2):
            try:
                resp = httpx.post(GROQ_URL, json=body, headers=headers, timeout=self.timeout)
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
                time.sleep(1.5)
                continue
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code in (429, 500, 502, 503) and attempt == 0:
                time.sleep(float(resp.headers.get("retry-after", 2)))
                continue
            break
        raise LLMError(last_error or "unknown error")


def parse_json(content: str | None) -> dict | None:
    """Parse a JSON object from model output, tolerating code fences or leading text."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
