"""Pydantic schemas for the document types docket knows how to extract.

Each schema doubles as the contract handed to the LLM (via
`model_json_schema()`) and as the validator that catches malformed
extractions before they reach `validate.py`'s business-rule checks.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator


class DocType(str, Enum):
    INVOICE = "invoice"
    RECEIPT = "receipt"
    CONTRACT = "contract"
    PURCHASE_ORDER = "purchase_order"
    BANK_STATEMENT = "bank_statement"
    ACCEPTANCE_ACT = "acceptance_act"
    WAYBILL = "waybill"
    BOARDING_PASS = "boarding_pass"
    UNKNOWN = "unknown"


class LineItem(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float
    total: float
    sku: str | None = Field(
        default=None, description="Item code, catalog number, or SKU, if present."
    )
    unit_of_measure: str | None = Field(
        default=None,
        description="Unit of measurement (e.g. 'pcs', 'hrs', 'kg', 'month'), if present.",
    )


class SourceLocation(BaseModel):
    """Auditable link from a field to the original page region."""

    page: int = Field(ge=1)
    quote: str = Field(
        min_length=1, description="Exact text copied from the source page."
    )


class Invoice(BaseModel):
    doc_type: DocType = DocType.INVOICE
    invoice_number: str
    issue_date: date
    due_date: date | None = None
    vendor_name: str
    vendor_tax_id: str | None = None
    vendor_iban: str | None = Field(
        default=None,
        description="Vendor's bank account IBAN, if the document states one.",
    )
    vendor_vat_number: str | None = Field(
        default=None,
        description="EU-style VAT registration number (e.g. DE123456789), if present.",
    )
    vendor_bic: str | None = Field(
        default=None, description="Vendor's SWIFT/BIC bank identifier code, if present."
    )
    vendor_address: str | None = Field(
        default=None, description="Vendor's physical or registered address, if present."
    )
    customer_name: str
    customer_tax_id: str | None = Field(
        default=None, description="Customer's Tax ID, VAT number, or EIN, if present."
    )
    customer_address: str | None = Field(
        default=None, description="Customer's physical or billing address, if present."
    )
    purchase_order_number: str | None = Field(
        default=None,
        description="Purchase order (PO) number referenced by the invoice, if present.",
    )
    payment_reference: str | None = Field(
        default=None,
        description="Payment reference, remittance reference, or unstructured payment note, if present.",
    )
    currency: str = Field(default="USD", min_length=3, max_length=3)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float
    tax_rate_percent: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Applicable tax or VAT rate percentage (e.g. 20.0 for 20%), if stated.",
    )
    tax_amount: float = 0
    discount_amount: float = Field(
        default=0,
        description="Positive magnitude of any discount; subtracted from subtotal+tax to get total_amount.",
    )
    shipping_amount: float = Field(
        default=0,
        description="Positive shipping/handling charge; added to subtotal+tax to get total_amount.",
    )
    total_amount: float
    field_locations: dict[str, SourceLocation] = Field(
        default_factory=dict,
        description="Page and exact source region for each material extracted field.",
    )

    @field_validator("discount_amount")
    @classmethod
    def _discount_is_a_magnitude(cls, v: float) -> float:
        """Invoices write discounts in accounting parentheses — ($371.00) —
        which a model faithfully transcribes as -371.0. The sign is notation,
        not data: the schema's contract is a positive magnitude that gets
        subtracted. Normalizing here means the convention can't be violated,
        rather than being documented and hoped for.
        """
        return abs(v)


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


class Receipt(BaseModel):
    doc_type: DocType = DocType.RECEIPT
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)

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


class Contract(BaseModel):
    doc_type: DocType = DocType.CONTRACT
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class BoardingPass(BaseModel):
    """Airline travel document. Unlike an invoice it has no arithmetic to
    check, but almost every field is drawn from a controlled vocabulary —
    IATA codes, a carrier-prefixed flight number, a six-character booking
    reference — so the validation leans on format rules instead of sums.
    """

    doc_type: DocType = DocType.BOARDING_PASS
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class PurchaseOrder(BaseModel):
    doc_type: DocType = DocType.PURCHASE_ORDER
    po_number: str
    po_date: date
    vendor_name: str
    customer_name: str
    currency: str = Field(default="USD", min_length=3, max_length=3)
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float
    tax_amount: float = 0
    total_amount: float
    payment_terms: str | None = None
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class MatchingStatus(str, Enum):
    MATCHED = "matched"
    DISCREPANCY = "discrepancy"
    UNMATCHED = "unmatched"


class DiscrepancyType(str, Enum):
    PRICE_VARIANCE = "price_variance"
    QUANTITY_OVERBILLING = "quantity_overbilling"
    UNORDERED_ITEM = "unordered_item"
    TOTAL_MISMATCH = "total_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    DATE_OUT_OF_BOUNDS = "date_out_of_bounds"
    CURRENCY_MISMATCH = "currency_mismatch"
    PARTY_MISMATCH = "party_mismatch"
    PO_NUMBER_MISMATCH = "po_number_mismatch"
    UNFULFILLED_BILLING = "unfulfilled_billing"


class Discrepancy(BaseModel):
    type: DiscrepancyType
    field: str
    expected: Any
    actual: Any
    difference: float | None = None
    severity: str = "error"  # "error" | "warning"
    message: str


class MatchResult(BaseModel):
    status: MatchingStatus
    matched_amount: float = 0.0
    variance_amount: float = 0.0
    discrepancies: list[Discrepancy] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_matched(self) -> bool:
        return self.status == MatchingStatus.MATCHED and not any(
            d.severity == "error" for d in self.discrepancies
        )


class BankTransaction(BaseModel):
    transaction_date: date
    amount: float
    currency: str = Field(default="USD", min_length=3, max_length=3)
    description: str = ""
    card_last_four: str | None = None
    transaction_id: str | None = None


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


class BankStatement(BaseModel):
    doc_type: DocType = DocType.BANK_STATEMENT
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


class AcceptanceActItem(BaseModel):
    description: str
    quantity: float = 1.0
    unit_price: float
    total: float
    unit_of_measure: str | None = None


class AcceptanceAct(BaseModel):
    doc_type: DocType = DocType.ACCEPTANCE_ACT
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


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


class Waybill(BaseModel):
    doc_type: DocType = DocType.WAYBILL
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
    field_locations: dict[str, SourceLocation] = Field(default_factory=dict)


SCHEMA_BY_DOC_TYPE: dict[DocType, type[BaseModel]] = {
    DocType.INVOICE: Invoice,
    DocType.RECEIPT: Receipt,
    DocType.CONTRACT: Contract,
    DocType.PURCHASE_ORDER: PurchaseOrder,
    DocType.BANK_STATEMENT: BankStatement,
    DocType.ACCEPTANCE_ACT: AcceptanceAct,
    DocType.WAYBILL: Waybill,
    DocType.BOARDING_PASS: BoardingPass,
}


class ClassificationResult(BaseModel):
    # A DocType for built-in types, a plain string for types registered with
    # `docket.register_document_type`. DocType is a str enum, so comparing
    # against a string works for both; `type_name` is always a plain string.
    doc_type: DocType | str
    confidence: float
    method: str  # "rules" | "tfidf" | "llm" | "unavailable"
    scores: dict[str, float] = Field(default_factory=dict)

    @field_validator("doc_type", mode="before")
    @classmethod
    def _builtin_as_enum(cls, value: object) -> object:
        try:
            return DocType(value)
        except ValueError:
            return value

    @property
    def type_name(self) -> str:
        return self.doc_type.value if isinstance(self.doc_type, DocType) else self.doc_type


class ValidationIssue(BaseModel):
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


class StampDetection(BaseModel):
    """Detection of a physical seal, official rubber stamp, or organization stamp."""

    page: int = Field(ge=1, description="1-indexed page number where stamp was found.")
    box: tuple[float, float, float, float] = Field(
        description="Normalized bounding box (ymin, xmin, ymax, xmax) from 0.0 to 1.0."
    )
    color: str = Field(
        default="blue",
        description="Dominant ink color (e.g. blue, violet, red, black).",
    )
    shape: str = Field(
        default="circular",
        description="Geometry of the stamp (circular, oval, rectangular, irregular).",
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Detection confidence score."
    )
    text: str | None = Field(
        default=None,
        description="Decoded text or organization name from stamp, if legible.",
    )


class SignatureDetection(BaseModel):
    """Detection of a handwritten signature, mark, or cursive endorsement."""

    page: int = Field(
        ge=1, description="1-indexed page number where signature was found."
    )
    box: tuple[float, float, float, float] = Field(
        description="Normalized bounding box (ymin, xmin, ymax, xmax) from 0.0 to 1.0."
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Signature detection confidence score."
    )
    signatory_label: str | None = Field(
        default=None,
        description="Contextual role (e.g. Customer, Contractor, Director) if labeled.",
    )


class HandwrittenAnnotation(BaseModel):
    """Detection of handwritten marginalia, numeric corrections, or status stamps."""

    page: int = Field(ge=1, description="1-indexed page number.")
    text: str = Field(description="Transcribed or recognized text.")
    annotation_type: str = Field(
        description="Classification (e.g. payment_stamp, price_correction, approval, marginalia)."
    )
    box: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalized bounding box if isolated."
    )


class DocumentForensicReport(BaseModel):
    """Forensic report covering physical execution, stamps, signatures, and alterations."""

    has_signatures: bool = Field(
        default=False,
        description="Whether at least one handwritten signature is present.",
    )
    has_stamps: bool = Field(
        default=False,
        description="Whether at least one seal or official stamp is present.",
    )
    is_executed: bool = Field(
        default=False,
        description="Whether the document appears legally executed (signed/stamped).",
    )
    is_empty_template: bool = Field(
        default=False,
        description="Whether the document appears to be an unexecuted blank template.",
    )
    stamps: list[StampDetection] = Field(default_factory=list)
    signatures: list[SignatureDetection] = Field(default_factory=list)
    annotations: list[HandwrittenAnnotation] = Field(default_factory=list)
    alterations_detected: bool = Field(
        default=False,
        description="Whether handwritten overrides or crossed-out amounts were detected.",
    )
    risk_flags: list[str] = Field(
        default_factory=list,
        description="Forensic risk flags (e.g. UNEXECUTED_TEMPLATE, MISSING_STAMP, HANDWRITTEN_ALTERATION).",
    )


class PipelineResult(BaseModel):
    source: str
    classification: ClassificationResult
    extracted: dict | None
    field_sources: dict[str, SourceLocation] = Field(
        default_factory=dict,
        description="Where each extracted field was read from — kept beside the data, not inside it.",
    )
    extract_attempts: int
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    ocr_method: str  # "pdf_text" | "ocr" | "vlm" | "ocr_degraded"
    raw_text_chars: int
    language: str = "unknown"  # "en" | "es" | "unknown"
    escalated_to_vlm: bool = False
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    llm_calls: int = 0
    llm_estimated_tokens: int = 0
    pages_total: int = 1
    pages_processed: int = 1
    complete: bool = True
    document_id: str | None = None
    page_methods: list[str] = Field(default_factory=list)
    forensic_report: DocumentForensicReport | None = None

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == "error" for i in self.validation_issues)

    @property
    def document(self) -> BaseModel | None:
        """`extracted` as its typed schema (Invoice, Receipt, …), or None if
        extraction failed or no longer matches the schema."""
        from .doctypes import get_document_type

        doc_type = get_document_type(self.classification.doc_type)
        if doc_type is None or self.extracted is None:
            return None
        schema = doc_type.schema
        try:
            return schema.model_validate(self.extracted)
        except ValidationError:
            return None
