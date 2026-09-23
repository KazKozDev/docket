"""Why a result needs a human: the rules behind `needs_review`.

Separate from review_queue.py, which stores flagged documents and needs the
[review] extra (SQLAlchemy); deciding *whether* to flag needs nothing.
"""
from __future__ import annotations

from . import config
from .result import DocumentResult


def reasons_for(result: DocumentResult, *, min_classification_confidence: float | None = None) -> list[str]:
    floor = config.MIN_CLASSIFICATION_CONFIDENCE if min_classification_confidence is None else min_classification_confidence
    reasons: list[str] = []
    if result.error is not None:
        return [f"{result.error.stage} failed: {result.error.message}"]
    if not result.complete:
        empty = [p.page_number for p in (result.layout.pages if result.layout else []) if not p.text.strip()]
        reasons.append(f"incomplete processing (no text on page(s) {', '.join(map(str, empty)) or '?'})")
    if result.classification is not None:
        if result.classification.confidence < floor:
            reasons.append(f"low classification confidence ({result.classification.confidence:.2f} < {floor:.2f})")
        if result.classification.doc_type == "unknown":
            reasons.append("unrecognized document type")
    degraded = result.ocr.degraded_pages if result.ocr else []
    if degraded:
        reasons.append(
            f"text on page(s) {', '.join(map(str, degraded))} came from a reading below "
            "the confidence gate — every backend that should have read it better failed or was unavailable"
        )
    if result.extracted is None:
        reasons.append("extraction failed to produce valid structured output")
    reasons.extend(f"validation error: {i.field} — {i.message}" for i in result.validation_issues if i.severity == "error")
    return reasons


__all__ = ["reasons_for"]
