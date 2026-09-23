# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): the public API is everything
exported from `docket`, the `docket` / `docket-api` commands, the HTTP API in
`docs/openapi.json`, and the `DOCKET_*` environment variables.

## [Unreleased]

## [0.4.0] - 2026-09-23

### Changed

- Removed leftover compatibility shims: `docket.extract.extract()` (use
  `extract_pages()`), the unused `forensics._detect_colored_clusters()`, and
  the `DOCKET_REVIEW_QUEUE` / `review.queue` setting with
  `ReviewOptions.queue_path`. The review store is configured only by
  `DOCKET_REVIEW_DATABASE_URL` / `ReviewOptions(database_url=...)`.
- Repository root trimmed to the library: removed the `api.py` shim (use
  `docket-api` or `uvicorn docket.api:app`) and `requirements.txt` (use
  `pip install -e ".[dev]"`); the macOS launcher moved to
  `examples/start_demo.command`.

- **Breaking for Peppol, CII and Factur-X validation:** the OpenPeppol BIS
  Billing rules, the Factur-X schemas and Schematron and the UN/CEFACT CII
  D16B schemas are no longer in the repository, wheel or sdist, because
  their redistribution terms could not be verified. Run
  `docket einvoice fetch` (or `fetch_einvoice_resources()`) once to download
  them from their pinned upstream releases; every file is checked against
  the manifest. Until then those profiles raise `EInvoiceResourcesMissing`,
  an `EInvoiceUnavailable`. UBL EN 16931 and XRechnung validation work as
  installed. New setting `DOCKET_EINVOICE_DOWNLOADS`.

- Review-queue persistence is now opt-in for the Python API. Set
  `ReviewOptions(enqueue=True)` or `DOCKET_REVIEW_QUEUE_ENABLED=true` when the
  transactional review workflow is wanted. Logs no longer include the OCR
  quality model's evidence text.

Line-item provenance: every row of every repeated list now carries source
citations that are grounded, located on the page and validated like any
top-level amount. Measured on the 17 golden scans (tesseract,
`eval/benchmark_ocr.py --dataset eval/golden_dataset`):

| citation metric | before | after |
|---|---|---|
| line-item rows cited | 0.08 | **1.00** |
| line-item rows located on the page | 0.06 | 0.98 |
| top-level fields cited | 0.92 | 0.97 |
| top-level fields located | 0.87 | 0.92 |
| documents in review | 1 | 1 |

### Fixed

- Documents flagged during processing went to the SQLite file named by
  `DOCKET_REVIEW_QUEUE` even when `DOCKET_REVIEW_DATABASE_URL` pointed
  elsewhere (e.g. PostgreSQL), so the HTTP review API did not see them. The
  pipeline now uses the configured database URL.

### Added

- A documented SemVer, deprecation, and serialized-result compatibility policy,
  enforced by a snapshot of the public Python API.
- Audited notices and bundled license texts for vendored e-invoice validation
  artefacts, with unresolved Peppol, Factur-X and UN/CEFACT redistribution
  terms called out explicitly.
- CycloneDX release SBOMs covering runtime dependencies, optional extras and
  bundled e-invoice artefacts, plus a CI gate against copyleft runtime packages.
- Machine-readable PII categories on built-in schema fields and a public
  `pii_fields()` helper that also traverses custom Pydantic schemas.
- Security regression coverage for hostile prompt content, malformed and
  oversized documents, and entity-expanding XML; library inputs now enforce
  byte and decoded-pixel limits and return controlled acquisition errors.
- README positioning now treats the Python package as the product, the CLI as
  its supported interface, and service, web, desktop and Docker surfaces as
  reference applications.

- Optional Docling/TableFormer layout backend with `fast` / `accurate` modes,
  cell matching control, wrapped cell text and merged row/column spans.
- Fine-angle raster deskew with correction metadata, plus conservative
  borderless two-column table detection and wrapped-row reconstruction.

- Production review workflow backed by SQLite or PostgreSQL: transactional
  task leases, optimistic versions, append-only correction history and
  mandatory schema/business revalidation before approval.
- Live `/verify` workbench using the review API, with rendered document pages,
  field editing, revision history and source bbox highlights.

- Factur-X PDF/A-3 generation through the official `factur-x` engine, including
  embedded `factur-x.xml`, AF relationship and Factur-X XMP metadata.
- Structured veraPDF PDF/A-3 validation and strict generate -> extract ->
  validate round trips in Python and `docket factur-x create|extract|validate`.

