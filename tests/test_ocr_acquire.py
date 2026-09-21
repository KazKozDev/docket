"""The per-page fallback chain.

The vision model is optional infrastructure: when it's unreachable, a page
with usable-but-low-confidence OCR text degrades to that text and gets
flagged for review — it must not take the whole run down. Pages are decided
independently, so a mixed PDF reads its text layer where it has one.
"""
import importlib
from pathlib import Path

import pymupdf as fitz
import pytest

from docket.layout import text_only_page
from docket.ocr import (
    AcquisitionError,
    AcquisitionOptions,
    BackendUnavailable,
    OcrError,
    acquire,
    resolve_chain,
)
from tests.factories import ScriptedBackend, words_page, write_png


def _vlm(text="clean transcription", **kwargs):
    return ScriptedBackend(
        "vlm", {0: text_only_page(page_number=1, text=text, backend="vlm")}, geometry=False, **kwargs
    )


def _ocr(lines, confidence):
    return ScriptedBackend("tesseract", {0: words_page(lines, confidence=confidence)})


def _acquire(tmp_path, backend, fallbacks, **options):
    image = write_png(tmp_path / "page.png")
    return acquire(image, AcquisitionOptions(backend=backend, fallbacks=fallbacks, **options))


def test_confident_ocr_is_accepted_without_calling_the_fallback(tmp_path):
    vlm = _vlm()
    result = _acquire(tmp_path, _ocr(["TOTAL 12.00"], 0.9), [vlm])
    assert result.report.pages[0].backend == "tesseract"
    assert vlm.calls == []
    assert result.report.fallbacks_applied == []


def test_working_vlm_is_preferred_over_low_confidence_ocr(tmp_path):
    result = _acquire(tmp_path, _ocr(["garbled ocr"], 0.2), [_vlm()])
    page = result.report.pages[0]
    assert page.backend == "vlm" and not page.degraded
    assert result.layout.pages[0].text == "clean transcription"
    assert [a.outcome for a in page.attempts] == ["rejected", "accepted"]
    assert "confidence 0.20 < 0.60" in page.attempts[0].reason
    assert result.report.fallbacks_applied == ["vlm"]


def test_vlm_failure_falls_back_to_ocr_text(tmp_path):
    vlm = _vlm(error=OcrError("vision model failed: timed out"))
    result = _acquire(tmp_path, _ocr(["TOTAL 12.00", "SUB TTA"], 0.3), [vlm])
    page = result.report.pages[0]
    assert page.degraded and page.backend == "tesseract"
    assert "TOTAL" in result.text
    assert page.attempts[-1].outcome == "failed"


def test_vlm_failure_with_no_ocr_text_raises(tmp_path):
    vlm = _vlm(error=OcrError("vision model failed: timed out"))
    with pytest.raises(AcquisitionError, match="no text"):
        _acquire(tmp_path, _ocr([], 0.0), [vlm])


def test_escalation_keeps_ocr_only_as_witness(tmp_path):
    result = _acquire(tmp_path, _ocr(["TOTAL 12.00"], 0.95), [_vlm()], escalate=True)
    page = result.report.pages[0]
    assert page.backend == "vlm"
    assert page.attempts[0].reason.startswith("escalated")
    assert page.witness is not None and "12.00" in page.witness.text


def test_explicit_unavailable_backend_fails_before_reading(tmp_path):
    missing = ScriptedBackend("paddle", available=False)
    with pytest.raises(BackendUnavailable, match="paddle.*scripted as missing"):
        _acquire(tmp_path, missing, [])
    assert missing.calls == []


def test_unavailable_fallback_is_a_configuration_error():
    with pytest.raises(BackendUnavailable):
        resolve_chain(
            AcquisitionOptions(backend=_ocr(["x"], 0.9), fallbacks=[ScriptedBackend("x", available=False)])
        )


def test_unknown_backend_name_is_reported():
    with pytest.raises(ValueError, match="unknown OCR backend 'nope'"):
        resolve_chain(AcquisitionOptions(backend="nope", fallbacks=[]))


def test_auto_without_any_engine_uses_the_fallbacks(monkeypatch):
    acquire_module = importlib.import_module("docket.ocr.acquire")

    monkeypatch.setattr(acquire_module, "AUTO_ORDER", ("not_registered",))
    primary, fallbacks = resolve_chain(AcquisitionOptions(backend="auto", fallbacks=[_vlm()]))
    assert primary is None and [b.name for b in fallbacks] == ["vlm"]


def test_auto_with_nothing_at_all_is_an_error(monkeypatch):
    acquire_module = importlib.import_module("docket.ocr.acquire")

    monkeypatch.setattr(acquire_module, "AUTO_ORDER", ("not_registered",))
    with pytest.raises(BackendUnavailable, match="auto"):
        resolve_chain(AcquisitionOptions(backend="auto", fallbacks=[]))


def _mixed_pdf(path: Path, pages: int = 2) -> Path:
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Digital page with enough searchable text")
        for _ in range(pages - 1):
            document.new_page()
        document.save(path)
    return path


def test_mixed_pdf_uses_text_layer_and_ocr_per_page(tmp_path):
    path = _mixed_pdf(tmp_path / "mixed.pdf")
    ocr = _ocr(["Scanned second page"], 0.99)
    result = acquire(path, AcquisitionOptions(backend=ocr, fallbacks=[]))

    assert [p.backend for p in result.report.pages] == ["pdf_text", "tesseract"]
    assert ocr.calls == [2]  # the digital page never reached OCR
    assert "Digital page" in result.layout.pages[0].text
    assert result.layout.pages[1].text == "Scanned second page"
    assert result.report.pages[1].attempts[0].backend == "pdf_text"
    assert result.report.pages[1].attempts[0].outcome == "rejected"
    assert result.text.startswith("[PAGE 1]\n")


def test_blank_page_does_not_sink_the_document(tmp_path):
    path = _mixed_pdf(tmp_path / "blank-back.pdf")
    result = acquire(path, AcquisitionOptions(backend=_ocr([], 0.0), fallbacks=[]))
    assert result.report.degraded_pages == [2]
    assert not result.complete


def test_page_limit_is_enforced(tmp_path):
    from docket.ocr import UnsupportedDocument

    path = _mixed_pdf(tmp_path / "long.pdf", pages=3)
    with pytest.raises(UnsupportedDocument, match="3 pages; limit is 2"):
        acquire(path, AcquisitionOptions(backend=_ocr(["x"], 0.9), fallbacks=[], max_pages=2))


def test_plain_text_pages_split_on_form_feed(tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text("first page\fsecond page")
    result = acquire(path, AcquisitionOptions(backend=_ocr(["x"], 0.9), fallbacks=[]))
    assert result.layout.page_texts == ["first page", "second page"]
    assert {p.backend for p in result.report.pages} == {"text"}


def test_unsupported_suffix(tmp_path):
    from docket.ocr import UnsupportedDocument

    path = tmp_path / "doc.docx"
    path.write_bytes(b"x")
    with pytest.raises(UnsupportedDocument, match=".docx"):
        acquire(path, AcquisitionOptions(backend=_ocr(["x"], 0.9), fallbacks=[]))
