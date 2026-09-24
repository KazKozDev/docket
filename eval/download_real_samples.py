"""Pulls a handful of real, publicly-licensed documents from Hugging Face
into eval/real_samples/, with docket-schema expected.json files built from
each dataset's own ground truth — a reality check on top of the five
hand-written documents in eval/golden_dataset/.

Sources (see README for licenses/links):
  - katanaml-org/invoices-donut-data-v1  -> invoices (full field mapping)
  - mp-02/sroie (SROIE / ICDAR 2019)     -> receipts with field mapping
    (merchant, date, total reconstructed from the word-level NER tags)
  - naver-clova-ix/cord-v2               -> receipts (doc_type only —
    CORD's ground truth doesn't carry merchant name or a clean, unambiguous
    total, so field-level grading isn't attempted; see the note below)
  - dvgodoy/CUAD_v1_Contract_Understanding_PDF -> contracts (doc_type only —
    CUAD is labeled for clause extraction, not header fields)
  - nielsr/funsd (FUNSD)                 -> scanned forms, labeled
    doc_type "unknown" on purpose: none of docket's three schemas fit a
    generic form, so these test that the classifier declines to guess
    rather than forcing a form into the invoice bucket.
  - hf-tuner/rvl-cdip-document-classification (RVL-CDIP subsample) ->
    invoices (label "invoice") and a matched set of non-invoice business
    documents labeled "unknown", same out-of-distribution purpose as FUNSD.
  - Humayoun/DocILE100 (DocILE subsample) -> invoices only. DocILE mixes
    invoices with purchase orders, contracts, proposals and remittance
    advices, and the mirror carries no document type, so a document is
    kept only if plain Tesseract finds INVOICE / BILL / BILLING in the upper
    third of the page, where the title is. Vendor name, invoice number and
    date are graded.

    python eval/download_real_samples.py [--n 5] [--only sroie,funsd]

Requires the `datasets` package: `pip install -e ".[eval]"` — not a runtime
dependency of docket itself, so it's kept out of the base install.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from datasets import load_dataset

OUT_DIR = Path(__file__).parent / "real_samples"


def _parse_amount(raw: str | None) -> float | None:
    """'$ 889,20' -> 889.20, '1.234,56' -> 1234.56. Best-effort only."""
    if not raw:
        return None
    s = raw.replace("$", "").replace("€", "").strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _parse_date_mdy(raw: str | None) -> str | None:
    """'09/18/2015' -> '2015-09-18'. Returns None if it doesn't parse."""
    if not raw:
        return None
    try:
        m, d, y = raw.split("/")
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


# The katanaml invoices print the party name above a Faker-generated address,
# and the ground truth joins both into one string. Invoice 2.0 grades the name
# alone (seller.name), so it is cut where the address begins: a house number,
# "Unit N" / "PSC N", or a military vessel ("USS Kramer FPO AA 81651").
_ADDRESS_START = re.compile(r"\s(?=\d|(?:Unit|PSC) \d|(?:USNS|USNV|USS|USCGC) )")


def _party_name(block: str) -> str:
    return _ADDRESS_START.split(block.strip(), maxsplit=1)[0]


def download_invoices(n: int) -> None:
    print(f"katanaml-org/invoices-donut-data-v1 -> {n} invoice(s)")
    ds = load_dataset("katanaml-org/invoices-donut-data-v1", split="test", streaming=True)
    for i, row in enumerate(itertools.islice(ds, n)):
        gt = json.loads(row["ground_truth"])["gt_parse"]
        header = gt.get("header", {})
        summary = gt.get("summary", {})

        expected: dict = {"doc_type": "invoice"}
        if header.get("invoice_no"):
            expected["invoice_number"] = header["invoice_no"]
        if (d := _parse_date_mdy(header.get("invoice_date"))) is not None:
            expected["issue_date"] = d
        if header.get("seller"):
            expected["seller.name"] = _party_name(header["seller"])
            expected["_seller_block"] = header["seller"]
        if header.get("client"):
            expected["buyer.name"] = _party_name(header["client"])
            expected["_buyer_block"] = header["client"]
        if header.get("seller_tax_id"):
            expected["seller.tax_ids[0].value"] = header["seller_tax_id"]
        if (v := _parse_amount(summary.get("total_net_worth"))) is not None:
            expected["subtotal"] = v
        if (v := _parse_amount(summary.get("total_vat"))) is not None:
            expected["tax_amount"] = v
        if (v := _parse_amount(summary.get("total_gross_worth"))) is not None:
            expected["total_amount"] = v

        stem = f"invoice_hf_{i:02d}"
        row["image"].convert("RGB").save(OUT_DIR / f"{stem}.jpg", quality=90)
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2))


