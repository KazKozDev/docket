"""The canonical pipeline contract: ProcessOptions, explicit schema
selection, option precedence, layout trimming, review settings, export."""
from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import BaseModel

from docket import (
    CitedDocument,
    ConfigurationError,
    ExportError,
    ExportOptions,
    OcrOptions,
    ProcessOptions,
    ReviewOptions,
    config,
    export_document,
    pipeline,
)
from docket import classify as classify_module
from docket import extract as extract_module
from docket.catalog import Invoice, load_schema
from docket.catalog import SchemaError as DocumentTypeError
from docket.options import resolve
from docket.result import DocumentStatus

INVOICE_TEXT = (
    "INVOICE\nInvoice no: INV-7\nDate: 2026-03-02\nFrom: Acme GmbH\nTo: Beta SA\n"
    "Subtotal: 100.00\nVAT: 21.00\nTotal: 121.00\n"
)


def _invoice_payload(**overrides):
    payload = {
        "invoice_number": "INV-7",
        "issue_date": "2026-03-02",
        "seller": {"name": "Acme GmbH"},
        "buyer": {"name": "Beta SA"},
        "currency": "EUR",
        "subtotal": 100.0,
        "tax_amount": 21.0,
        "total_amount": 121.0,
        "field_locations": {
            "invoice_number": {"page": 1, "quote": "Invoice no: INV-7"},
            "issue_date": {"page": 1, "quote": "Date: 2026-03-02"},
            "seller.name": {"page": 1, "quote": "From: Acme GmbH"},
            "buyer.name": {"page": 1, "quote": "To: Beta SA"},
            "subtotal": {"page": 1, "quote": "Subtotal: 100.00"},
            "tax_amount": {"page": 1, "quote": "VAT: 21.00"},
            "total_amount": {"page": 1, "quote": "Total: 121.00"},
        },
    }
    payload.update(overrides)
    return payload


class Ticket(CitedDocument):
    """Parking ticket issued by a municipality."""

    ticket_number: str
    plate: str
    fine_amount: float


class PlainNote(BaseModel):
    """Anything without citations."""

    title: str


@pytest.fixture
def txt(tmp_path):
    def write(text=INVOICE_TEXT, name="doc.txt"):
        path = tmp_path / name
        path.write_text(text)
        return path

    return write


@pytest.fixture
def quiet(tmp_path):
    """Options that never touch the real review queue or a fallback."""

    def build(**kwargs):
        review = kwargs.pop("review", ReviewOptions(enqueue=False))
        ocr = kwargs.pop("ocr", OcrOptions(fallbacks=[]))
        return ProcessOptions(ocr=ocr, review=review, **kwargs)

    return build


def _no_classifier(monkeypatch):
    monkeypatch.setattr(pipeline, "classify", lambda text: pytest.fail("classification must not run"))


# ---- schema selection -------------------------------------------------------


def test_explicit_document_type_skips_classification(txt, quiet, monkeypatch):
    _no_classifier(monkeypatch)
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload())
    result = pipeline.process_document(txt(), quiet(document_type="invoice"))
    assert result.status == DocumentStatus.SUCCEEDED, result.review_reasons
    assert result.classification is None
    assert result.document_type == "invoice" and result.schema_id == "invoice"
    assert isinstance(result.document, Invoice)


def test_unregistered_pydantic_schema_is_extracted_and_citation_checked(txt, quiet, monkeypatch):
    _no_classifier(monkeypatch)
    text = "PARKING TICKET\nTicket no. PT-99\nPlate: B-XY 123\nFine: 35.00 EUR\n"
    payload = {
        "ticket_number": "PT-99",
        "plate": "B-XY 123",
        "fine_amount": 35.0,
        "field_locations": {
            "ticket_number": {"page": 1, "quote": "Ticket no. PT-99"},
            "plate": {"page": 1, "quote": "Plate: B-XY 999"},  # not on the page
            "fine_amount": {"page": 1, "quote": "Fine: 35.00 EUR"},
        },
    }
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    result = pipeline.process_document(txt(text), quiet(schema_model=Ticket))
    assert result.schema_id == f"{__name__}:Ticket"
    assert result.extracted["ticket_number"] == "PT-99"
    assert "field_locations" not in result.extracted
    assert any(i.field == "plate" for i in result.validation_issues)
    assert result.status == DocumentStatus.NEEDS_REVIEW


def test_schema_without_citations(txt, quiet, monkeypatch):
    _no_classifier(monkeypatch)
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: {"title": "Minutes"})
    result = pipeline.process_document(txt("Minutes of the meeting"), quiet(schema_model=PlainNote))
    assert result.extracted == {"title": "Minutes"}
    assert result.field_sources == {} and result.is_valid


