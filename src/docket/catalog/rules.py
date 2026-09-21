"""Business rules for the schemas added in 2.x.

Same principles as `docket.validate`: deterministic, no model calls, report
and never repair. Each rule is a `(document, ValidationContext)` validator
registered on its schema in `builtin.py`.
"""
from __future__ import annotations

import re
from datetime import date

from ..schemas import ValidationIssue
from ..validate import (
    _check_cited_sources,
    _check_date_convention,
    _check_date_range,
    _check_party_ids,
    _check_witness_contradicts,
    _core_name,
    _isclose,
    _unconfirmed_vlm_issue,
)
from .models import CertificateOfOrigin, CreditNote, DeliveryNote, IdDocument, TaxInvoice, UtilityBill
from .registry import ValidationContext

_HS_RE = re.compile(r"^\d{6}(\d{2}){0,2}$")
_ALPHA_COUNTRY_RE = re.compile(r"^[A-Z]{2,3}$")


def _same_party(a: str, b: str) -> bool:
    return bool(_core_name(a)) and _core_name(a) == _core_name(b)


# ---- tax invoice ----------------------------------------------------------------


def validate_tax_invoice(inv: TaxInvoice, ctx: ValidationContext) -> list[ValidationIssue]:
    """A tax invoice exists to evidence tax: the seller's registration and the
    tax charged must be on it."""
    issues: list[ValidationIssue] = []
    if inv.seller.tax_id("vat", "gst", "tax_id") is None:
        issues.append(
            ValidationIssue(
                field="seller.tax_ids",
                message="a tax invoice must state the seller's VAT/GST registration number",
            )
        )
    if inv.tax_amount == 0 and inv.tax_rate_percent not in (0, 0.0):
        issues.append(
            ValidationIssue(
                field="tax_amount",
                message="no tax amount stated — a tax invoice should show the tax charged (or a 0% rate)",
                severity="warning",
            )
        )
    if inv.tax_point_date and inv.tax_point_date > inv.issue_date:
        issues.append(
            ValidationIssue(
                field="tax_point_date",
                message="tax point date is after the issue date",
                severity="warning",
            )
        )
    return issues


# ---- credit note ------------------------------------------------------------------


