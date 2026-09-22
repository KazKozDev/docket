"""The OCR-quality judge is the only model allowed near a quality call, so
its contract is pinned tightly: it must fail safe, respect its config flag,
and demand real confidence before spending a vision call.

The behavioural result it exists for is measured, not asserted here — see
`ocr_quality.py` for the numbers and for the two cheaper signals that were
tried and rejected first.
"""
from unittest.mock import patch

from docket import config, ocr_quality
from docket.logging_setup import JsonFormatter


def _judge(response: dict):
    return patch.object(ocr_quality, "chat_json", return_value=response)


def test_unusable_with_high_confidence_triggers_a_re_read():
    with _judge({"unusable": True, "confidence": 0.95, "evidence": "sus TOTAL"}):
        assert ocr_quality.looks_garbled("sus TOTAL $1250") is True


def test_usable_text_does_not():
    with _judge({"unusable": False, "confidence": 0.9, "evidence": "coherent"}):
        assert ocr_quality.looks_garbled("SUB TOTAL $1250") is False


def test_low_confidence_is_not_enough_to_spend_a_vision_call(monkeypatch):
    monkeypatch.setattr(config, "OCR_QUALITY_MIN_CONFIDENCE", 0.7)
    with _judge({"unusable": True, "confidence": 0.4, "evidence": "not sure"}):
        assert ocr_quality.looks_garbled("something") is False


def test_an_unavailable_judge_fails_safe(monkeypatch):
    """A broken judge must not stall a document. Returning False keeps the
    existing path, and every deterministic check downstream still runs.
    """

    def boom(_prompt):
        raise ocr_quality.LLMError("Ollama request failed")

    monkeypatch.setattr(ocr_quality, "chat_json", boom)
    assert ocr_quality.looks_garbled("anything") is False


def test_the_check_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "OCR_QUALITY_CHECK", False)
    with _judge({"unusable": True, "confidence": 1.0, "evidence": "garbage"}) as judge:
        assert ocr_quality.looks_garbled("sus TOTAL") is False
        assert not judge.called


def test_empty_text_costs_nothing():
    with _judge({"unusable": True, "confidence": 1.0}) as judge:
        assert ocr_quality.looks_garbled("   ") is False
        assert not judge.called


def test_a_malformed_judge_response_does_not_raise():
    with _judge({}):
        assert ocr_quality.looks_garbled("anything") is False


def test_quality_check_does_not_ignore_the_end_of_a_long_document(monkeypatch):
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return {
            "unusable": "TAIL_GARBLED" in prompt,
            "confidence": 1.0,
            "evidence": "tail",
        }

    monkeypatch.setattr(ocr_quality, "chat_json", judge)
    assert ocr_quality.looks_garbled("x" * 3100 + "TAIL_GARBLED") is True
    assert len(prompts) == 2


def test_quality_logs_do_not_include_document_evidence(monkeypatch, caplog):
    secret = "customer account 123456"
    monkeypatch.setattr(config, "OCR_QUALITY_MIN_CONFIDENCE", 0.7)
    monkeypatch.setattr(
        ocr_quality,
        "chat_json",
        lambda _prompt: {"unusable": True, "confidence": 0.9, "evidence": secret},
    )

    with caplog.at_level("INFO", logger="docket"):
        assert ocr_quality.looks_garbled(secret)

    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert secret not in rendered
