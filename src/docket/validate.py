"""Business-rule validation layer.

Pydantic already rejects malformed types (bad dates, missing required
fields) before an object gets here — this module checks things a schema
can't express: totals that don't add up, dates in the wrong order, IDs
that don't look like IDs. Classic NLP/rules territory, deliberately kept
free of any LLM call.
"""
from __future__ import annotations

import re
from datetime import date

from . import amounts, checksums
from .schemas import BoardingPass, Contract, Invoice, Receipt, ValidationIssue

_TAX_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{4,20}$", re.I)
_AMOUNT_TOLERANCE = 0.01

# A number that is a rate, not an amount. Not a keyword heuristic — it's the
# difference between "IVA 21% 259,26" meaning a tax of 21 and a tax of 259.26.
# Parentheses are optional: English invoices write "Tax (21%)", Spanish ones
# "IVA 21%", and reading the 21 as money is wrong in both.
_PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")


def _isclose(a: float, b: float, tol: float = _AMOUNT_TOLERANCE) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(b))


# Business documents don't come from the 1800s, and a document recording
# something that already happened can't be dated years into the future. A
# garbled "1yo12036" that a model resolves to 2036-01-01 is the failure this
# catches — no other rule notices, because one plausible-looking date is as
# self-consistent as another.
_EARLIEST_PLAUSIBLE = date(1990, 1, 1)


def _check_date_range(
    field: str, value: date | None, *, max_years_ahead: int, today: date | None = None
) -> list[ValidationIssue]:
    if value is None:
        return []
    today = today or date.today()
    latest = today.replace(year=today.year + max_years_ahead)

    if value < _EARLIEST_PLAUSIBLE:
        return [
            ValidationIssue(
                field=field,
                message=f"{value.isoformat()} is before {_EARLIEST_PLAUSIBLE.isoformat()} — implausible for a business document",
            )
        ]
    if value > latest:
        return [
            ValidationIssue(
                field=field,
                message=f"{value.isoformat()} is more than {max_years_ahead} year(s) in the future",
            )
        ]
    return []


def _amounts_on_line(line: str) -> list[float]:
    """Every money amount readable on one line, ignoring the number inside a
    "(21%)" aside.

    Both the US and European conventions are parsed — see amounts.py. A
    written-out zero counts as readable: knowing the document says "nothing"
    is different from not being able to read it.
    """
    values: list[float] = []
    percent_spans = [m.span() for m in _PERCENT_RE.finditer(line)]
    for match in amounts.MONEY_RE.finditer(line):
        if any(start <= match.start() < end for start, end in percent_spans):
            continue
        value = amounts.parse_amount(match.group(1))
        if value is not None:
            values.append(value)
    return values


