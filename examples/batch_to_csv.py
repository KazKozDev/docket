"""Process a directory of documents into a summary CSV and a line-item CSV.

    python examples/batch_to_csv.py invoices/ results.csv

The same from the shell:

    docket batch invoices/ --recursive --format csv --output results.csv --workers 4

Results stream to the CSVs as documents finish (in input order), and every
finished result is also appended to results.checkpoint.jsonl — rerun the
same command after an interruption and finished documents are not processed
again. One document failing never stops the batch.
"""
import sys
from pathlib import Path

from docket import BatchOptions, ProcessOptions, ReviewOptions, process_batch
from docket.export import tabular

source, target = sys.argv[1], Path(sys.argv[2] if len(sys.argv) > 2 else "results.csv")
options = ProcessOptions(review=ReviewOptions(enqueue=False), include_layout=False)
batch_options = BatchOptions(recursive=True, workers=4, checkpoint=target.with_suffix(".checkpoint.jsonl"))

with target.open("w", newline="") as summary, target.with_suffix(".line_items.csv").open("w", newline="") as items:
    summary_csv = tabular.CsvWriter(summary, tabular.RESULT_COLUMNS)
    items_csv = tabular.CsvWriter(items, tabular.ITEM_COLUMNS)

    def write(_index, result):
        summary_csv.write(tabular.result_row(result))
        for row in tabular.line_item_rows(result):
            items_csv.write(row)

    batch = process_batch(source, options, batch_options, on_result=write)

print(f"{batch.total} documents: {batch.succeeded} ok, {batch.needs_review} to review, {batch.failed} failed")
for error in batch.errors:
    print(f"  {error.source}: [{error.code}] {error.message}")
