"""Money written by a Spaniard and by an American look confusingly alike.

Before amounts.py existed, this pipeline read "1.234,56" as 1.23 and
"IVA 21% 259,26" as 25926.0 — a hundredfold error on every Spanish invoice,
silently, inside the layer whose whole job is catching errors.
"""
import pytest

from docket.amounts import amount_readings, amounts_in, parse_amount


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


def _values(text):
    return [value for _, value in amount_readings(text)]


@pytest.mark.parametrize("line, amount", [
    ("$ 48 801,10", 48801.10),            # plain space
    ("1 199,97", 1199.97),           # no-break space
    ("Total 12 261,98 €", 12261.98),  # narrow no-break space
    ("143 572,15", 143572.15),            # two groups
])
def test_space_grouped_thousands_are_read_whole(line, amount):
    assert amount in _values(line)


def test_space_grouping_adds_a_reading_and_keeps_the_separate_ones():
    # "2 100.00" can be a quantity and a price; both readings stay.
    assert _values("Qty 2 100.00") == [2.0, 100.0, 2100.0]


def test_accounting_parentheses_and_minus_read_negative_too():
    assert _values("$ (5,020.24)") == [5020.24, -5020.24]
    assert -60.0 in _values("Discount -60.00")


def test_spaced_digits_that_are_not_thousands_stay_apart():
    assert _values("10 27 20") == [10.0, 27.0, 20.0]
    assert _values("1 2345,00") == [1.0, 2345.0]