def download_receipts(n: int) -> None:
    print(f"naver-clova-ix/cord-v2 -> {n} receipt(s)")
    print(
        "  note: CORD's ground truth has no merchant name or unambiguous total "
        "(amounts are Indonesian Rupiah with '.' as a thousands separator, not "
        "a decimal point) — grading doc_type only, not extracted fields."
    )
    ds = load_dataset("naver-clova-ix/cord-v2", split="test", streaming=True)
    for i, row in enumerate(itertools.islice(ds, n)):
        stem = f"receipt_hf_{i:02d}"
        row["image"].convert("RGB").save(OUT_DIR / f"{stem}.jpg", quality=90)
        expected = {"doc_type": "receipt"}
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2))


def _parse_date_flex(raw: str | None) -> str | None:
    """SROIE dates come in whatever format the merchant's till printed."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in (
        "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d",
        "%d %b %Y", "%d %B %Y", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_amount(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"(\d[\d,]*\.\d{2})", raw)
    if not match:
        return None
    try:
        return round(float(match.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def download_sroie(n: int) -> None:
    print(f"mp-02/sroie (SROIE / ICDAR 2019) -> {n} receipt(s)")
    print(
        "  note: labels are word-level (S-COMPANY/S-DATE/S-ADDRESS/S-TOTAL). Words sharing a "
        "tag are concatenated to rebuild each field — best-effort, so a receipt with two "
        "TOTAL-tagged regions can produce a merged string that won't parse. Those fields are "
        "dropped rather than graded against a value we know is wrong."
    )
    ds = load_dataset("mp-02/sroie", split="test", streaming=True)
    label_names = ["S-COMPANY", "S-DATE", "S-ADDRESS", "S-TOTAL", "O"]

    for i, row in enumerate(itertools.islice(ds, n)):
        spans: dict[str, list[str]] = defaultdict(list)
        for word, tag in zip(row["words"], row["ner_tags"]):
            name = label_names[tag]
            if name == "O":
                continue
            spans[name.split("-", 1)[-1]].append(word)
        joined = {k: " ".join(v) for k, v in spans.items()}

        expected: dict = {"doc_type": "receipt"}
        if joined.get("COMPANY"):
            expected["merchant_name"] = joined["COMPANY"]
        if (d := _parse_date_flex(joined.get("DATE"))) is not None:
            expected["transaction_date"] = d
        if (v := _extract_amount(joined.get("TOTAL"))) is not None:
            expected["total_amount"] = v

        stem = f"receipt_sroie_{i:02d}"
        row["image"].convert("RGB").save(OUT_DIR / f"{stem}.jpg", quality=90)
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2))


def download_funsd(n: int) -> None:
    print(f"nielsr/funsd (FUNSD) -> {n} form(s), labeled doc_type=unknown on purpose")
    print(
        "  note: a generic form is none of docket's three types. These documents exist in the "
        "eval set to check that the classifier says 'unknown' instead of forcing a form into "
        "the nearest schema — a false invoice is worse here than an abstention."
    )
    ds = load_dataset("nielsr/funsd", split="test", streaming=True)
    for i, row in enumerate(itertools.islice(ds, n)):
        stem = f"form_funsd_{i:02d}"
        row["image"].convert("RGB").save(OUT_DIR / f"{stem}.png")
        (OUT_DIR / f"{stem}.expected.json").write_text(
            json.dumps({"doc_type": "unknown"}, indent=2)
        )


def download_rvl_cdip(n: int) -> None:
    print(f"hf-tuner/rvl-cdip-document-classification (RVL-CDIP subsample) -> {n} invoice(s) + {n} other(s)")
    print("  note: doc_type only — RVL-CDIP is a classification corpus with no field-level labels.")
    ds = load_dataset("hf-tuner/rvl-cdip-document-classification", split="train", streaming=True)
    label_names = ds.features["label"].names
    invoice_label = label_names.index("invoice")

    n_invoice = n_other = 0
    for row in ds:
        if n_invoice >= n and n_other >= n:
            break
        is_invoice = row["label"] == invoice_label
        if is_invoice and n_invoice < n:
            stem, doc_type, n_invoice = f"invoice_rvlcdip_{n_invoice:02d}", "invoice", n_invoice + 1
        elif not is_invoice and n_other < n:
            stem, doc_type, n_other = f"other_rvlcdip_{n_other:02d}", "unknown", n_other + 1
        else:
            continue
        row["image"].convert("RGB").save(OUT_DIR / f"{stem}.png")
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps({"doc_type": doc_type}, indent=2))


_BILL_TITLE_RE = re.compile(r"^(invoice|bill|billing|billed)\b", re.I)


def _titled_as_invoice(image) -> bool:
    """INVOICE / BILL / BILLING printed in the upper third of the page, read
    by plain Tesseract. A fixed rule of our own, applied before docket ever
    sees the document, so it cannot be tuned to docket's answers."""
    import pytesseract

    data = pytesseract.image_to_data(image, lang="eng", output_type=pytesseract.Output.DICT)
    return any(
        _BILL_TITLE_RE.match(word.strip(".:#*|")) and top < image.height / 3
        for word, top in zip(data["text"], data["top"])
    )


