"""Text acquisition: the cheapest reading that can be trusted, page by page.

For each page:

1. A PDF page with a usable text layer is taken as is (`pdf_text`).
2. Otherwise the backends run in chain order — the primary OCR engine, then
   each fallback. A reading is accepted when it has text and, if the backend
   reports confidence, the page confidence clears `min_confidence`.
3. If nothing is accepted, the best rejected reading is kept and the page is
   marked `degraded`: low-confidence text a human can review beats no text.
   (The vision model being down must not take a whole batch with it.)

Pages are decided independently, so a PDF mixing born-digital and scanned
pages uses the text layer where it has one and OCR where it doesn't.

When the accepted reading has no word geometry (the vision model), the most
recent OCR reading that was overruled is kept as the page's `witness`: an
independent transcript validation cross-checks the model's numbers against.

`escalate=True` rejects every reading except the last backend's — the
pipeline uses it to re-read a document whose OCR text passed the confidence
gate but whose extraction then failed validation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from .. import limits
from ..layout import DocumentLayout, PageLayout, text_only_page
from .base import BackendUnavailable, OcrBackend, OcrError, OcrSettings
from .pdftext import PDFTextBackend, text_layer_problem
from .registry import get_ocr_backend
from .source import DocumentSource, PageSource
from .witness import confident_amounts

BackendSpec = Union[str, OcrBackend]

# `auto` picks the first of these that can run here.
AUTO_ORDER = ("tesseract", "paddle")


class AcquisitionError(OcrError):
    """No backend produced any text for the document."""


class AcquisitionOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend: BackendSpec = Field(
        default="auto", description="Primary OCR backend: a name, 'auto', or an OcrBackend instance."
    )
    fallbacks: list[BackendSpec] = Field(
        default_factory=lambda: ["vlm"], description="Tried in order when the primary reading is rejected."
    )
    use_pdf_text: bool = True
    min_confidence: float = Field(default=0.60, ge=0.0, le=1.0)
    escalate: bool = False
    settings: OcrSettings = Field(default_factory=OcrSettings)
    max_pages: int | None = None


class Attempt(BaseModel):
    backend: str
    outcome: Literal["accepted", "rejected", "unavailable", "failed"]
    confidence: float | None = None
    reason: str | None = None
    seconds: float = 0.0


class PageAcquisition(BaseModel):
    page: int
    backend: str = Field(description="Backend whose reading the page uses.")
    degraded: bool = Field(
        default=False, description="No reading was accepted; the best rejected one is used."
    )
    confidence: float | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    witness: PageLayout | None = Field(
        default=None,
        description="Independent OCR reading kept when the accepted one has no geometry (VLM).",
    )


class AcquisitionReport(BaseModel):
    primary_backend: str | None
    fallback_backends: list[str]
    fallbacks_applied: list[str] = Field(
        default_factory=list, description="Fallback backends whose reading some page uses."
    )
    pages: list[PageAcquisition] = Field(default_factory=list)

    @property
    def backends_used(self) -> list[str]:
        return sorted({p.backend for p in self.pages})

    @property
    def degraded_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.degraded]


@dataclass
class Acquisition:
    layout: DocumentLayout
    report: AcquisitionReport
    word_confidence_floor: float

    @property
    def text(self) -> str:
        return self.layout.text

    @property
    def witness_pages(self) -> list[str | None]:
        return [p.witness.text if p.witness and p.witness.text.strip() else None for p in self.report.pages]

    @property
    def witness_numbers(self) -> list[list[float]]:
        return [confident_amounts(p.witness, self.word_confidence_floor) for p in self.report.pages]

    @property
    def complete(self) -> bool:
        return bool(self.layout.pages) and all(p.text.strip() for p in self.layout.pages)


def _instantiate(spec: BackendSpec, settings: OcrSettings) -> OcrBackend:
    return spec if isinstance(spec, OcrBackend) else get_ocr_backend(spec, settings)


def resolve_chain(options: AcquisitionOptions) -> tuple[OcrBackend | None, list[OcrBackend]]:
    """Instantiate the primary backend and fallbacks, failing fast.

    A backend named explicitly (or passed as an object) that cannot run here
    is a configuration error, raised before any page is read. `auto`
    silently skips engines that aren't installed.
    """
    settings = options.settings
    if options.backend == "auto":
        primary = None
        for name in AUTO_ORDER:
            try:
                candidate = get_ocr_backend(name, settings)
            except (BackendUnavailable, ValueError):
                continue
            if candidate.availability().available:
                primary = candidate
                break
    else:
        primary = _instantiate(options.backend, settings)
        primary.require_available()
    fallbacks = []
    for spec in options.fallbacks:
        backend = _instantiate(spec, settings)
        backend.require_available()
        if primary is None or backend.name != primary.name:
            fallbacks.append(backend)
    if primary is None and not fallbacks:
        raise BackendUnavailable(
            "auto",
            f"none of {', '.join(AUTO_ORDER)} is available and no fallback is configured",
            "Install Tesseract (`brew install tesseract`) or `pip install \"docket-idp[paddle]\"`",
        )
    return primary, fallbacks


def _read_page(
    page: PageSource,
    chain: list[OcrBackend],
    options: AcquisitionOptions,
    pdf_text: PDFTextBackend,
) -> tuple[PageLayout, PageAcquisition]:
    attempts: list[Attempt] = []

    if page.kind == "text":
        layout = text_only_page(page_number=page.number, text=page.text(), backend="text")
        empty = not layout.text.strip()
        return layout, PageAcquisition(
            page=page.number,
            backend="text",
            degraded=empty,
            attempts=[
                Attempt(backend="text", outcome="rejected", reason="the page is empty")
                if empty
                else Attempt(backend="text", outcome="accepted")
            ],
        )

    if page.kind == "pdf" and options.use_pdf_text and not options.escalate:
        start = time.monotonic()
        try:
            layout = pdf_text.recognize_page(page)
            problem = text_layer_problem(layout)
        except OcrError as exc:
            layout, problem = None, str(exc)
        seconds = round(time.monotonic() - start, 3)
        if layout is not None and problem is None:
            attempts.append(Attempt(backend=pdf_text.name, outcome="accepted", seconds=seconds))
            return layout, PageAcquisition(page=page.number, backend=pdf_text.name, attempts=attempts)
        attempts.append(Attempt(backend=pdf_text.name, outcome="rejected", reason=problem, seconds=seconds))

    runnable = [b for b in chain if "image" in b.capabilities.inputs]
    rejected: list[PageLayout] = []
    for position, backend in enumerate(runnable):
        start = time.monotonic()
        try:
            if backend.capabilities.word_coordinates:
                # An OCR engine: bounded process-wide. (The vision model is
                # bounded by the LLM limit inside llm_client instead.)
                with limits.slot("ocr"):
                    layout = backend.recognize_page(page)
            else:
                layout = backend.recognize_page(page)
        except OcrError as exc:
            attempts.append(
                Attempt(
                    backend=backend.name,
                    outcome="failed",
                    reason=str(exc),
                    seconds=round(time.monotonic() - start, 3),
                )
            )
            continue
        seconds = round(time.monotonic() - start, 3)
        is_last = position == len(runnable) - 1
        reason = None
        if not layout.text.strip():
            reason = "no text recognized"
        elif options.escalate and not is_last:
            reason = "escalated: kept only as an independent witness"
        elif layout.confidence is not None and layout.confidence < options.min_confidence:
            reason = f"confidence {layout.confidence:.2f} < {options.min_confidence:.2f}"
        if reason is None:
            attempts.append(
                Attempt(backend=backend.name, outcome="accepted", confidence=layout.confidence, seconds=seconds)
            )
            witness = None
            if not layout.has_geometry:
                witness = next((r for r in reversed(rejected) if r.has_geometry and r.text.strip()), None)
            return layout, PageAcquisition(
                page=page.number,
                backend=backend.name,
                confidence=layout.confidence,
                attempts=attempts,
                witness=witness,
            )
        attempts.append(
            Attempt(
                backend=backend.name,
                outcome="rejected",
                confidence=layout.confidence,
                reason=reason,
                seconds=seconds,
            )
        )
        rejected.append(layout)

    usable = [r for r in rejected if r.text.strip()]
    if usable:
        best = max(usable, key=lambda r: r.confidence if r.confidence is not None else -1.0)
    else:
        best = text_only_page(page_number=page.number, text="", backend="none")
    return best, PageAcquisition(
        page=page.number,
        backend=best.backend,
        degraded=True,
        confidence=best.confidence,
        attempts=attempts,
    )


def acquire(path: str | Path, options: AcquisitionOptions | None = None) -> Acquisition:
    """Read every page of a document into one DocumentLayout."""
    options = options or AcquisitionOptions()
    primary, fallbacks = resolve_chain(options)
    chain = ([primary] if primary is not None else []) + fallbacks
    pdf_text = PDFTextBackend(options.settings)

    pages: list[PageLayout] = []
    records: list[PageAcquisition] = []
    with DocumentSource(path, dpi=options.settings.dpi, max_pages=options.max_pages) as source:
        for page in source.pages():
            layout, record = _read_page(page, chain, options, pdf_text)
            pages.append(layout)
            records.append(record)

    if not any(p.text.strip() for p in pages):
        failures = "; ".join(
            f"page {r.page}: " + ", ".join(f"{a.backend} {a.outcome}" + (f" ({a.reason})" if a.reason else "") for a in r.attempts)
            for r in records
        )
        raise AcquisitionError(f"no text could be read from {Path(path).name} — {failures}")

    fallback_names = {b.name for b in fallbacks}
    report = AcquisitionReport(
        primary_backend=primary.name if primary is not None else None,
        fallback_backends=[b.name for b in fallbacks],
        fallbacks_applied=sorted({r.backend for r in records if r.backend in fallback_names}),
        pages=records,
    )
    return Acquisition(
        layout=DocumentLayout(pages=pages),
        report=report,
        word_confidence_floor=options.settings.word_confidence_floor,
    )


__all__ = [
    "AUTO_ORDER",
    "Acquisition",
    "AcquisitionError",
    "AcquisitionOptions",
    "AcquisitionReport",
    "Attempt",
    "BackendSpec",
    "PageAcquisition",
    "acquire",
    "resolve_chain",
]