def test_registered_type_and_matching_schema_agree(quiet):
    resolved = resolve(quiet(document_type="invoice", schema_model=Invoice))
    assert resolved.document_type.schema_id == "invoice"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"document_type": "spaceship"}, "unknown schema 'spaceship'; known: invoice"),
        ({"document_type": "invoice", "schema_model": Ticket}, "uses Invoice, not Ticket"),
        ({"classify": False}, "classify=False needs document_type or schema_model"),
    ],
)
def test_schema_configuration_errors(quiet, kwargs, message):
    with pytest.raises(ConfigurationError, match=message):
        resolve(quiet(**kwargs))


def test_classification_runs_when_nothing_is_fixed(txt, quiet, monkeypatch):
    calls = []
    real = classify_module.classify
    monkeypatch.setattr(pipeline, "classify", lambda text: calls.append(text) or real(text))
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload())
    result = pipeline.process_document(txt(), quiet())
    assert calls and result.classification.doc_type == "invoice"


def test_load_schema_from_import_path():
    assert load_schema("docket.catalog:Invoice") is Invoice
    assert load_schema(f"{__name__}:Ticket") is Ticket
    for spec, message in [
        ("docket.catalog.Invoice", "must look like"),
        ("no_such_pkg_xyz:Model", "cannot import"),
        ("docket.catalog:Nope", "has no attribute"),
        ("docket.config:MAX_PDF_PAGES", "not a Pydantic"),
    ]:
        with pytest.raises(DocumentTypeError, match=message):
            load_schema(spec)


def test_non_model_schema_is_rejected():
    from docket.catalog import resolve as resolve_type

    with pytest.raises(ValueError, match="subclass of BaseModel"):
        ProcessOptions(schema_model=dict)
    with pytest.raises(DocumentTypeError, match="Pydantic BaseModel subclass"):
        resolve_type(model=dict)


# ---- precedence -----------------------------------------------------------------


def test_environment_fills_unset_options(monkeypatch):
    monkeypatch.setattr(config, "OCR_FALLBACKS", [])
    monkeypatch.setattr(config, "OCR_LANGUAGES", "de,fr")
    monkeypatch.setattr(config, "OCR_MIN_CONFIDENCE", 0.42)
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    resolved = resolve(ProcessOptions())
    assert resolved.acquisition.fallbacks == []
    assert resolved.acquisition.settings.languages == ["de", "fr"]
    assert resolved.acquisition.min_confidence == 0.42
    assert resolved.review.enqueue is False


def test_explicit_options_beat_the_environment(monkeypatch):
    monkeypatch.setattr(config, "OCR_LANGUAGES", "de")
    monkeypatch.setattr(config, "OCR_MIN_CONFIDENCE", 0.42)
    monkeypatch.setattr(config, "MIN_CLASSIFICATION_CONFIDENCE", 0.9)
    resolved = resolve(
        ProcessOptions(
            ocr=OcrOptions(languages=["en"], min_confidence=0.8, fallbacks=[]),
            review=ReviewOptions(min_classification_confidence=0.1),
        )
    )
    assert resolved.acquisition.settings.languages == ["en"]
    assert resolved.acquisition.min_confidence == 0.8
    assert resolved.review.min_classification_confidence == 0.1


def test_unknown_option_is_rejected():
    with pytest.raises(ValueError):
        ProcessOptions(enqueue_review=False)


# ---- layout and review ----------------------------------------------------------


def test_layout_can_be_left_out_but_locations_stay(tmp_path, quiet, monkeypatch):
    from tests.test_document_result import _payload, _pdf

    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _payload())
    result = pipeline.process_document(_pdf(tmp_path / "inv.pdf"), quiet(include_layout=False))
    assert result.layout is None
    assert all(p.witness is None for p in result.ocr.pages)
    assert result.field_sources["total_amount"].bbox is not None
    assert result.metrics.pages == 1


def test_review_threshold_and_database_are_per_call(txt, tmp_path, monkeypatch):
    from docket import review_queue
    from docket.schemas import ClassificationResult

    monkeypatch.setattr(
        pipeline, "classify",
        lambda text: ClassificationResult(doc_type="invoice", confidence=0.5, method="tfidf"),
    )
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload())
    queue = f"sqlite:///{tmp_path / 'q' / 'review.db'}"
    strict = ProcessOptions(
        ocr=OcrOptions(fallbacks=[]),
        review=ReviewOptions(
            enqueue=True, min_classification_confidence=0.9, database_url=queue, documents_dir=tmp_path / "docs"
        ),
    )
    result = pipeline.process_document(txt(), strict)
    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("low classification confidence (0.50 < 0.90)" in r for r in result.review_reasons)
    assert [r["document_id"] for r in review_queue.list_pending(database_url=queue)] == [result.document_id]

    lenient = strict.model_copy(
        update={"review": strict.review.model_copy(update={"min_classification_confidence": 0.3})}
    )
    assert pipeline.process_document(txt(), lenient).status == DocumentStatus.SUCCEEDED


