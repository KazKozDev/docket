"""Options for `process_document`, and how they resolve against the environment.

Every option is `None` by default, meaning "use the environment (DOCKET_*),
else the built-in default". An explicit value always wins. `resolve()` turns
options into concrete settings and validates everything that can be checked
without reading a document, so a typo fails before the first page is read.

    from docket import ProcessOptions, OcrOptions, process_document

    options = ProcessOptions(
        ocr=OcrOptions(backend="paddle", fallbacks=["tesseract", "vlm"], languages=["en", "de"]),
        document_type="invoice",          # skip classification
        include_layout=False,             # smaller results
    )
    result = process_document("scan.pdf", options=options)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from . import config
from .errors import ConfigurationError
from .ocr import AcquisitionOptions, OcrBackend, OcrSettings, parse_languages, resolve_chain

BackendSpec = Union[str, OcrBackend]


class OcrOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    backend: BackendSpec | None = Field(
        default=None, description="Primary OCR backend name, 'auto', or an OcrBackend. Env: DOCKET_OCR_BACKEND."
    )
    fallbacks: list[BackendSpec] | None = Field(
        default=None, description="Backends tried in order when a page's reading is rejected. Env: DOCKET_OCR_FALLBACKS."
    )
    languages: list[str] | str | None = Field(
        default=None, description="ISO 639-1 codes. Env: DOCKET_OCR_LANGUAGES."
    )
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detect_rotation: bool | None = None
    deskew: bool | None = None
    use_pdf_text: bool = Field(default=True, description="Take a usable PDF text layer without OCR.")
    dpi: int | None = Field(default=None, ge=50, le=600)
    device: str | None = Field(default=None, description="PaddleOCR device. Env: DOCKET_PADDLE_DEVICE.")
    paddle_model: Literal["mobile", "medium"] | None = None
    paddle_tables: bool | None = None
    docling_table_mode: Literal["fast", "accurate"] | None = None
    docling_cell_matching: bool | None = None


class ReviewOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enqueue: bool | None = Field(
        default=None,
        description="Append documents that need a human to the review queue. Env: DOCKET_REVIEW_QUEUE_ENABLED.",
    )
    min_classification_confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Env: DOCKET_MIN_CONFIDENCE."
    )
    database_url: str | None = Field(
        default=None, description="SQLAlchemy URL of the review store. Env: DOCKET_REVIEW_DATABASE_URL."
    )
    documents_dir: Path | None = Field(default=None, description="Env: DOCKET_REVIEW_DOCUMENTS.")


class ProcessOptions(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    ocr: OcrOptions = Field(default_factory=OcrOptions)
    document_type: str | None = Field(
        default=None, description="A registered schema id (e.g. 'invoice'). Skips classification."
    )
    schema_version: str | None = Field(
        default=None, description="A registered version of document_type; default the latest."
    )
    schema_model: type[BaseModel] | None = Field(
        default=None,
        description="A Pydantic model to extract into, registered or not. Skips classification.",
    )
    classify: bool = Field(
        default=True,
        description="Classify when no type or schema is given. False requires document_type or schema_model.",
    )
    include_layout: bool | None = Field(
        default=None,
        description="Keep page layouts (and OCR witnesses) in the result. Field locations are kept either way. "
        "Env: DOCKET_INCLUDE_LAYOUT.",
    )
    escalate: bool = Field(
        default=True,
        description="Re-read with the last fallback backend when OCR text fails validation.",
    )
    review: ReviewOptions = Field(default_factory=ReviewOptions)


@dataclass(frozen=True)
class ResolvedReview:
    enqueue: bool
    min_classification_confidence: float
    database_url: str
    documents_dir: Path


@dataclass(frozen=True)
class ResolvedOptions:
    """Concrete settings for one run: environment applied, backends
    instantiated and checked, schema chosen or classification required."""

    acquisition: AcquisitionOptions
    document_type: object | None  # catalog.SchemaSpec, when the schema is fixed
    classify: bool
    include_layout: bool
    escalate: bool
    review: ResolvedReview

    @property
    def can_escalate(self) -> bool:
        return self.escalate and bool(self.acquisition.fallbacks)


def _pick(value, env):
    return env if value is None else value


def resolve(options: ProcessOptions | None = None) -> ResolvedOptions:
    """Apply the environment to `options` and validate them. Raises
    ConfigurationError on anything that would fail later."""
    from . import catalog

    config.check()
    options = options or ProcessOptions()
    ocr = options.ocr
    settings = OcrSettings(
        languages=parse_languages(_pick(ocr.languages, config.OCR_LANGUAGES)),
        device=_pick(ocr.device, config.PADDLE_DEVICE),
        dpi=_pick(ocr.dpi, config.OCR_DPI),
        detect_rotation=_pick(ocr.detect_rotation, config.OCR_DETECT_ROTATION),
        deskew=_pick(ocr.deskew, config.OCR_DESKEW),
        tesseract_psm=config.TESSERACT_PSM,
        paddle_model=_pick(ocr.paddle_model, config.PADDLE_MODEL),
        paddle_tables=_pick(ocr.paddle_tables, config.PADDLE_TABLES),
        docling_table_mode=_pick(ocr.docling_table_mode, config.DOCLING_TABLE_MODE),
        docling_cell_matching=_pick(ocr.docling_cell_matching, config.DOCLING_CELL_MATCHING),
    )
    acquisition = AcquisitionOptions(
        backend=_pick(ocr.backend, config.OCR_BACKEND),
        fallbacks=list(_pick(ocr.fallbacks, config.OCR_FALLBACKS)),
        use_pdf_text=ocr.use_pdf_text,
        min_confidence=_pick(ocr.min_confidence, config.OCR_MIN_CONFIDENCE),
        settings=settings,
        max_pages=config.MAX_PDF_PAGES,
        max_pixels=config.MAX_IMAGE_PIXELS,
    )
    primary, fallbacks = resolve_chain(acquisition)
    acquisition = acquisition.model_copy(
        update={"backend": primary if primary is not None else "auto", "fallbacks": fallbacks}
    )

    doc_type = catalog.resolve(
        schema_id=options.document_type, model=options.schema_model, version=options.schema_version
    )
    if doc_type is None and not options.classify:
        raise ConfigurationError(
            "classify=False needs document_type or schema_model: there is nothing else to pick a schema with"
        )

    review = options.review
    return ResolvedOptions(
        acquisition=acquisition,
        document_type=doc_type,
        classify=options.classify,
        include_layout=_pick(options.include_layout, config.INCLUDE_LAYOUT),
        escalate=options.escalate,
        review=ResolvedReview(
            enqueue=_pick(review.enqueue, config.REVIEW_QUEUE_ENABLED),
            min_classification_confidence=_pick(
                review.min_classification_confidence, config.MIN_CLASSIFICATION_CONFIDENCE
            ),
            database_url=_pick(review.database_url, config.REVIEW_DATABASE_URL),
            documents_dir=_pick(review.documents_dir, config.REVIEW_DOCUMENTS_DIR),
        ),
    )


__all__ = [
    "OcrOptions",
    "ProcessOptions",
    "ResolvedOptions",
    "ResolvedReview",
    "ReviewOptions",
    "resolve",
]
