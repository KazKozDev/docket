"""Tests for accounting and e-Invoicing export modules."""

import csv
import io
import xml.etree.ElementTree as ET
from datetime import date

from docket.export import (
    export_to_1c_client_bank,
    export_to_1c_enterprise_xml,
    export_to_facturae_xml,
    export_to_quickbooks_iif,
    export_to_quickbooks_json,
    export_to_sap_idoc,
    export_to_sap_journal_csv,
    export_to_xero_csv,
    export_to_xero_json,
)
from docket.catalog import AcceptanceAct, AcceptanceActItem, BankStatement, BankStatementTransaction, Invoice, LineItem, Receipt, ReceiptItem
from tests.factories import flat_invoice, flat_po


def sample_invoice() -> Invoice:
    return flat_invoice(
        invoice_number="INV-2026-001",
        issue_date=date(2026, 9, 15),
        due_date=date(2026, 10, 15),
        vendor_name="Acme Solutions SL",
        vendor_vat_number="ESB12345678",
        vendor_iban="ES9121000418450200051332",
        vendor_bic="CAIXESBBXXX",
        vendor_address="Paseo de la Castellana 45, Madrid",
        customer_name="Global Logistics SA",
        customer_tax_id="ESA87654321",
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


def sample_receipt() -> Receipt:
    return Receipt(
        merchant_name="Starbucks Coffee",
        merchant_tax_id="US943029104",
        merchant_address="100 Market St, San Francisco, CA",
        transaction_date=date(2026, 9, 16),
        receipt_number="STR-8472",
        currency="USD",
        items=[
            ReceiptItem(
                description="Latte Grande", quantity=2.0, unit_price=4.50, price=9.00
            ),
            ReceiptItem(
                description="Croissant", quantity=1.0, unit_price=3.50, price=3.50
            ),
        ],
        subtotal=12.50,
        tax_amount=1.25,
        tip_amount=2.00,
        total_amount=15.75,
        card_last_four="4242",
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


def sample_acceptance_act() -> AcceptanceAct:
    return AcceptanceAct(
        act_number="ACT-2026-44",
        act_date=date(2026, 9, 17),
        contract_reference="CNT-2026-09",
        customer_name="Retail Holding Ltd",
        customer_tax_id="7701234567",
        contractor_name="IT Solutions LLC",
        contractor_tax_id="7709876543",
        items=[
            AcceptanceActItem(
                description="IT System Audit Stage 1",
                quantity=1.0,
                unit_price=80000.0,
                total=80000.0,
                unit_of_measure="усл",
            )
        ],
        subtotal=80000.0,
        tax_amount=16000.0,
        total_amount=96000.0,
        currency="RUB",
        claims_waived=True,
        signatories=["Ivanov I.I.", "Petrov P.P."],
    )


# ==========================================
# 1C:Enterprise Tests
# ==========================================


def test_1c_client_bank_export():
    statement = sample_bank_statement()
    txt = export_to_1c_client_bank(statement)

    assert "1CClientBankExchange" in txt
    assert "ВерсияФормата=1.03" in txt
    assert "РасчСчет=ES9121000418450200051332" in txt
    assert "НачальныйОстаток=10000.00" in txt
    assert "ВсегоПоступило=5000.00" in txt
    assert "ВсегоСписано=2500.00" in txt
    assert "КонечныйОстаток=12500.00" in txt
    assert "СекцияДокумент=Платежное поручение" in txt
    assert "Номер=TX-001" in txt
    assert "Сумма=5000.00" in txt
    assert "Плательщик=Client Corp" in txt
    assert "КонецФайла" in txt


def test_1c_enterprise_xml_export_invoice():
    inv = sample_invoice()
    xml_str = export_to_1c_enterprise_xml(inv)

    root = ET.fromstring(xml_str)
    ns = {"ed": "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.13"}
    doc_node = root.find("ed:DocumentReceipt", ns)
    assert doc_node is not None
    assert doc_node.find("ed:Number", ns).text == "INV-2026-001"
    assert (
        doc_node.find("ed:CounterpartyVendor/ed:Name", ns).text == "Acme Solutions SL"
    )
    assert doc_node.find("ed:DocumentTotals/ed:TotalAmount", ns).text == "1765.00"
    items = doc_node.findall("ed:LineItems/ed:Item", ns)
    assert len(items) == 2
    assert items[0].find("ed:ItemName", ns).text == "Consulting Services"


def test_1c_enterprise_xml_export_act():
    act = sample_acceptance_act()
    xml_str = export_to_1c_enterprise_xml(act)

    root = ET.fromstring(xml_str)
    ns = {"ed": "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.13"}
    doc_node = root.find("ed:DocumentReceipt", ns)
    assert doc_node is not None
    assert doc_node.find("ed:Number", ns).text == "ACT-2026-44"
    assert doc_node.find("ed:CounterpartyVendor/ed:Name", ns).text == "IT Solutions LLC"
    assert doc_node.find("ed:DocumentTotals/ed:TotalAmount", ns).text == "96000.00"


# ==========================================
# SAP Tests
# ==========================================


def test_sap_idoc_export():
    inv = sample_invoice()
    xml_str = export_to_sap_idoc(inv)

    root = ET.fromstring(xml_str)
    assert root.tag == "INVOIC02"
    idoc = root.find("IDOC")
    assert idoc is not None

    dc40 = idoc.find("EDI_DC40")
    assert dc40.find("IDOCTYP").text == "INVOIC02"

    edk01 = idoc.find("E1EDK01")
    assert edk01.find("BELNR").text == "INV-2026-001"
    assert edk01.find("CURCY").text == "EUR"

    vendors = [p for p in idoc.findall("E1EDKA1") if p.find("PARVW").text == "LF"]
    assert len(vendors) == 1
    assert vendors[0].find("NAME1").text == "Acme Solutions SL"
    assert vendors[0].find("BNKAC").text == "ES9121000418450200051332"

    items = idoc.findall("E1EDP01")
    assert len(items) == 2
    assert items[0].find("ARKTX").text == "Consulting Services"

    eds_total = [s for s in idoc.findall("E1EDS01") if s.find("SUMID").text == "002"]
    assert len(eds_total) == 1
    assert eds_total[0].find("SUMME").text == "1765.00"


def test_sap_journal_csv_export():
    inv = sample_invoice()
    csv_str = export_to_sap_journal_csv(inv)

    reader = list(csv.reader(io.StringIO(csv_str)))
    headers = reader[0]
    assert "PostingDate" in headers
    assert "PostingKey" in headers
    assert "Amount" in headers

    # 3 lines: Expense Debit (40), Tax Debit (40), Vendor Credit (31)
    rows = reader[1:]
    assert len(rows) == 3
    assert rows[0][7] == "40"  # Debit expense
    assert rows[0][8] == "600000"
    assert rows[0][9] == "1500.00"
    assert rows[1][7] == "40"  # Debit tax
    assert rows[1][9] == "315.00"
    assert rows[2][7] == "31"  # Credit vendor
    assert rows[2][9] == "-1765.00"

    # Test bank statement export to SAP CSV
    stmt = sample_bank_statement()
    stmt_csv = export_to_sap_journal_csv(stmt)
    stmt_reader = list(csv.reader(io.StringIO(stmt_csv)))
    assert len(stmt_reader) > 1


# ==========================================
# QuickBooks Tests
# ==========================================


def test_quickbooks_iif_export_invoice():
    inv = sample_invoice()
    iif_str = export_to_quickbooks_iif(inv)

    lines = iif_str.split("\r\n")
    assert lines[0].startswith("!TRNS")
    assert lines[1].startswith("!SPL")
    assert lines[2] == "!ENDTRNS"

    trns_line = [line for line in lines if line.startswith("TRNS")][0]
    assert "BILL" in trns_line
    assert "-1765.00" in trns_line
    assert "Acme Solutions SL" in trns_line


def test_quickbooks_iif_export_receipt():
    rcpt = sample_receipt()
    iif_str = export_to_quickbooks_iif(rcpt)

    assert "CHECK" in iif_str
    assert "-15.75" in iif_str
    assert "Starbucks Coffee" in iif_str


def test_quickbooks_json_export():
    inv = sample_invoice()
    payload = export_to_quickbooks_json(inv)

    assert payload["DocNumber"] == "INV-2026-001"
    assert payload["VendorRef"]["name"] == "Acme Solutions SL"
    assert payload["TotalAmt"] == 1765.0
    assert len(payload["Line"]) == 2
    assert payload["Line"][0]["DetailType"] == "ItemBasedExpenseLineDetail"

    rcpt = sample_receipt()
    rcpt_payload = export_to_quickbooks_json(rcpt)
    assert rcpt_payload["PaymentType"] == "CreditCard"
    assert rcpt_payload["TotalAmt"] == 15.75


# ==========================================
# Xero Tests
# ==========================================


def test_xero_csv_export():
    inv = sample_invoice()
    csv_str = export_to_xero_csv(inv)

    reader = list(csv.reader(io.StringIO(csv_str)))
    headers = reader[0]
    assert headers[0] == "*ContactName"
    assert headers[1] == "*InvoiceNumber"

    rows = reader[1:]
    assert len(rows) == 2
    assert rows[0][0] == "Acme Solutions SL"
    assert rows[0][1] == "INV-2026-001"
    assert rows[0][4] == "1765.00"


def test_xero_json_export():
    inv = sample_invoice()
    data = export_to_xero_json(inv)

    inv_obj = data["Invoices"][0]
    assert inv_obj["Type"] == "ACCPAY"
    assert inv_obj["InvoiceNumber"] == "INV-2026-001"
    assert inv_obj["Total"] == 1765.0
    assert len(inv_obj["LineItems"]) == 2


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
    assert seller_nif.text == "ESB12345678"

    inv_num = root.find("fe:Invoices/fe:Invoice/fe:InvoiceHeader/fe:InvoiceNumber", ns)
    assert inv_num.text == "INV-2026-001"

    total = root.find("fe:Invoices/fe:Invoice/fe:InvoiceTotals/fe:InvoiceTotal", ns)
    assert total.text == "1765.00"

    items = root.findall("fe:Invoices/fe:Invoice/fe:Items/fe:InvoiceLine", ns)
    assert len(items) == 2
    assert items[0].find("fe:ItemDescription", ns).text == "Consulting Services"
