from datetime import date

from docket.matching import (
    match_invoice_to_po,
    match_invoices_to_contract,
    match_receipt_to_transactions,
    match_three_way,
)
from docket.schemas import (
    BankTransaction,
    Contract,
    DiscrepancyType,
    Invoice,
    LineItem,
    MatchingStatus,
    PurchaseOrder,
    Receipt,
    Waybill,
    WaybillItem,
)


def _make_po(
    po_number="PO-2026-001",
    vendor_name="Acme Supplies LLC",
    customer_name="Global Tech Corp",
    total_amount=500.0,
    currency="USD",
    items=None,
):
    if items is None:
        items = [
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=10,
                unit_price=20.0,
                total=200.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
        ]
    return PurchaseOrder(
        po_number=po_number,
        po_date=date(2026, 1, 10),
        vendor_name=vendor_name,
        customer_name=customer_name,
        currency=currency,
        line_items=items,
        subtotal=total_amount,
        total_amount=total_amount,
    )


def _make_invoice(
    invoice_number="INV-2026-901",
    purchase_order_number="PO-2026-001",
    vendor_name="Acme Supplies Inc",
    customer_name="Global Tech Corp",
    total_amount=500.0,
    currency="USD",
    items=None,
    issue_date=date(2026, 1, 15),
):
    if items is None:
        items = [
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=10,
                unit_price=20.0,
                total=200.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
        ]
    return Invoice(
        invoice_number=invoice_number,
        purchase_order_number=purchase_order_number,
        issue_date=issue_date,
        vendor_name=vendor_name,
        customer_name=customer_name,
        currency=currency,
        line_items=items,
        subtotal=total_amount,
        total_amount=total_amount,
    )


# ---------------------------------------------------------------------------
# Invoice to PO Matching Tests
# ---------------------------------------------------------------------------


def test_invoice_po_perfect_match():
    po = _make_po()
    inv = _make_invoice()

    result = match_invoice_to_po(inv, po)
    assert result.status == MatchingStatus.MATCHED
    assert result.is_matched is True
    assert len(result.discrepancies) == 0
    assert result.matched_amount == 500.0
    assert result.variance_amount == 0.0


def test_invoice_po_price_variance_flagged():
    po = _make_po()
    # Invoiced unit price 25.0 instead of 20.0
    inv = _make_invoice(
        items=[
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=10,
                unit_price=25.0,
                total=250.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
        ],
        total_amount=550.0,
    )

    result = match_invoice_to_po(inv, po)
    assert result.status == MatchingStatus.DISCREPANCY
    assert result.is_matched is False
    assert any(d.type == DiscrepancyType.PRICE_VARIANCE for d in result.discrepancies)
    assert any(d.type == DiscrepancyType.TOTAL_MISMATCH for d in result.discrepancies)


def test_invoice_po_price_tolerance_passes():
    po = _make_po()
    # Invoiced unit price 20.20 (1% increase)
    inv = _make_invoice(
        items=[
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=10,
                unit_price=20.20,
                total=202.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
        ],
        total_amount=502.0,
    )

    result = match_invoice_to_po(inv, po, price_tolerance_pct=2.0)
    # Price variance is within 2%, only total exceeds
    price_discrepancies = [
        d for d in result.discrepancies if d.type == DiscrepancyType.PRICE_VARIANCE
    ]
    assert len(price_discrepancies) == 0


def test_invoice_po_quantity_overbilling_flagged():
    po = _make_po()
    # Invoiced 15 units instead of 10
    inv = _make_invoice(
        items=[
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=15,
                unit_price=20.0,
                total=300.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
        ],
        total_amount=600.0,
    )

    result = match_invoice_to_po(inv, po)
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(
        d.type == DiscrepancyType.QUANTITY_OVERBILLING for d in result.discrepancies
    )


def test_invoice_po_unordered_item_flagged():
    po = _make_po()
    # Invoiced unapproved third item
    inv = _make_invoice(
        items=[
            LineItem(
                sku="SKU-1",
                description="Industrial Paper",
                quantity=10,
                unit_price=20.0,
                total=200.0,
            ),
            LineItem(
                sku="SKU-2",
                description="Printer Toner",
                quantity=5,
                unit_price=60.0,
                total=300.0,
            ),
            LineItem(
                sku="SKU-99",
                description="Coffee Machine",
                quantity=1,
                unit_price=150.0,
                total=150.0,
            ),
        ],
        total_amount=650.0,
    )

    result = match_invoice_to_po(inv, po)
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(d.type == DiscrepancyType.UNORDERED_ITEM for d in result.discrepancies)


