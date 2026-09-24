# Benchmarks

Measurements behind the accuracy and limitation claims in the README. Every
number is produced by a script in `eval/`; per-document JSON lands in
`eval/results/`.

```bash
python eval/run_eval.py               # accuracy, P/R/F1, latency on the golden set
python eval/benchmark_methods.py      # rules vs TF-IDF vs LLM comparison (incl. confidence)
python eval/benchmark_ocr.py          # Tesseract vs Paddle (OCR-only + full pipeline) on the scans
python eval/benchmark_variance.py     # extraction stability: same document 10 times
python eval/benchmark_competitors.py  # docket against pip-installable alternatives
```

A full pipeline run takes one to two hours. `--checkpoint run.jsonl` writes
each finished document as it goes; the same command resumes an interrupted
run where it stopped. After a new docket run, `--rejoin` recomputes docket's
side of the saved competitor reports without running the competitors again:

```bash
python eval/benchmark_ocr.py --configs tesseract --pipeline-only --checkpoint eval/results/extended.jsonl --out eval/results/extended_stage2.json
python eval/benchmark_competitors.py --rejoin eval/results/competitors_*.json
```

The scripts use docket as a library, so they read settings from the
environment only, not from `.env`: export `DOCKET_TEXT_MODEL` and
`DOCKET_VISION_MODEL` (the published numbers use `deepseek-v4.1-flash:cloud`
for both) and `DOCKET_OCR_LANGUAGES=en,de,es,fr`. Each result file records
the models it ran with.

## OCR backends and field accuracy

Measured on the 33 labeled scans (golden + real samples; JSON with every
document in `eval/results/`):

| OCR backend          | word F1 | table cells | docs ok | fields | items F1 | median s |
|----------------------|---------|-------------|---------|--------|----------|----------|
| Tesseract            | 0.905   | 0.620       | 22/33   | 0.916  | 0.989    | 6.4      |
| Paddle (mobile)      | 0.994   | 0.897       | 27/33   | 0.927  | 0.989    | 10.6     |
| Paddle (medium)      | 0.986   | 0.839       | 27/33   | 0.927  | 0.989    | 22.7     |

Word F1 / table cells are OCR-only (16 golden scans with text and table
truth); docs ok counts correct classification plus every graded field right;
"median s" is the full pipeline per document. Extraction is deterministic at
temperature 0: 10 runs of the coupon receipt produce 15/15 identical fields.
Dropping the `[TABLE]` / `[COLUMN]` serialization markers changes nothing
measurable (identical outcomes for Paddle, ±2 marginal scans for Tesseract) —
the gain of layout serialization is for hard tables, not this set.

Citation coverage, measured on the 17 golden scans (tesseract,
`eval/benchmark_ocr.py --dataset eval/golden_dataset`): every line-item row
now carries a citation that validates (1.00 cited / 0.98 located on the page);
top-level fields 0.97 cited / 0.92 located. Before stage 1 the items were at
0.08 / 0.06 and fields at 0.92 / 0.87. One document stays in review by
design (purchase order); two receipt scans still extract wrong values from
garbled Tesseract text without triggering review — the false-success metric
that stage 2 measures.

## Credit notes

The credit note is the one experimental schema, kept because EN 16931 exports
it. There are no real credit notes with field labels in the corpus, so it is
measured on four synthetic golden scans: French (AVOIR), German (GUTSCHRIFT),
Spanish (FACTURA RECTIFICATIVA) and the official EN 16931 UBL example
(`ubl-tc434-creditnote1.xml`) printed as a page. Two runs (tesseract config,
`deepseek-v4.1-flash:cloud`) gave the same result: type right 4/4, fields
34/35. The one miss is the French seller name, which passed without review.
It stays experimental until it is measured on real documents.

## Extended corpus

**DocILE is filtered to invoices.** The DocILE mirror mixes invoices with
purchase orders, broadcast contracts, proposals and remittance advices, and
carries no document type; labelling all of them `invoice` counted docket
wrong for reading "PURCHASE ORDER" or "NETWORK SPOT CONTRACT" correctly. Since
September 2026 `download_real_samples.py` keeps a DocILE document only if plain
Tesseract finds INVOICE / BILL / BILLING in the upper third of the original
page image, a fixed rule applied before docket sees the document. Of the
first 28 rows it kept 15 and skipped 13. Results before and after this
change are not directly comparable on the DocILE rows.

Downloaded with `eval/download_real_samples.py --n` per source, then the
same benchmark: 198 scans — the golden set plus real documents
from Hugging Face (DocILE, donut-style invoices, SROIE receipts, CORD,
FUNSD, RVL-CDIP), with field-level ground truth where the source dataset
carries it. Tesseract config, same code:

| metric | golden only | extended corpus |
|---|---|---|
| documents | 17 | 198 |
| field accuracy | 0.97 | 0.72 |
| documents in review | 1 (6%) | 143 (72%) |
| false successes (silent wrong answers) | 3/16 (19%) | 19/55 (35%) |

Real scans are the honest test, and classification is the bottleneck:
SROIE "receipts" are Malaysian tax-invoice till slips — 39/120 still classify
as `tax_invoice`; where docket classifies right, field accuracy is
0.84–0.97 by source. The remaining false-success drivers: merchant
names and totals on degraded thermal receipts, and seller name /
invoice number on DocILE scans. (Before stage 3 this table read 0.53 / 23
of 50 false successes; the two measured fixes are in the CHANGELOG.)

## Against the pip-installable competition

`eval/benchmark_competitors.py` runs the same documents, graded with the same field metric on the intersection of
each tool's schema with the ground truth. docket's column is recomputed on
exactly those docs and fields; the LLM-backed tools ran against the same
Ollama daemon and model; they are handed the correct schema, while docket
must classify its way there; a tool's error counts as every graded field
wrong.

| tool | docs | field accuracy | docket, same docs+fields |
|---|---|---|---|
| docket, full comparable set | 179 | 0.72 | — |
| docpick 0.1.3 | 55 \* | 0.61 | 0.71 |
| ocrcontext 0.1.5 | 55 \* | 0.04 (16 parse errors) | 0.69 |
| invoice2data 1.0.1 | 44 | 0.00 (0 built-in template matches) | 0.90 |

\* evenly-spaced subsample — these tools read 60–80 s per document against
docket's 12 s mean. Per source, docket vs the best competitor: golden
1.00 vs 0.62 (docpick), donut 0.97 vs 0.48 (docpick), docile 0.50 vs 0.50
(ocrcontext), SROIE 0.55 vs 0.75 (docpick — the classification margin
above; on the 72 SROIE docs docket does classify as receipts, its fields
are 0.84).

## Vendor templates

On the 198-scan extended corpus, the two fictional vendor templates that ship
as examples match and pass validation on exactly their two documents (2/198, no false template
matches); the intentionally small hit rate is not presented as generic vendor
coverage.
