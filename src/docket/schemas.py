"""Models shared by the pipeline that are not document schemas:
classification and validation results, cross-document matching and
forensics. Document schemas live in `docket.catalog`."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MatchingStatus(str, Enum):
    MATCHED = "matched"
    DISCREPANCY = "discrepancy"
    UNMATCHED = "unmatched"


class DiscrepancyType(str, Enum):
    PRICE_VARIANCE = "price_variance"
    QUANTITY_OVERBILLING = "quantity_overbilling"
    UNORDERED_ITEM = "unordered_item"
    TOTAL_MISMATCH = "total_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    DATE_OUT_OF_BOUNDS = "date_out_of_bounds"
    CURRENCY_MISMATCH = "currency_mismatch"
    PARTY_MISMATCH = "party_mismatch"
    PO_NUMBER_MISMATCH = "po_number_mismatch"
    UNFULFILLED_BILLING = "unfulfilled_billing"


class Discrepancy(BaseModel):
    type: DiscrepancyType
    field: str
    expected: Any
    actual: Any
    difference: float | None = None
    severity: str = "error"  # "error" | "warning"
    message: str


class MatchResult(BaseModel):
    status: MatchingStatus
    matched_amount: float = 0.0
    variance_amount: float = 0.0
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_matched(self) -> bool:
        return self.status == MatchingStatus.MATCHED and not any(
            d.severity == "error" for d in self.discrepancies
        )


class BankTransaction(BaseModel):
    transaction_date: date
    amount: float
    currency: str = Field(default="USD", min_length=3, max_length=3)
    description: str = ""
    card_last_four: str | None = None
    transaction_id: str | None = None


class ClassificationResult(BaseModel):
    doc_type: str = Field(description="A registered schema id, or 'unknown'.")
    confidence: float
    method: str  # "rules" | "tfidf" | "llm" | "unavailable"
    scores: dict[str, float] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


class StampDetection(BaseModel):
    """Detection of a physical seal, official rubber stamp, or organization stamp."""

    page: int = Field(ge=1, description="1-indexed page number where stamp was found.")
    box: tuple[float, float, float, float] = Field(
        description="Normalized bounding box (ymin, xmin, ymax, xmax) from 0.0 to 1.0."
    )
    color: str = Field(
        default="blue",
        description="Dominant ink color (e.g. blue, violet, red, black).",
    )
    shape: str = Field(
        default="circular",
        description="Geometry of the stamp (circular, oval, rectangular, irregular).",
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Detection confidence score."
    )
    text: str | None = Field(
        default=None,
        description="Decoded text or organization name from stamp, if legible.",
    )


class SignatureDetection(BaseModel):
    """Detection of a handwritten signature, mark, or cursive endorsement."""

    page: int = Field(
        ge=1, description="1-indexed page number where signature was found."
    )
    box: tuple[float, float, float, float] = Field(
        description="Normalized bounding box (ymin, xmin, ymax, xmax) from 0.0 to 1.0."
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Signature detection confidence score."
    )
    signatory_label: str | None = Field(
        default=None,
        description="Contextual role (e.g. Customer, Contractor, Director) if labeled.",
    )


class HandwrittenAnnotation(BaseModel):
    """Detection of handwritten marginalia, numeric corrections, or status stamps."""

    page: int = Field(ge=1, description="1-indexed page number.")
    text: str = Field(description="Transcribed or recognized text.")
    annotation_type: str = Field(
        description="Classification (e.g. payment_stamp, price_correction, approval, marginalia)."
    )
    box: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalized bounding box if isolated."
    )


class DocumentForensicReport(BaseModel):
    """Forensic report covering physical execution, stamps, signatures, and alterations."""

    has_signatures: bool = Field(
        default=False,
        description="Whether at least one handwritten signature is present.",
    )
    has_stamps: bool = Field(
        default=False,
        description="Whether at least one seal or official stamp is present.",
    )
    is_executed: bool = Field(
        default=False,
        description="Whether the document appears legally executed (signed/stamped).",
    )
    is_empty_template: bool = Field(
        default=False,
        description="Whether the document appears to be an unexecuted blank template.",
    )
    stamps: list[StampDetection] = Field(default_factory=list)
    signatures: list[SignatureDetection] = Field(default_factory=list)
    annotations: list[HandwrittenAnnotation] = Field(default_factory=list)
    alterations_detected: bool = Field(
        default=False,
        description="Whether handwritten overrides or crossed-out amounts were detected.",
    )
    risk_flags: list[str] = Field(
        default_factory=list,
        description="Forensic risk flags (e.g. UNEXECUTED_TEMPLATE, MISSING_STAMP, HANDWRITTEN_ALTERATION).",
    )
