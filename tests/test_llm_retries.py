"""Stalled hosted models are cut short and retried; local models are given
their full timeout and never retried after one (a laptop can honestly need
minutes for a page)."""
from __future__ import annotations

import httpx
import pytest

from docket import config, llm_client


class _Server:
    """Plays back one outcome per request: an exception, or a status code."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.timeouts: list[float] = []

    def __call__(self, url, *, json, headers=None, timeout):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        request = httpx.Request("POST", url)
        return httpx.Response(outcome, json={"message": {"content": '{"ok": true}'}}, request=request)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_HOSTED_TIMEOUT_S", 60.0)
    monkeypatch.setattr(config, "LLM_RETRIES", 2)
    monkeypatch.setattr(llm_client, "_BACKOFF_S", 0.0)

    def install(*outcomes):
        fake = _Server(*outcomes)
        monkeypatch.setattr(llm_client.httpx, "post", fake)
        return fake

    return install


def test_a_stalled_cloud_model_is_cut_short_and_retried(server):
    fake = server(httpx.ReadTimeout("stalled"), 200)
    assert llm_client.chat_json("x", model="deepseek-v4.1-flash:cloud") == {"ok": True}
    assert fake.timeouts == [60.0, 60.0]


def test_a_local_model_keeps_its_timeout_and_is_not_retried_after_one(server):
    fake = server(httpx.ReadTimeout("slow laptop"), 200)
    with pytest.raises(llm_client.LLMError):
        llm_client.chat_json("x", model="qwen3:8b")
    assert fake.timeouts == [120.0]


def test_server_errors_are_retried_for_every_model(server):
    fake = server(502, 503, 200)
    assert llm_client.chat_json("x", model="qwen3:8b") == {"ok": True}
    assert len(fake.timeouts) == 3


def test_retries_run_out(server):
    fake = server(httpx.ReadTimeout("a"), httpx.ReadTimeout("b"), httpx.ReadTimeout("c"))
    with pytest.raises(llm_client.LLMError):
        llm_client.chat_json("x", model="gemma4:31b-cloud")
    assert len(fake.timeouts) == 3


def test_client_errors_are_not_retried(server):
    fake = server(400, 200)
    with pytest.raises(llm_client.LLMError):
        llm_client.chat_json("x", model="deepseek-v4.1-flash:cloud")
    assert len(fake.timeouts) == 1


@pytest.mark.parametrize("base_url, hosted", [
    ("https://api.mistral.ai/v1", True),
    ("http://localhost:8000/v1", False),
    ("http://127.0.0.1:8000/v1", False),
])
def test_openai_compatible_endpoints_are_hosted_unless_local(monkeypatch, base_url, hosted):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_BASE_URL", base_url)
    assert llm_client._hosted("any-model") is hosted
