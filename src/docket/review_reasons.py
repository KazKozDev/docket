"""Why a result needs a human: the rules behind `needs_review`.

Separate from review_queue.py, which stores flagged documents and needs the
[review] extra (SQLAlchemy); deciding *whether* to flag needs nothing.
"""
from __future__ import annotations

from datetime import date

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
    """Key fields whose value OCR itself was unsure of.

    Every other check compares the extraction with the OCR text. When that
    text is wrong — a 3 read as 8 on a faded thermal slip — the quote, the
    value and the arithmetic can all agree with each other and still be
    wrong. The recognizer's own confidence in those words is the one signal
    that looks past the text, so a low one on a field that must be right
    goes to a human instead of out as a success — unless something
    independent of that reading already confirms the value: amounts that
    reconcile across separately printed lines, or another document
    (`docket.corroborate`).
    """
    floor = config.MIN_SOURCE_CONFIDENCE
    confirmed = _reconciled_amounts(result) | set(result.corroborated)
    return [
        low_confidence_reason(field, weakest, floor)
        for field, weakest in key_field_confidence(result).items()
        if weakest is not None and weakest < floor and field not in confirmed
    ]


_LOW_CONFIDENCE = "{field} was read from words OCR recognised with low confidence"


def low_confidence_reason(field: str, confidence: float, floor: float) -> str:
    return _LOW_CONFIDENCE.format(field=field) + f" ({confidence:.2f} < {floor:.2f})"


def is_low_confidence_reason(reason: str, field: str) -> bool:
    return reason.startswith(_LOW_CONFIDENCE.format(field=field) + " (")


def key_field_confidence(result: DocumentResult) -> dict[str, float | None]:
    """For each cited key field whose value carries digits — amounts, dates,
    document numbers — the lowest OCR confidence among the words holding
    those digits (a list field counts each element). None when nothing
    reports one, e.g. a PDF text layer.

    Only the value's own words count: in "Total amount due: 1 815,00" the
    label says nothing about whether the amount was read right. Names are
    left out — a letter misread in a merchant's name changes no amount, and
    a name is matched fuzzily downstream anyway.
    """
    from . import catalog
    from .validate import value_at

    spec = catalog.get_schema(result.schema_id) if result.schema_id else None
    document = result.document
    if spec is None or document is None:
        return {}
    words = {w.id: w for page in (result.layout.pages if result.layout else []) for w in page.words}
    confidences: dict[str, float | None] = {}
    for field in spec.required_citations:
        value = value_at(document, field)
        is_name = isinstance(value, str) and not field.endswith("_number")
        if value is None or is_name or isinstance(value, (list, bool)):
            continue
        sources = [s for key, s in result.field_sources.items() if key == field or key.startswith(f"{field}[")]
        if not sources:
            continue
        forms = _digit_forms(value)
        readings = []
        for source in sources:
            cited = [words[i] for i in source.word_ids if i in words and words[i].confidence is not None]
            digit_words = [w for w in cited if _digits(w.text)]
            # A line can hold two values ("Invoice INV-7 date 2026-03-01");
            # each field answers for the words whose digits are its own.
            own = [w for w in digit_words if any(_digits(w.text) in form for form in forms)] or digit_words
            scores = [w.confidence for w in own if w.confidence is not None]
            readings.append(min(scores) if scores else source.confidence)
        confidences[field] = min((c for c in readings if c is not None), default=None)
    return confidences


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def _digit_forms(value) -> list[str]:
    """The digit sequences a value can be printed as."""
    if isinstance(value, date):
        y, m, d = value.year, value.month, value.day
        return [
            f"{y:04}{m:02}{d:02}", f"{d:02}{m:02}{y:04}", f"{m:02}{d:02}{y:04}",
            f"{d:02}{m:02}{y % 100:02}", f"{m:02}{d:02}{y % 100:02}", f"{d}{m}{y}", f"{m}{d}{y}",
        ]
    if isinstance(value, (int, float)):
        return [_digits(f"{abs(value):.2f}")]
    return [_digits(str(value))]


def _reconciled_amounts(result: DocumentResult) -> set[str]:
    """Amounts confirmed by arithmetic across separately cited lines.

    A misread digit in one amount breaks the sum it belongs to; for
    subtotal + tax = total to hold to the cent, the same misreading would
    have to happen consistently on different lines. So when the totals
    reconcile and were cited from different lines, OCR's doubt about one of
    them is already answered.
    """
    from . import catalog
    from .validate import value_at

    document = result.document
    spec = catalog.get_schema(result.schema_id) if result.schema_id else None
    if document is None or spec is None:
        return set()

    def amount(name: str) -> float:
        value = getattr(document, name, None)
        return value if isinstance(value, (int, float)) else 0.0

    def quote(name: str) -> str | None:
        source = result.field_sources.get(name)
        return source.quote.strip() if source and source.quote.strip() else None

    reconciled: set[str] = set()
    total, subtotal = amount("total_amount"), amount("subtotal")
    if total and subtotal and quote("total_amount") and quote("subtotal") and quote("total_amount") != quote("subtotal"):
        derived = subtotal + amount("tax_amount") + amount("shipping_amount") - amount("discount_amount")
        if abs(derived - total) <= 0.01:
            reconciled |= {"subtotal", "total_amount"}
    if spec.line_items is not None and "total" in spec.line_items.columns:
        rows = value_at(document, spec.line_items.path) or []
        column = spec.line_items.columns["total"]
        row_totals = [getattr(row, column, None) for row in rows]
        numeric = [float(t) for t in row_totals if isinstance(t, (int, float))]
        if len(numeric) >= 2 and len(numeric) == len(row_totals):
            for name in ("subtotal", "total_amount"):
                if amount(name) and abs(sum(numeric) - amount(name)) <= 0.01:
                    reconciled.add(name)
    return reconciled


__all__ = ["is_low_confidence_reason", "key_field_confidence", "low_confidence_reason", "reasons_for"]
