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

from docket import pipeline, review_queue
from docket.llm_client import LLMError
from docket.schemas import ClassificationResult, DocType, Invoice, PipelineResult, ValidationIssue


def _result(ocr_method: str, issues: list[ValidationIssue]) -> PipelineResult:
    return PipelineResult(
        source="doc.png",
        classification=ClassificationResult(doc_type=DocType.INVOICE, confidence=0.9, method="rules"),
        extracted={"invoice_number": "INV-1"},
        extract_attempts=1,
        validation_issues=issues,
        ocr_method=ocr_method,
        raw_text_chars=100,
    )


_ERROR = [ValidationIssue(field="total_amount", message="does not add up")]


def _runner(first: PipelineResult, second: PipelineResult | None):
    calls = []

    def _run_once(path, *, on_stage, force_vlm=False):
        calls.append(force_vlm)
        if force_vlm:
            if second is None:
                raise LLMError("vision model unavailable")
            return second
        return first

    return _run_once, calls


def test_validation_failure_on_ocr_text_escalates(monkeypatch, tmp_path):
    clean = _result("vlm", [])
    run_once, calls = _runner(_result("ocr", _ERROR), clean)
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    result = pipeline.process(Path("doc.png"))
    assert calls == [False, True]
    assert result.escalated_to_vlm is True
    assert result.needs_review is False


def test_clean_ocr_result_does_not_pay_for_a_vlm_call(monkeypatch, tmp_path):
    run_once, calls = _runner(_result("ocr", []), _result("vlm", []))
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    result = pipeline.process(Path("doc.png"))
    assert calls == [False]
    assert result.escalated_to_vlm is False


def test_a_tie_goes_to_the_vision_model(monkeypatch, tmp_path):
    """Equal error counts aren't a reason to keep the OCR reading. Getting
    here at all means that reading already failed validation, so it has no
    claim to the benefit of the doubt.
    """
    run_once, calls = _runner(_result("ocr", _ERROR), _result("vlm", _ERROR))
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    result = pipeline.process(Path("doc.png"))
    assert calls == [False, True]
    assert result.escalated_to_vlm is True
    assert result.ocr_method == "vlm"
    assert result.needs_review is True


def test_a_strictly_worse_vlm_reading_loses(monkeypatch, tmp_path):
    two_errors = [
        ValidationIssue(field="total_amount", message="does not add up"),
        ValidationIssue(field="tax_amount", message="unverifiable"),
    ]
    run_once, calls = _runner(_result("ocr", _ERROR), _result("vlm", two_errors))
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    result = pipeline.process(Path("doc.png"))
    assert calls == [False, True]
    assert result.escalated_to_vlm is False
    assert result.ocr_method == "ocr"


def test_escalation_survives_an_unavailable_vision_model(monkeypatch, tmp_path):
    run_once, calls = _runner(_result("ocr", _ERROR), None)
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    result = pipeline.process(Path("doc.png"))
    assert calls == [False, True]
    assert result.needs_review is True


def test_pdf_text_failures_do_not_escalate(monkeypatch, tmp_path):
    """A born-digital PDF's text layer is exact — if validation failed there,
    the document is wrong, not the transcription. Don't pay for a VLM call.
    """
    run_once, calls = _runner(_result("pdf_text", _ERROR), _result("vlm", []))
    monkeypatch.setattr(pipeline, "_run_once", run_once)
    monkeypatch.setattr(review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")

    pipeline.process(Path("doc.pdf"))
    assert calls == [False]


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
