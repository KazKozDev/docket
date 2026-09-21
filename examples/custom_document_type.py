"""Teach docket a document type it doesn't ship with.

    python examples/custom_document_type.py strafzettel.pdf

A registered schema goes through the whole pipeline: the keyword rules, the
TF-IDF tier (if you give it example sentences) and the LLM classifier
recognise it, the LLM fills your model, every cited field's page/quote is
checked against the source and resolved to a box on the page, your
validators run, and exporters can target it.

To use a model once without registering it, pass it directly:
`ProcessOptions(schema_model=ParkingTicket)` or
`docket process file.pdf --schema my_module:ParkingTicket`.
"""
import sys
from datetime import date

from pydantic import Field

from docket import (
    CitedDocument,
    Party,
    ProcessOptions,
    ReviewOptions,
    SchemaSpec,
    ValidationIssue,
    add_validator,
    keywords,
    process_document,
    register_schema,
)


class ParkingTicket(CitedDocument):
    """Field descriptions go into the JSON Schema the LLM sees — write them
    as instructions."""

    ticket_number: str
    issuing_authority: Party = Field(description="The municipality or police office that issued it.")
    issue_date: date
    plate: str | None = Field(default=None, description="Vehicle registration plate, as printed.")
    offence: str | None = None
    fine: float
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    pay_by: date | None = None


def pay_by_after_issue(ticket: ParkingTicket, ctx):
    if ticket.pay_by and ticket.pay_by < ticket.issue_date:
        yield ValidationIssue(field="pay_by", message="payment deadline is before the issue date")


register_schema(
    SchemaSpec(
        schema_id="parking_ticket",
        version="1.0",
        display_name="Parking ticket",
        status="experimental",
        model=ParkingTicket,
        description="Parking ticket / Strafzettel / avis de contravention for a parking offence",
        keywords=keywords("parking ticket", "penalty charge notice", "strafzettel", "verwarnungsgeld",
                          "avis de contravention", "multa de aparcamiento"),
        # Optional: sentences in your own words let the TF-IDF tier learn the type too.
        examples=(
            "Your vehicle was parked without a valid ticket; the fine is payable within 14 days.",
            "Ihr Fahrzeug parkte im Halteverbot; das Verwarnungsgeld ist binnen einer Woche zu zahlen.",
            "Stationnement gênant constaté par l'agent, amende forfaitaire à régler sous 45 jours.",
        ),
        cited_fields=("ticket_number", "issuing_authority.name", "issue_date", "fine"),
        validators=(pay_by_after_issue,),
    )
)


# Validators can be attached to built-in schemas too.
def po_required(invoice, ctx):
    if not invoice.purchase_order_number:
        yield ValidationIssue(
            field="references", message="our AP policy requires a PO number", severity="warning"
        )


add_validator("invoice", po_required)

if __name__ == "__main__":
    result = process_document(sys.argv[1], ProcessOptions(review=ReviewOptions(enqueue=False)))
    print(result.document_type, result.schema_version, "valid" if result.is_valid else "INVALID")
    print(result.document)
    for issue in result.validation_issues:
        print(f"[{issue.severity}] {issue.field}: {issue.message}")