def _check_cited_sources(
    document, raw_text: str, numeric_fields: dict[str, float]
) -> list[ValidationIssue]:
    """Verify each field against the line the model says it came from.

    This replaced a keyword search — "find a line mentioning tax, read the
    amount near it" — that had to be told every place a keyword appears
    without being that field's amount. That list turned out to be endless:
    a tax ID, a table column header, "Total excluding VAT", a "GST #" in the
    footer's legal text, the word "discount" inside a product name. Each fix
    was one more entry in a blacklist, and the list of places a word can
    appear has no end. The mirror problem was just as bad: the search had
    never heard of "HST", so on a Canadian invoice it sailed past the real
    tax line and objected to the registration number in the footer.

    The label is the thing we kept guessing at — and the model has just read
    it. So it cites the line, and the checking left to do is exact: does
    that line exist in the document, and does it contain the number claimed?
    Both are string and arithmetic operations, which is what a machine can
    be trusted with. No keywords, in any language.

    What it cannot do is notice an omission: a model that dropped the tax
    line will not cite it either. That's covered separately, by requiring
    every amount in the document's totals block to find a home.
    """
    issues: list[ValidationIssue] = []
    locations = getattr(document, "field_locations", None) or {}
    normalized_text = _normalize(raw_text)

    for field, value in numeric_fields.items():
        source_text = normalized_text
        location = locations.get(field)
        cited = location.quote if location is not None else None
        if not cited or not cited.strip():
            if "[PAGE " in raw_text:
                # No citation means the value was computed, not read. Real
                # invoices do this: one printed "Total excl. VAT 372.00" and
                # "Total incl. VAT 450.12" and no VAT line at all, so the tax
                # could only be derived. That's a different epistemic status,
                # worth recording — but an absence of evidence is not an
                # error, and shouldn't queue a correct extraction for review.
                issues.append(
                    ValidationIssue(
                        field=field,
                        message=(
                            f"{field}={value:.2f} is not stated on any line — it was derived "
                            f"rather than read, so nothing independent confirms it"
                        ),
                        severity="warning",
                    )
                )
            continue

        # Narrow the search to the cited page when the text is paginated. Text
        # handed in without page markers is one page by definition, so a
        # citation to page 1 is checked against all of it — the validator has
        # no business demanding a marker its caller never added.
        if location is not None and "[PAGE " in raw_text:
            marker = f"[PAGE {location.page}]"
            if marker not in raw_text:
                issues.append(
                    ValidationIssue(
                        field=field,
                        message=f"source citation refers to absent page {location.page}",
                    )
                )
                continue
            start = raw_text.index(marker) + len(marker)
            end = raw_text.find("[PAGE ", start)
            source_text = _normalize(raw_text[start : end if end >= 0 else None])

        # Garbled OCR lines are evidence of a bad transcript, not reading
        # material — quote them truncated so review output stays legible.
        short_cited = cited if len(cited) <= 80 else cited[:77] + "..."
        if not _appears_in(cited, source_text):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=(
                        f"cited source {short_cited!r} does not appear in the document — "
                        f"the value {value:.2f} is not grounded in anything on the page"
                    ),
                )
            )
            continue

        stated = _amounts_on_line(cited)
        if stated and not any(_isclose(s, value, tol=0.02) for s in stated):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=(
                        f"cited line {short_cited!r} states "
                        f"{', '.join(f'{s:.2f}' for s in stated)}, but {field}={value:.2f}"
                    ),
                )
            )

    return issues


def _citations_of(document) -> dict[str, str]:
    """The line the extraction says each field came from, whichever of the
    two citation shapes the schema uses.
    """
    locations = getattr(document, "field_locations", None) or {}
    return {f: loc.quote for f, loc in locations.items() if getattr(loc, "quote", None)}


def _digit_signature(value: float) -> str:
    """The digits of a number with the decimal point's position discarded.

    Tesseract loses decimal separators routinely — 7.75 came back as 775.00,
    5.00 as 500, 15.00 as 1500 — so a disagreement that only moves the point
    is the witness being unreliable, not the transcript being altered. A
    changed *digit* is the thing worth reporting: 450.00 against 480.00
    survives this comparison, 7.75 against 775.00 does not.
    """
    digits = f"{value:.4f}".replace(".", "").lstrip("0").rstrip("0")
    return digits or "0"


def _label_of(line: str) -> str:
    """A line with its numbers removed — "Sales Tax 6.25% 9.06" -> "sales tax".

    What's left is the document's own wording for the field, which is what
    two transcripts of the same page can be anchored on without anyone
    having to supply a vocabulary.
    """
    without_numbers = re.sub(r"[\d.,%$€£]+", " ", line)
    return _normalize(without_numbers)


