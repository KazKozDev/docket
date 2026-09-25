# docket — Python library for invoice, receipt and contract OCR extraction with LLMs

Turn scanned invoices, receipts and contracts into validated JSON with source citations, then export EU e-invoices checked against the official rules.

```bash
pip install docket-idp
```

![Docket Desktop reviewing a receipt and its extracted fields](docs/assets/demo-docket.png)

Python library + CLI · Ollama or any OpenAI-compatible API · Every value cited to its source line · Apache-2.0

---

## Quick start

Needs Python 3.10+, Tesseract on PATH (`brew install tesseract` / `apt install tesseract-ocr`), and an LLM: [Ollama](https://ollama.com) with a text and a vision model pulled, or an OpenAI-compatible API key (see [Configuration](#configuration)).

```bash
pip install docket-idp
docket process invoice.pdf        # JSON on stdout; exit 2 if validation fails
```

```json
{
  "status": "succeeded",
  "document_type": "invoice",
  "extracted": {"invoice_number": "FAC-2026-0042", "issue_date": "2026-03-15", "total_amount": 1493.82, "currency": "EUR"},
  "field_sources": {"total_amount": {"page": 1, "quote": "Total factura: 1.493,82 €", "bbox": {"x0": 0.62, "y0": 0.71, "x1": 0.89, "y1": 0.73}}}
}
```

## Extract invoice data in your Python application

```python
from docket import ExportError, OcrOptions, ProcessOptions, export_document, process_document

options = ProcessOptions(document_type="invoice", ocr=OcrOptions(backend="tesseract", fallbacks=["vlm"]))
result = process_document("invoice.pdf", options)
try:
    xml = export_document(result, "xrechnung-ubl").content   # refuses invalid or unreviewed results
except ExportError:
    print(result.status, result.review_reasons)
```

`result.document` is the typed schema (`Invoice`, `Receipt`, `Contract`, …), `result.status` is `succeeded`, `needs_review` or `failed`, and `result.field_sources` gives the page, quote and box of every value. Nothing is written to disk unless you ask for it. For directories use `process_batch("scans/", options, BatchOptions(workers=4, checkpoint="run.jsonl"))` or `docket batch scans/ --format csv --output results.csv`: input order is kept, one failure never stops the rest, and a rerun skips checkpointed documents.

## Export and validate XRechnung, Factur-X and Peppol e-invoices

| Format | Name | Checked against |
|---|---|---|
| UBL 2.1 / Peppol BIS 3.0 | `ubl`, `peppol` | EN 16931 (+ Peppol BIS 3.0.20) |
| XRechnung 3.0, UBL / CII | `xrechnung-ubl`, `xrechnung-cii` | EN 16931 + XRechnung 3.0.2 |
| Factur-X / ZUGFeRD CII | `factur-x-en16931`, `factur-x-basic` | Factur-X 1.09 profile rules |
| Facturae 3.2.2 (Spain) | `facturae` | — |

Formats for a particular ERP or accounting system (SAP, Xero, QuickBooks, 1C) are not built in: register your own with `register_exporter()`.

```bash
pip install "docket-idp[einvoice]"
docket einvoice fetch                              # once: Peppol, CII and Factur-X rules are not shipped
docket process invoice.pdf --export xrechnung-ubl --validate-export
docket validate-einvoice invoice.xml               # XSD + Schematron; exit 0 valid, 2 invalid, 3 not set up
docket factur-x create invoice.pdf factur-x.xml -o hybrid.pdf
```

Converting a supplier's scanned or PDF invoice gives you its data in the EN 16931 model for your own books and checks; it does not turn it into a legally issued e-invoice, which only the supplier can send.

Extracted the data with something else? `verify(data, page_texts, document_type="invoice")` runs the same checks against the source text (every cited quote on its page, every number and date on its cited line, arithmetic, check digits) and returns the same `DocumentResult`, so `export_document` accepts or refuses it on the same terms. A schema instance passed straight to `export_document` is checked for arithmetic, dates and check digits before it is exported.

Exporters refuse what they can't represent faithfully (no line items, tax that doesn't match the lines) instead of guessing. Validation runs offline with the official artifacts, pinned by SHA-256 in the [manifest](https://github.com/KazKozDev/docket/blob/master/src/docket/einvoice/resources/manifest.json). Artifacts without verified redistribution terms are downloaded on request, not shipped; see the [third-party licence inventory](https://github.com/KazKozDev/docket/blob/master/docs/THIRD_PARTY_LICENSES.md). In Python: `validate_einvoice("invoice.xml")`, and `generate_facturx_pdf()` / `verify_facturx_round_trip()` for PDF/A-3 with veraPDF.

## Add custom document types and vendor templates

The catalog has 7 versioned schemas (`docket schemas list`), all for the documents of accounts payable. Six are stable: invoice, purchase order, receipt, contract, bank statement and waybill (the goods receipt in `match_three_way`). The credit note is experimental. Add your own as a Pydantic model:

```python
from datetime import date
from docket import CitedDocument, Party, SchemaSpec, keywords, register_schema

class ParkingTicket(CitedDocument):      # CitedDocument adds page/quote citations
    ticket_number: str
    issuing_authority: Party
    issue_date: date
    fine: float

register_schema(SchemaSpec(
    schema_id="parking_ticket", version="1.0", model=ParkingTicket,
    description="Parking ticket / Strafzettel",
    keywords=keywords("parking ticket", "strafzettel"),
    cited_fields=("ticket_number", "issuing_authority.name", "fine"),
))
```

Registered schemas are classified, extracted, citation-checked and exported like built-in ones. `register_vendor_template(VendorTemplate(...))` reads a known vendor layout with explicit rules and no LLM, accepted only when validation passes. `register_exporter()` adds output formats. Schemas, exporters and OCR backends can also ship as separate packages via entry points; see [`examples/`](https://github.com/KazKozDev/docket/blob/master/examples/).

## Measure extraction accuracy on public datasets

The number that matters most is how often docket says "succeeded" and is wrong, because that result goes straight into the books unseen. On the latest full run it was **13 of 66 silent successes (20%)**, down from 27 of 59 (46%) on a slightly different corpus before the checks that target it (date order, payment arithmetic, document numbers against their cited line, OCR confidence on key fields); excluding the SROIE Malaysian receipts, which the checks are not tuned for, it is **3 of 24 (13%)**. On that run the confidence check sent 57 documents to review, 17 of them actually wrong; the document-number check flagged none.

195 scans: the project's labelled golden set plus real documents from public Hugging Face datasets (DocILE, SROIE, CORD, FUNSD, RVL-CDIP, donut-style invoices), graded field by field against the datasets' own ground truth. On the same run, docket gets **0.88 field accuracy**: 0.96 on the golden set, 0.99 on donut invoices, 0.80 on SROIE receipts. 73% of documents come back with every field right.

Against the pip-installable alternatives, each tool is graded only on the documents and fields it supports, and docket is graded on exactly the same ones:

| | docs | tool | docket on the same docs |
|---|---|---|---|
| docpick 0.1.3 | 55 | 0.61 | **0.85** |
| ocrcontext 0.1.5 | 55 | 0.04 | **0.83** |
| invoice2data 1.0.1 | 44 | 0.00 | **0.89** |

Method, per-source results and how to rerun: [docs/BENCHMARKS.md](https://github.com/KazKozDev/docket/blob/master/docs/BENCHMARKS.md).

## How it works

Text comes from the cheapest source that works, page by page: PDF text layer, then OCR (Tesseract, PaddleOCR, Docling or a plugin), then a vision model only when OCR is unusable. Before OCR, a phone photo of a receipt or invoice is cropped to the paper and flattened (the `[photo]` extra), and Tesseract re-reads small text enlarged; `OcrOptions(preprocess=fn)` adds your own step on every page image. Classification tries keyword rules, TF-IDF, then an LLM. Extraction fills a Pydantic schema and cites the verbatim line for every value; schema errors go back to the model. Validation never calls a model: arithmetic to the cent, dates, IBAN, VAT and tax-ID check digits, and that every cited line contains the value. Anything uncertain goes to review instead of being silently fixed.

```
document → text layer / OCR / VLM → classify → extract + cite → validate → JSON or review
```

## Configuration

Priority, lowest to highest: defaults, a TOML file (`--config` or `DOCKET_CONFIG`), the environment, explicit arguments. Importing docket reads nothing else; the `docket` CLI and `docket-api` also read `.env` and `./docket.toml` from the working directory. Every setting is in [`docket.example.toml`](https://github.com/KazKozDev/docket/blob/master/docket.example.toml); `docket config show` prints effective values and their source.

| Variable | Default | What it does |
|---|---|---|
| `DOCKET_LLM_PROVIDER` | `ollama` | `ollama`, or `openai` for any OpenAI-compatible API |
| `DOCKET_LLM_BASE_URL` / `DOCKET_LLM_API_KEY` | OpenAI / unset | Endpoint and key for `openai`, e.g. `https://api.mistral.ai/v1` |
| `DOCKET_TEXT_MODEL` / `DOCKET_VISION_MODEL` | `deepseek-v4.1-flash:cloud` | Models for extraction and for reading scans |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening |
| `DOCKET_OCR_BACKEND` | `auto` | `tesseract`, `paddle`, `docling`, `auto` or a plugin name |
| `DOCKET_OCR_FALLBACKS` | `vlm` | Backends tried when a page's reading is rejected |
| `DOCKET_OCR_LANGUAGES` | `en` | ISO 639-1 codes, e.g. `en,de,fr` |
| `DOCKET_OCR_CROP_PHOTOS` | `true` | Crop and flatten the document in a photo (needs `[photo]`) |
| `DOCKET_OCR_MIN_TEXT_HEIGHT` | `20` | Tesseract re-reads a page enlarged when words are shorter than this (px); `0` off |
| `DOCKET_MIN_CONFIDENCE` | `0.55` | Classification confidence below which a document goes to review |
| `DOCKET_MIN_SOURCE_CONFIDENCE` | `0.8` | OCR confidence a key field's cited words need, or the document goes to review |
| `DOCKET_REVIEW_QUEUE_ENABLED` | `false` | Persist flagged documents in the review queue (`[review]` extra) |
| `DOCKET_REVIEW_DATABASE_URL` | `sqlite:///data/review.db` | Review store; PostgreSQL with the `[postgres]` extra |
| `DOCKET_BATCH_WORKERS` | `4` | Documents in flight per batch |
| `DOCKET_EINVOICE_DOWNLOADS` | `~/.cache/docket/einvoice` | Where `docket einvoice fetch` stores artifacts |

## Requirements

- Python 3.10+
- Tesseract on PATH (or the `[paddle]` / `[docling]` extra)
- Ollama, or any OpenAI-compatible API (Mistral, OpenAI, Azure, vLLM)
- macOS or Linux; CI runs on Ubuntu

## Limitations

- A silent wrong answer is possible: on the 195-scan corpus, 20% of "succeeded, no review" documents (13 of 66) had at least one wrong field — 10 of the 13 are degraded Malaysian thermal receipts (SROIE); excluding that source it is 3 of 24 (13%). See [benchmarks](https://github.com/KazKozDev/docket/blob/master/docs/BENCHMARKS.md).
- Two thirds of documents still go to review, which is the intended path when anything is uncertain. Scanned DocILE invoices are the weakest source (0.43).
- The vision model has been seen changing digits so that a page reconciles.
- Windows is untested. A document takes a median of 6.4–22.7 s depending on the OCR backend (8.7 s on the full corpus with Tesseract), longer with the vision model.

<details>
<summary>Extras, HTTP service, Docker, development</summary>

### Extras

```bash
pip install "docket-idp[api]"       # HTTP service (docket-api)
pip install "docket-idp[einvoice]"  # official e-invoice validation
pip install "docket-idp[review]"    # persistent review queue (SQLAlchemy; [postgres] for PostgreSQL)
pip install "docket-idp[paddle]"    # PaddleOCR backend
pip install "docket-idp[docling]"   # Docling/TableFormer backend
pip install "docket-idp[photo]"     # crop and flatten documents in phone photos (OpenCV)
pip install "docket-idp[all]"       # + Langfuse tracing and e-invoice validation
```

The native invoice and receipt app lives in the separate
[`apps/desktop`](apps/desktop/) package. From a checkout, install it with
`python -m pip install -e . -e apps/desktop` and run `docket-desktop`.

For the native, browser-free invoice and receipt workflow, see [Docket Desktop](docs/DESKTOP.md).

Langfuse tracing starts only when `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, and then records model, latency and sizes. Prompts and answers, which contain the document's text, are sent only with `DOCKET_LANGFUSE_CONTENT=true`.

### HTTP service and Docker

Reference applications on the same library contract, for other languages:

```bash
docker run -p 8000:8000 -e DOCKET_API_KEY=secret ghcr.io/kazkozdev/docket
curl -H "Authorization: Bearer secret" -F file=@invoice.pdf localhost:8000/process
```

Without `DOCKET_API_KEY`, `docket-api` serves only on 127.0.0.1; on any other address it refuses to start unless you pass `--no-auth` (an authenticating proxy in front). `POST /jobs` handles multi-file jobs with CSV/JSONL downloads; the typed contract is [`docs/openapi.json`](https://github.com/KazKozDev/docket/blob/master/docs/openapi.json), with interactive docs at `/docs`.

### Development

```bash
git clone https://github.com/KazKozDev/docket.git && cd docket
python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest                                   # no test needs a running LLM
streamlit run examples/streamlit_demo.py # demo UI
```

</details>

---

<div align="center">

![macOS](https://img.shields.io/badge/macOS-333?style=flat-square&logo=apple&logoColor=fff) ![Linux](https://img.shields.io/badge/Linux-333?style=flat-square&logo=linux&logoColor=fff)

![Python](https://img.shields.io/badge/Python-3.10+-333?style=flat-square&logo=python&logoColor=fff) [![PyPI](https://img.shields.io/pypi/v/docket-idp?style=flat-square)](https://pypi.org/project/docket-idp/) [![License](https://img.shields.io/badge/License-Apache--2.0-blue?style=flat-square)](https://github.com/KazKozDev/docket/blob/master/LICENSE) [![Tests](https://github.com/KazKozDev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/KazKozDev/docket/actions)

[Contributing](https://github.com/KazKozDev/docket/blob/master/CONTRIBUTING.md) · [Security](https://github.com/KazKozDev/docket/blob/master/docs/SECURITY.md) · [License](https://github.com/KazKozDev/docket/blob/master/LICENSE) · [Architecture](https://github.com/KazKozDev/docket/blob/master/docs/ARCHITECTURE.md) · [API stability](https://github.com/KazKozDev/docket/blob/master/docs/API_STABILITY.md) · [Benchmarks](https://github.com/KazKozDev/docket/blob/master/docs/BENCHMARKS.md) · [Changelog](https://github.com/KazKozDev/docket/blob/master/CHANGELOG.md)

</div>
