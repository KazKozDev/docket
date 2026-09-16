"""Money parsing that survives crossing a border.

`1.234,56` and `1,234.56` are the same amount written by a Spaniard and an
American. Parsing one convention and hoping is how a validation layer ends
up confidently comparing 25926.0 against 259.26 — which is exactly what
this pipeline did before this module existed.

Nothing here guesses a locale from the document. The convention is decided
per number, from the number's own shape, because a single document can
legitimately mix them (an English-language invoice issued in Barcelona) and
because a locale guess is one more thing that can be silently wrong.
"""
from __future__ import annotations

import re

# A run of digits that may carry grouping and decimal marks. The lookarounds
# keep it from biting into longer alphanumeric tokens — an IBAN or an order
# number is not an amount.
MONEY_RE = re.compile(r"(?<![\w.,])(\d[\d.,]*\d|\d)(?![\w])")


def parse_amount(raw: str) -> float | None:
    """Parse one money-looking token into a float, or None if it isn't one.

    The rules, in order:
      - both separators present -> whichever comes last is the decimal point
        ("1.234,56" -> 1234.56, "1,234.56" -> 1234.56)
      - one separator, exactly three digits after it, and nothing else that
        looks decimal -> grouping ("1.234" and "1,234" are both 1234)
      - one separator otherwise -> decimal point ("259,26" -> 259.26)
    """
    token = raw.strip().replace(" ", "").replace(" ", "")
    if not token or not token[0].isdigit() or not token[-1].isdigit():
        return None
    if not re.fullmatch(r"[\d.,]+", token):
        return None

    has_dot, has_comma = "." in token, "," in token

    if has_dot and has_comma:
        decimal_sep = "." if token.rindex(".") > token.rindex(",") else ","
        grouping_sep = "," if decimal_sep == "." else "."
        token = token.replace(grouping_sep, "").replace(decimal_sep, ".")
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        if token.count(sep) > 1:
            token = token.replace(sep, "")  # 1.234.567 -> grouping only
        else:
            _, _, tail = token.partition(sep)
            # Three trailing digits is grouping; two (or one, or four+) is a
            # decimal fraction. Currencies don't group to three decimals.
            token = token.replace(sep, "" if len(tail) == 3 else ".")

    try:
        return float(token)
    except ValueError:
        return None


def amounts_in(text: str) -> list[float]:
    """Every parseable money amount in a string, in order of appearance."""
    values = []
    for match in MONEY_RE.finditer(text):
        value = parse_amount(match.group(1))
        if value is not None:
            values.append(value)
    return values
