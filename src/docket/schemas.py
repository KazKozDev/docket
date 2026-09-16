"""Pydantic schemas for the document types docket knows how to extract.

Each schema doubles as the contract handed to the LLM (via
`model_json_schema()`) and as the validator that catches malformed
extractions before they reach `validate.py`'s business-rule checks.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class DocType(str, Enum):
    INVOICE = "invoice"
    RECEIPT = "receipt"
    CONTRACT = "contract"
    BOARDING_PASS = "boarding_pass"
    UNKNOWN = "unknown"


class LineItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float
    total: float


class SourceLocation(BaseModel):
    """Auditable link from a field to the original page region."""

    page: int = Field(ge=1)
    quote: str = Field(min_length=1, description="Exact text copied from the source page.")


class Invoice(BaseModel):
    doc_type: DocType = DocType.INVOICE
    invoice_number: str
    issue_date: date
    due_date: date | None = None
    vendor_name: str
    vendor_tax_id: str | None = None
    vendor_iban: str | None = Field(
        default=None, description="Vendor's bank account IBAN, if the document states one."
    )
    vendor_vat_number: str | None = Field(
        default=None, description="EU-style VAT registration number (e.g. DE123456789), if present."
    )
    customer_name: str
    currency: str = Field(default="USD", min_length=3, max_length=3)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0
    discount_amount: float = Field(
        default=0, description="Positive magnitude of any discount; subtracted from subtotal+tax to get total_amount."
    )
    shipping_amount: float = Field(
        default=0, description="Positive shipping/handling charge; added to subtotal+tax to get total_amount."
    )
    total_amount: float
    field_locations: dict[str, SourceLocation] = Field(
        default_factory=dict,
        description="Page and exact source region for each material extracted field.",
    )

    @field_validator("discount_amount")
    @classmethod
    def _discount_is_a_magnitude(cls, v: float) -> float:
        """Invoices write discounts in accounting parentheses — ($371.00) —
        which a model faithfully transcribes as -371.0. The sign is notation,
        not data: the schema's contract is a positive magnitude that gets
        subtracted. Normalizing here means the convention can't be violated,
        rather than being documented and hoped for.
        """
        return abs(v)


class ReceiptItem(BaseModel):
    description: str
    price: float


class Receipt(BaseModel):
    doc_type: DocType = DocType.RECEIPT
    merchant_name: str
    transaction_date: date
    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal: float = Field(
        default=0,
        description="Pre-tax subtotal, if the receipt prints one. 0 if it doesn't.",
    )
    tax_amount: float = Field(
        default=0,
        description="Sales tax charged on top of the subtotal. 0 if the receipt shows none.",
    )
    total_amount: float
    payment_method: str | None = None
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class Contract(BaseModel):
    doc_type: DocType = DocType.CONTRACT
    contract_title: str
    # Lists, not strings: a real contract in the eval corpus has two
    # counterparties on one side ("...TELCOSTAR PTE, LTD. ... and Ability
    # Computer & Software Industries Ltd (each and both of them
    # 'Recipient')"). With a single string the model had no way to answer
    # except by concatenating them into a name that appears nowhere.
    parties_a: list[str] = Field(
        description="Every entity on the first side, one per element. Never join names with 'and'."
    )
    parties_b: list[str] = Field(
        description="Every entity on the second side, one per element. Never join names with 'and'."
    )
    effective_date: date
    expiration_date: date | None = None
    governing_law: str | None = None
    key_obligations: list[str] = Field(default_factory=list)
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class BoardingPass(BaseModel):
    """Airline travel document. Unlike an invoice it has no arithmetic to
    check, but almost every field is drawn from a controlled vocabulary —
    IATA codes, a carrier-prefixed flight number, a six-character booking
    reference — so the validation leans on format rules instead of sums.
    """

    doc_type: DocType = DocType.BOARDING_PASS
    passenger_name: str
    booking_reference: str = Field(
        description="Six-character PNR / record locator, e.g. 'X4H2QP'."
    )
    flight_number: str = Field(description="Carrier code plus number, e.g. 'IB3241' or 'BA475'.")
    departure_airport: str = Field(description="Three-letter IATA code of the origin, e.g. 'BCN'.")
    arrival_airport: str = Field(description="Three-letter IATA code of the destination, e.g. 'LHR'.")
    departure_datetime: datetime
    boarding_time: str | None = None
    seat: str | None = None
    gate: str | None = None
    cabin_class: str | None = None
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


SCHEMA_BY_DOC_TYPE: dict[DocType, type[BaseModel]] = {
    DocType.INVOICE: Invoice,
    DocType.RECEIPT: Receipt,
    DocType.CONTRACT: Contract,
    DocType.BOARDING_PASS: BoardingPass,
}


class ClassificationResult(BaseModel):
    doc_type: DocType
    confidence: float
    method: str  # "rules" | "tfidf" | "llm" | "unavailable"
    scores: dict[str, float] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


class PipelineResult(BaseModel):
    source: str
    classification: ClassificationResult
    extracted: dict | None
    field_sources: dict[str, SourceLocation] = Field(
        default_factory=dict,
        description="Where each extracted field was read from — kept beside the data, not inside it.",
    )
    extract_attempts: int
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    ocr_method: str  # "pdf_text" | "ocr" | "vlm" | "ocr_degraded"
    raw_text_chars: int
    language: str = "unknown"  # "en" | "es" | "unknown"
    escalated_to_vlm: bool = False
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    llm_calls: int = 0
    llm_estimated_tokens: int = 0
    pages_total: int = 1
    pages_processed: int = 1
    complete: bool = True
    document_id: str | None = None
    page_methods: list[str] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == "error" for i in self.validation_issues)
