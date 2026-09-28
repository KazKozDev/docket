"""European invoices with exact ground truth: the ZUGFeRD / Factur-X corpus.

    python eval/download_zugferd.py            # download, build expected.json, render scans
    python eval/download_zugferd.py --rescan   # re-render scan/ from pdf/

Downloads the valid ZUGFeRD 2 / Factur-X PDFs of github.com/ZUGFeRD/corpus
(Apache-2.0) into eval/zugferd_samples/. Every one carries its invoice as
embedded EN 16931 CII XML, so the expected.json is read from that XML,
not labelled by hand: number, dates, parties, seller VAT ID, IBAN, currency,
net, tax and gross totals, and the line items.

The corpus keeps the same sample invoice in several ZUGFeRD versions;
duplicates (same type, number, seller and total) are kept once.

Two copies of each invoice are written:

- pdf/  the original PDF, text layer included (docket reads it without OCR);
- scan/ the same pages rendered to images at 200 dpi, no text layer, so
  the OCR path is exercised against the same ground truth.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docket import extract_facturx_xml

OUT = ROOT / "eval" / "zugferd_samples"
TREE = "https://api.github.com/repos/ZUGFeRD/corpus/git/trees/master?recursive=1"
RAW = "https://raw.githubusercontent.com/ZUGFeRD/corpus/master/"
FOLDERS = ("ZUGFeRDv2/correct/", "XML-Rechnung/FX/")

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
# UNTDID 1001 codes EN 16931 allows: the credit-note family is a credit note,
# and so is 384, the corrected invoice ("Rechnungskorrektur"), which docket
# files under credit_note like a factura rectificativa; every other code is
# an invoice. 751 ("invoice information for accounting
# purposes", the MINIMUM / BASIC-WL booking aids) is not an invoice at all.
CREDIT_NOTE_CODES = {"81", "83", "261", "262", "296", "308", "381", "384", "396", "420", "458", "532"}
NOT_AN_INVOICE = {"751"}


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "docket-eval"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _text(node, path: str) -> str | None:
    found = node.find(path, NS)
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


_NOT_A_NAME_LINE = re.compile(
    r"^(?:herrn?|frau|firma|m\.|mme|monsieur|madame)$|^(?:gln|lief(?:erant)?[-.\s]*nr|kunden[-.\s]*nr)\b",
    re.IGNORECASE,
)


def _name(node, path: str) -> str | None:
    """A party name as the page prints it: the XML samples put a salutation
    ("Herrn") or identifier lines ("GLN 4333741000005", "Lief-Nr: 549910")
    into the name element on their own lines."""
    raw = _text(node, path)
    if raw is None:
        return None
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _NOT_A_NAME_LINE.search(ln)]
    return " ".join(kept) or None


def _compact(value: str | None) -> str | None:
    """An IBAN without the print grouping ("DE88 2008 ..." -> "DE882008...")."""
    return "".join(value.split()) if value else None


def _date(node, path: str) -> str | None:
    raw = _text(node, path)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if raw and len(raw) == 8 and raw.isdigit() else None


def _amount(node, path: str, currency: str | None = None) -> float | None:
    for found in node.findall(path, NS):
        if currency and found.get("currencyID") not in (None, currency):
            continue
        if found.text and found.text.strip():
            return round(float(found.text), 2)
    return None


def expected_from_cii(xml: bytes) -> dict | None:
    """The graded fields of one CII invoice, in docket's expected.json keys."""
    root = ET.fromstring(xml)
    if root.tag != f"{{{NS['rsm']}}}CrossIndustryInvoice":
        return None
    doc = root.find("rsm:ExchangedDocument", NS)
    trade = root.find("rsm:SupplyChainTradeTransaction", NS)
    if doc is None or trade is None:
        return None
    code = _text(doc, "ram:TypeCode")
    if not code or code in NOT_AN_INVOICE:
        return None
    if code in CREDIT_NOTE_CODES:
        doc_type, number_key = "credit_note", "credit_note_number"
    else:
        doc_type, number_key = "invoice", "invoice_number"
    agreement = trade.find("ram:ApplicableHeaderTradeAgreement", NS)
    settlement = trade.find("ram:ApplicableHeaderTradeSettlement", NS)
    seller = agreement.find("ram:SellerTradeParty", NS)
    buyer = agreement.find("ram:BuyerTradeParty", NS)
    currency = _text(settlement, "ram:InvoiceCurrencyCode")
    totals = settlement.find("ram:SpecifiedTradeSettlementHeaderMonetarySummation", NS)

    expected: dict = {"doc_type": doc_type}
    fields = {
        number_key: _text(doc, "ram:ID"),
        "issue_date": _date(doc, "ram:IssueDateTime/udt:DateTimeString"),
        "due_date": _date(settlement, "ram:SpecifiedTradePaymentTerms/ram:DueDateDateTime/udt:DateTimeString"),
        "seller.name": _name(seller, "ram:Name"),
        "buyer.name": _name(buyer, "ram:Name"),
        "currency": currency,
        "payment_account.iban": _compact(_text(
            settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID")),
        "subtotal": _amount(totals, "ram:LineTotalAmount"),
        "tax_amount": _amount(totals, "ram:TaxTotalAmount", currency),
        "total_amount": _amount(totals, "ram:GrandTotalAmount"),
    }
    # The seller's tax identifiers are graded by position, so only an
    # unambiguous one: a seller with exactly one, and it a VAT ID.
    tax_ids = seller.findall("ram:SpecifiedTaxRegistration/ram:ID", NS)
    if len(tax_ids) == 1 and tax_ids[0].get("schemeID") == "VA":
        fields["seller.tax_ids[0].value"] = tax_ids[0].text.strip()
    expected.update({k: v for k, v in fields.items() if v is not None})

    lines = []
    for item in trade.findall("ram:IncludedSupplyChainTradeLineItem", NS):
        total = _amount(item, "ram:SpecifiedLineTradeSettlement/"
                              "ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount")
        quantity = _amount(item, "ram:SpecifiedLineTradeDelivery/ram:BilledQuantity")
        lines.append({"description": _text(item, "ram:SpecifiedTradeProduct/ram:Name"),
                      "quantity": quantity, "total": total})
    if lines:
        expected["_line_items"] = lines
    expected["_source"] = "ZUGFeRD/corpus (Apache-2.0), fields from the embedded CII XML"
    return expected


def render_scan(pdf: Path, out: Path, dpi: int = 200) -> None:
    """The PDF's pages as images in a new PDF: same look, no text layer."""
    import pymupdf

    with pymupdf.open(pdf) as source, pymupdf.open() as scan:
        for page in source:
            pixmap = page.get_pixmap(dpi=dpi)
            target = scan.new_page(width=page.rect.width, height=page.rect.height)
            # JPEG, as a scanner stores a page; raw pixmaps make a PDF past
            # docket's input size limit.
            target.insert_image(target.rect, stream=pixmap.tobytes("jpeg", jpg_quality=85))
        scan.save(out, garbage=3, deflate=True)


def rescan() -> None:
    """Re-render scan/ from the PDFs already in pdf/."""
    for pdf in sorted((OUT / "pdf").glob("*.pdf")):
        render_scan(pdf, OUT / "scan" / pdf.name)
    print(f"re-rendered {len(list((OUT / 'pdf').glob('*.pdf')))} scans")


def main() -> None:
    if "--rescan" in sys.argv:
        rescan()
        return
    tree = json.loads(_get(TREE))["tree"]
    paths = [e["path"] for e in tree if e["type"] == "blob" and e["path"].lower().endswith(".pdf")
             and e["path"].startswith(FOLDERS)]
    print(f"{len(paths)} candidate PDFs in {', '.join(FOLDERS)}")
    (OUT / "pdf").mkdir(parents=True, exist_ok=True)
    (OUT / "scan").mkdir(parents=True, exist_ok=True)
    seen: set[tuple] = set()
    kept = skipped = 0
    for path in sorted(paths):
        data = _get(RAW + urllib.parse.quote(path))
        try:
            expected = expected_from_cii(extract_facturx_xml(data))
        except Exception as exc:  # noqa: BLE001 — a PDF without readable CII is just not usable here
            print(f"  skip {path}: {type(exc).__name__}: {str(exc)[:80]}")
            skipped += 1
            continue
        if expected is None:
            print(f"  skip {path}: not an invoice or credit note in CII")
            skipped += 1
            continue
        number = expected.get("invoice_number") or expected.get("credit_note_number")
        key = (expected["doc_type"], number, expected.get("seller.name"), expected.get("total_amount"))
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        stem = "zf_" + Path(path).stem.replace(" ", "_")
        expected["_corpus_path"] = path
        (OUT / "pdf" / f"{stem}.pdf").write_bytes(data)
        render_scan(OUT / "pdf" / f"{stem}.pdf", OUT / "scan" / f"{stem}.pdf")
        for folder in ("pdf", "scan"):
            (OUT / folder / f"{stem}.expected.json").write_text(
                json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")
        kept += 1
        print(f"  {stem:60} {expected['doc_type']:12} {len([k for k in expected if not k.startswith('_')]) - 1} fields, "
              f"{len(expected.get('_line_items', []))} lines")
    print(f"\nkept {kept}, skipped {skipped} (duplicates or unusable) -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
