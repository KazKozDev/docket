"""ERP and Accounting export adapters for Docket documents.

Provides structured conversion to:
- 1C:Enterprise (1CClientBankExchange txt format and EnterpriseData XML)
- SAP (INVOIC02 IDoc XML and S/4HANA Journal Entry CSV)
- QuickBooks (Intuit Interchange Format .iif and QuickBooks Online API JSON)
- Xero (Xero Bills CSV and Accounting API JSON)
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from ..schemas import (
    AcceptanceAct,
    BankStatement,
    Invoice,
    Receipt,
)


def export_to_1c_client_bank(statement: BankStatement) -> str:
    """Export a BankStatement to 1C:ClientBank text exchange format (1CClientBankExchange 1.03).

    Args:
        statement: Extracted and validated BankStatement.

    Returns:
        Formatted 1CClientBank text content.
    """
    lines = [
        "1CClientBankExchange",
        "ВерсияФормата=1.03",
        "Кодировка=Windows",
        "Отправитель=Docket",
        f"ДатаСоздания={datetime.now().strftime('%Y-%m-%d')}",
        f"ВремяСоздания={datetime.now().strftime('%H:%M:%S')}",
        f"ДатаНачала={statement.statement_period_start.strftime('%d.%m.%Y')}",
        f"ДатаКонца={statement.statement_period_end.strftime('%d.%m.%Y')}",
        f"РасчСчет={statement.account_iban}",
        "СекцияРасчСчет",
        f"ДатаНачала={statement.statement_period_start.strftime('%d.%m.%Y')}",
        f"ДатаКонца={statement.statement_period_end.strftime('%d.%m.%Y')}",
        f"НачальныйОстаток={statement.opening_balance:.2f}",
        f"ВсегоПоступило={statement.total_deposits:.2f}",
        f"ВсегоСписано={statement.total_withdrawals:.2f}",
        f"КонечныйОстаток={statement.closing_balance:.2f}",
        "КонецРасчСчет",
    ]

    for idx, tx in enumerate(statement.transactions, start=1):
        doc_num = tx.reference or str(idx)
        tx_date_str = tx.transaction_date.strftime("%d.%m.%Y")
        lines.append("СекцияДокумент=Платежное поручение")
        lines.append(f"Номер={doc_num}")
        lines.append(f"Дата={tx_date_str}")
        lines.append(f"Сумма={abs(tx.amount):.2f}")

        if tx.amount > 0:
            # Deposit: Payer is counterparty, Recipient is account holder
            lines.append(f"Плательщик={tx.counterparty_name or 'Не указан'}")
            lines.append(f"ПлательщикСчет={tx.counterparty_iban or ''}")
            lines.append(f"Получатель={statement.account_holder}")
            lines.append(f"ПолучательСчет={statement.account_iban}")
        else:
            # Withdrawal: Payer is account holder, Recipient is counterparty
            lines.append(f"Плательщик={statement.account_holder}")
            lines.append(f"ПлательщикСчет={statement.account_iban}")
            lines.append(f"Получатель={tx.counterparty_name or 'Не указан'}")
            lines.append(f"ПолучательСчет={tx.counterparty_iban or ''}")

        lines.append(f"НазначениеПлатежа={tx.description}")
        lines.append("КонецДокумента")

    lines.append("КонецФайла")
    return "\r\n".join(lines)


def export_to_1c_enterprise_xml(doc: Invoice | AcceptanceAct) -> str:
    """Export an Invoice or AcceptanceAct to 1C:Enterprise (EnterpriseData / CommerceML) XML format.

    Args:
        doc: Validated Invoice or AcceptanceAct.

    Returns:
        Formatted UTF-8 XML string representing a Goods and Services Receipt document.
    """
    ns_uri = "http://v8.1c.ru/edi/edi_stnd/EnterpriseData/1.13"
    ET.register_namespace("", ns_uri)
    root = ET.Element(f"{{{ns_uri}}}EnterpriseData")

    doc_elem = ET.SubElement(root, f"{{{ns_uri}}}DocumentReceipt")

    if isinstance(doc, Invoice):
        doc_num = doc.invoice_number
        doc_date = doc.issue_date.isoformat()
        vendor_name = doc.vendor_name
        vendor_tax_id = doc.vendor_vat_number or doc.vendor_tax_id or ""
        customer_name = doc.customer_name
        customer_tax_id = doc.customer_tax_id or ""
        currency = doc.currency
        subtotal = doc.subtotal
        tax_amount = doc.tax_amount
        total = doc.total_amount
        items_data = [
            (
                item.description,
                item.quantity,
                item.unit_of_measure or "шт",
                item.unit_price,
                item.total,
            )
            for item in doc.line_items
        ]
    else:
        doc_num = doc.act_number
        doc_date = doc.act_date.isoformat()
        vendor_name = doc.contractor_name
        vendor_tax_id = doc.contractor_tax_id or ""
        customer_name = doc.customer_name
        customer_tax_id = doc.customer_tax_id or ""
        currency = doc.currency
        subtotal = doc.subtotal
        tax_amount = doc.tax_amount
        total = doc.total_amount
        items_data = [
            (
                item.description,
                item.quantity,
                item.unit_of_measure or "усл",
                item.unit_price,
                item.total,
            )
            for item in doc.items
        ]

    def add_sub(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        elem = ET.SubElement(parent, f"{{{ns_uri}}}{tag}")
        if text is not None:
            elem.text = text
        return elem

    add_sub(doc_elem, "Number", doc_num)
    add_sub(doc_elem, "Date", doc_date)
    add_sub(doc_elem, "Currency", currency)
    add_sub(doc_elem, "OperationType", "PurchaseReceipt")

    # Counterparties
    vendor_elem = add_sub(doc_elem, "CounterpartyVendor")
    add_sub(vendor_elem, "Name", vendor_name)
    if vendor_tax_id:
        add_sub(vendor_elem, "TaxId", vendor_tax_id)

    customer_elem = add_sub(doc_elem, "CounterpartyCustomer")
    add_sub(customer_elem, "Name", customer_name)
    if customer_tax_id:
        add_sub(customer_elem, "TaxId", customer_tax_id)

    # Totals
    totals_elem = add_sub(doc_elem, "DocumentTotals")
    add_sub(totals_elem, "Subtotal", f"{subtotal:.2f}")
    add_sub(totals_elem, "TaxAmount", f"{tax_amount:.2f}")
    add_sub(totals_elem, "TotalAmount", f"{total:.2f}")

    # Line Items Tabular Section
    items_elem = add_sub(doc_elem, "LineItems")
    for idx, (desc, qty, uom, price, line_total) in enumerate(items_data, start=1):
        line = add_sub(items_elem, "Item")
        add_sub(line, "LineNumber", str(idx))
        add_sub(line, "ItemName", desc)
        add_sub(line, "Quantity", f"{qty:.3f}")
        add_sub(line, "UnitOfMeasure", uom)
        add_sub(line, "UnitPrice", f"{price:.2f}")
        add_sub(line, "LineTotal", f"{line_total:.2f}")

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def export_to_sap_idoc(invoice: Invoice) -> str:
    """Export an Invoice to SAP INVOIC02 IDoc XML format.

    Args:
        invoice: Validated Invoice.

    Returns:
        Formatted SAP INVOIC02 XML string.
    """
    root = ET.Element("INVOIC02")
    idoc = ET.SubElement(root, "IDOC", {"BEGIN": "1"})

    # Control Record
    dc40 = ET.SubElement(idoc, "EDI_DC40", {"SEGMENT": "1"})
    ET.SubElement(dc40, "TABNAM").text = "EDI_DC40"
    ET.SubElement(dc40, "DIRECT").text = "2"
    ET.SubElement(dc40, "IDOCTYP").text = "INVOIC02"
    ET.SubElement(dc40, "MESTYP").text = "INVOIC"
    ET.SubElement(dc40, "SNDPRT").text = "LS"
    ET.SubElement(dc40, "RCVPRT").text = "LS"

    # Header Data
    edk01 = ET.SubElement(idoc, "E1EDK01", {"SEGMENT": "1"})
    ET.SubElement(edk01, "BELNR").text = invoice.invoice_number
    ET.SubElement(edk01, "CURCY").text = invoice.currency
    ET.SubElement(edk01, "BSART").text = "INVO"
    ET.SubElement(edk01, "REC_DATE").text = invoice.issue_date.strftime("%Y%m%d")

    # Vendor Partner Segment (LF)
    edka1_vendor = ET.SubElement(idoc, "E1EDKA1", {"SEGMENT": "1"})
    ET.SubElement(edka1_vendor, "PARVW").text = "LF"
    ET.SubElement(edka1_vendor, "NAME1").text = invoice.vendor_name
    if invoice.vendor_address:
        ET.SubElement(edka1_vendor, "STRAS").text = invoice.vendor_address
    if invoice.vendor_iban:
        ET.SubElement(edka1_vendor, "BNKAC").text = invoice.vendor_iban

    # Customer Partner Segment (AG)
    edka1_cust = ET.SubElement(idoc, "E1EDKA1", {"SEGMENT": "1"})
    ET.SubElement(edka1_cust, "PARVW").text = "AG"
    ET.SubElement(edka1_cust, "NAME1").text = invoice.customer_name
    if invoice.customer_address:
        ET.SubElement(edka1_cust, "STRAS").text = invoice.customer_address

    # Line Item Segments
    for idx, item in enumerate(invoice.line_items, start=1):
        edp01 = ET.SubElement(idoc, "E1EDP01", {"SEGMENT": "1"})
        ET.SubElement(edp01, "POSEX").text = str(idx)
        ET.SubElement(edp01, "MENGE").text = f"{item.quantity:.3f}"
        ET.SubElement(edp01, "MENEE").text = item.unit_of_measure or "PCE"
        ET.SubElement(edp01, "VPREI").text = f"{item.unit_price:.2f}"
        ET.SubElement(edp01, "NETWR").text = f"{item.total:.2f}"
        ET.SubElement(edp01, "ARKTX").text = item.description

    # Summary Segment
    eds01_total = ET.SubElement(idoc, "E1EDS01", {"SEGMENT": "1"})
    ET.SubElement(eds01_total, "SUMID").text = "002"
    ET.SubElement(eds01_total, "SUMME").text = f"{invoice.total_amount:.2f}"

    if invoice.tax_amount > 0:
        eds01_tax = ET.SubElement(idoc, "E1EDS01", {"SEGMENT": "1"})
        ET.SubElement(eds01_tax, "SUMID").text = "005"
        ET.SubElement(eds01_tax, "SUMME").text = f"{invoice.tax_amount:.2f}"

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def export_to_sap_journal_csv(doc: Invoice | BankStatement) -> str:
    """Export an Invoice or BankStatement to SAP S/4HANA Journal Entry CSV format.

    Header columns:
    PostingDate,DocumentDate,DocumentType,CompanyCode,Currency,Reference,HeaderDocText,PostingKey,Account,Amount,TaxCode,ItemText

    Args:
        doc: Validated Invoice or BankStatement.

    Returns:
        RFC 4180 compliant CSV string.
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    headers = [
        "PostingDate",
        "DocumentDate",
        "DocumentType",
        "CompanyCode",
        "Currency",
        "Reference",
        "HeaderDocText",
        "PostingKey",
        "Account",
        "Amount",
        "TaxCode",
        "ItemText",
    ]
    writer.writerow(headers)

    company_code = "1000"

    if isinstance(doc, Invoice):
        post_date = doc.issue_date.strftime("%Y%m%d")
        doc_date = post_date
        doc_type = "KR"  # Vendor Invoice
        ref = doc.invoice_number
        currency = doc.currency

        # Expense Debit (PostingKey 40)
        writer.writerow(
            [
                post_date,
                doc_date,
                doc_type,
                company_code,
                currency,
                ref,
                f"Inv {doc.vendor_name}",
                "40",
                "600000",
                f"{doc.subtotal:.2f}",
                "V1" if doc.tax_amount > 0 else "V0",
                doc.line_items[0].description if doc.line_items else "Vendor expense",
            ]
        )

        # Tax Debit (PostingKey 40) if tax present
        if doc.tax_amount > 0:
            writer.writerow(
                [
                    post_date,
                    doc_date,
                    doc_type,
                    company_code,
                    currency,
                    ref,
                    f"Tax {doc.vendor_name}",
                    "40",
                    "154000",
                    f"{doc.tax_amount:.2f}",
                    "V1",
                    "Input Tax",
                ]
            )

        # Vendor Credit (PostingKey 31)
        writer.writerow(
            [
                post_date,
                doc_date,
                doc_type,
                company_code,
                currency,
                ref,
                f"Vendor {doc.vendor_name}",
                "31",
                "700000",
                f"-{doc.total_amount:.2f}",
                "",
                doc.vendor_name,
            ]
        )
    else:
        # BankStatement
        doc_type = "SA"  # G/L Account Document
        currency = doc.currency
        ref = f"STMT-{doc.statement_period_end.strftime('%Y%m%d')}"

        for tx in doc.transactions:
            tx_date = tx.transaction_date.strftime("%Y%m%d")
            tx_amt = abs(tx.amount)
            if tx.amount > 0:
                # Bank Debit (40), Clearing Credit (50)
                writer.writerow(
                    [
                        tx_date,
                        tx_date,
                        doc_type,
                        company_code,
                        currency,
                        ref,
                        "Bank Deposit",
                        "40",
                        "113100",
                        f"{tx_amt:.2f}",
                        "",
                        tx.description[:50],
                    ]
                )
                writer.writerow(
                    [
                        tx_date,
                        tx_date,
                        doc_type,
                        company_code,
                        currency,
                        ref,
                        "Clearing Account",
                        "50",
                        "113199",
                        f"-{tx_amt:.2f}",
                        "",
                        tx.counterparty_name or "Deposit",
                    ]
                )
            else:
                # Clearing Debit (40), Bank Credit (50)
                writer.writerow(
                    [
                        tx_date,
                        tx_date,
                        doc_type,
                        company_code,
                        currency,
                        ref,
                        "Clearing Account",
                        "40",
                        "113199",
                        f"{tx_amt:.2f}",
                        "",
                        tx.counterparty_name or "Withdrawal",
                    ]
                )
                writer.writerow(
                    [
                        tx_date,
                        tx_date,
                        doc_type,
                        company_code,
                        currency,
                        ref,
                        "Bank Withdrawal",
                        "50",
                        "113100",
                        f"-{tx_amt:.2f}",
                        "",
                        tx.description[:50],
                    ]
                )

    return output.getvalue()