- **Stage 4 vendor templates**: deterministic extraction for known vendor
  layouts through `VendorTemplate`, `FieldRule` and `ItemsRule`. Successful
  templates produce normal nested Pydantic documents and per-field/line-item
  citations, skip structured LLM extraction, and are accepted only after the
  existing business validation passes; an incomplete or invalid reading falls
  back to the model.
- Public template registry in Python, `docket templates list|show`, and
  `GET /vendor-templates[/{id}]`. Two fictional golden-corpus invoice vendors
  exercise German and noisy Spanish OCR layouts end to end.
- Template metrics in the OCR benchmark: selected `template_id`, hit rate,
  per-template counts, template-document latency and the minimum number of
  structured-extraction LLM calls avoided.

### Measured (stage 4 deterministic pass — 198 scans, Tesseract, no LLM)

- Both built-in examples matched, extracted and passed normal validation:
  Nordlicht and Distribuciones Albufera, 2/198 documents (1.01%).
- No unrelated document matched either template. Seventeen scans produced no
  Tesseract text and were counted as OCR errors rather than template misses.
- Mean acquisition plus template time for the two accepted documents was
  1.75 s. The full pipeline benchmark now records template usage, but was not
  re-labelled with these OCR-only numbers.

- Line-item and nested citations: the extraction prompt requires a citation
  for every row of every repeated list, per field (`line_items[0].quantity`,
  `items[0].price`, ...), the quote being that row's own text.
- `SourceLocation.status` (`verified` / `fuzzy` / `conflicting` / `unlocated`)
  and `SourceLocation.regions`: every place a quote was found is kept
  (several exact matches mark the value `conflicting`), with `match` saying
  how it was found.
- `DocumentResult.highlights(page=None)`: `(field, source, region)` triples
  for drawing provenance boxes over the original pages.
- Citation coverage metrics in `eval/metrics.py` and `eval/benchmark_ocr.py`
  (spec-aware paths, so `line_items[0].total` counts against the item rows).
- Grounding of line-item numeric fields: a fabricated row that balances the
  totals now fails validation like any invented top-level amount.
- **Stage 2**: the false-success metric in `eval/benchmark_ocr.py` — documents
  that come back "succeeded, no review needed" with wrong graded fields,
  the wrong number nobody was told to check (rate taken over the silent
  successes only; review and failed docs made no clean claim).
- **Stage 2**: `eval/benchmark_competitors.py` — docket against docpick,
  invoice2data and ocrcontext on the same corpus, graded with the same
  field metric on each tool's own schema intersection; docket's number is
  recomputed on exactly those docs and fields, tool errors count as every
  graded field wrong, LLM-backed tools run against the same Ollama model
  and are handed the correct schema while docket must classify its way
  there.

### Fixed (stage 3 — the two measured false-success drivers)

- US-format dates read day-first on dollar documents: an ambiguous date
  (`06/02/2015`) on a document that prints a bare dollar sign is now read
  month-first even when the amounts use decimal commas (`$ 889,20` — the
  donut corpus). The dollar sign outranks the decimal comma; `AU$`/`US$`/
  `HK$`-style prefixed signs abstain (those countries write day-first).
- A `TAX INVOICE` header no longer scores for the plain `invoice` schema
  (`\binvoice\b` matched inside "tax invoice", adding 3.0 points — with
  "Bill To" it won the rules tier outright on 19/120 Malaysian till
  receipts). The multilingual invoice names are likewise guarded against
  "factura fiscal" / "fattura fiscale" / "faktura VAT".
- Till-receipt classification signals: a cashier + approval-code
  combination (3.0), "please come again" (2.0), "terima kasih" (2.0) and
  receipt corpus examples for tax-invoice till slips, so the rules and
  TF-IDF tiers answer `receipt` on point-of-sale slips printed with a
  legal TAX INVOICE header; the receipt and tax_invoice descriptions now
  tell the LLM tier the same thing.

### Measured (stage 3 re-run — same 198-scan corpus, tesseract)

| | before stage 3 | after |
|---|---|---|
| field accuracy | 0.53 | **0.72** |
| document success rate | 0.32 | 0.54 |
| false successes | 23/50 silent (46%) | 19/55 silent (35%) |
| SROIE docs classified receipt | 17/120 | **72/120** |
| donut fields | 0.92 | 0.975 |
| golden fields | 0.97 | 0.97 (no regression) |

