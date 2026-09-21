"""Reusable building blocks for document schemas.

Built-in schemas use these wherever a document names a party, an address, a
tax registration, an amount in its own currency or another document; custom
schemas can use them too, so the same concept serializes the same way
everywhere.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    """What the extraction model returns for a field: the page and the exact
    text it read the value from. Geometry is never asked of the model — the
    pipeline resolves the quote against the page layout into a
    `docket.SourceLocation`."""

    page: int = Field(ge=1)
    quote: str = Field(min_length=1, description="Exact text copied from the source page.")


class CitedDocument(BaseModel):
    """Base class for schemas with source citations.

    The extraction model fills `field_locations` with the page and verbatim
    quote each value came from — keyed by field path, e.g. "total_amount" or
    "seller.name" — and validation checks each quote is really on that page.
    """

    field_locations: dict[str, Citation] = Field(
        default_factory=dict,
        description=(
            "Page and exact source text for each material field, keyed by field path "
            "(e.g. 'invoice_number', 'seller.name')."
        ),
    )


class Address(BaseModel):
    street: str | None = Field(default=None, description="Street and number, as printed.")
    additional_line: str | None = Field(default=None, description="Building, floor, c/o, PO box.")
    postal_code: str | None = None
    city: str | None = None
    region: str | None = Field(default=None, description="State, province or county.")
    country_code: str | None = Field(
        default=None,
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 code (DE, FR, US), if the country is stated or unambiguous.",
    )
    text: str | None = Field(
        default=None, description="The full address exactly as printed, when it can't be split reliably."
    )

    @field_validator("country_code")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


TaxScheme = Literal["vat", "tax_id", "gst", "company_registration", "other"]


class TaxIdentifier(BaseModel):
    value: str = Field(description="The identifier exactly as printed, e.g. 'DE123456789'.")
    scheme: TaxScheme = Field(
        default="tax_id",
        description=(
            "'vat' for an EU-style VAT number, 'gst' for GST/HST registration, "
            "'company_registration' for a trade-register or company number, "
            "'tax_id' for other national tax numbers (EIN, NIF, INN, ...)."
        ),
    )
    country_code: str | None = Field(default=None, min_length=2, max_length=2)


class Party(BaseModel):
    """A company or person the document names: seller, buyer, shipper, ..."""

    name: str
    address: Address | None = None
    tax_ids: list[TaxIdentifier] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    electronic_address: str | None = Field(
        default=None, description="E-invoicing endpoint (e.g. Peppol participant ID), if printed."
    )
    electronic_address_scheme: str | None = Field(
        default=None, description="Scheme of electronic_address, e.g. '0088' (GLN) or '9930' (DE VAT)."
    )

    def tax_id(self, *schemes: str) -> str | None:
        """The first identifier of the given schemes (any scheme if none given)."""
        for tax in self.tax_ids:
            if not schemes or tax.scheme in schemes:
                return tax.value
        return None


class Money(BaseModel):
    """An amount with its own currency, for documents that mix currencies or
    state a value without a document-wide currency."""

    amount: float
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code.")


ReferenceKind = Literal[
    "invoice",
    "credit_note",
    "purchase_order",
    "contract",
    "delivery_note",
    "waybill",
    "order_confirmation",
    "other",
]


class DocumentReference(BaseModel):
    """Another document this one refers to (PO number, original invoice, ...)."""

    kind: ReferenceKind
    number: str
    issue_date: date | None = None


class BankAccount(BaseModel):
    iban: str | None = Field(default=None, description="IBAN, if stated.")
    bic: str | None = Field(default=None, description="SWIFT/BIC code, if stated.")
    account_number: str | None = Field(
        default=None, description="Non-IBAN account number (e.g. US/CA), if stated."
    )
    bank_name: str | None = None


class LineItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float
    total: float
    sku: str | None = Field(default=None, description="Item code, catalog number, or SKU, if present.")
    unit_of_measure: str | None = Field(
        default=None, description="Unit of measurement (e.g. 'pcs', 'hrs', 'kg', 'month'), if present."
    )
    tax_rate_percent: float | None = Field(
        default=None, ge=0, le=100, description="VAT/GST rate for this line, if stated per line."
    )


__all__ = [
    "Address",
    "BankAccount",
    "Citation",
    "CitedDocument",
    "DocumentReference",
    "LineItem",
    "Money",
    "Party",
    "ReferenceKind",
    "TaxIdentifier",
    "TaxScheme",
]
