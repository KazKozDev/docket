"""The OCR backend contract.

A backend turns one page into a `PageLayout`. Whatever engine sits behind it
— a PDF text layer, Tesseract, PaddleOCR, a vision model — the result has the
same shape, so everything downstream (layout analysis, citations, witness
checks, extraction) is written once.

    class MyBackend(OcrBackend):
        name = "my_engine"
        capabilities = Capabilities(confidence=True, word_coordinates=True, ...)

        def availability(self) -> BackendStatus: ...
        def recognize_page(self, page: PageSource) -> PageLayout: ...

Register it with `docket.ocr.register_ocr_backend`, expose it through the
`docket.ocr_backends` entry point, or pass an instance straight to
`process_document(..., ocr_backend=MyBackend())`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, Field

from ..layout import DocumentLayout, PageLayout

if TYPE_CHECKING:
    from .source import DocumentSource, PageSource


class OcrError(RuntimeError):
    """A backend failed while reading a page."""


class BackendUnavailable(OcrError):
    """A backend was asked for but cannot run here (not installed, no binary,
    missing language data). The message says why and how to fix it."""

    def __init__(self, name: str, reason: str, install_hint: str | None = None):
        self.name = name
        self.reason = reason
        self.install_hint = install_hint
        message = f"OCR backend {name!r} is unavailable: {reason}"
        if install_hint:
            message += f". {install_hint}"
        super().__init__(message)


class Capabilities(BaseModel):
    """What a backend's PageLayout can be trusted to contain."""

    confidence: bool = Field(description="Reports per-word and page confidence.")
    word_coordinates: bool = Field(description="Reports a box for every word.")
    lines: bool = Field(description="Produces text lines (from geometry or natively).")
    tables: bool = Field(description="Reports table structure itself or via layout analysis.")
    rotation: bool = Field(description="Detects and corrects page rotation.")
    languages: list[str] | None = Field(
        default=None,
        description="ISO 639-1 codes the backend can read here; None means not enumerable.",
    )
    inputs: list[str] = Field(
        default_factory=lambda: ["image", "pdf"],
        description="Source kinds it accepts: 'image' (rendered page) and/or 'pdf' (text layer).",
    )


class BackendStatus(BaseModel):
    name: str
    available: bool
    reason: str | None = None
    install_hint: str | None = None


class OcrSettings(BaseModel):
    """Engine-independent settings each backend maps to its own options."""

    languages: list[str] = Field(
        default_factory=lambda: ["en"], description="ISO 639-1 codes, e.g. ['en', 'de']."
    )
    device: str = Field(default="cpu", description="Compute device for engines that have one ('cpu', 'gpu', 'gpu:0').")
    dpi: int = Field(default=200, ge=50, le=600, description="Resolution scanned PDF pages are rendered at.")
    detect_rotation: bool = True
    word_confidence_floor: float = Field(
        default=0.60, ge=0.0, le=1.0,
        description="Words below this confidence neither count toward page confidence nor act as witnesses.",
    )
    tesseract_psm: str = "3"
    paddle_model: Literal["mobile", "medium"] = Field(
        default="mobile",
        description="PaddleOCR model size: 'mobile' (PP-OCRv5 mobile) or 'medium' (PaddleOCR's default for the language).",
    )
    paddle_tables: bool = Field(
        default=False,
        description="Run PaddleOCR's table recognition pipeline for cell structure (extra models, slower).",
    )


class OcrBackend(ABC):
    name: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    def __init__(self, settings: OcrSettings | None = None) -> None:
        self.settings = settings or OcrSettings()

    @abstractmethod
    def availability(self) -> BackendStatus:
        """Whether this backend can run here — cheap, no page is read."""

    @abstractmethod
    def recognize_page(self, page: "PageSource") -> PageLayout:
        """Read one page. Raise OcrError on failure, never return None."""

    def recognize_document(self, document: "DocumentSource") -> DocumentLayout:
        """Read every page. Override when the engine is faster on a whole file."""
        return DocumentLayout(pages=[self.recognize_page(p) for p in document.pages()])

    def require_available(self) -> None:
        status = self.availability()
        if not status.available:
            raise BackendUnavailable(self.name, status.reason or "unknown reason", status.install_hint)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


__all__ = [
    "BackendStatus",
    "BackendUnavailable",
    "Capabilities",
    "OcrBackend",
    "OcrError",
    "OcrSettings",
]
