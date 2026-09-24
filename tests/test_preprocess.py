"""Page image preparation: photo cropping, small-text re-reading, and the
caller's own preprocessing hook — on synthetic images, no LLM."""
from __future__ import annotations

import shutil
from typing import ClassVar

import pytest
from PIL import Image, ImageDraw, ImageFont

from docket.layout import PageLayout, build_page
from docket.ocr import AcquisitionOptions, OcrSettings, acquire
from docket.ocr.base import BackendStatus, Capabilities, OcrBackend
from docket.ocr.preprocess import crop_document, upscale_factor
from docket.ocr.source import DocumentSource

needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def _text_page(size, lines, font_size, origin=(40, 60), step=60) -> Image.Image:
    page = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=font_size)
    for i, line in enumerate(lines):
        draw.text((origin[0], origin[1] + i * step), line, fill="black", font=font)
    return page


def _photo() -> tuple[Image.Image, list[tuple[float, float]]]:
    """An invoice page photographed at an angle on a dark, grainy table."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    page = np.asarray(_text_page((600, 800), ["INVOICE 2026-17", "Acme GmbH", "Total 1815.00 EUR"], 28))
    corners = np.float32([[180, 140], [760, 200], [700, 1050], [120, 980]])
    warp = cv2.getPerspectiveTransform(np.float32([[0, 0], [600, 0], [600, 800], [0, 800]]), corners)
    table = np.random.default_rng(0).integers(40, 90, (1200, 900, 3)).astype("uint8")
    placed = cv2.warpPerspective(page, warp, (900, 1200))
    footprint = cv2.warpPerspective(np.full((800, 600), 255, np.uint8), warp, (900, 1200))
    table[footprint > 0] = placed[footprint > 0]
    return Image.fromarray(table), [(x / 900, y / 1200) for x, y in corners]


# ---- cropping ---------------------------------------------------------------------------------


def test_a_photographed_page_is_found_and_flattened():
    photo, corners = _photo()

    flat, quad = crop_document(photo)

    assert quad is not None
    assert all(abs(qx - cx) < 0.02 and abs(qy - cy) < 0.02 for (qx, qy), (cx, cy) in zip(quad, corners))
    # Flattened back to (roughly) the page's own 3:4 proportions.
    assert abs(flat.width / flat.height - 600 / 800) < 0.08


def test_a_scan_is_left_alone():
    pytest.importorskip("cv2")
    scan = _text_page((850, 1100), ["Line of invoice text 12.00"] * 20, 22, origin=(60, 60), step=45)
    assert crop_document(scan) == (scan, None)


def test_a_table_frame_on_a_scan_is_not_mistaken_for_the_page():
    """Cropping to the frame would cut away the header and footer text."""
    pytest.importorskip("cv2")
    scan = _text_page((850, 1100), ["Line of invoice text 12.00"] * 20, 22, origin=(60, 60), step=45)
    ImageDraw.Draw(scan).rectangle((100, 300, 750, 800), outline="black", width=3)
    assert crop_document(scan)[1] is None


class _ImageReader(OcrBackend):
    """Records the image OCR is handed; reads one fixed word."""

    name = "image-reader"
    capabilities = Capabilities(confidence=True, word_coordinates=True, lines=True, tables=False, rotation=False)
    seen: ClassVar[list[Image.Image]] = []

    def availability(self) -> BackendStatus:
        return BackendStatus(name=self.name, available=True)

    def recognize_page(self, page) -> PageLayout:
        image = page.image()
        type(self).seen.append(image)
        from docket.layout import RawWord

        word = RawWord(text="INVOICE", x0=10, y0=10, x1=90, y1=30, confidence=0.95)
        return build_page(page_number=page.number, width=image.width, height=image.height, unit="px",
                          backend=self.name, confidence=0.95, words=[word])


def test_acquisition_records_where_the_page_was_in_the_photo(tmp_path):
    photo, _ = _photo()
    path = tmp_path / "receipt.jpg"
    photo.save(path)
    _ImageReader.seen = []

    reading = acquire(path, AcquisitionOptions(backend=_ImageReader(), fallbacks=[],
                                               settings=OcrSettings(crop_photos=True)))

    page = reading.layout.pages[0]
    assert page.crop_quad is not None and len(page.crop_quad) == 4
    assert _ImageReader.seen[0].size != photo.size  # OCR saw the flattened page


def test_cropping_can_be_turned_off(tmp_path):
    photo, _ = _photo()
    path = tmp_path / "receipt.jpg"
    photo.save(path)
    _ImageReader.seen = []

    reading = acquire(path, AcquisitionOptions(backend=_ImageReader(), fallbacks=[],
                                               settings=OcrSettings(crop_photos=False)))

    assert reading.layout.pages[0].crop_quad is None
    assert _ImageReader.seen[0].size == photo.size


# ---- the caller's hook ------------------------------------------------------------------------


def test_the_preprocess_hook_sees_every_page_and_its_result_is_what_ocr_reads(tmp_path):
    path = tmp_path / "page.png"
    _text_page((300, 200), ["INVOICE"], 20).save(path)
    calls = []

    def grayscale(image: Image.Image, page_number: int) -> Image.Image:
        calls.append(page_number)
        return image.convert("L").convert("RGB").resize((150, 100))

    _ImageReader.seen = []
    acquire(path, AcquisitionOptions(backend=_ImageReader(), fallbacks=[],
                                     settings=OcrSettings(preprocess=grayscale)))

    assert calls == [1]
    assert _ImageReader.seen[0].size == (150, 100)


def test_the_hook_reaches_acquisition_from_process_options():
    from docket import OcrOptions, ProcessOptions
    from docket.options import resolve

    def hook(image, page_number):
        return image

    settings = resolve(ProcessOptions(ocr=OcrOptions(preprocess=hook, crop_photos=False, min_text_height=0))).acquisition.settings
    assert (settings.preprocess, settings.crop_photos, settings.min_text_height) == (hook, False, 0)


def test_the_hook_runs_once_per_page_however_often_it_is_read(tmp_path):
    path = tmp_path / "page.png"
    _text_page((300, 200), ["INVOICE"], 20).save(path)
    calls = []
    with DocumentSource(path, preprocess=lambda image, n: calls.append(n) or image) as source:
        page = source.page(1)
        page.image()
        page.image()
    assert calls == [1]


# ---- small text -------------------------------------------------------------------------------


@pytest.mark.parametrize("heights, min_height, expected", [
    ([10, 11, 9], 20, 3.0),       # capped
    ([15, 15, 16], 20, 2.0),      # to the 30 px target
    ([24, 26], 20, 1.0),          # already legible
    ([], 20, 1.0),                # nothing read: nothing to measure
    ([10, 10], 0, 1.0),           # disabled
])
def test_upscale_factor(heights, min_height, expected):
    assert upscale_factor(heights, min_height=min_height) == pytest.approx(expected)


SMALL_INVOICE = ["INVOICE INV-2026-0417", "Seller Acme Handels GmbH", "Date 15/03/2026",
                 "Subtotal 1500.00", "VAT 315.00", "Total 1815.00 EUR"]


@needs_tesseract
def test_small_text_is_reread_enlarged_and_reads_right(tmp_path):
    """At 11 px Tesseract misreads 15/03/2026 as 16/03/2028 and 315.00 as
    316.00 — digits changed, nothing flagged. Enlarged, it reads them right."""
    from docket.ocr.tesseract import TesseractBackend

    path = tmp_path / "small.png"
    _text_page((420, 260), SMALL_INVOICE, 11, origin=(12, 12), step=18).save(path)

    def read(min_height):
        settings = OcrSettings(min_text_height=min_height, deskew=False, detect_rotation=False)
        with DocumentSource(path) as source:
            return TesseractBackend(settings).recognize_page(source.page(1))

    as_is, enlarged = read(0), read(20)

    assert as_is.width == 420 and enlarged.width > 420
    assert enlarged.confidence >= as_is.confidence
    for value in ("15/03/2026", "315.00", "1815.00"):
        assert value in enlarged.text


@needs_tesseract
def test_legible_text_is_read_once(tmp_path, monkeypatch):
    from docket.ocr import tesseract as tesseract_module

    path = tmp_path / "big.png"
    _text_page((900, 500), SMALL_INVOICE[:3], 32).save(path)
    reads = []
    original = tesseract_module.TesseractBackend._read
    monkeypatch.setattr(tesseract_module.TesseractBackend, "_read",
                        lambda self, image, n: reads.append(image.size) or original(self, image, n))

    with DocumentSource(path) as source:
        tesseract_module.TesseractBackend(OcrSettings(deskew=False, detect_rotation=False)).recognize_page(source.page(1))

    assert reads == [(900, 500)]