def export_to_quickbooks_iif(doc: Invoice | Receipt) -> str:
    """Export an Invoice or Receipt to QuickBooks Intuit Interchange Format (.iif).

    Args:
        doc: Validated Invoice or Receipt.

    Returns:
        Formatted .iif text string with !TRNS, !SPL, and !ENDTRNS blocks.
    """
    lines = [
        "!TRNS\tTRNSID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO",
        "!SPL\tSPLID\tTRNSTYPE\tDATE\tACCNT\tNAME\tAMOUNT\tDOCNUM\tMEMO",
        "!ENDTRNS",
    ]

    if isinstance(doc, Invoice):
        dt_str = doc.issue_date.strftime("%m/%d/%Y")
        vendor = doc.vendor_name
        doc_num = doc.invoice_number
        total = doc.total_amount
        subtotal = doc.subtotal
        tax = doc.tax_amount

        # Accounts Payable credit line is negative in QuickBooks IIF
        lines.append(
            f"TRNS\t\tBILL\t{dt_str}\tAccounts Payable\t{vendor}\t-{total:.2f}\t{doc_num}\tInvoice"
        )
        lines.append(
            f"SPL\t\tBILL\t{dt_str}\tJob Expenses\t{vendor}\t{subtotal:.2f}\t{doc_num}\tSubtotal"
        )
        if tax > 0:
            lines.append(
                f"SPL\t\tBILL\t{dt_str}\tSales Tax\t{vendor}\t{tax:.2f}\t{doc_num}\tTax"
            )
        lines.append("ENDTRNS")
    else:
        dt_str = doc.transaction_date.strftime("%m/%d/%Y")
        vendor = doc.merchant_name
        doc_num = doc.receipt_number or ""
        total = doc.total_amount
        subtotal = doc.subtotal if doc.subtotal > 0 else total - doc.tax_amount
        tax = doc.tax_amount

        lines.append(
            f"TRNS\t\tCHECK\t{dt_str}\tBank Account\t{vendor}\t-{total:.2f}\t{doc_num}\tReceipt Expense"
        )
        lines.append(
            f"SPL\t\tCHECK\t{dt_str}\tMeals & Entertainment\t{vendor}\t{subtotal:.2f}\t{doc_num}\tExpense"
        )
        if tax > 0:
            lines.append(
                f"SPL\t\tCHECK\t{dt_str}\tSales Tax\t{vendor}\t{tax:.2f}\t{doc_num}\tTax"
            )
        lines.append("ENDTRNS")

    return "\r\n".join(lines)