On the competitors' own docs+fields, docket now leads every tool:
docket 0.71 vs docpick 0.61, 0.69 vs ocrcontext 0.04, 0.90 vs
invoice2data 0.00. docpick still wins SROIE (0.75 vs 0.55) — the remaining
39/120 misclassified till slips are the next classification margin;
when docket does classify them receipt, its fields are 0.84.

### Measured (stage 2 — extended corpus and the competition)

The corpus grew from 17 golden scans to 198 scans (real documents from
Hugging Face: DocILE, donut-style invoices, SROIE receipts, CORD, FUNSD,
RVL-CDIP — `eval/download_real_samples.py --n` per source). Tesseract
config throughout:

| | golden only | extended corpus |
|---|---|---|
| field accuracy | 0.97 | 0.53 |
| documents in review | 1 (6%) | 148 (75%) |
| false successes | 3/16 silent (19%) | 23/50 silent (46%) |

What the extended corpus says:

- Classification is the bottleneck, not extraction: SROIE "receipts" are
  Malaysian tax-invoice till slips, 82/120 classify as `tax_invoice` and
  every graded field of those documents counts wrong. Where classification
  is right, field accuracy is 0.80–0.97 per source.
- The top false-success drivers are measured and actionable: a wrong or
  missing date in 17 of the 23 docs — five are US-format day/month swaps
  (`06/02/2015` → 2015-02-06 instead of 2015-06-02), the rest dates not
  read at all from degraded thermal receipts and DocILE scans — plus
  merchant names and totals on those same hard scans.

Against the pip-installable competition, same documents and same graded
fields (see the runner docstring for the fairness rules):

| tool | docs | field accuracy | docket, same docs+fields |
|---|---|---|---|
| docpick 0.1.3 | 55 (subsample) | 0.61 | 0.45 |
| ocrcontext 0.1.5 | 55 (subsample) | 0.04 (16 parse errors) | 0.40 |
| invoice2data 1.0.1 | 44 | 0.00 (0 built-in template matches) | 0.86 |

