from datetime import date
from pathlib import Path

import pytest

from docket import review_queue, review_reasons
from docket.catalog import Invoice
from docket.schemas import ClassificationResult, ValidationIssue
from docket.result import DocumentResult
from tests.factories import acquisition, flat_invoice, flat_po, make_result, words_page


def _result(**overrides) -> DocumentResult:
    defaults = dict(
        source="doc.txt",
        extracted=flat_invoice(
            invoice_number="INV-1",
            issue_date=date(2026, 1, 1),
            vendor_name="Acme",
            customer_name="Bob",
            subtotal=100.0,
            total_amount=100.0,
        ).model_dump(mode="json"),
    )
    defaults.update(overrides)
    return make_result(**defaults)


def test_clean_result_has_no_review_reasons():
    assert review_reasons.reasons_for(_result()) == []


def test_low_confidence_triggers_review():
    result = _result(
        classification=ClassificationResult(
            doc_type="invoice", confidence=0.2, method="llm"
        )
    )
    reasons = review_reasons.reasons_for(result)
    assert any("confidence" in r for r in reasons)


def test_unknown_doc_type_triggers_review():
    result = _result(
        classification=ClassificationResult(
            doc_type="unknown", confidence=0.9, method="llm"
        )
    )
    reasons = review_reasons.reasons_for(result)
    assert any("unrecognized" in r for r in reasons)


def test_failed_extraction_triggers_review():
    result = _result(extracted=None)
    reasons = review_reasons.reasons_for(result)
    assert any("extraction failed" in r for r in reasons)


def test_error_severity_validation_issue_triggers_review():
    result = _result(
        validation_issues=[ValidationIssue(field="total_amount", message="bad total")]
    )
    reasons = review_reasons.reasons_for(result)
    assert any("validation error" in r for r in reasons)


def test_warning_severity_alone_does_not_trigger_review():
    result = _result(
        validation_issues=[
            ValidationIssue(
                field="vendor_tax_id", message="looks odd", severity="warning"
            )
        ]
    )
    assert review_reasons.reasons_for(result) == []


def test_enqueue_and_list_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(review_queue.config, "REVIEW_DATABASE_URL", f"sqlite:///{tmp_path / 'queue.db'}")
    monkeypatch.setattr(
        review_queue.config, "REVIEW_DOCUMENTS_DIR", tmp_path / "documents"
    )
    result = _result(extracted=None)
    review_queue.enqueue(result, review_reasons.reasons_for(result))

    pending = review_queue.list_pending()
    assert len(pending) == 1
    assert pending[0]["source"] == "doc.txt"

    review_queue.clear()
    assert review_queue.list_pending() == []


def test_review_preserves_original_and_records_correction_history(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(review_queue.config, "REVIEW_DATABASE_URL", f"sqlite:///{tmp_path / 'queue.db'}")
    monkeypatch.setattr(
        review_queue.config, "REVIEW_DOCUMENTS_DIR", tmp_path / "documents"
    )
    source = tmp_path / "invoice.txt"
    source.write_text("INVOICE")
    result = _result(source=str(source))

    document_id = review_queue.enqueue(result, review_reasons.reasons_for(result))
    source.unlink()
    claimed = review_queue.claim(document_id, actor="alice")
    record = review_queue.update(
        document_id,
        status="corrected",
        corrections={"total_amount": 100.0},
        actor="alice",
        lock_token=claimed["lock_token"],
        expected_version=claimed["version"],
        note="checked against original",
    )

    assert Path(record["original_path"]).read_text() == "INVOICE"
    assert record["status"] == "corrected"
    assert record["corrections"] == {"total_amount": 100.0}
    assert record["history"][-1]["actor"] == "alice"


def test_review_lock_excludes_another_reviewer(tmp_path):
    queue = f"sqlite:///{tmp_path / 'reviews.db'}"
    document_id = review_queue.enqueue(_result(), ["manual"], database_url=queue)
    alice = review_queue.claim(document_id, actor="alice", database_url=queue)

    with pytest.raises(review_queue.ReviewConflict):
        review_queue.claim(document_id, actor="bob", database_url=queue)
    with pytest.raises(review_queue.ReviewConflict):
        review_queue.update(document_id, status="approved", actor="bob", lock_token="wrong", database_url=queue)

    released = review_queue.release(document_id, actor="alice", lock_token=alice["lock_token"], database_url=queue)
    assert released["status"] == "pending" and not released["locked"]


def test_correction_creates_revision_and_revalidates(tmp_path):
    queue = f"sqlite:///{tmp_path / 'reviews.db'}"
    document_id = review_queue.enqueue(_result(), ["manual"], database_url=queue)
    claimed = review_queue.claim(document_id, actor="alice", database_url=queue)
    corrected = review_queue.update(
        document_id, status="corrected", corrections={"invoice_number": "INV-2"},
        actor="alice", lock_token=claimed["lock_token"], expected_version=claimed["version"], database_url=queue,
    )
    assert corrected["corrections"]["invoice_number"] == "INV-2"
    assert corrected["history"][-1]["action"] == "corrected"


def test_degraded_page_triggers_review():
    page = words_page(["TOTAL 12.00"], confidence=0.3)
    acq = acquisition([page], degraded={1})
    result = _result(layout=acq.layout, ocr=acq.report)
    reasons = review_reasons.reasons_for(result)
    assert any("page(s) 1" in r and "confidence gate" in r for r in reasons)


def test_page_without_text_triggers_review():
    blank = words_page([], page_number=2)
    acq = acquisition([words_page(["TOTAL 12.00"]), blank])
    reasons = review_reasons.reasons_for(_result(layout=acq.layout, ocr=acq.report))
    assert any("incomplete" in r and "2" in r for r in reasons)


def test_failed_document_reports_its_error():
    from docket.result import DocumentError, DocumentStatus

    result = _result(
        status=DocumentStatus.FAILED,
        extracted=None,
        error=DocumentError(code="no_text", stage="acquire", message="nothing readable"),
    )
    assert review_reasons.reasons_for(result) == ["acquire failed: nothing readable"]
