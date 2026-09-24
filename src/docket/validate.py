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
from .catalog.common import Party
from .catalog.models import (
    AcceptanceAct,
    BankStatement,
    BoardingPass,
    Contract,
    CreditNote,
    Invoice,
    PurchaseOrder,
    Receipt,
    Waybill,
)
from .catalog.registry import SchemaSpec, ValidationContext
from .schemas import ValidationIssue

_TAX_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]{4,20}$", re.I)
_BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
_AMOUNT_TOLERANCE = 0.01

# A number that is a rate, not an amount. Not a keyword heuristic — it's the
# difference between "IVA 21% 259,26" meaning a tax of 21 and a tax of 259.26.
# Parentheses are optional: English invoices write "Tax (21%)", Spanish ones
# "IVA 21%", and reading the 21 as money is wrong in both.
_PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")


def _isclose(a: float, b: float, tol: float = _AMOUNT_TOLERANCE) -> bool:
    """Equal to within an absolute tolerance, in currency units.

    Absolute, not relative: EN 16931 totals must agree to the cent, and a
    1 % relative tolerance let a total of 1,100.00 differ from its components
    by 10.00 unnoticed. The epsilon absorbs binary floating-point error.
    """
    return abs(a - b) <= tol + 1e-9


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


def _item_numeric_fields(document) -> dict[str, float]:
    """Every numeric value of every repeated-list row, keyed by its schema
    path (spec.line_items knows where each schema keeps them). Line items are
    where a model fabricates least visibly — a made-up row that balances the
    totals — so their grounding is checked like any top-level amount."""
    from .catalog import for_model

    spec = for_model(type(document))
    spec_items = spec.line_items if spec is not None else None
    if spec_items is None:
        return {}
    out: dict[str, float] = {}
    for i, _item in enumerate(value_at(document, spec_items.path) or []):
        for attr in set(spec_items.columns.values()):
            value = value_at(document, f"{spec_items.path}[{i}].{attr}")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[f"{spec_items.path}[{i}].{attr}"] = float(value)
    return out


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
    item_fields = _item_numeric_fields(document)
    numeric_fields = {**item_fields, **numeric_fields}

    for field, value in numeric_fields.items():
        source_text = normalized_text
        location = locations.get(field)
        cited = location.quote if location is not None else None
        if not cited or not cited.strip():
            # Line items get no derived-value warning: a row without a citation
            # is a coverage gap the benchmark measures, while dozens of
            # warnings per document would drown the real signals.
            if "[PAGE " in raw_text and field not in item_fields:
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
            # Not printed — but the row can still ground the value: a derived
            # unit price (4.98 / 2 = 2.49), a line total (2 x 58.50 = 117.00).
            # Only pairs of numbers actually on the cited line count, so an
            # invented amount never derives from a real row.
            derived = any(
                (_isclose(a / b, value, tol=0.02) if b else False)
                or _isclose(a * b, value, tol=0.02)
                for a in stated
                for b in stated
                if a != b
            )
            # A quantity of 1 is the implicit single item: receipts print one
            # price per row and never a "1" — quantity 1 is the schema default
            # and the multiplicative identity, not a fabricated amount.
            implicit_one = value == 1 and field.endswith(".quantity")
            if not derived and not implicit_one:
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
        is_verified_arithmetic = False
        if field == "total_amount" and hasattr(document, "subtotal") and hasattr(document, "tax_amount"):
            sub = getattr(document, "subtotal", 0.0) or 0.0
            tax = getattr(document, "tax_amount", 0.0) or 0.0
            ship = getattr(document, "shipping_amount", 0.0) or 0.0
            disc = getattr(document, "discount_amount", 0.0) or 0.0
            items_sum = sum(
                getattr(li, "total", 0.0) for li in getattr(document, "line_items", [])
            )
            if _isclose(sub + tax + ship - disc, value) and (
                not getattr(document, "line_items", None) or _isclose(items_sum, sub)
            ):
                is_verified_arithmetic = True
        if "[" in field:
            # An item value contradicted by a garbled witness on a degraded
            # page is a warning, not a blocker, when the rows close their own
            # arithmetic: the items sum to the document's stated subtotal
            # (under either coupon layout).
            sub = getattr(document, "subtotal", None)
            items = getattr(document, "line_items", None) or getattr(document, "items", None) or []
            if sub:
                items_sum = sum(getattr(li, "total", getattr(li, "price", 0.0)) for li in items)
                discount = getattr(document, "discount_amount", 0.0) or 0.0
                if _isclose(items_sum, sub) or (discount and _isclose(items_sum, sub + discount)):
                    is_verified_arithmetic = True

        severity = "warning" if is_verified_arithmetic else "error"
        issues.append(
            ValidationIssue(
                field=field,
                message=(
                    f"an independent OCR reading of {label!r} gives "
                    f"{', '.join(f'{n:.2f}' for n in witnessed)}, but {field}={value:.2f} — "
                    f"the transcripts disagree; check against the original page"
                ),
                severity=severity,
            )
        )
    return issues


