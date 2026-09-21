"""The one result every entry point returns: `DocumentResult`.

The Python API returns it, the CLI prints it, the HTTP API serves it —
the same model, serialized the same way.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, ValidationError

from .layout import BoundingBox, DocumentLayout
from .ocr.acquire import AcquisitionReport
from .schemas import ClassificationResult, DocumentForensicReport, ValidationIssue


class SourceLocation(BaseModel):
    """Where an extracted value was read: the quote the model cited, and the
    page region it resolves to.

    `bbox` and `word_ids` come from matching the quote against the page's
    word boxes, never from the model. A page read by the vision model has no
    boxes; its quote is then looked up in the OCR reading kept as the page's
    witness (`located_by` says which). They are None / empty when neither has
    the quote.
    """

    page: int = Field(ge=1)
    quote: str
    bbox: BoundingBox | None = None
    word_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Quote match score × mean OCR confidence of the matched words; None when unresolved.",
    )
    located_by: str | None = Field(
        default=None,
        description=(
            "Backend whose word boxes located the quote. Differs from the page's backend "
            "when the page was read by the vision model and the quote was found in the "
            "independent OCR reading kept as its witness."
        ),
    )


class DocumentStatus(str, Enum):
    SUCCEEDED = "succeeded"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class DocumentError(BaseModel):
    """Why processing a document stopped."""

    code: str = Field(description="Stable machine-readable code, e.g. 'ocr_unavailable'.")
    stage: str = Field(description="Pipeline stage that failed: acquire, classify, extract, ...")
    message: str


class ProcessingMetrics(BaseModel):
    elapsed_seconds: float = 0.0
    stage_seconds: dict[str, float] = Field(default_factory=dict)
    pages: int = 0
    llm_calls: int = 0
    llm_estimated_tokens: int = 0
    extract_attempts: int = 0
    escalated_to_vlm: bool = Field(
        default=False,
        description="OCR text passed its gate but failed validation, and a vision-model re-read won.",
    )


class DocumentResult(BaseModel):
    source: str
    document_id: str
    status: DocumentStatus
    document_type: str | None = Field(
        default=None, description="Registered document type name, or 'unknown'."
    )
    schema_id: str | None = None
    schema_version: str | None = None
    classification: ClassificationResult | None = None
    language: str = "unknown"
    ocr: AcquisitionReport | None = Field(
        default=None, description="Which backend read each page, what was tried, and why."
    )
    layout: DocumentLayout | None = None
    extracted: dict | None = None
    field_sources: dict[str, SourceLocation] = Field(
        default_factory=dict,
        description="Where each extracted field was read — kept beside the data, not inside it.",
    )
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    metrics: ProcessingMetrics = Field(default_factory=ProcessingMetrics)
    error: DocumentError | None = None
    forensic_report: DocumentForensicReport | None = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and not any(
            i.severity == "error" for i in self.validation_issues
        )

    @property
    def complete(self) -> bool:
        """Every page yielded text."""
        return (
            self.layout is not None
            and bool(self.layout.pages)
            and all(p.text.strip() for p in self.layout.pages)
        )

    @property
    def document(self):
        """`extracted` as its typed schema (Invoice, Receipt, …), or None if
        extraction failed or no longer matches the schema."""
        from .doctypes import get_document_type

        if self.document_type is None or self.extracted is None:
            return None
        doc_type = get_document_type(self.document_type)
        if doc_type is None:
            return None
        try:
            return doc_type.schema.model_validate(self.extracted)
        except ValidationError:
            return None


__all__ = [
    "DocumentError",
    "DocumentResult",
    "DocumentStatus",
    "ProcessingMetrics",
    "SourceLocation",
]
