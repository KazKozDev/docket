# Benchmarks

What docket's own evaluation shows, and where each number comes from.
Two sets were measured:

- **The golden set** (`eval/golden_dataset`): 21 labeled documents, 14 of
  them scans. It is made for this project.
- **European sample invoices from the ZUGFeRD corpus**: 28 German and
  French invoices and credit notes. They are published samples with
  invented parties, not real business mail.
- **Real Portuguese receipts**: 50 phone photos of till documents, with
  hand-typed fields.

Both sets are small, so treat these numbers as regression checks, not
accuracy claims about your documents. The raw results are committed in
`eval/results/`.

Setup: `deepseek-v4.1-flash:cloud` through Ollama for text and vision,
Tesseract 5.5.3, PaddleOCR 3.7.0, `DOCKET_OCR_LANGUAGES=en,de,es,fr`,
macOS arm64. The runs used the working tree of the commit that adds this
file; the result files record its parent, `cd1bd0a`, as `git_commit`.

## Do the checks catch a wrong value?

`python eval/benchmark_checks.py` — no LLM, no OCR, deterministic.

Each golden document with ground-truth text is rebuilt from its labeled
fields, with each field citing the line that prints it, and passed to
`docket.verify()`. Then one mistake at a time is planted, keeping the
original citation. A mistake counts as caught when the document goes to
review.

| planted mistake | caught |
|---|---|
| one digit of the total changed | 16/16 |
| subtotal and total raised together (arithmetic still adds up) | 13/13 |
| wrong tax amount | 13/13 |
| day and month swapped | 9/9 |
| document number changed | 12/12 |
| total cited from a line that is not on the page | 16/16 |
| another company's name | 16/16 |
| IBAN with a wrong check digit | 2/2 |

Clean documents that passed: 18/18, so no false alarms.

The first run caught 60/97. The misses were party names (never checked),
dates written with a month name, day/month swaps in an ambiguous numeric
date, and contract, purchase-order and bank-statement amounts that were
never compared with their cited line. These were fixed and the same set was
run again, so 97/97 shows that those gaps are closed. It is not an
independent estimate: the mistakes are ones this benchmark plants, and a
model can make others.

## Full pipeline

`python eval/benchmark_ocr.py --configs tesseract --pipeline-only --dataset eval/golden_dataset`
covers the 14 scans; `python eval/run_eval.py` covers the 7 text and
text-layer documents. The default chain was used: Tesseract, with the
vision model as fallback.

| | scans | text |
|---|---|---|
| documents | 14 | 7 |
| every graded field right | 12 | 7 |
| field accuracy | 0.983 | 1.00 |
| sent to review | 1 | 1 |
| wrong but passed without review | 2 of 13 | 0 of 6 |
| pages read by the vision model | 2 of 15 | — |
| median seconds per document | 7.2 | 3.6 (mean) |

- **Scans sent to review:** 1. The purchase order was correct, but its
  classification confidence was 0.46.
- **Text documents sent to review:** 1. `invoice_bad_total`, whose planted
  total error was caught.
- **Wrong but not sent to review:** two scans.
  - The market receipt: `card_last_four` was missing. It was left out, not
    misread, and no check can see an omission.
  - The waybill: Tesseract read the carrier's "Sp. z o.o." as "Sp. z 0.0.",
    and the model copied it faithfully. The cited line shows the same
    misreading, so the checks agree with it.
- **Citations:** 0.98 of fields and 0.91 of line-item rows cite a line
  found on the page.
- **Vendor templates:** they read 2 of the 14 scans. On one of them,
  `invoice_es_lowres`, the template read no line items (0/3).

## European sample invoices (ZUGFeRD corpus)

