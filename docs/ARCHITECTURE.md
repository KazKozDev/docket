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
|                       Text Acquisition Tier (per page, pluggable)                     |
|   1. Direct PDF Text Layer (pdf_text backend) -> words, boxes, ruled tables           |
|   2. OCR backend (tesseract / plugin) + confidence gate + garbled pre-flight          |
|   3. Fallback backends (vision LLM by default); overruled OCR kept as witness         |
|   -> PageLayout: words, lines, blocks, columns, tables (normalized coordinates)       |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                                  Classification Tier                                  |
|   1. Fast Deterministic Keyword Rules (clear margin over the runner-up)               |
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

## 1. Text Acquisition and Layout

Every page becomes a `PageLayout`: words with normalized boxes (0..1,
top-left origin, upright page), lines, blocks, text columns and tables, plus
the page's original width/height for converting back to pixels or points.
The LLM reads a serialization of that layout; the structured layout stays in
the result as the source of geometry.

### OCR backends

A backend implements `OcrBackend` (`name`, `capabilities`, `availability()`,
`recognize_page()`) and returns a `PageLayout`. Built in:

| Backend | Input | Confidence | Word boxes | Tables | Rotation |
|---|---|---|---|---|---|
| `pdf_text` | PDF text layer (pdfplumber) | – | yes | ruled (drawn borders) + aligned | glyph matrices |
| `tesseract` | rendered page (`image_to_data`) | yes | yes | aligned | OSD |
| `paddle` (optional extra) | rendered page, PaddleOCR 3.x | per line | yes | engine table pipeline (opt-in) + aligned | orientation classifier |
| `vlm` | rendered page, vision LLM | – | – | – | – |

Backends are looked up by name in a registry; plugins register through the
`docket.ocr_backends` entry point, and an `OcrBackend` instance can be passed
straight to `process_document(ocr_backend=...)`. A backend named explicitly
that cannot run (binary missing, language data missing, extra not installed)
is a configuration error raised before any page is read, with the reason and
an install hint. `auto` takes the first installed of `tesseract`, `paddle`.

### PaddleOCR

`pip install "docket-idp[paddle]"`; `import docket` never imports it. Words
come from PaddleOCR's per-token boxes (`return_word_box`), joined at
whitespace; PaddleOCR scores lines, so each word carries its line's score,
and page confidence uses the same character-weighted definition as
Tesseract. `DOCKET_PADDLE_MODEL=mobile` (default) loads PP-OCRv5 mobile
detection + recognition; `medium` lets PaddleOCR pick its default for the
language (PP-OCRv6 medium for Latin scripts). One model reads one script
family, so `DOCKET_OCR_LANGUAGES` must stay within Latin, East Slavic,
Cyrillic, Greek, Arabic, Korean or CJK — `en,ru` is refused at startup.
`DOCKET_PADDLE_TABLES=true` runs `TableRecognitionPipelineV2` on the OCR
result already computed; its cell boxes become `detection="backend"` tables.
Models download once to `~/.paddlex/official_models`; docket disables
PaddleX's model-hoster connectivity probe so cached models load offline.
Observed limit: the orientation classifier left a sparse page (three text
lines) turned 90° uncorrected, where Tesseract OSD corrected it.

### Per-page chain

1. A PDF page with a usable text layer is taken as is. Unusable means fewer
   than 20 characters, or more than 10 % unmapped `(cid:N)` glyphs.
2. Otherwise the primary backend, then each fallback (default: `vlm`). A
   reading is accepted when it has text and, if the backend reports
   confidence, page confidence ≥ `DOCKET_OCR_MIN_CONFIDENCE` (0.60).
   Tesseract's page confidence is the character-weighted share of text in
   lines whose mean word confidence clears the word floor.
3. If nothing is accepted, the best rejected reading is used and the page is
   marked degraded, which sends the document to review.

Mixed PDFs fall out of this naturally. When the accepted reading has no word
boxes (the vision model), the OCR reading it overruled is kept as the page's
**witness**: validation cross-checks the model's numbers against it, and
citations are located in it.

Two escalations re-run the chain with every reading but the last backend's
rejected: before extraction, when a cheap text model judges the OCR text
garbled (`looks_garbled`); after validation, when OCR text passed its gate
but the extraction failed validation (fewer or equal errors wins, ties go to
the re-read).

### Layout analysis

`docket.layout.analysis.build_page` is shared by every backend with word
boxes. Pure geometry, no keywords:

- **Rows**: words overlapping vertically by ≥40 % of the smaller height. An
  engine's own line identity (Tesseract block/paragraph/line) is respected,
  so skewed lines don't interleave.
