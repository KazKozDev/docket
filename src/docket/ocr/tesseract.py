"""Tesseract OCR from word boxes (`image_to_data`).

One image_to_data pass feeds everything — the words for layout analysis, the
page confidence gate and the witness numbers — so they can never disagree
about what Tesseract saw.

Page confidence is character-weighted over Tesseract's own lines: the share
of characters sitting in lines whose average word confidence clears the word
floor. A page with one crisp heading and a garbled body scores low, which a
plain word average does not do.
"""
from __future__ import annotations

import re
import shutil

import pytesseract
from PIL import Image

from ..layout import PageLayout, RawWord, build_page
from .base import BackendStatus, Capabilities, OcrBackend, OcrError
from .deskew import deskew_image
from .languages import TESSERACT, tesseract_codes
from .preprocess import upscale_factor
from .source import PageSource

_ROTATE = re.compile(r"Rotate:\s*(\d+)")
# An enlarged page is never allowed past this many pixels.
_MAX_UPSCALED_PIXELS = 40_000_000


def word_heights(data: dict) -> list[float]:
    """Box heights of the words Tesseract actually recognized."""
    heights = []
    for i, raw in enumerate(data.get("text", [])):
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if (raw or "").strip() and conf >= 0:
            heights.append(float(data["height"][i]))
    return heights


def page_confidence(data: dict, floor: float) -> float:
    """Character-weighted share of text in lines whose mean word confidence
    (0..100 scale, as Tesseract reports it) clears `floor` (0..1)."""
    lines: dict[tuple, list[tuple[str, float]]] = {}
    for i, raw in enumerate(data.get("text", [])):
        word = (raw or "").strip()
        if not word:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0:
            continue
        key = (data["page_num"][i], data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((word, conf))
    total = good = 0
    for words in lines.values():
        chars = sum(len(w) for w, _ in words)
        total += chars
        if sum(c for _, c in words) / len(words) >= floor * 100:
            good += chars
    return good / total if total else 0.0


def words_from_data(data: dict) -> list[RawWord]:
    words = []
    for i, raw in enumerate(data.get("text", [])):
        text = (raw or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < 0:
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        width, height = float(data["width"][i]), float(data["height"][i])
        words.append(
            RawWord(
                text=text,
                x0=left,
                y0=top,
                x1=left + width,
                y1=top + height,
                confidence=min(1.0, conf / 100.0),
                line_key=(
                    data["page_num"][i],
                    data["block_num"][i],
                    data["par_num"][i],
                    data["line_num"][i],
                ),
            )
        )
    return words


class TesseractBackend(OcrBackend):
    name = "tesseract"
    capabilities = Capabilities(
        confidence=True,
        word_coordinates=True,
        lines=True,
        tables=True,
        rotation=True,
        languages=sorted(TESSERACT),
        inputs=["image"],
    )

    def availability(self) -> BackendStatus:
        if shutil.which(pytesseract.pytesseract.tesseract_cmd) is None:
            return BackendStatus(
                name=self.name,
                available=False,
                reason="the tesseract binary is not on PATH",
                install_hint="Install it: `brew install tesseract` or `apt install tesseract-ocr`",
            )
        try:
            installed = set(pytesseract.get_languages(config=""))
        except Exception as exc:  # noqa: BLE001 — a broken install reports here
            return BackendStatus(name=self.name, available=False, reason=f"tesseract failed to start: {exc}")
        missing = [c for c in tesseract_codes(self.settings.languages) if c not in installed]
        if missing:
            return BackendStatus(
                name=self.name,
                available=False,
                reason=f"language data not installed: {', '.join(missing)}",
                install_hint="Install the traineddata, e.g. `brew install tesseract-lang` or `apt install tesseract-ocr-<lang>`",
            )
        return BackendStatus(name=self.name, available=True)

    def _rotation(self, image: Image.Image) -> int:
        """Clockwise degrees that make the page upright, per Tesseract OSD.
        OSD refuses pages with too little text; those are read as they are."""
        try:
            osd = pytesseract.image_to_osd(image, config="--psm 0")
        except pytesseract.TesseractError:
            return 0
        match = _ROTATE.search(osd)
        return int(match.group(1)) % 360 if match else 0

    def recognize_page(self, page: PageSource) -> PageLayout:
        image = page.image()
        rotation = self._rotation(image) if self.settings.detect_rotation else 0
        if rotation:
            # PIL rotates counter-clockwise; OSD reports a clockwise fix.
            image = image.rotate(-rotation, expand=True)
        image, deskew_angle = deskew_image(image) if self.settings.deskew else (image, 0.0)
        floor = self.settings.word_confidence_floor
        data = self._read(image, page.number)
        confidence = page_confidence(data, floor)
        # Small text (a low-resolution scan, a receipt shot from afar) is
        # read again enlarged; the enlarged reading is kept only if Tesseract
        # is at least as sure of it, so this can't make a page worse.
        factor = upscale_factor(word_heights(data), min_height=self.settings.min_text_height)
        factor = min(factor, (_MAX_UPSCALED_PIXELS / (image.width * image.height)) ** 0.5)
        if factor > 1.05:
            enlarged = image.resize((round(image.width * factor), round(image.height * factor)), Image.Resampling.LANCZOS)
            enlarged_data = self._read(enlarged, page.number)
            enlarged_confidence = page_confidence(enlarged_data, floor)
            if enlarged_confidence >= confidence:
                image, data, confidence = enlarged, enlarged_data, enlarged_confidence
        return build_page(
            page_number=page.number,
            width=float(image.width),
            height=float(image.height),
            unit="px",
            rotation=rotation,
            deskew_angle=deskew_angle,
            backend=self.name,
            confidence=confidence,
            words=words_from_data(data),
        )

    def _read(self, image: Image.Image, page_number: int) -> dict:
        try:
            return pytesseract.image_to_data(
                image,
                lang="+".join(tesseract_codes(self.settings.languages)),
                config=f"--psm {self.settings.tesseract_psm}",
                output_type=pytesseract.Output.DICT,
            )
        except pytesseract.TesseractError as exc:
            raise OcrError(f"tesseract failed on page {page_number}: {exc}") from exc


__all__ = ["TesseractBackend", "page_confidence", "words_from_data"]