docket wins golden (1.00 vs docpick's 0.62), donut (0.93 vs 0.48) and
docile (0.50 vs 0.08); docpick wins SROIE (0.75 vs 0.10) because it is
handed the receipt schema. invoice2data matched none of its built-in
vendor templates — authoring templates per vendor is its design.

### Fixed (stage 1 — false citation-check flags that queued correct extractions for review)
- a derived value (unit price 4.98 / 2 = 2.49, line total 2 × 58.50 = 117.00)
  is grounded by its own row when both operands are printed on it;
- `quantity == 1` is the implicit single item, not a fabricated amount;
- an item value contradicted by a garbled witness is a warning, not a
  blocker, when the rows close their own arithmetic (items sum to the
  stated subtotal under either coupon layout);
- a list cited element-wise (`parties_a[0]`) satisfies the citation
  requirement on the whole list (`parties_a`).
- Review queue after the fixes: 1 document (purchase order, by design),
  down from 5 during development. Note: the intermediate run's higher
  "docs ok" (0.94) was an artifact — the false flags escalated two garbled
  receipt scans to a vision-model re-read that fixed fields by accident.
  With honest flags those scans keep their Tesseract misreads
  (`total 775.0`, `card_last_four` missing) without review; measuring that
  false-success rate is the next stage.

## [0.3.0] - 2026-09-21

Layout-first redesign: a layout model shared by every OCR backend,
pluggable engines with per-page fallback, a versioned schema catalog of 14
built-in types, batch processing, official EN 16931 e-invoice validation,
TOML configuration, and a measured golden-set benchmark.

### Breaking changes
- `process()` is replaced by `process_document()`, which returns a
  `DocumentResult` instead of `PipelineResult`. `PipelineResult` is removed.
  `DocumentResult` carries `status` (`succeeded` / `needs_review` / `failed`),
  `document_type`, `schema_id`, `ocr` (per-page acquisition report),
  `layout`, `metrics` (timings, LLM calls, `escalated_to_vlm`,
  `extract_attempts`) and a structured `error`. The old `ocr_method`,
  `page_methods`, `raw_text_chars`, `pages_total`, `pages_processed`,
  `llm_calls`, `llm_estimated_tokens`, `extract_attempts` and
  `escalated_to_vlm` fields are gone; their data lives in `ocr` and `metrics`.
- A document that cannot be read now comes back as a `DocumentResult` with
  `status="failed"` and an `error` instead of raising. Configuration errors
  (unknown or unavailable OCR backend, unknown language code) raise before
  any page is read.
- `SourceLocation` now describes a resolved location (`page`, `quote`,
  `bbox`, `word_ids`, `confidence`) and lives in `docket.result`. The
  `{page, quote}` model the extraction LLM fills is `Citation`
  (`CitedDocument.field_locations: dict[str, Citation]`).
- The `docket.ocr` module is now a package; `extract_text()` and `OcrResult`
  are removed in favour of `docket.ocr.acquire()` and OCR backends.
- `DOCKET_OCR_LANG` (`eng+deu`) is replaced by `DOCKET_OCR_LANGUAGES`
  (ISO 639-1, `en,de`); `DOCKET_OCR_PSM` is renamed `DOCKET_TESSERACT_PSM`.
- `llm_client.vision_transcribe()` takes encoded image bytes, not a path.
- On-stage callback: the `"ocr"` stage is now `"acquire"` and receives an
  `Acquisition`; `"classify"` receives `None` when the schema was fixed.
- `process_document(source, options)` takes a `ProcessOptions` object; the
  `enqueue_review=` keyword is gone (`ReviewOptions(enqueue=...)`).
- `export_document(source, format, options)` takes a `DocumentResult` (or a
  schema instance) and returns an `ExportResult` (`format`, `media_type`,
  `content`) instead of a string. It refuses a result that failed, has
  validation errors or needs review unless `ExportOptions(require_valid=False)`.
- Document types are schemas in a versioned catalog (`docket.catalog`).
  `register_document_type`, `get_document_type`, `list_document_types`,
  `unregister_document_type`, `DocumentType`, `DocumentTypeError` and the
  `docket.doctypes` module are replaced by `register_schema(SchemaSpec(...))`,
  `get_schema`, `list_schemas`, `unregister_schema`, `SchemaSpec` and
  `SchemaError`; the `docket.document_types` entry point is now
  `docket.schemas` (target: a SchemaSpec, a list, or a callable).
- Validators take `(document, ValidationContext)` instead of
  `(document, raw_text)`.
- `DocType` is removed; `ClassificationResult.doc_type` is a plain schema id
  string and `type_name` is gone.
- Invoice 2.0 and PurchaseOrder 2.0: `vendor_*` / `customer_*` fields become
  `seller` / `buyer` (`supplier` / `buyer` on POs) `Party` objects with
  `address` and `tax_ids`; `vendor_iban` / `vendor_bic` become
  `payment_account`; `purchase_order_number` becomes a `references` entry
  (still readable as the `purchase_order_number` property). Citation keys
  and validation issue fields use paths: `seller.name`,
  `seller.tax_ids[0]`, `payment_account.iban`.
- The other original schemas drop the `doc_type` field (version 1.1).
  Document models moved from `docket.schemas` to `docket.catalog`;
  `docket.schemas` keeps the non-document models.
- Matching discrepancies name `seller.name` / `buyer.name` instead of
  `vendor_name` / `customer_name`.
- CLI is subcommand-based: `docket process FILE`, `docket schemas
  list|show|json-schema`, `docket formats`, `docket ocr-backends`,
  `docket forensics FILE`. `--list-formats`, `--list-types`,
  `--list-ocr-backends` and `--forensics` are gone.
- HTTP: `GET /document-types` is replaced by `GET /schemas`,
  `GET /schemas/{id}` and `GET /schemas/{id}/json-schema`;
  `/export-formats` entries gain `media_type` and `schemas`.
- Eval golden files grade nested fields by path (`seller.name`).
- HTTP jobs are batches: `POST /jobs` takes `files` (one or more) plus form
  options and returns a `Job` (`job_id`, `status`, `documents`, `counts`);
  results are served by `/jobs/{id}/results/{index}`, `results.jsonl`,
  `results.csv` and `line-items.csv` instead of inside the job. `POST
  /process` accepts the same form options. Errors are `{"error": {"code",
  "message"}}`; unsupported files are 415, invalid options 422, unfinished
  results 409.
- Jobs live in a directory per job under `DOCKET_JOBS_DIR`
  (`DOCKET_JOB_STORE` and `DOCKET_JOB_UPLOADS` are gone); uploads are
  deleted when a job finishes.
- CLI exit codes: 0 all succeeded, 1 partial (some failed or need review),
  2 all failed, 3 configuration error. `process` without `--include-layout`
  leaves page layouts out of its JSON (`--no-layout` is gone).
- Configuration errors share the base `docket.ConfigurationError`
  (`UnknownLanguage`, `OcrBackendError`, `BackendUnavailable`,
  `DocumentTypeError`).
- `review_queue.reasons_for`, `enqueue`, `list_pending`, `get`, `update` and
  `clear` take the threshold / queue location as keyword arguments.
- CLI: a configuration error exits with code 3.
- E-invoice export formats are renamed and rewritten on one EN 16931 model:
  `ubl` (EN 16931 core), `peppol`, `xrechnung-ubl`, `xrechnung-cii`,
  `factur-x-en16931`, `factur-x-basic`. `zugferd` and `xrechnung` are gone
  (use `factur-x-en16931` / `xrechnung-cii`), as are `export_to_ubl_xml` and
  `export_to_zugferd_xml`; use `export_document(doc, format)`.
  `docket.export.einvoice` is now `docket.export.facturae` (Facturae only).
- The EN 16931 exporters raise `ExportError` for an invoice they can't
  represent faithfully (no line items, no determinable VAT rate, tax or line
  sums that disagree with the stated amounts, a discount over several VAT
  rates) instead of writing XML that fails the standard. They also accept
  `CreditNote` (UBL `CreditNote` / CII type 381).
- Amount checks use an absolute tolerance of 0.01 instead of 1 % of the
  amount: a 10.00 gap on a 1,100 total is now a validation error.
- Settings are validated as a whole: an invalid `DOCKET_*` value no longer
  raises `ValueError` at import; it is reported, with every other problem,
  by `config.check()`, which the CLI, `docket-api` and `process_document()`
  run before reading anything. `DOCKET_TESSERACT_PSM` is an integer 0-13;
  `config.OCR_LANGUAGES` is a list.
- Page layouts are kept in CLI and HTTP results by default, following
  `DOCKET_INCLUDE_LAYOUT` (default true) like the Python API; `--no-include-layout`
  or `include_layout=false` leaves them out. `ProcessOptions.include_layout`
  defaults to `None` (use the setting).
- `Invoice` (2.0) gains `buyer_reference` (BT-10, the XRechnung Leitweg-ID)
  and `Party` gains `contact_name`; both are optional.

### Added
- Golden dataset for every one of the 14 built-in schemas: 16 rendered
  scans (JPEG, seeded noise, low-res/rotated/multipage variants) with
  ground-truth text, tables, line items and fields (`eval/golden_dataset/`,
  `eval/build_golden.py`), plus `eval/metrics.py` grading: word
  precision/recall/F1, table-cell accuracy, greedy one-to-one line-item
  matching, identifier normalization for grouped values.
- Reproducible OCR and pipeline benchmark (`eval/benchmark_ocr.py`):
  Tesseract vs. PaddleOCR mobile/medium, OCR-only runs (word F1, table
  cells, latency) and full-pipeline runs (document success, field accuracy,
  line-item precision/recall, table cells, mean/median seconds, VLM fallback
  share, LLM calls, review counts) over the golden and real-sample scans,
  with environment and versions recorded; results in
  `eval/results/ocr_benchmark.json`. `--no-layout-markers` runs the pipeline
  with plain text serialization for the layout-marker comparison: no
  measurable difference on this set (both Paddle configs identical on
  every document, Tesseract ±2 marginal scans, all within run-to-run
  noise), so the markers stay on by default for hard tables, not for a
  claimed accuracy gain.
- Extraction-stability benchmark (`eval/benchmark_variance.py`): the same
  document N times at temperature 0, per-field agreement. The receipt_taxed
  coupon case is identical across 10 runs (15/15 fields).
- `eval/benchmark_methods.py` now records classification confidence
  (mean, split by correctness, confidently-wrong count) beside accuracy and
  latency.
- Layout model (`docket.layout`): `BoundingBox`, `WordToken`, `TextLine`,
  `TextBlock`, `Column`, `TableCell`, `Table`, `PageLayout`,
  `DocumentLayout`, with normalized 0..1 coordinates and each page's
  original size for conversion back to pixels or points.
- Layout analysis shared by every backend: rows, blocks, text columns with
  column-major reading order, tables from PDF rulings or word alignment, and
  a text serialization with `[TABLE n]` / `[COLUMN n]` markers for the LLM.
- Extracted fields' citations resolve to page regions (`bbox`, `word_ids`,
  match confidence) by matching the quote against the page's words; the
  model is never asked for coordinates.
