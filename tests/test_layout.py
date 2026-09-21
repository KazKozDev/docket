"""Layout analysis: coordinates, rows, tables, columns, reading order,
serialization and quote location — pure geometry, no OCR engine needed."""
import pytest
from pydantic import ValidationError

from docket.layout import (
    BoundingBox,
    CellHint,
    PageLayout,
    RawWord,
    TableHint,
    build_page,
    locate_quote,
    serialize_page,
)

W, H = 1000.0, 1400.0


def _put(words, text, x, y, *, conf=0.9, key=None, h=12.0, char=7.0):
    for token in text.split():
        w = char * len(token)
        words.append(RawWord(token, x, y, x + w, y + h, conf, line_key=key))
        x += w + 4.0
    return words


def _page(words, **kwargs):
    return build_page(page_number=1, width=W, height=H, unit="px", backend="t", words=words, **kwargs)


# ---- coordinates -----------------------------------------------------------


def test_boxes_are_normalized_and_convert_back():
    box = BoundingBox.from_absolute(100, 140, 300, 280, W, H)
    assert (box.x0, box.y0, box.x1, box.y1) == (0.1, 0.1, 0.3, 0.2)
    assert box.to_absolute(W, H) == pytest.approx((100, 140, 300, 280))


def test_boxes_outside_the_page_are_clamped():
    box = BoundingBox.from_absolute(-5, -5, 2000, 2000, W, H)
    assert (box.x0, box.y0, box.x1, box.y1) == (0.0, 0.0, 1.0, 1.0)


def test_inverted_or_out_of_range_boxes_are_rejected():
    with pytest.raises(ValidationError):
        BoundingBox(x0=0.5, y0=0.1, x1=0.2, y1=0.3)
    with pytest.raises(ValidationError):
        BoundingBox(x0=0.1, y0=0.1, x1=1.2, y1=0.3)


def test_every_word_box_is_normalized_and_keeps_page_size():
    page = _page(_put([], "Invoice INV-1 dated today", 900, 1380))
    assert page.width == W and page.height == H
    for word in page.words:
        for v in (word.bbox.x0, word.bbox.y0, word.bbox.x1, word.bbox.y1):
            assert 0.0 <= v <= 1.0


# ---- words, lines, ids --------------------------------------------------------


def test_ids_link_words_lines_and_blocks():
    words = _put([], "Line one here", 20, 20)
    _put(words, "Line two here", 20, 36)
    page = _page(words)
    ids = [w.id for w in page.words]
    assert len(ids) == len(set(ids)) and ids[0] == "p1-w0"
    assert [line.text for line in page.lines] == ["Line one here", "Line two here"]
    for line in page.lines:
        for word_id in line.word_ids:
            assert page.word(word_id).line_id == line.id
    assert len(page.blocks) == 1 and page.blocks[0].line_ids == [l.id for l in page.lines]
    assert all(w.block_id == page.blocks[0].id for w in page.words)


def test_empty_page_has_no_structure():
    page = _page([])
    assert page.words == [] and page.lines == [] and page.text == ""


def test_paragraph_gap_starts_a_new_block():
    words = _put([], "First paragraph line", 20, 20)
    _put(words, "Second paragraph line", 20, 200)
    assert len(_page(words).blocks) == 2


def test_engine_line_grouping_keeps_skewed_lines_apart():
    """Two skewed lines whose word boxes brush against each other must not
    interleave: the engine's own line identity wins over a flat band."""
    words = []
    for i, token in enumerate("the quick brown fox jumps".split()):
        words.append(RawWord(token, 20 + 60 * i, 20 + 3 * i, 70 + 60 * i, 32 + 3 * i, 0.9, line_key="a"))
    for i, token in enumerate("over the lazy dog again".split()):
        words.append(RawWord(token, 20 + 60 * i, 30 + 3 * i, 70 + 60 * i, 42 + 3 * i, 0.9, line_key="b"))
    page = _page(words)
    assert [line.text for line in page.lines] == [
        "the quick brown fox jumps",
        "over the lazy dog again",
    ]


def test_side_by_side_engine_lines_join_one_row():
    words = _put([], "Subtotal:", 20, 100, key="left")
    _put(words, "145.00", 600, 100, key="right")
    page = _page(words)
    assert [line.text for line in page.lines] == ["Subtotal: | 145.00"]


# ---- tables ------------------------------------------------------------------


def _invoice_table(words, top=100):
    rows = [
        ("Description", "Qty", "Price", "Total"),
        ("Widget large", "2", "4.00", "8.00"),
        ("Bolt", "10", "0.50", "5.00"),
    ]
    for r, cells in enumerate(rows):
        for text, x in zip(cells, (20, 400, 520, 640)):
            _put(words, text, x, top + 18 * r)
    return words


def test_aligned_rows_become_a_structured_table():
    page = _page(_invoice_table([]))
    assert len(page.tables) == 1
    table = page.tables[0]
    assert (table.rows, table.columns, table.detection) == (3, 4, "aligned")
    assert table.grid() == [
        ["Description", "Qty", "Price", "Total"],
        ["Widget large", "2", "4.00", "8.00"],
        ["Bolt", "10", "0.50", "5.00"],
    ]
    cell = next(c for c in table.cells if c.text == "4.00")
    assert (cell.row, cell.column) == (1, 2)
    assert all(page.word(i).text in cell.text for i in cell.word_ids)
    assert all(line.table_id == table.id for line in page.lines)


