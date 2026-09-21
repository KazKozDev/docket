"""Business rules of the schemas added with the catalog: each rule fires on
the fault it exists for and stays silent on the clean fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from docket.catalog import get_schema
from docket.catalog.rules import mrz_check_digit
from docket.validate import validate

FIXTURES = Path(__file__).parent / "fixtures" / "catalog"


def _issues(schema_id: str, **changes):
    spec = get_schema(schema_id)
    text = (FIXTURES / f"{schema_id}.txt").read_text()
    data = json.loads((FIXTURES / f"{schema_id}.expected.json").read_text())
    data.update(changes)
    return validate(spec.model.model_validate(data), pages=[text], spec=spec)


def _fields(issues, severity="error"):
    return {i.field for i in issues if i.severity == severity}


# ---- tax invoice / credit note --------------------------------------------------


def test_tax_invoice_needs_the_seller_registration():
    issues = _issues("tax_invoice", seller={"name": "Lion City Electronics Pte Ltd"})
    assert "seller.tax_ids" in _fields(issues)


def test_tax_invoice_shares_the_billing_arithmetic():
    # Beyond the validator's 1 % relative amount tolerance (validate._isclose).
    issues = _issues("tax_invoice", total_amount=1200.0)
    assert "total_amount" in _fields(issues)


def test_credit_note_without_an_original_invoice_is_a_warning():
    issues = _issues("credit_note", references=[])
    assert "references" in _fields(issues, "warning")
    assert "references" not in _fields(issues)


def test_credit_note_signs_are_notation():
    spec = get_schema("credit_note")
    data = json.loads((FIXTURES / "credit_note.expected.json").read_text())
    data.update(subtotal=-300.0, tax_amount=-57.0, total_amount=-357.0)
    note = spec.model.model_validate(data)
    assert (note.subtotal, note.tax_amount, note.total_amount) == (300.0, 57.0, 357.0)


# ---- utility bill ------------------------------------------------------------------


def test_utility_bill_amount_due_must_follow_from_the_balance():
    issues = _issues("utility_bill", amount_due=99.25)
    assert "amount_due" in _fields(issues)


def test_utility_bill_itemized_charges_must_add_up():
    issues = _issues("utility_bill", charges=[{"description": "Arbeitspreis", "amount": 70.0}])
    assert "current_charges" in _fields(issues)


def test_utility_bill_meter_difference_is_checked():
    reading = {"meter_id": "M1", "previous_reading": 100, "current_reading": 150, "consumption": 210, "unit": "kWh"}
    issues = _issues("utility_bill", meter_readings=[reading])
    assert "meter_readings[0].consumption" in _fields(issues, "warning")


def test_utility_bill_period_must_run_forwards():
    issues = _issues("utility_bill", billing_period_start="2026-08-01", billing_period_end="2026-07-01")
    assert "billing_period_end" in _fields(issues)


# ---- delivery note ----------------------------------------------------------------


def test_delivery_note_over_delivery_is_a_warning():
    items = [{"description": "Eichenbretter 30 mm", "quantity_ordered": 40, "quantity_delivered": 45}]
    issues = _issues("delivery_note", items=items)
    assert "items[0].quantity_delivered" in _fields(issues, "warning")


def test_delivery_note_supplier_and_recipient_must_differ():
    issues = _issues("delivery_note", recipient={"name": "Holzwerk Bayern GmbH"})
    assert "recipient.name" in _fields(issues)


# ---- certificate of origin ---------------------------------------------------------


def test_certificate_hs_codes_are_checked():
    goods = [{"description": "Spindle", "hs_code": "84A6"}]
    issues = _issues("certificate_of_origin", goods=goods)
    assert "goods[0].hs_code" in _fields(issues, "warning")


def test_certificate_needs_goods_and_an_authority():
    issues = _issues("certificate_of_origin", goods=[], issuing_authority=" ")
    assert {"goods", "issuing_authority"} <= _fields(issues)


def test_certificate_cannot_be_dated_in_the_future():
    issues = _issues("certificate_of_origin", issue_date="2099-01-01")
    assert "issue_date" in _fields(issues)


# ---- identity document ---------------------------------------------------------------


@pytest.mark.parametrize(
    "data, digit",
    [("L898902C3", 6), ("740812", 2), ("120415", 9), ("520727", 3), ("<<<<<<<<<", 0)],
)
def test_icao_check_digits(data, digit):
    # Values from the ICAO 9303 specimen passport and its worked examples.
    assert mrz_check_digit(data) == digit


def test_specimen_mrz_passes():
    issues = _issues("id_document")
    assert not _fields(issues)
    assert _fields(issues, "warning") == {"date_of_expiry"}  # the specimen expired in 2012


def test_misread_mrz_character_is_caught():
    mrz = ["P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<", "L898902C36UTO7408132F1204159ZE184226B<<<<<10"]
    issues = _issues("id_document", mrz=mrz)
    assert "mrz" in _fields(issues)


def test_printed_number_must_match_the_mrz():
    issues = _issues(
        "id_document",
        document_number="L898902C4",
        field_locations={"document_number": {"page": 1, "quote": "Passport No.: L898902C3"}},
    )
    assert "document_number" in _fields(issues)


def test_td1_card_layout_is_understood():
    # ICAO 9303 part 5 TD1 specimen.
    mrz = [
        "I<UTOD231458907<<<<<<<<<<<<<<<",
        "7408122F1204159UTO<<<<<<<<<<<6",
        "ERIKSSON<<ANNA<MARIA<<<<<<<<<<",
    ]
    issues = _issues("id_document", mrz=mrz, document_kind="national_id", document_number="D23145890")
    assert "mrz" not in _fields(issues)


def test_impossible_dates_are_errors():
    issues = _issues("id_document", date_of_issue="1970-01-01")
    assert "date_of_issue" in _fields(issues)


def test_unrecognised_mrz_shape_is_only_a_warning():
    issues = _issues("id_document", mrz=["P<UTO"])
    assert "mrz" in _fields(issues, "warning") and "mrz" not in _fields(issues)