def _check_witness_contradicts(
    document, witness_pages: list[str | None] | None, fields: list[tuple[str, float]]
) -> list[ValidationIssue]:
    """Flag a number only when an independent reading of the same line
    disagrees with it.

    An earlier version required every extracted number to appear somewhere
    in a confidence-filtered Tesseract reading, and flagged the ones that
    didn't. That is absence of evidence read as evidence of error, and on a
    clean invoice it accused nine correct fields at once: the witness list
    held zip codes, a street number and fragments of dates, while "5.00" and
    "15.00" had come back as 500 and 1500 with the decimal point dropped. A
    sparse, noisy witness cannot confirm anything — but it can still
    contradict.

    So the anchor is the cited line's own wording. Strip the numbers from
    the line the extraction cited, find a line in the witness transcript
    whose wording matches exactly, and compare only there. A witness that
    never read that region stays silent, which is the honest answer.
    """
    issues: list[ValidationIssue] = []
    citations = _citations_of(document)
    witness_lines = [
        line
        for page in witness_pages or []
        if page
        for line in page.splitlines()
        if line.strip()
    ]
    if not witness_lines:
        return issues

    by_label: dict[str, list[float]] = {}
    for line in witness_lines:
        label = _label_of(line)
        if label:
            by_label.setdefault(label, []).extend(_amounts_on_line(line))

    for field, value in fields:
        cited = citations.get(field)
        if not cited:
            continue
        label = _label_of(cited)
        # A label needs enough words to identify a row; "1" or "$" does not.
        if len(label) < 3:
            continue
        witnessed = by_label.get(label)
        if not witnessed:
            continue
        if any(_isclose(value, n, tol=0.02) for n in witnessed):
            continue
        # Same digits, different decimal placement: that's the witness's own
        # known weakness, not evidence the value was altered.
        if any(_digit_signature(value) == _digit_signature(n) for n in witnessed):
            continue
        issues.append(
            ValidationIssue(
                field=field,
                message=(
                    f"an independent OCR reading of {label!r} gives "
                    f"{', '.join(f'{n:.2f}' for n in witnessed)}, but {field}={value:.2f} — "
                    f"the transcripts disagree; check against the original page"
                ),
            )
        )
    return issues


def _check_material_locations(
    document, raw_text: str, fields: tuple[str, ...]
) -> list[ValidationIssue]:
    """Require a page and exact quote for key fields in page-aware pipeline input."""
    if "[PAGE " not in raw_text:
        return []
    issues: list[ValidationIssue] = []
    locations = getattr(document, "field_locations", None) or {}
    for field in fields:
        value = getattr(document, field, None)
        if value is None or value == "" or value == []:
            continue
        location = locations.get(field)
        if location is None:
            issues.append(
                ValidationIssue(
                    field=field, message="missing page and source-region citation"
                )
            )
            continue
        marker = f"[PAGE {location.page}]"
        start = raw_text.find(marker)
        if start < 0:
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"source citation refers to absent page {location.page}",
                )
            )
            continue
        next_page = raw_text.find("[PAGE ", start + len(marker))
        page_text = raw_text[start : next_page if next_page >= 0 else None]
        if not _appears_in(location.quote, _normalize(page_text)):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"cited source region does not appear on page {location.page}",
                )
            )
    return issues


def _unconfirmed_vlm_issue() -> ValidationIssue:
    # The vision model invents digits when reconciling (observed: a printed
    # 450.00 transcribed as 480.00). With no confident OCR reading backing
    # any number, self-consistent output proves nothing — one review issue,
    # not one per field, and no value is rewritten.
    return ValidationIssue(
        field="*",
        message=(
            "VLM-transcribed values have no confident OCR support — "
            "verify key figures against the original page"
        ),
        # A warning, not an error: it names no specific fault, so on its own
        # it shouldn't queue a correct extraction for a human. It states the
        # epistemic position — nothing here was independently read — and
        # leaves the routing to checks that can point at something.
        severity="warning",
    )


