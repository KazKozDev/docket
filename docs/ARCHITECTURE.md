# Architecture

Docket converts unstructured or semi-structured documents (invoices, receipts, contracts, boarding passes) into validated, auditable JSON objects with line citations and human review escalation.

```
+---------------------------------------------------------------------------------------+
|                                    Document Ingestion                                  |
|   (PDF, PNG, JPG, TIFF, TXT)                                                          |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                                 Text Acquisition Tier                                 |
|   1. Direct PDF Text Layer (PyMuPDF / pdfplumber)                                     |
|   2. Local OCR (Tesseract) + Garbled Quality Gate                                     |
|   3. Vision-Language Model (Ollama VLM) on low OCR confidence / garbled scans         |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                                  Classification Tier                                  |
|   1. Fast Deterministic Keyword Rules (confidence 1.0)                                |
|   2. TF-IDF Classifier (scikit-learn, confidence floor threshold)                     |
|   3. LLM Zero-shot Classifier (fallback when confidence < floor)                      |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                              Structured Extraction Tier                               |
|   - Pydantic Schemas per document type (Invoice, Receipt, Contract, BoardingPass)     |
|   - JSON Schema contract enforcement via local Ollama LLM                             |
|   - Verbatim Source Citations (field_sources / field_locations)                       |
+---------------------------------------------------------------------------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                             Deterministic Validation Tier                             |
|   - Arithmetic verification (subtotal + tax + shipping - discount = total)           |
|   - Date logic & future bounds checks                                                 |
|   - IBAN mod-97 check digits (ISO 7064) across all European countries & Brazil        |
|   - VAT check digits (all 27 EU member states, GB, CH, NO)                            |
|   - National Tax ID checksums (US EIN, Canadian BN, Brazilian CNPJ/CPF)               |
|   - Verbatim citation existence & exact substring witness checks                      |
+---------------------------------------------------------------------------------------+
                                           |
                   +-----------------------+-----------------------+
                   | (Passed all checks)                           | (Validation Error / Low Conf)
                   v                                               v
+------------------------------------+           +------------------------------------+
|           Validated JSON           |           |         Human Review Queue         |
|  (Clean downstream persistence)    |           |  (data/review_queue.jsonl + UI)    |
+------------------------------------+           +------------------------------------+
```

---

## 1. Text Acquisition Tier

To minimize inference costs and latency, Docket selects the cheapest extraction method that yields reliable text:

1. **Digital PDF Text Layer**: Extracted directly using `pdfplumber` / `PyMuPDF`. Zero model overhead.
2. **Local Tesseract OCR**: Used when no text layer is present.
3. **OCR Quality Pre-flight (`looks_garbled`)**: Checks character distribution and token validity. If a scan is noisy or degraded, passing it to an extraction model wastes computation (garbled inputs take up to $2.5\times$ longer to process).
4. **VLM Transcription Fallback**: Triggered automatically when OCR confidence is low or text is garbled.

---

## 2. Classification Tier

Classification determines which Pydantic schema will govern extraction:

1. **Keyword Rules**: High-precision header and structural pattern matching. If a document matches definitive rules (e.g., unambiguous invoice headers or boarding pass markers), it classifies immediately with confidence `1.0`.
2. **TF-IDF Classifier**: Trained on standard document classes. If prediction confidence exceeds `DOCKET_TFIDF_CONFIDENCE_FLOOR`, it skips the LLM call entirely.
3. **LLM Fallback**: Invoked only when rule-based and TF-IDF classifiers cannot make a confident decision.

---

## 3. Extraction & Verbatim Citations

- **JSON Schema Contracts**: The target schema is defined as a Pydantic model (`Invoice`, `Receipt`, `Contract`, `BoardingPass`).
- **Verbatim Evidence**: The model must provide verbatim quotes (`quote`, `page`) for extracted values.
- **Multilingual Parsing**: Supports both European (`1.234,56 €`) and American (`$1,234.56`) numerical conventions.

---

## 4. Deterministic Validation

Validation never calls a model. It executes deterministic arithmetic and mathematical checksum algorithms:

- **Totals & Line Items**: Verifies `subtotal + tax + shipping - discount == total_amount` within floating-point tolerance ($0.05$).
- **IBAN**: ISO 7064 MOD 97-10 check digits for all European nations and Brazil. Identifies non-IBAN systems (US, Canada) and requests routing numbers instead.
- **VAT / Sales Tax**: Algorithmic check-digit verification across all 27 EU member states, the UK, Switzerland, and Norway.
- **Americas Tax IDs**: Modulo-11 CNPJ/CPF checks for Brazil, Luhn mod-10 checks for Canadian Business Numbers (BN), and prefix verification for US EINs.
- **Citation Grounding**: Asserts that every cited quote exists in the document and contains the claimed numerical value.

---

## 5. Human Review Queue

Documents that fail any error-level validation rule, fail extraction, or carry low classification confidence are routed to the Review Queue (`data/review_queue.jsonl`):

- Preserves original document artifacts, raw text, and audit trails.
- Accessible via CLI, FastAPI endpoints (`/review-queue`), and Streamlit web UI.