def value_at(document, path: str):
    """Value at a dotted field path ('seller.name', 'items[0].price'); None
    where any step is missing."""
    current = document
    for part in path.split("."):
        name, _, index = part.partition("[")
        current = getattr(current, name, None)
        if current is None:
            return None
        if index:
            try:
                current = current[int(index.rstrip("]"))]
            except (IndexError, ValueError, TypeError):
                return None
    return current


def _check_material_locations(
    document, raw_text: str, fields: tuple[str, ...]
) -> list[ValidationIssue]:
    """Require a page and exact quote for key fields in page-aware pipeline input."""
    if "[PAGE " not in raw_text:
        return []
    issues: list[ValidationIssue] = []
    locations = getattr(document, "field_locations", None) or {}
    for field in fields:
        value = value_at(document, field)
        if value is None or value == "" or value == []:
            continue
        location = locations.get(field)
        if location is None:
            # A list field is properly cited element-wise: 'parties_a[0]' is
            # the citation for the whole 'parties_a' requirement.
            location = next((loc for key, loc in locations.items() if key.startswith(f"{field}[")), None)
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


def _numeric_date_readings(first: str, second: str, year: str) -> set[date]:
    """Every date a printed d/m/y or m/d/y triple can mean."""
    y = int(year)
    if len(year) == 2:
        y += 2000 if y < 70 else 1900
    readings = set()
    for day, month in ((int(first), int(second)), (int(second), int(first))):
        try:
            readings.add(date(y, month, day))
        except ValueError:
            pass
    return readings


def _check_cited_dates(document, raw_text: str) -> list[ValidationIssue]:
    """A date must be read from the line it cites.

    The model sometimes invents a date it could not read (2020-01-01,
    2024-01-01 on thermal receipts) and cites any line on the page, the
    merchant name or a TAX INVOICE header. The quote exists, so the location
    check passes; the value was never on it. Only two cases are errors, both
    certain without knowing the language: a cited line with no digit at all,
    and a cited line whose numeric dates all mean something else. Dates
    spelled with month names are left alone.
    """
    if "[PAGE " not in raw_text:
        return []
    locations = getattr(document, "field_locations", None) or {}
    issues: list[ValidationIssue] = []
    for field in type(document).model_fields:
        value = getattr(document, field, None)
        location = locations.get(field)
        if not isinstance(value, date) or location is None or not (location.quote or "").strip():
            continue
        quote = location.quote
        short = quote if len(quote) <= 60 else quote[:57] + "..."
        if not re.search(r"\d", quote):
            issues.append(ValidationIssue(
                field=field,
                message=f"{value.isoformat()} cites {short!r}, which holds no date — the value was not read there",
            ))
            continue
        printed = _NUMERIC_DATE_RE.findall(quote) + _ISO_DATE_RE.findall(quote)
        readings = set()
        for groups in printed:
            if len(groups[0]) == 4:  # yyyy-mm-dd
                try:
                    readings.add(date(int(groups[0]), int(groups[1]), int(groups[2])))
                except ValueError:
                    pass
            else:
                readings |= _numeric_date_readings(*groups)
        if printed and value not in readings:
            issues.append(ValidationIssue(
                field=field,
                message=f"{value.isoformat()} cites {short!r}, whose printed date cannot be read as that day",
            ))
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


def _check_party_ids(party: Party | None, prefix: str) -> list[ValidationIssue]:
    """Checksums and formats of every tax identifier a party carries."""
    issues: list[ValidationIssue] = []
    if party is None:
        return issues
    for n, tax in enumerate(party.tax_ids):
        field = f"{prefix}.tax_ids[{n}]"
        value = tax.value
        # The scheme is the extraction model's reading of the label, not
        # something the document asserts: a "Tax ID: GB-771-4402" gets filed
        # as VAT for its GB prefix, though GB VAT numbers have 9 or 12 digits.
        # Only a value with its country's VAT format is held to the VAT
        # checksum; anything else is checked as a plain tax number.
        if tax.scheme == "vat" and checksums.vat_format_ok(value) is not False:
            vat_ok = checksums.validate_vat(value)
            if vat_ok is False:
                issues.append(ValidationIssue(field=field, message=f"{value!r} fails its country's VAT checksum"))
            elif vat_ok is None:
                issues.append(
                    ValidationIssue(
                        field=field,
                        message=(
                            f"{value!r} — format looks plausible but no checksum algorithm is "
                            "implemented for this country, so it isn't verified"
                        ),
                        severity="warning",
                    )
                )
            continue
        tax_ok, scheme = checksums.validate_tax_id(value)
        if tax_ok is False:
            issues.append(
                ValidationIssue(field=field, message=f"{value!r} fails the {scheme} checksum or format")
            )
        elif tax_ok is None and not _TAX_ID_RE.match(value):
            issues.append(
                ValidationIssue(field=field, message=f"{value!r} doesn't look like a tax ID", severity="warning")
            )
    return issues


