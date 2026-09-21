"""The built-in document models.

Invoice-family documents (invoice, credit note, tax invoice, purchase order)
and every schema added in 2.x are built from the shared blocks in
`common.py` — Party, Address, TaxIdentifier, DocumentReference, Money. The
older flat schemas (receipt, contract, boarding pass, bank statement,
acceptance act, waybill) keep their field layout.

Each model doubles as the contract handed to the LLM (its JSON Schema) and
as the first validator of what comes back. Registration metadata — ids,
versions, keywords, cited fields, rules — lives in `builtin.py`.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .common import (
    Address,
    BankAccount,
    CitedDocument,
    DocumentReference,
    LineItem,
    Money,
    Party,
)


def _magnitude(value: float) -> float:
    """Documents write discounts and credits in accounting parentheses —
    ($371.00) — which a model faithfully transcribes as -371.0. The sign is
    notation, not data: these fields are positive magnitudes by contract."""
    return abs(value)


# ---- invoice family ----------------------------------------------------------


class _Billing(CitedDocument):
    """Fields every invoice-like document shares."""

    issue_date: date
    due_date: date | None = None
    seller: Party = Field(description="Who issues the document and is owed the money (vendor, supplier).")
    buyer: Party = Field(description="Who is billed (customer).")
    buyer_reference: str | None = Field(
        default=None,
        description="Buyer's reference for routing, e.g. a German Leitweg-ID or a cost centre (BT-10), if printed.",
    )
    references: list[DocumentReference] = Field(
        default_factory=list,
        description="Other documents referred to: purchase order, contract, delivery note, original invoice.",
    )
    payment_account: BankAccount | None = Field(
        default=None, description="Seller's bank account for payment, if stated."
    )
    payment_reference: str | None = Field(
        default=None, description="Remittance reference or payment note, if present."
    )
    payment_terms: str | None = Field(default=None, description="e.g. 'Net 30', '2% 10 days'.")
    currency: str = Field(default="USD", min_length=3, max_length=3, description="ISO 4217 code.")
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float = Field(description="Sum of line totals before tax, discount and shipping.")
    tax_rate_percent: float | None = Field(
        default=None, ge=0, le=100, description="Document-wide VAT/GST rate, if a single rate is stated."
    )
    tax_amount: float = 0
    discount_amount: float = Field(
        default=0, description="Positive magnitude of any discount; subtracted to get total_amount."
    )
    shipping_amount: float = Field(
        default=0, description="Positive shipping/handling charge; added to get total_amount."
    )
    total_amount: float

    @field_validator("discount_amount")
    @classmethod
    def _discount_is_a_magnitude(cls, value: float) -> float:
        return _magnitude(value)

    def reference(self, kind: str) -> str | None:
        return next((r.number for r in self.references if r.kind == kind), None)

    @property
    def purchase_order_number(self) -> str | None:
        return self.reference("purchase_order")


class Invoice(_Billing):
    """A request for payment for goods or services."""

    invoice_number: str


class TaxInvoice(Invoice):
    """An invoice that must identify the seller's VAT/GST registration and
    state the tax charged (GST tax invoice, 'Tax Invoice' / 'Steuerrechnung'
    in jurisdictions that require one)."""

    tax_point_date: date | None = Field(
        default=None, description="Date of supply / tax point, if printed separately from the issue date."
    )


class CreditNote(_Billing):
    """A credit against an earlier invoice. Amounts are positive magnitudes of
    the credit, however the document signs them."""

    credit_note_number: str
    reason: str | None = Field(default=None, description="Why the credit is issued, if stated.")

    @field_validator("subtotal", "tax_amount", "total_amount")
    @classmethod
    def _credit_totals_are_magnitudes(cls, value: float) -> float:
        return _magnitude(value)

    @property
    def original_invoice_number(self) -> str | None:
        return self.reference("invoice")


class PurchaseOrder(CitedDocument):
    """An order a buyer issues to a supplier."""

    po_number: str
    po_date: date
    buyer: Party
    supplier: Party
    delivery_address: Address | None = None
    requested_delivery_date: date | None = None
    references: list[DocumentReference] = Field(default_factory=list)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0
    total_amount: float
    payment_terms: str | None = None


# ---- flat 1.x schemas ------------------------------------------------------------


class ReceiptItem(BaseModel):
    description: str
    price: float
    quantity: float = Field(
        default=1.0, gt=0, description="Quantity of items purchased, default 1.0."
    )
    unit_price: float | None = Field(
        default=None,
        description="Price per unit if printed, such that quantity * unit_price == price.",
    )


class Receipt(CitedDocument):
    merchant_name: str
    merchant_tax_id: str | None = Field(
        default=None,
        description="Merchant's tax ID, VAT number, CIF/NIF, or EIN, if present.",
    )
    merchant_address: str | None = Field(
        default=None, description="Merchant store address or location, if printed."
    )
    transaction_date: date
    currency: str = Field(
        default="USD",
        min_length=3,
        max_length=3,
        description="Three-letter ISO currency code (e.g. USD, EUR).",
    )
    receipt_number: str | None = Field(
        default=None,
        description="Receipt or transaction number printed on the receipt, if present.",
    )
    items: list[ReceiptItem] = Field(default_factory=list)
    subtotal: float = Field(
        default=0,
        description="Pre-tax subtotal, if the receipt prints one. 0 if it doesn't.",
    )
    tax_amount: float = Field(
        default=0,
        description="Sales tax charged on top of the subtotal. 0 if the receipt shows none.",
    )
    tip_amount: float = Field(
        default=0,
        description="Tip or gratuity added to the total (e.g. in restaurants or taxis).",
    )
    discount_amount: float = Field(
        default=0,
        description="Positive magnitude of discount or coupon applied to the receipt.",
    )
    total_amount: float
    payment_method: str | None = None
    card_last_four: str | None = Field(
        default=None,
        description="Last 4 digits of the payment card used (e.g. '1234'), if printed.",
    )
    expense_category: str | None = Field(
        default=None,
        description="Expense category (e.g. 'meals', 'travel', 'lodging', 'fuel', 'office_supplies', 'groceries', 'other').",
    )

    @field_validator("discount_amount")
    @classmethod
    def _discount_is_a_magnitude(cls, v: float) -> float:
        return abs(v)

    @field_validator("card_last_four")
    @classmethod
    def _validate_card_last_four(cls, v: str | None) -> str | None:
        if v is not None:
            v_clean = v.strip()
            if not (len(v_clean) == 4 and v_clean.isdigit()):
                raise ValueError("card_last_four must be exactly 4 digits")
            return v_clean
        return v


class Contract(CitedDocument):
    contract_title: str
    # Lists, not strings: a real contract in the eval corpus has two
    # counterparties on one side ("...TELCOSTAR PTE, LTD. ... and Ability
    # Computer & Software Industries Ltd (each and both of them
    # 'Recipient')"). With a single string the model had no way to answer
    # except by concatenating them into a name that appears nowhere.
    parties_a: list[str] = Field(
        description="Every entity on the first side, one per element. Never join names with 'and'."
    )
    parties_b: list[str] = Field(
        description="Every entity on the second side, one per element. Never join names with 'and'."
    )
    effective_date: date
    expiration_date: date | None = None
    governing_law: str | None = None
    contract_value: float | None = Field(
        default=None,
        ge=0,
        description="Total monetary value or fixed fee stated in the contract, if specified.",
    )
    currency: str | None = Field(
        default=None,
        min_length=3,
        max_length=3,
        description="Three-letter ISO currency code (e.g. USD, EUR), if a contract value is specified.",
    )
    payment_terms: str | None = Field(
        default=None,
        description="Payment terms or schedule (e.g. 'within 30 days of invoice', 'Net 30'), if specified.",
    )
    auto_renewal: bool = Field(
        default=False,
        description="True if the agreement automatically renews unless terminated or opted out.",
    )
    notice_period_days: int | None = Field(
        default=None,
        ge=0,
        description="Notice period in days required to terminate or prevent renewal, if specified.",
    )
    liability_cap: str | None = Field(
        default=None,
        description="Limitation of liability or liability cap description (e.g. '12 months of fees', '$100,000'), if stated.",
    )
    termination_for_convenience: bool = Field(
        default=False,
        description="True if either party may terminate without cause upon notice.",
    )
    cure_period_days: int | None = Field(
        default=None,
        ge=0,
        description="Grace or cure period in days to remedy a material breach before termination, if specified.",
    )
    non_solicit: bool = Field(
        default=False,
        description="True if the agreement restricts soliciting or hiring the other party's employees or contractors.",
    )
    signatories: list[str] = Field(
        default_factory=list,
        description="Names and titles of individuals signing or executing the contract, if stated.",
    )
    risk_factors: list[str] = Field(
        default_factory=list,
        description="Identified legal or business risk factors (e.g. unlimited liability, auto-renewal trap).",
    )
    key_obligations: list[str] = Field(default_factory=list)


class BoardingPass(CitedDocument):
    """Airline travel document. Unlike an invoice it has no arithmetic to
    check, but almost every field is drawn from a controlled vocabulary —
    IATA codes, a carrier-prefixed flight number, a six-character booking
    reference — so the validation leans on format rules instead of sums.
    """

    passenger_name: str
    booking_reference: str = Field(
        description="Six-character PNR / record locator, e.g. 'X4H2QP'."
    )
    flight_number: str = Field(
        description="Carrier code plus number, e.g. 'IB3241' or 'BA475'."
    )
    departure_airport: str = Field(
        description="Three-letter IATA code of the origin, e.g. 'BCN'."
    )
    arrival_airport: str = Field(
        description="Three-letter IATA code of the destination, e.g. 'LHR'."
    )
    departure_datetime: datetime
    boarding_time: str | None = None
    seat: str | None = None
    gate: str | None = None
    cabin_class: str | None = None


class BankStatementTransaction(BaseModel):
    transaction_date: date
    value_date: date | None = None
    description: str
    amount: float = Field(
        description="Positive for deposits/credits, negative for withdrawals/debits."
    )
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    balance_after: float | None = None
    reference: str | None = None


class BankStatement(CitedDocument):
    bank_name: str
    account_holder: str
    account_iban: str
    statement_period_start: date
    statement_period_end: date
    currency: str = Field(default="USD", min_length=3, max_length=3)
    opening_balance: float
    closing_balance: float
    total_deposits: float = 0.0
    total_withdrawals: float = 0.0
    transactions: list[BankStatementTransaction] = Field(default_factory=list)


class AcceptanceActItem(BaseModel):
    description: str
    quantity: float = 1.0
    unit_price: float
    total: float
    unit_of_measure: str | None = None


class AcceptanceAct(CitedDocument):
    act_number: str
    act_date: date
    contract_reference: str | None = None
    invoice_reference: str | None = None
    customer_name: str
    customer_tax_id: str | None = None
    contractor_name: str
    contractor_tax_id: str | None = None
    items: list[AcceptanceActItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0.0
    total_amount: float
    currency: str = Field(default="USD", min_length=3, max_length=3)
    claims_waived: bool = Field(
        default=True,
        description="Whether the document confirms services were rendered satisfactorily with no mutual claims.",
    )
    signatories: list[str] = Field(default_factory=list)


class WaybillItem(BaseModel):
    item_name: str
    sku: str | None = None
    quantity: float
    unit_of_measure: str = "pcs"
    unit_price: float | None = None
    total_price: float | None = None
    gross_weight_kg: float | None = None
    net_weight_kg: float | None = None
    package_count: int | None = None


class Waybill(CitedDocument):
    waybill_number: str
    waybill_date: date
    shipper_name: str
    shipper_address: str | None = None
    consignee_name: str
    consignee_address: str | None = None
    carrier_name: str | None = None
    vehicle_number: str | None = None
    items: list[WaybillItem] = Field(default_factory=list)
    total_quantity: float | None = None
    total_gross_weight_kg: float | None = None
    total_net_weight_kg: float | None = None
    total_packages: int | None = None
    total_amount: float | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)


# ---- schemas added in 2.x ----------------------------------------------------------


class MeterReading(BaseModel):
    meter_id: str | None = None
    previous_reading: float | None = None
    current_reading: float | None = None
    consumption: float = Field(description="Units consumed in the period, as printed.")
    unit: str = Field(description="e.g. 'kWh', 'm3', 'GB'.")
    estimated: bool = Field(default=False, description="True if the bill marks the reading as estimated.")


class UtilityCharge(BaseModel):
    description: str
    amount: float


ServiceType = Literal["electricity", "gas", "water", "heating", "telecom", "internet", "waste", "other"]


class UtilityBill(CitedDocument):
    """A periodic bill for electricity, gas, water, telecom or similar."""

    provider: Party
    customer: Party
    account_number: str = Field(description="Customer account or contract number with the provider.")
    bill_number: str | None = None
    issue_date: date
    due_date: date | None = None
    billing_period_start: date
    billing_period_end: date
    service_type: ServiceType
    service_address: Address | None = Field(default=None, description="Where the service is supplied.")
    meter_readings: list[MeterReading] = Field(default_factory=list)
    charges: list[UtilityCharge] = Field(default_factory=list, description="Itemized current charges.")
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    current_charges: float = Field(description="Total of this period's charges, tax included.")
    tax_amount: float = 0
    previous_balance: float | None = Field(default=None, description="Balance carried from the last bill.")
    payments_received: float | None = Field(default=None, description="Payments credited since the last bill.")
    amount_due: float


class DeliveryNoteItem(BaseModel):
    description: str
    sku: str | None = None
    quantity_delivered: float
    quantity_ordered: float | None = Field(default=None, description="Ordered quantity, if the note shows it.")
    unit_of_measure: str | None = None
    batch_number: str | None = None


class DeliveryNote(CitedDocument):
    """A note listing goods handed over to the recipient (Lieferschein,
    bon de livraison, albarán de entrega)."""

    delivery_note_number: str
    delivery_date: date
    supplier: Party
    recipient: Party
    delivery_address: Address | None = None
    references: list[DocumentReference] = Field(
        default_factory=list, description="Purchase order, order confirmation or invoice it relates to."
    )
    items: list[DeliveryNoteItem] = Field(default_factory=list)
    total_packages: int | None = Field(default=None, ge=0)
    total_gross_weight_kg: float | None = Field(default=None, ge=0)
    received_by: str | None = Field(default=None, description="Name of the person who signed for receipt.")


class OriginGoodsItem(BaseModel):
    description: str
    hs_code: str | None = Field(default=None, description="Harmonized System tariff code, digits only.")
    quantity: float | None = None
    unit_of_measure: str | None = None
    gross_weight_kg: float | None = Field(default=None, ge=0)
    marks_and_numbers: str | None = None


class CertificateOfOrigin(CitedDocument):
    """A certificate attesting the country where exported goods were produced."""

    certificate_number: str
    issue_date: date
    certificate_type: Literal["non_preferential", "eur1", "eur_med", "form_a", "other"] = Field(
        default="non_preferential",
        description="Non-preferential (chamber of commerce), EUR.1, EUR-MED, GSP Form A, or other.",
    )
    exporter: Party
    consignee: Party
    producer: Party | None = None
    country_of_origin: str = Field(description="Country of origin as printed (ISO code or name).")
    destination_country: str | None = None
    transport_details: str | None = Field(default=None, description="Means of transport and route, as printed.")
    issuing_authority: str = Field(description="Chamber of commerce or customs office that certified it.")
    goods: list[OriginGoodsItem] = Field(default_factory=list)
    invoice_references: list[DocumentReference] = Field(default_factory=list)
    goods_value: Money | None = Field(default=None, description="Declared value of the goods, if stated.")


class IdDocument(CitedDocument):
    """The printed textual fields of an identity document.

    Only what is printed and machine-readable as text: no face matching, no
    biometrics, no authenticity or identity verification.
    """

    document_kind: Literal["passport", "national_id", "residence_permit", "driving_licence", "other"]
    document_number: str
    issuing_country: str = Field(description="ISO 3166 alpha-2 or alpha-3 code as printed.")
    surname: str
    given_names: str
    date_of_birth: date
    sex: Literal["M", "F", "X"] | None = None
    nationality: str | None = Field(default=None, description="ISO alpha-3 code or name as printed.")
    place_of_birth: str | None = None
    date_of_issue: date | None = None
    date_of_expiry: date | None = None
    issuing_authority: str | None = None
    personal_number: str | None = Field(default=None, description="Optional national personal number.")
    mrz: list[str] = Field(
        default_factory=list,
        description="Machine-readable zone lines exactly as printed ('<' fillers kept), if present.",
    )

    @field_validator("mrz")
    @classmethod
    def _strip_mrz(cls, lines: list[str]) -> list[str]:
        return [line.replace(" ", "").upper() for line in lines if line.strip()]


__all__ = [
    "AcceptanceAct",
    "AcceptanceActItem",
    "BankStatement",
    "BankStatementTransaction",
    "BoardingPass",
    "CertificateOfOrigin",
    "Contract",
    "CreditNote",
    "DeliveryNote",
    "DeliveryNoteItem",
    "IdDocument",
    "Invoice",
    "MeterReading",
    "OriginGoodsItem",
    "PurchaseOrder",
    "Receipt",
    "ReceiptItem",
    "TaxInvoice",
    "UtilityBill",
    "UtilityCharge",
    "Waybill",
    "WaybillItem",
]
