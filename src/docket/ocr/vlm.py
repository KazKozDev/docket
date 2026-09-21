"""The vision-language model as an OCR backend of last resort.

It reads what Tesseract can't — faint scans, handwriting, odd fonts — but it
returns text only: no boxes, no confidence, and it has been observed
inventing digits to make totals add up. That is why it sits last in the
fallback chain and why the acquisition keeps the OCR reading it overruled as
an independent witness (see acquire.py and validate.py).
"""
from __future__ import annotations

from .. import config
from ..layout import PageLayout, text_only_page
from ..llm_client import LLMError, vision_transcribe
from .base import BackendStatus, Capabilities, OcrBackend, OcrError
from .source import PageSource


class VlmBackend(OcrBackend):
    name = "vlm"
    capabilities = Capabilities(
        confidence=False,
        word_coordinates=False,
        lines=False,
        tables=False,
        rotation=False,
        languages=None,
        inputs=["image"],
    )

    def availability(self) -> BackendStatus:
        # Reachability costs a network round trip, so it is found out on the
        # first page; here only the configuration is checked.
        if not config.VISION_MODEL:
            return BackendStatus(
                name=self.name, available=False, reason="DOCKET_VISION_MODEL is empty"
            )
        if config.LLM_PROVIDER == "openai" and not config.LLM_API_KEY:
            return BackendStatus(
                name=self.name,
                available=False,
                reason="DOCKET_LLM_PROVIDER=openai but DOCKET_LLM_API_KEY is not set",
            )
        return BackendStatus(name=self.name, available=True)

    def recognize_page(self, page: PageSource) -> PageLayout:
        image = page.image()
        try:
            text = vision_transcribe(page.image_png())
        except LLMError as exc:
            raise OcrError(f"vision model failed on page {page.number}: {exc}") from exc
        return text_only_page(
            page_number=page.number,
            text=text,
            backend=self.name,
            width=float(image.width),
            height=float(image.height),
        )


__all__ = ["VlmBackend"]
