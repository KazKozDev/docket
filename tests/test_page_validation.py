import pytest

from docket import ocr, pipeline
from docket.schemas import ClassificationResult, Receipt
from docket.validate import validate


TEXT = "Example Shop\n2026-03-18\nSUBTOTAL 167.93\nTAX 12.59\nTOTAL 180.52"


def receipt(page=1):
    return Receipt(
        merchant_name="Example Shop",
        transaction_date="2026-03-18",
        subtotal=167.93,
        tax_amount=12.59,
        total_amount=180.52,
        field_locations={
            field: {"page": page, "quote": quote}
            for field, quote in {
                "merchant_name": "Example Shop",
                "transaction_date": "2026-03-18",
                "subtotal": "SUBTOTAL 167.93",
                "tax_amount": "TAX 12.59",
                "total_amount": "TOTAL 180.52",
            }.items()
        },
    )


@pytest.mark.parametrize("method", ["ocr", "ocr_degraded"])
def test_image_pipeline_passes_acquired_pages_to_real_validator(
    tmp_path, monkeypatch, method
):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"synthetic fixture; acquisition mocked")
    monkeypatch.setattr(
        ocr, "extract_text", lambda *a, **k: ocr.OcrResult(TEXT, method)
    )
    monkeypatch.setattr(pipeline, "looks_garbled", lambda text: False)
    monkeypatch.setattr(
        pipeline,
        "classify",
        lambda text: ClassificationResult(
            doc_type="receipt", confidence=1, method="rules"
        ),
    )
    monkeypatch.setattr(pipeline, "extract_pages", lambda *a, **k: (receipt(), 1))
    result = pipeline._run_once(path, on_stage=None)
    assert result.validation_issues == []


def test_vlm_reading_without_ocr_support_is_flagged(tmp_path, monkeypatch):
    path = tmp_path / "receipt.png"
    path.write_bytes(b"synthetic fixture; acquisition mocked")
    monkeypatch.setattr(ocr, "extract_text", lambda *a, **k: ocr.OcrResult(TEXT, "vlm"))
    monkeypatch.setattr(pipeline, "looks_garbled", lambda text: False)
    monkeypatch.setattr(
        pipeline,
        "classify",
        lambda text: ClassificationResult(
            doc_type="receipt", confidence=1, method="rules"
        ),
    )
    monkeypatch.setattr(pipeline, "extract_pages", lambda *a, **k: (receipt(), 1))
    result = pipeline._run_once(path, on_stage=None)
    assert any(
        i.field == "*" and "no confident OCR" in i.message
        for i in result.validation_issues
    )


def test_actual_absent_page_is_still_rejected():
    issues = validate(receipt(2), TEXT, pages=[TEXT])
    assert any("absent page 2" in issue.message for issue in issues)


def test_numeric_quote_must_exist_on_cited_page():
    issues = validate(receipt(), pages=["Example Shop\n2026-03-18", TEXT])
    assert any(
        issue.field == "tax_amount" and "does not appear" in issue.message
        for issue in issues
    )