def validate_billing(inv, ctx: ValidationContext) -> list[ValidationIssue]:
    """Invoice, tax invoice and credit note: one set of rules, since they
    share their structure (see catalog.models._Billing)."""
    raw_text, witness_pages = ctx.raw_text, ctx.witness_pages
    issues: list[ValidationIssue] = []
    if ctx.vlm_unconfirmed:
        issues.append(_unconfirmed_vlm_issue())

    number_field = "credit_note_number" if isinstance(inv, CreditNote) else "invoice_number"
    if not getattr(inv, number_field).strip():
        issues.append(ValidationIssue(field=number_field, message=f"empty {number_field.replace('_', ' ')}"))

    if inv.due_date and inv.due_date < inv.issue_date:
        issues.append(
            ValidationIssue(field="due_date", message="due_date is before issue_date")
        )

    # An invoice records something already billed, so its issue date can't be
    # meaningfully in the future; payment terms can run a while, so due dates
    # get more room.
    issues.extend(_check_date_range("issue_date", inv.issue_date, max_years_ahead=1))
    issues.extend(_check_date_range("due_date", inv.due_date, max_years_ahead=10))

    issues.extend(_check_party_ids(inv.seller, "seller"))
    issues.extend(_check_party_ids(inv.buyer, "buyer"))

    account = inv.payment_account
    if account is not None and account.iban and not checksums.validate_iban(account.iban):
        # Two different findings with two different remedies: a real IBAN
        # with a bad check digit means compare the digits, a string that was
        # never an IBAN means ask the vendor for the number. Reporting the
        # second as the first sends a reviewer looking for a typo that isn't
        # there — seen on a template whose IBAN field still read "[IBAN code]".
        clean_iban = account.iban.replace(" ", "").upper()
        if clean_iban[:2] in checksums._NON_IBAN_COUNTRIES:
            message = (
                f"{account.iban!r} starts with country code {clean_iban[:2]!r}, but the "
                "United States / Canada does not use IBANs (use ABA Routing / Transit number instead)"
            )
        elif checksums.is_iban_shaped(account.iban):
            message = f"{account.iban!r} fails the IBAN mod-97 checksum"
        else:
            message = (
                f"{account.iban!r} is not an IBAN — the document states no usable "
                f"account number for this vendor"
            )
        issues.append(ValidationIssue(field="payment_account.iban", message=message))

    if account is not None and account.bic:
        clean_bic = account.bic.replace(" ", "").upper()
        if not _BIC_RE.match(clean_bic):
            issues.append(
                ValidationIssue(
                    field="payment_account.bic",
                    message=f"{account.bic!r} is not a valid 8- or 11-character SWIFT/BIC code",
                    severity="warning",
                )
            )

    if _core_name(inv.seller.name) and _core_name(inv.seller.name) == _core_name(inv.buyer.name):
        issues.append(
            ValidationIssue(
                field="buyer.name",
                message="seller and buyer resolve to the same entity",
                severity="warning",
            )
        )

    if inv.tax_rate_percent is not None:
        if inv.tax_rate_percent < 0 or inv.tax_rate_percent > 100:
            issues.append(
                ValidationIssue(
                    field="tax_rate_percent",
                    message=f"tax_rate_percent ({inv.tax_rate_percent}) is out of reasonable range (0-100%)",
                    severity="warning",
                )
            )
        elif inv.subtotal > 0:
            expected_tax = round(inv.subtotal * (inv.tax_rate_percent / 100.0), 2)
            if abs(inv.tax_amount - expected_tax) > 0.05:
                issues.append(
                    ValidationIssue(
                        field="tax_rate_percent",
                        message=(
                            f"tax_rate_percent ({inv.tax_rate_percent}%) on subtotal ({inv.subtotal:.2f}) "
                            f"yields {expected_tax:.2f}, but tax_amount is {inv.tax_amount:.2f}"
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


def validate_receipt(rec: Receipt, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
    witness_pages, vlm_unconfirmed = ctx.witness_pages, ctx.vlm_unconfirmed
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

    if rec.merchant_tax_id:
        if checksums.is_vat_shaped(rec.merchant_tax_id):
            vat_ok = checksums.validate_vat(rec.merchant_tax_id)
            if vat_ok is False:
                issues.append(
                    ValidationIssue(
                        field="merchant_tax_id",
                        message=f"{rec.merchant_tax_id!r} fails its country's VAT checksum",
                        severity="error",
                    )
                )
            elif vat_ok is None:
                issues.append(
                    ValidationIssue(
                        field="merchant_tax_id",
                        message=(
                            f"{rec.merchant_tax_id!r} — format looks plausible but no checksum "
                            "algorithm is implemented for this country, so it isn't verified"
                        ),
                        severity="warning",
                    )
                )
        else:
            tax_ok, scheme = checksums.validate_tax_id(rec.merchant_tax_id)
            if tax_ok is False:
                issues.append(
                    ValidationIssue(
                        field="merchant_tax_id",
                        message=f"{rec.merchant_tax_id!r} fails the {scheme} checksum or format",
                        severity="error",
                    )
                )
            elif tax_ok is None and not _TAX_ID_RE.match(rec.merchant_tax_id):
                issues.append(
                    ValidationIssue(
                        field="merchant_tax_id",
                        message=f"{rec.merchant_tax_id!r} doesn't look like a tax ID",
                        severity="warning",
                    )
                )

    if rec.items:
        for idx, item in enumerate(rec.items):
            if item.unit_price is not None and item.quantity > 0:
                expected_line_total = round(item.quantity * item.unit_price, 2)
                if not _isclose(expected_line_total, item.price):
                    issues.append(
                        ValidationIssue(
                            field=f"items[{idx}]",
                            message=(
                                f"quantity ({item.quantity}) * unit_price ({item.unit_price:.2f}) = "
                                f"{expected_line_total:.2f}, does not match line price {item.price:.2f}"
                            ),
                            severity="error",
                        )
                    )

        items_sum = sum(i.price for i in rec.items)
        # Line items are pre-tax and pre-tip, after line discounts. Coupons are
        # extracted as discount_amount, but receipts print them either above
        # the SUBTOTAL (the stated subtotal is then items_sum - discount) or
        # below it (the stated subtotal is then items_sum). Either reading is
        # consistent; only flag when neither closes.
        if rec.subtotal > 0:
            base = rec.subtotal
            ok = any(_isclose(items_sum, b) for b in (rec.subtotal, rec.subtotal + rec.discount_amount))
        else:
            # No subtotal line: back tax, tip and discount out of the total.
            base = rec.total_amount - rec.tax_amount - rec.tip_amount + rec.discount_amount
            ok = _isclose(items_sum, base)
        if not ok:
            issues.append(
                ValidationIssue(
                    field="items",
                    message=(
                        f"items sum to {items_sum:.2f}, but the pre-tax amount they should "
                        f"match is {base:.2f}"
                    ),
                )
            )

    if rec.subtotal > 0:
        # Same ambiguity: the stated subtotal may already have the discount
        # applied, so accept the printed-total arithmetic under either layout.
        without_discount = rec.subtotal + rec.tax_amount + rec.tip_amount
        with_discount = without_discount - rec.discount_amount
        if not any(_isclose(e, rec.total_amount) for e in (with_discount, without_discount)):
            issues.append(
                ValidationIssue(
                    field="total_amount",
                    message=(
                        f"subtotal + tax + tip - discount = {with_discount:.2f} "
                        f"(or, with the discount already in the subtotal, "
                        f"{without_discount:.2f}), total_amount says {rec.total_amount:.2f}"
                    ),
                    severity="error",
                )
            )

    if raw_text is not None:
        numeric_fields = {"total_amount": rec.total_amount}
        if rec.subtotal:
            numeric_fields["subtotal"] = rec.subtotal
        if rec.tax_amount:
            numeric_fields["tax_amount"] = rec.tax_amount
        if rec.tip_amount:
            numeric_fields["tip_amount"] = rec.tip_amount
        if rec.discount_amount:
            numeric_fields["discount_amount"] = rec.discount_amount
        issues.extend(_check_cited_sources(rec, raw_text, numeric_fields))
        witness_fields = [("total_amount", rec.total_amount)]
        if rec.subtotal:
            witness_fields.append(("subtotal", rec.subtotal))
        if rec.tax_amount:
            witness_fields.append(("tax_amount", rec.tax_amount))
        if rec.tip_amount:
            witness_fields.append(("tip_amount", rec.tip_amount))
        if rec.discount_amount:
            witness_fields.append(("discount_amount", rec.discount_amount))
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
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})(?!\d)")


# "404,00" / "1.278,00": a decimal comma. "1,800.00" / "12.50": a decimal point.
_DECIMAL_COMMA_RE = re.compile(r"\d,\d{2}(?!\d)")
_DECIMAL_POINT_RE = re.compile(r"\d\.\d{2}(?!\d)")
# A bare dollar sign on an amount. Currency prefixes are excluded one letter
# back: AU$/US$/S$/HK$/C$ countries write day-first, and guessing month-first
# for them would be a misread, not an abstention.
_DOLLAR_RE = re.compile(r"(?<![A-Z])\$\s?\d")


def _document_date_convention(raw_text: str) -> str | None:
    """"dmy", "mdy" or None, from the document's own evidence.

    First choice: a numeric date with a part above 12 fixes the order. When
    every date is ambiguous (03/09/2026), a bare dollar sign marks a
    month-first document (US-style invoices can print decimal commas too —
    the donut corpus does). Amounts written only with a decimal comma mark a
    continental-European document, where dates are day-first; month-first
    countries write a decimal point. English documents with a decimal point
    and no currency sign stay undecided (US and UK disagree)."""
    return (
        _convention_from_dates(raw_text)
        or _convention_from_currency(raw_text)
        or _convention_from_amounts(raw_text)
    )


def _convention_from_dates(raw_text: str) -> str | None:
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


def _convention_from_amounts(raw_text: str) -> str | None:
    if _DECIMAL_COMMA_RE.search(raw_text) and not _DECIMAL_POINT_RE.search(raw_text):
        return "dmy"
    return None


def _convention_from_currency(raw_text: str) -> str | None:
    """The dollar sign outranks the decimal comma when both appear: US-style
    documents that print comma amounts (the donut invoices, and US vendors
    pandering to European eyes) still write month-first dates."""
    return "mdy" if _DOLLAR_RE.search(raw_text) else None


def _check_date_convention(
    fields: list[tuple[str, date]], raw_text: str
) -> list[ValidationIssue]:
    """Flag extracted dates that contradict the document's own convention.

    With unambiguous dates (a part above 12) pointing one way, a date with
    no matching rendering under that convention was read the other way —
    the Nov-2/Feb-24 mix-up. With only the weaker decimal-comma cue, a date
    is flagged only when the text shows it written the other way round
    (9 March extracted, "03/09/2026" printed), never merely for being absent.
    """
    from_dates = _convention_from_dates(raw_text)
    convention = (
        from_dates or _convention_from_currency(raw_text) or _convention_from_amounts(raw_text)
    )
    if convention is None:
        return []
    normalized = _normalize(raw_text)

    def renderings(value: date, order: str) -> list[str]:
        short_year = value.year % 100
        first, second = (value.day, value.month) if order == "dmy" else (value.month, value.day)
        return [
            f"{first:02d} {second:02d} {value.year}",
            f"{first} {second:02d} {value.year}",
            f"{first:02d} {second:02d} {short_year:02d}",
        ]

    other = "mdy" if convention == "dmy" else "dmy"
    issues: list[ValidationIssue] = []
    for field, value in fields:
        if any(r in normalized for r in renderings(value, convention)):
            continue
        if from_dates is None and (
            value.day == value.month or not any(r in normalized for r in renderings(value, other))
        ):
            continue
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
        ("payment_terms", c.payment_terms),
        ("liability_cap", c.liability_cap),
    ):
        if value and value.strip() and not _appears_in(value, normalized_text):
            issues.append(
                ValidationIssue(
                    field=field,
                    message=f"{value!r} does not appear in the document text",
                    severity="warning",
                )
            )

    for i, sig in enumerate(c.signatories):
        if sig and sig.strip() and not _appears_in(sig, normalized_text):
            issues.append(
                ValidationIssue(
                    field=f"signatories[{i}]",
                    message=f"{sig!r} does not appear in the document text",
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


def assess_contract_risks(c: Contract) -> list[str]:
    """Identifies business and legal risk factors deterministically from contract terms."""
    risks: list[str] = []

    # 1. High value with unlimited liability
    if c.contract_value is not None and c.contract_value > 0 and not c.liability_cap:
        risks.append(
            "unlimited liability: high-value contract has no liability cap specified"
        )

    # 2. Auto-renewal trap: agreement auto-renews without unilateral termination for convenience
    if c.auto_renewal and not c.termination_for_convenience:
        risks.append(
            "auto-renewal trap: agreement auto-renews without unilateral termination for convenience"
        )

    # 3. Short notice period: notice period is under 14 days
    if c.notice_period_days is not None and c.notice_period_days < 14:
        risks.append(
            f"short notice period: notice period ({c.notice_period_days} days) is under 14 days"
        )

    # 4. Long cure period: grace period to fix breaches exceeds 60 days
    if c.cure_period_days is not None and c.cure_period_days > 60:
        risks.append(
            f"long cure period: breach cure period ({c.cure_period_days} days) exceeds 60 days"
        )

    return risks


def validate_contract(c: Contract, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
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

    if c.contract_value is not None:
        if c.contract_value < 0:
            issues.append(
                ValidationIssue(
                    field="contract_value",
                    message="contract_value cannot be negative",
                )
            )
        elif c.contract_value > 0 and not c.currency:
            issues.append(
                ValidationIssue(
                    field="currency",
                    message="currency is missing when contract_value is specified",
                    severity="warning",
                )
            )

    if c.currency is not None and c.contract_value is None:
        issues.append(
            ValidationIssue(
                field="contract_value",
                message="currency is specified without a contract_value",
                severity="warning",
            )
        )

    if c.notice_period_days is not None:
        if c.notice_period_days < 0:
            issues.append(
                ValidationIssue(
                    field="notice_period_days",
                    message="notice_period_days cannot be negative",
                )
            )
        elif c.notice_period_days > 365:
            issues.append(
                ValidationIssue(
                    field="notice_period_days",
                    message=f"notice_period_days ({c.notice_period_days}) exceeds 365 days",
                    severity="warning",
                )
            )

    if c.cure_period_days is not None:
        if c.cure_period_days < 0:
            issues.append(
                ValidationIssue(
                    field="cure_period_days",
                    message="cure_period_days cannot be negative",
                )
            )
        elif c.cure_period_days > 180:
            issues.append(
                ValidationIssue(
                    field="cure_period_days",
                    message=f"cure_period_days ({c.cure_period_days}) exceeds 180 days",
                    severity="warning",
                )
            )

    # Assess and record contract risk factors
    risks = assess_contract_risks(c)
    for risk in risks:
        if risk not in c.risk_factors:
            c.risk_factors.append(risk)
        issues.append(
            ValidationIssue(
                field="risk_factors",
                message=risk,
                severity="warning",
            )
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

    return issues


_IATA_RE = re.compile(r"^[A-Z]{3}$")
_FLIGHT_RE = re.compile(r"^[A-Z0-9]{2,3}\s?\d{1,4}[A-Z]?$")
_PNR_RE = re.compile(r"^[A-Z0-9]{6}$")


def validate_boarding_pass(bp: BoardingPass, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
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

    return issues


def validate_purchase_order(po: PurchaseOrder, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
    issues: list[ValidationIssue] = []
    if not po.po_number.strip():
        issues.append(
            ValidationIssue(
                field="po_number", message="empty PO number", severity="error"
            )
        )
    if not po.supplier.name.strip():
        issues.append(ValidationIssue(field="supplier.name", message="empty supplier name"))
    if not po.buyer.name.strip():
        issues.append(ValidationIssue(field="buyer.name", message="empty buyer name"))

    if _core_name(po.supplier.name) == _core_name(po.buyer.name):
        issues.append(
            ValidationIssue(
                field="buyer.name",
                message="supplier and buyer resolve to the same entity",
                severity="error",
            )
        )
    issues.extend(_check_party_ids(po.supplier, "supplier"))
    issues.extend(_check_party_ids(po.buyer, "buyer"))

    issues.extend(_check_date_range("po_date", po.po_date, max_years_ahead=1))

    expected_total = round(po.subtotal + po.tax_amount, 2)
    if not _isclose(expected_total, po.total_amount):
        issues.append(
            ValidationIssue(
                field="total_amount",
                message=f"subtotal ({po.subtotal:.2f}) + tax ({po.tax_amount:.2f}) = {expected_total:.2f}, total_amount says {po.total_amount:.2f}",
                severity="error",
            )
        )

    if po.line_items:
        for idx, item in enumerate(po.line_items):
            expected_line = round(item.quantity * item.unit_price, 2)
            if not _isclose(expected_line, item.total):
                issues.append(
                    ValidationIssue(
                        field=f"line_items[{idx}]",
                        message=f"quantity ({item.quantity}) * unit_price ({item.unit_price:.2f}) = {expected_line:.2f}, total says {item.total:.2f}",
                        severity="error",
                    )
                )
        items_sum = round(sum(i.total for i in po.line_items), 2)
        if not _isclose(items_sum, po.subtotal):
            issues.append(
                ValidationIssue(
                    field="line_items",
                    message=f"line items sum to {items_sum:.2f}, subtotal is {po.subtotal:.2f}",
                    severity="error",
                )
            )

    return issues


def validate_bank_statement(stmt: BankStatement, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
    issues: list[ValidationIssue] = []

    if not stmt.bank_name.strip():
        issues.append(
            ValidationIssue(
                field="bank_name", message="empty bank name", severity="error"
            )
        )
    if not stmt.account_holder.strip():
        issues.append(
            ValidationIssue(
                field="account_holder",
                message="empty account holder name",
                severity="error",
            )
        )

    if stmt.statement_period_start > stmt.statement_period_end:
        issues.append(
            ValidationIssue(
                field="statement_period_start",
                message=f"statement period start ({stmt.statement_period_start}) is after end ({stmt.statement_period_end})",
                severity="error",
            )
        )

    # IBAN validation
    if stmt.account_iban:
        if not checksums.validate_iban(stmt.account_iban):
            issues.append(
                ValidationIssue(
                    field="account_iban",
                    message=f"{stmt.account_iban!r} fails IBAN checksum or format",
                    severity="error",
                )
            )

    # Balance reconciliation formula:
    # opening_balance + total_deposits - total_withdrawals == closing_balance
    expected_closing = round(
        stmt.opening_balance + stmt.total_deposits - stmt.total_withdrawals, 2
    )
    if not _isclose(expected_closing, stmt.closing_balance):
        diff = round(abs(expected_closing - stmt.closing_balance), 2)
        issues.append(
            ValidationIssue(
                field="closing_balance",
                message=(
                    f"opening_balance ({stmt.opening_balance:.2f}) + deposits ({stmt.total_deposits:.2f}) - "
                    f"withdrawals ({stmt.total_withdrawals:.2f}) = {expected_closing:.2f}, "
                    f"does not match closing_balance {stmt.closing_balance:.2f} (diff: {diff:.2f})"
                ),
                severity="error",
            )
        )

    # If transactions are listed, verify sum of deposits and withdrawals
    if stmt.transactions:
        calc_deposits = round(
            sum(tx.amount for tx in stmt.transactions if tx.amount > 0), 2
        )
        calc_withdrawals = round(
            sum(abs(tx.amount) for tx in stmt.transactions if tx.amount < 0), 2
        )

        if stmt.total_deposits > 0 and not _isclose(calc_deposits, stmt.total_deposits):
            issues.append(
                ValidationIssue(
                    field="total_deposits",
                    message=f"sum of deposit transactions ({calc_deposits:.2f}) does not match total_deposits ({stmt.total_deposits:.2f})",
                    severity="error",
                )
            )
        if stmt.total_withdrawals > 0 and not _isclose(
            calc_withdrawals, stmt.total_withdrawals
        ):
            issues.append(
                ValidationIssue(
                    field="total_withdrawals",
                    message=f"sum of withdrawal transactions ({calc_withdrawals:.2f}) does not match total_withdrawals ({stmt.total_withdrawals:.2f})",
                    severity="error",
                )
            )

        # Continuity check if balance_after is provided
        running_balance = stmt.opening_balance
        for idx, tx in enumerate(stmt.transactions):
            running_balance = round(running_balance + tx.amount, 2)
            if tx.balance_after is not None and not _isclose(
                running_balance, tx.balance_after
            ):
                issues.append(
                    ValidationIssue(
                        field=f"transactions[{idx}].balance_after",
                        message=(
                            f"transaction {idx} balance_after says {tx.balance_after:.2f}, "
                            f"but running balance calculates to {running_balance:.2f}"
                        ),
                        severity="error",
                    )
                )

    return issues


def validate_acceptance_act(act: AcceptanceAct, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
    issues: list[ValidationIssue] = []

    if not act.act_number.strip():
        issues.append(
            ValidationIssue(
                field="act_number", message="empty act number", severity="error"
            )
        )
    if not act.customer_name.strip():
        issues.append(
            ValidationIssue(
                field="customer_name", message="empty customer name", severity="error"
            )
        )
    if not act.contractor_name.strip():
        issues.append(
            ValidationIssue(
                field="contractor_name",
                message="empty contractor name",
                severity="error",
            )
        )

    if _core_name(act.customer_name) == _core_name(act.contractor_name):
        issues.append(
            ValidationIssue(
                field="contractor_name",
                message="customer and contractor resolve to the same entity",
                severity="error",
            )
        )

    issues.extend(_check_date_range("act_date", act.act_date, max_years_ahead=1))

    # Total check: subtotal + tax_amount == total_amount
    expected_total = round(act.subtotal + act.tax_amount, 2)
    if not _isclose(expected_total, act.total_amount):
        issues.append(
            ValidationIssue(
                field="total_amount",
                message=f"subtotal ({act.subtotal:.2f}) + tax ({act.tax_amount:.2f}) = {expected_total:.2f}, total_amount says {act.total_amount:.2f}",
                severity="error",
            )
        )

    # Line items check
    if act.items:
        for idx, item in enumerate(act.items):
            expected_item_total = round(item.quantity * item.unit_price, 2)
            if not _isclose(expected_item_total, item.total):
                issues.append(
                    ValidationIssue(
                        field=f"items[{idx}]",
                        message=(
                            f"quantity ({item.quantity}) * unit_price ({item.unit_price:.2f}) = {expected_item_total:.2f}, "
                            f"does not match line total {item.total:.2f}"
                        ),
                        severity="error",
                    )
                )
        items_sum = round(sum(i.total for i in act.items), 2)
        if not _isclose(items_sum, act.subtotal):
            issues.append(
                ValidationIssue(
                    field="items",
                    message=f"items sum to {items_sum:.2f}, but subtotal is {act.subtotal:.2f}",
                    severity="error",
                )
            )

    # Check contractor and customer tax IDs if present
    for field_name, tax_val in [
        ("contractor_tax_id", act.contractor_tax_id),
        ("customer_tax_id", act.customer_tax_id),
    ]:
        if tax_val:
            tax_ok, scheme = checksums.validate_tax_id(tax_val)
            if tax_ok is False:
                issues.append(
                    ValidationIssue(
                        field=field_name,
                        message=f"{tax_val!r} fails the {scheme} checksum or format",
                        severity="error",
                    )
                )

    if not act.claims_waived:
        issues.append(
            ValidationIssue(
                field="claims_waived",
                message="act records reservations or outstanding claims between parties",
                severity="warning",
            )
        )

    return issues


def validate_waybill(wb: Waybill, ctx: ValidationContext) -> list[ValidationIssue]:
    raw_text = ctx.raw_text
    issues: list[ValidationIssue] = []

    if not wb.waybill_number.strip():
        issues.append(
            ValidationIssue(
                field="waybill_number", message="empty waybill number", severity="error"
            )
        )
    if not wb.shipper_name.strip():
        issues.append(
            ValidationIssue(
                field="shipper_name", message="empty shipper name", severity="error"
            )
        )
    if not wb.consignee_name.strip():
        issues.append(
            ValidationIssue(
                field="consignee_name", message="empty consignee name", severity="error"
            )
        )

    if _core_name(wb.shipper_name) == _core_name(wb.consignee_name):
        issues.append(
            ValidationIssue(
                field="consignee_name",
                message="shipper and consignee resolve to the same entity",
                severity="error",
            )
        )

    issues.extend(_check_date_range("waybill_date", wb.waybill_date, max_years_ahead=1))

    if wb.items:
        # Check quantities
        calc_qty = sum(item.quantity for item in wb.items)
        if wb.total_quantity is not None and not _isclose(calc_qty, wb.total_quantity):
            issues.append(
                ValidationIssue(
                    field="total_quantity",
                    message=f"sum of item quantities ({calc_qty}) does not match total_quantity ({wb.total_quantity})",
                    severity="error",
                )
            )

        # Check weights if provided
        items_with_weight = [
            i.gross_weight_kg for i in wb.items if i.gross_weight_kg is not None
        ]
        if items_with_weight and wb.total_gross_weight_kg is not None:
            sum_weight = sum(items_with_weight)
            if not _isclose(sum_weight, wb.total_gross_weight_kg):
                issues.append(
                    ValidationIssue(
                        field="total_gross_weight_kg",
                        message=f"sum of item gross weights ({sum_weight:.2f} kg) does not match total_gross_weight_kg ({wb.total_gross_weight_kg:.2f} kg)",
                        severity="error",
                    )
                )

        # Check pricing if provided
        for idx, item in enumerate(wb.items):
            if item.unit_price is not None and item.total_price is not None:
                expected_p = round(item.quantity * item.unit_price, 2)
                if not _isclose(expected_p, item.total_price):
                    issues.append(
                        ValidationIssue(
                            field=f"items[{idx}].total_price",
                            message=f"quantity ({item.quantity}) * unit_price ({item.unit_price:.2f}) = {expected_p:.2f}, does not match total_price {item.total_price:.2f}",
                            severity="error",
                        )
                    )

        if wb.total_amount is not None:
            items_prices = [
                i.total_price for i in wb.items if i.total_price is not None
            ]
            if items_prices:
                calc_total = round(sum(items_prices), 2)
                if not _isclose(calc_total, wb.total_amount):
                    issues.append(
                        ValidationIssue(
                            field="total_amount",
                            message=f"sum of item prices ({calc_total:.2f}) does not match total_amount ({wb.total_amount:.2f})",
                            severity="error",
                        )
                    )

    return issues


def validate(
    document,
    raw_text: str | None = None,
    *,
    pages: list[str] | None = None,
    witness_pages: list[str | None] | None = None,
    vlm_unconfirmed: bool = False,
    spec: SchemaSpec | None = None,
) -> list[ValidationIssue]:
    """Every deterministic check for one extracted document.

    `spec` is the schema the document was extracted with; when omitted it is
    looked up by the document's model. Citation checks run for the spec's
    cited fields, then each of its validators.
    """
    from .catalog import for_model

    spec = spec or for_model(type(document))
    if spec is None:
        raise TypeError(f"{type(document).__name__} is not a registered schema; pass spec=")
    # Page identity comes from acquisition, never from whether OCR happened
    # to include a synthetic marker. Serialize the same 1-based page contract
    # used by extraction for every input format, including single images.
    if pages is not None:
        raw_text = "\n".join(f"[PAGE {number}]\n{text}" for number, text in enumerate(pages, 1))
    ctx = ValidationContext(
        raw_text=raw_text,
        pages=pages,
        witness_pages=witness_pages,
        vlm_unconfirmed=vlm_unconfirmed,
    )
    issues: list[ValidationIssue] = []
    if raw_text and spec.required_citations:
        issues.extend(_check_material_locations(document, raw_text, spec.required_citations))
    if raw_text:
        issues.extend(_check_cited_dates(document, raw_text))
    for validator in spec.validators:
        issues.extend(validator(document, ctx) or [])
    return issues