def download_docile(n: int) -> None:
    print(f"Humayoun/DocILE100 (DocILE subsample) -> {n} business document(s)")
    print(
        "  note: this mirror's 'text' column holds the ground-truth JSON, not the document "
        "text — the document itself is the image. Vendor name, invoice number and date map "
        "cleanly onto docket's Invoice schema; the line-item structure doesn't, so it's left "
        "ungraded rather than bent into a shape it doesn't fit."
    )
    ds = load_dataset("Humayoun/DocILE100", split="train", streaming=True)
    for stale in OUT_DIR.glob("invoice_docile_*"):
        stale.unlink()
    kept = 0
    for i, row in enumerate(ds):
        if kept == n:
            break
        image = row["image"].convert("RGB")
        if not _titled_as_invoice(image):
            print(f"  skipped DocILE row {i}: no INVOICE/BILL title")
            continue
        kept += 1
        expected: dict = {"doc_type": "invoice"}
        try:
            vendor = json.loads(row["text"]).get("Vendor Information", {})
        except (json.JSONDecodeError, TypeError, AttributeError):
            vendor = {}
        if vendor.get("vendorName"):
            expected["seller.name"] = vendor["vendorName"]
        if vendor.get("invoiceNumber"):
            expected["invoice_number"] = str(vendor["invoiceNumber"])
        if (d := _parse_date_flex(vendor.get("invoiceDate"))) is not None:
            expected["issue_date"] = d

        stem = f"invoice_docile_{i:02d}"  # the dataset row, so a document keeps its name
        image.save(OUT_DIR / f"{stem}.jpg", quality=90)
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2))


def download_contracts(n: int) -> None:
    print(f"dvgodoy/CUAD_v1_Contract_Understanding_PDF -> {n} contract(s)")
    print("  note: CUAD is labeled for clause extraction, not header fields — doc_type only.")
    ds = load_dataset(
        "dvgodoy/CUAD_v1_Contract_Understanding_PDF", split="train", streaming=True
    )
    for i, row in enumerate(itertools.islice(ds, n)):
        stem = f"contract_hf_{i:02d}"
        (OUT_DIR / f"{stem}.txt").write_text(row["text"])
        expected = {"doc_type": "contract"}
        (OUT_DIR / f"{stem}.expected.json").write_text(json.dumps(expected, indent=2))


_DOWNLOADERS = {
    "invoices": download_invoices,
    "sroie": download_sroie,
    "cord": download_receipts,
    "contracts": download_contracts,
    "funsd": download_funsd,
    "rvlcdip": download_rvl_cdip,
    "docile": download_docile,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=5, help="samples per dataset (default: 5)")
    parser.add_argument(
        "--only",
        default=None,
        help=f"comma-separated subset of: {', '.join(_DOWNLOADERS)} (default: all)",
    )
    args = parser.parse_args()

    selected = list(_DOWNLOADERS) if args.only is None else [s.strip() for s in args.only.split(",")]
    unknown = [s for s in selected if s not in _DOWNLOADERS]
    if unknown:
        parser.error(f"unknown source(s): {', '.join(unknown)}")

    OUT_DIR.mkdir(exist_ok=True)
    for name in selected:
        try:
            _DOWNLOADERS[name](args.n)
        except Exception as exc:  # noqa: BLE001
            # One dataset going away (moved, gated, mirror deleted) shouldn't
            # cost you the other six.
            print(f"  SKIPPED {name}: {type(exc).__name__}: {exc}")

    print(f"\nDone — documents in {OUT_DIR}/")
    print(f"Run: python eval/run_eval.py {OUT_DIR}")


if __name__ == "__main__":
    main()
