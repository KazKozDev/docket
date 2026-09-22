# Security

## Threat model

Docket treats every document as untrusted input. Content passes through file
decoding, text extraction or OCR, an optional LLM, deterministic schema and
business validation, and optionally a human review queue. Text in a document,
PDF metadata, hidden layers and apparent JSON are data, never instructions.
The model can still follow hostile text, so its output is not trusted merely
because it matches a Pydantic schema.

## Library guarantees

- Input byte size, PDF page count, and decoded image or rendered-page pixels
  are bounded before expensive processing. Decoder failures become structured
  `DocumentResult.error` values rather than escaping from the pipeline.
- Extracted totals, checksums and citations are checked deterministically.
  Unsupported values, invalid identifiers, missing source quotes and broken
  arithmetic cannot produce `status="succeeded"`.
- E-invoice XML disables entity resolution and network access and rejects every
  `DOCTYPE`; external entities and entity-expansion payloads are not evaluated.
- Documents and extracted values are not logged. Operational logs contain file
  names, stage names, statuses, timings and aggregate counters.
- The Python API does not persist results unless review storage or another
  output path is explicitly enabled.

These controls reduce risk; they do not make document content trustworthy or
turn LLM output into authoritative financial, legal or identity data.

## Integrator responsibilities

Run document processing with OS-level CPU, memory and wall-clock limits suited
to the deployment. Isolate OCR and PDF native libraries when processing hostile
multi-tenant uploads. Apply authentication, authorization, rate limits, malware
scanning and retention rules around the HTTP service and its job directory.
Treat `needs_review` and `failed` as non-approved outcomes, and decide which
fields require human approval even after deterministic checks pass.

Keep Docket and its native dependencies patched. Restrict outbound network
access when local models are used. Configure review and job storage explicitly,
encrypt it where required, and use `pii_fields()` to drive redaction or access
controls. Never render untrusted extracted strings as HTML without escaping.

## Reporting a vulnerability

Do not publish exploit details in a public issue. Use GitHub's private security
advisory flow for this repository and include the affected version, a minimal
reproducer, impact, and any suggested mitigation. Maintainers will acknowledge
the report, coordinate a fix and disclosure, and credit the reporter when
requested.
