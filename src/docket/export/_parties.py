"""Flatten the shared party/address models for export formats that carry a
name, one address line and one tax number per party."""
from __future__ import annotations

from ..catalog.common import Address, Party


def vat_number(party: Party) -> str | None:
    return party.tax_id("vat")


def tax_number(party: Party) -> str | None:
    """VAT number if the party has one, else any other tax identifier."""
    return party.tax_id("vat") or party.tax_id()


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


def bic(document) -> str | None:
    account = getattr(document, "payment_account", None)
    return account.bic if account is not None else None
