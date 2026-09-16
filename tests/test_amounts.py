"""Money written by a Spaniard and by an American look confusingly alike.

Before amounts.py existed, this pipeline read "1.234,56" as 1.23 and
"IVA 21% 259,26" as 25926.0 — a hundredfold error on every Spanish invoice,
silently, inside the layer whose whole job is catching errors.
"""
import pytest

from docket.amounts import amounts_in, parse_amount


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Both separators present: the last one is the decimal point.
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1.234.567,89", 1234567.89),
        ("1,234,567.89", 1234567.89),
        # One separator, two trailing digits: a decimal fraction.
        ("259,26", 259.26),
        ("93.62", 93.62),
        ("0,00", 0.0),
        # One separator, three trailing digits: grouping. Both conventions
        # happen to agree on the answer here, which is why it's safe.
        ("1.234", 1234.0),
        ("1,234", 1234.0),
        # No separator at all.
        ("1512", 1512.0),
        ("0", 0.0),
    ],
)
def test_parses_both_conventions(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3,4,5", "-", "."])
def test_rejects_non_amounts(raw):
    assert parse_amount(raw) is None


def test_spanish_invoice_totals_are_read_correctly():
    line = "Base imponible: 1.234,56    IVA 21%: 259,26    Importe total: 1.493,82"
    assert amounts_in(line) == [1234.56, 21.0, 259.26, 1493.82]


def test_us_invoice_totals_are_still_read_correctly():
    line = "Subtotal: 1,234.56  Tax: 259.26  Total: 1,493.82"
    assert amounts_in(line) == [1234.56, 259.26, 1493.82]


def test_the_two_conventions_agree_on_the_same_invoice():
    spanish = amounts_in("1.234,56")
    american = amounts_in("1,234.56")
    assert spanish == american == [1234.56]
