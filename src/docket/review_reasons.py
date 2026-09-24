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
    reasons.extend(_weakly_read_fields(result))
    return reasons


def _weakly_read_fields(result: DocumentResult) -> list[str]:
    """Key fields whose cited words OCR itself was unsure of.

    Every other check compares the extraction with the OCR text. When that
    text is wrong — a 3 read as 8 on a faded thermal slip — the quote, the
    value and the arithmetic can all agree with each other and still be
    wrong. The recognizer's own confidence in those words is the one signal
    that looks past the text, so a low one on a field that must be right
    goes to a human instead of out as a success.
    """
    from . import catalog

    spec = catalog.get_schema(result.schema_id) if result.schema_id else None
    if spec is None:
        return []
    floor = config.MIN_SOURCE_CONFIDENCE
    reasons = []
    for field in spec.required_citations:
        sources = [s for key, s in result.field_sources.items() if key == field or key.startswith(f"{field}[")]
        weakest = min((s.confidence for s in sources if s.confidence is not None), default=None)
        if weakest is not None and weakest < floor:
            reasons.append(
                f"{field} was read from words OCR recognised with low confidence ({weakest:.2f} < {floor:.2f})"
            )
    return reasons


__all__ = ["reasons_for"]
