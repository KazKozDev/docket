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
        status, headers_out = outcome if isinstance(outcome, tuple) else (outcome, {})
        request = httpx.Request("POST", url)
        body = {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 50, "eval_count": 5}
        return httpx.Response(status, json=body, headers=headers_out, request=request)


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


def test_retry_after_is_honoured_and_capped(monkeypatch, server):
    slept = []
    monkeypatch.setattr(llm_client.time, "sleep", slept.append)
    server((429, {"Retry-After": "7"}), (503, {"Retry-After": "3600"}), 200)
    assert llm_client.chat_json("x", model="qwen3:8b") == {"ok": True}
    assert slept == [7.0, llm_client._MAX_RETRY_AFTER_S]


def test_retry_after_as_an_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    resp = httpx.Response(429, headers={"Retry-After": when})
    assert 25 <= llm_client._retry_delay(0, resp) <= 30


def test_backoff_without_retry_after_grows_with_jitter(monkeypatch):
    monkeypatch.setattr(llm_client, "_BACKOFF_S", 2.0)
    first = [llm_client._retry_delay(0) for _ in range(50)]
    third = [llm_client._retry_delay(2) for _ in range(50)]
    assert all(1.0 <= d <= 3.0 for d in first) and len(set(first)) > 1
    assert all(4.0 <= d <= 12.0 for d in third)


def test_provider_token_counts_are_recorded(server):
    server(200)
    llm_client.usage.reset()
    llm_client.chat_json("x", model="qwen3:8b")
    assert (llm_client.usage.input_tokens, llm_client.usage.output_tokens) == (50, 5)
    assert llm_client.usage.models == ["qwen3:8b"]

