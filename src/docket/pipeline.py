"""One document through the pipeline: acquire → select schema → extract →
validate → review. Export is a separate step on the result.

`process_document` is the single entry point the CLI, the HTTP API and
library users share. It returns a `DocumentResult` for every document it can
open — one that fails mid-way comes back with `status="failed"` and a
structured `error`, not an exception. Only configuration mistakes raise
(`docket.errors.ConfigurationError`), and they do so before any page is read.

The stages are plain functions, usable on their own:

- `acquire` (docket.ocr) — pages to a DocumentLayout, per-page backend chain
- `select_schema` — the caller's type/schema, or classification
- `extract` — schema-constrained LLM extraction
- `validate_extraction` — deterministic business rules and grounding checks
- `review` — why a human should look, and the review queue
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from . import catalog, config, review_queue
from .catalog import SchemaSpec
from .classify import classify
from .extract import extract_pages
from .language import detect_language
from .layout import locate_all
from .llm_client import usage as llm_usage
from .logging_setup import get_logger, log_stage
from .ocr import (
    Acquisition,
    AcquisitionError,
    AcquisitionOptions,
    UnsupportedDocument,
    acquire,
)
from .ocr_quality import looks_garbled
from .options import ProcessOptions, ResolvedOptions, resolve
from .result import (
    DocumentError,
    DocumentResult,
    DocumentStatus,
    ProcessingMetrics,
    SourceLocation,
)
from .schemas import ClassificationResult, ValidationIssue
from .validate import validate

StageCallback = Callable[[str, Any], None]
log = get_logger()

# Page backends whose reading is plain text with no engine behind it to
# second-guess: re-reading them with the vision model gains nothing.
_NOT_OCR = {"pdf_text", "text", "none"}


class _Stages:
    """Wall-clock time per stage, summed over re-reads."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}

    def timed(self, stage: str) -> "_Timer":
        return _Timer(self.seconds, stage)


class _Timer:
    def __init__(self, seconds: dict[str, float], stage: str):
        self.seconds, self.stage = seconds, stage

    def __enter__(self) -> None:
        self.start = time.monotonic()

    def __exit__(self, *exc) -> None:
        self.seconds[self.stage] = self.seconds.get(self.stage, 0.0) + time.monotonic() - self.start


def _notify(on_stage: StageCallback | None, stage: str, payload: object) -> None:
    if on_stage is not None:
        on_stage(stage, payload)


def _error_count(result: DocumentResult) -> int:
    return sum(1 for i in result.validation_issues if i.severity == "error")