def export_to_quickbooks_json(doc: Invoice | Receipt) -> dict[str, Any]:
    """Export an Invoice or Receipt to QuickBooks Online API Bill / Purchase JSON payload.

    Args:
        doc: Validated Invoice or Receipt.

    Returns:
        Dictionary adhering to QuickBooks Online API specifications.
    """
    if isinstance(doc, Invoice):
        lines: list[dict[str, Any]] = []
        if doc.line_items:
            for item in doc.line_items:
                lines.append(
                    {
                        "DetailType": "ItemBasedExpenseLineDetail",
                        "Amount": item.total,
                        "Description": item.description,
                        "ItemBasedExpenseLineDetail": {
                            "UnitPrice": item.unit_price,
                            "Qty": item.quantity,
                            "ItemRef": {"name": item.description[:30]},
                        },
                    }
                )
        else:
            lines.append(
                {
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "Amount": doc.subtotal,
                    "Description": "Invoice goods and services",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"name": "Job Expenses"}
                    },
                }
            )

        payload: dict[str, Any] = {
            "TxnDate": doc.issue_date.isoformat(),
            "DocNumber": doc.invoice_number,
            "VendorRef": {"name": doc.vendor_name},
            "CurrencyRef": {"value": doc.currency},
            "TotalAmt": doc.total_amount,
            "Line": lines,
        }
        if doc.due_date:
            payload["DueDate"] = doc.due_date.isoformat()
        if doc.purchase_order_number:
            payload["PrivateNote"] = f"PO: {doc.purchase_order_number}"
        return payload

    # Receipt
    lines = [
        {
            "DetailType": "AccountBasedExpenseLineDetail",
            "Amount": doc.subtotal
            if doc.subtotal > 0
            else (doc.total_amount - doc.tax_amount),
            "Description": doc.items[0].description if doc.items else "General expense",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"name": "Meals and Entertainment"}
            },
        }
    ]
    if doc.tax_amount > 0:
        lines.append(
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": doc.tax_amount,
                "Description": "Sales Tax",
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"name": "Sales Tax Expense"}
                },
            }
        )

    return {
        "PaymentType": "CreditCard" if doc.card_last_four else "Cash",
        "TxnDate": doc.transaction_date.isoformat(),
        "DocNumber": doc.receipt_number or "",
        "EntityRef": {"type": "Vendor", "name": doc.merchant_name},
        "CurrencyRef": {"value": doc.currency},
        "TotalAmt": doc.total_amount,
        "Line": lines,
    }


