"""PaddleOCR as an OCR backend (optional: `pip install "docket-idp[paddle]"`).

Importing this module never imports PaddleOCR; the engine loads on the first
page it reads, and `availability()` reports a missing install with the
command that fixes it.

What it uses from PaddleOCR 3.x:

- **Words and boxes**: `return_word_box=True` gives per-token boxes inside
  each recognized line; tokens are joined into words at whitespace. PaddleOCR
  scores whole lines, so every word carries its line's score.
- **Page confidence**: the character-weighted share of text in lines whose
  score clears the word floor — the same definition the Tesseract backend
  uses, so one `min_confidence` gate means the same thing for both.
- **Rotation**: the document-orientation classifier (0/90/180/270). Boxes
  are reported on the corrected image.
- **Tables** (`DOCKET_PADDLE_TABLES=true`): `TableRecognitionPipelineV2`,
  fed the OCR result already computed so the page isn't read twice. Its
  cell boxes become a backend table; cell text comes from the words inside.

Model files download on first use to `~/.paddlex/official_models` and are
reused offline after that.
"""
from __future__ import annotations

import importlib.util
import os
import re
import threading
from statistics import median

from ..layout import PageLayout, RawWord, TableHint, build_page, cells_from_boxes
from .base import BackendStatus, Capabilities, OcrBackend, OcrError
from .languages import UnknownLanguage, paddle_language
from .source import PageSource

INSTALL_HINT = 'Install it with `pip install "docket-idp[paddle]"`'
MOBILE_DET = "PP-OCRv5_mobile_det"

# One engine per configuration per process: loading models takes seconds.
# Paddle predictors are not documented as thread-safe, so each is guarded.
_ENGINES: dict[tuple, tuple[object, threading.Lock]] = {}
_ENGINES_LOCK = threading.Lock()


def _engine(key: tuple, factory):
    with _ENGINES_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = (factory(), threading.Lock())
        return _ENGINES[key]


def _words_of_line(tokens: list[str], boxes: list, line_key: int, score: float) -> list[RawWord]:
    """Join PaddleOCR's per-token boxes into whitespace-separated words."""
    words: list[RawWord] = []
    text, parts = "", []

    def flush() -> None:
        nonlocal text, parts
        if text.strip():
            words.append(
                RawWord(
                    text=text.strip(),
                    x0=min(float(b[0]) for b in parts),
                    y0=min(float(b[1]) for b in parts),
                    x1=max(float(b[2]) for b in parts),
                    y1=max(float(b[3]) for b in parts),
                    confidence=score,
                    line_key=line_key,
                )
            )
        text, parts = "", []

    for token, box in zip(tokens, boxes):
        for piece in re.split(r"(\s+)", token):
            if not piece:
                continue
            if piece.isspace():
                flush()
            else:
                text += piece
                parts.append(box)
    flush()
    return words


def _split_line_evenly(text: str, box, line_key: int, score: float) -> list[RawWord]:
    """Fallback when no token boxes came back: share the line box out by
    character count. Coarser, but keeps every word on its line."""
    tokens = text.split()
    total = sum(len(t) for t in tokens) + max(0, len(tokens) - 1)
    x0, y0, x1, y1 = (float(v) for v in box)
    step = (x1 - x0) / total if total else 0.0
    words, cursor = [], x0
    for token in tokens:
        end = cursor + step * len(token)
        words.append(RawWord(token, cursor, y0, end, y1, confidence=score, line_key=line_key))
        cursor = end + step
    return words


def words_from_result(result) -> tuple[list[RawWord], float]:
    """Words of every recognized line, and the median line height."""
    texts = list(result.get("rec_texts") or [])
    scores = list(result.get("rec_scores") or [])
    boxes = result.get("rec_boxes")
    boxes = [] if boxes is None else [list(b) for b in boxes]
    token_texts = result.get("text_word")
    token_boxes = result.get("text_word_boxes")
    words: list[RawWord] = []
    heights = []
    for n, (text, score, box) in enumerate(zip(texts, scores, boxes)):
        heights.append(float(box[3]) - float(box[1]))
        if token_texts and token_boxes and n < len(token_texts) and len(token_texts[n]) == len(token_boxes[n]):
            words.extend(_words_of_line(token_texts[n], [list(b) for b in token_boxes[n]], n, float(score)))
        else:
            words.extend(_split_line_evenly(text, box, n, float(score)))
    return words, median(heights) if heights else 0.0


def page_confidence(result, floor: float) -> float:
    total = good = 0
    for text, score in zip(result.get("rec_texts") or [], result.get("rec_scores") or []):
        chars = len(text.replace(" ", ""))
        total += chars
        if float(score) >= floor:
            good += chars
    return good / total if total else 0.0


