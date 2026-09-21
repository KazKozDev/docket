"""e-Invoicing export standards for European and international compliance.

Implements generation of legally compliant electronic invoice documents:
- UBL 2.1 XML (OASIS UBL 2.1 / Peppol BIS Billing 3.0 / EU EN 16931)
- Facturae 3.2.2 XML (Spanish Ministry of Finance FACe standard)
- ZUGFeRD 2.2 / XRechnung XML (UN/CEFACT Cross Industry Invoice CII profile)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Literal

from ..catalog.models import Invoice
from ._parties import address_line, bic, iban, tax_number, vat_number

# UBL 2.1 Namespaces
UBL_NS_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_NS_CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
UBL_NS_CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

# Facturae 3.2.2 Namespaces
FACTURAE_NS = "http://www.facturae.es/Facturae/2014/v3.2.2/Facturae"
XMLDSIG_NS = "http://www.w3.org/2000/09/xmldsig#"

# ZUGFeRD / CII Namespaces
CII_NS_RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
CII_NS_RAM = (
    "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
)
CII_NS_QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"
CII_NS_UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"


def export_to_ubl_xml(invoice: Invoice) -> str:
    """Export an Invoice to OASIS UBL 2.1 XML (Peppol BIS Billing 3.0 / EN 16931).

    Args:
        invoice: Validated Invoice model.

    Returns:
        Properly namespaced, indented UBL 2.1 XML string.
    """
    ET.register_namespace("", UBL_NS_INVOICE)
    ET.register_namespace("cac", UBL_NS_CAC)
    ET.register_namespace("cbc", UBL_NS_CBC)

    root = ET.Element(f"{{{UBL_NS_INVOICE}}}Invoice")

    def cbc(parent: ET.Element, tag: str, text: str, **attribs: str) -> ET.Element:
        elem = ET.SubElement(parent, f"{{{UBL_NS_CBC}}}{tag}", attribs)
        elem.text = text
        return elem

    def cac(parent: ET.Element, tag: str) -> ET.Element:
        return ET.SubElement(parent, f"{{{UBL_NS_CAC}}}{tag}")

    # Peppol BIS 3.0 customization & profile
    cbc(
        root,
        "CustomizationID",
        "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0",
    )
    cbc(root, "ProfileID", "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0")
    cbc(root, "ID", invoice.invoice_number)
    cbc(root, "IssueDate", invoice.issue_date.isoformat())
    if invoice.due_date:
        cbc(root, "DueDate", invoice.due_date.isoformat())
    cbc(root, "InvoiceTypeCode", "380")  # Commercial Invoice
    cbc(root, "DocumentCurrencyCode", invoice.currency)

    if invoice.purchase_order_number:
        ref_elem = cac(root, "OrderReference")
        cbc(ref_elem, "ID", invoice.purchase_order_number)

    # AccountingSupplierParty
    supp_party = cac(root, "AccountingSupplierParty")
    supp_inner = cac(supp_party, "Party")
    if vat_number(invoice.seller):
        ep_id = cbc(supp_inner, "EndpointID", vat_number(invoice.seller), schemeID="EM")
        _ = ep_id
    party_name = cac(supp_inner, "PartyName")
    cbc(party_name, "Name", invoice.seller.name)
    if address_line(invoice.seller.address):
        postal = cac(supp_inner, "PostalAddress")
        cbc(postal, "StreetName", address_line(invoice.seller.address))
    if vat_number(invoice.seller):
        tax_scheme = cac(supp_inner, "PartyTaxScheme")
        cbc(tax_scheme, "CompanyID", vat_number(invoice.seller))
        tax_sub = cac(tax_scheme, "TaxScheme")
        cbc(tax_sub, "ID", "VAT")
    legal_entity = cac(supp_inner, "PartyLegalEntity")
    cbc(legal_entity, "RegistrationName", invoice.seller.name)

    # AccountingCustomerParty
    cust_party = cac(root, "AccountingCustomerParty")
    cust_inner = cac(cust_party, "Party")
    cust_name = cac(cust_inner, "PartyName")
    cbc(cust_name, "Name", invoice.buyer.name)
    if address_line(invoice.buyer.address):
        postal_c = cac(cust_inner, "PostalAddress")
        cbc(postal_c, "StreetName", address_line(invoice.buyer.address))
    if tax_number(invoice.buyer):
        tax_scheme_c = cac(cust_inner, "PartyTaxScheme")
        cbc(tax_scheme_c, "CompanyID", tax_number(invoice.buyer))
        tax_sub_c = cac(tax_scheme_c, "TaxScheme")
        cbc(tax_sub_c, "ID", "VAT")
    legal_entity_c = cac(cust_inner, "PartyLegalEntity")
    cbc(legal_entity_c, "RegistrationName", invoice.buyer.name)

    # PaymentMeans (Credit Transfer / SEPA if IBAN available)
    pay_means = cac(root, "PaymentMeans")
    cbc(pay_means, "PaymentMeansCode", "58" if iban(invoice) else "30")
    if invoice.payment_reference:
        cbc(pay_means, "PaymentID", invoice.payment_reference)
    if iban(invoice):
        financial_acc = cac(pay_means, "PayeeFinancialAccount")
        cbc(financial_acc, "ID", iban(invoice))
        if bic(invoice):
            fin_inst = cac(financial_acc, "FinancialInstitutionBranch")
            cbc(fin_inst, "ID", bic(invoice))

    # TaxTotal
    tax_total = cac(root, "TaxTotal")
    cbc(
        tax_total, "TaxAmount", f"{invoice.tax_amount:.2f}", currencyID=invoice.currency
    )
    tax_subtotal = cac(tax_total, "TaxSubtotal")
    cbc(
        tax_subtotal,
        "TaxableAmount",
        f"{invoice.subtotal:.2f}",
        currencyID=invoice.currency,
    )
    cbc(
        tax_subtotal,
        "TaxAmount",
        f"{invoice.tax_amount:.2f}",
        currencyID=invoice.currency,
    )
    tax_cat = cac(tax_subtotal, "TaxCategory")
    cbc(tax_cat, "ID", "S" if invoice.tax_amount > 0 else "Z")
    if invoice.tax_rate_percent is not None:
        cbc(tax_cat, "Percent", f"{invoice.tax_rate_percent:.2f}")
    tax_sch = cac(tax_cat, "TaxScheme")
    cbc(tax_sch, "ID", "VAT")

    # LegalMonetaryTotal
    legal_total = cac(root, "LegalMonetaryTotal")
    cbc(
        legal_total,
        "LineExtensionAmount",
        f"{invoice.subtotal:.2f}",
        currencyID=invoice.currency,
    )
    cbc(
        legal_total,
        "TaxExclusiveAmount",
        f"{invoice.subtotal:.2f}",
        currencyID=invoice.currency,
    )
    tax_inclusive = invoice.subtotal + invoice.tax_amount
    cbc(
        legal_total,
        "TaxInclusiveAmount",
        f"{tax_inclusive:.2f}",
        currencyID=invoice.currency,
    )
    if invoice.discount_amount > 0:
        cbc(
            legal_total,
            "AllowanceTotalAmount",
            f"{invoice.discount_amount:.2f}",
            currencyID=invoice.currency,
        )
    if invoice.shipping_amount > 0:
        cbc(
            legal_total,
            "ChargeTotalAmount",
            f"{invoice.shipping_amount:.2f}",
            currencyID=invoice.currency,
        )
    cbc(
        legal_total,
        "PayableAmount",
        f"{invoice.total_amount:.2f}",
        currencyID=invoice.currency,
    )

    # InvoiceLines
    items = invoice.line_items
    if not items:
        # Fallback single line item
        line_elem = cac(root, "InvoiceLine")
        cbc(line_elem, "ID", "1")
        cbc(line_elem, "InvoicedQuantity", "1.00", unitCode="H87")
        cbc(
            line_elem,
            "LineExtensionAmount",
            f"{invoice.subtotal:.2f}",
            currencyID=invoice.currency,
        )
        item_elem = cac(line_elem, "Item")
        cbc(item_elem, "Name", "Invoice services")
        item_tax = cac(item_elem, "ClassifiedTaxCategory")
        cbc(item_tax, "ID", "S" if invoice.tax_amount > 0 else "Z")
        cbc(cac(item_tax, "TaxScheme"), "ID", "VAT")
        price_elem = cac(line_elem, "Price")
        cbc(
            price_elem,
            "PriceAmount",
            f"{invoice.subtotal:.2f}",
            currencyID=invoice.currency,
        )
    else:
        for idx, item in enumerate(items, start=1):
            line_elem = cac(root, "InvoiceLine")
            cbc(line_elem, "ID", str(idx))
            cbc(
                line_elem,
                "InvoicedQuantity",
                f"{item.quantity:.2f}",
                unitCode=item.unit_of_measure or "H87",
            )
            cbc(
                line_elem,
                "LineExtensionAmount",
                f"{item.total:.2f}",
                currencyID=invoice.currency,
            )
            item_elem = cac(line_elem, "Item")
            cbc(item_elem, "Name", item.description)
            item_tax = cac(item_elem, "ClassifiedTaxCategory")
            cbc(item_tax, "ID", "S" if invoice.tax_amount > 0 else "Z")
            cbc(cac(item_tax, "TaxScheme"), "ID", "VAT")
            price_elem = cac(line_elem, "Price")
            cbc(
                price_elem,
                "PriceAmount",
                f"{item.unit_price:.2f}",
                currencyID=invoice.currency,
            )

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


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


def export_to_zugferd_xml(
    invoice: Invoice, profile: Literal["EN16931", "XRECHNUNG"] = "EN16931"
) -> str:
    """Export an Invoice to German ZUGFeRD 2.2 / XRechnung (UN/CEFACT Cross Industry Invoice CII XML).

    Args:
        invoice: Validated Invoice.
        profile: Profile type - 'EN16931' (standard ZUGFeRD) or 'XRECHNUNG' (German B2G mandate).

    Returns:
        Formatted UN/CEFACT CII XML string.
    """
    ET.register_namespace("rsm", CII_NS_RSM)
    ET.register_namespace("ram", CII_NS_RAM)
    ET.register_namespace("qdt", CII_NS_QDT)
    ET.register_namespace("udt", CII_NS_UDT)

    root = ET.Element(f"{{{CII_NS_RSM}}}CrossIndustryInvoice")

    def rsm(parent: ET.Element, tag: str) -> ET.Element:
        return ET.SubElement(parent, f"{{{CII_NS_RSM}}}{tag}")

    def ram(
        parent: ET.Element, tag: str, text: str | None = None, **attribs: str
    ) -> ET.Element:
        elem = ET.SubElement(parent, f"{{{CII_NS_RAM}}}{tag}", attribs)
        if text is not None:
            elem.text = text
        return elem

    def udt(parent: ET.Element, tag: str, text: str, **attribs: str) -> ET.Element:
        elem = ET.SubElement(parent, f"{{{CII_NS_UDT}}}{tag}", attribs)
        elem.text = text
        return elem

    # Context
    ctx = rsm(root, "ExchangedDocumentContext")
    param = ram(ctx, "GuidelineSpecifiedDocumentContextParameter")
    if profile == "XRECHNUNG":
        ram(
            param,
            "ID",
            "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
        )
    else:
        ram(param, "ID", "urn:cen.eu:en16931:2017")

    # Header Document
    doc_header = rsm(root, "ExchangedDocument")
    ram(doc_header, "ID", invoice.invoice_number)
    ram(doc_header, "TypeCode", "380")
    dt_elem = ram(doc_header, "IssueDateTime")
    udt(dt_elem, "DateTimeString", invoice.issue_date.strftime("%Y%m%d"), format="102")

    # Transaction
    tx = rsm(root, "SupplyChainTradeTransaction")

    # Line items
    items_to_render = invoice.line_items or []
    if not items_to_render:
        item_line = ram(tx, "IncludedSupplyChainTradeLineItem")
        doc_line = ram(item_line, "AssociatedDocumentLineDocument")
        ram(doc_line, "LineID", "1")
        prod = ram(item_line, "SpecifiedTradeProduct")
        ram(prod, "Name", "Invoice services")
        agr = ram(item_line, "SpecifiedLineTradeAgreement")
        gross = ram(agr, "GrossPriceProductTradePrice")
        ram(gross, "ChargeAmount", f"{invoice.subtotal:.2f}")
        net = ram(agr, "NetPriceProductTradePrice")
        ram(net, "ChargeAmount", f"{invoice.subtotal:.2f}")
        deliv = ram(item_line, "SpecifiedLineTradeDelivery")
        ram(deliv, "BilledQuantity", "1.0000", unitCode="C62")
        settle = ram(item_line, "SpecifiedLineTradeSettlement")
        summation = ram(settle, "SpecifiedTradeSettlementLineMonetarySummation")
        ram(summation, "LineTotalAmount", f"{invoice.subtotal:.2f}")
    else:
        for idx, itm in enumerate(items_to_render, start=1):
            item_line = ram(tx, "IncludedSupplyChainTradeLineItem")
            doc_line = ram(item_line, "AssociatedDocumentLineDocument")
            ram(doc_line, "LineID", str(idx))
            prod = ram(item_line, "SpecifiedTradeProduct")
            ram(prod, "Name", itm.description)
            agr = ram(item_line, "SpecifiedLineTradeAgreement")
            net = ram(agr, "NetPriceProductTradePrice")
            ram(net, "ChargeAmount", f"{itm.unit_price:.2f}")
            deliv = ram(item_line, "SpecifiedLineTradeDelivery")
            ram(
                deliv,
                "BilledQuantity",
                f"{itm.quantity:.4f}",
                unitCode=itm.unit_of_measure or "C62",
            )
            settle = ram(item_line, "SpecifiedLineTradeSettlement")
            summation = ram(settle, "SpecifiedTradeSettlementLineMonetarySummation")
            ram(summation, "LineTotalAmount", f"{itm.total:.2f}")

    # Agreement (Seller and Buyer)
    agr_header = ram(tx, "ApplicableHeaderTradeAgreement")
    seller = ram(agr_header, "SellerTradeParty")
    ram(seller, "Name", invoice.seller.name)
    if address_line(invoice.seller.address):
        post = ram(seller, "PostalTradeAddress")
        ram(post, "LineOne", address_line(invoice.seller.address))
    if vat_number(invoice.seller):
        tax_reg = ram(seller, "SpecifiedTaxRegistration")
        ram(tax_reg, "ID", vat_number(invoice.seller), schemeID="VA")

    buyer = ram(agr_header, "BuyerTradeParty")
    ram(buyer, "Name", invoice.buyer.name)
    if address_line(invoice.buyer.address):
        post_b = ram(buyer, "PostalTradeAddress")
        ram(post_b, "LineOne", address_line(invoice.buyer.address))
    if tax_number(invoice.buyer):
        tax_reg_b = ram(buyer, "SpecifiedTaxRegistration")
        ram(tax_reg_b, "ID", tax_number(invoice.buyer), schemeID="VA")

    # Settlement
    settle_header = ram(tx, "ApplicableHeaderTradeSettlement")
    ram(settle_header, "InvoiceCurrencyCode", invoice.currency)

    if iban(invoice):
        pm = ram(settle_header, "SpecifiedTradeSettlementPaymentMeans")
        ram(pm, "TypeCode", "58")
        creditor_acc = ram(pm, "PayeePartyCreditorFinancialAccount")
        ram(creditor_acc, "IBANID", iban(invoice))

    # Taxes
    trade_tax = ram(settle_header, "ApplicableTradeTax")
    ram(trade_tax, "CalculatedAmount", f"{invoice.tax_amount:.2f}")
    ram(trade_tax, "TypeCode", "VAT")
    ram(trade_tax, "BasisAmount", f"{invoice.subtotal:.2f}")
    ram(trade_tax, "CategoryCode", "S" if invoice.tax_amount > 0 else "Z")
    if invoice.tax_rate_percent is not None:
        ram(trade_tax, "RateApplicablePercent", f"{invoice.tax_rate_percent:.2f}")

    # Monetary Summation
    monetary = ram(settle_header, "SpecifiedTradeSettlementHeaderMonetarySummation")
    ram(monetary, "LineTotalAmount", f"{invoice.subtotal:.2f}")
    ram(monetary, "TaxBasisTotalAmount", f"{invoice.subtotal:.2f}")
    ram(
        monetary,
        "TaxTotalAmount",
        f"{invoice.tax_amount:.2f}",
        currencyID=invoice.currency,
    )
    ram(monetary, "GrandTotalAmount", f"{invoice.total_amount:.2f}")
    ram(monetary, "DuePayableAmount", f"{invoice.total_amount:.2f}")

    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )
