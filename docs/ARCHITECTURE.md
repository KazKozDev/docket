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
|   - Versioned schema catalog: 14 built-in Pydantic schemas + registered ones          |
|   - JSON Schema contract enforcement via local Ollama LLM                             |
|   - Verbatim Source Citations (field_sources / field_locations)                       |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                             Deterministic Validation Tier                             |
|   - Arithmetic verification to the cent (subtotal + tax + shipping - discount)       |
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
|  (Clean downstream persistence)    |           |  (SQLite/PostgreSQL + review UI)   |
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
| `paddle` (optional extra) | rendered page, PaddleOCR 3.x | per line | yes | engine table pipeline (opt-in) + aligned | orientation classifier + fine deskew |
| `docling` (optional extra) | PDF or image, Docling + TableFormer | – | yes | backend cells, merged/wrapped + aligned | pipeline normalization |
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

### Docling and advanced layout

`pip install "docket-idp[docling]"` enables the lazy `docling` backend. It
uses Docling's standard PDF/image pipeline and TableFormer in `accurate` mode
by default. `DOCKET_DOCLING_TABLE_MODE=fast` trades quality for throughput;
`DOCKET_DOCLING_CELL_MATCHING=false` uses the structure model's own cells when
matching them back to document text merges columns incorrectly. Docling cell
offsets become `row_span` / `column_span`, embedded newlines remain wrapped
cell text, and every table keeps normalized source boxes.

The shared geometry pass also recognizes conservative borderless two-column
numeric tables, while rejecting colon-ended label/value forms. Physical rows
with fewer occupied bands are attached to the preceding logical cells. Raster
OCR applies a projection-based fine deskew before recognition and records the
clockwise correction as `PageLayout.deskew_angle`; 90-degree orientation stays
in `PageLayout.rotation`.

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

Known limits — measured on the annotated golden scans (`eval/benchmark_ocr.py`:
word F1 0.905 Tesseract / 0.994 Paddle mobile, table-cell accuracy 0.62 /
0.90 on 242 expected cells), not on third-party benchmarks:

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

Every occurrence is kept, not just the first: `SourceLocation.regions`
holds each contiguous place the quote was found, and `status` says how it
resolved — `verified` (one exact match), `fuzzy` (no exact match, one close
window), `conflicting` (several exact matches; the document doesn't say
which one is the source), `unlocated` (no geometry or the quote isn't on
the page). `bbox` / `word_ids` remain the first region's coordinates for
back-compatibility. `DocumentResult.highlights(page=None)` returns
`(field, source, region)` triples for drawing provenance boxes over the
original pages, optionally filtered to one page.

Rotation detection with Tesseract OSD added 0.34 s in a single run on one sample page
(`form_funsd_00.png`, Apple Silicon); disable it with
`DOCKET_OCR_DETECT_ROTATION=false` if your scans are always upright.

---

## 2. Pipeline contract

`process_document(source, ProcessOptions) -> DocumentResult` is the one
entry point; the CLI and the HTTP API call it. Its stages are plain
functions in `docket.pipeline`: `acquire` → `select_schema` → `extract` →
`validate_extraction` → `review`; `export_document(result, format)` is the
separate last step and refuses results that failed or need review unless
told otherwise.

- **Options** (`docket.options`): `OcrOptions` (chain, languages, engine
  settings), `document_type` / `schema_model` (either one skips
  classification; given both, they must agree), `classify`,
  `include_layout`, `escalate`, `ReviewOptions` (enqueue, classification
  threshold, queue location). Every unset option comes from `DOCKET_*`, else
  the built-in default; `resolve()` applies that and validates it.
- **Errors**: anything detectable up front — unknown or unavailable backend,
  unknown language, unknown document type, a schema that isn't a Pydantic
  model — raises `ConfigurationError` before the first page is read. A
  document that fails later returns `status="failed"` with `error.code`
  (`unsupported_document`, `unreadable_file`, `no_text`) and `error.stage`.
- **Status**: `failed` if `error` is set, `needs_review` if any review reason
  applies, otherwise `succeeded`. Failed documents are not written to the
  review queue.

---

### Batches

`process_batch(sources, ProcessOptions, BatchOptions)` runs
`process_document` over a directory (optionally recursive and
glob-filtered), a glob pattern or any iterable of paths:

- Sources are discovered lazily and at most `2 × workers` documents are
  submitted ahead of the one being returned, so a large directory is never
  listed or loaded into memory whole; `keep_results=False` plus `on_result`
  streams results out without keeping them.
- Results come back in input order. Each document runs in a fresh context,
  so its LLM usage counters are its own.
- A failing or crashing document becomes a failed `DocumentResult` and a
  `BatchError`; `fail_fast` stops submitting after the first one and counts
  the rest as `skipped`.
- `docket.limits` bounds the expensive calls process-wide: at most
  `DOCKET_LLM_CONCURRENCY` LLM requests and `DOCKET_OCR_CONCURRENCY` OCR
  engines at once, whatever the number of workers or HTTP jobs.
- A checkpoint (JSON Lines of finished results) is appended as documents
  finish; a rerun reuses the result of any source whose content hash is
  unchanged. Failed results are retried.
- `BatchResult` carries counts (`total`, `succeeded`, `needs_review`,
  `failed`, `skipped`), errors, elapsed time and aggregate metrics (pages,
  LLM calls and tokens, VLM pages, escalations, mean/median seconds per
  document, time per stage).

`docket.export.tabular` renders results as a fixed-column summary CSV (each
schema's `summary` map fills `document_number`, `document_date`, `issuer`,
`recipient`, `currency`, `subtotal`, `tax_amount`, `total_amount`), a
line-item CSV linked by `document_id` (each schema's `line_items` map), and
JSON Lines.

### HTTP jobs

A job is a batch of uploaded files stored under `DOCKET_JOBS_DIR/<job_id>/`:
`job.json` (options, per-document index, counts, status), `results.jsonl`
(the batch checkpoint, so a restarted server resumes unfinished jobs
without redoing finished documents) and `uploads/`, which is deleted when
the job finishes. Uploads stream to random file names — only the original
suffix is kept — and are checked against per-file, per-job and page limits
before a job exists; anything rejected is deleted. Callers choose schemas by
registered id only. `DOCKET_MAX_CONCURRENT_JOBS` jobs run at once.

---

### Configuration

`docket.config` declares every setting once (`SETTINGS`: attribute, TOML
key, environment variable, type, bounds, default, help) and loads them as
defaults < config file < environment into module attributes that the rest
of the package reads at call time; explicit arguments are applied on top by
`options.resolve()`, the CLI and the HTTP form handling. The file is TOML
(`--config`, else `DOCKET_CONFIG`, else `./docket.toml`), with paths
relative to the file. Loading never raises: an invalid value keeps its
default and is recorded; `config.check()` raises one `ConfigurationError`
naming every problem, with its source (`DOCKET_OCR_DPI='x'`,
`docket.toml: [ocr] dpi = 'x'`, unknown keys, unknown OCR language, malformed
Paddle device). The CLI checks before any command but `config`, `docket-api`
before starting the server and the app's lifespan before accepting a
request, and `resolve()` before `process_document()` reads a file.
`docket config show` lists values with sources, secrets masked.

## 3. Schema catalog

`docket.catalog` holds every schema docket can extract, as `SchemaSpec`s
keyed by `(schema_id, version)`:

- **Spec**: model, version, display name, description (read by the LLM
  classifier), status (`stable` / `experimental`), weighted keywords, TF-IDF
  example sentences, cited field paths, validators `(document,
  ValidationContext) -> issues`, and migrations.
- **Shared blocks** (`catalog/common.py`): `Party`, `Address`,
  `TaxIdentifier`, `Money`, `DocumentReference`, `BankAccount`, `LineItem`,
  `Citation`, `CitedDocument`.
- **Built-ins**: invoice 2.0 and purchase order 2.0 (parties as `Party`,
  PO numbers as references), tax invoice and credit note (same billing
  structure and rules as the invoice, plus their own), receipt / contract /
  bank statement / acceptance act / waybill / boarding pass 1.1 (flat, as
  before, minus `doc_type`), utility bill, delivery note, certificate of
  origin and ID document 1.0. Fixtures and expected extractions for each are
  in `tests/fixtures/catalog/`.
- **Versions and migrations**: several versions of a schema can be
  registered; the latest is the default and `--schema-version` /
  `ProcessOptions.schema_version` pick another. A result keeps its
  `schema_version`; reading `result.document` under a newer registration
  runs the migration chain (all built-in 1.0 → current steps are automatic).
- **Registration errors** are raised at registration: bad id or version,
  duplicate, a model that can't be described as JSON Schema, a
  `field_locations` that isn't `dict[str, Citation]`, cited paths the model
  doesn't have, a model already registered under another id.
- **Sources**: built-ins, `register_schema()`, the `docket.schemas` entry
  point, and unregistered models passed as `schema_model` (id =
  `module:Class`, no version).

---

## 4. Classification Tier

Classification determines which Pydantic schema will govern extraction:

Every tier reads the schema catalog, so a registered schema takes part in all three.

1. **Keyword Rules**: each schema's weighted `keywords` (English and Spanish cues, plus the document's own name in German, French, Italian, Dutch, Portuguese and Polish; the added schemas carry their names in the main EU languages). If the top schema leads the runner-up by 2 points, it classifies immediately; confidence is the winner's share of all matched weight. With 14 schemas more text matches several of them — a certificate of origin mentioning its invoice number scored 0.46 and went to review on classification confidence alone — so that share is lower than it was with 8.
2. **TF-IDF Classifier**: word and character n-grams trained on each schema's `examples` (paraphrases in EN, ES, DE, FR, IT, NL, PT; 12–26 per built-in schema). Above `DOCKET_TFIDF_CONFIDENCE_FLOOR` it skips the LLM call. It is skipped when any registered schema brought no examples, because it would file that type under a neighbour. On the 34-sentence held-out set in `tests/test_classify_tfidf_multilingual.py` it is right 33 times, confident 19 times and never confidently wrong; on the clean bank-statement fixture it was confidently wrong (invoice, 0.71) — the rules tier answers first there.
3. **LLM Fallback**: Invoked only when rule-based and TF-IDF classifiers cannot make a confident decision.

---

## 5. Extraction & Verbatim Citations

- **JSON Schema Contracts**: The target schema is the Pydantic model of the selected catalog schema; its JSON Schema is the extraction contract.
- **Nested citations**: `field_locations` keys are field paths (`seller.name`, `references[0].number`); validation and quote location follow them.
- **Line-item citations**: every row of every repeated list must be cited per field (`line_items[0].quantity`, `items[0].price`, `transactions[0].amount`), the quote being that row's own text. Grounding, location and review treats an item value exactly like a top-level amount.
- **Verbatim Evidence**: The model must provide verbatim quotes (`quote`, `page`) for extracted values.
- **Multilingual Parsing**: Supports both European (`1.234,56 €`) and American (`$1,234.56`) numerical conventions.

---

### Vendor-template extraction

`docket.templates` is the deterministic fast path for known vendors. A
`VendorTemplate` belongs to one schema, requires every issuer pattern to
match, and maps explicit page labels and table columns into schema paths.
Values are type-coerced with the same amount and date conventions as model
extraction, and every field and line-item cell receives a citation.

The pipeline runs a matching template after schema selection and before the
extraction model. A candidate is accepted only when Pydantic validation and
the normal deterministic business rules produce no errors. A missing rule,
unparseable value, incomplete row or invalid total discards the candidate and
runs model extraction; templates therefore reduce model calls but never
bypass validation. `ProcessingMetrics.template_id` records the accepted
template and the benchmark reports template hit rate, latency and the minimum
number of structured-extraction calls avoided.

The registry is available through Python, `docket templates`, and
`/vendor-templates`. Built-ins are fictional golden-corpus examples rather
than a claim to recognize arbitrary real vendors.

## 6. Deterministic Validation

Validation never calls a model. It executes deterministic arithmetic and mathematical checksum algorithms:

- **Citation grounding**: every numeric field — top-level and line-item (`_item_numeric_fields` keys every row value by schema path) — must be found on its cited line. A value the cited row actually implies also grounds: a derived unit price (4.98 / 2 = 2.49) or line total (2 × 58.50 = 117.00) counts only when both operands are printed on that row. A quantity of 1 is the implicit single item, not a fabricated amount. An item value contradicted by a garbled witness is a warning, not a blocker, when the rows close their own arithmetic (items sum to the stated subtotal under either coupon layout). A list cited element-wise (`parties_a[0]`) satisfies the requirement on the whole list.

- **Totals & Line Items**: Verifies `subtotal + tax + shipping - discount == total_amount` and the other sums to the cent: `validate._isclose` allows an absolute difference of 0.01 (one rounding step of a printed two-decimal amount), whatever the size of the amount. A relative tolerance was used before; it let a 10.00 gap through on a 1,100 total. The EN 16931 rules downstream compare exact decimals, so a looser check here would only move the failure to export time.
- **IBAN**: ISO 7064 MOD 97-10 check digits for all European nations and Brazil. Identifies non-IBAN systems (US, Canada) and requests routing numbers instead.
- **VAT / Sales Tax**: Algorithmic check-digit verification across all 27 EU member states, the UK, Switzerland, and Norway.
- **Americas Tax IDs**: Modulo-11 CNPJ/CPF checks for Brazil, Luhn mod-10 checks for Canadian Business Numbers (BN), and prefix verification for US EINs.
- **B2B Invoicing**: Validates customer tax IDs, ISO 9362 SWIFT/BIC codes, SKU and unit of measure on line items, and mathematical cross-checks tax rate percentage against subtotal and tax amounts.
- **Receipts & Expenses**: Validates retail/restaurant balancing
  `subtotal + tax + tip - discount == total_amount`, line item pricing
  `quantity * unit_price == price`, merchant tax IDs (VAT and national), and
  4-digit payment card format. Coupons print above the SUBTOTAL (stated
  subtotal already discounted) or below it, so the items-sum and balancing
  rules accept either layout and flag only when neither closes.
- **Bank Statements**: Validates balance equation `opening_balance + total_deposits - total_withdrawals == closing_balance`, sums of transaction deposits and withdrawals, running balance continuity across consecutive transaction entries, and bank IBAN check digits.
- **Acceptance Acts**: Validates services completion `subtotal + tax == total_amount`, line item pricing `quantity * unit_price == total`, distinct counterparties (customer != contractor), tax ID formats for customer and contractor, and warns if `claims_waived` is false.
- **Waybills / Consignment Notes (CMR, ТОРГ-12)**: Validates physical logistics balancing: sum of item quantities vs `total_quantity`, sum of gross weights vs `total_gross_weight_kg`, line item pricing `quantity * unit_price == price`, distinct consignor and consignee, and carrier tracking.
- **Citation Grounding**: Asserts that every cited quote exists in the document and contains the claimed numerical value.
- **Contract Legal Validation**: Asserts counterparty sanity (an entity cannot contract with itself; parent/subsidiary relationships trigger reviews), verifies that parties, governing law, payment terms, and signatories exist verbatim in the source text, checks term dates and notice/cure period limits, and runs automated risk factor assessment (unlimited liability, auto-renewal trap).

---

## 7. Human Review Queue

Documents that fail any error-level validation rule, fail extraction, or carry low classification confidence are routed to the Review Queue (`sqlite:///data/review.db` by default, PostgreSQL in multi-worker deployments):

- `review_tasks` stores current state; append-only `review_revisions` preserves every claim, correction, validation and decision.
- Claim uses expiring leases and opaque lock tokens. Updates also carry the expected version, preventing concurrent reviewers from overwriting each other.
- Corrections are merged over the original extraction and rerun through its Pydantic schema and deterministic business validators. Approval is refused while error-level issues remain.
- The FastAPI workflow exposes claim, release, revalidate, history, originals and rendered pages. The `/verify` workbench draws normalized source bboxes directly over those pages.

---

## 8. Cross-Document Reconciliation & 3-Way Matching

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

## 9. Accounting & e-Invoicing Export Tier

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
- **Facturae 3.2.2** (`docket.export.facturae`): Spanish electronic invoice (FACe) with Party Tax Identification and TaxesOutputs breakdown.
- **EN 16931 e-invoices** (`docket.export.en16931`), see below.

### EN 16931 exporters

One semantic model, two syntaxes. `en16931.semantic(doc)` maps an `Invoice`,
`TaxInvoice` or `CreditNote` onto EN 16931 business terms (BT/BG): parties
with VAT (BT-31/48), tax registration and legal ids, contacts, electronic
addresses, the payment account as credit transfer (BG-17), references
(order BT-13, preceding invoice BG-3), lines with unit codes
mapped to UN/ECE Rec 20, document-level allowance and charge for discount
and shipping, and one VAT breakdown per rate. It computes every total the
standard defines from the lines and refuses (`EN16931Error`, surfaced as
`ExportError`) whatever it cannot represent without inventing data: no
lines (BR-16), no determinable rate, tax or line sums that disagree with
the stated amounts (BR-CO-10/14), a discount or charge over several rates.
Only VAT categories `S` and `Z` are produced; exemptions (`E`, `AE`, `K`,
`G`, `O`) need an exemption reason the extraction schema doesn't carry.

`_Ubl` and `_Cii` render that model. The registered formats differ only in
syntax, specification identifier (BT-24) and business process (BT-23):

| Format | Syntax | BT-24 | Validated as |
|---|---|---|---|
| `ubl` | UBL 2.1 | `urn:cen.eu:en16931:2017` | `en16931` |
| `peppol` | UBL 2.1 | `...#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0` | `peppol` |
| `xrechnung-ubl` | UBL 2.1 | `...#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0` | `xrechnung` |
| `xrechnung-cii` | CII D16B | same | `factur-x-xrechnung` |
| `factur-x-en16931` | CII D16B | `urn:cen.eu:en16931:2017` | `factur-x-en16931` |
| `factur-x-basic` | CII D16B | `...#compliant#urn:factur-x.eu:1p0:basic` | `factur-x-basic` |

