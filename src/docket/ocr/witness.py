"""Numbers an OCR reading vouches for, used to second-guess the vision model.

Only words the engine itself read at or above the confidence floor count — a
garbled reading must stay silent, never accuse a correct extraction.

Engines often split one printed amount across boxes ("8,480" ".00"). A
per-word parse misses both halves, so runs of touching boxes are also joined
and parsed, provided every word in the run clears the floor.
"""
from __future__ import annotations

from .. import amounts
from ..layout import PageLayout


def _parse(text: str) -> list[float]:
    found = []
    for match in amounts.MONEY_RE.finditer(text):
        value = amounts.parse_amount(match.group(1))
        if value is not None:
            found.append(value)
    return found


def confident_amounts(page: PageLayout | None, floor: float) -> list[float]:
    if page is None or not page.words:
        return []
    by_id = {w.id: w for w in page.words}
    out: list[float] = []
    for line in page.lines:
        words = [by_id[i] for i in line.word_ids if i in by_id]
        run: list[str] = []
        run_ok = True
        prev_x1: float | None = None
        for word in words:
            conf = word.confidence if word.confidence is not None else 1.0
            # A confident word always votes on its own — adjacency to garbage
            # must not silence it.
            if conf >= floor:
                out.extend(_parse(word.text))
            width_px = word.bbox.width * page.width
            gap_px = (word.bbox.x0 - prev_x1) * page.width if prev_x1 is not None else 0.0
            if run and gap_px > max(2.0, 0.3 * width_px):
                if len(run) > 1 and run_ok:
                    out.extend(_parse("".join(run)))
                run, run_ok = [], True
            run.append(word.text)
            run_ok = run_ok and conf >= floor
            prev_x1 = word.bbox.x1
        if len(run) > 1 and run_ok:
            out.extend(_parse("".join(run)))
    seen: set[float] = set()
    deduped = []
    for value in out:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


__all__ = ["confident_amounts"]