def validate_invoice(
    inv: Invoice,
    raw_text: str | None = None,
    witness_pages: list[str | None] | None = None,
    vlm_unconfirmed: bool = False,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if vlm_unconfirmed:
        issues.append(_unconfirmed_vlm_issue())

    if not inv.invoice_number.strip():
        issues.append(
            ValidationIssue(field="invoice_number", message="empty invoice number")
        )

    if inv.due_date and inv.due_date < inv.issue_date:
        issues.append(
            ValidationIssue(field="due_date", message="due_date is before issue_date")
        )

    # An invoice records something already billed, so its issue date can't be
    # meaningfully in the future; payment terms can run a while, so due dates
    # get more room.
    issues.extend(_check_date_range("issue_date", inv.issue_date, max_years_ahead=1))
    issues.extend(_check_date_range("due_date", inv.due_date, max_years_ahead=10))

    if inv.vendor_tax_id:
        tax_ok, scheme = checksums.validate_tax_id(inv.vendor_tax_id)
        if tax_ok is False:
            issues.append(
                ValidationIssue(
                    field="vendor_tax_id",
                    message=f"{inv.vendor_tax_id!r} fails the {scheme} checksum or format",
                    severity="error",
                )
            )
        elif tax_ok is None and not _TAX_ID_RE.match(inv.vendor_tax_id):
            issues.append(
                ValidationIssue(
                    field="vendor_tax_id",
                    message=f"{inv.vendor_tax_id!r} doesn't look like a tax ID",
                    severity="warning",
                )
            )

    if inv.vendor_iban and not checksums.validate_iban(inv.vendor_iban):
        # Two different findings with two different remedies: a real IBAN
        # with a bad check digit means compare the digits, a string that was
        # never an IBAN means ask the vendor for the number. Reporting the
        # second as the first sends a reviewer looking for a typo that isn't
        # there — seen on a template whose IBAN field still read "[IBAN code]".
        clean_iban = inv.vendor_iban.replace(" ", "").upper()
        if clean_iban[:2] in checksums._NON_IBAN_COUNTRIES:
            message = (
                f"{inv.vendor_iban!r} starts with country code {clean_iban[:2]!r}, but the "
                "United States / Canada does not use IBANs (use ABA Routing / Transit number instead)"
            )
        elif checksums.is_iban_shaped(inv.vendor_iban):
            message = f"{inv.vendor_iban!r} fails the IBAN mod-97 checksum"
        else:
            message = (
                f"{inv.vendor_iban!r} is not an IBAN — the document states no usable "
                f"account number for this vendor"
            )
        issues.append(ValidationIssue(field="vendor_iban", message=message))

    if inv.vendor_vat_number:
        vat_ok = checksums.validate_vat(inv.vendor_vat_number)
        if vat_ok is False:
            issues.append(
                ValidationIssue(
                    field="vendor_vat_number",
                    message=(
                        f"{inv.vendor_vat_number!r} fails its country's VAT checksum"
                        if checksums.is_vat_shaped(inv.vendor_vat_number)
                        else f"{inv.vendor_vat_number!r} is not a VAT number — the document "
                        f"states none for this vendor"
                    ),
                )
            )
        elif vat_ok is None:
            issues.append(
                ValidationIssue(
                    field="vendor_vat_number",
                    message=(
                        f"{inv.vendor_vat_number!r} — format looks plausible but no checksum "
                        "algorithm is implemented for this country, so it isn't verified"
                    ),
                    severity="warning",
                )
            )

    for n, li in enumerate(inv.line_items, start=1):
        # quantity × unit_price must equal the line total. Cheap, always true,
        # and the only check that catches a garbled *price* — a wrong unit
        # price leaves every other total intact, so the invoice-level
        # arithmetic still adds up and nothing else notices.
        if not _isclose(li.quantity * li.unit_price, li.total):
            issues.append(
                ValidationIssue(
                    field=f"line_items[{n - 1}]",
                    message=(
                        f"line {n} ({li.description!r}): {li.quantity:g} × {li.unit_price:.2f} "
                        f"= {li.quantity * li.unit_price:.2f}, but the line total says {li.total:.2f}"
                    ),
                )
            )

    if inv.line_items:
        computed_subtotal = sum(li.total for li in inv.line_items)
        if not _isclose(computed_subtotal, inv.subtotal):
            issues.append(
                ValidationIssue(
                    field="subtotal",
                    message=f"line items sum to {computed_subtotal:.2f}, subtotal says {inv.subtotal:.2f}",
                )
            )

    expected_total = (
        inv.subtotal + inv.tax_amount + inv.shipping_amount - inv.discount_amount
    )
    if not _isclose(expected_total, inv.total_amount):
        issues.append(
            ValidationIssue(
                field="total_amount",
                message=(
                    f"subtotal + tax + shipping - discount = {expected_total:.2f}, "
                    f"total_amount says {inv.total_amount:.2f}"
                ),
            )
        )

    if raw_text is not None:
        issues.extend(
            _check_material_locations(
                inv,
                raw_text,
                (
                    "invoice_number",
                    "issue_date",
                    "vendor_name",
                    "customer_name",
                    "subtotal",
                    "total_amount",
                ),
            )
        )
        numeric_fields = {"subtotal": inv.subtotal, "total_amount": inv.total_amount}
        numeric_fields.update(
            {
                field: value
                for field, value in {
                    "tax_amount": inv.tax_amount,
                    "shipping_amount": inv.shipping_amount,
                    "discount_amount": inv.discount_amount,
                }.items()
                if value != 0
            }
        )
        issues.extend(
            _check_cited_sources(
                inv,
                raw_text,
                numeric_fields,
            )
        )
        witness_fields: list[tuple[str, float]] = [
            ("subtotal", inv.subtotal),
            ("total_amount", inv.total_amount),
        ]
        for name in ("tax_amount", "shipping_amount", "discount_amount"):
            if getattr(inv, name) != 0:
                witness_fields.append((name, getattr(inv, name)))
        for n, li in enumerate(inv.line_items):
            witness_fields.extend(
                [
                    (f"line_items[{n}].quantity", li.quantity),
                    (f"line_items[{n}].unit_price", li.unit_price),
                    (f"line_items[{n}].total", li.total),
                ]
            )
        issues.extend(_check_witness_contradicts(inv, witness_pages, witness_fields))
        date_fields = [("issue_date", inv.issue_date)]
        if inv.due_date is not None:
            date_fields.append(("due_date", inv.due_date))
        issues.extend(_check_date_convention(date_fields, raw_text))

    return issues


def validate_receipt(
    rec: Receipt,
    raw_text: str | None = None,
    witness_pages: list[str | None] | None = None,
    vlm_unconfirmed: bool = False,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if vlm_unconfirmed:
        issues.append(_unconfirmed_vlm_issue())

    if not rec.merchant_name.strip():
        issues.append(
            ValidationIssue(field="merchant_name", message="empty merchant name")
        )

    # A receipt is proof of a purchase that already happened.
    issues.extend(
        _check_date_range("transaction_date", rec.transaction_date, max_years_ahead=1)
    )

    if rec.items:
        items_sum = sum(i.price for i in rec.items)
        # Line items are pre-tax. Comparing them straight to the total flags
        # every taxed receipt — which is most retail receipts — as broken.
        # Compare against the stated subtotal, or back the tax out of the
        # total when the receipt didn't print a subtotal line.
        base = rec.subtotal if rec.subtotal > 0 else rec.total_amount - rec.tax_amount
        if not _isclose(items_sum, base):
            issues.append(
                ValidationIssue(
                    field="items",
                    message=(
                        f"items sum to {items_sum:.2f}, but the pre-tax amount they should "
                        f"match is {base:.2f}"
                    ),
                )
            )

    if rec.subtotal > 0 and not _isclose(
        rec.subtotal + rec.tax_amount, rec.total_amount
    ):
        issues.append(
            ValidationIssue(
                field="total_amount",
                message=(
                    f"subtotal + tax = {rec.subtotal + rec.tax_amount:.2f}, "
                    f"total_amount says {rec.total_amount:.2f}"
                ),
            )
        )

    if raw_text is not None:
        issues.extend(
            _check_material_locations(
                rec, raw_text, ("merchant_name", "transaction_date", "total_amount")
            )
        )
        numeric_fields = {"total_amount": rec.total_amount}
        if rec.subtotal:
            numeric_fields["subtotal"] = rec.subtotal
        if rec.tax_amount:
            numeric_fields["tax_amount"] = rec.tax_amount
        issues.extend(_check_cited_sources(rec, raw_text, numeric_fields))
        witness_fields = [("total_amount", rec.total_amount)]
        if rec.subtotal:
            witness_fields.append(("subtotal", rec.subtotal))
        if rec.tax_amount:
            witness_fields.append(("tax_amount", rec.tax_amount))
        for n, item in enumerate(rec.items):
            witness_fields.append((f"items[{n}].price", item.price))
        issues.extend(_check_witness_contradicts(rec, witness_pages, witness_fields))

    return issues


# Stripped before comparing company names, so that "Acme Corp" and
# "Acme Corporation" are recognised as one entity. Spanish, Catalan and the
# other EU forms are here because the target deployment is Barcelona.
_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "ltd",
    "limited",
    "llc",
    "llp",
    "lp",
    "plc",
    "sa",
    "sau",
    "sl",
    "slu",
    "sca",
    "gmbh",
    "mbh",
    "bv",
    "nv",
    "ag",
    "as",
    "oy",
    "ab",
    "pte",
    "pty",
    "srl",
    "spa",
    "sarl",
    "kg",
    "ou",
}


def _core_name(name: str) -> str:
    """A company name with its legal form removed.

    "Acme Corp" and "Acme Corporation" are the same counterparty; comparing
    the raw strings says otherwise, which is how a contract with itself
    passes validation.
    """
    tokens = _normalize(name).split()
    # Normalising drops the dots in "S.A.", leaving two single-letter tokens.
    # Glue trailing single letters back together so the suffix list can see
    # "sa" — otherwise every Spanish company name keeps its legal form.
    while len(tokens) >= 2 and len(tokens[-1]) == 1 and len(tokens[-2]) == 1:
        tokens[-2:] = [tokens[-2] + tokens[-1]]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _normalize(text: str) -> str:
    """Collapse to lowercase alphanumerics so that "Acme Corp." and
    "ACME  Corp" compare equal. Punctuation and spacing are exactly what a
    model rewrites without changing meaning, so they can't be part of the
    comparison.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _date_renderings(d: date) -> list[str]:
    """The ways a document might print one date. A model returns ISO; the
    page it read almost certainly didn't.
    """
    months = [
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    ]
    month, abbr = months[d.month - 1], months[d.month - 1][:3]
    return [
        d.isoformat(),
        f"{d.day:02d} {d.month:02d} {d.year}",
        f"{d.month:02d} {d.day:02d} {d.year}",
        f"{d.day} {month} {d.year}",
        f"{month} {d.day} {d.year}",
        f"{d.day} {abbr} {d.year}",
        f"{abbr} {d.day} {d.year}",
        f"{d.year} {d.month:02d} {d.day:02d}",
    ]


def _appears_in(value: str, normalized_text: str) -> bool:
    normalized = _normalize(value)
    return bool(normalized) and normalized in normalized_text


# Numeric dates with one part above 12 can only be read one way. No locale
# is assumed — the document's own unambiguous dates set the convention, and
# every extracted date must be readable under it. "11/02/2019" alone is
# ambiguous, but next to a "26/02/2019" the document has declared day-first.
_NUMERIC_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})(?!\d)")


def _document_date_convention(raw_text: str) -> str | None:
    day_first = month_first = False
    for first, second, _ in _NUMERIC_DATE_RE.findall(raw_text):
        if int(second) > 12:
            month_first = True
        elif int(first) > 12:
            day_first = True
    if day_first and not month_first:
        return "dmy"
    if month_first and not day_first:
        return "mdy"
    return None


def _check_date_convention(
    fields: list[tuple[str, date]], raw_text: str
) -> list[ValidationIssue]:
    """Flag extracted dates that contradict the document's own convention.

    Only fires when the text holds unambiguous evidence (a part above 12)
    pointing one way. A date with no matching day-first rendering in the
    text was read under the other convention — exactly the Nov-2/Feb-24
    mix-up this catches. Never guesses a locale for an ambiguous document.
    """
    convention = _document_date_convention(raw_text)
    if convention is None:
        return []
    normalized = _normalize(raw_text)
    issues: list[ValidationIssue] = []
    for field, value in fields:
        short_year = value.year % 100
        if convention == "dmy":
            renderings = [
                f"{value.day:02d} {value.month:02d} {value.year}",
                f"{value.day} {value.month:02d} {value.year}",
                f"{value.day:02d} {value.month:02d} {short_year:02d}",
            ]
        else:
            renderings = [
                f"{value.month:02d} {value.day:02d} {value.year}",
                f"{value.month} {value.day} {value.year}",
                f"{value.month:02d} {value.day:02d} {short_year:02d}",
            ]
        if not any(r in normalized for r in renderings):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=(
                        f"{value.isoformat()} does not appear in the document under "
                        f"its {convention} convention — the date may have been read "
                        f"with a mixed convention; check against the original page"
                    ),
                )
            )
    return issues


def _check_contract_against_raw_text(
    c: Contract, raw_text: str
) -> list[ValidationIssue]:
    """Require the extracted fields to be findable in the document.

    A contract has none of the internal redundancy that protects an
    invoice: no arithmetic, no totals cross-checking each other. A party
    name the model reworded — or invented — is indistinguishable from a
    correct one by any rule about the extraction alone. So the check asks
    the one question the model doesn't get to answer: is this string
    actually on the page?
    """
    issues: list[ValidationIssue] = []
    normalized_text = _normalize(raw_text)

    named_parties = [
        (f"{field}[{i}]", value)
        for field, names in (("parties_a", c.parties_a), ("parties_b", c.parties_b))
        for i, value in enumerate(names)
    ]
    for field, value in named_parties:
        if not value.strip():
            continue
        if not _normalize(value):
            # Seen on a real CUAD contract: the provider's name is redacted
            # to "[ * * * ]", which the model transcribed faithfully. The
            # extraction isn't wrong, but the field is unusable, and saying
            # "not in the document" would be a lie — it's there five times.
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"{value!r} carries no usable name — the document appears to redact this party",
                )
            )
        elif not _appears_in(value, normalized_text):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"{value!r} does not appear anywhere in the document text",
                )
            )

    for field, value in (
        ("contract_title", c.contract_title),
        ("governing_law", c.governing_law),
    ):
        if value and value.strip() and not _appears_in(value, normalized_text):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"{value!r} does not appear in the document text",
                    severity="warning",
                )
            )

    for field, value in (
        ("effective_date", c.effective_date),
        ("expiration_date", c.expiration_date),
    ):
        if value is None:
            continue
        if any(_appears_in(r, normalized_text) for r in _date_renderings(value)):
            continue
        issues.append(
            ValidationIssue(
                field=field,
                message=(
                    f"{value.isoformat()} could not be found in the document text in any common "
                    f"date format — contracts often spell dates out, so this is worth a look "
                    f"rather than proof of an error"
                ),
                severity="warning",
            )
        )

    return issues


def validate_contract(
    c: Contract, raw_text: str | None = None
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for field, names in (("parties_a", c.parties_a), ("parties_b", c.parties_b)):
        if not [n for n in names if n.strip()]:
            issues.append(
                ValidationIssue(field=field, message="no party named on this side")
            )

    # An entity can't contract with itself. Comparing raw strings misses the
    # common case, because "Acme Corp" and "Acme Corporation" are one
    # company wearing two spellings.
    for a in c.parties_a:
        for b in c.parties_b:
            core_a, core_b = _core_name(a), _core_name(b)
            if not core_a or not core_b:
                continue
            if core_a == core_b:
                issues.append(
                    ValidationIssue(
                        field="parties_b",
                        message=f"{a!r} and {b!r} are the same entity on both sides",
                    )
                )
            elif core_a in core_b or core_b in core_a:
                issues.append(
                    ValidationIssue(
                        field="parties_b",
                        message=(
                            f"{a!r} and {b!r} share a name — possibly one entity written two "
                            f"ways, possibly a parent and its subsidiary"
                        ),
                        severity="warning",
                    )
                )

    if c.expiration_date and c.expiration_date < c.effective_date:
        issues.append(
            ValidationIssue(
                field="expiration_date",
                message="expiration_date is before effective_date",
            )
        )

    # Contracts can be signed ahead of time and can run for decades (leases,
    # licences), so both windows are wider than an invoice's.
    issues.extend(
        _check_date_range("effective_date", c.effective_date, max_years_ahead=10)
    )
    issues.extend(
        _check_date_range("expiration_date", c.expiration_date, max_years_ahead=100)
    )

    if not c.key_obligations:
        issues.append(
            ValidationIssue(
                field="key_obligations",
                message="no obligations extracted",
                severity="warning",
            )
        )

    if raw_text is not None:
        issues.extend(_check_contract_against_raw_text(c, raw_text))
        issues.extend(
            _check_material_locations(
                c,
                raw_text,
                ("contract_title", "parties_a", "parties_b", "effective_date"),
            )
        )

    return issues


_IATA_RE = re.compile(r"^[A-Z]{3}$")
_FLIGHT_RE = re.compile(r"^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$")
_PNR_RE = re.compile(r"^[A-Z0-9]{6}$")


def validate_boarding_pass(
    bp: BoardingPass, raw_text: str | None = None
) -> list[ValidationIssue]:
    """A boarding pass has no sums to reconcile, so the checks are all about
    controlled vocabularies: IATA station codes, a carrier-prefixed flight
    number, a six-character record locator. Those formats are exact, which
    makes a violation proof of a bad read rather than a hint.
    """
    issues: list[ValidationIssue] = []

    if not bp.passenger_name.strip():
        issues.append(
            ValidationIssue(field="passenger_name", message="empty passenger name")
        )

    for field, code in (
        ("departure_airport", bp.departure_airport),
        ("arrival_airport", bp.arrival_airport),
    ):
        if not _IATA_RE.match(code.strip().upper()):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"{code!r} is not a three-letter IATA station code",
                )
            )

    if bp.departure_airport.strip().upper() == bp.arrival_airport.strip().upper():
        issues.append(
            ValidationIssue(
                field="arrival_airport",
                message="departure and arrival airports are the same",
            )
        )

    if not _FLIGHT_RE.match(bp.flight_number.strip().upper()):
        issues.append(
            ValidationIssue(
                field="flight_number",
                message=f"{bp.flight_number!r} doesn't look like a carrier code plus flight number",
            )
        )

    if not _PNR_RE.match(bp.booking_reference.strip().upper()):
        issues.append(
            ValidationIssue(
                field="booking_reference",
                message=f"{bp.booking_reference!r} is not a six-character record locator",
            )
        )

    issues.extend(
        _check_date_range(
            "departure_datetime", bp.departure_datetime.date(), max_years_ahead=2
        )
    )

    if raw_text is not None:
        normalized_text = _normalize(raw_text)
        for field, value in (
            ("booking_reference", bp.booking_reference),
            ("flight_number", bp.flight_number),
        ):
            if value.strip() and not _appears_in(value, normalized_text):
                issues.append(
                    ValidationIssue(
                        field=field,
                        message=f"{value!r} does not appear in the document text",
                    )
                )
        issues.extend(
            _check_material_locations(
                bp,
                raw_text,
                (
                    "passenger_name",
                    "booking_reference",
                    "flight_number",
                    "departure_airport",
                    "arrival_airport",
                    "departure_datetime",
                ),
            )
        )

    return issues


_VALIDATORS = {
    Invoice: validate_invoice,
    Receipt: validate_receipt,
    Contract: validate_contract,
    BoardingPass: validate_boarding_pass,
}


def validate(
    document,
    raw_text: str | None = None,
    *,
    pages: list[str] | None = None,
    witness_pages: list[str | None] | None = None,
    vlm_unconfirmed: bool = False,
) -> list[ValidationIssue]:
    # Page identity comes from acquisition, never from whether OCR happened
    # to include a synthetic marker. Serialize the same 1-based page contract
    # used by extraction for every input format, including single images.
    if pages is not None:
        raw_text = "\n".join(
            f"[PAGE {number}]\n{text}" for number, text in enumerate(pages, 1)
        )
    validator = _VALIDATORS.get(type(document))
    if validator is None:
        raise TypeError(f"No validator registered for {type(document)}")
    if validator in (validate_invoice, validate_receipt):
        return validator(document, raw_text, witness_pages, vlm_unconfirmed)
    return validator(document, raw_text)