def export_to_xero_csv(doc: Invoice | Receipt) -> str:
    """Export an Invoice or Receipt to official Xero Bills CSV import format.

    Columns:
    *ContactName,*InvoiceNumber,*InvoiceDate,*DueDate,*Total,*Quantity,*UnitAmount,*AccountCode,*TaxType,Description,Currency

    Args:
        doc: Validated Invoice or Receipt.

    Returns:
        RFC 4180 compliant CSV string for Xero Bills import.
    """
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\r\n")
    headers = [
        "*ContactName",
        "*InvoiceNumber",
        "*InvoiceDate",
        "*DueDate",
        "*Total",
        "*Quantity",
        "*UnitAmount",
        "*AccountCode",
        "*TaxType",
        "Description",
        "Currency",
    ]
    writer.writerow(headers)

    if isinstance(doc, Invoice):
        contact = doc.vendor_name
        inv_num = doc.invoice_number
        inv_date = doc.issue_date.strftime("%d/%m/%Y")
        due_date = doc.due_date.strftime("%d/%m/%Y") if doc.due_date else inv_date
        total = f"{doc.total_amount:.2f}"
        currency = doc.currency

        if doc.line_items:
            for item in doc.line_items:
                tax_type = "INPUT" if (doc.tax_amount > 0) else "NONE"
                writer.writerow(
                    [
                        contact,
                        inv_num,
                        inv_date,
                        due_date,
                        total,
                        f"{item.quantity:.2f}",
                        f"{item.unit_price:.2f}",
                        "300",  # Default Direct Costs / Expenses
                        tax_type,
                        item.description,
                        currency,
                    ]
                )
        else:
            tax_type = "INPUT" if doc.tax_amount > 0 else "NONE"
            writer.writerow(
                [
                    contact,
                    inv_num,
                    inv_date,
                    due_date,
                    total,
                    "1.00",
                    f"{doc.subtotal:.2f}",
                    "300",
                    tax_type,
                    "Invoice services",
                    currency,
                ]
            )
    else:
        # Receipt
        contact = doc.merchant_name
        inv_num = (
            doc.receipt_number or f"RCPT-{doc.transaction_date.strftime('%Y%m%d')}"
        )
        inv_date = doc.transaction_date.strftime("%d/%m/%Y")
        total = f"{doc.total_amount:.2f}"
        tax_type = "INPUT" if doc.tax_amount > 0 else "NONE"
        currency = doc.currency

        writer.writerow(
            [
                contact,
                inv_num,
                inv_date,
                inv_date,
                total,
                "1.00",
                f"{doc.total_amount:.2f}",
                "420",  # Travel and Entertainment
                tax_type,
                doc.items[0].description if doc.items else "Expense receipt",
                currency,
            ]
        )

    return output.getvalue()