- OCR backend interface (`OcrBackend`, `Capabilities`, `availability()`),
  built-in `pdf_text`, `tesseract` and `vlm` backends, a registry
  (`register_ocr_backend`, `get_ocr_backend`, `list_ocr_backends`), plugins
  through the `docket.ocr_backends` entry point, and backend objects passed
  straight to `process_document(ocr_backend=...)`.
- Per-page fallback chain: `DOCKET_OCR_BACKEND`, `DOCKET_OCR_FALLBACKS`,
  `DOCKET_OCR_MIN_CONFIDENCE`; `auto` picks the first installed engine.
  Mixed PDFs use the text layer where it is usable and OCR elsewhere.
- Rotation: Tesseract OSD for scans (`DOCKET_OCR_DETECT_ROTATION`), glyph
  matrices for rotated PDF pages. Multi-frame TIFFs are read as multipage.
- A PDF text layer made of unmapped `(cid:N)` glyphs is treated as unusable.
- PaddleOCR backend (`paddle`), optional via `pip install "docket-idp[paddle]"`:
  word boxes, line confidence, orientation correction, and PaddleOCR's table
  structure pipeline (`DOCKET_PADDLE_TABLES`). `DOCKET_PADDLE_DEVICE`,
  `DOCKET_PADDLE_MODEL` (`mobile` / `medium`). The base install and
  `import docket` never need PaddleOCR; selecting it without the extra is a
  configuration error naming the install command.
