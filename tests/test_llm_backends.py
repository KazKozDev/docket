"""Pluggable LLM backends: an embedder's own backend per call, the
OpenAI-compatible path's JSON Schema with its json_object fallback, and the
configuration errors raised before any document is read."""
from __future__ import annotations

import httpx
import pytest

from docket import (
    ConfigurationError,
    LLMBackend,
    LLMError,
    LLMReply,
    OcrOptions,
    OllamaBackend,
    OpenAICompatibleBackend,
    ProcessOptions,
    config,
    llm_client,
    process_document,
)
from docket.options import resolve

INVOICE_TEXT = "INVOICE\nInvoice no: INV-7\nDate: 2026-03-02\nDue date: 2026-04-01\nTotal: 121.00\n"


class RecordingBackend:
    text_model = "my-model"
    vision_model = ""

    def __init__(self, answer: str = "{}", error: Exception | None = None):
        self.answer, self.error = answer, error
        self.schemas: list[dict | None] = []

    def generate_json(self, prompt, *, schema, timeout):
        self.schemas.append(schema)
        if self.error is not None:
            raise self.error
        return LLMReply(self.answer, input_tokens=10, output_tokens=2)

    def transcribe_image(self, image, *, mime, prompt, timeout):
        raise AssertionError("no vision model")


def test_a_plain_class_satisfies_the_protocol():
    assert isinstance(RecordingBackend(), LLMBackend)
    assert isinstance(OllamaBackend(), LLMBackend) and isinstance(OpenAICompatibleBackend(), LLMBackend)


def test_process_document_uses_the_backend_it_is_given(tmp_path):
    path = tmp_path / "invoice.txt"
    path.write_text(INVOICE_TEXT)
    backend = RecordingBackend()
    options = ProcessOptions(document_type="invoice", llm=backend, ocr=OcrOptions(fallbacks=[]))
    result = process_document(path, options)
    assert backend.schemas and backend.schemas[0] is not None  # extraction sent its schema
    assert result.metrics.llm_models == ["my-model"]
    assert result.metrics.llm_input_tokens == 10 * result.metrics.llm_calls
    # The backend is scoped to the run: afterwards calls go to the default again.
    assert not isinstance(llm_client.current_backend(), RecordingBackend)


def test_a_backends_own_exception_becomes_an_llm_error():
    backend = RecordingBackend(error=RuntimeError("gateway down"))
    with llm_client.use_backend(backend), pytest.raises(LLMError, match="RuntimeError: gateway down"):
        llm_client.chat_json("x")


def test_no_text_model_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(config, "TEXT_MODEL", "")
    with pytest.raises(ConfigurationError, match="DOCKET_TEXT_MODEL"):
        resolve(ProcessOptions(ocr=OcrOptions(fallbacks=[])))


def test_vlm_fallback_without_a_vision_model_is_a_configuration_error(monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "")
    with pytest.raises(ConfigurationError, match="no vision model"):
        resolve(ProcessOptions(ocr=OcrOptions(fallbacks=["vlm"])))


def test_the_vision_model_is_checked_on_the_given_backend(monkeypatch):
    monkeypatch.setattr(config, "VISION_MODEL", "")
    backend = OllamaBackend(text_model="t", vision_model="v")
    assert resolve(ProcessOptions(llm=backend, ocr=OcrOptions(fallbacks=["vlm"]))).llm is backend


def test_a_vision_server_without_a_key_is_usable(monkeypatch):
    # vLLM or llama.cpp on the LAN usually runs without one.
    monkeypatch.setattr(config, "LLM_API_KEY", None)
    backend = OpenAICompatibleBackend(base_url="http://gpu-box:8000/v1", text_model="t", vision_model="v")
    resolve(ProcessOptions(llm=backend, ocr=OcrOptions(fallbacks=["vlm"])))


class _Server:
    """An OpenAI-compatible server that may refuse json_schema."""

    def __init__(self, refuse_schema: bool):
        self.refuse_schema = refuse_schema
        self.formats: list[dict | None] = []

    def __call__(self, url, *, json, headers=None, timeout):
        response_format = json.get("response_format")
        self.formats.append(response_format)
        request = httpx.Request("POST", url)
        if self.refuse_schema and (response_format or {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": "unsupported response_format"}, request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}, request=request)


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


@pytest.fixture
def openai_server(monkeypatch):
    monkeypatch.setattr(llm_client, "_JSON_SCHEMA_REFUSED", set())

    def install(refuse_schema=False):
        server = _Server(refuse_schema)
        monkeypatch.setattr(llm_client.httpx, "post", server)
        return server

    return install


def test_the_schema_is_sent_as_json_schema(openai_server):
    server = openai_server()
    backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1", text_model="m")
    with llm_client.use_backend(backend):
        assert llm_client.chat_json("x", schema=SCHEMA) == {"ok": True}
    sent = server.formats[0]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["schema"] == SCHEMA and sent["json_schema"]["strict"] is False


def test_a_server_refusing_json_schema_gets_json_object_from_then_on(openai_server):
    server = openai_server(refuse_schema=True)
    backend = OpenAICompatibleBackend(base_url="http://localhost:8000/v1", text_model="m")
    with llm_client.use_backend(backend):
        assert llm_client.chat_json("x", schema=SCHEMA) == {"ok": True}
        assert llm_client.chat_json("y", schema=SCHEMA) == {"ok": True}
    kinds = [f["type"] for f in server.formats]
    assert kinds == ["json_schema", "json_object", "json_object"]


def test_json_object_setting_never_sends_the_schema(openai_server, monkeypatch):
    server = openai_server()
    monkeypatch.setattr(config, "LLM_STRUCTURED_OUTPUT", "json_object")
    with llm_client.use_backend(OpenAICompatibleBackend(base_url="http://localhost:8000/v1", text_model="m")):
        llm_client.chat_json("x", schema=SCHEMA)
    assert server.formats == [{"type": "json_object"}]


def test_backend_arguments_win_over_config(monkeypatch):
    captured: dict = {}

    def fake_post(url, *, json, headers=None, timeout):
        captured.update(url=url, model=json["model"])
        return httpx.Response(200, json={"message": {"content": "{}"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://from-config:11434")
    with llm_client.use_backend(OllamaBackend(host="http://gpu-box:11434/", text_model="qwen3:14b")):
        llm_client.chat_json("x")
    assert captured == {"url": "http://gpu-box:11434/api/chat", "model": "qwen3:14b"}
