# Architecture

Docket converts unstructured or semi-structured documents (invoices, receipts, contracts, boarding passes) into validated, auditable JSON objects with line citations and human review escalation.

```
+---------------------------------------------------------------------------------------+
|                                    Document Ingestion                                  |
|   (PDF, PNG, JPG, TIFF, TXT)                                                          |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                                 Text Acquisition Tier                                 |
|   1. Direct PDF Text Layer (pdfplumber)                                               |
|   2. Local OCR (Tesseract) + Garbled Quality Gate                                     |
|   3. Vision-Language Model (Ollama VLM) on low OCR confidence / garbled scans         |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                                  Classification Tier                                  |
|   1. Fast Deterministic Keyword Rules (confidence 1.0)                                |
|   2. TF-IDF Classifier (scikit-learn, confidence floor threshold)                     |
|   3. LLM Zero-shot Classifier (fallback when confidence < floor)                      |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                              Structured Extraction Tier                               |
|   - Pydantic Schemas per document type (Invoice, Receipt, Contract, BoardingPass)     |
|   - JSON Schema contract enforcement via local Ollama LLM                             |
|   - Verbatim Source Citations (field_sources / field_locations)                       |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                             Deterministic Validation Tier                             |
|   - Arithmetic verification (subtotal + tax + shipping - discount = total)           |
|   - Date logic & future bounds checks                                                 |
|   - IBAN mod-97 check digits (ISO 7064) across all European countries & Brazil        |
|   - VAT check digits (all 27 EU member states, GB, CH, NO)                            |
|   - National Tax ID checksums (US EIN, Canadian BN, Brazilian CNPJ/CPF)               |
|   - Verbatim citation existence & exact substring witness checks                      |
+---------------------------------------------------------------------------------------+
                                           |
                   +-----------------------+-----------------------+
                   | (Passed all checks)                           | (Validation Error / Low Conf)
                   v                                               v
+------------------------------------+           +------------------------------------+
|           Validated JSON           |           |         Human Review Queue         |
|  (Clean downstream persistence)    |           |  (data/review_queue.jsonl + UI)    |
+------------------------------------+           +------------------------------------+
```

---

## 1. Text Acquisition Tier

To minimize inference costs and latency, Docket selects the cheapest extraction method that yields reliable text:

1. **Digital PDF Text Layer**: Extracted directly using `pdfplumber`; scanned pages are rendered with `pypdfium2`. Zero model overhead.
2. **Local Tesseract OCR**: Used when no text layer is present.
3. **OCR Quality Pre-flight (`looks_garbled`)**: Checks character distribution and token validity. If a scan is noisy or degraded, passing it to an extraction model wastes computation (garbled inputs take up to $2.5\times$ longer to process).
4. **VLM Transcription Fallback**: Triggered automatically when OCR confidence is low or text is garbled.

---

## 2. Classification Tier

Classification determines which Pydantic schema will govern extraction:

1. **Keyword Rules**: High-precision header and structural pattern matching. If a document matches definitive rules (e.g., unambiguous invoice headers or boarding pass markers), it classifies immediately with confidence `1.0`.
2. **TF-IDF Classifier**: Trained on standard document classes. If prediction confidence exceeds `DOCKET_TFIDF_CONFIDENCE_FLOOR`, it skips the LLM call entirely.
3. **LLM Fallback**: Invoked only when rule-based and TF-IDF classifiers cannot make a confident decision.

---

## 3. Extraction & Verbatim Citations

- **JSON Schema Contracts**: The target schema is defined as a Pydantic model (`Invoice`, `Receipt`, `Contract`, `BoardingPass`, `PurchaseOrder`, `BankStatement`, `AcceptanceAct`, `Waybill`).
- **Verbatim Evidence**: The model must provide verbatim quotes (`quote`, `page`) for extracted values.
- **Multilingual Parsing**: Supports both European (`1.234,56 €`) and American (`$1,234.56`) numerical conventions.

---

## 4. Deterministic Validation

Validation never calls a model. It executes deterministic arithmetic and mathematical checksum algorithms:

- **Totals & Line Items**: Verifies `subtotal + tax + shipping - discount == total_amount` within floating-point tolerance ($0.05$).
- **IBAN**: ISO 7064 MOD 97-10 check digits for all European nations and Brazil. Identifies non-IBAN systems (US, Canada) and requests routing numbers instead.
- **VAT / Sales Tax**: Algorithmic check-digit verification across all 27 EU member states, the UK, Switzerland, and Norway.
- **Americas Tax IDs**: Modulo-11 CNPJ/CPF checks for Brazil, Luhn mod-10 checks for Canadian Business Numbers (BN), and prefix verification for US EINs.
- **B2B Invoicing**: Validates customer tax IDs, ISO 9362 SWIFT/BIC codes, SKU and unit of measure on line items, and mathematical cross-checks tax rate percentage against subtotal and tax amounts.
- **Receipts & Expenses**: Validates retail/restaurant balancing `subtotal + tax + tip - discount == total_amount`, line item pricing `quantity * unit_price == price`, merchant tax IDs (VAT and national), and 4-digit payment card format.
- **Bank Statements**: Validates balance equation `opening_balance + total_deposits - total_withdrawals == closing_balance`, sums of transaction deposits and withdrawals, running balance continuity across consecutive transaction entries, and bank IBAN check digits.
- **Acceptance Acts**: Validates services completion `subtotal + tax == total_amount`, line item pricing `quantity * unit_price == total`, distinct counterparties (customer != contractor), tax ID formats for customer and contractor, and warns if `claims_waived` is false.
- **Waybills / Consignment Notes (CMR, ТОРГ-12)**: Validates physical logistics balancing: sum of item quantities vs `total_quantity`, sum of gross weights vs `total_gross_weight_kg`, line item pricing `quantity * unit_price == price`, distinct consignor and consignee, and carrier tracking.
- **Citation Grounding**: Asserts that every cited quote exists in the document and contains the claimed numerical value.
- **Contract Legal Validation**: Asserts counterparty sanity (an entity cannot contract with itself; parent/subsidiary relationships trigger reviews), verifies that parties, governing law, payment terms, and signatories exist verbatim in the source text, checks term dates and notice/cure period limits, and runs automated risk factor assessment (unlimited liability, auto-renewal trap).