- `ProcessOptions`, `OcrOptions`, `ReviewOptions`: one options object with
  explicit-argument > environment > default precedence, validated before
  processing starts.
- Fixed schema selection: `document_type=` or `schema_model=` (any Pydantic
  model, registered or not) skips classification; `classify=False` makes
  forgetting both an error. `docket --document-type`, `--schema MODULE:CLASS`,
  `--no-layout`; `doctypes.load_schema("pkg.mod:Class")`.
- `include_layout=False` drops page layouts and OCR witnesses from results;
  field locations are kept.
- Pipeline stages usable on their own: `select_schema`, `extract`,
  `validate_extraction`, `review`.
- Versioned schema catalog with metadata (display name, description,
  status, keywords, cited fields, validators, exporters, migrations),
  automatic migration of stored results from older schema versions,
  JSON Schema per schema, and `docket schemas` / `GET /schemas` to browse it.
- Shared schema blocks: `Party`, `Address`, `TaxIdentifier`, `Money`,
  `DocumentReference`, `BankAccount`.
- New experimental schemas: credit note, tax invoice, utility bill, delivery
  note, certificate of origin, ID document (printed text only; ICAO 9303 MRZ
  check digits for TD1/TD3). Each has multilingual keywords, TF-IDF example
  sentences, business rules, cited fields, and a fixture with expected
  extraction in `tests/fixtures/catalog/`.
- Classification is catalog-driven: keyword rules, TF-IDF training
  sentences and LLM descriptions come from each schema, so a registered
  schema with `examples` is learned by the TF-IDF tier too.
- `--schema-version` / `ProcessOptions.schema_version`;
  `DocumentResult.schema_version` is filled.
- `examples/schema_plugin/`: a schema shipped as a pip package.
- `process_batch(sources, options, BatchOptions)` → `BatchResult`: a
  directory (recursive, glob), a glob pattern or any iterable; input-order
  results, partial failure, `fail_fast`, bounded look-ahead, streaming via
  `on_result`, checkpoint resume, aggregate metrics.
- `docket batch INPUT` with `--recursive`, `--glob`, `--workers`,
  `--fail-fast`, `--checkpoint`, `--output`, `--format json|jsonl|csv`,
  `--line-items`, `--include-layout`; `process` gains `--output` and
  `--format`.
- Summary and line-item CSV with fixed columns for every schema
  (`docket.export.tabular`, `SchemaSpec.summary` / `line_items`).
- Process-wide limits on simultaneous LLM requests and OCR engines
  (`DOCKET_LLM_CONCURRENCY`, `DOCKET_OCR_CONCURRENCY`); `DOCKET_BATCH_WORKERS`,
  `DOCKET_MAX_BATCH_FILES`, `DOCKET_MAX_BATCH_BYTES`.
