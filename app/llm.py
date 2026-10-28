"""Minimal client for Groq's OpenAI-compatible chat completions API."""
import json
import os
import re
import time
from pathlib import Path

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_RETRIES = 4          # retries after the first attempt, for rate limits, server and network errors
MAX_WAIT_SECONDS = 30.0  # a longer wait (for example a daily token limit) fails at once instead of hanging the request
CHECK_TTL_SECONDS = 60.0


class LLMError(RuntimeError):
    """Raised with a short, user-facing reason. Never contains the API key or raw response bodies."""


def load_env_file(path: Path) -> None:
    """Load KEY=value lines into os.environ. Variables already set in the environment win."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def describe_error(status: int, body: str, model: str) -> str:
    """Turn a Groq error response into a short reason that is safe to show in the UI."""
    if status == 401:
        return "Groq rejected the API key"
    if status == 404 or "model_not_found" in body:
        return f"model {model} is not available for this API key"
    if status == 429:
        m = re.search(r"on (tokens|requests) per (minute|day)", body)
        return f"rate limited by Groq ({m.group(1)} per {m.group(2)})" if m else "rate limited by Groq"
    if status == 400 and "tool_use_failed" in body:
        return "the model produced an invalid tool call"
    if status >= 500:
        return f"Groq server error (HTTP {status})"
    return f"Groq returned HTTP {status}"


def retry_wait(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying: the retry-after header, then Groq's 'try again in 1m2.5s', then backoff."""
    header = resp.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", resp.text)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return 2.0 * 2 ** attempt


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 45.0):
        # An explicit empty key means "no LLM"; only None falls back to the environment.
        self.api_key = api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")
        self.model = model or os.getenv("GROQ_MODEL", DEFAULT_MODEL)
        self.timeout = timeout
        self.sleep = time.sleep
        self.last_error: str | None = None   # reason of the most recent failed call, cleared by a success
        self._check: tuple[float, str, str | None] | None = None  # (time, model, reason) of the last model check

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             temperature: float = 0.1, max_tokens: int = 2048) -> dict:
        """Return the assistant message dict. Retries rate limits, server and network errors with backoff."""
        if not self.available:
            raise LLMError("GROQ_API_KEY is not set")
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        reason = "unknown error"
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = httpx.post(GROQ_URL, json=body, headers=headers, timeout=self.timeout)
            except httpx.HTTPError:
                reason, wait = "Groq could not be reached (network error)", 1.5 * 2 ** attempt
            else:
                if resp.status_code == 200:
                    self.last_error = None
                    return resp.json()["choices"][0]["message"]
                reason = describe_error(resp.status_code, resp.text, self.model)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
                wait = retry_wait(resp, attempt)
                if wait > MAX_WAIT_SECONDS:
                    break
            if attempt < MAX_RETRIES:
                self.sleep(wait)
        self.last_error = reason
        raise LLMError(reason)

    def check(self) -> str | None:
        """Confirm the key and model work without spending tokens. Returns None if fine, else a reason. Cached."""
        if not self.available:
            return "GROQ_API_KEY is not set"
        now = time.monotonic()
        if self._check and self._check[1] == self.model and now - self._check[0] < CHECK_TTL_SECONDS:
            return self._check[2]
        try:
            resp = httpx.get(f"{GROQ_MODELS_URL}/{self.model}", headers={"Authorization": f"Bearer {self.api_key}"},
                             timeout=10.0)
            reason = None if resp.status_code == 200 else describe_error(resp.status_code, resp.text, self.model)
        except httpx.HTTPError:
            reason = "Groq could not be reached (network error)"
        self._check = (now, self.model, reason)
        return reason


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
