# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): the public API is everything
exported from `docket`, the `docket` / `docket-api` commands, the HTTP API in
`docs/openapi.json`, and the `DOCKET_*` environment variables.

## [Unreleased]

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
- `docket <file> --export <format>` crashed because `PipelineResult` had no `document` attribute.

[Unreleased]: https://github.com/KazKozDev/docket/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/KazKozDev/docket/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/KazKozDev/docket/releases/tag/v0.1.0
