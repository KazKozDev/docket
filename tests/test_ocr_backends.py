"""OCR backends: the shared contract, each built-in engine on real input, and
the registry (built-ins, programmatic registration, entry-point plugins)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image

from docket import config, llm_client
from docket.layout import PageLayout
from docket.ocr import (
    BackendStatus,
    BackendUnavailable,
    Capabilities,
    DocumentSource,
    OcrBackend,
    OcrBackendError,
    OcrSettings,
    UnknownLanguage,
    get_ocr_backend,
    list_ocr_backends,
    parse_languages,
    register_ocr_backend,
    unregister_ocr_backend,
)
from docket.ocr import registry as registry_module
from docket.ocr.pdftext import PDFTextBackend, text_layer_problem
from docket.ocr.tesseract import TesseractBackend
from docket.ocr.vlm import VlmBackend

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_tesseract = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract binary not installed")

INVOICE_LINES = [
    "INVOICE INV-2026-0042",
    "Vendor: Northgate Supplies Ltd",
    "Customer: Iberia Mantenimiento SA",
    "Subtotal: EUR 2600.00",
    "Total: EUR 3146.00",
]


def _text_pdf(path: Path, *, rotate: int = 0, table: bool = False) -> Path:
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        for n, line in enumerate(INVOICE_LINES):
            page.insert_text((72, 90 + 24 * n), line, fontsize=13)
        if table:
            x = [72, 300, 400, 520]
            y = [300, 330, 360, 390]
            for yy in y:
                page.draw_line((x[0], yy), (x[-1], yy))
            for xx in x:
                page.draw_line((xx, y[0]), (xx, y[-1]))
            cells = [["Item", "Qty", "Amount"], ["Seal kit", "12", "540.00"], ["Brake pad", "6", "720.00"]]
            for r, row in enumerate(cells):
                for c, text in enumerate(row):
                    page.insert_text((x[c] + 6, y[r] + 20), text, fontsize=11)
        if rotate:
            page.set_rotation(rotate)
        document.save(path)
    return path


def _scan(tmp_path: Path, *, rotate: int = 0) -> Path:
    """A 200-dpi raster of the text PDF — what a scanner would produce."""
    pdf = _text_pdf(tmp_path / "source.pdf")
    with DocumentSource(pdf, dpi=200) as source:
        image = source.page(1).image()
    if rotate:
        image = image.rotate(rotate, expand=True)
    path = tmp_path / "scan.png"
    image.save(path)
    return path


def _assert_contract(backend: OcrBackend, page: PageLayout, number: int = 1) -> None:
    assert isinstance(page, PageLayout)
    assert page.page_number == number
    assert page.backend == backend.name
    assert page.width > 0 and page.height > 0
    assert page.text.strip()
    caps = backend.capabilities
    if caps.word_coordinates:
        assert page.words, "a backend claiming word coordinates returned none"
        for word in page.words:
            assert 0.0 <= word.bbox.x0 <= word.bbox.x1 <= 1.0
            assert 0.0 <= word.bbox.y0 <= word.bbox.y1 <= 1.0
            assert word.page == number
    else:
        assert page.words == []
    if caps.confidence:
        assert page.confidence is not None and 0.0 <= page.confidence <= 1.0
        assert all(w.confidence is not None for w in page.words)
    if caps.lines:
        assert page.lines


# ---- contract, per backend ---------------------------------------------------


def test_pdf_text_backend_contract(tmp_path):
    backend = PDFTextBackend()
    with DocumentSource(_text_pdf(tmp_path / "doc.pdf")) as source:
        page = backend.recognize_page(source.page(1))
    _assert_contract(backend, page)
    assert page.unit == "pt" and (page.width, page.height) == (595, 842)
    assert page.lines[0].text == "INVOICE INV-2026-0042"
    assert text_layer_problem(page) is None


@needs_tesseract
def test_tesseract_backend_contract(tmp_path):
    backend = TesseractBackend()
    with DocumentSource(_scan(tmp_path)) as source:
        page = backend.recognize_page(source.page(1))
    _assert_contract(backend, page)
    assert page.unit == "px"
    assert "INV-2026-0042" in page.text
    assert "3146.00" in page.text
    assert page.confidence > 0.6


def test_vlm_backend_contract(tmp_path, monkeypatch):
    sent: list[bytes] = []
    monkeypatch.setattr(
        "docket.ocr.vlm.vision_transcribe", lambda png: sent.append(png) or "Total: EUR 3146.00"
    )
    backend = VlmBackend()
    image = tmp_path / "page.png"
    Image.new("RGB", (300, 200), "white").save(image)
    with DocumentSource(image) as source:
        page = backend.recognize_page(source.page(1))
    _assert_contract(backend, page)
    assert sent and sent[0].startswith(b"\x89PNG")  # rendered in memory, no temp file
    assert list(tmp_path.iterdir()) == [image]


def test_every_backend_declares_its_capabilities():
    for info in list_ocr_backends():
        if info.capabilities is not None:
            assert isinstance(info.capabilities, Capabilities)
        assert isinstance(info.status, BackendStatus)


# ---- PDF text layer -------------------------------------------------------------


def test_pdf_ruled_table_is_structured(tmp_path):
    with DocumentSource(_text_pdf(tmp_path / "t.pdf", table=True)) as source:
        page = PDFTextBackend().recognize_page(source.page(1))
    assert len(page.tables) == 1
    table = page.tables[0]
    assert table.detection == "ruled"
    assert table.grid() == [["Item", "Qty", "Amount"], ["Seal kit", "12", "540.00"], ["Brake pad", "6", "720.00"]]
    assert "[TABLE 1: 3 rows x 3 columns]" in page.text
    assert "Seal kit | 12 | 540.00" in page.text


@pytest.mark.parametrize("rotate, fix", [(90, 270), (180, 180), (270, 90)])
def test_pdf_rotated_page_is_read_upright(tmp_path, rotate, fix):
    with DocumentSource(_text_pdf(tmp_path / "r.pdf", rotate=rotate)) as source:
        page = PDFTextBackend().recognize_page(source.page(1))
    assert page.rotation == fix
    assert (page.width, page.height) == (595, 842)
    assert page.lines[0].text == "INVOICE INV-2026-0042"
    assert page.lines[-1].text == "Total: EUR 3146.00"


def test_unmapped_glyph_text_layer_is_rejected():
    from tests.factories import words_page

    page = words_page(["(cid:12)(cid:13)(cid:14) (cid:15)(cid:16)"], backend="pdf_text")
    assert "unmapped glyph" in text_layer_problem(page)


def test_sparse_text_layer_is_rejected():
    from tests.factories import words_page

    assert "characters" in text_layer_problem(words_page(["p. 1"], backend="pdf_text"))


# ---- Tesseract ---------------------------------------------------------------


@needs_tesseract
@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_tesseract_detects_and_corrects_rotation(tmp_path, rotate):
    with DocumentSource(_scan(tmp_path, rotate=rotate)) as source:
        page = TesseractBackend().recognize_page(source.page(1))
    # PIL turns counter-clockwise, so the fix is the same angle clockwise.
    assert page.rotation == rotate
    assert "INV-2026-0042" in page.text
    assert page.width < page.height  # back to portrait


@needs_tesseract
def test_tesseract_rotation_detection_can_be_disabled(tmp_path):
    settings = OcrSettings(detect_rotation=False)
    with DocumentSource(_scan(tmp_path, rotate=90)) as source:
        page = TesseractBackend(settings).recognize_page(source.page(1))
    assert page.rotation == 0


def test_tesseract_missing_binary_is_diagnosed(monkeypatch):
    monkeypatch.setattr("pytesseract.pytesseract.tesseract_cmd", "/nonexistent/tesseract")
    status = TesseractBackend().availability()
    assert not status.available
    assert "not on PATH" in status.reason and "install" in status.install_hint.lower()


@needs_tesseract
def test_tesseract_missing_language_is_diagnosed(monkeypatch):
    monkeypatch.setattr("pytesseract.get_languages", lambda config="": ["eng", "osd"])
    status = TesseractBackend(OcrSettings(languages=["en", "de"])).availability()
    assert not status.available and "deu" in status.reason


def test_multiframe_tiff_is_multipage(tmp_path):
    path = tmp_path / "two.tiff"
    frames = [Image.new("RGB", (100, 100), c) for c in ("white", "black")]
    frames[0].save(path, save_all=True, append_images=frames[1:])
    with DocumentSource(path) as source:
        assert source.page_count == 2
        assert source.page(2).image().getpixel((5, 5)) == (0, 0, 0)


# ---- VLM availability ------------------------------------------------------------


def test_vlm_needs_a_key_for_openai_compatible_providers(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "LLM_API_KEY", None)
    status = VlmBackend().availability()
    assert not status.available and "DOCKET_LLM_API_KEY" in status.reason


def test_vlm_error_surfaces_as_ocr_error(tmp_path, monkeypatch):
    from docket.ocr import OcrError

    def boom(_png):
        raise llm_client.LLMError("timed out")

    monkeypatch.setattr("docket.ocr.vlm.vision_transcribe", boom)
    image = tmp_path / "p.png"
    Image.new("RGB", (50, 50), "white").save(image)
    with DocumentSource(image) as source, pytest.raises(OcrError, match="timed out"):
        VlmBackend().recognize_page(source.page(1))


# ---- languages ----------------------------------------------------------------


def test_languages_are_iso_codes():
    assert parse_languages("en, de") == ["en", "de"]
    assert parse_languages(["FR"]) == ["fr"]
    with pytest.raises(UnknownLanguage, match="eng"):
        parse_languages("eng+deu")


# ---- registry and plugins --------------------------------------------------------


class _EchoBackend(OcrBackend):
    name = "echo"
    capabilities = Capabilities(
        confidence=False, word_coordinates=False, lines=False, tables=False, rotation=False
    )

    def availability(self):
        return BackendStatus(name=self.name, available=True)

    def recognize_page(self, page):
        from docket.layout import text_only_page

        return text_only_page(page_number=page.number, text="echo", backend=self.name)


def test_builtins_are_registered():
    names = {info.name for info in list_ocr_backends()}
    assert {"pdf_text", "tesseract", "vlm"} <= names


def test_unknown_backend_lists_the_known_ones():
    with pytest.raises(OcrBackendError, match="available: .*tesseract"):
        get_ocr_backend("nope")


def test_programmatic_registration_round_trip():
    register_ocr_backend("echo", _EchoBackend)
    try:
        backend = get_ocr_backend("echo", OcrSettings(languages=["de"]))
        assert isinstance(backend, _EchoBackend) and backend.settings.languages == ["de"]
        with pytest.raises(OcrBackendError, match="already registered"):
            register_ocr_backend("echo", _EchoBackend)
        register_ocr_backend("echo", _EchoBackend, replace=True)
    finally:
        unregister_ocr_backend("echo")


@pytest.mark.parametrize("name, message", [("tesseract", "built-in"), ("Bad Name", "snake_case"), ("auto", "reserved")])
def test_registration_errors(name, message):
    with pytest.raises(OcrBackendError, match=message):
        register_ocr_backend(name, _EchoBackend)


def test_entry_point_plugin_is_discovered(monkeypatch):
    class _EP:
        name = "plugin_echo"
        value = f"{__name__}:_EchoBackend"

    monkeypatch.setattr(registry_module, "_plugins_loaded", False)
    monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._BUILTIN))
    monkeypatch.setattr(
        registry_module, "entry_points", lambda group: [_EP()] if group == "docket.ocr_backends" else []
    )
    backend = get_ocr_backend("plugin_echo")
    assert isinstance(backend, _EchoBackend)
    assert "plugin_echo" in {i.name for i in list_ocr_backends()}


def test_plugin_that_fails_to_import_is_reported_unavailable(monkeypatch):
    class _EP:
        name = "broken"
        value = "no_such_module_anywhere:Backend"

    monkeypatch.setattr(registry_module, "_plugins_loaded", False)
    monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._BUILTIN))
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_EP()])
    with pytest.raises(BackendUnavailable, match="cannot import no_such_module_anywhere"):
        get_ocr_backend("broken")
    info = next(i for i in list_ocr_backends() if i.name == "broken")
    assert info.status.available is False


def test_backend_object_passes_straight_through_the_pipeline(tmp_path, monkeypatch):
    from docket import pipeline

    monkeypatch.setattr(pipeline, "_run_once", lambda path, doc_id, acq, **k: pytest.fail("not reached") if not acq else _capture(acq))
    captured = {}

    def _capture(acq):
        captured["text"] = acq.layout.pages[0].text
        from tests.factories import make_result

        return make_result(source=str(tmp_path / "x.png"))

    image = tmp_path / "x.png"
    Image.new("RGB", (50, 50), "white").save(image)
    pipeline.process_document(image, ocr_backend=_EchoBackend(), ocr_fallbacks=[], enqueue_review=False)
    assert captured["text"] == "echo"