def test_table_serializes_with_a_marker_and_cell_separators():
    text = _page(_invoice_table([])).text
    assert text.splitlines() == [
        "[TABLE 1: 3 rows x 4 columns]",
        "Description | Qty | Price | Total",
        "Widget large | 2 | 4.00 | 8.00",
        "Bolt | 10 | 0.50 | 5.00",
    ]


def test_markdown_rendering_of_a_table():
    table = _page(_invoice_table([])).tables[0]
    assert table.to_markdown().splitlines()[1] == "| Widget large | 2 | 4.00 | 8.00 |"


def test_label_value_pairs_are_neither_table_nor_columns():
    words = []
    for r, (label, value) in enumerate(
        [("Invoice no:", "001"), ("Date:", "2026-01-01"), ("Due:", "2026-02-01"),
         ("Terms:", "Net 30"), ("Currency:", "EUR")]
    ):
        _put(words, label, 20, 20 + 18 * r)
        _put(words, value, 400, 20 + 18 * r)
    page = _page(words)
    assert page.tables == [] and page.columns == []
    assert page.lines[0].text == "Invoice no: | 001"


def test_ruled_table_hint_owns_its_words_with_spans():
    words = _put([], "Item", 30, 110)
    _put(words, "Amount", 330, 110)
    _put(words, "Consulting", 30, 150)
    _put(words, "1200.00", 330, 150)
    _put(words, "Grand total 1200.00", 30, 190)
    hint = TableHint(
        x0=20, y0=100, x1=500, y1=220,
        cells=[
            CellHint(row=0, column=0, x0=20, y0=100, x1=300, y1=140),
            CellHint(row=0, column=1, x0=300, y0=100, x1=500, y1=140),
            CellHint(row=1, column=0, x0=20, y0=140, x1=300, y1=180),
            CellHint(row=1, column=1, x0=300, y0=140, x1=500, y1=180),
            CellHint(row=2, column=0, column_span=2, x0=20, y0=180, x1=500, y1=220),
        ],
    )
    page = _page(words, table_hints=[hint])
    table = page.tables[0]
    assert (table.rows, table.columns, table.detection) == (3, 2, "ruled")
    assert table.grid() == [["Item", "Amount"], ["Consulting", "1200.00"], ["Grand total 1200.00", ""]]
    spanning = next(c for c in table.cells if c.row == 2)
    assert spanning.column_span == 2
    # Every word appears exactly once in the serialized text.
    assert page.text.count("Consulting") == 1


# ---- columns and reading order -------------------------------------------------


def _two_columns(words, rows=5):
    for k in range(rows):
        _put(words, f"left column prose line number {k} here", 20, 300 + 16 * k)
        _put(words, f"right column prose line {k} goes here", 520, 300 + 16 * k)
    return words


def test_text_columns_read_column_major():
    words = _put([], "Full width heading across the page", 20, 250)
    page = _page(_two_columns(words))
    assert len(page.columns) == 2
    texts = [line.text for line in page.lines]
    assert texts[0] == "Full width heading across the page"
    assert texts[1].startswith("left column prose line number 0")
    assert texts[5].startswith("left column prose line number 4")
    assert texts[6].startswith("right column prose line 0")
    assert {line.column for line in page.lines[1:6]} == {0}
    assert {line.column for line in page.lines[6:]} == {1}
    assert page.columns[0].bbox.x1 < page.columns[1].bbox.x0


def test_column_markers_can_be_switched_off():
    page = _page(_two_columns([]))
    assert "[COLUMN 1]" in page.text and "[COLUMN 2]" in page.text
    plain = serialize_page(page, markers=False)
    assert "[COLUMN" not in plain
    assert plain.splitlines()[0].startswith("left column prose line number 0")


def test_too_few_rows_are_not_columns():
    page = _page(_two_columns([], rows=2))
    assert page.columns == []


def test_layout_round_trips_through_json():
    page = _page(_two_columns(_invoice_table([])))
    assert PageLayout.model_validate_json(page.model_dump_json()) == page


# ---- quote location ---------------------------------------------------------


def test_exact_quote_resolves_to_its_words():
    words = _put([], "Subtotal: USD 8000.00", 20, 20)
    _put(words, "Total: USD 8,450.00", 20, 40, conf=0.8)
    page = _page(words)
    located = locate_quote("Total: USD 8,450.00", page)
    assert [page.word(i).text for i in located.word_ids] == ["Total:", "USD", "8,450.00"]
    assert located.match_score == 1.0
    assert located.confidence == pytest.approx(0.8)
    assert located.bbox.y0 == pytest.approx(40 / H)


def test_quote_spanning_split_tokens_still_resolves():
    words = [RawWord("8,480", 20, 20, 60, 32, 0.9), RawWord(".00", 61, 20, 80, 32, 0.9)]
    located = locate_quote("8,480.00", _page(words))
    assert located is not None and len(located.word_ids) == 2


def test_quote_with_column_separator_matches_the_line():
    page = _page(_invoice_table([]))
    assert locate_quote("Widget large | 2 | 4.00 | 8.00", page) is not None


def test_near_miss_resolves_with_a_lower_score():
    page = _page(_put([], "Total: USD 13.00", 20, 20))
    located = locate_quote("Totl: USD 13,00", page)
    assert located is not None and 0.8 <= located.match_score < 1.0


def test_absent_quote_and_geometryless_page_do_not_resolve():
    page = _page(_put([], "Total: USD 13.00", 20, 20))
    assert locate_quote("Vendor: Acme Ltd", page) is None
    text_only = PageLayout(page_number=1, width=1, height=1, backend="vlm", text="Total 13.00")
    assert locate_quote("Total 13.00", text_only) is None
