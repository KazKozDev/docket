from datetime import date

import pytest
from pydantic import ValidationError

from docket.schemas import Invoice, LineItem


def test_invoice_minimal_valid():
    inv = Invoice(
        invoice_number="INV-001",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        subtotal=100.0,
        tax_amount=10.0,
        total_amount=110.0,
    )
    assert inv.doc_type.value == "invoice"
    assert inv.currency == "USD"


def test_invoice_missing_required_field_raises():
    with pytest.raises(ValidationError):
        Invoice(  # type: ignore[call-arg]
            issue_date=date(2026, 1, 1),
            vendor_name="Acme Corp",
            customer_name="Wile E. Coyote",
            subtotal=100.0,
            total_amount=100.0,
        )


def test_invoice_bad_date_raises():
    with pytest.raises(ValidationError):
        Invoice(
            invoice_number="INV-001",
            issue_date="not-a-date",  # type: ignore[arg-type]
            vendor_name="Acme Corp",
            customer_name="Wile E. Coyote",
            subtotal=100.0,
            total_amount=100.0,
        )


def test_line_item_roundtrip():
    li = LineItem(description="Widget", quantity=2, unit_price=5.0, total=10.0)
    assert li.model_dump()["total"] == 10.0
