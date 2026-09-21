"""Custom schemas registered by an integrating application flow through
classification, extraction, validation, export and the API — and every way
a registration can be wrong fails loudly at registration time."""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import docket.catalog.registry as registry
import docket.export as export_module
from docket import (
    CitedDocument,
    Invoice,
    ProcessOptions,
    ReviewOptions,
    SchemaError,
    SchemaSpec,
    ValidationIssue,
    add_validator,
    export_document,
    keywords,
    process_document,
    register_exporter,
    register_schema,
)
from docket import api, classify as classify_module, extract as extract_module, validate
from docket.catalog import get_schema, list_schemas, migrate, unregister_schema
from docket.catalog.registry import Migration
from docket.result import DocumentResult, SourceLocation
from docket.schemas import ClassificationResult
from tests.factories import make_result
from tests.test_export import sample_invoice

TICKET_TEXT = """PARKING TICKET / STRAFZETTEL
Ticket number: PT-2026-0117
Issued by: City of Munich
Date: 2026-09-14
Plate: M-AB 1234
Fine: 35.00 EUR
"""


class ParkingTicket(CitedDocument):
    """Municipal parking fine notice."""

    ticket_number: str
    issued_by: str
    issue_date: date
    plate: str | None = None
    fine: float


class Note(BaseModel):
    title: str


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_REGISTRY", {k: dict(v) for k, v in registry._REGISTRY.items()})
    monkeypatch.setattr(export_module, "_REGISTRY", dict(export_module._REGISTRY))
    monkeypatch.setattr(api.config, "REVIEW_QUEUE_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(api.config, "JOBS_DIR", tmp_path / "jobs")


def _spec(**overrides) -> SchemaSpec:
    base = dict(
        schema_id="parking_ticket",
        version="1.0",
        model=ParkingTicket,
        description="Parking ticket / Strafzettel for a parking offence",
        keywords=keywords("parking ticket", "strafzettel"),
        cited_fields=("ticket_number", "issued_by", "fine"),
    )
    base.update(overrides)
    return SchemaSpec(**base)


def _payload(**overrides):
    payload = {
        "ticket_number": "PT-2026-0117",
        "issued_by": "City of Munich",
        "issue_date": "2026-09-14",
        "plate": "M-AB 1234",
        "fine": 35.0,
        "field_locations": {
            "ticket_number": {"page": 1, "quote": "Ticket number: PT-2026-0117"},
            "issued_by": {"page": 1, "quote": "Issued by: City of Munich"},
            "fine": {"page": 1, "quote": "Fine: 35.00 EUR"},
        },
    }
    payload.update(overrides)
    return payload


def _run(tmp_path, monkeypatch, payload, **options):
    source = tmp_path / "ticket.txt"
    source.write_text(TICKET_TEXT)
    monkeypatch.setattr(extract_module, "chat_json", lambda *a, **k: payload)
    return process_document(source, ProcessOptions(review=ReviewOptions(enqueue=False), **options))


# ---- flow ----------------------------------------------------------------------


def test_rules_tier_recognises_custom_keywords():
    register_schema(_spec())
    result = classify_module.classify_rules(TICKET_TEXT)
    assert result is not None and result.doc_type == "parking_ticket"
    assert "parking_ticket" in result.scores


def test_end_to_end_custom_document(tmp_path, monkeypatch):
    register_schema(_spec())
    result = _run(tmp_path, monkeypatch, _payload())
    assert result.classification.doc_type == "parking_ticket"
    assert (result.schema_id, result.schema_version) == ("parking_ticket", "1.0")
    assert result.is_valid, result.validation_issues
    assert isinstance(result.document, ParkingTicket)
    assert result.document.fine == 35.0
    assert "ticket_number" in result.field_sources
    assert "field_locations" not in result.extracted


def test_fabricated_citation_is_caught(tmp_path, monkeypatch):
    register_schema(_spec())
    payload = _payload()
    payload["field_locations"]["issued_by"] = {"page": 1, "quote": "Issued by: Acme Ltd"}
    result = _run(tmp_path, monkeypatch, payload)
    assert not result.is_valid
    assert any(i.field == "issued_by" for i in result.validation_issues)


def test_missing_citation_on_cited_field_is_flagged(tmp_path, monkeypatch):
    register_schema(_spec())
    payload = _payload()
    del payload["field_locations"]["fine"]
    result = _run(tmp_path, monkeypatch, payload)
    assert any(i.field == "fine" and "citation" in i.message for i in result.validation_issues)


def test_schema_without_citations_is_extracted_as_is(tmp_path, monkeypatch):
    register_schema(SchemaSpec(schema_id="note", model=Note, description="Free-form note"))
    result = _run(tmp_path, monkeypatch, {"title": "Hello"}, document_type="note")
    assert result.extracted == {"title": "Hello"} and result.is_valid


def test_custom_validator_runs(tmp_path, monkeypatch):
    def recent(ticket, ctx):
        assert ctx.raw_text and "[PAGE 1]" in ctx.raw_text
        if ticket.issue_date > date(2026, 9, 1):
            yield ValidationIssue(field="issue_date", message="too recent", severity="warning")

    register_schema(_spec(validators=(recent,)))
    result = _run(tmp_path, monkeypatch, _payload())
    assert [i.message for i in result.validation_issues] == ["too recent"]
    assert result.is_valid


def test_validator_can_extend_a_builtin_schema():
    def require_po(invoice, ctx):
        if not invoice.purchase_order_number:
            return [ValidationIssue(field="references", message="PO required")]
        return []

    add_validator("invoice", require_po)
    issues = validate.validate(sample_invoice().model_copy(update={"references": []}))
    assert any(i.message == "PO required" for i in issues)


def test_llm_tier_is_told_about_custom_schemas(monkeypatch):
    register_schema(_spec())
    prompts = []

    def fake_chat(prompt, **_kwargs):
        prompts.append(prompt)
        return {"doc_type": "parking_ticket", "confidence": 0.9}

    monkeypatch.setattr(classify_module, "chat_json", fake_chat)
    assert classify_module.classify_llm("ambiguous text").doc_type == "parking_ticket"
    assert "parking_ticket: Parking ticket / Strafzettel" in prompts[0]


def test_tfidf_is_skipped_for_a_schema_without_examples():
    from docket.classify_tfidf import classify_tfidf

    register_schema(_spec())
    assert classify_tfidf("Pay the fine within 14 days.") is None


def test_tfidf_learns_a_schema_that_brings_examples():
    from docket.classify_tfidf import classify_tfidf

    register_schema(
        _spec(
            examples=(
                "Your vehicle was parked in a no-parking zone; the fine is payable within 14 days.",
                "Offence recorded by the traffic warden at the meter bay outside number 12.",
                "Ihr Fahrzeug parkte ohne gültigen Parkschein; Verwarnungsgeld zahlbar binnen einer Woche.",
                "Stationnement interdit constaté par l'agent, amende forfaitaire à régler sous 45 jours.",
            )
        )
    )
    result = classify_tfidf("The warden recorded the vehicle parked in a no-parking zone; pay within 14 days.")
    assert result is not None and result.doc_type == "parking_ticket"


def test_unregistered_llm_answer_becomes_unknown(monkeypatch):
    monkeypatch.setattr(classify_module, "chat_json", lambda *a, **k: {"doc_type": "spaceship", "confidence": 1})
    assert classify_module.classify_llm("x").doc_type == "unknown"


def test_custom_result_survives_json_round_trip():
    register_schema(_spec())
    result = make_result(
        source="x.txt",
        document_type="parking_ticket",
        schema_id="parking_ticket",
        schema_version="1.0",
        classification=ClassificationResult(doc_type="parking_ticket", confidence=1.0, method="rules"),
        extracted=ParkingTicket.model_validate(_payload()).model_dump(mode="json"),
    )
    restored = DocumentResult.model_validate_json(result.model_dump_json())
    assert restored.classification.doc_type == "parking_ticket"
    assert isinstance(restored.document, ParkingTicket)


def test_exporter_for_custom_schema():
    register_schema(_spec())
    register_exporter(
        "ticket-csv", lambda t: f"{t.ticket_number};{t.fine:.2f}", accepts=(ParkingTicket,), media_type="text/csv"
    )
    assert get_schema("parking_ticket").exporters == ["ticket-csv"]
    ticket = ParkingTicket.model_validate(_payload())
    assert export_document(ticket, "ticket-csv").content == "PT-2026-0117;35.00"


# ---- registration errors ---------------------------------------------------------


class BadCitations(BaseModel):
    field_locations: dict[str, str] = {}
    x: str


class Opaque:
    pass


class NotJsonable(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    thing: Opaque


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"schema_id": "Parking Ticket"}, "snake_case"),
        ({"schema_id": "unknown"}, "reserved"),
        ({"schema_id": "invoice", "version": "2.0", "model": Note, "cited_fields": None}, "already registered"),
        ({"model": Invoice, "cited_fields": None}, "already registered as 'invoice'"),
        ({"description": " "}, "description is required"),
        ({"cited_fields": ("nope",)}, "cited_fields not in"),
        ({"version": "one"}, "must look like '1.0'"),
        ({"model": dict}, "Pydantic BaseModel subclass"),
        ({"model": BadCitations, "cited_fields": None}, r"must be dict\[str, Citation\]"),
        ({"model": NotJsonable, "cited_fields": None}, "cannot be described as JSON Schema"),
        ({"model": Note, "cited_fields": ("title",)}, "no field_locations"),
    ],
)
def test_registration_errors(overrides, message):
    with pytest.raises(SchemaError, match=message):
        register_schema(_spec(**overrides))


