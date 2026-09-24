"""Tests for accounting and e-Invoicing export modules."""

import xml.etree.ElementTree as ET
from datetime import date

from docket.catalog import BankStatement, BankStatementTransaction, Invoice, LineItem
from docket.export import export_to_facturae_xml
from tests.factories import flat_invoice


def sample_invoice() -> Invoice:
    return flat_invoice(
        invoice_number="INV-2026-001",
        issue_date=date(2026, 9, 15),
        due_date=date(2026, 10, 15),
        vendor_name="Acme Solutions SL",
        vendor_vat_number="ESB12345674",
        vendor_iban="ES9121000418450200051332",
        vendor_bic="CAIXESBBXXX",
        vendor_address="Paseo de la Castellana 45, Madrid",
        customer_name="Global Logistics SA",
        customer_tax_id="ESA87654323",
        customer_address="Avenida Diagonal 120, Barcelona",
        purchase_order_number="PO-9988",
        payment_reference="REF-INV-001",
        currency="EUR",
        line_items=[
            LineItem(
                description="Consulting Services",
                quantity=10.0,
                unit_price=100.0,
                total=1000.0,
                unit_of_measure="HUR",
            ),
            LineItem(
                description="Software License",
                quantity=1.0,
                unit_price=500.0,
                total=500.0,
                unit_of_measure="C62",
            ),
        ],
        subtotal=1500.0,
        tax_rate_percent=21.0,
        tax_amount=315.0,
        discount_amount=50.0,
        shipping_amount=0.0,
        total_amount=1765.0,
    )


def sample_bank_statement() -> BankStatement:
    return BankStatement(
        bank_name="Banco Santander",
        account_holder="Iberia Tech Services SL",
        account_iban="ES9121000418450200051332",
        statement_period_start=date(2026, 9, 1),
        statement_period_end=date(2026, 9, 30),
        currency="EUR",
        opening_balance=10000.0,
        closing_balance=12500.0,
        total_deposits=5000.0,
        total_withdrawals=2500.0,
        transactions=[
            BankStatementTransaction(
                transaction_date=date(2026, 9, 5),
                description="Client Payment Project Alpha",
                amount=5000.0,
                balance_after=15000.0,
                counterparty_name="Client Corp",
                counterparty_iban="ES7921000813420123456789",
                reference="TX-001",
            ),
            BankStatementTransaction(
                transaction_date=date(2026, 9, 10),
                description="Office Rent Sept",
                amount=-2500.0,
                balance_after=12500.0,
                counterparty_name="Realty Madrid",
                reference="TX-002",
            ),
        ],
    )


# ==========================================
# Facturae (EN 16931 formats are covered by tests/test_einvoice.py)
# ==========================================


def test_facturae_xml_export():
    inv = sample_invoice()
    xml_str = export_to_facturae_xml(inv)

    root = ET.fromstring(xml_str)
    assert root.tag == "{http://www.facturae.es/Facturae/2014/v3.2.2/Facturae}Facturae"

    ns = {"fe": "http://www.facturae.es/Facturae/2014/v3.2.2/Facturae"}

    schema_ver = root.find("fe:FileHeader/fe:SchemaVersion", ns)
    assert schema_ver.text == "3.2.2"

    seller_nif = root.find(
        "fe:Parties/fe:SellerParty/fe:TaxIdentification/fe:TaxIdentificationNumber",
        ns,
    )
    assert seller_nif.text == "ESB12345674"

    inv_num = root.find("fe:Invoices/fe:Invoice/fe:InvoiceHeader/fe:InvoiceNumber", ns)
    assert inv_num.text == "INV-2026-001"

    total = root.find("fe:Invoices/fe:Invoice/fe:InvoiceTotals/fe:InvoiceTotal", ns)
    assert total.text == "1765.00"

    items = root.findall("fe:Invoices/fe:Invoice/fe:Items/fe:InvoiceLine", ns)
    assert len(items) == 2
    assert items[0].find("fe:ItemDescription", ns).text == "Consulting Services"
