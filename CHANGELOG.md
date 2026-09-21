# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): the public API is everything
exported from `docket`, the `docket` / `docket-api` commands, the HTTP API in
`docs/openapi.json`, and the `DOCKET_*` environment variables.

## [Unreleased]

Redesign in progress (layout-first model, pluggable OCR, schema catalog,
batch processing, official e-invoice validation). This section grows with
each stage; see the Breaking changes list.

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
- Configuration errors share the base `docket.ConfigurationError`
  (`UnknownLanguage`, `OcrBackendError`, `BackendUnavailable`,
  `DocumentTypeError`).
- `review_queue.reasons_for`, `enqueue`, `list_pending`, `get`, `update` and
  `clear` take the threshold / queue location as keyword arguments.
- CLI: a configuration error exits with code 3.

### Added
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
- `docket --ocr-backend`, `--ocr-fallback`, `--no-ocr-fallback`,
  `--ocr-languages`, `--list-ocr-backends`; `GET /ocr-backends`.

### Changed
- With 14 built-in schemas instead of 8 the TF-IDF tier is confident less
  often: 19 of 34 held-out sentences (none confidently wrong), and the rules
  tier's confidence (a share of all matched weight) is lower when a text
  matches several schemas. Not yet measured on the eval sets.

### Fixed
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
- The HTTP `/process` and `/jobs` runner called the endpoint function instead
  of the pipeline (a name collision introduced with `process_document`).
- `docket <file> --export <format>` crashed because `PipelineResult` had no `document` attribute.

[Unreleased]: https://github.com/KazKozDev/docket/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/KazKozDev/docket/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/KazKozDev/docket/releases/tag/v0.1.0
