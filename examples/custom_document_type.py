"""Teach docket a document type it doesn't ship with.

    python examples/custom_document_type.py lieferschein.pdf

Once registered, the type goes through the whole pipeline: keyword rules and
the LLM classifier recognise it, the LLM fills your schema, every required
field's page/quote citation is checked against the source, your validators
run, and exporters can target it.
"""
import sys
from datetime import date

from pydantic import BaseModel, Field

from docket import (
    CitedDocument,
    ValidationIssue,
    add_validator,
    ProcessOptions,
    ReviewOptions,
    process_document,
    register_document_type,
)


class DeliveryItem(BaseModel):
    description: str
    quantity: float
    unit: str | None = None


class DeliveryNote(CitedDocument):
    """Field descriptions go into the JSON Schema the LLM sees — write them
    as instructions."""

    note_number: str
    supplier_name: str
    supplier_vat_number: str | None = None
    delivery_date: date
    order_reference: str | None = Field(
        default=None, description="Buyer's purchase order number, if printed"
    )
    items: list[DeliveryItem] = []


def items_present(note: DeliveryNote, _raw_text: str | None):
    if not note.items:
        yield ValidationIssue(field="items", message="no delivered items found")


register_document_type(
    "delivery_note",
    DeliveryNote,
    description="Delivery note / Lieferschein / bon de livraison listing goods handed over",
    keywords=["delivery note", "lieferschein", "bon de livraison", ("packing list", 1.0)],
    validators=[items_present],
)


# Validators can be attached to built-in types too.
def po_required(invoice, _raw_text):
    if not invoice.purchase_order_number:
        yield ValidationIssue(
            field="purchase_order_number",
            message="our AP policy requires a PO number",
            severity="warning",
        )


add_validator("invoice", po_required)

if __name__ == "__main__":
    result = process_document(sys.argv[1], ProcessOptions(review=ReviewOptions(enqueue=False)))
    print(result.document_type, "valid" if result.is_valid else "INVALID")
    print(result.document)
    for issue in result.validation_issues:
        print(f"[{issue.severity}] {issue.field}: {issue.message}")