- `checksums.vat_format_ok`: per-country VAT formats (VIES, plus GB/XI, CH, NO).
- Official e-invoice validation (`pip install "docket-idp[einvoice]"`):
  `validate_einvoice()` / `docket validate-einvoice FILE [--profile]
  [--format json]` (exit 0 valid, 2 invalid, 3 extra missing) /
  `POST /validate/einvoice`. Profiles EN 16931, Peppol BIS Billing 3.0,
  XRechnung 3.0 (UBL and CII) and Factur-X / ZUGFeRD MINIMUM, BASIC WL,
  BASIC, EN16931, EXTENDED, XRECHNUNG. XML Schema (lxml) plus the official
  Schematron (SaxonC-HE): CEN 1.3.16, KoSIT XRechnung 2.6.0, Peppol 3.0.20,
  Factur-X 1.09, vendored with versions, licenses and SHA-256 checksums in
  `einvoice/resources/manifest.json`; offline, no Java. Factur-X / ZUGFeRD
  PDFs are validated from their embedded XML. `EInvoiceValidationResult`
  reports detected format, declared and applied profile, validator and rule
  versions, per-layer results and each issue's rule id, severity, location,
  rule source and layer; a declared profile that differs from the requested
  one is `DOCKET-PROFILE-MISMATCH`.
- `ExportOptions(validate_einvoice=True)` and `docket process --export FORMAT
  --validate-export` validate an e-invoice right after export
  (`ExportResult.einvoice_validation`).
- `scripts/update_einvoice_resources.py` rebuilds the vendored artifacts from
  their pinned official downloads; `--check` verifies them.
  `DOCKET_EINVOICE_RESOURCES` points to a separately maintained copy.
- Examples `validate_xrechnung.py` and `validate_peppol.py`.
- TOML config file (`docket --config`, `docket-api --config`, `DOCKET_CONFIG`,
  `./docket.toml`) below the environment in priority; `docket.example.toml`
  lists every setting. `docket config show|check`.
