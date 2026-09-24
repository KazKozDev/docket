"""process_document end to end on a real born-digital PDF: layout in the
result, citations resolved to page regions, structured failures, and
configuration errors raised before any page is read."""
import pymupdf as fitz
import pytest

from docket import extract as extract_module
from docket import pipeline
from docket.ocr import BackendUnavailable, OcrBackendError, UnknownLanguage
from docket.options import OcrOptions, ProcessOptions, ReviewOptions
from docket.result import DocumentResult, DocumentStatus


def _options(**ocr):
    return ProcessOptions(ocr=OcrOptions(fallbacks=[], **ocr), review=ReviewOptions(enqueue=False))

LINES = [
    "INVOICE",
    "Invoice no: INV-2026-0042",
    "Issue date: 2026-03-02",
    "Vendor: Northgate Supplies Ltd",
    "Bill to: Iberia Mantenimiento SA",
    "Subtotal: EUR 100.00",
    "VAT: EUR 21.00",
    "Total due: EUR 121.00",
]

CITES = {
    "invoice_number": "Invoice no: INV-2026-0042",
    "issue_date": "Issue date: 2026-03-02",
    "seller.name": "Vendor: Northgate Supplies Ltd",
    "buyer.name": "Bill to: Iberia Mantenimiento SA",
    "subtotal": "Subtotal: EUR 100.00",
    "tax_amount": "VAT: EUR 21.00",
    "total_amount": "Total due: EUR 121.00",
}


def _pdf(path):
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        for n, line in enumerate(LINES):
            page.insert_text((72, 90 + 22 * n), line, fontsize=12)
        document.save(path)
    return path


def _payload():
    return {
        "invoice_number": "INV-2026-0042",
        "issue_date": "2026-03-02",
        "seller": {"name": "Northgate Supplies Ltd"},
        "buyer": {"name": "Iberia Mantenimiento SA"},
        "currency": "EUR",
        "subtotal": 100.0,
        "tax_amount": 21.0,
        "total_amount": 121.0,
        "field_locations": {f: {"page": 1, "quote": q} for f, q in CITES.items()},
    }


@pytest.fixture
def invoice_result(tmp_path, monkeypatch) -> DocumentResult:
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _payload())
    return pipeline.process_document(_pdf(tmp_path / "inv.pdf"), _options())


def test_result_carries_layout_and_acquisition_report(invoice_result):
    result = invoice_result
    assert result.status == DocumentStatus.SUCCEEDED, result.review_reasons
    assert result.document_type == "invoice" and result.schema_id == "invoice"
    assert result.ocr.pages[0].backend == "pdf_text"
    page = result.layout.pages[0]
    assert page.unit == "pt" and page.words
    assert result.metrics.pages == 1
    assert set(result.metrics.stage_seconds) >= {"acquire", "classify", "extract", "validate"}
    assert result.document_id.startswith("doc_")


def test_every_cited_field_resolves_to_a_region(invoice_result):
    sources = invoice_result.field_sources
    assert set(sources) == set(CITES)
    page = invoice_result.layout.pages[0]
    total = sources["total_amount"]
    assert total.bbox is not None and total.confidence == 1.0
    assert " ".join(page.word(i).text for i in total.word_ids) == "Total due: EUR 121.00"
    # Lines were laid out top to bottom, so the boxes are too.
    assert sources["invoice_number"].bbox.y0 < sources["total_amount"].bbox.y0
    x0, _y0, _x1, y1 = total.bbox.to_absolute(page.width, page.height)
    assert 70 <= x0 <= 74 and y1 <= page.height


def test_result_round_trips_through_json(invoice_result):
    restored = DocumentResult.model_validate_json(invoice_result.model_dump_json())
    assert restored == invoice_result
    assert restored.document.total_amount == 121.0


def test_unsupported_file_is_a_structured_failure(tmp_path):
    path = tmp_path / "doc.docx"
    path.write_bytes(b"PK")
    result = pipeline.process_document(path, _options())
    assert result.status == DocumentStatus.FAILED
    assert result.error.code == "unsupported_document" and result.error.stage == "acquire"
    assert result.needs_review and not result.is_valid


def test_unreadable_document_is_a_structured_failure(tmp_path, monkeypatch):
    from tests.factories import ScriptedBackend, write_png

    blank = ScriptedBackend("blank")
    result = pipeline.process_document(write_png(tmp_path / "blank.png"), _options(backend=blank))
    assert result.status == DocumentStatus.FAILED and result.error.code == "no_text"


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"backend": "nope"}, OcrBackendError),
        ({"languages": "klingon"}, UnknownLanguage),
        ({"fallbacks": ["nope"]}, OcrBackendError),
    ],
)
def test_configuration_errors_raise_before_reading(tmp_path, kwargs, error):
    path = tmp_path / "never-read.pdf"  # does not exist: nothing may touch it
    with pytest.raises(error):
        pipeline.process_document(path, ProcessOptions(ocr=OcrOptions(**kwargs)))


def test_unavailable_explicit_backend_raises(tmp_path):
    from tests.factories import ScriptedBackend

    with pytest.raises(BackendUnavailable):
        pipeline.process_document(tmp_path / "x.png", _options(backend=ScriptedBackend("gone", available=False)))


def test_cli_reports_configuration_errors_with_exit_code_3(capsys):
    from docket import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["process", "whatever.pdf", "--ocr-backend", "nope"])
    assert exc.value.code == 3
    assert "unknown OCR backend 'nope'" in capsys.readouterr().err


def test_cli_lists_ocr_backends(capsys):
    from docket import cli

    cli.main(["ocr-backends"])
    out = capsys.readouterr().out
    assert "pdf_text" in out and "tesseract" in out and "vlm" in out
