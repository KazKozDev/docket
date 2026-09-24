# Benchmarks

Measurements behind the accuracy and limitation claims in the README. Every
number is produced by a script in `eval/`; per-document JSON lands in
`eval/results/`.

The headline metric is the **false-success rate**: of the documents docket
returns as `succeeded` with nothing for review, the share with at least one
wrong field (`false_success_rate` in `benchmark_ocr.py` output). A document
sent to review costs a person a minute; a wrong one that succeeds reaches the
books unseen. Field accuracy matters, but only after this number is near zero.
Latest full run: 13/66 (20%) — and 3/24 (13%) excluding the SROIE Malaysian
receipts, which the checks are not tuned for.

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
same benchmark: the golden set plus real documents from Hugging Face
(DocILE, donut-style invoices, SROIE receipts, CORD, FUNSD, RVL-CDIP), with
field-level ground truth where the source dataset carries it. Tesseract
config, `deepseek-v4.1-flash:cloud` for text and vision.

Latest full run: commit `68f8eea` (docket 0.4.0 plus the checks of #22–#30
and the DocILE filter above; the benchmark records each document's dataset
source and key-field OCR confidences there). The first two columns cover the
same 195 documents; the `68f8eea` column runs on the current corpus — three
schemas removed in #30 and DocILE filtered to invoices — so the columns are
close but not exactly comparable.

| metric | 0.3.0 | `de96112` | `68f8eea` |
|---|---|---|---|
| false successes (silent wrong answers) | 19/52 (37%) | 27/59 (46%) | **13/66 (20%)** |
| field accuracy | 0.71 | 0.85 | **0.88** |
| documents fully right | 53% | 68% | **73%** |
| document type right | 139 | 176 | **183** |
| documents in review | 143 | 136 | **129** (133 at the 0.80 default) |
| median seconds per document | 7.1 | 8.7 | **7.7** |

By source (`de96112`): golden 0.96, donut invoices 0.98, SROIE receipts
0.78 (0.49 before; 110/120 now classified as receipts, 72 before), DocILE
0.43. By source (`68f8eea`): golden 0.96, donut invoices 0.99, SROIE
receipts 0.80, DocILE (invoices only) 0.71.

Almost all of the gain is receipt classification. A TAX INVOICE header on
Malaysian till slips no longer files them under a separate `tax_invoice`
type (#19). The rise in silent wrong answers comes from the same place:
receipts that used to be misfiled and sent to review are now filed
correctly, and a few of their fields are still misread on degraded thermal
paper. The date check (#22) and payment check (#23) were added against
exactly those cases, and the `68f8eea` run measures all four checks
together: a key field read from OCR words below `DOCKET_MIN_SOURCE_CONFIDENCE`
sent 57 documents to review, 17 of them actually wrong (30% precision); a
document number must be printed on the line it cites, and that check flagged
no document on this corpus. `DOCKET_MIN_SOURCE_CONFIDENCE` was chosen
from the same run by re-scoring the per-document key-field confidences it
recorded (`eval/analyze_pr30.py`):

| threshold | false successes | documents in review |
|---|---|---|
| 0.50 | 13/66 (20%) | 107/195 (55%) |
| 0.60 | 13/66 (20%) | 114/195 (59%) |
| 0.70 | 13/66 (20%) | 124/195 (64%) |
| 0.75 (previous default) | 13/66 (20%) | 129/195 (66%) |
| **0.80 (new default)** | **12/62 (19%)** | 133/195 (68%) |
| 0.85 | 11/53 (21%) | 142/195 (73%) |
| 0.90 | 9/44 (21%) | 151/195 (77%) |

0.80 is the sweep's minimum, though only by one document, so treat it as a
reasonable setting rather than a calibrated one: every threshold at or below 0.75 leaves the
same 13 false successes, and above 0.80 the silent set loses correct
documents faster than wrong ones, so the rate rises while reviews grow.
Most "misclassified invoices" on DocILE were purchase orders, contracts and
proposals labelled `invoice` by our downloader, which is why DocILE is now
filtered.

## Against the pip-installable competition

`eval/benchmark_competitors.py` runs the same documents, graded with the same
field metric on the intersection of each tool's schema with the ground
truth. docket's column is recomputed on exactly those docs and fields with
`--rejoin`; the LLM-backed tools ran against the same Ollama daemon and
model; they are handed the correct schema, while docket must classify its
way there; a tool's error counts as every graded field wrong. The
competitors were run once (pinned versions); docket's side is from
`de96112`.

| tool | docs | tool's field accuracy | docket, same docs and fields |
|---|---|---|---|
| docpick 0.1.3 | 55 \* | 0.61 | **0.85** (0.71 on 0.3.0) |
| ocrcontext 0.1.5 | 55 \* | 0.04 (16 parse errors) | **0.83** (0.69) |
| invoice2data 1.0.1 | 44 | 0.00 (0 built-in template matches) | **0.89** (0.90) |

\* evenly spaced subsample: these tools take 60–80 s per document.

## Vendor templates

On the 198-scan extended corpus, the two fictional vendor templates that ship
as examples match and pass validation on exactly their two documents (2/198, no false template
matches); the intentionally small hit rate is not presented as generic vendor
coverage.