def test_nested_cited_field_paths_are_checked():
    from docket.catalog import Party

    class Fine(CitedDocument):
        authority: Party
        amount: float

    class OtherFine(Fine):
        pass

    register_schema(SchemaSpec(schema_id="fine", model=Fine, description="Fine", cited_fields=("authority.name",)))
    with pytest.raises(SchemaError, match="authority.street"):
        register_schema(
            SchemaSpec(schema_id="fine2", model=OtherFine, description="Fine", cited_fields=("authority.street",))
        )


def test_duplicate_version_needs_replace():
    register_schema(_spec())
    with pytest.raises(SchemaError, match="already registered"):
        register_schema(_spec())
    register_schema(_spec(description="Updated"), replace_existing=True)
    assert get_schema("parking_ticket").description == "Updated"


def test_versions_coexist_and_migrate():
    class ParkingTicketV2(ParkingTicket):
        currency: str = "EUR"

    def upgrade(data):
        return {**data, "currency": "EUR"}

    register_schema(_spec())
    register_schema(
        _spec(model=ParkingTicketV2, version="2.0", migrations=(Migration("1.0", "2.0", "adds currency", upgrade),))
    )
    assert get_schema("parking_ticket").version == "2.0"
    assert get_schema("parking_ticket", "1.0").model is ParkingTicket
    assert [s.version for s in list_schemas(all_versions=True) if s.schema_id == "parking_ticket"] == ["1.0", "2.0"]
    assert migrate("parking_ticket", {"fine": 1}, "1.0") == {"fine": 1, "currency": "EUR"}
    with pytest.raises(SchemaError, match="no migration"):
        migrate("parking_ticket", {}, "0.9")

    # A result saved under 1.0 stays readable through its own version.
    old = make_result(
        document_type="parking_ticket", schema_id="parking_ticket", schema_version="1.0",
        extracted=ParkingTicket.model_validate(_payload()).model_dump(mode="json"),
    )
    assert isinstance(old.document, ParkingTicket)