- **Segments**: a gap wider than 1.5 × the page's median word height splits
  a row; serialized as ` | `.
- **Aligned tables**: ≥2 consecutive rows with ≥3 segments that fall into ≥3
  shared column bands. **Ruled tables** come from pdfplumber's rulings,
  including row/column spans, and take precedence.
- **Text columns**: an ink-free gutter over ≥4 consecutive rows with
  substantial text on both sides (median ≥12 characters, ≥20 % of the page
  width per side). Reading order inside such a region is column-major.
- **Blocks**: consecutive lines in the same column/table with at most one
  line height between them.

Serialization writes lines in reading order, with `[TABLE n: R rows x C
columns]` and `[COLUMN n]` marker lines.

Known limits — the heuristics were checked on synthetic layouts and a few
real scans, not measured on an annotated table/column benchmark:

- A table cell that wraps onto a second line becomes its own row (or breaks
  the table run); it is not merged back into the cell above.
- Two-column tables (description | amount) are not tables — they read as
  lines with a ` | ` separator. Tables need ≥3 columns.
- A borderless table whose columns are separated by less than 1.5 × word
  height is read as plain lines.
- Text columns with narrow gutters (below 1.5 × word height), or with
  short lines (label/value blocks), are read row by row.
- Rotation is corrected in 90° steps; skew is not.
- Upside-down PDF pages with a mirrored text layer, and vertical CJK text,
  are not handled.

### Source locations

The extraction model returns only `page` and `quote` for each field. The
pipeline matches the quote against the page's words (whitespace-free,
case-folded character stream; exact first, then a fuzzy window that must
score ≥0.8) and records `bbox`, `word_ids`, a confidence (match score ×
mean word confidence) and `located_by`. The model is never asked for
coordinates.

Rotation detection with Tesseract OSD added 0.34 s in a single run on one sample page
(`form_funsd_00.png`, Apple Silicon); disable it with
`DOCKET_OCR_DETECT_ROTATION=false` if your scans are always upright.

---

## 2. Classification Tier

Classification determines which Pydantic schema will govern extraction:

1. **Keyword Rules**: Weighted patterns (English and Spanish cues, plus each document's own name in German, French, Italian, Dutch, Portuguese and Polish). If the top type leads the runner-up by a clear margin, it classifies immediately; confidence is the winner's share of all matched weight.
2. **TF-IDF Classifier**: Word and character n-grams over a small embedded corpus of paraphrases in seven languages (EN, ES, DE, FR, IT, NL, PT) for every built-in type. If prediction confidence exceeds `DOCKET_TFIDF_CONFIDENCE_FLOOR`, it skips the LLM call entirely. Skipped when custom document types are registered, since it only knows the built-in ones.
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

## 8. Document Forensics (stamps, signatures, alterations)

`docket.forensics` is a pixel heuristic over Pillow and Tesseract, not a
trained vision model. What it does, and deliberately does not do:

- **Colored stamps and seals**: blue, violet and red ink is separated from
  black print by hue, grouped into clusters on a 16 px grid, and classified by
  geometry: round-ish clusters are seals, red ink is a stamp of any shape.
- **Handwriting and signatures**: colored clusters that are not stamp-shaped,
  plus *black* ink that Tesseract did not recognise as printed words, after
  long straight runs (table rules, signature lines) are removed. Black ink is
  only considered in the signing zone (lower part of the page or next to a
  "Signature / Unterschrift / Firma / Подпись" label), must be at least twice
  as tall as a line of print, wider than tall, away from the page edges and
  not made of straight segments.
- **No black stamps**: to this method a black seal looks like a logo, a table
  cell or a chart, so none are reported.
- **Status stamps**: PAID / BEZAHLT / PAYÉ / PAGADO / ОПЛАЧЕНО, APPROVED /
  GENEHMIGT, VOID / STORNIERT and equivalents are reported only when the word
  is read *inside* a detected stamp (`PAYMENT_STAMP_PRESENT`,
  `VOID_STAMP_PRESENT`).
- **Corrections**: marker words ("corrected", "korrigiert", "corrigé",
  "исправлено", ...) anywhere in the OCR text. Strike-throughs are not detected.
- **Blank template gate**: no signature and no stamp gives
  `UNEXECUTED_TEMPLATE`, which validation turns into an error for contracts,
  acceptance acts and waybills.
- **Confidence** is a score from geometry and position (roundness, size,
  elongation, signing zone, label nearby; lower for black ink), useful for
  ranking and thresholds, not a calibrated probability.

Keyword detection depends on the Tesseract language packs for `DOCKET_OCR_LANGUAGES`
(Russian markers need `rus`).