def document_id_for(path: Path) -> str:
    """Content hash: the same file always gets the same id."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return f"doc_{digest.hexdigest()[:20]}"


def _uses_ocr(acquisition: Acquisition) -> bool:
    """Some page's accepted text came from an OCR engine (not a text layer,
    not already the vision model)."""
    return any(p.backend not in _NOT_OCR and p.has_geometry for p in acquisition.layout.pages)


# ---- stages ------------------------------------------------------------------


def select_schema(
    acquisition: Acquisition, fixed: SchemaSpec | None
) -> tuple[SchemaSpec | None, ClassificationResult | None]:
    """The schema to extract into. A type or schema the caller fixed wins and
    no classifier runs; otherwise the rules → TF-IDF → LLM cascade decides."""
    if fixed is not None:
        return fixed, None
    classification = classify(acquisition.text)
    return catalog.get_schema(classification.doc_type), classification


def _resolve_sources(extracted: dict, acquisition: Acquisition) -> dict[str, SourceLocation]:
    """Turn the model's {page, quote} citations into page regions: the quote
    is matched against the page's word boxes, or — for a page the vision
    model read — against the OCR reading kept as its witness. Every place
    the quote occurs becomes a region; one exact hit is `verified`, several
    are `conflicting` (the document itself is ambiguous about which
    occurrence is the source), a close-but-inexact window is `fuzzy`."""
    from .result import SourceRegion

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
                located = locate_all(quote, candidate)
                if located:
                    located_by = candidate.backend
                    break
        if not located:
            sources[field] = SourceLocation(page=page_number, quote=quote)
            continue
        status = "conflicting" if len(located) > 1 and located[0].match_score >= 1.0 else (
            "verified" if located[0].match_score >= 1.0 else "fuzzy"
        )
        regions = [SourceRegion(page=page_number, bbox=l.bbox, word_ids=l.word_ids) for l in located]
        sources[field] = SourceLocation(
            page=page_number,
            quote=quote,
            status=status,
            match="exact" if located[0].match_score >= 1.0 else "fuzzy",
            regions=regions,
            bbox=located[0].bbox,
            word_ids=located[0].word_ids,
            confidence=max(l.confidence for l in located),
            located_by=located_by,
        )
    return sources


def extract(
    acquisition: Acquisition, doc_type: SchemaSpec
) -> tuple[BaseModel | None, int]:
    """Fill the schema from the acquired pages. Returns the instance (None if
    the model never produced valid output) and the number of attempts."""
    return extract_pages(acquisition.layout.page_texts, doc_type.model)


def validate_extraction(
    instance: BaseModel | None, acquisition: Acquisition, doc_type: SchemaSpec
) -> list[ValidationIssue]:
    if instance is None:
        return [ValidationIssue(field="*", message="extraction failed to produce valid structured output")]
    layout = acquisition.layout
    # A vision-model transcript no confident OCR reading backs is unconfirmed,
    # even when self-consistent — the model has been observed inventing
    # digits to force totals to reconcile.
    unbacked = [p for p in layout.pages if not p.has_geometry and p.backend not in _NOT_OCR]
    return validate(
        instance,
        pages=layout.page_texts,
        witness_pages=acquisition.witness_pages,
        vlm_unconfirmed=bool(unbacked) and not any(acquisition.witness_numbers),
        spec=doc_type,
    )


def review(result: DocumentResult, options: ResolvedOptions) -> DocumentResult:
    """Decide whether a human must look, and queue the document if so."""
    reasons = review_queue.reasons_for(
        result, min_classification_confidence=options.review.min_classification_confidence
    )
    if result.error is not None:
        status = DocumentStatus.FAILED
    else:
        status = DocumentStatus.NEEDS_REVIEW if reasons else DocumentStatus.SUCCEEDED
    result = result.model_copy(
        update={"needs_review": bool(reasons), "review_reasons": reasons, "status": status}
    )
    if reasons and result.error is None and options.review.enqueue:
        review_queue.enqueue(
            result,
            reasons,
            queue_path=options.review.queue_path,
            documents_dir=options.review.documents_dir,
        )
    return result


# ---- composition ---------------------------------------------------------------


def _read(
    path: Path,
    document_id: str,
    acquisition: Acquisition,
    options: ResolvedOptions,
    stages: _Stages,
    on_stage: StageCallback | None,
) -> DocumentResult:
    """Schema selection, extraction and validation of one acquired reading."""
    doc = {"document": path.name}
    with stages.timed("classify"), log_stage(log, "select_schema", **doc):
        doc_type, classification = select_schema(acquisition, options.document_type)
    _notify(on_stage, "classify", classification)

    language, _ = detect_language(acquisition.text)
    common = {
        "source": str(path),
        "document_id": document_id,
        "status": DocumentStatus.SUCCEEDED,
        "classification": classification,
        "language": language,
        "ocr": acquisition.report,
        "layout": acquisition.layout,
    }
    if doc_type is None:
        name = classification.doc_type if classification else catalog.UNKNOWN
        _notify(on_stage, "extract", None)
        return DocumentResult(
            **common,
            document_type=name,
            validation_issues=[
                ValidationIssue(field="doc_type", message=f"unrecognized document type: {name}")
            ],
        )

    # A vendor template reads what it knows deterministically; the model only
    # runs when no template matches, or when the template's reading doesn't
    # validate clean. A rule that misses costs an LLM call, never a wrong answer.
    from . import templates as vendor_templates

    template = vendor_templates.match_vendor_template(acquisition.text, doc_type.schema_id)
    template_id: str | None = None
    instance, attempts = None, 0
    if template is not None:
        with stages.timed("template"), log_stage(log, "template", **doc, template=template.template_id):
            candidate = vendor_templates.extract_with_template(acquisition.layout, doc_type, template)
        if candidate is not None:
            candidate_issues = validate_extraction(candidate, acquisition, doc_type)
            if not any(i.severity == "error" for i in candidate_issues):
                instance, template_id = candidate, template.template_id
    if instance is None:
        with stages.timed("extract"), log_stage(log, "extract", **doc, schema=doc_type.schema_id):
            instance, attempts = extract(acquisition, doc_type)
    _notify(on_stage, "extract", instance)

    with stages.timed("validate"), log_stage(log, "validate", **doc):
        issues = validate_extraction(instance, acquisition, doc_type)

    # Citations are how validation checks the extraction, not something the
    # document says. They move beside the data, resolved to page regions.
    extracted = instance.model_dump(mode="json") if instance else None
    return DocumentResult(
        **common,
        document_type=doc_type.schema_id,
        schema_id=doc_type.schema_id,
        schema_version=doc_type.version,
        extracted=extracted,
        field_sources=_resolve_sources(extracted, acquisition) if extracted else {},
        validation_issues=issues,
        metrics=ProcessingMetrics(extract_attempts=attempts, template_id=template_id),
    )


def _acquire(path: Path, options: AcquisitionOptions, stages: _Stages) -> Acquisition:
    with stages.timed("acquire"):
        return acquire(path, options)


def _escalated(options: AcquisitionOptions) -> AcquisitionOptions:
    return options.model_copy(update={"escalate": True})


def _failed(path: Path, document_id: str, stage: str, code: str, exc: Exception) -> DocumentResult:
    return DocumentResult(
        source=str(path),
        document_id=document_id,
        status=DocumentStatus.FAILED,
        error=DocumentError(code=code, stage=stage, message=str(exc)),
    )


def _slim(result: DocumentResult) -> DocumentResult:
    """Drop page layouts and OCR witnesses; field locations stay."""
    report = result.ocr
    if report is not None:
        report = report.model_copy(
            update={"pages": [p.model_copy(update={"witness": None}) for p in report.pages]}
        )
    return result.model_copy(update={"layout": None, "ocr": report})


def process_document(
    source: str | Path,
    options: ProcessOptions | ResolvedOptions | None = None,
    *,
    on_stage: StageCallback | None = None,
) -> DocumentResult:
    """Process one document into a DocumentResult.

    `options` (docket.ProcessOptions) picks the OCR chain, fixes the document
    type or schema (skipping classification), trims the layout from the
    result and configures review; anything left unset comes from the
    DOCKET_* environment. `on_stage(stage, payload)` fires after "acquire",
    "classify", "extract" and "validate".

    If an OCR reading passes its confidence gate but the extraction then
    fails validation, the document is re-read with the last backend in the
    chain (the vision model by default) and the better result wins. The
    confidence score alone is a poor gate: Tesseract scored 77.5 on an
    invoice where it read "$530.00" as "$830.00" and dropped the grand-total
    line. Validation checks the meaning of the output, and the re-read is
    only paid for on documents already known to be broken.
    """
    resolved = options if isinstance(options, ResolvedOptions) else resolve(options)
    path = Path(source)
    doc = {"document": path.name}
    stages = _Stages()
    started = time.monotonic()
    llm_usage.reset()

    try:
        if path.stat().st_size > config.MAX_FILE_BYTES:
            raise OSError(f"{path.name} exceeds the {config.MAX_FILE_BYTES} byte input limit")
        document_id = document_id_for(path)
    except OSError as exc:
        code = "file_too_large" if "byte input limit" in str(exc) else "unreadable_file"
        return review(_failed(path, f"missing:{path.name}", "acquire", code, exc), resolved)

    try:
        with log_stage(log, "acquire", **doc):
            acquisition = _acquire(path, resolved.acquisition, stages)
            # Pre-flight: garbled text is the most expensive thing to hand a
            # model — it reasons far longer trying to reconcile nonsense
            # (measured on one scan: 117s against 52s on a clean transcription
            # of the same page). Judging the text first turns a wasted
            # extraction into a skipped one.
            if resolved.can_escalate and _uses_ocr(acquisition) and looks_garbled(acquisition.text):
                log.info("OCR judged unusable, re-reading with the last-resort backend", extra=doc)
                acquisition = _acquire(path, _escalated(resolved.acquisition), stages)
    except UnsupportedDocument as exc:
        return review(_failed(path, document_id, "acquire", "unsupported_document", exc), resolved)
    except AcquisitionError as exc:
        return review(_failed(path, document_id, "acquire", "no_text", exc), resolved)
    _notify(on_stage, "acquire", acquisition)

    result = _read(path, document_id, acquisition, resolved, stages, on_stage)
    escalated = False
    if resolved.can_escalate and _uses_ocr(acquisition) and _error_count(result) > 0:
        try:
            second = _acquire(path, _escalated(resolved.acquisition), stages)
            retry = _read(path, document_id, second, resolved, stages, on_stage)
        except AcquisitionError:
            retry = None
        # Ties go to the re-read: reaching this branch means the OCR text
        # already failed validation, so it has no claim to the benefit of
        # the doubt.
        if retry is not None and _error_count(retry) <= _error_count(result):
            result, escalated = retry, True

    result = result.model_copy(
        update={
            "metrics": result.metrics.model_copy(
                update={
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "stage_seconds": {k: round(v, 3) for k, v in stages.seconds.items()},
                    "pages": len(result.layout.pages) if result.layout else 0,
                    "llm_calls": llm_usage.calls,
                    "llm_estimated_tokens": llm_usage.estimated_tokens,
                    "escalated_to_vlm": escalated,
                }
            )
        }
    )
    result = review(result, resolved)
    if not resolved.include_layout:
        result = _slim(result)
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


__all__ = [
    "StageCallback",
    "document_id_for",
    "extract",
    "process_document",
    "review",
    "select_schema",
    "validate_extraction",
]
