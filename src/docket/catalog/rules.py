"""Business rules for the schemas added in 2.x.

Same principles as `docket.validate`: deterministic, no model calls, report
and never repair. Each rule is a `(document, ValidationContext)` validator
registered on its schema in `builtin.py`.
"""
from __future__ import annotations


from ..schemas import ValidationIssue
from ..validate import (
    _check_date_convention,
    _check_date_range,
    _core_name,
)
from .models import CreditNote, DeliveryNote
from .registry import ValidationContext


def _same_party(a: str, b: str) -> bool:
    return bool(_core_name(a)) and _core_name(a) == _core_name(b)


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


__all__ = [
    "validate_credit_note",
    "validate_delivery_note",
]