- `DOCKET_INCLUDE_LAYOUT`, `DOCKET_LAYOUT_MARKERS` (table/column markers in
  the LLM's text).
- `docket --ocr-backend`, `--ocr-fallback`, `--no-ocr-fallback`,
  `--ocr-languages`, `--list-ocr-backends`; `GET /ocr-backends`.

### Changed
- With 14 built-in schemas instead of 8 the TF-IDF tier is confident less
  often: 19 of 34 held-out sentences (none confidently wrong), and the rules
  tier's confidence (a share of all matched weight) is lower when a text
  matches several schemas. Measured on the eval sets
  (`eval/results/benchmark_methods.json`, 10 labeled text documents): rules,
  TF-IDF and LLM tiers all classify 10/10 correctly (TF-IDF was 8/10 before the
  expansion), at mean confidences 0.79 / 0.55 / 0.98 with zero confidently
  wrong answers; on the 33 scanned documents classification is 30-31/33 per
  OCR backend (the misses are SROIE retail receipts, not new-type confusion).

### Fixed
- Receipt validation demanded mutually exclusive coupon layouts: a coupon
  printed above the SUBTOTAL (the stated subtotal already has the discount
  applied, `receipt_taxed`) failed both the items-sum rule and the
  total-balancing rule no matter what the extraction said. Both rules now
  accept either printed layout and flag only when neither closes; a wrong
  total still fails under both.
- `docket forensics` crashed with `NameError` whenever it found an empty
  template or an alteration; it now exits 2.
- `.env.example` said thinking made extraction 18x slower; the recorded
  measurement is 11.9 s vs 5.1 s on one invoice.
- The UBL and ZUGFeRD/XRechnung exporters produced XML that failed the
  official EN 16931 rules (the README called it Peppol BIS compatible). The
  new exporters pass the official XSD and Schematron of every profile they
  declare, checked in the test suite.
- A tax number the extraction filed as VAT (e.g. "Tax ID: GB-771-4402",
  filed as VAT for its GB prefix) failed the VAT checksum it never claimed;
  only a value with its country's VAT format is now held to that checksum.
- An empty text page was reported as "text accepted"; it is now a rejected,
  degraded page.
- The HTTP `/process` and `/jobs` runner called the endpoint function instead
  of the pipeline (a name collision introduced with `process_document`).
- Scanned pages sent to the vision model were written as temporary PNGs next
  to the input file; pages are now encoded in memory.
- A blank page (e.g. an empty back side) no longer fails the whole document
  when the vision model is unavailable; it is reported as degraded.

## [0.2.0] - 2026-09-21

### Added
- Custom document types: `register_document_type()` adds a type with its own
  Pydantic schema, LLM description, keyword rules and validators; it is
  classified, extracted, citation-checked, validated and exported like the
  built-in ones. `CitedDocument` base class for schemas with source citations.
- `add_validator()` attaches extra rules to any type, built-in included.
- `docket.document_types` entry point for shipping types as packages.
- `docket --list-types`; `GET /document-types` and `GET /export-formats` API endpoints.
- `ClassificationResult.type_name`, and `SourceLocation` / `ValidationIssue`
  exported from the package root.
- Keyword rules recognise each document's own name in German, French,
  Italian, Dutch, Portuguese and Polish.
- `VOID_STAMP_PRESENT` forensic flag; status stamps (paid / approved / void)
  in the main EU languages.

### Changed
- TF-IDF tier: corpus rebuilt per type in seven languages (EN, ES, DE, FR,
  IT, NL, PT) with boarding passes added, and word + character n-gram
  features. Before, its confidence never exceeded ~0.45, so it never cleared
  the 0.65 floor and every ambiguous document went to the LLM; it now answers
  about two thirds of held-out documents, with no confident mistakes there.
- Forensics: handwriting in black ink is detected (outside printed words and
  ruled lines, in the signing zone); confidence is computed from geometry
  instead of fixed constants; a red stamp counts as a payment stamp only when
  a payment word is read inside it; black stamps are never claimed.
- The Streamlit demo moved to `examples/streamlit_demo.py` and now renders
  PDF pages; the `ui` and `tui` extras, `tui.py` and `gui.py` were removed.
- `ClassificationResult.doc_type` is `DocType | str`: built-in types stay
  `DocType` members, custom types are plain strings. Code comparing against
  strings (`doc_type == "invoice"`) works unchanged; use `type_name` instead
  of `doc_type.value` to handle both.
- With any custom type registered, classification skips the TF-IDF tier
  (trained on built-in types only) and falls through to the LLM.

## [0.1.0] - 2026-09-21

First packaged release.

### Added
- Published to PyPI as `docket-idp` (import name and command stay `docket`); `docket` and `docket-api` console commands.
- Optional extras: `api`, `ui`, `tui`, `tracing`, `all`.
- Docker image on `ghcr.io/kazkozdev/docket` (amd64 + arm64) with EU Tesseract language packs.
- OpenAI-compatible LLM backend (`DOCKET_LLM_PROVIDER=openai`) for Mistral, OpenAI, Azure OpenAI, vLLM, LM Studio.
- Export format registry: `export_document()`, `register_exporter()`, `list_exporters()`, plus third-party formats via the `docket.exporters` entry point; `docket --list-formats`.
- `PipelineResult.document` returns the extracted data as its typed schema.
- `process(..., enqueue_review=False)` and `DOCKET_REVIEW_QUEUE_ENABLED` for apps with their own review flow.
- `DOCKET_OCR_LANG` to choose Tesseract languages.
- `docs/openapi.json` for generating API clients; examples for Python, TypeScript, curl and docker-compose.

### Changed
- License changed to Apache-2.0.
- PDF rendering moved from PyMuPDF (AGPL) to pypdfium2, so no copyleft code is installed at runtime.
- The HTTP service moved to `docket.api` (`uvicorn docket.api:app`); the root `api.py` remains as a shim.

### Fixed
- The UBL and ZUGFeRD/XRechnung exporters produced XML that failed the
  official EN 16931 rules (the README called it Peppol BIS compatible). The
  new exporters pass the official XSD and Schematron of every profile they
  declare, checked in the test suite.
- A tax number the extraction filed as VAT (e.g. "Tax ID: GB-771-4402",
  filed as VAT for its GB prefix) failed the VAT checksum it never claimed;
  only a value with its country's VAT format is now held to that checksum.
- An empty text page was reported as "text accepted"; it is now a rejected,
  degraded page.
- The HTTP `/process` and `/jobs` runner called the endpoint function instead
  of the pipeline (a name collision introduced with `process_document`).
- `docket <file> --export <format>` crashed because `PipelineResult` had no `document` attribute.

[Unreleased]: https://github.com/KazKozDev/docket/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/KazKozDev/docket/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/KazKozDev/docket/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/KazKozDev/docket/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/KazKozDev/docket/releases/tag/v0.1.0
