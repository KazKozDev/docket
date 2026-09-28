"""verify(): docket's checks on an extraction made somewhere else."""

import pytest

from docket import DocumentStatus, ExportError, export_document, verify
from docket.catalog import SchemaError
from docket.verification import NO_SOURCE_TEXT
from tests.test_export import sample_invoice

PAGE = """Acme Solutions SL  NIF ESB12345674
Paseo de la Castellana 45, Madrid
Cliente: Global Logistics SA  NIF ESA87654323
Avenida Diagonal 120, Barcelona
Factura INV-2026-001
Fecha: 15/09/2026  Vencimiento: 15/10/2026
Pedido: PO-9988
Consulting Services 10 x 100,00 = 1.000,00
Software License 1 x 500,00 = 500,00
Base imponible: 1.500,00
IVA 21%: 315,00
Total: 1.815,00 EUR
IBAN ES9121000418450200051332  BIC CAIXESBBXXX"""

CITATIONS = {
    "invoice_number": {"page": 1, "quote": "Factura INV-2026-001"},
    "issue_date": {"page": 1, "quote": "Fecha: 15/09/2026  Vencimiento: 15/10/2026"},
    "seller.name": {"page": 1, "quote": "Acme Solutions SL  NIF ESB12345674"},
    "buyer.name": {"page": 1, "quote": "Cliente: Global Logistics SA  NIF ESA87654323"},
    "subtotal": {"page": 1, "quote": "Base imponible: 1.500,00"},
    "tax_amount": {"page": 1, "quote": "IVA 21%: 315,00"},
    "total_amount": {"page": 1, "quote": "Total: 1.815,00 EUR"},
    "line_items[0].total": {"page": 1, "quote": "Consulting Services 10 x 100,00 = 1.000,00"},
    "line_items[1].total": {"page": 1, "quote": "Software License 1 x 500,00 = 500,00"},
}


def external_json(**overrides) -> dict:
    data = sample_invoice().model_copy(update={"discount_amount": 0.0, "total_amount": 1815.0}).model_dump(mode="json")
    data["field_locations"] = CITATIONS
    data.update(overrides)
    return data


def test_grounded_external_json_succeeds():
    result = verify(external_json(), [PAGE], document_type="invoice")

    assert result.status == DocumentStatus.SUCCEEDED, result.review_reasons
    assert result.field_sources["total_amount"].quote == "Total: 1.815,00 EUR"
    assert "field_locations" not in result.extracted
    assert export_document(result, "ubl").content


def test_value_not_on_the_cited_line_needs_review():
    result = verify(external_json(invoice_number="INV-2026-007"), [PAGE], document_type="invoice")

    assert result.status == DocumentStatus.NEEDS_REVIEW
    with pytest.raises(ExportError):
        export_document(result, "ubl")


def test_quote_missing_from_the_page_needs_review():
    citations = {**CITATIONS, "total_amount": {"page": 1, "quote": "Total: 9.999,00 EUR"}}
    result = verify(external_json(field_locations=citations), [PAGE], document_type="invoice")

    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("total_amount" in r for r in result.review_reasons)


def test_without_source_text_nothing_is_confirmed():
    result = verify(sample_invoice())

    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert NO_SOURCE_TEXT in result.review_reasons


def test_dict_that_breaks_the_schema_fails():
    result = verify({"total_amount": "lots"}, "text", document_type="invoice")

    assert result.status == DocumentStatus.FAILED
    assert result.error.code == "invalid_document"


def test_dict_needs_a_document_type():
    with pytest.raises(SchemaError):
        verify({"invoice_number": "1"}, "text")


def test_export_refuses_a_schema_instance_whose_arithmetic_fails():
    broken = sample_invoice().model_copy(update={"total_amount": 1999.0})

    with pytest.raises(ExportError, match="total"):
        export_document(broken, "ubl")


# ---- a value must be printed on the line it cites --------------------------------------------


def test_a_single_string_is_checked_like_one_page():
    """A plain string used to skip the document-number check (it keyed on a
    [PAGE n] marker only a page list gets)."""
    result = verify(external_json(invoice_number="INV-2026-007"), PAGE, document_type="invoice")

    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("invoice_number" in r for r in result.review_reasons)


def test_a_party_name_not_on_its_cited_line_needs_review():
    data = external_json()
    data["seller"]["name"] = "Harbor View Trading Ltd"
    result = verify(data, [PAGE], document_type="invoice")

    assert result.status == DocumentStatus.NEEDS_REVIEW
    assert any("seller.name" in r for r in result.review_reasons)


def test_a_name_ocr_misspelled_on_the_page_still_passes():
    page = PAGE.replace("Acme Solutions SL", "Acrne Solutions SL")
    citations = {**CITATIONS, "seller.name": {"page": 1, "quote": "Acrne Solutions SL  NIF ESB12345674"}}
    result = verify(external_json(field_locations=citations), [page], document_type="invoice")

    assert result.status == DocumentStatus.SUCCEEDED, result.review_reasons


def test_a_date_spelled_with_a_month_name_must_match():
    page = PAGE.replace("Fecha: 15/09/2026", "Fecha: 9 de septiembre de 2026")
    citations = {**CITATIONS, "issue_date": {"page": 1, "quote": "Fecha: 9 de septiembre de 2026  Vencimiento: 15/10/2026"}}
    right = verify(external_json(issue_date="2026-09-09", field_locations=citations), [page], document_type="invoice")
    swapped = verify(external_json(issue_date="2026-09-09".replace("09-09", "09-10"), field_locations=citations),
                     [page], document_type="invoice")

    assert right.status == DocumentStatus.SUCCEEDED, right.review_reasons
    assert any("issue_date" in r for r in swapped.review_reasons)


def test_an_ambiguous_date_is_read_the_way_the_page_writes_dates():
    """15/09 on the same page makes the document day-first, so 01/06 is
    the first of June, not the sixth of January."""
    page = PAGE.replace("Pedido: PO-9988", "Pedido: PO-9988  Entrega: 01/06/2026")
    citations = {**CITATIONS, "due_date": {"page": 1, "quote": "Pedido: PO-9988  Entrega: 01/06/2026"}}
    result = verify(external_json(due_date="2026-01-06", field_locations=citations), [page], document_type="invoice")

    assert any("due_date" in r for r in result.review_reasons)


def test_a_document_number_of_punctuation_only_needs_review():
    citations = {**CITATIONS, "invoice_number": {"page": 1, "quote": "Factura °: INV-2026-001"}}
    result = verify(external_json(invoice_number="°:", field_locations=citations), [PAGE], document_type="invoice")

    assert any("invoice_number" in r for r in result.review_reasons)