def test_builtins_cannot_be_removed_and_customs_can():
    with pytest.raises(SchemaError, match="built in"):
        unregister_schema("invoice")
    register_schema(_spec())
    unregister_schema("parking_ticket")
    assert get_schema("parking_ticket") is None


def test_entry_point_plugin_registers_schemas(monkeypatch):
    class _EP:
        name = "tickets"

        def load(self):
            return lambda: [_spec()]

    monkeypatch.setattr(registry, "_plugins_loaded", False)
    monkeypatch.setattr(registry, "entry_points", lambda group: [_EP()] if group == "docket.schemas" else [])
    assert get_schema("parking_ticket").model is ParkingTicket


def test_entry_point_producing_garbage_is_an_error(monkeypatch):
    class _EP:
        name = "broken"

        def load(self):
            return lambda: ["not a spec"]

    monkeypatch.setattr(registry, "_plugins_loaded", False)
    monkeypatch.setattr(registry, "entry_points", lambda group: [_EP()])
    with pytest.raises(SchemaError, match="not a SchemaSpec"):
        list_schemas()


# ---- API --------------------------------------------------------------------------


def test_api_lists_schemas_and_json_schema(monkeypatch):
    register_schema(_spec())
    monkeypatch.setattr(api.config, "API_KEY", None)
    with TestClient(api.app) as client:
        schemas = {s["schema_id"]: s for s in client.get("/schemas").json()}
        one = client.get("/schemas/parking_ticket").json()
        json_schema = client.get("/schemas/parking_ticket/json-schema").json()
        missing = client.get("/schemas/nope")
        formats = {f["name"]: f for f in client.get("/export-formats").json()}
    assert schemas["parking_ticket"]["builtin"] is False
    assert schemas["invoice"]["version"] == "2.0"
    assert one["cited_fields"] == ["ticket_number", "issued_by", "fine"]
    assert "ticket_number" in json_schema["properties"]
    assert missing.status_code == 404
    assert "invoice" in formats["xrechnung-ubl"]["schemas"]


def test_source_location_is_public():
    location = SourceLocation(page=1, quote="x")
    assert location.page == 1 and location.bbox is None
