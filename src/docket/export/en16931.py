"""EN 16931 e-invoices: UBL 2.1 and UN/CEFACT CII, per profile.

    en16931 UBL · Peppol BIS Billing 3.0 UBL · XRechnung 3.0 UBL/CII ·
    Factur-X / ZUGFeRD BASIC and EN16931 CII

An Invoice or CreditNote is first mapped onto the EN 16931
semantic model (business terms BT-/BG-), then written in the syntax the
profile uses, with elements in the order the XML Schemas require.

The export never invents data. It refuses (ExportError) when EN 16931 can't
be satisfied from what was extracted: no invoice lines (BR-16), a VAT rate
it cannot state (BR-S-05), or a per-rate VAT breakdown that disagrees with
the stated tax amount. Anything else a profile requires but the document
doesn't state — a country code, a Peppol endpoint, a Leitweg-ID — is left
out, and `docket.einvoice.validate_einvoice` reports it with the official
rule that is violated.

Known simplifications: every line is VAT category S (rate > 0) or Z
(0 %); exempt, reverse-charge and intra-community categories (E, AE, K, G,
O) need an exemption reason the schemas don't capture yet. A document-level
discount or shipping charge carries the rate of a single-rate invoice and is
refused on a multi-rate one.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from ..catalog.common import Address, Party
from ..catalog.models import CreditNote, Invoice

Syntax = Literal["ubl", "cii"]

UBL_INVOICE = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
UBL_CREDIT_NOTE = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
RSM = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
RAM = "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
UDT = "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100"
QDT = "urn:un:unece:uncefact:data:standard:QualifiedDataType:100"

PEPPOL_PROCESS = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    syntax: Syntax
    specification_id: str  # BT-24
    process_id: str | None = None  # BT-23


PROFILES: dict[str, ProfileSpec] = {
    "en16931": ProfileSpec("en16931", "ubl", "urn:cen.eu:en16931:2017"),
    "peppol": ProfileSpec(
        "peppol", "ubl", "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0", PEPPOL_PROCESS
    ),
    "xrechnung-ubl": ProfileSpec(
        "xrechnung", "ubl", "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0", PEPPOL_PROCESS
    ),
    "xrechnung-cii": ProfileSpec(
        "factur-x-xrechnung", "cii", "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0",
        PEPPOL_PROCESS,
    ),
    "factur-x-en16931": ProfileSpec("factur-x-en16931", "cii", "urn:cen.eu:en16931:2017"),
    "factur-x-basic": ProfileSpec("factur-x-basic", "cii", "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic"),
}


class EN16931Error(ValueError):
    """The extracted document can't be expressed as a valid EN 16931 invoice."""


# ---- semantic model --------------------------------------------------------------

_CENT = Decimal("0.01")


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _qty(value: float) -> str:
    text = f"{Decimal(str(value)).normalize():f}"
    return text if "." in text or "E" not in text else str(value)


# UN/ECE Recommendation 20 codes for the units invoices usually print.
_UNITS = {
    "pcs": "C62", "pc": "C62", "piece": "C62", "pieces": "C62", "unit": "C62", "units": "C62", "ea": "C62",
    "each": "C62", "stk": "C62", "stück": "C62", "st": "C62", "x": "C62",
    "h": "HUR", "hr": "HUR", "hrs": "HUR", "hour": "HUR", "hours": "HUR", "std": "HUR",
    "day": "DAY", "days": "DAY", "month": "MON", "months": "MON", "year": "ANN", "week": "WEE",
    "kg": "KGM", "g": "GRM", "t": "TNE", "m": "MTR", "km": "KMT", "cm": "CMT", "m2": "MTK", "m3": "MTQ",
    "l": "LTR", "litre": "LTR", "liter": "LTR", "kwh": "KWH", "set": "SET", "pack": "PK", "box": "BX",
    "pallet": "PF", "pallets": "PF", "lump sum": "LS", "ls": "LS",
}


def unit_code(unit: str | None) -> str:
    if not unit:
        return "C62"
    text = unit.strip()
    if re.fullmatch(r"[A-Z0-9]{2,3}", text):
        return text
    return _UNITS.get(text.lower().rstrip("."), "C62")


@dataclass
class VatGroup:
    category: str
    rate: Decimal
    taxable: Decimal = Decimal("0")
    tax: Decimal = Decimal("0")


