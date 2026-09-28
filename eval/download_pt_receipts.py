"""Real Portuguese receipts: Francisco-Cruz/InvoicesReceiptsPT (Apache-2.0).

    python eval/download_pt_receipts.py [--n 50]

1003 phone photos of Portuguese till documents with hand-typed fields. The
number prefixes show what they are: FS (fatura simplificada, a till
receipt), FT and FR (fatura, fatura-recibo) printed at the till of a shop,
restaurant or petrol station for a customer who gave their NIF. docket
files a till slip under `receipt` whatever its header says, so every
document is labelled `receipt`.

Every (1003 // n)-th document is taken, so the sample is fixed and spread
over the whole set. The dataset's fields map onto the receipt schema:

    company -> merchant_name        nif_seller     -> merchant_tax_id
    date    -> transaction_date     invoice_number -> receipt_number
    total   -> total_amount         iva_amount     -> tax_amount

Empty fields are not graded. The labels are lower-cased and tokenised
("FS A / 42402"); the grading ignores case and punctuation. Written to
eval/pt_receipts/ as pt_<name>.jpg + pt_<name>.expected.json.
"""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
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


def expected_from(annotation: dict) -> dict:
    fields = {
        "merchant_name": annotation.get("company", "").strip() or None,
        "merchant_tax_id": annotation.get("nif_seller", "").strip() or None,
        "transaction_date": annotation.get("date", "").strip() or None,
        "receipt_number": annotation.get("invoice_number", "").strip() or None,
        "total_amount": _amount(annotation.get("total", "")),
        "tax_amount": _amount(annotation.get("iva_amount", "")),
    }
    return {"doc_type": "receipt", **{k: v for k, v in fields.items() if v is not None},
            "_source": f"{REPO} (Apache-2.0), hand-typed fields"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=50)
    args = parser.parse_args()

    from huggingface_hub import HfApi

    names = sorted(p.split("/")[-1][: -len(".txt")] for p in HfApi().list_repo_files(REPO, repo_type="dataset")
                   if p.startswith("2_Annotations_Json/") and p.endswith(".txt"))
    step = max(1, len(names) // args.n)
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names[::step][: args.n]:
        annotation = json.loads(_get(f"2_Annotations_Json/{name}.txt"))
        (OUT / f"pt_{name}.jpg").write_bytes(_get(f"1_Images/{name}.jpg"))
        (OUT / f"pt_{name}.expected.json").write_text(
            json.dumps(expected_from(annotation), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(list(OUT.glob('*.jpg')))} receipts to {OUT}")


if __name__ == "__main__":
    main()
