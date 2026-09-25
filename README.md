# docket — Python OCR and LLM for invoices, receipts and contracts

Extract cited document data, check it, and export validated invoices.


![Docket Desktop showing a scanned receipt beside extracted fields](https://raw.githubusercontent.com/KazKozDev/docket/master/docs/assets/docket-desktop-promo.png)

Docket Desktop


## Quick start

You need Python 3.10+, Tesseract for scans, and a reachable text model. Configure Ollama or an OpenAI-compatible API; a vision model is needed for the default OCR fallback. Check the active settings with `docket config show`.

```bash
docket process invoice.pdf
```

The command prints a JSON result with extracted fields, source citations, validation issues and review reasons. Its exit code is 0 for `succeeded`, 1 for `needs_review`, 2 for `failed`, or 3 for a configuration error.

## Extract invoice, receipt and contract data in Python

Use the typed result in your own application. Keep results needing review out of automatic downstream updates.

```python
from docket import DocumentStatus, ProcessOptions, process_document

result = process_document("invoice.pdf", ProcessOptions(document_type="invoice"))
if result.status == DocumentStatus.SUCCEEDED:
    print(result.document)
    print(result.field_sources.get("total_amount"))
else:
    print(result.status.value, result.review_reasons)
```

`result.document` is a Pydantic schema; each available field source includes its page, quote and location. Seven built-in document types are listed by `docket schemas list`. You can register another schema or OCR backend; see the [examples](https://github.com/KazKozDev/docket/tree/master/examples).

## Process invoices and receipts in a folder into CSV

Batch processing writes a summary CSV, a line-item CSV and a checkpoint file. Repeating the command resumes completed documents; one failure does not stop the rest.

```bash
docket batch scans/ --recursive --workers 4 --format csv --output results.csv
```

Use `--format jsonl` for one complete result per line or `--ocr-backend paddle` after installing the optional PaddleOCR backend.

## Validate and export European e-invoices from PDFs

Install the validator and fetch official rule files that cannot be shipped in the package. Export is refused when the extracted result needs review or the invoice cannot be represented faithfully.

```bash
pip install "docket-idp[einvoice]"
docket einvoice fetch
docket process invoice.pdf --document-type invoice --export xrechnung-ubl --validate-export -o invoice.xml
```

Other built-in formats include UBL, Peppol, XRechnung CII, Factur-X and Facturae (`docket formats`). Converting a supplier PDF gives you data for your books; it does not issue a legal e-invoice on the supplier's behalf.

## How it works

Docket reads each page from a usable PDF text layer, an OCR backend, or a vision model when OCR is unusable. Rules, TF-IDF and then an LLM classify the document. Extraction fills a versioned Pydantic schema and cites source lines. Deterministic checks cover citations, amounts, dates and check digits; uncertain results go to review. The [architecture](https://github.com/KazKozDev/docket/blob/master/docs/ARCHITECTURE.md) describes the stages and extension points.

```text
document → PDF text / OCR / vision → classify → extract + cite → validate → JSON or review
```

## Configuration

| Environment variable | Default | Purpose |
|---|---|---|
| `DOCKET_LLM_PROVIDER` | `ollama` | `ollama` or `openai` for an OpenAI-compatible endpoint |
| `DOCKET_TEXT_MODEL` / `DOCKET_VISION_MODEL` | `deepseek-v4.1-flash:cloud` | Extraction model / page-image model |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `DOCKET_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API URL |
| `DOCKET_LLM_API_KEY` | unset | Key for the OpenAI-compatible API |
| `DOCKET_OCR_BACKEND` / `DOCKET_OCR_FALLBACKS` | `auto` / `vlm` | Primary OCR / fallback chain |
| `DOCKET_OCR_LANGUAGES` | `en` | Comma-separated language codes, e.g. `en,de` |
| `DOCKET_MIN_SOURCE_CONFIDENCE` | `0.8` | Minimum OCR confidence for cited key fields |
| `DOCKET_CONFIG` | unset | TOML file; environment and explicit options override it; CLI also reads `./docket.toml` |

## Requirements

- Python 3.10 or newer on macOS or Linux; CI tests Python 3.10–3.12 on Ubuntu.
- Tesseract on `PATH` for scanned pages, or install the optional PaddleOCR or Docling backend. Usable PDF text layers need no OCR engine.
- A reachable Ollama server or OpenAI-compatible API, with a text model configured. The `vlm` fallback also needs a vision model.
- The `[einvoice]` extra and `docket einvoice fetch` for official e-invoice validation.
- The desktop app is a separate source package in [`apps/desktop`](https://github.com/KazKozDev/docket/tree/master/apps/desktop); its native build targets macOS.

## Limitations

- Wrong fields can still pass as `succeeded`: 13 of 66 such results were wrong in the latest published 195-document run. Review critical values before use ([method and results](https://github.com/KazKozDev/docket/blob/master/docs/BENCHMARKS.md)).
- About two thirds of documents in that run needed human review; degraded SROIE receipts were the main source of silent errors.
- The vision model has been observed changing digits to reconcile totals.
- Windows is untested. The local macOS app build is unsigned and unnotarized.
- The desktop workflow handles invoices and receipts; contracts and e-invoice tools remain in the library and CLI.

<details>
<summary>Desktop app, extras, HTTP API and development</summary>

### Desktop app

From a checkout, install the separate package with `python -m pip install -e . -e apps/desktop` and run `docket-desktop`. It imports PDF/images, supports correction and approval, and exports approved data as XLSX, CSV or JSON. See the [desktop guide](https://github.com/KazKozDev/docket/blob/master/docs/DESKTOP.md).

### Extras

`docket-idp[api]` adds the HTTP service; `[photo]` adds photo cropping; `[paddle]` and `[docling]` add OCR backends; `[review]` adds the persistent review queue. See [`pyproject.toml`](https://github.com/KazKozDev/docket/blob/master/pyproject.toml) for the full list.

### HTTP API and Docker

```bash
docker run -p 8000:8000 -e DOCKET_API_KEY=secret ghcr.io/kazkozdev/docket
curl -H "Authorization: Bearer secret" -F file=@invoice.pdf localhost:8000/process
```

See the [OpenAPI specification](https://github.com/KazKozDev/docket/blob/master/docs/openapi.json) for endpoints and responses.

### Development

```bash
git clone https://github.com/KazKozDev/docket.git && cd docket
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && pytest
```

</details>

---

<div align="center">

[![Tests](https://github.com/KazKozDev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/KazKozDev/docket/actions) [![Python](https://img.shields.io/badge/Python-3.10%2B-333?style=flat-square)](https://github.com/KazKozDev/docket/blob/master/pyproject.toml) [![PyPI](https://img.shields.io/pypi/v/docket-idp?style=flat-square)](https://pypi.org/project/docket-idp/) [![License](https://img.shields.io/badge/License-Apache--2.0-blue?style=flat-square)](https://github.com/KazKozDev/docket/blob/master/LICENSE)

[Issues](https://github.com/KazKozDev/docket/issues) · [Contributing](https://github.com/KazKozDev/docket/blob/master/CONTRIBUTING.md) · [Security](https://github.com/KazKozDev/docket/blob/master/docs/SECURITY.md) · [License](https://github.com/KazKozDev/docket/blob/master/LICENSE) · [Architecture](https://github.com/KazKozDev/docket/blob/master/docs/ARCHITECTURE.md) · [Benchmarks](https://github.com/KazKozDev/docket/blob/master/docs/BENCHMARKS.md) · [Changelog](https://github.com/KazKozDev/docket/blob/master/CHANGELOG.md)

</div>