def test_failed_documents_are_not_queued(tmp_path, monkeypatch):
    queue = tmp_path / "review.db"
    path = tmp_path / "doc.docx"
    path.write_bytes(b"PK")
    result = pipeline.process_document(
        path, ProcessOptions(ocr=OcrOptions(fallbacks=[]), review=ReviewOptions(enqueue=True, database_url=f"sqlite:///{queue}"))
    )
    assert result.status == DocumentStatus.FAILED and result.needs_review
    assert not queue.exists()


def test_missing_file_is_a_structured_failure(tmp_path, quiet):
    result = pipeline.process_document(tmp_path / "gone.pdf", quiet())
    assert result.status == DocumentStatus.FAILED
    assert result.error.code == "unreadable_file"


# ---- export ----------------------------------------------------------------------


def _processed(txt, quiet, monkeypatch, payload):
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    return pipeline.process_document(txt(), quiet(document_type="invoice"))


def test_export_a_valid_result(txt, quiet, monkeypatch, json_format):
    result = _processed(txt, quiet, monkeypatch, _invoice_payload())
    exported = export_document(result, json_format)
    assert exported.format == json_format and exported.media_type == "application/json"
    assert "INV-7" in exported.content


def test_export_refuses_an_invalid_result(txt, quiet, monkeypatch, json_format):
    result = _processed(txt, quiet, monkeypatch, _invoice_payload(total_amount=999.0))
    assert not result.is_valid
    with pytest.raises(ExportError, match="not exporting"):
        export_document(result, json_format)
    forced = export_document(result, json_format, ExportOptions(require_valid=False))
    assert "999" in forced.content


def test_export_a_model_built_by_hand(json_format):
    invoice = Invoice(
        invoice_number="X-1", issue_date=date(2026, 1, 1), seller={"name": "A"}, buyer={"name": "B"},
        subtotal=1, total_amount=1,
    )
    exported = export_document(invoice, json_format)
    assert exported.media_type == "application/json"
    assert json.loads(exported.content)


def test_export_of_a_failed_result_explains_why(tmp_path, quiet, json_format):
    path = tmp_path / "doc.docx"
    path.write_bytes(b"PK")
    failed = pipeline.process_document(path, quiet())
    with pytest.raises(ExportError, match="acquire failed"):
        export_document(failed, json_format)


def test_process_document_does_not_persist_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "invoice.txt"
    source.write_text(INVOICE_TEXT)
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    _no_classifier(monkeypatch)
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "OCR_FALLBACKS", [])
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload(total_amount=999.0))

    result = pipeline.process_document(source, ProcessOptions(document_type="invoice"))

    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


# ---- CLI and HTTP use the same contract -----------------------------------------------


def test_cli_document_type_and_schema(txt, monkeypatch, capsys):
    from docket import cli

    _no_classifier(monkeypatch)
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload())
    cli.main(["process", str(txt()), "--document-type", "invoice", "--no-ocr-fallback", "--no-include-layout"])
    out = json.loads(capsys.readouterr().out)
    assert out["schema_id"] == "invoice" and "layout" not in out
    monkeypatch.setattr(config, "INCLUDE_LAYOUT", False)
    cli.main(["process", str(txt()), "--document-type", "invoice", "--no-ocr-fallback"])
    assert "layout" not in json.loads(capsys.readouterr().out)
    cli.main(["process", str(txt()), "--document-type", "invoice", "--no-ocr-fallback", "--include-layout"])
    assert json.loads(capsys.readouterr().out)["layout"]["pages"]

    cli.main(["process", str(txt()), "--schema", "docket.catalog:Invoice", "--no-ocr-fallback"])
    assert json.loads(capsys.readouterr().out)["schema_id"] == "invoice"

    with pytest.raises(SystemExit) as exc:
        cli.main(["process", str(txt()), "--schema", "nowhere:Model"])
    assert exc.value.code == 3


def test_http_process_runs_the_real_pipeline(txt, tmp_path, monkeypatch):
    """Regression: the endpoint function once shadowed `process_document`, so
    the job runner called the endpoint instead of the pipeline."""
    from fastapi.testclient import TestClient

    from docket import api, job_store

    monkeypatch.setattr(api.config, "API_KEY", None)
    monkeypatch.setattr(job_store.config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "REVIEW_QUEUE_ENABLED", False)
    monkeypatch.setattr(config, "OCR_FALLBACKS", [])
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: _invoice_payload())
    with TestClient(api.app) as client:
        response = client.post("/process", files={"file": ("inv.txt", INVOICE_TEXT.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_type"] == "invoice" and body["status"] == "succeeded"
