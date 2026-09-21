"""Custom document types registered by an integrating application flow
through classification, extraction, validation, export and the API."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import docket.doctypes as doctypes
import docket.export as export_module
from docket import (
    CitedDocument,
    DocumentTypeError,
    Invoice,
    ValidationIssue,
    add_validator,
    export_document,
    process,
    register_document_type,
    register_exporter,
)
from docket import api, classify as classify_module, extract as extract_module, validate
from docket.schemas import ClassificationResult, DocType, PipelineResult, SourceLocation
from tests.test_export import sample_invoice

NOTE_TEXT = """LIEFERSCHEIN / DELIVERY NOTE
Note number: LS-2026-0117
Supplier: Holzwerk Bayern GmbH
Delivery date: 2026-09-14
Goods: 40 pallets of oak planks
"""


class DeliveryNote(CitedDocument):
    note_number: str
    supplier_name: str
    delivery_date: date
    goods: str | None = None


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch, tmp_path):
    monkeypatch.setattr(doctypes, "_REGISTRY", dict(doctypes._REGISTRY))
    monkeypatch.setattr(export_module, "_REGISTRY", dict(export_module._REGISTRY))
    monkeypatch.setattr(api.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(api.config, "JOB_STORE_PATH", tmp_path / "jobs.json")


def _register(**kwargs):
    return register_document_type(
        "delivery_note",
        DeliveryNote,
        description="Delivery note / Lieferschein listing goods handed over",
        keywords=["delivery note", "lieferschein"],
        **kwargs,
    )


def _payload(**overrides):
    payload = {
        "note_number": "LS-2026-0117",
        "supplier_name": "Holzwerk Bayern GmbH",
        "delivery_date": "2026-09-14",
        "goods": "40 pallets of oak planks",
        "field_locations": {
            "note_number": {"page": 1, "quote": "Note number: LS-2026-0117"},
            "supplier_name": {"page": 1, "quote": "Supplier: Holzwerk Bayern GmbH"},
            "delivery_date": {"page": 1, "quote": "Delivery date: 2026-09-14"},
        },
    }
    payload.update(overrides)
    return payload


def _run(tmp_path, monkeypatch, payload):
    source = tmp_path / "note.txt"
    source.write_text(NOTE_TEXT)
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    return process(source, enqueue_review=False)


def test_rules_tier_recognises_custom_keywords():
    _register()
    result = classify_module.classify_rules(NOTE_TEXT)
    assert result is not None
    assert result.doc_type == "delivery_note"
    assert result.type_name == "delivery_note"
    assert "delivery_note" in result.scores


def test_end_to_end_custom_document(tmp_path, monkeypatch):
    _register()
    result = _run(tmp_path, monkeypatch, _payload())

    assert result.classification.type_name == "delivery_note"
    assert result.is_valid, result.validation_issues
    note = result.document
    assert isinstance(note, DeliveryNote)
    assert note.supplier_name == "Holzwerk Bayern GmbH"
    assert "note_number" in result.field_sources
    assert "field_locations" not in result.extracted


def test_fabricated_citation_is_caught(tmp_path, monkeypatch):
    _register()
    payload = _payload()
    payload["field_locations"]["supplier_name"] = {"page": 1, "quote": "Supplier: Acme Ltd"}
    result = _run(tmp_path, monkeypatch, payload)

    assert not result.is_valid
    assert any(i.field == "supplier_name" for i in result.validation_issues)


def test_missing_citation_on_required_field_is_flagged(tmp_path, monkeypatch):
    _register()
    payload = _payload()
    del payload["field_locations"]["delivery_date"]
    result = _run(tmp_path, monkeypatch, payload)
    assert any(
        i.field == "delivery_date" and "citation" in i.message
        for i in result.validation_issues
    )


def test_custom_validator_runs(tmp_path, monkeypatch):
    def future_delivery(note, _raw_text):
        if note.delivery_date > date(2026, 9, 1):
            yield ValidationIssue(field="delivery_date", message="too late", severity="warning")

    _register(validators=[future_delivery])
    result = _run(tmp_path, monkeypatch, _payload())
    assert [i.message for i in result.validation_issues] == ["too late"]
    assert result.is_valid  # warnings don't fail validation


def test_validator_can_extend_a_builtin_type():
    def require_po(invoice, _raw_text):
        if not invoice.purchase_order_number:
            return [ValidationIssue(field="purchase_order_number", message="PO required")]
        return []

    add_validator("invoice", require_po)
    invoice = sample_invoice().model_copy(update={"purchase_order_number": None})
    issues = validate.validate(invoice)
    assert any(i.message == "PO required" for i in issues)


def test_llm_tier_is_told_about_custom_types(monkeypatch):
    _register()
    prompts = []

    def fake_chat(prompt, **_kwargs):
        prompts.append(prompt)
        return {"doc_type": "delivery_note", "confidence": 0.9}

    monkeypatch.setattr(classify_module, "chat_json", fake_chat)
    result = classify_module.classify_llm("ambiguous text")
    assert result.doc_type == "delivery_note"
    assert "delivery_note: Delivery note / Lieferschein" in prompts[0]


def test_tfidf_is_skipped_once_custom_types_exist(monkeypatch):
    _register()
    monkeypatch.setattr(
        classify_module, "classify_tfidf", lambda _t: pytest.fail("TF-IDF must not run")
    )
    monkeypatch.setattr(
        classify_module, "chat_json", lambda *a, **k: {"doc_type": "invoice", "confidence": 0.8}
    )
    assert classify_module.classify("nothing recognisable here").doc_type == DocType.INVOICE


def test_unregistered_llm_answer_becomes_unknown(monkeypatch):
    monkeypatch.setattr(
        classify_module, "chat_json", lambda *a, **k: {"doc_type": "spaceship", "confidence": 1}
    )
    assert classify_module.classify_llm("x").doc_type == DocType.UNKNOWN


def test_custom_type_survives_json_round_trip():
    result = PipelineResult(
        source="x.txt",
        classification=ClassificationResult(
            doc_type="delivery_note", confidence=1.0, method="rules"
        ),
        extracted=None,
        extract_attempts=0,
        ocr_method="text",
        raw_text_chars=1,
    )
    restored = PipelineResult.model_validate_json(result.model_dump_json())
    assert restored.classification.doc_type == "delivery_note"
    assert restored.classification.type_name == "delivery_note"


def test_exporter_for_custom_type():
    _register()
    register_exporter("note-csv", lambda n: f"{n.note_number};{n.supplier_name}",
                      accepts=(DeliveryNote,))
    note = DeliveryNote(note_number="LS-1", supplier_name="X", delivery_date=date(2026, 1, 1))
    assert export_document(note, "note-csv") == "LS-1;X"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"name": "Delivery Note"}, "snake_case"),
        ({"name": "unknown"}, "reserved"),
        ({"name": "invoice"}, "already registered"),
        ({"schema": Invoice}, "already registered as 'invoice'"),
        ({"description": " "}, "description is required"),
        ({"cited_fields": ["nope"]}, "cited_fields not in"),
    ],
)
def test_registration_errors(kwargs, message):
    args = {
        "name": "delivery_note",
        "schema": DeliveryNote,
        "description": "Delivery note",
        **kwargs,
    }
    with pytest.raises(DocumentTypeError, match=message):
        register_document_type(args.pop("name"), args.pop("schema"), **args)


def test_duplicate_custom_type_needs_replace():
    _register()
    with pytest.raises(DocumentTypeError, match="already registered"):
        _register()
    _register(replace=True)


def test_api_lists_document_types(monkeypatch):
    _register()
    monkeypatch.setattr(api.config, "API_KEY", None)
    with TestClient(api.app) as client:
        types = {t["name"]: t for t in client.get("/document-types").json()}
        formats = {f["name"] for f in client.get("/export-formats").json()}
    assert types["delivery_note"]["builtin"] is False
    assert "note_number" in types["delivery_note"]["schema"]["properties"]
    assert types["invoice"]["builtin"] is True
    assert "xrechnung" in formats


def test_source_location_is_public():
    assert SourceLocation(page=1, quote="x").page == 1