def validate_credit_note(note: CreditNote, ctx: ValidationContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if note.original_invoice_number is None:
        issues.append(
            ValidationIssue(
                field="references",
                message="no original invoice referenced — a credit note is normally issued against one",
                severity="warning",
            )
        )
    return issues


# ---- utility bill -----------------------------------------------------------------


def validate_utility_bill(bill: UtilityBill, ctx: ValidationContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if ctx.vlm_unconfirmed:
        issues.append(_unconfirmed_vlm_issue())
    if not bill.account_number.strip():
        issues.append(ValidationIssue(field="account_number", message="empty account number"))
    if bill.billing_period_end < bill.billing_period_start:
        issues.append(ValidationIssue(field="billing_period_end", message="billing period ends before it starts"))
    if (bill.billing_period_end - bill.billing_period_start).days > 400:
        issues.append(
            ValidationIssue(
                field="billing_period_end",
                message="billing period is longer than a year — check the dates",
                severity="warning",
            )
        )
    if bill.due_date and bill.due_date < bill.issue_date:
        issues.append(ValidationIssue(field="due_date", message="due_date is before issue_date"))
    issues.extend(_check_date_range("issue_date", bill.issue_date, max_years_ahead=1))

    for n, reading in enumerate(bill.meter_readings):
        if reading.previous_reading is None or reading.current_reading is None:
            continue
        # Meters roll over, and some bills print a multiplier; only a plain
        # difference that disagrees is worth flagging.
        difference = reading.current_reading - reading.previous_reading
        if difference >= 0 and not _isclose(difference, reading.consumption, tol=0.5):
            issues.append(
                ValidationIssue(
                    field=f"meter_readings[{n}].consumption",
                    message=(
                        f"readings {reading.previous_reading:g} → {reading.current_reading:g} give "
                        f"{difference:g} {reading.unit}, consumption says {reading.consumption:g}"
                    ),
                    severity="warning",
                )
            )
    if bill.charges:
        charged = sum(c.amount for c in bill.charges)
        if not _isclose(charged, bill.current_charges) and not _isclose(
            charged + bill.tax_amount, bill.current_charges
        ):
            issues.append(
                ValidationIssue(
                    field="current_charges",
                    message=f"itemized charges sum to {charged:.2f}, current charges say {bill.current_charges:.2f}",
                )
            )
    if bill.previous_balance is not None:
        expected = bill.previous_balance - (bill.payments_received or 0.0) + bill.current_charges
        if not _isclose(expected, bill.amount_due):
            issues.append(
                ValidationIssue(
                    field="amount_due",
                    message=(
                        f"previous balance - payments + current charges = {expected:.2f}, "
                        f"amount due says {bill.amount_due:.2f}"
                    ),
                )
            )
    elif not _isclose(bill.current_charges, bill.amount_due):
        issues.append(
            ValidationIssue(
                field="amount_due",
                message=(
                    f"amount due {bill.amount_due:.2f} differs from current charges "
                    f"{bill.current_charges:.2f} and no previous balance explains it"
                ),
                severity="warning",
            )
        )
    issues.extend(_check_party_ids(bill.provider, "provider"))

    if ctx.raw_text is not None:
        numeric = {"amount_due": bill.amount_due, "current_charges": bill.current_charges}
        if bill.tax_amount:
            numeric["tax_amount"] = bill.tax_amount
        issues.extend(_check_cited_sources(bill, ctx.raw_text, numeric))
        issues.extend(_check_witness_contradicts(bill, ctx.witness_pages, list(numeric.items())))
        dates = [("issue_date", bill.issue_date), ("billing_period_start", bill.billing_period_start),
                 ("billing_period_end", bill.billing_period_end)]
        if bill.due_date:
            dates.append(("due_date", bill.due_date))
        issues.extend(_check_date_convention(dates, ctx.raw_text))
    return issues


# ---- delivery note ------------------------------------------------------------------


def validate_delivery_note(note: DeliveryNote, ctx: ValidationContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not note.delivery_note_number.strip():
        issues.append(ValidationIssue(field="delivery_note_number", message="empty delivery note number"))
    if _same_party(note.supplier.name, note.recipient.name):
        issues.append(
            ValidationIssue(field="recipient.name", message="supplier and recipient resolve to the same entity")
        )
    issues.extend(_check_date_range("delivery_date", note.delivery_date, max_years_ahead=1))
    for n, item in enumerate(note.items):
        if item.quantity_delivered < 0:
            issues.append(
                ValidationIssue(field=f"items[{n}].quantity_delivered", message="negative delivered quantity")
            )
        if item.quantity_ordered is not None and item.quantity_delivered > item.quantity_ordered:
            issues.append(
                ValidationIssue(
                    field=f"items[{n}].quantity_delivered",
                    message=(
                        f"{item.description!r}: delivered {item.quantity_delivered:g} exceeds "
                        f"ordered {item.quantity_ordered:g}"
                    ),
                    severity="warning",
                )
            )
    if not note.items:
        issues.append(ValidationIssue(field="items", message="no delivered items listed", severity="warning"))
    if ctx.raw_text is not None:
        issues.extend(_check_date_convention([("delivery_date", note.delivery_date)], ctx.raw_text))
    return issues


# ---- certificate of origin ------------------------------------------------------------


def validate_certificate_of_origin(cert: CertificateOfOrigin, ctx: ValidationContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not cert.certificate_number.strip():
        issues.append(ValidationIssue(field="certificate_number", message="empty certificate number"))
    if _same_party(cert.exporter.name, cert.consignee.name):
        issues.append(
            ValidationIssue(field="consignee.name", message="exporter and consignee resolve to the same entity")
        )
    issues.extend(_check_date_range("issue_date", cert.issue_date, max_years_ahead=0))
    if not cert.issuing_authority.strip():
        issues.append(ValidationIssue(field="issuing_authority", message="no certifying authority stated"))
    origin = cert.country_of_origin.strip()
    if not origin:
        issues.append(ValidationIssue(field="country_of_origin", message="empty country of origin"))
    for n, item in enumerate(cert.goods):
        if item.hs_code:
            digits = re.sub(r"[\s.]", "", item.hs_code)
            if not _HS_RE.match(digits):
                issues.append(
                    ValidationIssue(
                        field=f"goods[{n}].hs_code",
                        message=f"{item.hs_code!r} is not a 6-, 8- or 10-digit HS code",
                        severity="warning",
                    )
                )
    if not cert.goods:
        issues.append(ValidationIssue(field="goods", message="no goods described"))
    return issues


# ---- identity document ---------------------------------------------------------------

_MRZ_WEIGHTS = (7, 3, 1)


def mrz_check_digit(field: str) -> int:
    """ICAO 9303 check digit: digits as themselves, A–Z as 10–35, '<' as 0,
    weighted 7-3-1 repeating, modulo 10."""
    total = 0
    for i, ch in enumerate(field):
        if ch.isdigit():
            value = int(ch)
        elif "A" <= ch <= "Z":
            value = ord(ch) - 55
        else:
            value = 0
        total += value * _MRZ_WEIGHTS[i % 3]
    return total % 10


def _mrz_fields(lines: list[str]) -> list[tuple[str, str, str]]:
    """(name, data, check digit) for the check-digited fields of a TD3
    (passport, 2×44) or TD1 (ID card, 3×30) machine-readable zone."""
    if len(lines) == 2 and all(len(line) == 44 for line in lines):
        l2 = lines[1]
        fields = [
            ("document_number", l2[0:9], l2[9]),
            ("date_of_birth", l2[13:19], l2[19]),
            ("date_of_expiry", l2[21:27], l2[27]),
        ]
        composite = l2[0:10] + l2[13:20] + l2[21:43]
        return fields + [("composite", composite, l2[43])]
    if len(lines) == 3 and all(len(line) == 30 for line in lines):
        l1, l2 = lines[0], lines[1]
        fields = [
            ("document_number", l1[5:14], l1[14]),
            ("date_of_birth", l2[0:6], l2[6]),
            ("date_of_expiry", l2[8:14], l2[14]),
        ]
        composite = l1[5:30] + l2[0:7] + l2[8:15] + l2[18:29]
        return fields + [("composite", composite, l2[29])]
    return []


def validate_id_document(doc: IdDocument, ctx: ValidationContext) -> list[ValidationIssue]:
    """Consistency of the printed text only — never a claim that the document
    or the person is genuine."""
    issues: list[ValidationIssue] = []
    if not doc.document_number.strip():
        issues.append(ValidationIssue(field="document_number", message="empty document number"))
    today = date.today()
    if doc.date_of_birth > today:
        issues.append(ValidationIssue(field="date_of_birth", message="date of birth is in the future"))
    if doc.date_of_issue and doc.date_of_issue < doc.date_of_birth:
        issues.append(ValidationIssue(field="date_of_issue", message="issued before the holder's date of birth"))
    if doc.date_of_issue and doc.date_of_expiry and doc.date_of_expiry <= doc.date_of_issue:
        issues.append(ValidationIssue(field="date_of_expiry", message="expires on or before its issue date"))
    if doc.date_of_expiry and doc.date_of_expiry < today:
        issues.append(
            ValidationIssue(field="date_of_expiry", message="the document has expired", severity="warning")
        )
    if not _ALPHA_COUNTRY_RE.match(doc.issuing_country.strip().upper()):
        issues.append(
            ValidationIssue(
                field="issuing_country",
                message=f"{doc.issuing_country!r} is not an ISO 3166 alpha-2/alpha-3 code",
                severity="warning",
            )
        )
    if doc.mrz:
        fields = _mrz_fields(doc.mrz)
        if not fields:
            issues.append(
                ValidationIssue(
                    field="mrz",
                    message="MRZ is neither 2×44 (TD3) nor 3×30 (TD1) characters — cannot verify check digits",
                    severity="warning",
                )
            )
        for name, data, digit in fields:
            if not digit.isdigit() or mrz_check_digit(data) != int(digit):
                issues.append(
                    ValidationIssue(
                        field="mrz",
                        message=f"MRZ check digit for {name} does not match — a character was misread or altered",
                    )
                )
        number_in_mrz = fields[0][1].replace("<", "") if fields else None
        if number_in_mrz and number_in_mrz != doc.document_number.replace(" ", "").upper():
            issues.append(
                ValidationIssue(
                    field="document_number",
                    message=f"printed number {doc.document_number!r} differs from the MRZ ({number_in_mrz})",
                )
            )
    return issues


__all__ = [
    "mrz_check_digit",
    "validate_certificate_of_origin",
    "validate_credit_note",
    "validate_delivery_note",
    "validate_id_document",
    "validate_tax_invoice",
    "validate_utility_bill",
]
