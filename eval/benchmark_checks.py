"""Do docket's deterministic checks catch a wrong value? No LLM, no OCR.

    python eval/benchmark_checks.py

For every golden document with ground-truth text (the `.txt` documents and
the scans' `_text`), the graded fields are assembled into a schema instance,
each field cited by the source line that prints its value. That clean
document is passed to `docket.verify()` together with the source text.

Then one error at a time is planted in a copy — the kinds of mistakes an
extraction model makes — keeping the original citation, and `verify()` runs
again. A mutation is caught when the document goes to review or fails.

- false alarm: the clean document does not come back `succeeded`;
- catch rate: per mutation kind, over the documents where the clean copy
  passed and the mutation applies.

Results go to eval/results/benchmark_checks.json.
"""
from __future__ import annotations

import copy
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docket import DocumentStatus, verify

GOLDEN = ROOT / "eval" / "golden_dataset"
OUT = ROOT / "eval" / "results" / "benchmark_checks.json"

TOTALS = ("total_amount", "closing_balance", "contract_value")
NUMBERS = ("invoice_number", "credit_note_number", "po_number", "waybill_number")
DATES = ("issue_date", "transaction_date", "po_date", "waybill_date", "effective_date", "statement_period_start")
PATH_RE = re.compile(r"^(\w+)(?:\[(\d+)\])?$")


def source_text(expected_path: Path, expected: dict) -> str | None:
    if "_text" in expected:
        return "\n".join(expected["_text"])
    txt = expected_path.parent / (expected_path.name[: -len(".expected.json")] + ".txt")
    return txt.read_text(encoding="utf-8") if txt.exists() else None


def put(doc: dict, path: str, value) -> None:
    """Set a dotted path with list indices ("seller.tax_ids[0].value")."""
    parts = path.split(".")
    node = doc
    for i, part in enumerate(parts):
        name, index = PATH_RE.match(part).groups()
        last = i == len(parts) - 1
        if index is None:
            if last:
                node[name] = value
            else:
                node = node.setdefault(name, {})
            continue
        items = node.setdefault(name, [])
        idx = int(index)
        while len(items) <= idx:
            items.append(None if last else {})
        if last:
            items[idx] = value
        else:
            node = items[idx]


