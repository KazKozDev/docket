"""Escalation: when Tesseract's text passes the confidence gate but the
extraction then fails validation, re-read the document with the vision
model and keep the better result.

The motivating case is in the README: Tesseract reported 77.5 confidence
(floor: 60) on an invoice where it read "$530.00" as "$830.00" and dropped
the grand-total line entirely. Confidence measures how crisp the pixels
looked; validation measures whether the numbers mean anything.
"""
from datetime import date
from pathlib import Path

import pytest

from docket import pipeline, review_queue
from docket.ocr import AcquisitionError
from docket.result import DocumentResult
from docket.schemas import Invoice, ValidationIssue
from tests.factories import acquisition, make_result, text_acquisition, words_page

_ERROR = [ValidationIssue(field="total_amount", message="does not add up")]


def _ocr_reading():
    return acquisition([words_page(["TOTAL 830.00"], backend="tesseract")])


def _vlm_reading():
    return text_acquisition("TOTAL 530.00", backend="vlm")


def _pdf_reading():
    return acquisition([words_page(["TOTAL 530.00"], backend="pdf_text", confidence=None)], primary=None)


def _result(issues, backend) -> DocumentResult:
    return make_result(validation_issues=issues, document_type="invoice", schema_id=backend)


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(pipeline, "looks_garbled", lambda _text: False)
    path = tmp_path / "doc.png"
    path.write_bytes(b"not read: acquisition is scripted")
    return path


def _script(monkeypatch, first, first_issues, second_issues, *, second_fails=False):
    """First pass reads `first`; an escalated pass reads with the vision model."""
    calls: list[bool] = []

    def fake_acquire(path, options, stage_seconds):
        calls.append(options.escalate)
        if options.escalate:
            if second_fails:
                raise AcquisitionError("vision model unavailable")
            return _vlm_reading()
        return first

    def fake_run_once(path, document_id, acq, *, on_stage, stage_seconds):
        vlm = acq.layout.pages[0].backend == "vlm"
        return _result(second_issues if vlm else first_issues, "vlm" if vlm else "ocr")

    monkeypatch.setattr(pipeline, "_acquire", fake_acquire)
    monkeypatch.setattr(pipeline, "_run_once", fake_run_once)
    return calls


def test_validation_failure_on_ocr_text_escalates(source, monkeypatch):
    calls = _script(monkeypatch, _ocr_reading(), _ERROR, [])
    result = pipeline.process_document(source)
    assert calls == [False, True]
    assert result.metrics.escalated_to_vlm is True
    assert result.needs_review is False


def test_clean_ocr_result_does_not_pay_for_a_vlm_call(source, monkeypatch):
    calls = _script(monkeypatch, _ocr_reading(), [], [])
    result = pipeline.process_document(source)
    assert calls == [False]
    assert result.metrics.escalated_to_vlm is False


def test_a_tie_goes_to_the_vision_model(source, monkeypatch):
    """Equal error counts aren't a reason to keep the OCR reading. Getting
    here at all means that reading already failed validation, so it has no
    claim to the benefit of the doubt.
    """
    calls = _script(monkeypatch, _ocr_reading(), _ERROR, _ERROR)
    result = pipeline.process_document(source)
    assert calls == [False, True]
    assert result.metrics.escalated_to_vlm is True
    assert result.schema_id == "vlm"
    assert result.needs_review is True


def test_a_strictly_worse_vlm_reading_loses(source, monkeypatch):
    two_errors = _ERROR + [ValidationIssue(field="tax_amount", message="unverifiable")]
    calls = _script(monkeypatch, _ocr_reading(), _ERROR, two_errors)
    result = pipeline.process_document(source)
    assert calls == [False, True]
    assert result.metrics.escalated_to_vlm is False
    assert result.schema_id == "ocr"


def test_escalation_survives_an_unavailable_vision_model(source, monkeypatch):
    calls = _script(monkeypatch, _ocr_reading(), _ERROR, [], second_fails=True)
    result = pipeline.process_document(source)
    assert calls == [False, True]
    assert result.needs_review is True


def test_pdf_text_failures_do_not_escalate(source, monkeypatch):
    """A born-digital PDF's text layer is exact — if validation failed there,
    the document is wrong, not the transcription. Don't pay for a VLM call.
    """
    calls = _script(monkeypatch, _pdf_reading(), _ERROR, [])
    pipeline.process_document(source)
    assert calls == [False]


def test_no_fallback_means_no_escalation(source, monkeypatch):
    calls = _script(monkeypatch, _ocr_reading(), _ERROR, [])
    pipeline.process_document(source, ocr_fallbacks=[])
    assert calls == [False]


def test_garbled_ocr_is_reread_before_extraction(source, monkeypatch):
    calls = _script(monkeypatch, _ocr_reading(), [], [])
    monkeypatch.setattr(pipeline, "looks_garbled", lambda _text: True)
    result = pipeline.process_document(source)
    assert calls == [False, True]
    assert result.schema_id == "vlm"


def test_discount_in_accounting_parentheses_is_normalized():
    """($371.00) transcribes as -371.0; the schema's contract is a positive
    magnitude, so the sign is normalized rather than trusted.
    """
    inv = Invoice(
        invoice_number="INV-1",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme",
        customer_name="Bob",
        subtotal=5300.0,
        tax_amount=530.0,
        discount_amount=-371.0,
        total_amount=5459.0,
    )
    assert inv.discount_amount == 371.0