@dataclass
class Line:
    line_id: str
    name: str
    quantity: float
    unit: str
    price: Decimal
    net: Decimal
    category: str
    rate: Decimal
    seller_item_id: str | None


@dataclass
class Semantic:
    """EN 16931 business terms of one invoice or credit note."""

    credit_note: bool
    number: str
    issue_date: str
    type_code: str
    currency: str
    due_date: str | None
    buyer_reference: str | None
    order_reference: str | None
    preceding_invoice: str | None
    seller: Party
    buyer: Party
    payment_reference: str | None
    payment_terms: str | None
    iban: str | None
    bic: str | None
    lines: list[Line]
    allowance: Decimal
    charge: Decimal
    groups: list[VatGroup]
    line_total: Decimal
    tax_exclusive: Decimal
    tax_total: Decimal
    tax_inclusive: Decimal
    payable: Decimal
    notes: list[str] = field(default_factory=list)


def _rate(doc) -> Decimal | None:
    if doc.tax_rate_percent is not None:
        return Decimal(str(doc.tax_rate_percent))
    base = money(doc.subtotal) - money(doc.discount_amount) + money(doc.shipping_amount)
    if doc.tax_amount == 0:
        return Decimal("0")
    if base == 0:
        return None
    rate = (money(doc.tax_amount) / base * 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    # Only a rate that reproduces the stated tax to the cent is a rate the
    # document implies; anything else would be a guess.
    return rate if money(base * rate / 100) == money(doc.tax_amount) else None


def semantic(doc: Invoice | CreditNote) -> Semantic:
    credit_note = isinstance(doc, CreditNote)
    if not doc.line_items:
        raise EN16931Error("EN 16931 requires at least one invoice line (BR-16); the extraction found none")
    document_rate = _rate(doc)
    lines: list[Line] = []
    groups: OrderedDict[tuple[str, Decimal], VatGroup] = OrderedDict()
    for n, item in enumerate(doc.line_items, start=1):
        rate = Decimal(str(item.tax_rate_percent)) if item.tax_rate_percent is not None else document_rate
        if rate is None:
            raise EN16931Error(
                f"line {n} has no VAT rate and none follows from the document (BR-S-05); "
                f"subtotal {doc.subtotal}, tax {doc.tax_amount}"
            )
        category = "S" if rate > 0 else "Z"
        net = money(item.total)
        price = money(item.unit_price) if item.quantity else net
        lines.append(Line(str(n), item.description, item.quantity or 1, unit_code(item.unit_of_measure), price, net,
                          category, rate, item.sku))
        group = groups.setdefault((category, rate), VatGroup(category, rate))
        group.taxable += net
    allowance = money(doc.discount_amount)
    charge = money(doc.shipping_amount)
    if (allowance or charge) and len(groups) > 1:
        raise EN16931Error(
            "a document-level discount or charge on an invoice with several VAT rates needs its own VAT "
            "category, which the document does not state"
        )
    if groups:
        only = next(iter(groups.values()))
        only.taxable += charge - allowance
    for group in groups.values():
        group.tax = money(group.taxable * group.rate / 100)
    tax_total = sum((g.tax for g in groups.values()), Decimal("0"))
    stated_tax = money(doc.tax_amount)
    if len(groups) == 1:
        # One rate: the stated tax is the breakdown's tax. EN 16931 allows
        # rounding at document level, so a cent of difference is the
        # document's rounding, not an error.
        only = next(iter(groups.values()))
        if abs(only.tax - stated_tax) > _CENT:
            raise EN16931Error(
                f"{only.rate}% of {_fmt(only.taxable)} is {_fmt(only.tax)}, but the document states tax "
                f"{_fmt(stated_tax)}"
            )
        only.tax = stated_tax
        tax_total = stated_tax
    elif tax_total != stated_tax:
        raise EN16931Error(
            f"VAT by rate sums to {_fmt(tax_total)}, but the document states tax {_fmt(stated_tax)}"
        )
    line_total = sum((line.net for line in lines), Decimal("0"))
    if abs(line_total - money(doc.subtotal)) > _CENT:
        raise EN16931Error(f"lines sum to {_fmt(line_total)}, subtotal is {_fmt(money(doc.subtotal))} (BR-CO-10)")
    tax_exclusive = line_total - allowance + charge
    tax_inclusive = tax_exclusive + tax_total
    account = doc.payment_account
    return Semantic(
        credit_note=credit_note,
        number=doc.credit_note_number if credit_note else doc.invoice_number,
        issue_date=doc.issue_date.isoformat(),
        type_code="381" if credit_note else "380",
        currency=doc.currency.upper(),
        due_date=doc.due_date.isoformat() if doc.due_date else None,
        buyer_reference=doc.buyer_reference,
        order_reference=doc.reference("purchase_order"),
        preceding_invoice=doc.reference("invoice"),
        seller=doc.seller,
        buyer=doc.buyer,
        payment_reference=doc.payment_reference,
        payment_terms=doc.payment_terms,
        iban=account.iban.replace(" ", "") if account and account.iban else None,
        bic=account.bic.replace(" ", "") if account and account.bic else None,
        lines=lines,
        allowance=allowance,
        charge=charge,
        groups=list(groups.values()),
        line_total=line_total,
        tax_exclusive=tax_exclusive,
        tax_total=tax_total,
        tax_inclusive=tax_inclusive,
        payable=tax_inclusive,
        notes=[doc.reason] if credit_note and doc.reason else [],
    )


def _vat_id(party: Party) -> str | None:
    value = party.tax_id("vat")
    return value.replace(" ", "").replace("-", "").replace(".", "") if value else None


def _tax_registration(party: Party) -> str | None:
    return party.tax_id("tax_id", "gst")


def _legal_id(party: Party) -> str | None:
    return party.tax_id("company_registration")


def _payment_means(sem: Semantic) -> str:
    # 58 = SEPA credit transfer (needs an IBAN), 30 = credit transfer.
    return "58" if sem.iban else "30"


# ---- UBL -----------------------------------------------------------------------------------


class _Ubl:
    def __init__(self) -> None:
        ET.register_namespace("cac", CAC)
        ET.register_namespace("cbc", CBC)

    @staticmethod
    def cbc(parent, tag, text, **attrs):
        el = ET.SubElement(parent, f"{{{CBC}}}{tag}", attrs)
        el.text = text
        return el

    @staticmethod
    def cac(parent, tag):
        return ET.SubElement(parent, f"{{{CAC}}}{tag}")

    def address(self, parent, address: Address | None) -> None:
        post = self.cac(parent, "PostalAddress")
        if address is not None:
            if address.street:
                self.cbc(post, "StreetName", address.street)
            elif address.text:
                self.cbc(post, "StreetName", address.text)
            if address.additional_line:
                self.cbc(post, "AdditionalStreetName", address.additional_line)
            if address.city:
                self.cbc(post, "CityName", address.city)
            if address.postal_code:
                self.cbc(post, "PostalZone", address.postal_code)
            if address.region:
                self.cbc(post, "CountrySubentity", address.region)
            if address.country_code:
                self.cbc(self.cac(post, "Country"), "IdentificationCode", address.country_code)

    def party(self, parent, role: str, party: Party) -> None:
        wrapper = self.cac(parent, role)
        node = self.cac(wrapper, "Party")
        if party.electronic_address:
            self.cbc(node, "EndpointID", party.electronic_address, schemeID=party.electronic_address_scheme or "EM")
        self.cbc(self.cac(node, "PartyName"), "Name", party.name)
        self.address(node, party.address)
        vat = _vat_id(party)
        if vat:
            scheme = self.cac(node, "PartyTaxScheme")
            self.cbc(scheme, "CompanyID", vat)
            self.cbc(self.cac(scheme, "TaxScheme"), "ID", "VAT")
        registration = _tax_registration(party)
        if registration and role == "AccountingSupplierParty":
            scheme = self.cac(node, "PartyTaxScheme")
            self.cbc(scheme, "CompanyID", registration)
            self.cbc(self.cac(scheme, "TaxScheme"), "ID", "FC")
        legal = self.cac(node, "PartyLegalEntity")
        self.cbc(legal, "RegistrationName", party.name)
        if _legal_id(party):
            self.cbc(legal, "CompanyID", _legal_id(party))
        if party.contact_name or party.phone or party.email:
            contact = self.cac(node, "Contact")
            if party.contact_name:
                self.cbc(contact, "Name", party.contact_name)
            if party.phone:
                self.cbc(contact, "Telephone", party.phone)
            if party.email:
                self.cbc(contact, "ElectronicMail", party.email)

    def tax_category(self, parent, tag: str, category: str, rate: Decimal) -> None:
        node = self.cac(parent, tag)
        self.cbc(node, "ID", category)
        self.cbc(node, "Percent", _fmt(rate))
        self.cbc(self.cac(node, "TaxScheme"), "ID", "VAT")

    def render(self, sem: Semantic, profile: ProfileSpec) -> str:
        credit = sem.credit_note
        root = ET.Element(f"{{{UBL_CREDIT_NOTE if credit else UBL_INVOICE}}}{'CreditNote' if credit else 'Invoice'}")
        cbc, cac, cur = self.cbc, self.cac, sem.currency
        cbc(root, "CustomizationID", profile.specification_id)
        if profile.process_id:
            cbc(root, "ProfileID", profile.process_id)
        cbc(root, "ID", sem.number)
        cbc(root, "IssueDate", sem.issue_date)
        if sem.due_date and not credit:
            cbc(root, "DueDate", sem.due_date)
        cbc(root, "CreditNoteTypeCode" if credit else "InvoiceTypeCode", sem.type_code)
        for note in sem.notes:
            cbc(root, "Note", note)
        cbc(root, "DocumentCurrencyCode", cur)
        if sem.buyer_reference:
            cbc(root, "BuyerReference", sem.buyer_reference)
        if sem.order_reference:
            cbc(cac(root, "OrderReference"), "ID", sem.order_reference)
        if sem.preceding_invoice:
            cbc(cac(cac(root, "BillingReference"), "InvoiceDocumentReference"), "ID", sem.preceding_invoice)
        self.party(root, "AccountingSupplierParty", sem.seller)
        self.party(root, "AccountingCustomerParty", sem.buyer)
        means = cac(root, "PaymentMeans")
        cbc(means, "PaymentMeansCode", _payment_means(sem))
        if sem.due_date and credit:
            cbc(means, "PaymentDueDate", sem.due_date)
        if sem.payment_reference:
            cbc(means, "PaymentID", sem.payment_reference)
        if sem.iban:
            account = cac(means, "PayeeFinancialAccount")
            cbc(account, "ID", sem.iban)
            if sem.bic:
                cbc(cac(account, "FinancialInstitutionBranch"), "ID", sem.bic)
        if sem.payment_terms:
            cbc(cac(root, "PaymentTerms"), "Note", sem.payment_terms)
        group = sem.groups[0]
        for indicator, amount, reason in ((False, sem.allowance, "Discount"), (True, sem.charge, "Shipping")):
            if amount:
                node = cac(root, "AllowanceCharge")
                cbc(node, "ChargeIndicator", "true" if indicator else "false")
                cbc(node, "AllowanceChargeReason", reason)
                cbc(node, "Amount", _fmt(amount), currencyID=cur)
                self.tax_category(node, "TaxCategory", group.category, group.rate)
        total = cac(root, "TaxTotal")
        cbc(total, "TaxAmount", _fmt(sem.tax_total), currencyID=cur)
        for g in sem.groups:
            sub = cac(total, "TaxSubtotal")
            cbc(sub, "TaxableAmount", _fmt(g.taxable), currencyID=cur)
            cbc(sub, "TaxAmount", _fmt(g.tax), currencyID=cur)
            self.tax_category(sub, "TaxCategory", g.category, g.rate)
        monetary = cac(root, "LegalMonetaryTotal")
        cbc(monetary, "LineExtensionAmount", _fmt(sem.line_total), currencyID=cur)
        cbc(monetary, "TaxExclusiveAmount", _fmt(sem.tax_exclusive), currencyID=cur)
        cbc(monetary, "TaxInclusiveAmount", _fmt(sem.tax_inclusive), currencyID=cur)
        if sem.allowance:
            cbc(monetary, "AllowanceTotalAmount", _fmt(sem.allowance), currencyID=cur)
        if sem.charge:
            cbc(monetary, "ChargeTotalAmount", _fmt(sem.charge), currencyID=cur)
        cbc(monetary, "PayableAmount", _fmt(sem.payable), currencyID=cur)
        for line in sem.lines:
            node = cac(root, "CreditNoteLine" if credit else "InvoiceLine")
            cbc(node, "ID", line.line_id)
            cbc(node, "CreditedQuantity" if credit else "InvoicedQuantity", _qty(line.quantity), unitCode=line.unit)
            cbc(node, "LineExtensionAmount", _fmt(line.net), currencyID=cur)
            item = cac(node, "Item")
            cbc(item, "Name", line.name)
            if line.seller_item_id:
                cbc(cac(item, "SellersItemIdentification"), "ID", line.seller_item_id)
            self.tax_category(item, "ClassifiedTaxCategory", line.category, line.rate)
            cbc(cac(node, "Price"), "PriceAmount", _fmt(line.price), currencyID=cur)
        ET.indent(root, space="  ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


# ---- CII -----------------------------------------------------------------------------------


class _Cii:
    def __init__(self) -> None:
        for prefix, uri in (("rsm", RSM), ("ram", RAM), ("udt", UDT), ("qdt", QDT)):
            ET.register_namespace(prefix, uri)

    @staticmethod
    def el(parent, ns, tag, text=None, **attrs):
        node = ET.SubElement(parent, f"{{{ns}}}{tag}", attrs)
        if text is not None:
            node.text = text
        return node

    def ram(self, parent, tag, text=None, **attrs):
        return self.el(parent, RAM, tag, text, **attrs)

    def date(self, parent, tag, value: str) -> None:
        self.el(self.ram(parent, tag), UDT, "DateTimeString", value.replace("-", ""), format="102")

    def party(self, parent, tag: str, party: Party, *, seller: bool, basic: bool) -> None:
        ram = self.ram
        node = ram(parent, tag)
        ram(node, "Name", party.name)
        if _legal_id(party):
            ram(ram(node, "SpecifiedLegalOrganization"), "ID", _legal_id(party))
        if seller and not basic and (party.contact_name or party.phone or party.email):
            contact = ram(node, "DefinedTradeContact")
            if party.contact_name:
                ram(contact, "PersonName", party.contact_name)
            if party.phone:
                ram(ram(contact, "TelephoneUniversalCommunication"), "CompleteNumber", party.phone)
            if party.email:
                ram(ram(contact, "EmailURIUniversalCommunication"), "URIID", party.email)
        address = party.address
        if address is not None:
            post = ram(node, "PostalTradeAddress")
            if address.postal_code:
                ram(post, "PostcodeCode", address.postal_code)
            line_one = address.street or (address.text if not address.city else None)
            if line_one:
                ram(post, "LineOne", line_one)
            if address.additional_line:
                ram(post, "LineTwo", address.additional_line)
            if address.city:
                ram(post, "CityName", address.city)
            if address.country_code:
                ram(post, "CountryID", address.country_code)
            if address.region:
                ram(post, "CountrySubDivisionName", address.region)
        if party.electronic_address:
            ram(ram(node, "URIUniversalCommunication"), "URIID", party.electronic_address,
                schemeID=party.electronic_address_scheme or "EM")
        if _vat_id(party):
            ram(ram(node, "SpecifiedTaxRegistration"), "ID", _vat_id(party), schemeID="VA")
        if seller and _tax_registration(party):
            ram(ram(node, "SpecifiedTaxRegistration"), "ID", _tax_registration(party), schemeID="FC")

    def tax(self, parent, tag, category: str, rate: Decimal, *, calculated=None, basis=None) -> None:
        ram = self.ram
        node = ram(parent, tag)
        if calculated is not None:
            ram(node, "CalculatedAmount", _fmt(calculated))
        ram(node, "TypeCode", "VAT")
        if basis is not None:
            ram(node, "BasisAmount", _fmt(basis))
        ram(node, "CategoryCode", category)
        ram(node, "RateApplicablePercent", _fmt(rate))

    def render(self, sem: Semantic, profile: ProfileSpec) -> str:
        ram = self.ram
        # Factur-X BASIC is a strict subset: no item ids, no contacts, no BIC.
        basic = profile.name == "factur-x-basic"
        root = ET.Element(f"{{{RSM}}}CrossIndustryInvoice")
        context = self.el(root, RSM, "ExchangedDocumentContext")
        if profile.process_id:
            ram(ram(context, "BusinessProcessSpecifiedDocumentContextParameter"), "ID", profile.process_id)
        ram(ram(context, "GuidelineSpecifiedDocumentContextParameter"), "ID", profile.specification_id)
        document = self.el(root, RSM, "ExchangedDocument")
        ram(document, "ID", sem.number)
        ram(document, "TypeCode", sem.type_code)
        self.date(document, "IssueDateTime", sem.issue_date)
        for note in sem.notes:
            ram(ram(document, "IncludedNote"), "Content", note)
        transaction = self.el(root, RSM, "SupplyChainTradeTransaction")
        for line in sem.lines:
            item = ram(transaction, "IncludedSupplyChainTradeLineItem")
            ram(ram(item, "AssociatedDocumentLineDocument"), "LineID", line.line_id)
            product = ram(item, "SpecifiedTradeProduct")
            if line.seller_item_id and not basic:
                ram(product, "SellerAssignedID", line.seller_item_id)
            ram(product, "Name", line.name)
            agreement = ram(item, "SpecifiedLineTradeAgreement")
            ram(ram(agreement, "NetPriceProductTradePrice"), "ChargeAmount", _fmt(line.price))
            ram(ram(item, "SpecifiedLineTradeDelivery"), "BilledQuantity", _qty(line.quantity), unitCode=line.unit)
            settlement = ram(item, "SpecifiedLineTradeSettlement")
            self.tax(settlement, "ApplicableTradeTax", line.category, line.rate)
            ram(ram(settlement, "SpecifiedTradeSettlementLineMonetarySummation"), "LineTotalAmount", _fmt(line.net))
        agreement = ram(transaction, "ApplicableHeaderTradeAgreement")
        if sem.buyer_reference:
            ram(agreement, "BuyerReference", sem.buyer_reference)
        self.party(agreement, "SellerTradeParty", sem.seller, seller=True, basic=basic)
        self.party(agreement, "BuyerTradeParty", sem.buyer, seller=False, basic=basic)
        if sem.order_reference:
            ram(ram(agreement, "BuyerOrderReferencedDocument"), "IssuerAssignedID", sem.order_reference)
        ram(transaction, "ApplicableHeaderTradeDelivery")
        settlement = ram(transaction, "ApplicableHeaderTradeSettlement")
        if sem.payment_reference:
            ram(settlement, "PaymentReference", sem.payment_reference)
        ram(settlement, "InvoiceCurrencyCode", sem.currency)
        means = ram(settlement, "SpecifiedTradeSettlementPaymentMeans")
        ram(means, "TypeCode", _payment_means(sem))
        if sem.iban:
            ram(ram(means, "PayeePartyCreditorFinancialAccount"), "IBANID", sem.iban)
            if sem.bic and not basic:
                ram(ram(means, "PayeeSpecifiedCreditorFinancialInstitution"), "BICID", sem.bic)
        for g in sem.groups:
            self.tax(settlement, "ApplicableTradeTax", g.category, g.rate, calculated=g.tax, basis=g.taxable)
        group = sem.groups[0]
        for indicator, amount, reason in ((False, sem.allowance, "Discount"), (True, sem.charge, "Shipping")):
            if amount:
                node = ram(settlement, "SpecifiedTradeAllowanceCharge")
                self.el(ram(node, "ChargeIndicator"), UDT, "Indicator", "true" if indicator else "false")
                ram(node, "ActualAmount", _fmt(amount))
                ram(node, "Reason", reason)
                self.tax(node, "CategoryTradeTax", group.category, group.rate)
        if sem.payment_terms or sem.due_date:
            terms = ram(settlement, "SpecifiedTradePaymentTerms")
            if sem.payment_terms:
                ram(terms, "Description", sem.payment_terms)
            if sem.due_date:
                self.date(terms, "DueDateDateTime", sem.due_date)
        summary = ram(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
        ram(summary, "LineTotalAmount", _fmt(sem.line_total))
        if sem.charge:
            ram(summary, "ChargeTotalAmount", _fmt(sem.charge))
        if sem.allowance:
            ram(summary, "AllowanceTotalAmount", _fmt(sem.allowance))
        ram(summary, "TaxBasisTotalAmount", _fmt(sem.tax_exclusive))
        ram(summary, "TaxTotalAmount", _fmt(sem.tax_total), currencyID=sem.currency)
        ram(summary, "GrandTotalAmount", _fmt(sem.tax_inclusive))
        ram(summary, "DuePayableAmount", _fmt(sem.payable))
        if sem.preceding_invoice:
            ram(ram(settlement, "InvoiceReferencedDocument"), "IssuerAssignedID", sem.preceding_invoice)
        ET.indent(root, space="  ")
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def render(doc: Invoice | CreditNote, profile: str) -> str:
    """The document as an e-invoice of the given profile (a key of PROFILES)."""
    spec = PROFILES[profile]
    sem = semantic(doc)
    return (_Ubl() if spec.syntax == "ubl" else _Cii()).render(sem, spec)


__all__ = ["EN16931Error", "PROFILES", "ProfileSpec", "Semantic", "render", "semantic", "unit_code"]