def renderings(value) -> list[str]:
    """Ways a value is printed on a page, most specific first."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        v = float(value)
        dot = f"{v:,.2f}"
        out = [dot, dot.replace(",", ""), dot.replace(",", " ").replace(".", ","),
               dot.replace(",", "X").replace(".", ",").replace("X", "."), f"{v:.2f}".replace(".", ",")]
        if v == int(v):
            out.append(str(int(v)))
        return list(dict.fromkeys(out))
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        d = date.fromisoformat(text)
        return [text, d.strftime("%d.%m.%Y"), d.strftime("%d/%m/%Y"), d.strftime("%m/%d/%Y"),
                d.strftime("%d-%m-%Y"), d.strftime("%B %-d, %Y"), d.strftime("%-d %B %Y"),
                d.strftime("%d %b %Y"), d.strftime("%b %d, %Y")]
    return [text]


def cite(value, lines: list[str]) -> str | None:
    for form in renderings(value):
        for line in lines:
            if form.lower() in line.lower():
                return line.strip()
    return None


def build(expected: dict, text: str) -> tuple[dict, list[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    doc: dict = {}
    locations: dict = {}
    uncited = []
    for key, value in expected.items():
        if key.startswith("_") or key == "doc_type":
            continue
        put(doc, key, value)
        values = value if isinstance(value, list) else [value]
        for i, v in enumerate(values):
            path = f"{key}[{i}]" if isinstance(value, list) else key
            quote = cite(v, lines)
            if quote:
                locations[path] = {"page": 1, "quote": quote}
            elif renderings(v):
                uncited.append(path)
    doc["field_locations"] = locations
    return doc, uncited


def get(doc: dict, path: str):
    node = doc
    for part in path.split("."):
        name, index = PATH_RE.match(part).groups()
        if not isinstance(node, dict) or name not in node:
            return None
        node = node[name]
        if index is not None:
            node = node[int(index)] if isinstance(node, list) and len(node) > int(index) else None
    return node


# ---- mutations: (doc) -> mutated doc, or None when it does not apply --------------------------

def _first(doc: dict, keys: tuple[str, ...]) -> str | None:
    return next((k for k in keys if isinstance(get(doc, k), (int, float)) and not isinstance(get(doc, k), bool)
                 or isinstance(get(doc, k), str) and get(doc, k)), None)


def digit_in_total(doc):
    """A misread digit in the grand total ("530.00" -> "830.00"), citation unchanged."""
    key = _first(doc, TOTALS)
    if key is None or not isinstance(get(doc, key), (int, float)):
        return None
    v = float(get(doc, key))
    s = f"{v:.2f}"
    i = next(i for i, c in enumerate(s) if c.isdigit() and c != "0") if any(c in "123456789" for c in s) else 0
    d = int(s[i])
    wrong = float(s[:i] + str((d + 3) % 10 or 1) + s[i + 1:])
    out = copy.deepcopy(doc)
    put(out, key, wrong)
    return out


def consistent_inflation(doc):
    """Subtotal, tax and total moved together so the arithmetic still adds up
    (what a vision model does when it 'reconciles'): only the page can tell."""
    if not all(isinstance(get(doc, k), (int, float)) for k in ("subtotal", "total_amount")):
        return None
    out = copy.deepcopy(doc)
    put(out, "subtotal", round(float(get(doc, "subtotal")) + 100, 2))
    put(out, "total_amount", round(float(get(doc, "total_amount")) + 100, 2))
    return out


def tax_off(doc):
    """Wrong tax amount; subtotal + tax no longer equals the total."""
    if not all(isinstance(get(doc, k), (int, float)) for k in ("subtotal", "tax_amount", "total_amount")):
        return None
    out = copy.deepcopy(doc)
    put(out, "tax_amount", round(float(get(doc, "tax_amount")) + 7.5, 2))
    return out


def day_month_swap(doc):
    """An ambiguous date read the other way round (2026-07-04 -> 2026-04-07)."""
    for key in DATES:
        v = get(doc, key)
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            y, m, d = v.split("-")
            if d != m and int(d) <= 12:
                out = copy.deepcopy(doc)
                put(out, key, f"{y}-{d}-{m}")
                return out
    return None


def wrong_doc_number(doc):
    """A document number that is not the printed one (one character changed)."""
    for key in NUMBERS:
        v = get(doc, key)
        if isinstance(v, str) and re.search(r"\d", v):
            i = max(i for i, c in enumerate(v) if c.isdigit())
            out = copy.deepcopy(doc)
            put(out, key, v[:i] + str((int(v[i]) + 1) % 10) + v[i + 1:])
            return out
    return None


def fabricated_quote(doc):
    """A total with a citation that is not on the page at all."""
    key = _first(doc, TOTALS)
    if key is None or not isinstance(get(doc, key), (int, float)):
        return None
    out = copy.deepcopy(doc)
    wrong = round(float(get(doc, key)) + 41.0, 2)
    put(out, key, wrong)
    out["field_locations"][key] = {"page": 1, "quote": f"TOTAL DUE {wrong:.2f}"}
    return out


def wrong_party(doc):
    """The seller or merchant name replaced by another plausible company name."""
    for key in ("seller.name", "merchant_name", "supplier.name", "shipper_name", "bank_name"):
        if isinstance(get(doc, key), str):
            out = copy.deepcopy(doc)
            put(out, key, "Harbor View Trading Ltd")
            return out
    return None


def broken_iban(doc):
    """An IBAN with a wrong check digit."""
    for key in ("payment_account.iban", "account_iban"):
        v = get(doc, key)
        if isinstance(v, str) and len(v.replace(" ", "")) > 10:
            s = v.replace(" ", "")
            out = copy.deepcopy(doc)
            put(out, key, s[:-1] + str((int(s[-1]) + 1) % 10) if s[-1].isdigit() else s[:-1] + "0")
            return out
    return None


MUTATIONS = {
    "digit_in_total": digit_in_total,
    "consistent_inflation": consistent_inflation,
    "tax_off": tax_off,
    "day_month_swap": day_month_swap,
    "wrong_doc_number": wrong_doc_number,
    "fabricated_quote": fabricated_quote,
    "wrong_party": wrong_party,
    "broken_iban": broken_iban,
}


def run(doc: dict, doc_type: str, text: str):
    # One page, as a list: the form process_document validates with. A bare
    # string skips the document-number check (it keys on the [PAGE n] marker).
    result = verify(copy.deepcopy(doc), [text], document_type=doc_type)
    return result.status, result.review_reasons


def main() -> None:
    rows = []
    for expected_path in sorted(GOLDEN.glob("*.expected.json")):
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        if "_expect_validation_error_field" in expected:
            continue  # the document itself is wrong; its planted error is covered below
        text = source_text(expected_path, expected)
        if text is None:
            continue
        doc_type = expected["doc_type"]
        doc, uncited = build(expected, text)
        status, reasons = run(doc, doc_type, text)
        row = {"document": expected_path.name[: -len(".expected.json")], "doc_type": doc_type,
               "uncited": uncited, "clean_status": status.value, "clean_reasons": reasons, "mutations": {}}
        if status == DocumentStatus.SUCCEEDED:
            for name, mutate in MUTATIONS.items():
                bad = mutate(doc)
                if bad is None:
                    continue
                m_status, m_reasons = run(bad, doc_type, text)
                row["mutations"][name] = {"caught": m_status != DocumentStatus.SUCCEEDED,
                                          "status": m_status.value, "reasons": m_reasons}
        rows.append(row)
        caught = sum(m["caught"] for m in row["mutations"].values())
        print(f"{row['document']:28} clean={status.value:13} caught {caught}/{len(row['mutations'])}"
              + (f"  uncited={uncited}" if uncited else ""))

    clean_ok = [r for r in rows if r["clean_status"] == "succeeded"]
    summary = {"documents": len(rows), "clean_succeeded": len(clean_ok),
               "false_alarms": [{"document": r["document"], "status": r["clean_status"], "reasons": r["clean_reasons"]}
                                for r in rows if r["clean_status"] != "succeeded"],
               "by_mutation": {}}
    for name in MUTATIONS:
        applied = [r["mutations"][name] for r in clean_ok if name in r["mutations"]]
        summary["by_mutation"][name] = {"applied": len(applied), "caught": sum(m["caught"] for m in applied)}
    total_applied = sum(v["applied"] for v in summary["by_mutation"].values())
    total_caught = sum(v["caught"] for v in summary["by_mutation"].values())
    summary["caught_total"] = f"{total_caught}/{total_applied}"

    print(f"\nclean documents passing: {len(clean_ok)}/{len(rows)}")
    for name, v in summary["by_mutation"].items():
        print(f"  {name:22} caught {v['caught']}/{v['applied']}")
    print(f"  {'all':22} caught {summary['caught_total']}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "documents": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
