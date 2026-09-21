"""Rebuild lines, blocks, columns, tables and reading order from word boxes.

Every backend that reports word geometry (PDF text layer, Tesseract,
PaddleOCR) funnels through `build_page`, so the same page read by two engines
gets the same structure rules. The rules are pure geometry — no keywords, no
vendor templates — and deliberately conservative:

- **Rows**: words whose boxes overlap vertically by at least 40 % of the
  smaller height share a row.
- **Segments**: a horizontal gap wider than `GAP_FACTOR` × the page's median
  word height splits a row. Serialized, that gap becomes ` | `, which keeps
  quantities from sliding into neighbouring columns.
- **Aligned tables**: two or more consecutive rows with ≥3 segments whose
  segments line up in ≥3 shared column bands. Wrapped cell text on a row with
  fewer segments is not merged back into the cell above.
- **Ruled tables**: supplied by the backend (e.g. pdfplumber's cell borders);
  they win over aligned detection for the rows they cover.
- **Text columns**: a vertical gutter free of ink across ≥4 consecutive rows,
  with substantial text (median ≥12 characters, ≥20 % of the page width) on
  both sides. The text-heavy requirement is what keeps a label/value block
  ("Invoice no:    123") from being read as two columns of labels and values.
  Within such a region the reading order is column-major.

Known limits are recorded in docs/ARCHITECTURE.md ("Layout analysis").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .models import (
    BoundingBox,
    Column,
    PageLayout,
    Table,
    TableCell,
    TextBlock,
    TextLine,
    Unit,
    WordToken,
)

# Segment gap, in multiples of the page's median word height. Ordinary word
# spacing is ~0.3 h; a column gap in an invoice table is several h.
GAP_FACTOR = 1.5
ROW_OVERLAP = 0.4
MIN_TABLE_COLUMNS = 3
MIN_TABLE_ROWS = 2
MIN_COLUMN_ROWS = 4
MIN_COLUMN_TEXT_CHARS = 12
MIN_COLUMN_WIDTH_SHARE = 0.20
# Rows further apart than this many median line heights break a table run.
TABLE_ROW_GAP = 2.5
BLOCK_LINE_GAP = 1.0


@dataclass
class RawWord:
    """A word as a backend read it, in the page's absolute units."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float | None = None
    # Backend's own line identity (e.g. Tesseract block/paragraph/line).
    line_key: object | None = None

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class CellHint:
    row: int
    column: int
    x0: float
    y0: float
    x1: float
    y1: float
    row_span: int = 1
    column_span: int = 1
    text: str | None = None  # None: take the words whose centers fall inside


@dataclass
class TableHint:
    """A table the backend found itself (ruled PDF table, engine structure)."""

    x0: float
    y0: float
    x1: float
    y1: float
    cells: list[CellHint]
    detection: str = "ruled"


def _cluster(values: list[float], tolerance: float) -> list[float]:
    """Representative positions of values that lie within `tolerance` of
    their neighbour (sorted); each cluster is represented by its mean."""
    clusters: list[list[float]] = []
    for v in sorted(values):
        if clusters and v - clusters[-1][-1] <= tolerance:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c) / len(c) for c in clusters]


def cells_from_boxes(
    boxes: list[tuple[float, float, float, float]], tolerance: float
) -> list[CellHint]:
    """Grid positions and spans for table cells known only by their boxes.

    Cell edges closer than `tolerance` are one grid line; a cell spans the
    grid lines between its edges. Rows and columns are numbered densely by
    the cells that start there.
    """
    if not boxes:
        return []
    xs = _cluster([v for b in boxes for v in (b[0], b[2])], tolerance)
    ys = _cluster([v for b in boxes for v in (b[1], b[3])], tolerance)

    def nearest(lines: list[float], v: float) -> int:
        return min(range(len(lines)), key=lambda k: abs(lines[k] - v))

    cells = []
    for x0, y0, x1, y1 in boxes:
        c0, c1 = nearest(xs, x0), nearest(xs, x1)
        r0, r1 = nearest(ys, y0), nearest(ys, y1)
        cells.append(
            CellHint(
                row=r0, column=c0,
                row_span=max(1, r1 - r0), column_span=max(1, c1 - c0),
                x0=x0, y0=y0, x1=x1, y1=y1,
            )
        )
    rows = sorted({c.row for c in cells})
    cols = sorted({c.column for c in cells})
    for cell in cells:
        cell.row, cell.column = rows.index(cell.row), cols.index(cell.column)
    return cells