def export_to_xero_json(doc: Invoice | Receipt) -> dict[str, Any]:
    """Export an Invoice or Receipt to Xero Accounting API JSON format.

    Args:
        doc: Validated Invoice or Receipt.

    Returns:
        JSON structure matching Xero Invoices POST API schema.
    """
    if isinstance(doc, Invoice):
        line_items = []
        if doc.line_items:
            for item in doc.line_items:
                line_items.append(
                    {
                        "Description": item.description,
                        "Quantity": item.quantity,
                        "UnitAmount": item.unit_price,
                        "LineAmount": item.total,
                        "AccountCode": "300",
                    }
                )
        else:
            line_items.append(
                {
                    "Description": "Invoice lines",
                    "Quantity": 1.0,
                    "UnitAmount": doc.subtotal,
                    "LineAmount": doc.subtotal,
                    "AccountCode": "300",
                }
            )

        return {
            "Invoices": [
                {
                    "Type": "ACCPAY",
                    "Contact": {"Name": doc.vendor_name},
                    "Date": doc.issue_date.isoformat(),
                    "DueDate": doc.due_date.isoformat()
                    if doc.due_date
                    else doc.issue_date.isoformat(),
                    "InvoiceNumber": doc.invoice_number,
                    "CurrencyCode": doc.currency,
                    "Status": "AUTHORISED",
                    "LineItems": line_items,
                    "Total": doc.total_amount,
                    "TotalTax": doc.tax_amount,
                }
            ]
        }

    # Receipt
    return {
        "Invoices": [
            {
                "Type": "ACCPAY",
                "Contact": {"Name": doc.merchant_name},
                "Date": doc.transaction_date.isoformat(),
                "DueDate": doc.transaction_date.isoformat(),
                "InvoiceNumber": doc.receipt_number
                or f"REC-{doc.transaction_date.strftime('%Y%m%d')}",
                "CurrencyCode": doc.currency,
                "Status": "PAID",
                "LineItems": [
                    {
                        "Description": doc.items[0].description
                        if doc.items
                        else "Receipt expense",
                        "Quantity": 1.0,
                        "UnitAmount": doc.total_amount,
                        "AccountCode": "420",
                    }
                ],
                "Total": doc.total_amount,
                "TotalTax": doc.tax_amount,
            }
        ]
    }
