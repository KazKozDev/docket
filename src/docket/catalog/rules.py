"""Business rules for the credit note.

Same principles as `docket.validate`: deterministic, no model calls, report
and never repair. Each rule is a `(document, ValidationContext)` validator
registered on its schema in `builtin.py`.
"""
from __future__ import annotations

from ..schemas import ValidationIssue
from .models import CreditNote
from .registry import ValidationContext


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


__all__ = [
    "validate_credit_note",
]
