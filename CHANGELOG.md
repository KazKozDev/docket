# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/): the public API is everything
exported from `docket`, the `docket` / `docket-api` commands, the HTTP API in
`docs/openapi.json`, and the `DOCKET_*` environment variables.

## [Unreleased]

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

[Unreleased]: https://github.com/KazKozDev/docket/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/KazKozDev/docket/releases/tag/v0.1.0