@dataclass
class _Row:
    words: list[int]  # indices into the page's word list, left to right
    y0: float
    y1: float
    segments: list[list[int]] = field(default_factory=list)

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class _Line:
    words: list[int]
    text: str
    column: int | None = None
    table: int | None = None  # index into the page's table list


def _group_rows(words: list[RawWord]) -> list[_Row]:
    """Rows from vertical overlap, respecting the backend's own line grouping.

    Words sharing a `line_key` stay together: an engine that tracked a
    skewed line knows better than a straight horizontal band does. Line
    units then merge into rows only when their median bands overlap and
    their x-ranges don't, so side-by-side cells join a row while two skewed
    lines that brush against each other never interleave.
    """
    units: dict[object, list[int]] = {}
    for i, w in enumerate(words):
        units.setdefault(w.line_key if w.line_key is not None else ("word", i), []).append(i)

    def band(indices: list[int]) -> tuple[float, float]:
        return median(words[i].y0 for i in indices), median(words[i].y1 for i in indices)

    ordered = sorted(units.values(), key=lambda u: (sum(band(u)) / 2, min(words[i].x0 for i in u)))
    rows: list[_Row] = []
    for unit in ordered:
        y0, y1 = band(unit)
        lo, hi = _extent(unit, words)
        placed = False
        for row in reversed(rows[-3:]):
            overlap = min(row.y1, y1) - max(row.y0, y0)
            if overlap < ROW_OVERLAP * min(row.height or (y1 - y0), (y1 - y0) or row.height):
                continue
            if any(words[i].x0 < hi and lo < words[i].x1 for i in row.words):
                continue
            row.words.extend(unit)
            row.y0, row.y1 = band(row.words)
            placed = True
            break
        if not placed:
            rows.append(_Row(words=list(unit), y0=y0, y1=y1))
    for row in rows:
        row.words.sort(key=lambda i: words[i].x0)
    rows.sort(key=lambda r: (r.y0 + r.y1) / 2)
    return rows


def _segment(row: _Row, words: list[RawWord], gap: float) -> list[list[int]]:
    segments: list[list[int]] = []
    for i in row.words:
        if segments and words[i].x0 - words[segments[-1][-1]].x1 <= gap:
            segments[-1].append(i)
        else:
            segments.append([i])
    return segments


def _extent(indices: list[int], words: list[RawWord]) -> tuple[float, float]:
    return min(words[i].x0 for i in indices), max(words[i].x1 for i in indices)


def _segment_text(indices: list[int], words: list[RawWord]) -> str:
    return " ".join(words[i].text for i in indices)