def test_invoice_po_mismatched_reference_and_currency():
    po = _make_po(po_number="PO-100", currency="USD")
    inv = _make_invoice(purchase_order_number="PO-999", currency="EUR")

    result = match_invoice_to_po(inv, po)
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(
        d.type == DiscrepancyType.PO_NUMBER_MISMATCH for d in result.discrepancies
    )
    assert any(
        d.type == DiscrepancyType.CURRENCY_MISMATCH for d in result.discrepancies
    )


# ---------------------------------------------------------------------------
# Invoices to Contract Matching Tests
# ---------------------------------------------------------------------------


def test_contract_invoices_within_budget_and_terms():
    contract = Contract(
        contract_title="MASTER SERVICES AGREEMENT",
        parties_a=["Acme Consulting Group LLC"],
        parties_b=["MegaCorp International Inc"],
        effective_date=date(2026, 1, 1),
        expiration_date=date(2026, 12, 31),
        contract_value=100000.0,
        currency="USD",
    )

    inv1 = _make_invoice(
        invoice_number="INV-01",
        vendor_name="Acme Consulting Group Inc",
        customer_name="MegaCorp International Corp",
        total_amount=30000.0,
        issue_date=date(2026, 2, 1),
    )
    inv2 = _make_invoice(
        invoice_number="INV-02",
        vendor_name="Acme Consulting Group Inc",
        customer_name="MegaCorp International Corp",
        total_amount=40000.0,
        issue_date=date(2026, 5, 1),
    )

    result = match_invoices_to_contract(contract, [inv1, inv2])
    assert result.status == MatchingStatus.MATCHED
    assert result.is_matched is True
    assert result.matched_amount == 70000.0
    assert result.variance_amount == 0.0


def test_contract_invoices_budget_exceeded():
    contract = Contract(
        contract_title="SOFTWARE DEVELOPMENT AGREEMENT",
        parties_a=["Acme Consulting Group LLC"],
        parties_b=["MegaCorp International Inc"],
        effective_date=date(2026, 1, 1),
        expiration_date=date(2026, 12, 31),
        contract_value=50000.0,
        currency="USD",
    )

    inv1 = _make_invoice(invoice_number="INV-01", total_amount=30000.0)
    inv2 = _make_invoice(invoice_number="INV-02", total_amount=35000.0)

    result = match_invoices_to_contract(contract, [inv1, inv2])
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(d.type == DiscrepancyType.BUDGET_EXCEEDED for d in result.discrepancies)
    assert result.variance_amount == 15000.0  # 65000 - 50000


def test_contract_invoices_date_out_of_bounds():
    contract = Contract(
        contract_title="SERVICES AGREEMENT",
        parties_a=["Acme Consulting Group LLC"],
        parties_b=["MegaCorp International Inc"],
        effective_date=date(2026, 1, 1),
        expiration_date=date(2026, 6, 30),
    )

    # Invoiced after contract expired
    inv = _make_invoice(issue_date=date(2026, 8, 15))

    result = match_invoices_to_contract(contract, [inv])
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(
        d.type == DiscrepancyType.DATE_OUT_OF_BOUNDS for d in result.discrepancies
    )


def test_contract_invoices_unknown_party():
    contract = Contract(
        contract_title="SERVICES AGREEMENT",
        parties_a=["Authorized Vendor LLC"],
        parties_b=["MegaCorp International Inc"],
        effective_date=date(2026, 1, 1),
    )

    inv = _make_invoice(vendor_name="Unknown Freelancer Ltd")

    result = match_invoices_to_contract(contract, [inv])
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(d.type == DiscrepancyType.PARTY_MISMATCH for d in result.discrepancies)


# ---------------------------------------------------------------------------
# Receipt to Bank Transactions Matching Tests
# ---------------------------------------------------------------------------


