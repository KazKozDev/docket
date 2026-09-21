from datetime import date
from pathlib import Path

from docket import review_queue
from docket.schemas import (
    ClassificationResult,
    DocType,
    Invoice,
    PipelineResult,
    ValidationIssue,
)


def _result(**overrides) -> PipelineResult:
    defaults = dict(
        source="doc.txt",
        classification=ClassificationResult(
            doc_type=DocType.INVOICE, confidence=0.9, method="rules"
        ),
        extracted=Invoice(
            invoice_number="INV-1",
            issue_date=date(2026, 1, 1),
            vendor_name="Acme",
            customer_name="Bob",
            subtotal=100.0,
            total_amount=100.0,
        ).model_dump(mode="json"),
        extract_attempts=1,
        validation_issues=[],
        ocr_method="pdf_text",
        raw_text_chars=100,
    )
    defaults.update(overrides)
    return PipelineResult(**defaults)


def test_clean_result_has_no_review_reasons():
    assert review_queue.reasons_for(_result()) == []


def test_low_confidence_triggers_review():
    result = _result(
        classification=ClassificationResult(
            doc_type=DocType.INVOICE, confidence=0.2, method="llm"
        )
    )
    reasons = review_queue.reasons_for(result)
    assert any("confidence" in r for r in reasons)


def test_unknown_doc_type_triggers_review():
    result = _result(
        classification=ClassificationResult(
            doc_type=DocType.UNKNOWN, confidence=0.9, method="llm"
        )
    )
    reasons = review_queue.reasons_for(result)
    assert any("unrecognized" in r for r in reasons)


def test_failed_extraction_triggers_review():
    result = _result(extracted=None)
    reasons = review_queue.reasons_for(result)
    assert any("extraction failed" in r for r in reasons)


def test_error_severity_validation_issue_triggers_review():
    result = _result(
        validation_issues=[ValidationIssue(field="total_amount", message="bad total")]
    )
    reasons = review_queue.reasons_for(result)
    assert any("validation error" in r for r in reasons)


def test_warning_severity_alone_does_not_trigger_review():
    result = _result(
        validation_issues=[
            ValidationIssue(
                field="vendor_tax_id", message="looks odd", severity="warning"
            )
        ]
    )
    assert review_queue.reasons_for(result) == []


def test_enqueue_and_list_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "queue.jsonl"
    )
    monkeypatch.setattr(
        review_queue.config, "REVIEW_DOCUMENTS_DIR", tmp_path / "documents"
    )
    result = _result(extracted=None)
    review_queue.enqueue(result, review_queue.reasons_for(result))

    pending = review_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["source"] == "doc.txt"

    review_queue.clear()
    assert review_queue.list_pending() == []


def test_review_preserves_original_and_records_correction_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        review_queue.config, "REVIEW_QUEUE_PATH", tmp_path / "queue.jsonl"
    )
    monkeypatch.setattr(
        review_queue.config, "REVIEW_DOCUMENTS_DIR", tmp_path / "documents"
    )
    source = tmp_path / "invoice.txt"
    source.write_text("INVOICE")
    result = _result(source=str(source), extracted=None)

    document_id = review_queue.enqueue(result, review_queue.reasons_for(result))
    source.unlink()
    record = review_queue.update(
        document_id,
        status="approved",
        corrections={"total_amount": 100.0},
        actor="alice",
        note="checked against original",
    )

    assert Path(record["original_path"]).read_text() == "INVOICE"
    assert record["status"] == "approved"
    assert record["corrections"] == {"total_amount": 100.0}
    assert record["history"][-1]["actor"] == "alice"
