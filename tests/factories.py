"""Shared builders for tests: results, layouts and a scriptable OCR backend."""
from __future__ import annotations

from pathlib import Path

from docket.layout import DocumentLayout, PageLayout, RawWord, build_page, text_only_page
from docket.ocr import (
    Acquisition,
    AcquisitionReport,
    BackendStatus,
    Capabilities,
    OcrBackend,
    PageAcquisition,
)
from docket.result import DocumentResult, DocumentStatus
from docket.schemas import ClassificationResult


def words_page(
    lines: list[str],
    *,
    page_number: int = 1,
    backend: str = "tesseract",
    confidence: float | None = 0.9,
    word_confidence: float | None = 0.9,
    width: float = 1000.0,
    height: float = 1400.0,
) -> PageLayout:
    """A page whose lines are laid out top to bottom, one word box per token."""
    words = []
    for row, line in enumerate(lines):
        x = 20.0
        for token in line.split():
            w = 9.0 * len(token)
            words.append(
                RawWord(
                    text=token,
                    x0=x,
                    y0=20.0 + 20 * row,
                    x1=x + w,
                    y1=32.0 + 20 * row,
                    confidence=word_confidence,
                )
            )
            x += w + 4.0
    return build_page(
        page_number=page_number,
        width=width,
        height=height,
        unit="px",
        backend=backend,
        confidence=confidence,
        words=words,
    )


def acquisition(
    pages: list[PageLayout],
    *,
    primary: str | None = "tesseract",
    fallbacks: list[str] | None = None,
    degraded: set[int] | None = None,
    witnesses: dict[int, PageLayout] | None = None,
) -> Acquisition:
    degraded = degraded or set()
    witnesses = witnesses or {}
    report = AcquisitionReport(
        primary_backend=primary,
        fallback_backends=fallbacks if fallbacks is not None else ["vlm"],
        pages=[
            PageAcquisition(
                page=p.page_number,
                backend=p.backend,
                degraded=p.page_number in degraded,
                confidence=p.confidence,
                witness=witnesses.get(p.page_number),
            )
            for p in pages
        ],
    )
    return Acquisition(layout=DocumentLayout(pages=pages), report=report, word_confidence_floor=0.6)


def text_acquisition(text: str, backend: str = "pdf_text") -> Acquisition:
    page = text_only_page(page_number=1, text=text, backend=backend)
    return acquisition([page], primary=None if backend == "pdf_text" else backend)


def make_result(**overrides) -> DocumentResult:
    base = dict(
        source="doc.png",
        document_id="doc_test",
        status=DocumentStatus.SUCCEEDED,
        document_type="invoice",
        schema_id="invoice",
        schema_version="2.0",
        classification=ClassificationResult(
            doc_type="invoice", confidence=0.9, method="rules"
        ),
        layout=DocumentLayout(pages=[text_only_page(page_number=1, text="x", backend="pdf_text")]),
        extracted={"invoice_number": "INV-1"},
    )
    base.update(overrides)
    return DocumentResult(**base)


class ScriptedBackend(OcrBackend):
    """An OCR backend whose readings are given up front, per page number."""

    capabilities = Capabilities(
        confidence=True, word_coordinates=True, lines=True, tables=False, rotation=False
    )

    def __init__(
        self,
        name: str = "scripted",
        pages: dict[int, PageLayout] | None = None,
        *,
        available: bool = True,
        error: Exception | None = None,
        geometry: bool = True,
    ):
        super().__init__()
        self.name = name  # type: ignore[misc]
        self.pages = pages or {}
        self.available = available
        self.error = error
        self.calls: list[int] = []
        if not geometry:
            self.capabilities = Capabilities(  # type: ignore[misc]
                confidence=False, word_coordinates=False, lines=False, tables=False, rotation=False
            )

    def availability(self) -> BackendStatus:
        if self.available:
            return BackendStatus(name=self.name, available=True)
        return BackendStatus(name=self.name, available=False, reason="scripted as missing")

    def recognize_page(self, page) -> PageLayout:
        self.calls.append(page.number)
        if self.error is not None:
            raise self.error
        layout = self.pages.get(page.number) or self.pages.get(0)
        if layout is None:
            return text_only_page(page_number=page.number, text="", backend=self.name)
        return layout.model_copy(update={"page_number": page.number, "backend": self.name})


def write_png(path: Path, size=(200, 100)) -> Path:
    from PIL import Image

    Image.new("RGB", size, "white").save(path)
    return path


def flat_invoice(**fields):
    """An Invoice 2.0 built from 1.0-style flat fields (vendor_name, vendor_vat_number, ...),
    through the same migration that upgrades stored 1.0 results."""
    from docket.catalog import Invoice
    from docket.catalog.builtin import _upgrade_invoice

    return Invoice.model_validate(_upgrade_invoice(fields))


def flat_po(**fields):
    """A PurchaseOrder 2.0 from 1.0-style vendor_name/customer_name fields."""
    from docket.catalog import PurchaseOrder
    from docket.catalog.builtin import _upgrade_purchase_order

    return PurchaseOrder.model_validate(_upgrade_purchase_order(fields))