`python eval/download_zugferd.py` fetches the valid ZUGFeRD 2 / Factur-X
PDFs of [ZUGFeRD/corpus](https://github.com/ZUGFeRD/corpus) (Apache-2.0).
Each PDF embeds its invoice as EN 16931 XML, and the expected values are
read from that XML, not labelled by hand: the number, dates, parties,
seller VAT ID, IBAN, net, tax and gross totals, and the line items. The
corpus repeats the same samples across ZUGFeRD versions. After duplicates
are removed, 28 documents remain.

Labelling choices:

- Salutation and identifier lines in the XML name elements are dropped:
  "Herrn", "GLN 4333741000005", "Lief-Nr: …".
- Party names are compared without their legal form: "Au bon moulin"
  matches "Au bon moulin SARL".
- A corrected invoice (type 384, "Rechnungskorrektur") is labelled
  `credit_note`, which is where docket files corrections.

Each document ran twice: as the PDF with its text layer (no OCR), and as
the same pages rendered to 200 dpi images (Tesseract, vision fallback).
The numbers below come from `eval/replay_checks.py`. It re-runs the
deterministic checks and the grading over the saved extractions, without
the model. That picked up two late changes: contact persons are no longer
checked as party names, and IBANs are labelled without print spacing.

| | PDF text layer | rendered scans |
|---|---|---|
| document type right | 28/28 | 28/28 |
| every graded field right | 19/28 | 22/28 |
| field accuracy | 0.952 | 0.967 |
| sent to review | 19 | 20 |
| of those, only for an invalid sample VAT ID or IBAN | 12 | 14 |
| wrong but passed without review | 3 of 9 | 2 of 8 |
| pages read by the vision model | 0 | 34 of 66 |
| median seconds per document | 5.4 | 33.6 |

- **Why so many go to review:** the samples use invented VAT IDs and
  IBANs (`DE123456789`), which fail their checksums. docket is right to
  send these to review, but it means the review rate here says little about
  real invoices.
- **Wrong but passed without review:**
  - On two Mustang samples, the buyer was taken from the seller's contact
    column ("Ingmar N. Fo" for "Theodor Est"). The name is printed on the
    page, so no check can object.
  - One French sample had its due date left out.
- **The vision model on scans:** it read about half of the rendered pages,
  because docket judged the Tesseract text unusable. That is why scans are
  slower.

The first run of this set found problems in docket, which were fixed
before the numbers above:

- A line's VAT rate was never matched against the percentage printed on
  its row.
- Rows with a line discount, a gross price, a negative credit line, or a
  quantity glued to its unit ("5Unit(s)") failed the row arithmetic.
- A "$" in a German text layer (a rendered "§") made dotted dates read
  month-first.
- German compounds ("Handelsrechnung", "Warenrechnung") scored nothing for
  invoice, while the order they quote scored for purchase order.
- A self-billed "Gutschrift" was taken for a credit note.

On the first run, 5 of 29 documents had the wrong type.

The export benchmark on the PDF run's results (`eval/benchmark_export.py
eval/results/zugferd_pdf_results`):

- **Refused:** the 21 documents in review. Export does not write
  unchecked data.
- **Valid:** 6 of the 7 others in UBL, Factur-X EN16931 and BASIC.
- **Invalid in UBL and Factur-X:** a French overseas invoice with no buyer
  country code (BR-11).
- **Peppol and XRechnung:** all 7 are invalid, because they lack the
  electronic addresses and buyer reference.

## Real Portuguese receipts

`python eval/download_pt_receipts.py --n 50` takes every 20th document of
[Francisco-Cruz/InvoicesReceiptsPT](https://huggingface.co/datasets/Francisco-Cruz/InvoicesReceiptsPT)
(Apache-2.0). These are phone photos of Portuguese till documents from
shops, restaurants, petrol stations and toll roads, with hand-typed seller
name, seller NIF, date, number, total and VAT. They are labelled `receipt`,
which is docket's own type for a till slip. One run, Tesseract with vision
fallback, `DOCKET_OCR_LANGUAGES` including `pt`.

| | |
|---|---|
| classified as receipt | 11/50 |
| classified as invoice | 39/50 |
| sent to review | 42 |
| passed without review | 8, of which 7 have a graded field wrong |
| total right, on the 11 read as receipts | 11/11 |
| pages read by the vision model | 42 of 50 |
| median seconds per document | 18.5 |

docket does not handle these well:

- **The type is wrong on most of them.** The slips print "Fatura" or
  "Fatura simplificada", and the rules tier scores that as invoice. The
  invoice checks then treat prices that already include VAT as net. That
  produces "subtotal + tax ≠ total" and "lines do not add up", which sends
  42 documents to review. This is the problem the Malaysian "TAX INVOICE"
  slips had, and it is not fixed for Portuguese.
- **Of the 8 that passed without review:**
  - One is a correct extraction, filed as invoice. The grade counts it
    wrong only because of the type.
  - One lacks its receipt number.
  - The rest carry misread photo text into a field: a garbled merchant
    name, or a wrong or cut-off NIF ("NIPC 302 790"). The cited line holds
    the same misreading, so the checks agree with it.
- **A Portuguese NIF has a check digit**, which would catch part of the
  wrong NIFs. See "After the fixes" below.
- **Labels:** the labels name the shop sign ("Maria da Glória Cab."), where
  the slip also prints the legal name ("… Cabeleireiros Lda."). An
  extraction of the legal name counts as wrong.

### After the fixes

Changes made:

- An invoice title alone ("Fatura", "Invoice", "Rechnung") no longer lets
  the rules tier call a document an invoice. The rules also need something
  only an invoice has: an IBAN, payment terms, a billed address or a date
  of supply. Otherwise the decision goes to a tier that reads the whole
  document.
- Receipts gained an optional `merchant_country`. With it, a bare tax
  number is checked against its country's check digit in countries where
  the tax number is the VAT number (PT, BE, DK, ES, IT, PL).
- A party name containing OCR debris ("»", "¦", a colon inside a word)
  goes to review.

The same 50 receipts were run once more. The grading was then replayed
offline (`eval/replay_checks.py`) with the labels' dates normalised to ISO:
the first run's labels kept the slips' own date formats.

| | before | after |
|---|---|---|
| classified as receipt | 11/50 | 49/50 |
| total right | 11/50 | 47/50 |
| sent to review | 42 | 31 |
| passed without review | 8, 7 wrong | 19, 14 wrong |

Most of the 14 are merchant names, 10 of them. Many of those are the
labels' shop sign against the legal name docket reads, so the count
overstates the errors. The rest are wrong NIFs (5) and receipt numbers (4).

The NIF check needs `merchant_country`, which these results predate: the
model had no such field when they were extracted. How much it catches is
therefore not measured yet.

A held-out set of 50 other receipts (`--offset 10`) was stopped after 33
documents to save model quota. Of those 33, 2 were classified as invoices,
in line with the working set. That suggests the classification fix is not
tuned to the first 50.

## Classification

`python eval/benchmark_methods.py --dataset eval/golden_dataset` classifies
the 6 documents that ship their text with each tier separately: rules 6/6,
TF-IDF 5/6, LLM 6/6. Six documents are too few to rank the tiers. In the
full pipeline runs above, every one of the 21 documents was classified
correctly.

## OCR engines

`python eval/benchmark_ocr.py --ocr-only --dataset eval/golden_dataset`
measures OCR only, with no fallback and no LLM, on the 13 scans that have
text ground truth.

| engine | word F1 | table cells | median s |
|---|---|---|---|
| Tesseract | 0.960 | 0.57 | 3.0 |
| Paddle mobile | 0.996 | 0.71 | 7.8 |
| Paddle medium | 0.991 | 0.71 | 20.0 |

## E-invoice export

`python eval/benchmark_export.py eval/results/golden_results` takes the
invoices and credit notes read in the pipeline run, exports each to every
EN 16931 format, and checks the output with the official XSD and
Schematron rules. It needs no LLM.

| format | valid | invalid | refused |
|---|---|---|---|
| UBL (EN 16931) | 4 | 3 | 1 |
| Factur-X EN16931 | 4 | 3 | 1 |
| Factur-X BASIC | 4 | 3 | 1 |
| Peppol BIS 3.0 | 0 | 7 | 1 |
| XRechnung UBL | 0 | 7 | 1 |
| XRechnung CII | 0 | 7 | 1 |

All four credit notes are valid in UBL and Factur-X. Everything else fails
because data is missing, not because the exporter writes it wrongly:

- **Peppol and XRechnung:** they require electronic addresses for both
  parties. XRechnung also requires a buyer reference (Leitweg-ID) and a
  seller contact. A paper invoice does not carry these. The export leaves
  them out, and the validator names the missing business terms so the
  embedding application can supply them.
- **`invoice_de`:** its vendor template reads no addresses (BR-08, BR-10).
- **US and Australian invoices:** they have no EU VAT identifier (BR-S-02,
  BR-CO-26).
- **Refused:** `invoice_es_lowres`, which has no line items (BR-16). The
  export refuses it instead of writing it.

The first run had 0 of 48 valid because of exporter bugs, now fixed. A VAT
number that the model labelled `tax_id` was not sent as the VAT
identifier. A credit-transfer payment code was written with no account to
pay into.

## Not covered

- The real-document sets from `eval/download_real_samples.py` (DocILE,
  donut invoices, CORD, FUNSD, RVL-CDIP, CUAD).
- Real European invoices. The ZUGFeRD samples above are published examples,
  not supplier mail.
- The competitor comparison (`eval/benchmark_competitors.py`).
- Run-to-run variance (`eval/benchmark_variance.py`).

None of these were run for this report.
