"""Facturae 3.2.2 XML (Spanish Ministry of Finance, FACe).

Not validated against the official Facturae XSD or signed (XAdES); the
EN 16931 formats live in en16931.py and are validated by docket.einvoice.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from ..catalog.models import Invoice
from ._parties import address_line, iban, tax_number, vat_number

# Facturae 3.2.2 Namespaces
FACTURAE_NS = "http://www.facturae.es/Facturae/2014/v3.2.2/Facturae"
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"



def export_to_facturae_xml(invoice: Invoice) -> str:
    """Export an Invoice to Spanish Facturae 3.2.2 XML standard.

    Args:
        invoice: Validated Invoice.

    Returns:
        Formatted Facturae 3.2.2 XML string.
    """
    ET.register_namespace("fe", FACTURAE_NS)
    ET.register_namespace("ds", XMLDSIG_NS)

    root = ET.Element(f"{{{FACTURAE_NS}}}Facturae")

    def fe(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
        elem = ET.SubElement(parent, f"{{{FACTURAE_NS}}}{tag}")
        if text is not None:
            elem.text = text
        return elem

    # FileHeader
    header = fe(root, "FileHeader")
    fe(header, "SchemaVersion", "3.2.2")
    fe(header, "Modality", "I")  # Individual
    fe(header, "InvoiceIssuerType", "EM")  # Emisor (Vendor)

    batch = fe(header, "Batch")
    fe(batch, "BatchIdentifier", f"{invoice.invoice_number}-BATCH")
    fe(batch, "InvoicesCount", "1")
    total_invoices_amt = fe(batch, "TotalInvoicesAmount")
    fe(total_invoices_amt, "TotalAmount", f"{invoice.total_amount:.2f}")
    total_out_amt = fe(batch, "TotalOutstandingAmount")
    fe(total_out_amt, "TotalAmount", f"{invoice.total_amount:.2f}")
    total_exec_amt = fe(batch, "TotalExecutableAmount")
    fe(total_exec_amt, "TotalAmount", f"{invoice.total_amount:.2f}")
    fe(batch, "InvoiceCurrencyCode", invoice.currency)

    # Parties
    parties = fe(root, "Parties")

    # SellerParty
    seller = fe(parties, "SellerParty")
    tax_id_s = fe(seller, "TaxIdentification")
    fe(tax_id_s, "PersonTypeCode", "J")  # Legal Entity
    fe(tax_id_s, "ResidenceTypeCode", "R")  # Resident
    fe(tax_id_s, "TaxIdentificationNumber", vat_number(invoice.seller) or "ES000000000")
    legal_s = fe(seller, "LegalEntity")
    fe(legal_s, "CorporateName", invoice.seller.name)
    addr_s = fe(legal_s, "AddressInSpain")
    fe(addr_s, "Address", address_line(invoice.seller.address) or "Spain")
    fe(addr_s, "PostCode", "28001")
    fe(addr_s, "Town", "Madrid")
    fe(addr_s, "Province", "Madrid")
    fe(addr_s, "CountryCode", "ESP")

    # BuyerParty
    buyer = fe(parties, "BuyerParty")
    tax_id_b = fe(buyer, "TaxIdentification")
    fe(tax_id_b, "PersonTypeCode", "J")
    fe(tax_id_b, "ResidenceTypeCode", "R")
    fe(tax_id_b, "TaxIdentificationNumber", tax_number(invoice.buyer) or "ES000000000")
    legal_b = fe(buyer, "LegalEntity")
    fe(legal_b, "CorporateName", invoice.buyer.name)
    addr_b = fe(legal_b, "AddressInSpain")
    fe(addr_b, "Address", address_line(invoice.buyer.address) or "Spain")
    fe(addr_b, "PostCode", "08001")
    fe(addr_b, "Town", "Barcelona")
    fe(addr_b, "Province", "Barcelona")
    fe(addr_b, "CountryCode", "ESP")

    # Invoices
    invoices = fe(root, "Invoices")
    inv = fe(invoices, "Invoice")

    inv_header = fe(inv, "InvoiceHeader")
    fe(inv_header, "InvoiceNumber", invoice.invoice_number)
    fe(inv_header, "InvoiceDocumentType", "FC")  # Factura Completa
    fe(inv_header, "InvoiceClass", "OO")  # Original

    inv_issue = fe(inv, "InvoiceIssueData")
    fe(inv_issue, "IssueDate", invoice.issue_date.isoformat())
    fe(inv_issue, "InvoiceCurrencyCode", invoice.currency)
    fe(inv_issue, "TaxCurrencyCode", invoice.currency)

    # TaxesOutputs
    taxes_out = fe(inv, "TaxesOutputs")
    tax_elem = fe(taxes_out, "Tax")
    fe(tax_elem, "TaxTypeCode", "01")  # IVA
    rate = invoice.tax_rate_percent if invoice.tax_rate_percent is not None else 21.0
    fe(tax_elem, "TaxRate", f"{rate:.2f}")
    taxable_base = fe(tax_elem, "TaxableBase")
    fe(taxable_base, "TotalAmount", f"{invoice.subtotal:.2f}")
    tax_amt = fe(tax_elem, "TaxAmount")
    fe(tax_amt, "TotalAmount", f"{invoice.tax_amount:.2f}")

    # InvoiceTotals
    inv_totals = fe(inv, "InvoiceTotals")
    fe(inv_totals, "TotalGrossAmount", f"{invoice.subtotal:.2f}")
    if invoice.discount_amount > 0:
        fe(inv_totals, "TotalGeneralDiscounts", f"{invoice.discount_amount:.2f}")
    fe(inv_totals, "TotalGrossAmountBeforeTaxes", f"{invoice.subtotal:.2f}")
    fe(inv_totals, "TotalTaxOutputs", f"{invoice.tax_amount:.2f}")
    fe(inv_totals, "TotalTaxesWithheld", "0.00")
    fe(inv_totals, "InvoiceTotal", f"{invoice.total_amount:.2f}")
    fe(inv_totals, "TotalOutstandingAmount", f"{invoice.total_amount:.2f}")
    fe(inv_totals, "TotalExecutableAmount", f"{invoice.total_amount:.2f}")

    # Items
    items_elem = fe(inv, "Items")
    items_to_render = invoice.line_items or []
    if not items_to_render:
        line = fe(items_elem, "InvoiceLine")
        fe(line, "ItemDescription", "Servicios de factura")
        fe(line, "Quantity", "1.00")
        fe(line, "UnitPriceWithoutTax", f"{invoice.subtotal:.2f}")
        fe(line, "TotalCost", f"{invoice.subtotal:.2f}")
        fe(line, "GrossAmount", f"{invoice.subtotal:.2f}")
    else:
        for itm in items_to_render:
            line = fe(items_elem, "InvoiceLine")
            fe(line, "ItemDescription", itm.description)
            fe(line, "Quantity", f"{itm.quantity:.2f}")
            fe(line, "UnitPriceWithoutTax", f"{itm.unit_price:.2f}")
            fe(line, "TotalCost", f"{itm.total:.2f}")
            fe(line, "GrossAmount", f"{itm.total:.2f}")

    # PaymentDetails
    if iban(invoice):
        payment_details = fe(inv, "PaymentDetails")
        installment = fe(payment_details, "Installment")
        fe(
            installment,
            "InstallmentDueDate",
            (invoice.due_date or invoice.issue_date).isoformat(),
        )
        fe(installment, "InstallmentAmount", f"{invoice.total_amount:.2f}")
        fe(installment, "PaymentMeans", "04")  # Transferencia
        fe(installment, "AccountToBeCredited", iban(invoice))

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )
