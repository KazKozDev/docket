"""Orchestrates one document: acquire → classify → extract → validate → review.

`process_document` is the single entry point the CLI, the HTTP API and
library users share. It returns a `DocumentResult` for every document it can
open — a document that fails mid-way comes back with `status="failed"` and a
structured `error`, not an exception. Only configuration mistakes (an OCR
backend that cannot run here, an unknown language code) raise, and they do
so before any page is read.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from . import config, doctypes, review_queue
from .classify import classify
from .extract import extract_pages
from .language import detect_language
from .layout import locate_quote
from .llm_client import usage as llm_usage
from .logging_setup import get_logger, log_stage
from .ocr import (
    Acquisition,
    AcquisitionError,
    AcquisitionOptions,
    OcrBackend,
    OcrSettings,
    UnsupportedDocument,
    acquire,
    parse_languages,
    resolve_chain,
)
from .ocr_quality import looks_garbled
from .result import (
    DocumentError,
    DocumentResult,
    DocumentStatus,
    ProcessingMetrics,
    SourceLocation,
)
from .schemas import ValidationIssue
from .validate import validate

StageCallback = Callable[[str, Any], None]
log = get_logger()

# Page backends whose reading is plain text with no engine behind it to
# second-guess: re-reading them with the vision model gains nothing.
_NOT_OCR = {"pdf_text", "text", "none"}


def _notify(on_stage: StageCallback | None, stage: str, payload: object) -> None:
    if on_stage is not None:
        on_stage(stage, payload)


def _error_count(result: DocumentResult) -> int:
    return sum(1 for i in result.validation_issues if i.severity == "error")


def _document_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return f"doc_{digest.hexdigest()[:20]}"


def ocr_options(
    *,
    backend: str | OcrBackend | None = None,
    fallbacks: Sequence[str | OcrBackend] | None = None,
    languages: Sequence[str] | str | None = None,
) -> AcquisitionOptions:
    """Acquisition options from explicit arguments, falling back to the
    environment. Validates everything it can without reading a page."""
    settings = OcrSettings(
        languages=parse_languages(languages if languages is not None else config.OCR_LANGUAGES),
        device=config.PADDLE_DEVICE,
        dpi=config.OCR_DPI,
        detect_rotation=config.OCR_DETECT_ROTATION,
        tesseract_psm=config.TESSERACT_PSM,
        paddle_model=config.PADDLE_MODEL,
        paddle_tables=config.PADDLE_TABLES,
    )
    return AcquisitionOptions(
        backend=backend if backend is not None else config.OCR_BACKEND,
        fallbacks=list(fallbacks) if fallbacks is not None else list(config.OCR_FALLBACKS),
        min_confidence=config.OCR_MIN_CONFIDENCE,
        settings=settings,
        max_pages=config.MAX_PDF_PAGES,
    )


def _escalated(options: AcquisitionOptions) -> AcquisitionOptions:
    return options.model_copy(update={"escalate": True})


def _uses_ocr(acquisition: Acquisition) -> bool:
    """Some page's accepted text came from an OCR engine (not a text layer,
    not already the vision model)."""
    return any(
        p.backend not in _NOT_OCR and p.has_geometry for p in acquisition.layout.pages
    )


def _resolve_sources(extracted: dict, acquisition: Acquisition) -> dict[str, SourceLocation]:
    layout = acquisition.layout
    witnesses = {p.page: p.witness for p in acquisition.report.pages if p.witness is not None}
    citations = extracted.pop("field_locations", None) or {}
    sources: dict[str, SourceLocation] = {}
    for field, citation in citations.items():
        page_number, quote = citation.get("page"), citation.get("quote") or ""
        if not isinstance(page_number, int) or page_number < 1:
            continue
        located, located_by = None, None
        for candidate in (layout.page(page_number), witnesses.get(page_number)):
            if candidate is not None and candidate.has_geometry:
                located = locate_quote(quote, candidate)
                if located is not None:
                    located_by = candidate.backend
                    break
        sources[field] = SourceLocation(
            page=page_number,
            quote=quote,
            bbox=located.bbox if located else None,
            word_ids=located.word_ids if located else [],
            confidence=located.confidence if located else None,
            located_by=located_by,
        )
    return sources


def _run_once(
    path: Path,
    document_id: str,
    acquisition: Acquisition,
    *,
    on_stage: StageCallback | None,
    stage_seconds: dict[str, float],
) -> DocumentResult:
    """Classify, extract and validate one acquired reading. No review queue
    side effects — `process_document` decides what to do with the outcome."""
    doc = {"document": path.name}
    layout = acquisition.layout
    text = layout.text

    started = time.monotonic()
    with log_stage(log, "classify", **doc):
        classification = classify(text)
    stage_seconds["classify"] = stage_seconds.get("classify", 0.0) + time.monotonic() - started
    log.info(
        "document classified",
        extra={
            **doc,
            "doc_type": classification.type_name,
            "method": classification.method,
            "confidence": classification.confidence,
        },
    )
    _notify(on_stage, "classify", classification)

    language, _ = detect_language(text)
    common = {
        "source": str(path),
        "document_id": document_id,
        "status": DocumentStatus.SUCCEEDED,
        "classification": classification,
        "document_type": classification.type_name,
        "language": language,
        "ocr": acquisition.report,
        "layout": layout,
    }

    doc_type = doctypes.get_document_type(classification.doc_type)
    if doc_type is None:
        _notify(on_stage, "extract", None)
        return DocumentResult(
            **common,
            validation_issues=[
                ValidationIssue(
                    field="doc_type",
                    message=f"unrecognized document type: {classification.type_name}",
                )
            ],
        )
    common["schema_id"] = doc_type.name

    started = time.monotonic()
    with log_stage(log, "extract", **doc, schema=doc_type.schema.__name__):
        instance, attempts = extract_pages(layout.page_texts, doc_type.schema)
    stage_seconds["extract"] = stage_seconds.get("extract", 0.0) + time.monotonic() - started
    _notify(on_stage, "extract", instance)

    # A vision-model transcript no confident OCR reading backs is unconfirmed,
    # even when self-consistent — the model has been observed inventing
    # digits to force totals to reconcile.
    unbacked = [
        p for p in layout.pages if not p.has_geometry and p.backend not in _NOT_OCR
    ]
    unconfirmed = bool(unbacked) and not any(acquisition.witness_numbers)
    started = time.monotonic()
    with log_stage(log, "validate", **doc):
        if instance is None:
            issues = [
                ValidationIssue(
                    field="*",
                    message="extraction failed to produce valid structured output",
                )
            ]
        else:
            issues = validate(
                instance,
                text,
                pages=layout.page_texts,
                witness_pages=acquisition.witness_pages,
                vlm_unconfirmed=unconfirmed,
            )
    stage_seconds["validate"] = stage_seconds.get("validate", 0.0) + time.monotonic() - started

    # Citations are how validation checks the extraction, not something the
    # document says. They move beside the data, resolved to page regions.
    extracted = instance.model_dump(mode="json") if instance else None
    sources = _resolve_sources(extracted, acquisition) if extracted else {}
    return DocumentResult(
        **common,
        extracted=extracted,
        field_sources=sources,
        validation_issues=issues,
        metrics=ProcessingMetrics(extract_attempts=attempts),
    )


def _acquire(
    path: Path, options: AcquisitionOptions, stage_seconds: dict[str, float]
) -> Acquisition:
    started = time.monotonic()
    try:
        return acquire(path, options)
    finally:
        stage_seconds["acquire"] = stage_seconds.get("acquire", 0.0) + time.monotonic() - started


def _failed(path: Path, document_id: str, stage: str, code: str, exc: Exception) -> DocumentResult:
    return DocumentResult(
        source=str(path),
        document_id=document_id,
        status=DocumentStatus.FAILED,
        error=DocumentError(code=code, stage=stage, message=str(exc)),
        needs_review=True,
        review_reasons=[f"{stage} failed: {exc}"],
    )


def process_document(
    source: str | Path,
    *,
    ocr_backend: str | OcrBackend | None = None,
    ocr_fallbacks: Sequence[str | OcrBackend] | None = None,
    ocr_languages: Sequence[str] | str | None = None,
    on_stage: StageCallback | None = None,
    enqueue_review: bool | None = None,
) -> DocumentResult:
    """Process one document into a DocumentResult.

    ocr_backend:    primary OCR engine — a registered name ("tesseract",
                    "paddle", ...), "auto", or an OcrBackend instance.
                    Default: DOCKET_OCR_BACKEND.
    ocr_fallbacks:  engines tried in order when a page's reading is rejected.
                    Default: DOCKET_OCR_FALLBACKS ("vlm").
    ocr_languages:  ISO 639-1 codes. Default: DOCKET_OCR_LANGUAGES.
    on_stage:       `on_stage(stage, payload)` after "acquire", "classify",
                    "extract" and "validate".
    enqueue_review: append documents that need a human to the review queue
                    (default: DOCKET_REVIEW_QUEUE_ENABLED). Applications with
                    their own workflow pass False and read `needs_review`.

    If an OCR reading passes its confidence gate but the extraction then
    fails validation, the document is re-read with the last backend in the
    chain (the vision model by default) and the better result wins. The
    confidence score alone is a poor gate: Tesseract scored 77.5 on an
    invoice where it read "$530.00" as "$830.00" and dropped the grand-total
    line. Validation checks the meaning of the output, and the re-read is
    only paid for on documents already known to be broken.
    """
    path = Path(source)
    options = ocr_options(backend=ocr_backend, fallbacks=ocr_fallbacks, languages=ocr_languages)
    primary, fallbacks = resolve_chain(options)  # configuration errors raise here
    options = options.model_copy(
        update={"backend": primary if primary is not None else "auto", "fallbacks": fallbacks}
    )

    # Re-reading needs somewhere to go: a backend after the primary.
    can_escalate = bool(fallbacks)
    started = time.monotonic()
    llm_usage.reset()
    stage_seconds: dict[str, float] = {}
    document_id = _document_id(path)
    doc = {"document": path.name}

    try:
        with log_stage(log, "acquire", **doc):
            acquisition = _acquire(path, options, stage_seconds)
            # Pre-flight: garbled text is the most expensive thing to hand a
            # model — it reasons far longer trying to reconcile nonsense
            # (measured on one scan: 117s against 52s on a clean transcription
            # of the same page). Judging the text first turns a wasted
            # extraction into a skipped one.
            if can_escalate and _uses_ocr(acquisition) and looks_garbled(acquisition.text):
                log.info("OCR judged unusable, re-reading with the last-resort backend", extra=doc)
                acquisition = _acquire(path, _escalated(options), stage_seconds)
    except UnsupportedDocument as exc:
        return _failed(path, document_id, "acquire", "unsupported_document", exc)
    except AcquisitionError as exc:
        return _failed(path, document_id, "acquire", "no_text", exc)
    _notify(on_stage, "acquire", acquisition)

    result = _run_once(path, document_id, acquisition, on_stage=on_stage, stage_seconds=stage_seconds)
    escalated = False
    if can_escalate and _uses_ocr(acquisition) and _error_count(result) > 0:
        try:
            second = _acquire(path, _escalated(options), stage_seconds)
            retry = _run_once(path, document_id, second, on_stage=on_stage, stage_seconds=stage_seconds)
        except AcquisitionError:
            retry = None
        # Ties go to the re-read: reaching this branch means the OCR text
        # already failed validation, so it has no claim to the benefit of
        # the doubt.
        if retry is not None and _error_count(retry) <= _error_count(result):
            result, escalated = retry, True

    reasons = review_queue.reasons_for(result)
    result = result.model_copy(
        update={
            "needs_review": bool(reasons),
            "review_reasons": reasons,
            "status": DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.SUCCEEDED,
            "metrics": result.metrics.model_copy(
                update={
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stage_seconds": {k: round(v, 3) for k, v in stage_seconds.items()},
                    "pages": len(result.layout.pages) if result.layout else 0,
                    "llm_calls": llm_usage.calls,
                    "llm_estimated_tokens": llm_usage.estimated_tokens,
                    "escalated_to_vlm": escalated,
                }
            ),
        }
    )
    if enqueue_review is None:
        enqueue_review = config.REVIEW_QUEUE_ENABLED
    if result.needs_review and enqueue_review:
        review_queue.enqueue(result, reasons)
    log.info(
        "document processed",
        extra={
            "document": path.name,
            "doc_type": result.document_type,
            "ocr_backends": result.ocr.backends_used if result.ocr else [],
            "escalated_to_vlm": escalated,
            "llm_calls": result.metrics.llm_calls,
            "needs_review": result.needs_review,
            "validation_errors": _error_count(result),
        },
    )
    _notify(on_stage, "validate", result.validation_issues)
    return result


__all__ = ["StageCallback", "ocr_options", "process_document"]
