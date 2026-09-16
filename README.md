# docket — invoice and receipt data extraction with OCR and LLMs

Turn scanned invoices, receipts and contracts into validated JSON.

<img width="1653" height="961" alt="demo" src="https://github.com/user-attachments/assets/86355d41-34a7-4201-9699-0fd62080c488" />

Runs locally · Every number cites its source · MIT licensed

---

## Quick start

You need [Ollama](https://ollama.com) running with a text and a vision model
pulled, and Tesseract on PATH (`brew install tesseract`, `apt install
tesseract-ocr`).

```bash
git clone https://github.com/KazKozDev/docket.git
cd docket
pip install -e .
cp .env.example .env
```

Point it at a document. Classification, extraction and validation all run
locally against whichever models `.env` names.

```bash
python -m docket.cli eval/golden_dataset/invoice_spanish.txt
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

The process exits non-zero when validation fails, so it drops into a shell
pipeline as-is.

## Extract structured fields from an invoice or receipt

Each document type has a Pydantic schema, and the model fills it under a JSON
Schema contract rather than being asked nicely for JSON. A result that fails
validation is sent back to the model with the error attached.

```bash
python -m docket.cli invoice.pdf
python tui.py invoice.pdf        # same pipeline, live per-stage progress
streamlit run app.py             # browser UI with a document preview
```

European and American number conventions are both parsed, so `1.234,56` and
`1,234.56` read as the same amount. Keyword rules cover English and Spanish.

## Send uncertain documents to human review

Low classification confidence, a failed extraction or an error-severity
validation issue routes the document to a review queue instead of a database.
Each entry gets a stable id, a status, the preserved original, and an audit
history of who changed what.

```bash
python -m docket.cli eval/golden_dataset/invoice_bad_total.txt
```

```
doc_9479a6b321e02ab6de25  pending  invoice
    validation error: total_amount — subtotal + tax + shipping - discount = 270.60,
    total_amount says 500.00
```

That document prints `Amount Due: 500.00` while its own subtotal and tax add up
to 270.60. Nothing silently reconciles it.

## Run document extraction as an HTTP service

```bash
DOCKET_API_KEY=secret uvicorn api:app
curl -H "Authorization: Bearer secret" -F file=@invoice.pdf localhost:8000/process
```

`POST /process` runs synchronously; `POST /jobs` queues and returns 202 with a
job id for `GET /jobs/{id}`. `GET /review-queue` lists what is waiting for a
person, and `/review-queue/{id}/original` returns the document that produced it.
Interactive docs at `/docs`.

## How it works

Text comes from the cheapest source that works: a PDF text layer if there is
one, Tesseract for scans, and a vision model only when OCR confidence is low or
a cheap text model judges the scan unusable. Classification tries keyword rules,
then a TF-IDF model, then an LLM — each tier runs only because the last was not
confident. Extraction fills a Pydantic schema and cites, for every number, the
line it was read from. Validation is deterministic and never calls a model: it
checks arithmetic, date ranges, IBAN mod-97 and VAT check digits, and that each
cited line exists and contains the number claimed.

```
document → text layer / OCR / VLM → classify → extract + cite → validate → JSON or review
```

## Configuration

| Option | Default | What it does |
|---|---|---|
| `DOCKET_TEXT_MODEL` | `deepseek-v4.1-flash:cloud` | Model for classification and extraction |
| `DOCKET_VISION_MODEL` | `deepseek-v4.1-flash:cloud` | Model for transcribing scans |
| `OLLAMA_HOST` | `http://localhost:11434` | Where Ollama is listening |
| `DOCKET_ENABLE_THINKING` | `false` | Reasoning tokens; off is markedly faster for this task |
| `DOCKET_MIN_CONFIDENCE` | `0.55` | Classification confidence below which a document goes to review |
| `DOCKET_TFIDF_CONFIDENCE_FLOOR` | `0.65` | TF-IDF confidence needed to skip the LLM tier |
| `DOCKET_OCR_QUALITY_CHECK` | `true` | Ask a cheap model whether a scan is usable before extracting |
| `DOCKET_VISION_TIMEOUT_S` | `300` | Vision call timeout |
| `DOCKET_MAX_FILE_BYTES` | `20971520` | Upload limit for the API |
| `DOCKET_MAX_PDF_PAGES` | `100` | Page ceiling per document |
| `DOCKET_API_KEY` | unset | Bearer token; the API refuses requests without it when set |
| `DOCKET_REVIEW_QUEUE` | `data/review_queue.jsonl` | Review journal path |

Full list in `src/docket/config.py`; `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` enable optional tracing.

## Requirements

- Python 3.10+
- macOS or Linux
- Tesseract OCR on PATH
- A running Ollama with one text and one vision model pulled

## Limitations

- Four document types: invoice, receipt, contract, boarding pass.
- Keyword rules and the TF-IDF corpus cover English and Spanish only.
- The vision model has been observed altering digits to make a page reconcile — a printed `450.00` read three times out of three as `480.00`. No fix for that is in this repo.
- Line items carry no source citations, so the cited-source check does not cover them.
- The review queue is one file: durable on one node, not across hosts.
- Windows is untested; CI runs Linux only. 3.6–9.5 s per document, more when a page needs the vision model.

<details>
<summary>Manual installation, Docker, development setup</summary>

### From source

```bash
git clone https://github.com/KazKozDev/docket.git
cd docket && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On macOS, double-clicking `start.command` creates the venv, starts Ollama if
needed, frees ports 8000/8501 and opens the UI.

### Docker

```bash
docker build -t docket .
docker run -p 8000:8000 docket
```

The image installs Tesseract and talks to Ollama on the host.

### Development

```bash
pytest                                # 224 tests, none need a running Ollama
python eval/run_eval.py               # accuracy, P/R/F1, latency on the golden set
python eval/benchmark_methods.py      # the rules vs TF-IDF vs LLM comparison
```

</details>

---

<div align="center">

![macOS](https://img.shields.io/badge/macOS-333?style=flat-square&logo=apple&logoColor=fff) ![Linux](https://img.shields.io/badge/Linux-333?style=flat-square&logo=linux&logoColor=fff)

![Python](https://img.shields.io/badge/Python-3.10+-333?style=flat-square&logo=python&logoColor=fff) [![License](https://img.shields.io/badge/License-MIT-333?style=flat-square)](LICENSE) [![Tests](https://github.com/KazKozDev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/KazKozDev/docket/actions)

[Issues](https://github.com/KazKozDev/docket/issues) · [License](LICENSE)

</div>
