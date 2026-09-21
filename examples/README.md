# Examples

| File | What it shows |
|---|---|
| [`extract_invoice.py`](extract_invoice.py) | Use docket as a Python library: extract, check validation, read typed fields |
| [`export_einvoice.py`](export_einvoice.py) | Turn a PDF/scan into XRechnung, ZUGFeRD/Factur-X, UBL (Peppol) or Facturae XML |
| [`custom_exporter.py`](custom_exporter.py) | Register your own output format at runtime |
| [`exporter_plugin/`](exporter_plugin/) | Ship a format as a separate pip package via the `docket.exporters` entry point |
| [`api_client.sh`](api_client.sh) | Call the HTTP API with curl (sync and async jobs) |
| [`api_client.ts`](api_client.ts) | Call the HTTP API from TypeScript / Node 18+ |
| [`docker-compose.yml`](docker-compose.yml) | Run the API in Docker against an EU-hosted LLM (Mistral) |
