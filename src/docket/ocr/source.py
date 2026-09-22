"""A document opened for reading, page by page.

Pages render lazily and are cached only while the page is in use, so a long
PDF never sits in memory as a stack of bitmaps. Nothing is written to disk.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterator, Literal

import pdfplumber
from PIL import Image

from .. import pdf as pdf_render

PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
TEXT_SUFFIXES = {".txt", ".md"}
SUPPORTED_SUFFIXES = PDF_SUFFIXES | IMAGE_SUFFIXES | TEXT_SUFFIXES

SourceKind = Literal["pdf", "image", "text"]


class UnsupportedDocument(ValueError):
    pass


class DocumentSource:
    def __init__(
        self,
        path: str | Path,
        *,
        dpi: int = 200,
        max_pages: int | None = None,
        max_pixels: int | None = None,
    ):
        self.path = Path(path)
        suffix = self.path.suffix.lower()
        if suffix in PDF_SUFFIXES:
            self.kind: SourceKind = "pdf"
        elif suffix in IMAGE_SUFFIXES:
            self.kind = "image"
        elif suffix in TEXT_SUFFIXES:
            self.kind = "text"
        else:
            raise UnsupportedDocument(f"Unsupported file type: {suffix or '(none)'}")
        self.dpi = dpi
        self.max_pixels = max_pixels
        self._plumber: pdfplumber.PDF | None = None
        self._text_pages: list[str] | None = None
        self.page_count = self._count_pages()
        if max_pages is not None and self.page_count > max_pages:
            raise UnsupportedDocument(
                f"{self.path.name} has {self.page_count} pages; limit is {max_pages}"
            )

    def _count_pages(self) -> int:
        if self.kind == "pdf":
            pages = self.plumber.pages
            if self.max_pixels is not None:
                scale = self.dpi / 72
                for page in pages:
                    pixels = int(page.width * scale) * int(page.height * scale)
                    if pixels > self.max_pixels:
                        raise UnsupportedDocument(
                            f"{self.path.name} page {page.page_number} renders to {pixels} pixels; "
                            f"limit is {self.max_pixels}"
                        )
            return len(pages)
        if self.kind == "image":
            with Image.open(self.path) as image:
                pixels = image.width * image.height
                if self.max_pixels is not None and pixels > self.max_pixels:
                    raise UnsupportedDocument(
                        f"{self.path.name} contains {pixels} pixels; limit is {self.max_pixels}"
                    )
                return getattr(image, "n_frames", 1)
        return len(self.text_pages)

    @property
    def plumber(self) -> pdfplumber.PDF:
        if self.kind != "pdf":
            raise UnsupportedDocument(f"{self.path.name} is not a PDF")
        if self._plumber is None:
            self._plumber = pdfplumber.open(self.path)
        return self._plumber

    @property
    def text_pages(self) -> list[str]:
        """Pages of a plain-text source, split on form feeds."""
        if self._text_pages is None:
            self._text_pages = self.path.read_text(encoding="utf-8").split("\f")
        return self._text_pages

    def page(self, number: int) -> "PageSource":
        if not 1 <= number <= self.page_count:
            raise IndexError(f"page {number} out of range 1..{self.page_count}")
        return PageSource(self, number)

    def pages(self) -> Iterator["PageSource"]:
        for number in range(1, self.page_count + 1):
            yield self.page(number)

    def close(self) -> None:
        if self._plumber is not None:
            self._plumber.close()
            self._plumber = None

    def __enter__(self) -> "DocumentSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class PageSource:
    """One page: its PDF text layer (for PDFs) and its rendered image."""

    def __init__(self, document: DocumentSource, number: int):
        self.document = document
        self.number = number
        self._image: Image.Image | None = None

    @property
    def kind(self) -> SourceKind:
        return self.document.kind

    @property
    def plumber_page(self):
        return self.document.plumber.pages[self.number - 1]

    def image(self) -> Image.Image:
        if self._image is None:
            if self.kind == "pdf":
                self._image = pdf_render.render_page(
                    self.document.path, self.number - 1, self.document.dpi
                )
            elif self.kind == "image":
                with Image.open(self.document.path) as image:
                    image.seek(self.number - 1)
                    self._image = image.convert("RGB")
            else:
                raise UnsupportedDocument("a plain-text source has no page image")
        return self._image

    def image_png(self) -> bytes:
        buffer = io.BytesIO()
        self.image().save(buffer, format="PNG")
        return buffer.getvalue()

    def text(self) -> str:
        if self.kind != "text":
            raise UnsupportedDocument("only plain-text sources carry page text")
        return self.document.text_pages[self.number - 1]


__all__ = [
    "DocumentSource",
    "IMAGE_SUFFIXES",
    "PageSource",
    "SUPPORTED_SUFFIXES",
    "UnsupportedDocument",
]
