import pytest

from docket import pipeline
from docket.schemas import ClassificationResult, Receipt
from docket.validate import validate
from tests.factories import acquisition, words_page
from docket.layout import text_only_page


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


def _classified_receipt(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "classify",
        lambda text: ClassificationResult(
            doc_type="receipt", confidence=1, method="rules"
        ),
    )
    monkeypatch.setattr(pipeline, "extract_pages", lambda *a, **k: (receipt(), 1))


@pytest.mark.parametrize("degraded", [False, True])
def test_image_pipeline_passes_acquired_pages_to_real_validator(
    tmp_path, monkeypatch, degraded
):
    _classified_receipt(monkeypatch)
    page = words_page(TEXT.splitlines(), confidence=0.3 if degraded else 0.9)
    acq = acquisition([page], degraded={1} if degraded else set())
    result = pipeline._run_once(
        tmp_path / "receipt.png", "doc_x", acq, on_stage=None, stage_seconds={}
    )
    assert result.validation_issues == []
    # Citations resolve to page regions through the layout.
    total = result.field_sources["total_amount"]
    assert total.bbox is not None and total.word_ids
    assert total.confidence and total.confidence > 0.8
    assert total.located_by == "tesseract"


def test_vlm_reading_without_ocr_support_is_flagged(tmp_path, monkeypatch):
    _classified_receipt(monkeypatch)
    acq = acquisition([text_only_page(page_number=1, text=TEXT, backend="vlm")])
    result = pipeline._run_once(
        tmp_path / "receipt.png", "doc_x", acq, on_stage=None, stage_seconds={}
    )
    assert any(
        i.field == "*" and "no confident OCR" in i.message
        for i in result.validation_issues
    )
    # No geometry to resolve against: the citation is kept, unlocated.
    assert result.field_sources["total_amount"].bbox is None


def test_vlm_reading_backed_by_a_witness_is_not_flagged(tmp_path, monkeypatch):
    _classified_receipt(monkeypatch)
    witness = words_page(TEXT.splitlines(), confidence=0.4)
    acq = acquisition(
        [text_only_page(page_number=1, text=TEXT, backend="vlm")], witnesses={1: witness}
    )
    result = pipeline._run_once(
        tmp_path / "receipt.png", "doc_x", acq, on_stage=None, stage_seconds={}
    )
    assert not [i for i in result.validation_issues if "no confident OCR" in i.message]
    # The vision transcript has no boxes; the quote is found in the witness.
    total = result.field_sources["total_amount"]
    assert total.bbox is not None and total.located_by == "tesseract"
    assert total.word_ids[0].startswith("p1-w")


def test_actual_absent_page_is_still_rejected():
    issues = validate(receipt(2), TEXT, pages=[TEXT])
    assert any("absent page 2" in issue.message for issue in issues)


def test_numeric_quote_must_exist_on_cited_page():
    issues = validate(receipt(), pages=["Example Shop\n2026-03-18", TEXT])
    assert any(
        issue.field == "tax_amount" and "does not appear" in issue.message
        for issue in issues
    )
