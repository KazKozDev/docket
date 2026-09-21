"""PaddleOCR backend: optional install, result parsing, rotation, tables and
language mapping. Parsing is tested on recorded result shapes, so it runs
without PaddleOCR; one test drives the real engine when it is installed."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from docket.ocr import BackendUnavailable, DocumentSource, OcrSettings, UnknownLanguage, get_ocr_backend
from docket.ocr import paddle as paddle_module
from docket.ocr.languages import paddle_language
from docket.ocr.paddle import PaddleOCRBackend, page_confidence, words_from_result

ROOT = Path(__file__).resolve().parent.parent
HAS_PADDLE = importlib.util.find_spec("paddleocr") is not None


def _result(angle=0, size=(1400, 1000)):
    """The shape PaddleOCR 3.x returns with return_word_box=True."""
    return {
        "rec_texts": ["ATT. GEN. Fax:614-466-5087", "Total 12.00", "blurry"],
        "rec_scores": [0.96, 0.99, 0.30],
        "rec_boxes": np.array([[100, 80, 350, 100], [100, 120, 220, 140], [100, 160, 160, 180]]),
        "text_word": [
            ["ATT", ". ", "GEN", ". ", "Fax", ":", "614-466-5087"],
            ["Total", " ", "12.00"],
            ["blurry"],
        ],
        "text_word_boxes": [
            [np.array(b) for b in ([100, 80, 130, 100], [130, 80, 140, 100], [140, 80, 175, 100],
                                   [175, 80, 185, 100], [190, 80, 220, 100], [220, 80, 226, 100],
                                   [226, 80, 350, 100])],
            [np.array(b) for b in ([100, 120, 150, 140], [150, 120, 158, 140], [158, 120, 220, 140])],
            [np.array([100, 160, 160, 180])],
        ],
        "doc_preprocessor_res": {"angle": angle, "output_img": np.zeros((*size, 3), dtype=np.uint8)},
    }


class _FakeEngine:
    def __init__(self, result):
        self.result = result
        self.inputs = []

    def predict(self, image, **kwargs):
        self.inputs.append((image, kwargs))
        return [self.result]


@pytest.fixture
def fake_paddle(monkeypatch):
    """Route the backend to a scripted engine; availability says installed."""
    engines = {}

    def fake_engine(key, factory):
        import threading

        return engines.setdefault(key[0], (_FakeEngine(fake_engine.results[key[0]]), threading.Lock()))

    fake_engine.results = {"ocr": _result()}
    fake_engine.engines = engines
    monkeypatch.setattr(paddle_module, "_engine", fake_engine)
    monkeypatch.setattr(
        PaddleOCRBackend,
        "availability",
        lambda self: paddle_module.BackendStatus(name="paddle", available=True),
    )
    return fake_engine


def _page(tmp_path):
    path = tmp_path / "scan.png"
    Image.new("RGB", (1000, 1400), "white").save(path)
    return DocumentSource(path)


# ---- parsing ----------------------------------------------------------------


def test_tokens_join_into_words_at_whitespace():
    words, line_height = words_from_result(_result())
    assert [w.text for w in words] == ["ATT.", "GEN.", "Fax:614-466-5087", "Total", "12.00", "blurry"]
    att = words[0]
    assert (att.x0, att.x1) == (100, 140)  # "ATT" + ". " boxes
    assert {w.line_key for w in words[:3]} == {0}
    assert words[3].confidence == pytest.approx(0.99)
    assert line_height == 20


def test_lines_without_token_boxes_are_split_evenly():
    result = _result()
    result["text_word"] = None
    words, _ = words_from_result(result)
    total, amount = words[3], words[4]
    assert (total.text, amount.text) == ("Total", "12.00")
    assert total.x0 == 100 and amount.x1 == pytest.approx(220)
    assert total.x1 < amount.x0


def test_page_confidence_is_character_weighted():
    # 24 + 10 characters above the floor, 6 below.
    assert page_confidence(_result(), 0.6) == pytest.approx(34 / 40)


def test_backend_contract_on_a_scripted_engine(tmp_path, fake_paddle):
    backend = PaddleOCRBackend()
    with _page(tmp_path) as source:
        page = backend.recognize_page(source.page(1))
    assert page.backend == "paddle" and page.unit == "px"
    assert (page.width, page.height) == (1000, 1400)
    assert page.lines[1].text == "Total 12.00"
    assert all(0 <= w.bbox.x0 <= w.bbox.x1 <= 1 for w in page.words)
    assert page.confidence == pytest.approx(34 / 40)


def test_image_is_handed_over_as_bgr(tmp_path, fake_paddle):
    path = tmp_path / "red.png"
    Image.new("RGB", (40, 30), (255, 0, 0)).save(path)
    with DocumentSource(path) as source:
        PaddleOCRBackend().recognize_page(source.page(1))
    image, _ = fake_paddle.engines["ocr"][0].inputs[0]
    assert tuple(image[0, 0]) == (0, 0, 255)


@pytest.mark.parametrize("angle, rotation", [(0, 0), (90, 270), (180, 180), (270, 90)])
def test_orientation_angle_becomes_clockwise_rotation(tmp_path, fake_paddle, angle, rotation):
    fake_paddle.results["ocr"] = _result(angle=angle, size=(1400, 1000))
    with _page(tmp_path) as source:
        page = PaddleOCRBackend().recognize_page(source.page(1))
    assert page.rotation == rotation
    # Boxes live on the corrected image, whose size is reported.
    assert (page.width, page.height) == (1000, 1400)


def test_table_cells_become_a_backend_table(tmp_path, fake_paddle):
    words = {
        "rec_texts": ["Item", "Qty", "Seal kit", "12", "Pad", "6"],
        "rec_scores": [0.99] * 6,
        "rec_boxes": np.array([[110, 310, 160, 330], [410, 310, 450, 330],
                               [110, 350, 190, 370], [410, 350, 430, 370],
                               [110, 390, 150, 410], [410, 390, 420, 410]]),
        "doc_preprocessor_res": {"angle": 0, "output_img": np.zeros((1400, 1000, 3), dtype=np.uint8)},
    }
    fake_paddle.results["ocr"] = words
    fake_paddle.results["tables"] = {
        "table_res_list": [
            {"cell_box_list": [np.array(b, dtype=float) for b in (
                [100, 300, 400.4, 340], [400.6, 300, 600, 340],
                [100, 340, 400, 380], [400, 340.3, 600, 380],
                [100, 380, 400, 420], [400, 380, 600, 420],
            )]}
        ]
    }
    with _page(tmp_path) as source:
        page = PaddleOCRBackend(OcrSettings(paddle_tables=True)).recognize_page(source.page(1))
    assert len(page.tables) == 1
    table = page.tables[0]
    assert table.detection == "backend"
    assert table.grid() == [["Item", "Qty"], ["Seal kit", "12"], ["Pad", "6"]]
    assert "Seal kit | 12" in page.text


def test_engine_failure_is_an_ocr_error(tmp_path, fake_paddle, monkeypatch):
    from docket.ocr import OcrError

    class _Broken:
        def predict(self, *_a, **_k):
            raise RuntimeError("out of memory")

    import threading

    monkeypatch.setattr(paddle_module, "_engine", lambda key, factory: (_Broken(), threading.Lock()))
    with _page(tmp_path) as source, pytest.raises(OcrError, match="out of memory"):
        PaddleOCRBackend().recognize_page(source.page(1))


# ---- languages ----------------------------------------------------------------


@pytest.mark.parametrize(
    "languages, lang, model",
    [
        (["en"], "en", "en_PP-OCRv5_mobile_rec"),
        (["en", "de", "fr"], "de", "latin_PP-OCRv5_mobile_rec"),
        (["ru", "uk"], "ru", "eslav_PP-OCRv5_mobile_rec"),
        (["zh"], "ch", "PP-OCRv5_mobile_rec"),
    ],
)
def test_language_families(languages, lang, model):
    assert paddle_language(languages) == (lang, model)


def test_mixed_script_families_are_refused():
    with pytest.raises(UnknownLanguage, match="one script family"):
        paddle_language(["en", "ru"])
    status = PaddleOCRBackend(OcrSettings(languages=["en", "ru"])).availability()
    if HAS_PADDLE:
        assert not status.available and "script family" in status.reason


# ---- optional install ------------------------------------------------------------


def test_missing_package_is_diagnosed_with_install_command(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "paddleocr" else real(name, *a)
    )
    status = PaddleOCRBackend().availability()
    assert not status.available
    assert "'paddleocr' package is not installed" in status.reason
    assert 'docket-idp[paddle]' in status.install_hint
    with pytest.raises(BackendUnavailable, match=r"docket-idp\[paddle\]"):
        PaddleOCRBackend().require_available()


def test_docket_imports_and_runs_without_paddle():
    """Block paddle imports entirely: docket must import, list the backend
    as unavailable, and refuse an explicit selection with the install hint."""
    code = r"""
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in {"paddle", "paddleocr", "paddlex"}:
            raise ModuleNotFoundError(name)
        return None
