"""Every built-in schema: metadata, JSON Schema, a fixture document with its
expected extraction (which must validate cleanly), and classification of the
fixture by the cheap tiers alone."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from docket import classify as classify_module
from docket.catalog import BUILTIN_SCHEMAS, get_schema, list_schemas, migrate
from docket.catalog.registry import SchemaInfo
from docket.llm_client import LLMError
from docket.validate import validate

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"
IDS = [s.schema_id for s in BUILTIN_SCHEMAS]
ORIGINAL = {"invoice", "receipt", "contract", "purchase_order", "bank_statement", "acceptance_act", "waybill",
            "boarding_pass"}
ADDED = {"credit_note", "delivery_note", "utility_bill", "tax_invoice", "certificate_of_origin", "id_document"}


def _fixture(schema_id: str) -> tuple[str, dict]:
    text = (FIXTURES / f"{schema_id}.txt").read_text()
    expected = json.loads((FIXTURES / f"{schema_id}.expected.json").read_text())
    return text, expected


def test_catalog_holds_the_original_and_added_schemas():
    assert set(IDS) == ORIGINAL | ADDED
    assert {s.schema_id for s in list_schemas() if s.builtin} == set(IDS)


@pytest.mark.parametrize("schema_id", IDS)
def test_metadata_is_complete(schema_id):
    spec = get_schema(schema_id)
    assert spec.builtin and spec.registered
    assert re.fullmatch(r"\d+\.\d+", spec.version)
    assert spec.display_name and spec.description
    assert spec.status == ("experimental" if schema_id in ADDED else "stable")
    assert len(spec.keywords) >= 3
    assert len(spec.examples) >= 12
    assert spec.validators
    assert spec.required_citations, "every built-in schema cites its key fields"
    info = spec.info()
    assert isinstance(info, SchemaInfo) and info.model.startswith("docket.catalog.models:")
    if schema_id in ORIGINAL:
        assert spec.migrations and spec.migrations[0].from_version == "1.0"


@pytest.mark.parametrize("schema_id", IDS)
def test_keywords_cover_several_languages(schema_id):
    # The document's own name must be recognisable beyond English: each
    # schema's keywords match its name in at least three languages.
    names = {
        "invoice": ["invoice", "Rechnung", "facture", "factura"],
        "tax_invoice": ["tax invoice", "Steuerrechnung", "facture fiscale"],
        "credit_note": ["credit note", "Gutschrift", "avoir", "nota di credito"],
        "receipt": ["receipt", "Kassenbon", "scontrino", "recibo"],
        "contract": ["agreement", "Vertrag", "contrat", "contrato"],
        "purchase_order": ["purchase order", "Bestellung", "bon de commande"],
        "bank_statement": ["bank statement", "Kontoauszug", "estratto conto"],
        "acceptance_act": ["acceptance act", "Abnahmeprotokoll", "verbale di collaudo"],
        "waybill": ["waybill", "Frachtbrief", "lettre de voiture"],
        "boarding_pass": ["boarding pass", "Bordkarte", "carte d'embarquement"],
        "utility_bill": ["electricity bill", "Stromrechnung", "bolletta", "factura de luz"],
        "delivery_note": ["delivery note", "Lieferschein", "bon de livraison", "pakbon"],
        "certificate_of_origin": ["certificate of origin", "Ursprungszeugnis", "certificat d'origine"],
        "id_document": ["passport", "Personalausweis", "carte d'identité", "pasaporte"],
    }[schema_id]
    spec = get_schema(schema_id)
    for name in names:
        assert any(k.pattern.search(name) for k in spec.keywords), f"{schema_id}: {name!r} not recognised"


@pytest.mark.parametrize("schema_id", IDS)
def test_json_schema_exposes_every_field(schema_id):
    spec = get_schema(schema_id)
    schema = spec.json_schema()
    assert schema["type"] == "object"
    assert set(schema["properties"]) == set(spec.model.model_fields)
    for path in spec.required_citations:
        assert path.split(".")[0].split("[")[0] in schema["properties"]


@pytest.mark.parametrize("schema_id", IDS)
def test_fixture_matches_expected_and_validates_cleanly(schema_id):
    spec = get_schema(schema_id)
    text, expected = _fixture(schema_id)
    document = spec.model.model_validate(expected)
    issues = validate(document, pages=[text], spec=spec)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, [f"{i.field}: {i.message}" for i in errors]
    # Round trip: what the model serializes is what the fixture says.
    again = spec.model.model_validate(json.loads(document.model_dump_json()))
    assert again == document


@pytest.mark.parametrize("schema_id", IDS)
def test_fixture_is_classified_without_the_llm(schema_id, monkeypatch):
    def unavailable(*_a, **_k):
        raise LLMError("offline")

    monkeypatch.setattr(classify_module, "chat_json", unavailable)
    text, _ = _fixture(schema_id)
    result = classify_module.classify(text)
    assert result.doc_type == schema_id, result


@pytest.mark.parametrize("schema_id", IDS)
def test_citations_in_the_fixture_are_checked(schema_id):
    spec = get_schema(schema_id)
    text, expected = _fixture(schema_id)
    first = spec.required_citations[0]
    expected["field_locations"][first] = {"page": 1, "quote": "This line is not on the page"}
    issues = validate(spec.model.model_validate(expected), pages=[text], spec=spec)
    assert any(i.field == first and i.severity == "error" for i in issues)


@pytest.mark.parametrize("schema_id", IDS)
def test_exporters_are_listed(schema_id):
    spec = get_schema(schema_id)
    expected = {
        "invoice": {"ubl", "xrechnung", "zugferd", "facturae"},
        "tax_invoice": {"ubl", "xrechnung"},
        "receipt": {"xero-csv", "quickbooks-json"},
        "bank_statement": {"1c-bank", "sap-csv"},
        "acceptance_act": {"1c-enterprise"},
    }.get(schema_id, set())
    assert expected <= set(spec.exporters)
    if schema_id == "credit_note":
        # A credit note is not an invoice: exporting it as one would bill the buyer.
        assert "ubl" not in spec.exporters


# ---- migrations -----------------------------------------------------------------


def test_invoice_1_0_json_migrates_to_2_0():
    old = {
        "doc_type": "invoice",
        "invoice_number": "INV-1",
        "issue_date": "2026-01-10",
        "vendor_name": "Acme GmbH",
        "vendor_vat_number": "DE136695976",
        "vendor_tax_id": "12/345/67890",
        "vendor_address": "Hauptstr. 1, 10115 Berlin",
        "vendor_iban": "DE89370400440532013000",
        "customer_name": "Beta SA",
        "customer_tax_id": "B12345678",
        "purchase_order_number": "PO-9",
        "subtotal": 100.0,
        "total_amount": 100.0,
        "field_locations": {
            "vendor_name": {"page": 1, "quote": "Acme GmbH"},
            "vendor_tax_id": {"page": 1, "quote": "St.-Nr. 12/345/67890"},
            "purchase_order_number": {"page": 1, "quote": "PO-9"},
        },
    }
    new = migrate("invoice", old, "1.0")
    invoice = get_schema("invoice").model.model_validate(new)
    assert invoice.seller.name == "Acme GmbH"
    assert [(t.scheme, t.value) for t in invoice.seller.tax_ids] == [("vat", "DE136695976"), ("tax_id", "12/345/67890")]
    assert invoice.seller.address.text == "Hauptstr. 1, 10115 Berlin"
    assert invoice.payment_account.iban == "DE89370400440532013000"
    assert invoice.buyer.tax_id() == "B12345678"
    assert invoice.purchase_order_number == "PO-9"
    assert set(invoice.field_locations) == {"seller.name", "seller.tax_ids[1]", "references[0].number"}
    assert "doc_type" not in new


def test_purchase_order_1_0_migrates_to_2_0():
    new = migrate("purchase_order", {"doc_type": "purchase_order", "po_number": "PO-1", "po_date": "2026-01-01",
                                     "vendor_name": "Supplier", "customer_name": "Buyer", "subtotal": 1,
                                     "total_amount": 1}, "1.0")
    po = get_schema("purchase_order").model.model_validate(new)
    assert (po.supplier.name, po.buyer.name) == ("Supplier", "Buyer")


@pytest.mark.parametrize("schema_id", sorted(ORIGINAL - {"invoice", "purchase_order"}))
def test_flat_schemas_1_0_drop_doc_type(schema_id):
    _, expected = _fixture(schema_id)
    old = {"doc_type": schema_id, **expected}
    assert migrate(schema_id, old, "1.0") == expected


def test_a_1_0_result_still_yields_a_typed_invoice():
    from tests.factories import make_result

    result = make_result(
        schema_id="invoice", schema_version="1.0",
        extracted={"invoice_number": "INV-1", "issue_date": "2026-01-10", "vendor_name": "A",
                   "customer_name": "B", "subtotal": 1, "total_amount": 1},
    )
    assert result.document.seller.name == "A"
