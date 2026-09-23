"""Models shared by the pipeline that are not document schemas:
classification and validation results and cross-document matching.
Document schemas live in `docket.catalog`."""
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