def test_receipt_to_transaction_exact_match():
    receipt = Receipt(
        merchant_name="Restaurant Le Paris",
        transaction_date=date(2026, 3, 15),
        total_amount=48.50,
        currency="EUR",
        card_last_four="4242",
    )

    transactions = [
        BankTransaction(
            transaction_date=date(2026, 3, 10),
            amount=-120.0,
            currency="EUR",
            description="Hotel Booking",
            card_last_four="4242",
            transaction_id="TX-001",
        ),
        BankTransaction(
            transaction_date=date(2026, 3, 16),  # 1 day settlement delay
            amount=-48.50,
            currency="EUR",
            description="Restaurant Le Paris",
            card_last_four="4242",
            transaction_id="TX-002",
        ),
    ]

    result = match_receipt_to_transactions(receipt, transactions, date_tolerance_days=2)
    assert result.status == MatchingStatus.MATCHED
    assert result.is_matched is True
    assert result.metadata["matched_transaction_id"] == "TX-002"


def test_receipt_to_transaction_unmatched():
    receipt = Receipt(
        merchant_name="Taxi Co",
        transaction_date=date(2026, 3, 15),
        total_amount=25.0,
        currency="USD",
        card_last_four="9999",
    )

    transactions = [
        BankTransaction(
            transaction_date=date(2026, 3, 15),
            amount=-100.0,
            currency="USD",
            card_last_four="9999",
        )
    ]

    result = match_receipt_to_transactions(receipt, transactions)
    assert result.status == MatchingStatus.UNMATCHED
    assert result.is_matched is False


# ---------------------------------------------------------------------------
# 3-Way Matching Tests (PO ↔ Waybill ↔ Invoice)
# ---------------------------------------------------------------------------


def test_match_three_way_perfect_match():
    po = _make_po(
        po_number="PO-500",
        vendor_name="Hardware Direct LLC",
        customer_name="Alpha Manufacturing Inc",
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=2.50,
                total=250.0,
            ),
        ],
        total_amount=250.0,
    )
    waybill = Waybill(
        waybill_number="WB-777",
        waybill_date=date(2026, 2, 5),
        shipper_name="Hardware Direct Inc",
        consignee_name="Alpha Manufacturing Corp",
        items=[
            WaybillItem(
                sku="BOLT-01",
                item_name="Steel Bolts M8",
                quantity=100.0,
                unit_price=2.50,
                total_price=250.0,
            ),
        ],
        total_quantity=100.0,
    )
    invoice = _make_invoice(
        invoice_number="INV-909",
        purchase_order_number="PO-500",
        vendor_name="Hardware Direct LLC",
        customer_name="Alpha Manufacturing Inc",
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=2.50,
                total=250.0,
            ),
        ],
        total_amount=250.0,
    )

    result = match_three_way(po, waybill, invoice)
    assert result.status == MatchingStatus.MATCHED
    assert result.is_matched is True
    assert len(result.discrepancies) == 0


def test_match_three_way_unfulfilled_billing_flagged():
    po = _make_po(
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=2.50,
                total=250.0,
            ),
        ],
        total_amount=250.0,
    )
    # Only 60 delivered on Waybill
    waybill = Waybill(
        waybill_number="WB-777",
        waybill_date=date(2026, 2, 5),
        shipper_name=po.vendor_name,
        consignee_name=po.customer_name,
        items=[
            WaybillItem(sku="BOLT-01", item_name="Steel Bolts M8", quantity=60.0),
        ],
    )
    # Invoiced full 100
    invoice = _make_invoice(
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=2.50,
                total=250.0,
            ),
        ],
        total_amount=250.0,
    )

    result = match_three_way(po, waybill, invoice)
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(
        d.type == DiscrepancyType.UNFULFILLED_BILLING for d in result.discrepancies
    )


def test_match_three_way_price_variance_flagged():
    po = _make_po(
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=2.50,
                total=250.0,
            ),
        ],
        total_amount=250.0,
    )
    waybill = Waybill(
        waybill_number="WB-777",
        waybill_date=date(2026, 2, 5),
        shipper_name=po.vendor_name,
        consignee_name=po.customer_name,
        items=[
            WaybillItem(sku="BOLT-01", item_name="Steel Bolts M8", quantity=100.0),
        ],
    )
    # Invoiced at higher price: 3.50 instead of 2.50
    invoice = _make_invoice(
        items=[
            LineItem(
                sku="BOLT-01",
                description="Steel Bolts M8",
                quantity=100,
                unit_price=3.50,
                total=350.0,
            ),
        ],
        total_amount=350.0,
    )

    result = match_three_way(po, waybill, invoice)
    assert result.status == MatchingStatus.DISCREPANCY
    assert any(d.type == DiscrepancyType.PRICE_VARIANCE for d in result.discrepancies)