def _merge_bands(bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for lo, hi in sorted(bands):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def _aligned_columns(
    rows: list[_Row], words: list[RawWord]
) -> list[tuple[float, float]] | None:
    """Column bands shared by these rows, or None if any row puts two of its
    segments into one band (the rows don't really line up)."""
    bands = _merge_bands(
        [_extent(seg, words) for row in rows for seg in row.segments]
    )
    for row in rows:
        seen: set[int] = set()
        for seg in row.segments:
            lo, hi = _extent(seg, words)
            band = next(k for k, (blo, bhi) in enumerate(bands) if blo <= lo and hi <= bhi)
            if band in seen:
                return None
            seen.add(band)
    return bands


def _find_aligned_tables(
    rows: list[_Row], words: list[RawWord], line_height: float, claimed: set[int]
) -> list[tuple[list[int], list[tuple[float, float]]]]:
    """Runs of row indices that form a table, with their column bands."""
    tables = []
    k = 0
    while k < len(rows):
        if k in claimed or len(rows[k].segments) < MIN_TABLE_COLUMNS:
            k += 1
            continue
        run = [k]
        bands = _aligned_columns([rows[k]], words)
        j = k + 1
        while j < len(rows) and j not in claimed:
            if rows[j].y0 - rows[run[-1]].y1 > TABLE_ROW_GAP * line_height:
                break
            if len(rows[j].segments) < 2:
                break
            candidate = _aligned_columns([rows[r] for r in run] + [rows[j]], words)
            if candidate is None or len(candidate) < MIN_TABLE_COLUMNS:
                break
            run.append(j)
            bands = candidate
            j += 1
        full_rows = sum(1 for r in run if len(rows[r].segments) >= MIN_TABLE_COLUMNS)
        if bands is not None and len(run) >= MIN_TABLE_ROWS and full_rows >= MIN_TABLE_ROWS:
            tables.append((run, bands))
            k = j
        else:
            k += 1
    return tables


@dataclass
class _Gutter:
    lo: float
    hi: float
    start: int
    end: int  # exclusive


def _free_interval(row: _Row, words: list[RawWord], lo: float, hi: float) -> tuple[float, float] | None:
    """Shrink [lo, hi] to the ink-free part of this row around it."""
    for i in row.words:
        w = words[i]
        if w.x1 <= lo or w.x0 >= hi:
            continue
        # A word inside the band: keep the wider free side of it.
        left, right = (lo, w.x0), (w.x1, hi)
        lo, hi = left if left[1] - left[0] >= right[1] - right[0] else right
        if hi <= lo:
            return None
    return lo, hi


def _find_gutters(
    rows: list[_Row],
    words: list[RawWord],
    page_width: float,
    min_gutter: float,
    skip: set[int],
) -> list[_Gutter]:
    accepted: list[_Gutter] = []
    active: list[_Gutter] = []

    def close(g: _Gutter) -> None:
        span = [r for r in range(g.start, g.end) if r not in skip]
        if len(span) < MIN_COLUMN_ROWS:
            return
        if not (0.1 * page_width < (g.lo + g.hi) / 2 < 0.9 * page_width):
            return
        left_parts, right_parts = [], []
        both = 0
        for r in span:
            left = [i for i in rows[r].words if words[i].x1 <= g.lo]
            right = [i for i in rows[r].words if words[i].x0 >= g.hi]
            if left:
                left_parts.append(left)
            if right:
                right_parts.append(right)
            both += bool(left and right)
        if both < 0.5 * len(span) or not left_parts or not right_parts:
            return
        for parts in (left_parts, right_parts):
            if median(len(_segment_text(p, words)) for p in parts) < MIN_COLUMN_TEXT_CHARS:
                return
            lo = min(words[i].x0 for p in parts for i in p)
            hi = max(words[i].x1 for p in parts for i in p)
            if hi - lo < MIN_COLUMN_WIDTH_SHARE * page_width:
                return
        accepted.append(g)

    for r, row in enumerate(rows):
        if r in skip:
            for g in active:
                g.end = r
                close(g)
            active = []
            continue
        still: list[_Gutter] = []
        for g in active:
            free = _free_interval(row, words, g.lo, g.hi)
            if free and free[1] - free[0] >= min_gutter:
                g.lo, g.hi, g.end = free[0], free[1], r + 1
                still.append(g)
            else:
                close(g)
        # New candidates: this row's internal gaps not already tracked.
        for a, b in zip(row.words, row.words[1:]):
            lo, hi = words[a].x1, words[b].x0
            if hi - lo < min_gutter:
                continue
            if any(g.lo < hi and lo < g.hi for g in still):
                continue
            still.append(_Gutter(lo=lo, hi=hi, start=r, end=r + 1))
        active = still
    for g in active:
        close(g)
    return accepted


def _join_segments(segments: list[list[int]], words: list[RawWord]) -> str:
    return " | ".join(_segment_text(seg, words) for seg in segments)


def build_page(
    *,
    page_number: int,
    width: float,
    height: float,
    unit: Unit,
    backend: str,
    words: list[RawWord],
    rotation: int = 0,
    confidence: float | None = None,
    table_hints: list[TableHint] | None = None,
) -> PageLayout:
    """Turn a backend's words (absolute units) into a normalized PageLayout."""
    words = [w for w in words if w.text.strip()]
    if not words:
        return PageLayout(
            page_number=page_number,
            width=width,
            height=height,
            unit=unit,
            rotation=rotation,
            backend=backend,
            confidence=confidence,
        )

    word_height = median(w.height for w in words) or 1.0
    gap = GAP_FACTOR * word_height
    rows = _group_rows(words)
    for row in rows:
        row.segments = _segment(row, words, gap)

    tables: list[dict] = []  # {"rows": [row idx], "cells": [...], "bbox", "detection"}
    claimed_rows: set[int] = set()
    word_table: dict[int, int] = {}  # word -> index of the ruled table holding it

    # Ruled / backend tables first: they claim every row whose words sit inside.
    for hint in table_hints or []:
        hint_box = (hint.x0, hint.y0, hint.x1, hint.y1)
        inside = {
            i
            for i, w in enumerate(words)
            if hint_box[0] <= w.cx <= hint_box[2] and hint_box[1] <= w.cy <= hint_box[3]
        }
        if not inside:
            continue
        cells = []
        for cell in hint.cells:
            members = sorted(
                (
                    i
                    for i in inside
                    if cell.x0 <= words[i].cx <= cell.x1 and cell.y0 <= words[i].cy <= cell.y1
                ),
                key=lambda i: (words[i].cy, words[i].x0),
            )
            text = cell.text if cell.text is not None else " ".join(words[i].text for i in members)
            cells.append((cell, members, (text or "").strip()))
        # Words already taken by an earlier hint stay with it.
        inside -= set(word_table)
        for i in inside:
            word_table[i] = len(tables)
        claimed_rows.update(
            r for r, row in enumerate(rows) if any(i in inside for i in row.words)
        )
        tables.append({"hint": hint, "cells": cells, "detection": hint.detection})

    line_height = median(r.height for r in rows) or word_height
    for run, bands in _find_aligned_tables(rows, words, line_height, claimed_rows):
        claimed_rows.update(run)
        tables.append({"run": run, "bands": bands, "detection": "aligned"})

    gutters = _find_gutters(rows, words, width, gap, claimed_rows)

    # ---- lines in reading order -------------------------------------------
    lines: list[_Line] = []
    column_boxes: list[list[int]] = []  # word indices per global column index
    row_table = {}
    for t_index, t in enumerate(tables):
        for r in t.get("run", ()):
            row_table[r] = t_index

    emitted_tables: set[int] = set()
    r = 0
    while r < len(rows):
        if r in row_table:
            t_index = row_table[r]
            if t_index not in emitted_tables:
                emitted_tables.add(t_index)
                lines.extend(_table_lines(tables[t_index], rows, words, t_index))
            r += 1
            continue
        ruled = sorted({word_table[i] for i in rows[r].words if i in word_table})
        for t_index in ruled:
            if t_index not in emitted_tables:
                emitted_tables.add(t_index)
                lines.extend(_table_lines(tables[t_index], rows, words, t_index))
        region = next((g for g in gutters if g.start <= r < g.end), None)
        if region is None:
            rest = [i for i in rows[r].words if i not in word_table]
            if rest:
                segs = _segment(_Row(words=rest, y0=0, y1=0), words, gap)
                lines.append(_Line(words=rest, text=_join_segments(segs, words)))
            r += 1
            continue
        # Gutters of the same rows form one multi-column region.
        group = sorted(
            (g for g in gutters if g.start < region.end and region.start < g.end),
            key=lambda g: g.lo,
        )
        start = min(g.start for g in group)
        end = max(g.end for g in group)
        edges = [(-float("inf"), group[0].lo)]
        edges += [(a.hi, b.lo) for a, b in zip(group, group[1:])]
        edges += [(group[-1].hi, float("inf"))]
        base = len(column_boxes)
        column_boxes.extend([] for _ in edges)
        for c, (lo, hi) in enumerate(edges):
            for rr in range(start, end):
                if rr in row_table:
                    continue
                part = [i for i in rows[rr].words if lo <= words[i].cx <= hi]
                if not part:
                    continue
                segs = [s for s in (
                    [i for i in seg if i in part] for seg in rows[rr].segments
                ) if s]
                lines.append(_Line(words=part, text=_join_segments(segs, words), column=base + c))
                column_boxes[base + c].extend(part)
        r = end

    # ---- ids, models ---------------------------------------------------------
    order = [i for line in lines for i in line.words]
    stray = [i for i in range(len(words)) if i not in set(order)]
    word_id = {i: f"p{page_number}-w{n}" for n, i in enumerate(order + stray)}

    def box(indices: list[int]) -> BoundingBox:
        return BoundingBox.from_absolute(
            min(words[i].x0 for i in indices),
            min(words[i].y0 for i in indices),
            max(words[i].x1 for i in indices),
            max(words[i].y1 for i in indices),
            width,
            height,
        )

    table_models: list[Table] = []
    for t_index, t in enumerate(tables):
        table_models.append(_table_model(t, t_index, page_number, rows, words, word_id, width, height))

    line_models: list[TextLine] = []
    line_of_word: dict[int, str] = {}
    for n, line in enumerate(lines):
        line_id = f"p{page_number}-l{n}"
        for i in line.words:
            line_of_word[i] = line_id
        line_models.append(
            TextLine(
                id=line_id,
                text=line.text,
                bbox=box(line.words),
                word_ids=[word_id[i] for i in line.words],
                column=line.column,
                table_id=table_models[line.table].id if line.table is not None else None,
            )
        )

    block_models, block_of_line = _blocks(line_models, lines, words, page_number, height, line_height)
    block_of_word = {
        i: block_of_line[line_of_word[i]] for i in line_of_word
    }

    word_models = [
        WordToken(
            id=word_id[i],
            text=words[i].text,
            confidence=words[i].confidence,
            bbox=box([i]),
            page=page_number,
            block_id=block_of_word.get(i),
            line_id=line_of_word.get(i),
        )
        for i in order + stray
    ]
    columns = [
        Column(index=c, bbox=box(members))
        for c, members in enumerate(column_boxes)
        if members
    ]

    from .serialize import serialize_page

    page = PageLayout(
        page_number=page_number,
        width=width,
        height=height,
        unit=unit,
        rotation=rotation,
        backend=backend,
        confidence=confidence,
        words=word_models,
        lines=line_models,
        blocks=block_models,
        columns=columns,
        tables=table_models,
    )
    return page.model_copy(update={"text": serialize_page(page)})


def _table_lines(t: dict, rows: list[_Row], words: list[RawWord], t_index: int) -> list[_Line]:
    if "run" in t:
        return [
            _Line(
                words=rows[r].words,
                text=_join_segments(rows[r].segments, words),
                table=t_index,
            )
            for r in t["run"]
        ]
    by_row: dict[int, list[tuple]] = {}
    for cell, members, text in t["cells"]:
        by_row.setdefault(cell.row, []).append((cell.column, members, text))
    out = []
    for row_index in sorted(by_row):
        cells = sorted(by_row[row_index], key=lambda c: c[0])
        members = [i for _, m, _ in cells for i in m]
        if not members:
            continue
        out.append(
            _Line(words=members, text=" | ".join(text for _, _, text in cells), table=t_index)
        )
    return out


def _table_model(
    t: dict,
    t_index: int,
    page_number: int,
    rows: list[_Row],
    words: list[RawWord],
    word_id: dict[int, str],
    width: float,
    height: float,
) -> Table:
    table_id = f"p{page_number}-t{t_index}"
    cells: list[TableCell] = []
    if "run" in t:
        bands = t["bands"]
        for row_pos, r in enumerate(t["run"]):
            for seg in rows[r].segments:
                lo, hi = _extent(seg, words)
                column = next(k for k, (blo, bhi) in enumerate(bands) if blo <= lo and hi <= bhi)
                cells.append(
                    TableCell(
                        text=_segment_text(seg, words),
                        bbox=BoundingBox.from_absolute(
                            lo,
                            min(words[i].y0 for i in seg),
                            hi,
                            max(words[i].y1 for i in seg),
                            width,
                            height,
                        ),
                        row=row_pos,
                        column=column,
                        word_ids=[word_id[i] for i in seg],
                    )
                )
        all_words = [i for r in t["run"] for i in rows[r].words]
        bbox = BoundingBox.from_absolute(
            min(words[i].x0 for i in all_words),
            min(words[i].y0 for i in all_words),
            max(words[i].x1 for i in all_words),
            max(words[i].y1 for i in all_words),
            width,
            height,
        )
        return Table(
            id=table_id,
            bbox=bbox,
            rows=len(t["run"]),
            columns=len(bands),
            cells=cells,
            detection="aligned",
        )
    hint: TableHint = t["hint"]
    for cell, members, text in t["cells"]:
        cells.append(
            TableCell(
                text=text,
                bbox=BoundingBox.from_absolute(cell.x0, cell.y0, cell.x1, cell.y1, width, height),
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                word_ids=[word_id[i] for i in members],
            )
        )
    return Table(
        id=table_id,
        bbox=BoundingBox.from_absolute(hint.x0, hint.y0, hint.x1, hint.y1, width, height),
        rows=max((c.row + c.row_span for c, _, _ in t["cells"]), default=1),
        columns=max((c.column + c.column_span for c, _, _ in t["cells"]), default=1),
        cells=cells,
        detection="backend" if t["detection"] == "backend" else "ruled",
    )


def _blocks(
    line_models: list[TextLine],
    lines: list[_Line],
    words: list[RawWord],
    page_number: int,
    page_height: float,
    line_height: float,
) -> tuple[list[TextBlock], dict[str, str]]:
    """Consecutive lines in the same column/table with no paragraph gap."""
    groups: list[list[int]] = []
    for n, line in enumerate(lines):
        if groups:
            prev = lines[groups[-1][-1]]
            prev_bottom = max(words[i].y1 for i in prev.words)
            top = min(words[i].y0 for i in line.words)
            same_region = prev.column == line.column and prev.table == line.table
            if same_region and (
                line.table is not None or top - prev_bottom <= BLOCK_LINE_GAP * line_height
            ):
                groups[-1].append(n)
                continue
        groups.append([n])
    blocks, block_of_line = [], {}
    for b, members in enumerate(groups):
        block_id = f"p{page_number}-b{b}"
        for n in members:
            block_of_line[line_models[n].id] = block_id
        blocks.append(
            TextBlock(
                id=block_id,
                bbox=BoundingBox.union(line_models[n].bbox for n in members),
                line_ids=[line_models[n].id for n in members],
                column=lines[members[0]].column,
            )
        )
    return blocks, block_of_line


def text_only_page(
    *, page_number: int, text: str, backend: str, width: float = 1.0, height: float = 1.0,
    unit: Unit = "px", confidence: float | None = None,
) -> PageLayout:
    """A page from a backend that returns text without geometry (the VLM,
    plain-text files). No words, lines or tables — `text` is all there is."""
    return PageLayout(
        page_number=page_number,
        width=width,
        height=height,
        unit=unit,
        backend=backend,
        confidence=confidence,
        text=text,
    )


__all__ = ["CellHint", "RawWord", "TableHint", "build_page", "cells_from_boxes", "text_only_page"]
