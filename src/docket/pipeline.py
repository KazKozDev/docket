"""Orchestrates the full flow: OCR/VLM -> classify -> extract -> validate."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from . import config, doctypes, ocr, review_queue
from .classify import classify
from .extract import extract_pages
from .llm_client import LLMError
from .language import detect_language
from .llm_client import usage as llm_usage
from .logging_setup import get_logger, log_stage
from .ocr_quality import looks_garbled
from .schemas import PipelineResult, ValidationIssue
from .validate import validate

StageCallback = Callable[[str, Any], None]
log = get_logger()


def _notify(on_stage: StageCallback | None, stage: str, payload: object) -> None:
    if on_stage is not None:
        on_stage(stage, payload)


def _error_count(result: PipelineResult) -> int:
    return sum(1 for i in result.validation_issues if i.severity == "error")


def _run_once(
    path: Path, *, on_stage: StageCallback | None, force_vlm: bool = False
) -> PipelineResult:
    """One full pass: acquire text, classify, extract, validate. No review
    queue side effects — `process` decides what to do with the outcome.
    """
    doc = {"document": path.name}

    with log_stage(log, "text_acquisition", **doc, forced_vlm=force_vlm):
        ocr_result = ocr.extract_text(path, force_vlm=force_vlm)

        # Pre-flight: garbled input is the most expensive thing you can hand a
        # model — it reasons far longer trying to reconcile nonsense (measured
        # on one scan: 117s on the garbled text against 52s on a clean
        # transcription of the same page). Judging the text first turns a
        # wasted extraction into a skipped one.
        if (
            not force_vlm
            and "ocr" in (ocr_result.page_methods or [ocr_result.method])
            and looks_garbled(ocr_result.text)
        ):
            log.info("OCR judged unusable, re-reading with the vision model", extra=doc)
            try:
                ocr_result = ocr.extract_text(path, force_vlm=True)
            except LLMError as exc:
                log.warning(
                    "vision re-read unavailable, keeping the OCR text",
                    extra={**doc, "error": str(exc)},
                )

    _notify(on_stage, "ocr", ocr_result)

    with log_stage(log, "classify", **doc):
        classification = classify(ocr_result.text)
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

    language, lang_confidence = detect_language(ocr_result.text)
    log.info(
        "language detected",
        extra={**doc, "language": language, "confidence": lang_confidence},
    )

    common = {
        "source": str(path),
        "classification": classification,
        "ocr_method": ocr_result.method,
        "raw_text_chars": len(ocr_result.text),
        "language": language,
        "pages_total": len(ocr_result.pages or []),
        "pages_processed": len(ocr_result.pages or []),
        "complete": bool(ocr_result.pages)
        and all(page.strip() for page in ocr_result.pages),
        "page_methods": ocr_result.page_methods or [],
        "document_id": f"doc_{hashlib.sha256(path.read_bytes()).hexdigest()[:20]}",
    }

    doc_type = doctypes.get_document_type(classification.doc_type)
    schema_cls = doc_type.schema if doc_type is not None else None
    if schema_cls is None:
        _notify(on_stage, "extract", None)
        return PipelineResult(
            **common,
            extracted=None,
            extract_attempts=0,
            validation_issues=[
                ValidationIssue(
                    field="doc_type",
                    message=f"unrecognized document type: {classification.type_name}",
                )
            ],
            llm_calls=llm_usage.calls,
            llm_estimated_tokens=llm_usage.estimated_tokens,
        )

    with log_stage(log, "extract", **doc, schema=schema_cls.__name__):
        instance, attempts = extract_pages(
            ocr_result.pages or [ocr_result.text], schema_cls
        )
    _notify(on_stage, "extract", instance)

    # A VLM transcript no confident OCR reading backs is unconfirmed, even
    # when self-consistent — the model has been observed inventing digits
    # to force totals to reconcile. Pure-OCR pages need no such flag: the
    # primary text already is the independent reading.
    vlm_pages = [m for m in (ocr_result.page_methods or []) if m == "vlm"]
    unconfirmed = bool(vlm_pages) and not any(ocr_result.witness_numbers or [])
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
                ocr_result.text,
                pages=ocr_result.pages,
                witness_pages=ocr_result.witness_pages,
                vlm_unconfirmed=unconfirmed,
            )

    # Citations are how validation checks the extraction, not something the
    # document says. Left inside `extracted` they outweigh the data — on a
    # short invoice the quote blocks ran longer than the fields they vouched
    # for — so they move one level up, where an auditor can still read them
    # and a consumer of the fields doesn't have to.
    extracted = instance.model_dump(mode="json") if instance else None
    sources = extracted.pop("field_locations", None) if extracted else None

    return PipelineResult(
        **common,
        extracted=extracted,
        field_sources=sources or {},
        extract_attempts=attempts,
        validation_issues=issues,
        llm_calls=llm_usage.calls,
        llm_estimated_tokens=llm_usage.estimated_tokens,
    )


def process(
    path: str | Path,
    *,
    on_stage: StageCallback | None = None,
    enqueue_review: bool | None = None,
) -> PipelineResult:
    """Run the full pipeline. `on_stage(stage_name, result)` fires after each
    stage completes ("ocr", "classify", "extract", "validate") — used by the
    CLI/TUI to render live progress without duplicating this logic.

    Documents that need a human look are appended to the file-based review
    queue unless `enqueue_review` is False (default: `config.REVIEW_QUEUE_ENABLED`).
    Applications with their own review workflow should pass False and act on
    `result.needs_review` / `result.review_reasons` themselves.

    If Tesseract's text passes its confidence gate but the result then fails
    validation, the document is re-read with the vision model and the better
    of the two results wins. Tesseract's confidence score turns out to be a
    poor gate on its own: measured at 77.5 (floor is 60) on an invoice where
    it read "$530.00" as "$830.00" and dropped the grand-total line entirely.
    A failed validation is a far better signal that the text was wrong,
    because it checks the meaning of the output rather than the crispness of
    the input — and it only spends a VLM call on documents already known to
    be broken.
    """
    path = Path(path)
    llm_usage.reset()

    result = _run_once(path, on_stage=on_stage)

    if (
        result.ocr_method in {"ocr", "mixed"}
        and ("ocr" in result.page_methods or not result.page_methods)
        and _error_count(result) > 0
    ):
        try:
            escalated = _run_once(path, on_stage=on_stage, force_vlm=True)
        except LLMError:
            escalated = None
        # Ties go to the vision model. Reaching this branch at all means the
        # OCR text already produced a result that failed validation, so it has
        # no claim to the benefit of the doubt — and the VLM read the page
        # rather than guessing at characters.
        if escalated is not None and _error_count(escalated) <= _error_count(result):
            result = escalated.model_copy(update={"escalated_to_vlm": True})

    reasons = review_queue.reasons_for(result)
    result = result.model_copy(
        update={"needs_review": bool(reasons), "review_reasons": reasons}
    )
    if enqueue_review is None:
        enqueue_review = config.REVIEW_QUEUE_ENABLED
    if result.needs_review and enqueue_review:
        review_queue.enqueue(result, reasons)
    log.info(
        "document processed",
        extra={
            "document": Path(result.source).name,
            "doc_type": result.classification.type_name,
            "ocr_method": result.ocr_method,
            "escalated_to_vlm": result.escalated_to_vlm,
            "llm_calls": result.llm_calls,
            "needs_review": result.needs_review,
            "validation_errors": _error_count(result),
        },
    )
    _notify(on_stage, "validate", result.validation_issues)
    return result
