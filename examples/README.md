# Examples

| File | What it shows |
|---|---|
| [`extract_invoice.py`](extract_invoice.py) | Use docket as a Python library: extract, check validation, read typed fields and where on the page each came from |
| [`export_einvoice.py`](export_einvoice.py) | Turn a PDF/scan into XRechnung, Factur-X/ZUGFeRD, Peppol or UBL XML and validate it right after export |
| [`validate_xrechnung.py`](validate_xrechnung.py) | Check an XRechnung (UBL or CII) with the official KoSIT and CEN rules, layer by layer |
| [`validate_peppol.py`](validate_peppol.py) | Check UBL files against Peppol BIS Billing 3.0 or plain EN 16931, one JSON line per file |
| [`batch_to_csv.py`](batch_to_csv.py) | A directory into a summary CSV and a line-item CSV, resumable |
| [`ocr_paddle.py`](ocr_paddle.py) | Read a scan with PaddleOCR (`docket-idp[paddle]`): words, rotation, tables |
| [`ocr_fallback.py`](ocr_fallback.py) | An OCR fallback chain and a per-page report of what was tried and used |
| [`read_tables.py`](read_tables.py) | Tables as structured cells with coordinates and spans |
| [`ocr_plugin/`](ocr_plugin/) | Ship an OCR backend as a separate pip package via the `docket.ocr_backends` entry point |
| [`custom_document_type.py`](custom_document_type.py) | Register your own schema (model, keywords, examples, cited fields, validators) and extra rules for built-in schemas |
| [`schema_plugin/`](schema_plugin/) | Ship a schema as a separate pip package via the `docket.schemas` entry point |
| [`custom_exporter.py`](custom_exporter.py) | Register your own output format at runtime |
| [`exporter_plugin/`](exporter_plugin/) | Ship a format as a separate pip package via the `docket.exporters` entry point |
| [`api_client.sh`](api_client.sh) | Call the HTTP API with curl: one document, then a multi-file job with CSV/JSONL downloads |
| [`api_client.ts`](api_client.ts) | A batch job from TypeScript / Node 18+, with structured error handling |
| [`streamlit_demo.py`](streamlit_demo.py) | Development UI: document preview and per-stage results (`streamlit run examples/streamlit_demo.py`) |
| [`docker-compose.yml`](docker-compose.yml) | Run the API in Docker against an EU-hosted LLM (Mistral) |