A credit note becomes a UBL `CreditNote` (type 381) or CII `TypeCode` 381.
The BASIC profile omits what its schema doesn't allow (seller item id,
contacts, BIC). The Factur-X exporters produce CII XML; `docket.einvoice`
can embed it with XMP into a source PDF, extract it again, and verify the
complete round trip. XML uses the official profile rules and the PDF/A-3
container uses the external veraPDF CLI. Every format above passes its official rules
on the complete test invoice (`tests/test_einvoice.py`).

## 9a. E-invoice validation

`docket.einvoice` (extra `[einvoice]`: lxml, saxonche) validates an
e-invoice with the official artifacts, vendored in
`src/docket/einvoice/resources/`:

| Artifact | Version | License | Used for |
|---|---|---|---|
| KoSIT validator configuration | XRechnung 3.0.2, 2026-08-31 | Apache-2.0 (+ OASIS / UN/CEFACT schema terms) | UBL 2.1 and CII D16B XML Schemas |
| CEN EN 16931 validation artefacts | 1.3.16 | EUPL-1.2 | EN 16931 Schematron, UBL and CII |
| KoSIT XRechnung Schematron | 2.6.0 | Apache-2.0 | CIUS XRechnung (BR-DE-*) |
| OpenPeppol BIS Billing 3.0 | 3.0.20 | no license file (see below) | Peppol rules, compiled from .sch with SchXslt 1.10.1 (MIT) |
| Factur-X / ZUGFeRD | 1.09 (from the factur-x 6.8 package, BSD-2) | FNFE-MPE / FeRD, free download behind a form | profile XSD and Schematron, MINIMUM to EXTENDED |

`resources/manifest.json` records every file's SHA-256, the archive URL and
hash it came from, version and license; `artifacts.verify()` checks the
installed copy. `scripts/update_einvoice_resources.py` downloads the pinned
archives (refusing a hash mismatch), extracts only what validation needs,
compiles the Peppol Schematron and rewrites the manifest; test fixtures
(official examples, the XRechnung test suite, Peppol's unit tests) go to
`tests/fixtures/einvoice/official/`. To update: change the pinned URL and
hash, rerun, run the tests, commit the diff. The Peppol and Factur-X
artifacts ship without an explicit redistribution license; check that before
distributing a build that contains them.

`validate_einvoice(source, options)`:

1. Reads XML, or the embedded `factur-x.xml` / `zugferd-invoice.xml` /
   `xrechnung.xml` of a PDF (pypdfium2). XML with a DOCTYPE is refused;
   entities are never resolved and nothing is fetched from the network.
2. Detects the syntax from the root element (UBL Invoice, UBL CreditNote,
   CII) and the declared profile from BT-24. A requested profile that
   differs from the declared one is reported as `DOCKET-PROFILE-MISMATCH`
   and validation continues with the requested rules.
3. Runs the XML Schema (lxml), then each Schematron (SaxonC-HE, XSLT 2.0,
   SVRL output) of the profile's plan: EN 16931 core, plus Peppol or
   XRechnung rules; Factur-X profiles use their own combined Schematron.
   Schematron is skipped when the XML Schema fails, because its rules
   assume a schema-valid tree.
4. Returns `EInvoiceValidationResult`: `valid`, `detected_format`,
   `syntax`, `profile`, `declared_profile_id`, `validator` /
   `validator_version`, `validation_resource_version`, one `LayerReport` per
   layer (artifact, version, ran, passed, rules fired) and `issues` (code,
   severity from the Schematron flag, message, XPath or XSD line, rule
   source with version, layer).

Compiled stylesheets and schemas are cached per process behind a lock
(SaxonC is not thread-safe). The same function serves
`docket validate-einvoice`, `POST /validate/einvoice` and
`ExportOptions(validate_einvoice=True)`. Without the extra it raises
`EInvoiceUnavailable`, a `ConfigurationError` (CLI exit 3, HTTP 503
`einvoice_unavailable`).

The tests run all official examples of each artifact, all 227 cases of
Peppol's own UBL unit suite (expected rule ids fire, expected successes
don't), and negative cases: missing mandatory field (BR-07, BR-DE-15),
wrong totals on XSD-valid XML (BR-CO-15/16), wrong or unknown tax category
(BR-Z-05, BR-CL-18), bad endpoint scheme (PEPPOL-EN16931-CL008), unknown
and mismatched profile identifiers.

---

## 10. Document Forensics (stamps, signatures, alterations)

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