sys.meta_path.insert(0, Block())
import importlib.util
importlib.util.find_spec = (lambda real: lambda n, *a: None if n.split(".")[0] in {"paddle", "paddleocr", "paddlex"} else real(n, *a))(importlib.util.find_spec)
import docket
from docket.ocr import list_ocr_backends, BackendUnavailable
from docket.pipeline import process_document
info = {i.name: i for i in list_ocr_backends()}["paddle"]
assert not info.status.available, info
try:
    from docket.options import OcrOptions, ProcessOptions
    process_document("x.png", ProcessOptions(ocr=OcrOptions(backend="paddle")))
except BackendUnavailable as exc:
    assert "docket-idp[paddle]" in str(exc), exc
    print("OK")
assert not any(m.split(".")[0] in {"paddle", "paddleocr", "paddlex"} for m in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().endswith("OK")


def test_registry_knows_paddle():
    assert isinstance(get_ocr_backend("paddle"), PaddleOCRBackend)


# ---- the real engine ---------------------------------------------------------------


@pytest.mark.paddle
@pytest.mark.skipif(not HAS_PADDLE, reason="PaddleOCR not installed")
def test_real_paddle_reads_a_rendered_invoice(tmp_path):
    import pymupdf as fitz

    pdf = tmp_path / "inv.pdf"
    with fitz.open() as document:
        page = document.new_page(width=595, height=842)
        for n, line in enumerate(["INVOICE INV-2026-0042", "Subtotal: EUR 2600.00", "Total: EUR 3146.00"]):
            page.insert_text((72, 90 + 24 * n), line, fontsize=13)
        document.save(pdf)
    with DocumentSource(pdf) as source:
        page = PaddleOCRBackend().recognize_page(source.page(1))
    assert "INV-2026-0042" in page.text and "Total: EUR 3146.00" in page.text
    assert page.confidence > 0.9
    total = next(line for line in page.lines if "3146.00" in line.text)
    assert 0.1 < total.bbox.x0 < 0.15


@pytest.mark.paddle
@pytest.mark.skipif(not HAS_PADDLE, reason="PaddleOCR not installed")
def test_real_paddle_turns_a_rotated_scan_upright(tmp_path):
    scan = tmp_path / "turned.png"
    Image.open(ROOT / "eval" / "real_samples" / "form_funsd_00.png").rotate(90, expand=True).save(scan)
    with DocumentSource(scan) as source:
        page = PaddleOCRBackend().recognize_page(source.page(1))
    assert page.rotation == 90
    assert "CONFIDENTIAL FACSIMILE" in page.text
    assert page.height > page.width
