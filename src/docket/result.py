"""The one result every entry point returns: `DocumentResult`.

The Python API returns it, the CLI prints it, the HTTP API serves it —
the same model, serialized the same way.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .layout import BoundingBox, DocumentLayout
from .ocr.acquire import AcquisitionReport
from .schemas import ClassificationResult, ValidationIssue


class SourceRegion(BaseModel):
    """One contiguous place a quote was found on a page."""

    page: int = Field(ge=1)
    bbox: BoundingBox
    word_ids: list[str] = Field(default_factory=list)


class SourceLocation(BaseModel):
    """Where an extracted value was read: the quote the model cited, and the
    page region(s) it resolves to.

    `regions` holds every place the quote was found — usually one; a value
    that prints twice gives two, and `status` is then `conflicting` because
    nothing in the document says which occurrence is the source.

    `bbox` and `word_ids` come from matching the quote against the page's
    word boxes, never from the model. A page read by the vision model has no
    boxes; its quote is then looked up in the OCR reading kept as the page's
    witness (`located_by` says which). `bbox` is None when no region
    resolved; the top-level copy is the first region's, `regions` carries
    the rest.
    """

    page: int = Field(ge=1)
    quote: str
    status: Literal[
        "verified", "fuzzy", "conflicting", "unlocated"
    ] = Field(
        default="unlocated",
        description=(
            "verified: exactly one exact match on the page. "
            "fuzzy: no exact match, one close window. "
            "conflicting: several exact matches, ambiguous. "
            "unlocated: the page has no geometry or the quote is not on it."
        ),
    )
    match: Literal["exact", "fuzzy"] | None = Field(
        default=None, description="How the quote was found; None when unlocated."
    )
    regions: list[SourceRegion] = Field(
        default_factory=list,
        description=(
            "Every place the quote was found on the page (usually one), with the "
            "normalized coordinates needed to draw a highlight."
        ),
    )
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
    llm_models: list[str] = Field(default_factory=list, description="Models called, in first-use order.")
    llm_input_tokens: int = Field(default=0, description="Prompt tokens the provider reported.")
    llm_output_tokens: int = Field(default=0, description="Completion tokens the provider reported.")
    llm_unreported_calls: int = Field(
        default=0, description="Calls whose provider reported no token counts; not in the two sums above."
    )
    llm_estimated_tokens: int = Field(default=0, description="Characters / 4 over every call, reported or not.")
    extract_attempts: int = 0
    escalated_to_vlm: bool = Field(
        default=False,
        description="OCR text passed its gate but failed validation, and a vision-model re-read won.",
    )
    template_id: str | None = Field(
        default=None,
        description="Vendor template that read this document (no LLM extraction); None = model extraction.",
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
    corroborated: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Fields another document confirmed, and what confirmed them — e.g. "
            "{'total_amount': 'purchase order PO-9988 total'}. Set by docket.corroborate()."
        ),
    )
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    metrics: ProcessingMetrics = Field(default_factory=ProcessingMetrics)
    error: DocumentError | None = None

    @property
    def is_valid(self) -> bool:
        return self.error is None and not any(
            i.severity == "error" for i in self.validation_issues
        )

    def highlights(self, page: int | None = None) -> list[tuple[str, SourceLocation, SourceRegion]]:
        """(field, source, region) triples for drawing provenance boxes over
        the original pages — everything a viewer needs, straight from the
        result, optionally filtered to one page. Fields without geometry
        (vision-model pages, unresolved quotes) contribute nothing."""
        return [
            (field, source, region)
            for field, source in sorted(self.field_sources.items())
            for region in source.regions
            if page is None or region.page == page
        ]

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
        extraction failed or the schema isn't registered here. A result saved
        under an older schema version is migrated to the registered one."""
        from . import catalog

        if self.schema_id is None or self.extracted is None:
            return None
        spec = catalog.get_schema(self.schema_id, self.schema_version) if self.schema_version else None
        data = self.extracted
        if spec is None:
            spec = catalog.get_schema(self.schema_id)
            if spec is None and ":" in self.schema_id:
                # An unregistered model: its id is its import path.
                try:
                    spec = catalog.adhoc(catalog.load_schema(self.schema_id))
                except catalog.SchemaError:
                    return None
            if spec is None:
                return None
            if self.schema_version and self.schema_version != spec.version:
                try:
                    data = catalog.migrate(self.schema_id, data, self.schema_version, spec.version)
                except catalog.SchemaError:
                    return None
        try:
            return spec.model.model_validate(data)
        except ValidationError:
            return None


__all__ = [
    "DocumentError",
    "DocumentResult",
    "DocumentStatus",
    "ProcessingMetrics",
    "SourceLocation",
    "SourceRegion",
]
