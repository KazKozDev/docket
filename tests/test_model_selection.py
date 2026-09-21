"""The UI's model pickers write straight back to `config`, which only works
because every call site resolves the model at call time. That's an easy
thing to break with a well-meant refactor (binding the default in a
function signature would do it silently), so it's pinned here.
"""
from docket import config, llm_client


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"message": {"content": "{}"}}


def _capture_payload(monkeypatch) -> dict:
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):  # noqa: ANN001
        captured.update(json)
        return _FakeResponse()

    monkeypatch.setattr(llm_client.httpx, "post", fake_post)
    return captured


def test_config_model_is_resolved_at_call_time(monkeypatch):
    captured = _capture_payload(monkeypatch)
    monkeypatch.setattr(config, "TEXT_MODEL", "picked-in-the-ui:v9")

    llm_client.chat_json("hi")
    assert captured["model"] == "picked-in-the-ui:v9"


def test_explicit_model_argument_wins_over_config(monkeypatch):
    captured = _capture_payload(monkeypatch)
    monkeypatch.setattr(config, "TEXT_MODEL", "from-config:v1")

    llm_client.chat_json("hi", model="explicit:v2")
    assert captured["model"] == "explicit:v2"


def test_thinking_flag_is_sent(monkeypatch):
    captured = _capture_payload(monkeypatch)
    monkeypatch.setattr(config, "ENABLE_THINKING", False)

    llm_client.chat_json("hi")
    assert captured["think"] is False


def test_json_schema_is_sent_to_ollama_when_provided(monkeypatch):
    captured = _capture_payload(monkeypatch)
    monkeypatch.setattr(config, "TEXT_MODEL", "local-model:latest")
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    llm_client.chat_json("hi", schema=schema)
    assert captured["format"] == schema


def test_cloud_uses_json_mode_instead_of_unsupported_schema(monkeypatch):
    captured = _capture_payload(monkeypatch)
    llm_client.chat_json("synthetic", model="example:cloud", schema={"type": "object"})
    assert captured["format"] == "json"


def test_cloud_fenced_json_is_parsed(monkeypatch):
    class Response(_FakeResponse):
        def json(self):
            return {"message": {"content": '```json\n{"total_amount": 10}\n```'}}

    monkeypatch.setattr(llm_client.httpx, "post", lambda *args, **kwargs: Response())
    assert llm_client.chat_json("synthetic", model="example:cloud") == {
        "total_amount": 10
    }


def _fake_tags(models: list[dict]):
    class Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"models": models}

    return lambda url, timeout=None: Resp()  # noqa: ANN001


MODELS = [
    {"name": "chat-only:v1", "capabilities": ["completion", "tools"]},
    {"name": "sees-things:v1", "capabilities": ["completion", "vision"]},
    {"name": "embeddings-only:v1", "capabilities": ["embedding"]},
    {"name": "no-capabilities-reported:v1"},
]


def test_embedding_models_are_not_offered(monkeypatch):
    monkeypatch.setattr(llm_client.httpx, "get", _fake_tags(MODELS))
    assert llm_client.list_models() == ["chat-only:v1", "sees-things:v1"]


def test_vision_list_requires_both_capabilities(monkeypatch):
    monkeypatch.setattr(llm_client.httpx, "get", _fake_tags(MODELS))
    assert llm_client.list_models(vision_only=True) == ["sees-things:v1"]


def test_unreachable_ollama_returns_no_choices_rather_than_raising(monkeypatch):
    def boom(url, timeout=None):  # noqa: ANN001
        raise llm_client.httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_client.httpx, "get", boom)
    assert llm_client.list_models() == []


def test_a_document_run_uses_the_selected_vision_model(monkeypatch):
    captured = _capture_payload(monkeypatch)
    monkeypatch.setattr(config, "VISION_MODEL", "picked-vision:v9")
    llm_client.vision_transcribe(b"img")
    assert captured["model"] == "picked-vision:v9"
