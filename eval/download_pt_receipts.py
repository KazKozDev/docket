"""Real Portuguese receipts: Francisco-Cruz/InvoicesReceiptsPT (Apache-2.0).

    python eval/download_pt_receipts.py [--n 50]                          # dev set
    python eval/download_pt_receipts.py --offset 10 --out eval/pt_receipts_test   # held-out set

1003 phone photos of Portuguese till documents with hand-typed fields. The
number prefixes show what they are: FS (fatura simplificada, a till
receipt), FT and FR (fatura, fatura-recibo) printed at the till of a shop,
restaurant or petrol station for a customer who gave their NIF. docket
files a till slip under `receipt` whatever its header says, so every
document is labelled `receipt`.

Every (1003 // n)-th document is taken, starting at --offset, so a sample
is fixed and spread over the whole set; offsets 0 and 10 give two disjoint
sets, one to work on and one kept unseen to test the result. The dataset's fields map onto the receipt schema:

    company -> merchant_name        nif_seller     -> merchant_tax_id
    date    -> transaction_date     invoice_number -> receipt_number
    total   -> total_amount         iva_amount     -> tax_amount

Empty fields are not graded, and neither is a date the label cut short.
The labels are lower-cased and tokenised ("FS A / 42402"); the grading
ignores case and punctuation. Written to
eval/pt_receipts/ as pt_<name>.jpg + pt_<name>.expected.json.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "pt_receipts"
REPO = "Francisco-Cruz/InvoicesReceiptsPT"
RAW = f"https://huggingface.co/datasets/{REPO}/resolve/main/"


def _get(path: str) -> bytes:
    request = urllib.request.Request(RAW + urllib.parse.quote(path), headers={"User-Agent": "docket-eval"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _amount(raw: str) -> float | None:
    text = raw.strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _date(raw: str) -> str | None:
    """The labels write dates as the slip did (05/11/20, 17-03-2019,
    30.01.19); Portugal writes the day first. A label cut short (2019-02-2)
    is not graded rather than graded against a wrong day."""
    text = raw.strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    dmy = re.fullmatch(r"(\d{2})[/.\-](\d{2})[/.\-](\d{4}|\d{2})", text)
    try:
        if iso:
            return date(int(iso[1]), int(iso[2]), int(iso[3])).isoformat()
        if dmy:
            year = int(dmy[3]) + (2000 if len(dmy[3]) == 2 else 0)
            return date(year, int(dmy[2]), int(dmy[1])).isoformat()
    except ValueError:
        return None
    return None


def expected_from(annotation: dict) -> dict:
    fields = {
        "merchant_name": annotation.get("company", "").strip() or None,
        "merchant_tax_id": annotation.get("nif_seller", "").strip() or None,
        "transaction_date": _date(annotation.get("date", "")),
        "receipt_number": annotation.get("invoice_number", "").strip() or None,
        "total_amount": _amount(annotation.get("total", "")),
        "tax_amount": _amount(annotation.get("iva_amount", "")),
    }
    return {"doc_type": "receipt", **{k: v for k, v in fields.items() if v is not None},
            "_source": f"{REPO} (Apache-2.0), hand-typed fields"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    out = args.out

    from huggingface_hub import HfApi

    names = sorted(p.split("/")[-1][: -len(".txt")] for p in HfApi().list_repo_files(REPO, repo_type="dataset")
                   if p.startswith("2_Annotations_Json/") and p.endswith(".txt"))
    step = max(1, len(names) // args.n)
    out.mkdir(parents=True, exist_ok=True)
    for name in names[args.offset::step][: args.n]:
        annotation = json.loads(_get(f"2_Annotations_Json/{name}.txt"))
        (out / f"pt_{name}.jpg").write_bytes(_get(f"1_Images/{name}.jpg"))
        (out / f"pt_{name}.expected.json").write_text(
            json.dumps(expected_from(annotation), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(list(out.glob('*.jpg')))} receipts to {out}")


if __name__ == "__main__":
    main()
