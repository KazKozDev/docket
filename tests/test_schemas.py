from datetime import date

import pytest
from pydantic import ValidationError

from docket.catalog import AcceptanceAct, AcceptanceActItem, BankStatement, BankStatementTransaction, Contract, Invoice, LineItem, PurchaseOrder, Receipt, ReceiptItem, Waybill, WaybillItem
from tests.factories import flat_invoice, flat_po


def test_invoice_minimal_valid():
    inv = flat_invoice(
        invoice_number="INV-001",
        issue_date=date(2026, 1, 1),
        vendor_name="Acme Corp",
        customer_name="Wile E. Coyote",
        subtotal=100.0,
        tax_amount=10.0,
        total_amount=110.0,
    )
    assert inv.currency == "USD"


def test_invoice_missing_required_field_raises():
    with pytest.raises(ValidationError):
        flat_invoice(  # type: ignore[call-arg]
            issue_date=date(2026, 1, 1),
            vendor_name="Acme Corp",
            customer_name="Wile E. Coyote",
            subtotal=100.0,
            total_amount=100.0,
        )


def test_invoice_bad_date_raises():
    with pytest.raises(ValidationError):
        flat_invoice(
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


def test_line_item_with_b2b_fields():
    li = LineItem(
        description="Consulting Hour",
        quantity=10,
        unit_price=150.0,
        total=1500.0,
        sku="SVC-CONS-01",
        unit_of_measure="hrs",
    )
    dumped = li.model_dump()
    assert dumped["sku"] == "SVC-CONS-01"
    assert dumped["unit_of_measure"] == "hrs"


def test_invoice_with_b2b_fields():
    inv = flat_invoice(
        invoice_number="INV-2026-99",
        issue_date=date(2026, 1, 15),
        vendor_name="Global Tech Ltd",
        vendor_address="100 Tech Blvd, Suite 200, San Francisco, CA",
        vendor_bic="DEUTDEDDFXX",
        customer_name="Enterprise Buyer Inc",
        customer_tax_id="US-987654321",
        customer_address="500 Market St, New York, NY",
        purchase_order_number="PO-778899",
        payment_reference="RF18539007547034",
        tax_rate_percent=20.0,
        subtotal=1000.0,
        tax_amount=200.0,
        total_amount=1200.0,
    )
    dumped = inv.model_dump()
    assert dumped["payment_account"]["bic"] == "DEUTDEDDFXX"
    assert dumped["buyer"]["tax_ids"] == [{"value": "US-987654321", "scheme": "tax_id", "country_code": None}]
    assert dumped["references"] == [{"kind": "purchase_order", "number": "PO-778899", "issue_date": None}]
    assert inv.purchase_order_number == "PO-778899"
    assert dumped["payment_reference"] == "RF18539007547034"
    assert dumped["tax_rate_percent"] == 20.0
    assert "San Francisco" in dumped["seller"]["address"]["text"]
    assert "New York" in dumped["buyer"]["address"]["text"]


def test_invoice_invalid_tax_rate_raises():
    with pytest.raises(ValidationError):
        flat_invoice(
            invoice_number="INV-001",
            issue_date=date(2026, 1, 1),
            vendor_name="Acme",
            customer_name="Client",
            subtotal=100.0,
            total_amount=100.0,
            tax_rate_percent=-5.0,
        )

    with pytest.raises(ValidationError):
        flat_invoice(
            invoice_number="INV-001",
            issue_date=date(2026, 1, 1),
            vendor_name="Acme",
            customer_name="Client",
            subtotal=100.0,
            total_amount=100.0,
            tax_rate_percent=150.0,
        )


def test_contract_minimal_defaults():
    c = Contract(
        contract_title="Consulting Agreement",
        parties_a=["Company A"],
        parties_b=["Company B"],
        effective_date=date(2026, 1, 1),
    )
    assert c.auto_renewal is False
    assert c.contract_value is None
    assert c.currency is None
    assert c.payment_terms is None
    assert c.notice_period_days is None
    assert c.liability_cap is None
    assert c.termination_for_convenience is False
    assert c.cure_period_days is None
    assert c.non_solicit is False
    assert c.signatories == []
    assert c.risk_factors == []


def test_contract_with_business_fields():
    c = Contract(
        contract_title="Master Services Agreement",
        parties_a=["Acme Corp"],
        parties_b=["Partner Inc."],
        effective_date=date(2026, 1, 1),
        contract_value=150000.0,
        currency="USD",
        payment_terms="Net 30 days",
        auto_renewal=True,
        notice_period_days=60,
        liability_cap="12 months fees",
        termination_for_convenience=True,
        cure_period_days=30,
        non_solicit=True,
        signatories=["Alice Smith, CEO", "Bob Jones, CTO"],
        risk_factors=["custom business risk"],
    )
    dumped = c.model_dump()
    assert dumped["contract_value"] == 150000.0
    assert dumped["currency"] == "USD"
    assert dumped["payment_terms"] == "Net 30 days"
    assert dumped["auto_renewal"] is True
    assert dumped["notice_period_days"] == 60
    assert dumped["liability_cap"] == "12 months fees"
    assert dumped["termination_for_convenience"] is True
    assert dumped["cure_period_days"] == 30
    assert dumped["non_solicit"] is True
    assert dumped["signatories"] == ["Alice Smith, CEO", "Bob Jones, CTO"]
    assert dumped["risk_factors"] == ["custom business risk"]


def test_contract_negative_values_raise():
    with pytest.raises(ValidationError):
        Contract(
            contract_title="Agreement",
            parties_a=["A"],
            parties_b=["B"],
            effective_date=date(2026, 1, 1),
            contract_value=-100.0,
        )

    with pytest.raises(ValidationError):
        Contract(
            contract_title="Agreement",
            parties_a=["A"],
            parties_b=["B"],
            effective_date=date(2026, 1, 1),
            notice_period_days=-5,
        )

    with pytest.raises(ValidationError):
        Contract(
            contract_title="Agreement",
            parties_a=["A"],
            parties_b=["B"],
            effective_date=date(2026, 1, 1),
            cure_period_days=-10,
        )


def test_contract_invalid_currency_length_raises():
    with pytest.raises(ValidationError):
        Contract(
            contract_title="Agreement",
            parties_a=["A"],
            parties_b=["B"],
            effective_date=date(2026, 1, 1),
            currency="US",
        )


def test_receipt_item_with_quantity_and_unit_price():
    item = ReceiptItem(
        description="Espresso",
        price=7.0,
        quantity=2.0,
        unit_price=3.5,
    )
    dumped = item.model_dump()
    assert dumped["quantity"] == 2.0
    assert dumped["unit_price"] == 3.5


def test_receipt_with_expense_fields():
    rec = Receipt(
        merchant_name="Bistro Paris",
        merchant_tax_id="ESB12345674",
        merchant_address="12 Rue de Rivoli, Paris",
        transaction_date=date(2026, 3, 15),
        currency="EUR",
        receipt_number="REC-98712",
        subtotal=40.0,
        tax_amount=5.0,
        tip_amount=5.0,
        discount_amount=2.0,
        total_amount=48.0,
        card_last_four="4242",
        expense_category="meals",
    )
    dumped = rec.model_dump()
    assert dumped["merchant_tax_id"] == "ESB12345674"
    assert dumped["merchant_address"] == "12 Rue de Rivoli, Paris"
    assert dumped["currency"] == "EUR"
    assert dumped["receipt_number"] == "REC-98712"
    assert dumped["tip_amount"] == 5.0
    assert dumped["discount_amount"] == 2.0
    assert dumped["card_last_four"] == "4242"
    assert dumped["expense_category"] == "meals"


def test_receipt_discount_negative_normalized():
    rec = Receipt(
        merchant_name="Shop",
        transaction_date=date(2026, 1, 1),
        subtotal=20.0,
        total_amount=18.0,
        discount_amount=-2.0,
    )
    assert rec.discount_amount == 2.0


def test_receipt_invalid_card_last_four_raises():
    with pytest.raises(ValidationError):
        Receipt(
            merchant_name="Shop",
            transaction_date=date(2026, 1, 1),
            total_amount=10.0,
            card_last_four="123",
        )

    with pytest.raises(ValidationError):
        Receipt(
            merchant_name="Shop",
            transaction_date=date(2026, 1, 1),
            total_amount=10.0,
            card_last_four="12345",
        )

    with pytest.raises(ValidationError):
        Receipt(
            merchant_name="Shop",
            transaction_date=date(2026, 1, 1),
            total_amount=10.0,
            card_last_four="abcd",
        )


def test_purchase_order_schema():
    po = flat_po(
        po_number="PO-7788",
        po_date=date(2026, 3, 1),
        vendor_name="Acme Industrial",
        customer_name="Global Tech Corp",
        subtotal=1000.0,
        tax_amount=200.0,
        total_amount=1200.0,
        line_items=[
            LineItem(
                sku="SKU-1",
                description="Part A",
                quantity=10,
                unit_price=100.0,
                total=1000.0,
            )
        ],
    )
    assert len(po.line_items) == 1
    assert po.total_amount == 1200.0


def test_bank_statement_schema():
    stmt = BankStatement(
        bank_name="Deutsche Bank",
        account_holder="Enterprise GmbH",
        account_iban="DE89370400440532013000",
        statement_period_start=date(2026, 1, 1),
        statement_period_end=date(2026, 1, 31),
        currency="EUR",
        opening_balance=10000.0,
        closing_balance=15000.0,
        total_deposits=7000.0,
        total_withdrawals=2000.0,
        transactions=[
            BankStatementTransaction(
                transaction_date=date(2026, 1, 15),
                description="Client payment",
                amount=7000.0,
                balance_after=17000.0,
            ),
            BankStatementTransaction(
                transaction_date=date(2026, 1, 20),
                description="Office supplies",
                amount=-2000.0,
                balance_after=15000.0,
            ),
        ],
    )
    assert len(stmt.transactions) == 2
    assert stmt.closing_balance == 15000.0


def test_acceptance_act_schema():
    act = AcceptanceAct(
        act_number="ACT-2026-01",
        act_date=date(2026, 2, 28),
        contract_reference="CTR-2026/01",
        customer_name="Client Corp",
        contractor_name="Service Provider LLC",
        items=[
            AcceptanceActItem(
                description="Software architecture audit",
                quantity=1.0,
                unit_price=5000.0,
                total=5000.0,
                unit_of_measure="service",
            )
        ],
        subtotal=5000.0,
        tax_amount=1000.0,
        total_amount=6000.0,
        claims_waived=True,
        signatories=["Alice Smith (Client)", "Bob Jones (Provider)"],
    )
    assert act.claims_waived is True
    assert len(act.signatories) == 2


def test_waybill_schema():
    wb = Waybill(
        waybill_number="WB-9988",
        waybill_date=date(2026, 3, 10),
        shipper_name="Factory Central LLC",
        consignee_name="Warehouse North Inc",
        carrier_name="DHL Freight",
        vehicle_number="B-1234-XY",
        items=[
            WaybillItem(
                item_name="Steel Pipes",
                sku="ST-PIPE-01",
                quantity=50.0,
                unit_of_measure="pcs",
                gross_weight_kg=1250.0,
                package_count=5,
            )
        ],
        total_quantity=50.0,
        total_gross_weight_kg=1250.0,
        total_packages=5,
    )
    assert wb.carrier_name == "DHL Freight"
    assert wb.total_gross_weight_kg == 1250.0
