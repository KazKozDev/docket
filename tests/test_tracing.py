"""Tracing is optional plumbing, which makes it exactly the kind of thing
that rots unnoticed: it's off by default, so a broken Langfuse call would
never show up in a normal run. These tests pin both halves of the
contract — off means silent, on means a generation span per call — and
that a failing tracer can't take the pipeline with it.
"""
import time
from unittest.mock import MagicMock

from docket import config, llm_client


def test_tracing_is_a_noop_without_keys(monkeypatch):
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", None)
    monkeypatch.setattr(config, "LANGFUSE_SECRET_KEY", None)
    assert llm_client._get_langfuse() is None


def test_trace_emits_a_generation_span(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(llm_client, "_get_langfuse", lambda: client)

    llm_client._trace("chat_json", "some-model", "in", "out", time.monotonic())

    kwargs = client.start_observation.call_args.kwargs
    assert kwargs["as_type"] == "generation"
    assert kwargs["model"] == "some-model"
    assert client.start_observation.return_value.end.called
    assert client.flush.called


def test_document_text_is_not_traced_unless_enabled(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(llm_client, "_get_langfuse", lambda: client)

    monkeypatch.setattr(config, "LANGFUSE_CONTENT", False)
    llm_client._trace("chat_json", "m", "Invoice INV-7 IBAN DE89...", "{}", time.monotonic())
    kwargs = client.start_observation.call_args.kwargs
    assert "input" not in kwargs and "output" not in kwargs
    assert kwargs["metadata"]["input_chars"] == len("Invoice INV-7 IBAN DE89...")

    monkeypatch.setattr(config, "LANGFUSE_CONTENT", True)
    llm_client._trace("chat_json", "m", "prompt", "answer", time.monotonic())
    assert client.start_observation.call_args.kwargs["input"] == "prompt"


def test_broken_tracer_does_not_raise(monkeypatch):
    client = MagicMock()
    client.start_observation.side_effect = RuntimeError("langfuse is having a day")
    monkeypatch.setattr(llm_client, "_get_langfuse", lambda: client)

    llm_client._trace("chat_json", "some-model", "in", "out", time.monotonic())


def test_usage_counter_accumulates_and_resets():
    llm_client.usage.reset()
    llm_client.usage.record("a" * 40, "b" * 40)
    assert llm_client.usage.calls == 1
    assert llm_client.usage.estimated_tokens == 20

    llm_client.usage.reset()
    assert llm_client.usage.snapshot() == {"calls": 0, "estimated_tokens": 0}
