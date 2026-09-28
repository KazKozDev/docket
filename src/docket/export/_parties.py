"""Flatten the shared party/address models for export formats that carry a
name, one address line and one tax number per party."""
from __future__ import annotations

import re

from ..catalog.common import Address, Party

# An EU VAT number is its country prefix plus 8-12 characters: DE136695976,
# ESB12345674, NL123456789B01. The model labels one printed as "USt-IdNr."
# or "NIF" `tax_id` as often as `vat`; the format says which it is.
_VAT_RE = re.compile(
    r"^(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|GB|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI)"
    r"(?=[0-9A-Z]*\d)[0-9A-Z]{8,12}$"
)


def compact(value: str) -> str:
    return value.replace(" ", "").replace("-", "").replace(".", "").upper()


def vat_number(party: Party) -> str | None:
    """The party's VAT number: one labelled `vat`, else any identifier
    written in the EU VAT format."""
    labelled = party.tax_id("vat")
    if labelled:
        return labelled
    return next((t.value for t in party.tax_ids if _VAT_RE.match(compact(t.value))), None)


def tax_number(party: Party) -> str | None:
    """VAT number if the party has one, else any other tax identifier."""
    return vat_number(party) or party.tax_id()


def address_line(address: Address | None) -> str | None:
    """The address as one comma-separated line, or None if there is none."""
    if address is None:
        return None
    parts = [
        address.street,
        address.additional_line,
        " ".join(p for p in (address.postal_code, address.city) if p),
        address.region,
        address.country_code,
    ]
    joined = ", ".join(p for p in parts if p)
    return joined or address.text


def iban(document) -> str | None:
    account = getattr(document, "payment_account", None)
    return account.iban if account is not None else None
