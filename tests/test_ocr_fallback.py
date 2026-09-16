"""The VLM tier is optional infrastructure: when it's unreachable, a
document with usable-but-low-confidence OCR text should degrade to that
text and get flagged for review, not take the whole run down.
"""
from pathlib import Path

import pytest

from docket import ocr
from docket.llm_client import LLMError

PAGE = Path("page.png")


def _boom(_path):
    raise LLMError("Ollama vision request failed: timed out")


def test_vlm_failure_falls_back_to_ocr_text(monkeypatch):
    monkeypatch.setattr(ocr, "vision_transcribe", _boom)

    result = ocr._vlm_or_degraded_ocr([PAGE], "TOTAL 12.00\nSUB TTA")
    assert result.method == "ocr_degraded"
    assert "TOTAL" in result.text


def test_vlm_failure_with_no_ocr_text_raises(monkeypatch):
    monkeypatch.setattr(ocr, "vision_transcribe", _boom)

    with pytest.raises(LLMError):
        ocr._vlm_or_degraded_ocr([PAGE], "   ")


def test_working_vlm_is_preferred_over_ocr_text(monkeypatch):
    monkeypatch.setattr(ocr, "vision_transcribe", lambda _p: "clean transcription")

    result = ocr._vlm_or_degraded_ocr([PAGE], "garbled ocr")
    assert result.method == "vlm"
    assert result.text == "clean transcription"
