"""Find where a verbatim quote sits on a page: the words, their box, and how
sure the match is.

The extraction model only ever returns `page` and `quote`; geometry is
computed here, deterministically, from the backend's word boxes. A model is
never asked for coordinates, so it cannot invent them.

Matching runs on a whitespace-free, case-folded character stream of the
page's words. That makes it indifferent to how a backend tokenized the text
("8,480" ".00" vs "8,480.00") and to the ` | ` column markers the serializer
adds. An exact hit scores 1.0; otherwise the best window around the longest
common run must reach `MIN_FUZZY_SCORE`, or no location is reported.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import BoundingBox, PageLayout

MIN_FUZZY_SCORE = 0.8
MIN_ANCHOR_CHARS = 3


@dataclass(frozen=True)
class LocatedQuote:
    bbox: BoundingBox
    word_ids: list[str]
    match_score: float
    confidence: float


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if not ch.isspace() and ch != "|")


def _reading_order(page: PageLayout) -> list[int]:
    index = {w.id: n for n, w in enumerate(page.words)}
    order = [index[wid] for line in page.lines for wid in line.word_ids if wid in index]
    seen = set(order)
    return order + [n for n in range(len(page.words)) if n not in seen]


def locate_quote(quote: str, page: PageLayout) -> LocatedQuote | None:
    """Locate `quote` among the page's words; None if the page has no word
    geometry or the quote isn't there."""
    target = _norm(quote)
    if not target or not page.words:
        return None
    stream_chars: list[str] = []
    owner: list[int] = []
    for n in _reading_order(page):
        for ch in _norm(page.words[n].text):
            stream_chars.append(ch)
            owner.append(n)
    stream = "".join(stream_chars)
    if not stream:
        return None

    start = stream.find(target)
    if start >= 0:
        end, score = start + len(target), 1.0
    else:
        matcher = SequenceMatcher(None, stream, target, autojunk=False)
        a, b, size = matcher.find_longest_match(0, len(stream), 0, len(target))
        if size < MIN_ANCHOR_CHARS:
            return None
        start = max(0, a - b)
        end = min(len(stream), start + len(target))
        score = SequenceMatcher(None, stream[start:end], target, autojunk=False).ratio()
        if score < MIN_FUZZY_SCORE:
            return None

    members = sorted(set(owner[start:end]), key=owner[start:end].index)
    words = [page.words[n] for n in members]
    confidences = [w.confidence for w in words if w.confidence is not None]
    reading = sum(confidences) / len(confidences) if confidences else 1.0
    return LocatedQuote(
        bbox=BoundingBox.union(w.bbox for w in words),
        word_ids=[w.id for w in words],
        match_score=round(score, 4),
        confidence=round(score * reading, 4),
    )


__all__ = ["LocatedQuote", "locate_quote"]
