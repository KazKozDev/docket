# docket — local document AI, invoice & receipt OCR parser with LLMs

Turn scanned invoices, receipts, and contracts into structured, validated JSON using OCR and LLMs, then export them as EU e-invoices (XRechnung, ZUGFeRD / Factur-X, Peppol UBL, Facturae). Use it as a Python library, an HTTP service, or a CLI. Apache-2.0, so commercial use is fine.

<img width="1653" height="961" alt="demo" src="https://github.com/user-attachments/assets/86355d41-34a7-4201-9699-0fd62080c488" />

Ollama or any OpenAI-compatible API (Mistral, OpenAI, Azure, vLLM) · Pydantic schemas · Every value cited to its source line · Deterministic validation

## Quick start

Requirements: Python 3.10+, Tesseract on PATH (`brew install tesseract` / `apt install tesseract-ocr`), and an LLM, meaning either [Ollama](https://ollama.com) with a text and a vision model pulled, or an OpenAI-compatible API key (see [Configuration](#configuration)).

```bash
pip install docket-idp
docket invoice.pdf                       # JSON on stdout, exit code 2 if validation fails
docket invoice.pdf --export xrechnung    # e-invoice XML on stdout
```

```json
{
  "doc_type": "invoice",
  "invoice_number": "FAC-2026-0042",
  "issue_date": "2026-03-15",
  "vendor_name": "Talleres Montjuïc S.A.",
  "subtotal": 1234.56,
  "tax_amount": 259.26,
  "total_amount": 1493.82,
  "currency": "EUR"
}
```

## Use it in your application

**Python library**

```python
from docket import Invoice, export_document, process

result = process("invoice.pdf", enqueue_review=False)  # your app owns the review flow
if result.is_valid and isinstance(result.document, Invoice):
    xml = export_document(result.document, "xrechnung")
else:
    print(result.review_reasons, result.validation_issues)
```

`result.document` is the typed schema (`Invoice`, `Receipt`, `Contract`, …), and `result.field_sources` gives the page and quote each value was read from.

**HTTP service (any language)**

```bash
docker run -p 8000:8000 -e DOCKET_API_KEY=secret ghcr.io/kazkozdev/docket
curl -H "Authorization: Bearer secret" -F file=@invoice.pdf localhost:8000/process
```

`POST /process` is synchronous. `POST /jobs` returns 202 and a job id to poll at `GET /jobs/{id}`, and an `Idempotency-Key` header makes retries safe. `/review-queue` serves flagged documents. Generate a typed client from [`docs/openapi.json`](https://github.com/KazKozDev/docket/blob/master/docs/openapi.json); interactive docs are at `/docs`. Without Docker: `pip install "docket-idp[api]" && docket-api`.

[`examples/`](https://github.com/KazKozDev/docket/blob/master/examples/) has runnable scripts, a TypeScript client, a Mistral-backed `docker-compose.yml` and plugin packages.

## Document types

Built in: invoice, receipt, contract, purchase order, bank statement, acceptance act, waybill, boarding pass. To add your own, write a Pydantic model and register it:

```python
from datetime import date
from docket import CitedDocument, register_document_type

class DeliveryNote(CitedDocument):       # CitedDocument adds page/quote citations
    note_number: str
    supplier_name: str
    delivery_date: date

register_document_type(
    "delivery_note", DeliveryNote,
    description="Delivery note / Lieferschein listing goods handed over",  # read by the LLM classifier
    keywords=["delivery note", "lieferschein"],                             # free rules tier
)
```

Registered types are classified, extracted, citation-checked and exported like the built-in ones. `add_validator("invoice", fn)` adds your own rules to any type, and the `docket.document_types` entry point lets a separate package ship types. See [`examples/custom_document_type.py`](https://github.com/KazKozDev/docket/blob/master/examples/custom_document_type.py).

## Export formats

| Format | Name |
|---|---|
| XRechnung (CII) | `xrechnung` |
| ZUGFeRD 2.2 / Factur-X, EN 16931 | `zugferd` |
| UBL 2.1 / Peppol BIS Billing 3.0 | `ubl` |
| Facturae 3.2.2 (Spain) | `facturae` |
| SAP IDoc / journal CSV | `sap-idoc`, `sap-csv` |
| Xero, QuickBooks | `xero-csv`, `xero-json`, `quickbooks-iif`, `quickbooks-json` |

Add your own with `register_exporter("my-erp", func, accepts=(Invoice,))` or the `docket.exporters` entry point. `docket --list-formats` shows everything available. Validate generated XML with the recipient's official validator (e.g. KoSIT for XRechnung) before going live.

## How it works

```
document → text layer / OCR / VLM → classify → extract + cite → validate → JSON or review
```

- **Text** comes from the cheapest source that works: the PDF text layer, then Tesseract, then a vision model, which is used only when OCR confidence is low or a cheap text model judges the scan unusable.
- **Classification** tries keyword rules, then TF-IDF, then an LLM. Each tier runs only when the one before it was unsure.
- **Extraction** fills a Pydantic schema under a JSON Schema contract and cites the verbatim line for every value. Output that fails the schema goes back to the model with the error attached.
- **Validation** never calls a model. It checks arithmetic, dates, IBAN mod-97, VAT check digits (all 27 EU states, UK, CH, NO), national tax IDs, and that every cited line exists and contains the claimed value. Contracts also get counterparty, grounding and risk checks (unlimited liability, auto-renewal, notice periods).
- **Review**: low confidence, failed extraction or a validation error sends the document to a review queue that keeps the original and an audit history. Nothing is silently reconciled. An invoice whose `Amount Due: 500.00` disagrees with its own 270.60 subtotal and tax is flagged, not fixed.

Also included: cross-document matching (invoice ↔ PO, three-way PO/waybill/invoice, invoice ↔ contract, receipt ↔ bank transactions) and stamp, signature and alteration detection (`docket file.pdf --forensics`). Details are in [ARCHITECTURE.md](https://github.com/KazKozDev/docket/blob/master/docs/ARCHITECTURE.md).

## Configuration

Set in the environment or `.env`. [`.env.example`](https://github.com/KazKozDev/docket/blob/master/.env.example) and [`config.py`](https://github.com/KazKozDev/docket/blob/master/src/docket/config.py) have the full list.

| Option | Default | What it does |
|---|---|---|
| `DOCKET_LLM_PROVIDER` | `ollama` | `ollama`, or `openai` for any OpenAI-compatible API |
| `DOCKET_LLM_BASE_URL` / `DOCKET_LLM_API_KEY` | OpenAI / unset | Endpoint and key for `openai`, e.g. `https://api.mistral.ai/v1` (EU-hosted) |
| `DOCKET_TEXT_MODEL` / `DOCKET_VISION_MODEL` | `deepseek-v4.1-flash:cloud` | Models for extraction and for reading scans |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening |
| `DOCKET_OCR_LANG` | `eng` | Tesseract languages, e.g. `eng+deu+fra+spa+ita` |
| `DOCKET_MIN_CONFIDENCE` | `0.55` | Classification confidence below which a document goes to review |
| `DOCKET_REVIEW_QUEUE_ENABLED` | `true` | Write flagged documents to the file-based review queue |
| `DOCKET_API_KEY` | unset | Bearer token the HTTP API requires when set |

## Limitations

- Built-in keyword rules and the TF-IDF model cover English and Spanish, so other languages rely on the LLM tier.
- The vision model has been observed changing digits so that a page reconciles (a printed `450.00` read as `480.00` three times out of three). There is no fix for that in this repo.
- Line items carry no source citations, so the citation check doesn't cover them.
- The review queue is a single file: durable on one node, not across hosts.
- Windows is untested. A document takes 3.6–9.5 s, longer when a page needs the vision model.

<details>
<summary>Install options, source setup, development</summary>

```bash
pip install docket-idp            # library + CLI
pip install "docket-idp[api]"     # + HTTP service (docket-api)
pip install "docket-idp[all]"     # + Streamlit UI, terminal UI, Langfuse tracing
```

From source:

```bash
git clone https://github.com/KazKozDev/docket.git
cd docket && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" && cp .env.example .env
python tui.py invoice.pdf        # live per-stage progress
streamlit run app.py             # browser UI with a document preview
```

On macOS, double-clicking `start.command` sets everything up and opens the UI.

```bash
pytest                                # no test needs a running LLM
python eval/run_eval.py               # accuracy, P/R/F1, latency on the golden set
python eval/benchmark_methods.py      # rules vs TF-IDF vs LLM comparison
```

</details>

---

<div align="center">

![macOS](https://img.shields.io/badge/macOS-333?style=flat-square&logo=apple&logoColor=fff) ![Linux](https://img.shields.io/badge/Linux-333?style=flat-square&logo=linux&logoColor=fff)

![Python](https://img.shields.io/badge/Python-3.10+-333?style=flat-square&logo=python&logoColor=fff) [![PyPI](https://img.shields.io/pypi/v/docket-idp?style=flat-square)](https://pypi.org/project/docket-idp/) [![License](https://img.shields.io/badge/License-Apache--2.0-blue?style=flat-square)](https://github.com/KazKozDev/docket/blob/master/LICENSE) [![Tests](https://github.com/KazKozDev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/KazKozDev/docket/actions)

[Issues](https://github.com/KazKozDev/docket/issues) · [ARCHITECTURE](https://github.com/KazKozDev/docket/blob/master/docs/ARCHITECTURE.md) · [CONTRIBUTING](https://github.com/KazKozDev/docket/blob/master/CONTRIBUTING.md) · [CHANGELOG](https://github.com/KazKozDev/docket/blob/master/CHANGELOG.md) · [LICENSE](https://github.com/KazKozDev/docket/blob/master/LICENSE) · [LinkedIn](https://www.linkedin.com/in/kazkozdev/)

</div>
