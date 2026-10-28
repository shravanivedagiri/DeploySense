"""Tests never use the network or the real GROQ_API_KEY.

The key is blanked before app.main is imported, so the .env file cannot fill it in
(load_env_file never overrides a variable that is already set). Any real HTTP call fails the test."""
import os

os.environ["GROQ_API_KEY"] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("tests must not call the network")
    monkeypatch.setattr(httpx, "post", blocked)
    monkeypatch.setattr(httpx, "get", blocked)
