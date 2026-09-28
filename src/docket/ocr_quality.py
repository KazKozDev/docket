"""Asking a cheap text model whether the OCR is garbage.

This is the one place in the pipeline where a model is allowed near a
quality judgement, and the distinction matters. The rule this project
argues for is that a validator must never be an LLM, because an LLM asked
to check an LLM's output agrees with it.

What happens here is a different question. There is no extraction yet for
the model to agree with: it reads a Tesseract artifact and judges the
artifact. It also never writes into the result or overrides a
deterministic check — it only decides whether to spend a vision call. A
false positive costs one unnecessary re-read; a false negative leaves the
status quo. That is a safe place for a model.

Why it exists at all: the free deterministic signal (an amount label with
no readable amount behind it) only works on documents with money on them.
A contract has no arithmetic to fail, so nothing triggered a re-read no
matter how badly it was scanned. Word lists and language detection do not
help: Tesseract's mistakes land on real words, and "sus TOTAL $1250" is
nonsense only in context, which is what a language model reads. It only
runs on OCR-derived text after the free deterministic check has said
nothing.
"""
from __future__ import annotations

from . import config
from .llm_client import (
    LLMError,
    chat_json,
)
from .logging_setup import get_logger

log = get_logger()

# The question has to be the decision, not the symptom. Asking "did OCR
# garble this?" flags every stray quote mark, and a first version did: it
# fired on two scans that extract perfectly today, which would have bought a
# vision call for nothing on every run. What the pipeline needs to know is
# narrower — will a *value* come out wrong.
_PROMPT = """You are judging whether text from a scanned document is still usable.

Almost every OCR output has small blemishes: a stray quote, a missing full
stop, an odd capital. Those do not matter — a reader still recovers the
facts.

Answer "unusable" ONLY if a key value would be read WRONG or could not be
read at all: a name, a date, an amount, an identifier. Amounts turned into
words ("sus TOTAL", an amount rendered as "sistz"), or lines dissolved into
nonsense, are unusable. Cosmetic noise around otherwise-legible facts is not.

Respond with JSON:
{{"unusable": true|false, "confidence": 0-1, "evidence": "<=15 words"}}

Text:
---
{text}
---"""


def looks_garbled(text: str) -> bool:
    """True when a cheap model thinks this text is a bad OCR read.

    Returns False on any failure — an unavailable judge must not stall a
    document, and the deterministic checks downstream still run either way.
    """
    if not config.OCR_QUALITY_CHECK or not text.strip():
        return False

    chunks = [text[i : i + 3000] for i in range(0, len(text), 3000)]
    for chunk_number, chunk in enumerate(chunks, start=1):
        try:
            result = chat_json(_PROMPT.format(text=chunk))
        except LLMError as exc:
            log.warning(
                "OCR quality check unavailable",
                extra={"error_type": type(exc).__name__, "chunk": chunk_number},
            )
            continue

        unusable = bool(result.get("unusable"))
        confidence = float(result.get("confidence") or 0.0)
        if unusable:
            log.info(
                "OCR judged unusable",
                extra={
                    "confidence": confidence,
                    "chunk": chunk_number,
                },
            )
        if unusable and confidence >= config.OCR_QUALITY_MIN_CONFIDENCE:
            return True
    return False