---

## 5. Human Review Queue

Documents that fail any error-level validation rule, fail extraction, or carry low classification confidence are routed to the Review Queue (`data/review_queue.jsonl`):

- Preserves original document artifacts, raw text, and audit trails.
- Accessible via CLI, FastAPI endpoints (`/review-queue`), and Streamlit web UI.

---

## 6. Cross-Document Reconciliation & 3-Way Matching

Deterministic multi-document audits connect extracted records across the procurement and expense lifecycle:

- **3-Way Matching (PO ↔ Waybill ↔ Invoice)**:
  - Reconciles Purchase Order authorizations against Waybill physical deliveries and Invoice billing claims.
  - Detects unit price variances (`PRICE_VARIANCE`) exceeding tolerance when invoice price exceeds PO unit price.
  - Detects unfulfilled billing (`UNFULFILLED_BILLING`) when invoiced quantities exceed physically delivered quantities on the waybill.
  - Verifies counterparty consistency across buyer/consignee/customer and vendor/consignor/seller.
- **Invoice ↔ Purchase Order (2-Way Matching)**:
  - Reconciles line items by SKU or description.
  - Detects unit price variances (`PRICE_VARIANCE`) exceeding configurable thresholds (`price_tolerance_pct`).
  - Detects quantity over-billing (`QUANTITY_OVERBILLING`) and unordered goods (`UNORDERED_ITEM`).
  - Verifies counterparty consistency and total amounts.
- **Contract ↔ Invoices (Budget & Compliance Audit)**:
  - Verifies invoice counterparties belong to the contracted parties.
  - Asserts invoice dates fall within the contract's effective and expiration window.
  - Tracks cumulative invoiced totals against the contract value ceiling (`BUDGET_EXCEEDED`).
- **Receipt ↔ Bank Transactions (Expense Reconciliation)**:
  - Matches receipts against card/bank statements using transaction date windows (clearing delays), exact currency, card last four digits, and total amounts.

---

## 7. Accounting & e-Invoicing Export Tier

Extracted and validated records can be deterministically converted to corporate ERP and standard electronic invoicing formats without external cloud dependencies:

- **1C:Enterprise (1С:Предприятие)**:
  - `export_to_1c_client_bank`: Produces 1CClientBankExchange 1.03 format for bank statements, including opening/closing balances and payment orders with payer/payee IBANs.
  - `export_to_1c_enterprise_xml`: Produces EnterpriseData XML for incoming vendor bills (ПоступлениеТоваровУслуг) and acceptance acts with VAT breakdown.
- **SAP S/4HANA & ERP**:
  - `export_to_sap_idoc`: Standard INVOIC02 IDoc XML with EDI_DC40, E1EDK01 header, E1EDKA1 vendor/customer partners, E1EDP01 line items, and E1EDS01 monetary sums.
  - `export_to_sap_journal_csv`: General ledger and vendor posting CSV with posting keys (40 Debit, 31 Credit, 50 Bank Credit), accounts, tax codes, and currency.
- **QuickBooks**:
  - `export_to_quickbooks_iif`: Intuit Interchange Format (.iif) with !TRNS and !SPL blocks for vendor bills, check expenses, and sales tax.
  - `export_to_quickbooks_json`: QuickBooks Online REST API Bill / Purchase payload with AccountBased and ItemBased line details.
- **Xero**:
  - `export_to_xero_csv`: Official Xero Bills CSV import format with account codes and tax types.
  - `export_to_xero_json`: Xero Accounting API Invoices payload with ACCPAY type and contact details.
- **International e-Invoicing Standards**:
  - **UBL 2.1 (Peppol BIS Billing 3.0 / EN 16931)**: OASIS Universal Business Language XML for cross-border European public procurement and B2B billing.
  - **Facturae 3.2.2**: Official Spanish electronic invoice standard (FACe) with complete Party Tax Identification and TaxesOutputs breakdown.
  - **ZUGFeRD 2.2 / XRechnung**: German UN/CEFACT Cross Industry Invoice (CII) XML supporting both EN 16931 and official German B2G XRechnung profiles.

---

## 8. Computer Vision & Document Forensics Tier

Physical execution verification and forensic analysis are performed on digital scans and photos:

- **Stamp & Seal Detection (`_detect_stamps`)**:
  - Distinguishes chromatic ink (blue, violet, red) from monochrome printed body text using RGB/HSV chromatic isolation.
  - Measures bounding dimensions, cluster ink density, and geometry (circular/oval organization seals vs rectangular approval stamps).
- **Signature Detection (`_detect_signatures`)**:
  - Analyzes cursive ink strokes with high angular variance in signatory regions (footer zones near "Подпись", "M.P.", "Signature").
- **Blank Template Gate (`is_empty_template` / `UNEXECUTED_TEMPLATE`)**:
  - Identifies unexecuted contracts, acts, and delivery notes that have no physical signatures or organization stamps.
  - Generates validation errors and flags for human review, preventing automated payments on draft templates.
- **Handwritten Alterations & Payment Stamps**:
  - Detects status stamps ("ОПЛАЧЕНО", "PAID", "ПОЛУЧЕНО", "VOID", "APPROVED").
  - Detects unauthorized handwritten price/quantity corrections and strike-through annotations on document bodies.



