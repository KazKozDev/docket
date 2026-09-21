"""Language detection without a model.

`detect_language` picks between the languages this pipeline actually has
rules for, by counting function words. Function words are the right signal
because they are frequent, short, and almost never appear in the other
language — unlike content words, which a bilingual Barcelona invoice is
full of. No dependency, no model, microseconds.

A NEGATIVE RESULT, kept here because it's the useful part: a
"gibberish ratio" was also implemented, to give contracts the OCR-quality
signal that the amount-legibility check can only provide for documents with
money on them. The idea was that OCR failures produce vowelless tokens
("sistz", "sus TTA"). Measured against real Tesseract output from a badly
read invoice, it scored 0.000 — identical to clean text, because Tesseract's
failures are mostly pronounceable junk. The metric discriminated nothing and
was removed rather than shipped as false reassurance. Detecting garbled
prose still needs a real signal: a word list, character n-gram
plausibility, or per-word OCR confidence used better than an average.
"""
from __future__ import annotations

import re

_STOPWORDS = {
    "en": {
        "the",
        "and",
        "of",
        "to",
        "in",
        "for",
        "is",
        "are",
        "be",
        "with",
        "this",
        "that",
        "shall",
        "from",
        "by",
        "on",
        "as",
        "at",
        "or",
        "an",
    },
    "es": {
        "de",
        "la",
        "el",
        "los",
        "las",
        "y",
        "en",
        "que",
        "por",
        "con",
        "del",
        "para",
        "se",
        "un",
        "una",
        "al",
        "es",
        "son",
        "su",
        "lo",
    },
}

_WORD_RE = re.compile(r"[a-záéíóúüñçàèòïA-ZÁÉÍÓÚÜÑÇÀÈÒÏ]+")


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def detect_language(text: str) -> tuple[str, float]:
    """Returns (language code, confidence 0-1).

    Confidence is the winning language's share of the stopword hits, so a
    document with no hits at all reports ("unknown", 0.0) rather than
    guessing — which matters, because the caller uses this to decide whether
    to trust language-specific rules.
    """
    words = _words(text)
    if not words:
        return "unknown", 0.0

    hits = {
        lang: sum(1 for w in words if w in stops) for lang, stops in _STOPWORDS.items()
    }
    total = sum(hits.values())
    if total == 0:
        return "unknown", 0.0

    best = max(hits.items(), key=lambda kv: kv[1])
    return best[0], round(best[1] / total, 2)