class PaddleOCRBackend(OcrBackend):
    name = "paddle"
    capabilities = Capabilities(
        confidence=True,
        word_coordinates=True,
        lines=True,
        tables=True,
        rotation=True,
        languages=sorted(
            {"en", "cs", "da", "de", "es", "et", "fi", "fr", "ga", "hr", "hu", "it", "lt", "lv",
             "mt", "nl", "no", "pl", "pt", "ro", "sk", "sl", "sv", "tr", "ru", "uk", "bg", "el",
             "ar", "ko", "zh", "ja"}
        ),
        inputs=["image"],
    )

    def availability(self) -> BackendStatus:
        for module in ("paddleocr", "paddle"):
            if importlib.util.find_spec(module) is None:
                return BackendStatus(
                    name=self.name,
                    available=False,
                    reason=f"the {module!r} package is not installed",
                    install_hint=INSTALL_HINT,
                )
        try:
            paddle_language(self.settings.languages)
        except UnknownLanguage as exc:
            return BackendStatus(name=self.name, available=False, reason=str(exc))
        if self.settings.paddle_tables:
            from paddlex.utils.deps import is_extra_available

            if not is_extra_available("ocr"):
                return BackendStatus(
                    name=self.name,
                    available=False,
                    reason="table recognition needs PaddleX's OCR extras",
                    install_hint=INSTALL_HINT,
                )
        if self.settings.device.startswith("gpu"):
            import paddle

            if not paddle.device.is_compiled_with_cuda():
                return BackendStatus(
                    name=self.name,
                    available=False,
                    reason=f"device {self.settings.device!r} requested but this paddlepaddle build has no GPU support",
                    install_hint="Install a GPU build of paddlepaddle, or set DOCKET_PADDLE_DEVICE=cpu",
                )
        return BackendStatus(name=self.name, available=True)

    def _model_kwargs(self) -> dict:
        lang, mobile_rec = paddle_language(self.settings.languages)
        if self.settings.paddle_model == "mobile":
            return {"text_detection_model_name": MOBILE_DET, "text_recognition_model_name": mobile_rec}
        return {"lang": lang}

    def _ocr(self):
        # Skip PaddleX's network probe of model hosters: models already on
        # disk must load offline.
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        kwargs = self._model_kwargs()
        key = ("ocr", self.settings.device, self.settings.detect_rotation, tuple(sorted(kwargs.items())))

        def factory():
            from paddleocr import PaddleOCR

            return PaddleOCR(
                device=self.settings.device,
                use_doc_orientation_classify=self.settings.detect_rotation,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                return_word_box=True,
                **kwargs,
            )

        return _engine(key, factory)

    def _tables(self):
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        kwargs = self._model_kwargs()
        kwargs.pop("lang", None)
        key = ("tables", self.settings.device, tuple(sorted(kwargs.items())))

        def factory():
            from paddleocr import TableRecognitionPipelineV2

            return TableRecognitionPipelineV2(
                device=self.settings.device,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                **kwargs,
            )

        return _engine(key, factory)

    def recognize_page(self, page: PageSource) -> PageLayout:
        import numpy as np

        status = self.availability()
        if not status.available:
            raise OcrError(f"PaddleOCR unavailable: {status.reason}")
        bgr = np.ascontiguousarray(np.array(page.image())[:, :, ::-1])
        engine, lock = self._ocr()
        try:
            with lock:
                result = engine.predict(bgr)[0]
        except Exception as exc:  # noqa: BLE001 — engine errors surface as OcrError
            raise OcrError(f"PaddleOCR failed on page {page.number}: {exc}") from exc

        preprocessed = result.get("doc_preprocessor_res") or {}
        corrected = preprocessed.get("output_img") if preprocessed else None
        height, width = (corrected if corrected is not None else bgr).shape[:2]
        # PaddleOCR reports the counter-clockwise angle the page sits at;
        # PageLayout.rotation is the clockwise turn applied to fix it.
        angle = int(preprocessed.get("angle", 0) or 0) if preprocessed else 0
        rotation = (360 - angle) % 360

        words, line_height = words_from_result(result)
        hints: list[TableHint] = []
        if self.settings.paddle_tables and words:
            hints = self._table_hints(corrected if corrected is not None else bgr, result, line_height)

        return build_page(
            page_number=page.number,
            width=float(width),
            height=float(height),
            unit="px",
            rotation=rotation,
            backend=self.name,
            confidence=page_confidence(result, self.settings.word_confidence_floor),
            words=words,
            table_hints=hints,
        )

    def _table_hints(self, image, ocr_result, line_height: float) -> list[TableHint]:
        pipeline, lock = self._tables()
        try:
            with lock:
                table_result = pipeline.predict(image, use_ocr_model=False, overall_ocr_res=ocr_result)[0]
        except Exception as exc:  # noqa: BLE001
            raise OcrError(f"PaddleOCR table recognition failed: {exc}") from exc
        hints = []
        tolerance = max(3.0, 0.4 * line_height)
        for table in table_result.get("table_res_list") or []:
            boxes = [tuple(float(v) for v in b[:4]) for b in table.get("cell_box_list") or []]
            cells = cells_from_boxes(boxes, tolerance)
            if len({c.row for c in cells}) < 2 or len({c.column for c in cells}) < 2:
                continue
            hints.append(
                TableHint(
                    x0=min(b[0] for b in boxes),
                    y0=min(b[1] for b in boxes),
                    x1=max(b[2] for b in boxes),
                    y1=max(b[3] for b in boxes),
                    cells=cells,
                    detection="backend",
                )
            )
        return hints


__all__ = ["PaddleOCRBackend", "page_confidence", "words_from_result"]
