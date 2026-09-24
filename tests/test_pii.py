from datetime import date

from pydantic import BaseModel, Field

from docket import Invoice, Receipt, pii_fields


def _paths(document):
    return {(item.path, item.category) for item in pii_fields(document)}


def test_invoice_pii_includes_nested_party_tax_and_bank_fields():
    invoice = Invoice(
        invoice_number="I-1",
        issue_date=date(2026, 1, 1),
        seller={"name": "Alice", "tax_ids": [{"value": "DE123", "scheme": "vat"}]},
        buyer={"name": "Bob", "address": {"city": "Paris"}},
        payment_account={"iban": "DE02120300000000202051"},
        subtotal=1,
        total_amount=1,
    )
    paths = _paths(invoice)
    assert ("seller.name", "person_name") in paths
    assert ("seller.tax_ids[0].value", "tax_id") in paths
    assert ("buyer.address.city", "address") in paths
    assert ("payment_account.iban", "bank_account") in paths


def test_receipt_and_travel_pii_categories():
    receipt = Receipt(
        merchant_name="Alice's Shop",
        merchant_tax_id="DE123",
        transaction_date=date(2026, 1, 1),
        total_amount=1,
        card_last_four="1234",
    )
    assert ("card_last_four", "bank_account") in _paths(receipt)


def test_custom_schema_pii_and_indexed_lists():
    class Passenger(BaseModel):
        names: list[str] = Field(json_schema_extra={"pii": "person_name"})

    assert _paths(Passenger(names=["Alice", "Bob"])) == {
        ("names[0]", "person_name"),
        ("names[1]", "person_name"),
    }
