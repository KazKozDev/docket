"""Render the synthetic scans in eval/golden_dataset from their definitions.

    python eval/build_golden.py            # (re)write every *_scan document and its .expected.json
    python eval/build_golden.py --check    # verify the committed files match the definitions

One scan per built-in schema (plus a 180°-rotated invoice, a low-quality
Spanish invoice and a two-page scanned bank statement), rendered at 200 DPI
with DejaVu Sans, then skewed, blurred and noised with a fixed seed. Each
.expected.json holds the graded fields, the line items (`_line_items`), the
table grids as printed (`_tables`) and every printed line (`_text`), so the
OCR benchmark can score fields, line items, tables and raw text.

The documents are fictitious: invented companies and people, check digits
made valid so that validation passes on a correct reading. The identity
card is the ICAO Doc 9303 specimen (UTOPIA, MRZ from the standard's TD1
example) with no photo.

The font is downloaded once (pinned SHA-256) into .cache/fonts; the rendered
files are committed, so running the benchmark never needs this script.
"""

import argparse
import hashlib
import io
import json
import random
import sys
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "golden_dataset"
FONT_ZIP = {
    "url": "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip",
    "sha256": "7576310b219e04159d35ff61dd4a4ec4cdba4f35c00e002a136f00e96a908b0a",
}
DPI = 200
A4 = (1654, 2339)


# ---- document definitions -------------------------------------------------------------------


@dataclass
class Table:
    header: list[str]
    rows: list[list[str]]
    widths: list[float]  # column widths as fractions of the content width
    align: str  # one letter per column: l or r
    ruled: bool = True


@dataclass
class Doc:
    name: str
    schema: str
    pages: list[list[tuple]]
    expected: dict
    line_items: list[dict] = field(default_factory=list)
    size: tuple[int, int] = A4
    margin: int = 120
    font_size: int = 27
    skew: float = 0.6
    noise: float = 4.0
    blur: float = 0.6
    rotate: int = 0
    downscale: float = 1.0
    mono: bool = False
    note: str = ""


DOCS: list[Doc] = [
    Doc(
        name="invoice_de_scan",
        schema="invoice",
        pages=[[
            ("title", "RECHNUNG"),
            ("gap", 20),
            ("cols",
             ["Nordlicht Bürobedarf GmbH", "Speicherstraße 8", "28195 Bremen",
              "USt-IdNr.: DE136695976"],
             ["Rechnungsnummer: RE-2026-1187", "Rechnungsdatum: 14.07.2026",
              "Fällig am: 13.08.2026", "Kundennummer: K-40217"]),
            ("gap", 40),
            ("line", "Rechnungsempfänger:"),
            ("lines", ["Hansa Logistik AG", "Am Kaiser-Kai 3", "20457 Hamburg"]),
            ("gap", 40),
            ("table", Table(
                ["Pos.", "Beschreibung", "Menge", "Einzelpreis", "Betrag"],
                [["1", "Kopierpapier A4, 500 Blatt", "40", "4,90", "196,00"],
                 ["2", "Toner schwarz TN-2420", "6", "58,50", "351,00"],
                 ["3", "Ordner breit, blau", "25", "2,80", "70,00"]],
                [0.08, 0.46, 0.12, 0.17, 0.17], "llrrr")),
            ("gap", 20),
            ("right", ["Zwischensumme: 617,00 EUR", "USt. 19 %: 117,23 EUR", "Gesamtbetrag: 734,23 EUR"]),
            ("gap", 50),
            ("lines", ["Zahlbar innerhalb von 30 Tagen ohne Abzug.",
                       "Bankverbindung: Commerzbank Bremen",
                       "IBAN: DE89 3704 0044 0532 0130 00  BIC: COBADEFFXXX"]),
        ]],
        expected={
            "doc_type": "invoice", "invoice_number": "RE-2026-1187", "issue_date": "2026-07-14",
            "due_date": "2026-08-13", "seller.name": "Nordlicht Bürobedarf GmbH", "buyer.name": "Hansa Logistik AG",
            "seller.tax_ids[0].value": "DE136695976", "subtotal": 617.0, "tax_amount": 117.23,
            "total_amount": 734.23, "currency": "EUR", "payment_account.iban": "DE89370400440532013000",
        },
        line_items=[
            {"description": "Kopierpapier A4, 500 Blatt", "quantity": 40, "total": 196.0},
            {"description": "Toner schwarz TN-2420", "quantity": 6, "total": 351.0},
            {"description": "Ordner breit, blau", "quantity": 25, "total": 70.0},
        ],
    ),
    Doc(
        name="invoice_rotated_scan",
        schema="invoice",
        rotate=180,
        note="page scanned upside down",
        pages=[[
            ("title", "INVOICE"),
            ("gap", 20),
            ("cols",
             ["Summit Ridge Analytics LLC", "410 Alder Street, Suite 12", "Portland, OR 97204",
              "billing@summitridge.example"],
             ["Invoice No: INV-40912", "Invoice Date: June 2, 2026", "Due Date: July 2, 2026", "Terms: Net 30"]),
            ("gap", 40),
            ("line", "Bill To:"),
            ("lines", ["Cobalt Harbor Inc.", "88 Pier Road", "Seattle, WA 98121"]),
            ("gap", 40),
            ("table", Table(
                ["Description", "Qty", "Rate", "Amount"],
                [["Data pipeline audit (hours)", "12", "150.00", "1,800.00"],
                 ["Dashboard build", "1", "2,400.00", "2,400.00"],
                 ["Training session", "2", "350.00", "700.00"]],
                [0.52, 0.12, 0.18, 0.18], "lrrr")),
            ("gap", 20),
            ("right", ["Subtotal: $4,900.00", "Sales Tax (6%): $294.00", "Total Due: $5,194.00"]),
            ("gap", 50),
            ("line", "Please reference the invoice number with your payment."),
        ]],
        expected={
            "doc_type": "invoice", "invoice_number": "INV-40912", "issue_date": "2026-06-02",
            "due_date": "2026-07-02", "seller.name": "Summit Ridge Analytics LLC", "buyer.name": "Cobalt Harbor Inc.",
            "subtotal": 4900.0, "tax_amount": 294.0, "total_amount": 5194.0, "currency": "USD",
        },
        line_items=[
            {"description": "Data pipeline audit (hours)", "quantity": 12, "total": 1800.0},
            {"description": "Dashboard build", "quantity": 1, "total": 2400.0},
            {"description": "Training session", "quantity": 2, "total": 700.0},
        ],
    ),
    Doc(
        name="invoice_es_lowres_scan",
        schema="invoice",
        downscale=0.55,
        noise=8.0,
        blur=0.9,
        skew=1.2,
        note="low-resolution, noisy scan",
        pages=[[
            ("title", "FACTURA"),
            ("gap", 20),
            ("cols",
             ["Distribuciones Albufera S.L.", "Calle de Colón 27", "46004 Valencia", "CIF: B96999545"],
             ["Factura n.º: A-2026/0457", "Fecha: 09/05/2026", "Vencimiento: 08/06/2026"]),
            ("gap", 40),
            ("line", "Cliente:"),
            ("lines", ["Hostal Mar Menor S.L.", "Avenida del Puerto 5", "30740 San Pedro del Pinatar"]),
            ("gap", 40),
            ("table", Table(
                ["Concepto", "Cant.", "Precio", "Importe"],
                [["Sábanas algodón 150 cm", "30", "18,50", "555,00"],
                 ["Toallas de baño", "60", "7,25", "435,00"],
                 ["Albornoz talla única", "12", "24,00", "288,00"]],
                [0.52, 0.12, 0.18, 0.18], "lrrr")),
            ("gap", 20),
            ("right", ["Base imponible: 1.278,00 €", "IVA 21 %: 268,38 €", "Total factura: 1.546,38 €"]),
            ("gap", 50),
            ("line", "Forma de pago: transferencia a 30 días."),
        ]],
        expected={
            "doc_type": "invoice", "invoice_number": "A-2026/0457", "issue_date": "2026-05-09",
            "due_date": "2026-06-08", "seller.name": "Distribuciones Albufera S.L.",
            "buyer.name": "Hostal Mar Menor S.L.", "seller.tax_ids[0].value": "B96999545",
            "subtotal": 1278.0, "tax_amount": 268.38, "total_amount": 1546.38, "currency": "EUR",
        },
        line_items=[
            {"description": "Sábanas algodón 150 cm", "quantity": 30, "total": 555.0},
            {"description": "Toallas de baño", "quantity": 60, "total": 435.0},
            {"description": "Albornoz talla única", "quantity": 12, "total": 288.0},
        ],
    ),
    Doc(
        name="tax_invoice_scan",
        schema="tax_invoice",
        pages=[[
            ("title", "TAX INVOICE"),
            ("gap", 20),
            ("cols",
             ["Blue Gum Hardware Pty Ltd", "14 Wattle Avenue", "Newcastle NSW 2300", "ABN 51 824 753 556"],
             ["Tax Invoice No: TI-88213", "Date of Issue: 19/05/2026", "Customer ID: HC-0093"]),
            ("gap", 40),
            ("line", "Invoice to:"),
            ("lines", ["Harbourside Cafe Pty Ltd", "2 Wharf Road", "Newcastle NSW 2300"]),
            ("gap", 40),
            ("table", Table(
                ["Item", "Qty", "Unit Price", "Amount"],
                [["Timber decking 90x19 (m)", "120", "6.50", "780.00"],
                 ["Deck screws, box of 500", "4", "32.00", "128.00"],
                 ["Decking stain 4L", "3", "89.00", "267.00"]],
                [0.52, 0.12, 0.18, 0.18], "lrrr")),
            ("gap", 20),
            ("right", ["Subtotal (excl. GST): $1,175.00", "GST 10%: $117.50", "Total (incl. GST): $1,292.50"]),
            ("gap", 50),
            ("line", "All prices in Australian dollars (AUD)."),
        ]],
        expected={
            "doc_type": "tax_invoice", "invoice_number": "TI-88213", "issue_date": "2026-05-19",
            "seller.name": "Blue Gum Hardware Pty Ltd", "buyer.name": "Harbourside Cafe Pty Ltd",
            "subtotal": 1175.0, "tax_amount": 117.5, "total_amount": 1292.5, "currency": "AUD",
        },
        line_items=[
            {"description": "Timber decking 90x19 (m)", "quantity": 120, "total": 780.0},
            {"description": "Deck screws, box of 500", "quantity": 4, "total": 128.0},
            {"description": "Decking stain 4L", "quantity": 3, "total": 267.0},
        ],
    ),
    Doc(
        name="credit_note_fr_scan",
        schema="credit_note",
        pages=[[
            ("title", "AVOIR"),
            ("gap", 20),
            ("cols",
             ["Atelier Lumière SARL", "12 rue des Tanneurs", "69002 Lyon", "TVA : FR40303265045"],
             ["Avoir n° : AV-2026-031", "Date : 03/09/2026", "Facture d'origine : FA-2026-512"]),
            ("gap", 40),
            ("line", "Client :"),
            ("lines", ["Maison Verdier SA", "45 boulevard Carnot", "06400 Cannes"]),
            ("gap", 30),
            ("line", "Motif : retour de marchandise"),
            ("gap", 30),
            ("table", Table(
                ["Désignation", "Qté", "Prix unitaire", "Montant HT"],
                [["Lampe de bureau laiton", "2", "145,00", "290,00"],
                 ["Abat-jour en lin", "3", "38,00", "114,00"]],
                [0.50, 0.12, 0.19, 0.19], "lrrr")),
            ("gap", 20),
            ("right", ["Total HT : 404,00 €", "TVA 20 % : 80,80 €", "Total TTC : 484,80 €"]),
        ]],
        expected={
            "doc_type": "credit_note", "credit_note_number": "AV-2026-031", "issue_date": "2026-09-03",
            "seller.name": "Atelier Lumière SARL", "buyer.name": "Maison Verdier SA",
            "seller.tax_ids[0].value": "FR40303265045", "subtotal": 404.0, "tax_amount": 80.8,
            "total_amount": 484.8, "currency": "EUR",
        },
        line_items=[
            {"description": "Lampe de bureau laiton", "quantity": 2, "total": 290.0},
            {"description": "Abat-jour en lin", "quantity": 3, "total": 114.0},
        ],
    ),
    Doc(
        name="receipt_market_scan",
        schema="receipt",
        size=(760, 1500),
        margin=50,
        font_size=25,
        mono=True,
        skew=1.4,
        pages=[[
            ("center", "GREENLEAF MARKET"),
            ("center", "221 Birch Lane, Madison WI 53703"),
            ("center", "Tel (608) 555-0142"),
            ("gap", 30),
            ("line", "Date: 08/21/2026   14:32"),
            ("line", "Receipt #: 0048-2291"),
            ("gap", 20),
            ("table", Table(
                ["Item", "Qty", "Price"],
                [["Organic bananas", "1", "2.39"],
                 ["Oat milk 1L", "2", "4.98"],
                 ["Sourdough loaf", "1", "4.50"],
                 ["Free-range eggs 12", "1", "5.20"]],
                [0.62, 0.14, 0.24], "lrr", ruled=False)),
            ("gap", 20),
            ("right", ["SUBTOTAL  17.07", "TAX 5%     0.85", "TOTAL     17.92"]),
            ("gap", 20),
            ("line", "VISA ************4821"),
            ("gap", 20),
            ("center", "THANK YOU FOR SHOPPING LOCAL"),
        ]],
        expected={
            "doc_type": "receipt", "merchant_name": "Greenleaf Market", "transaction_date": "2026-08-21",
            "subtotal": 17.07, "tax_amount": 0.85, "total_amount": 17.92, "card_last_four": "4821",
        },
        line_items=[
            {"description": "Organic bananas", "quantity": 1, "total": 2.39},
            {"description": "Oat milk 1L", "quantity": 2, "total": 4.98},
            {"description": "Sourdough loaf", "quantity": 1, "total": 4.50},
            {"description": "Free-range eggs 12", "quantity": 1, "total": 5.20},
        ],
    ),
    Doc(
        name="contract_scan",
        schema="contract",
        pages=[[
            ("title", "SOFTWARE MAINTENANCE AGREEMENT"),
            ("gap", 30),
            ("para", "This Software Maintenance Agreement (the \"Agreement\") is entered into as of "
                     "January 1, 2026 (the \"Effective Date\") by and between Helix Data Systems Ltd, a company "
                     "registered in Ireland (the \"Provider\"), and Greenfield Municipal Utilities, a public "
                     "utility (the \"Customer\")."),
            ("gap", 20),
            ("para", "1. Services. The Provider shall maintain and support the Customer's billing platform, "
                     "including corrective releases, security patches and a service desk available on business days."),
            ("para", "2. Term. The Agreement runs for twenty-four months from the Effective Date and ends on "
                     "December 31, 2027. It renews automatically for successive twelve-month periods unless either "
                     "party gives at least ninety (90) days' written notice before the end of the current term."),
            ("para", "3. Fees. The Customer shall pay a total fee of EUR 96,000.00, invoiced in eight equal "
                     "quarterly instalments and payable within thirty days of the invoice date."),
            ("para", "4. Breach. Either party may terminate this Agreement if the other party materially breaches it "
                     "and fails to cure the breach within thirty (30) days of written notice."),
            ("para", "5. Liability. Each party's aggregate liability is limited to the fees paid in the twelve "
                     "months preceding the claim."),
            ("para", "6. Governing Law. This Agreement is governed by the laws of Ireland."),
            ("gap", 40),
            ("cols", ["For Helix Data Systems Ltd", "Name: Ciara Byrne", "Title: Managing Director"],
                     ["For Greenfield Municipal Utilities", "Name: Thomas Wendt", "Title: Head of Procurement"]),
        ]],
        expected={
            "doc_type": "contract", "contract_title": "Software Maintenance Agreement",
            "effective_date": "2026-01-01", "expiration_date": "2027-12-31", "contract_value": 96000.0,
            "currency": "EUR", "auto_renewal": True, "notice_period_days": 90, "cure_period_days": 30,
            "parties_a[0]": "Helix Data Systems Ltd", "parties_b[0]": "Greenfield Municipal Utilities",
        },
    ),
    Doc(
        name="purchase_order_scan",
        schema="purchase_order",
        pages=[[
            ("title", "PURCHASE ORDER"),
            ("gap", 20),
            ("cols",
             ["Riverside Clinic Ltd", "Purchasing Department", "9 Mill Lane", "Galway H91 X2K4"],
             ["PO Number: PO-7731", "Order Date: 11 April 2026", "Delivery by: 25 April 2026", "Currency: EUR"]),
            ("gap", 40),
            ("line", "Supplier:"),
            ("lines", ["MedEquip Distribution BV", "Industrieweg 40", "3542 AD Utrecht, Netherlands"]),
            ("gap", 40),
            ("table", Table(
                ["Item", "Description", "Qty", "Unit Price", "Line Total"],
                [["GL-100M", "Nitrile gloves M, box of 100", "50", "7.80", "390.00"],
                 ["FM-FFP2", "FFP2 face masks, box of 20", "30", "12.50", "375.00"],
                 ["HS-500", "Hand sanitizer 500 ml", "40", "3.25", "130.00"]],
                [0.14, 0.42, 0.10, 0.16, 0.18], "llrrr")),
            ("gap", 20),
            ("right", ["Subtotal: 895.00", "VAT 21%: 187.95", "Order Total: EUR 1,082.95"]),
            ("gap", 50),
            ("line", "Payment terms: 30 days from receipt of goods and invoice."),
            ("line", "Authorised by: Dr. Aoife Kelly"),
        ]],
        expected={
            "doc_type": "purchase_order", "po_number": "PO-7731", "po_date": "2026-04-11",
            "requested_delivery_date": "2026-04-25", "buyer.name": "Riverside Clinic Ltd",
            "supplier.name": "MedEquip Distribution BV", "subtotal": 895.0, "tax_amount": 187.95,
            "total_amount": 1082.95, "currency": "EUR",
        },
        line_items=[
            {"description": "Nitrile gloves M, box of 100", "quantity": 50, "total": 390.0},
            {"description": "FFP2 face masks, box of 20", "quantity": 30, "total": 375.0},
            {"description": "Hand sanitizer 500 ml", "quantity": 40, "total": 130.0},
        ],
    ),
    Doc(
        name="bank_statement_2p_scan",
        schema="bank_statement",
        note="two-page scanned PDF, no text layer",
        pages=[
            [
                ("title", "Alpine Cantonal Bank"),
                ("line", "Account Statement"),
                ("gap", 20),
                ("cols",
                 ["Account holder: Keller & Frei GmbH", "Bahnhofstrasse 21", "8001 Zürich"],
                 ["IBAN: CH93 0076 2011 6238 5295 7", "Period: 01.06.2026 - 30.06.2026", "Currency: CHF"]),
                ("gap", 30),
                ("line", "Opening balance 01.06.2026: 12,450.00"),
                ("gap", 20),
                ("table", Table(
                    ["Date", "Description", "Debit", "Credit", "Balance"],
                    [["02.06.2026", "Customer payment Weber AG", "", "4,800.00", "17,250.00"],
                     ["03.06.2026", "Rent June, office Seefeld", "2,350.00", "", "14,900.00"],
                     ["08.06.2026", "Swisscom invoice 06/26", "189.90", "", "14,710.10"],
                     ["12.06.2026", "Customer payment Brunner & Co", "", "2,150.00", "16,860.10"],
                     ["15.06.2026", "Salaries June", "7,900.00", "", "8,960.10"],
                     ["18.06.2026", "Card purchase Digitec", "412.35", "", "8,547.75"]],
                    [0.16, 0.40, 0.14, 0.14, 0.16], "llrrr")),
                ("gap", 30),
                ("line", "Continued on page 2"),
            ],
            [
                ("line", "Alpine Cantonal Bank - Account Statement - page 2"),
                ("gap", 30),
                ("table", Table(
                    ["Date", "Description", "Debit", "Credit", "Balance"],
                    [["22.06.2026", "Customer payment Lindt Services", "", "3,600.00", "12,147.75"],
                     ["25.06.2026", "AHV contributions Q2", "1,275.40", "", "10,872.35"],
                     ["29.06.2026", "Bank fees June", "24.00", "", "10,848.35"],
                     ["30.06.2026", "Interest", "", "3.15", "10,851.50"]],
                    [0.16, 0.40, 0.14, 0.14, 0.16], "llrrr")),
                ("gap", 30),
                ("lines", ["Total debits: 12,151.65", "Total credits: 10,553.15",
                           "Closing balance 30.06.2026: 10,851.50"]),
            ],
        ],
        expected={
            "doc_type": "bank_statement", "bank_name": "Alpine Cantonal Bank", "account_holder": "Keller & Frei GmbH",
            "account_iban": "CH9300762011623852957", "statement_period_start": "2026-06-01",
            "statement_period_end": "2026-06-30", "currency": "CHF", "opening_balance": 12450.0,
            "closing_balance": 10851.5, "total_deposits": 10553.15, "total_withdrawals": 12151.65,
        },
        line_items=[
            {"description": "Customer payment Weber AG", "total": 4800.0},
            {"description": "Rent June, office Seefeld", "total": -2350.0},
            {"description": "Swisscom invoice 06/26", "total": -189.9},
            {"description": "Customer payment Brunner & Co", "total": 2150.0},
            {"description": "Salaries June", "total": -7900.0},
            {"description": "Card purchase Digitec", "total": -412.35},
            {"description": "Customer payment Lindt Services", "total": 3600.0},
            {"description": "AHV contributions Q2", "total": -1275.4},
            {"description": "Bank fees June", "total": -24.0},
            {"description": "Interest", "total": 3.15},
        ],
    ),
    Doc(
        name="acceptance_act_scan",
        schema="acceptance_act",
        pages=[[
            ("title", "ACT OF ACCEPTANCE OF SERVICES"),
            ("gap", 20),
            ("lines", ["Act No. AA-2026-044", "Date: 31 March 2026", "Under Service Agreement No. SA-2025-117"]),
            ("gap", 30),
            ("para", "Pixelforge Studio LLC (the \"Contractor\", Tax ID 84-3920175) has rendered and Orion Retail LLC "
                     "(the \"Customer\", Tax ID 27-5518043) has accepted the following services:"),
            ("gap", 20),
            ("table", Table(
                ["No.", "Service", "Qty", "Price", "Amount"],
                [["1", "UX research phase", "1", "3,200.00", "3,200.00"],
                 ["2", "UI design, screens", "24", "150.00", "3,600.00"]],
                [0.08, 0.46, 0.10, 0.18, 0.18], "llrrr")),
            ("gap", 20),
            ("right", ["Subtotal: 6,800.00 USD", "Sales tax: 0.00 USD", "Total: 6,800.00 USD"]),
            ("gap", 30),
            ("para", "The services were rendered in full and on time. The Customer has no claims regarding the "
                     "scope, quality or timing of the services."),
            ("gap", 40),
            ("cols", ["Contractor:", "Pixelforge Studio LLC", "Marco Ruiz, CEO"],
                     ["Customer:", "Orion Retail LLC", "Dana Whitfield, COO"]),
        ]],
        expected={
            "doc_type": "acceptance_act", "act_number": "AA-2026-044", "act_date": "2026-03-31",
            "contract_reference": "SA-2025-117", "customer_name": "Orion Retail LLC",
            "contractor_name": "Pixelforge Studio LLC", "subtotal": 6800.0, "total_amount": 6800.0,
            "currency": "USD", "claims_waived": True,
        },
        line_items=[
            {"description": "UX research phase", "quantity": 1, "total": 3200.0},
            {"description": "UI design, screens", "quantity": 24, "total": 3600.0},
        ],
    ),
    Doc(
        name="waybill_scan",
        schema="waybill",
        pages=[[
            ("title", "CMR CONSIGNMENT NOTE"),
            ("gap", 10),
            ("lines", ["No. CMR-558201", "Date: 17.02.2026"]),
            ("gap", 30),
            ("cols",
             ["1. Sender:", "Baltic Timber UAB", "Pramonės g. 12, LT-50300 Kaunas, Lithuania"],
             ["2. Consignee:", "Holzwerk Süd GmbH", "Industriestraße 7, 86150 Augsburg, Germany"]),
            ("gap", 30),
            ("cols",
             ["16. Carrier:", "TransEuro Freight Sp. z o.o.", "Vehicle: WX 4821K"],
             ["3. Place of delivery:", "Augsburg, Germany", "4. Taking over: Kaunas, 17.02.2026"]),
            ("gap", 30),
            ("table", Table(
                ["Goods", "Packages", "Quantity", "Unit", "Gross kg"],
                [["Pine boards 25x100 mm", "8", "180", "pcs", "1,450"],
                 ["Birch plywood 18 mm", "4", "60", "sheets", "1,320"]],
                [0.40, 0.14, 0.14, 0.14, 0.18], "lrrlr")),
            ("gap", 20),
            ("lines", ["Total packages: 12", "Total gross weight: 2,770 kg"]),
            ("gap", 40),
            ("cols", ["Signature of sender", "Baltic Timber UAB"], ["Signature of carrier", "TransEuro Freight"]),
        ]],
        expected={
            "doc_type": "waybill", "waybill_number": "CMR-558201", "waybill_date": "2026-02-17",
            "shipper_name": "Baltic Timber UAB", "consignee_name": "Holzwerk Süd GmbH",
            "carrier_name": "TransEuro Freight Sp. z o.o.", "vehicle_number": "WX 4821K",
            "total_packages": 12, "total_gross_weight_kg": 2770.0,
        },
        line_items=[
            {"description": "Pine boards 25x100 mm", "quantity": 180},
            {"description": "Birch plywood 18 mm", "quantity": 60},
        ],
    ),
    Doc(
        name="delivery_note_scan",
        schema="delivery_note",
        pages=[[
            ("title", "DELIVERY NOTE"),
            ("gap", 20),
            ("cols",
             ["Contoso Lab Supplies Ltd", "Unit 4, Beacon Park", "Leeds LS11 5QE"],
             ["Delivery Note No: DN-30918", "Delivery Date: 22/01/2026", "Your order: PO-5521"]),
            ("gap", 40),
            ("line", "Deliver to:"),
            ("lines", ["University of Westbrook", "Chemistry Department, Goods In", "Westbrook WB2 7LN"]),
            ("gap", 40),
            ("table", Table(
                ["Code", "Description", "Ordered", "Delivered", "Unit"],
                [["PT-200", "Pipette tips 200 ul, rack", "20", "20", "rack"],
                 ["BK-250", "Glass beaker 250 ml", "60", "48", "pcs"],
                 ["NG-L", "Nitrile gloves L, box", "10", "10", "box"]],
                [0.14, 0.42, 0.14, 0.14, 0.16], "llrrl")),
            ("gap", 20),
            ("lines", ["Packages: 3", "Backorder: 12 x BK-250 to follow"]),
            ("gap", 40),
            ("line", "Received by: J. Okafor"),
        ]],
        expected={
            "doc_type": "delivery_note", "delivery_note_number": "DN-30918", "delivery_date": "2026-01-22",
            "supplier.name": "Contoso Lab Supplies Ltd", "recipient.name": "University of Westbrook",
            "total_packages": 3, "received_by": "J. Okafor",
        },
        line_items=[
            {"description": "Pipette tips 200 ul, rack", "quantity": 20},
            {"description": "Glass beaker 250 ml", "quantity": 48},
            {"description": "Nitrile gloves L, box", "quantity": 10},
        ],
    ),
    Doc(
        name="utility_bill_scan",
        schema="utility_bill",
        pages=[[
            ("title", "Northshore Energy"),
            ("line", "Electricity bill"),
            ("gap", 20),
            ("cols",
             ["Priya Raman", "17 Harbour View Road", "Duluth, MN 55802"],
             ["Account number: 7730-2291-04", "Bill number: NE-2026-0912", "Bill date: September 5, 2026",
              "Payment due: September 26, 2026"]),
            ("gap", 30),
            ("line", "Billing period: August 1, 2026 - August 31, 2026"),
            ("gap", 20),
            ("table", Table(
                ["Meter", "Previous", "Current", "Usage"],
                [["E-55120", "40,211", "40,618", "407 kWh"]],
                [0.28, 0.24, 0.24, 0.24], "lrrr")),
            ("gap", 30),
            ("table", Table(
                ["Charge", "Amount"],
                [["Energy 407 kWh at $0.2140", "87.10"],
                 ["Standing charge, 31 days at $0.45", "13.95"],
                 ["Network fee", "18.40"],
                 ["Sales tax 5%", "5.97"]],
                [0.70, 0.30], "lr")),
            ("gap", 20),
            ("right", ["Current charges: $125.42", "Previous balance: $102.30", "Payment received: -$102.30",
                       "Amount due: $125.42"]),
        ]],
        expected={
            "doc_type": "utility_bill", "provider.name": "Northshore Energy", "customer.name": "Priya Raman",
            "account_number": "7730-2291-04", "bill_number": "NE-2026-0912", "issue_date": "2026-09-05",
            "due_date": "2026-09-26", "billing_period_start": "2026-08-01", "billing_period_end": "2026-08-31",
            "service_type": "electricity", "current_charges": 125.42, "tax_amount": 5.97, "amount_due": 125.42,
            "currency": "USD",
        },
        line_items=[
            {"description": "Energy 407 kWh at $0.2140", "total": 87.10},
            {"description": "Standing charge, 31 days at $0.45", "total": 13.95},
            {"description": "Network fee", "total": 18.40},
        ],
    ),
    Doc(
        name="certificate_of_origin_scan",
        schema="certificate_of_origin",
        pages=[[
            ("title", "CERTIFICATE OF ORIGIN"),
            ("line", "Non-preferential origin"),
            ("gap", 20),
            ("lines", ["Certificate No. CO-2026-004417", "Date of issue: 08.07.2026"]),
            ("gap", 30),
            ("cols",
             ["1. Exporter:", "Anatolia Textile A.S.", "Organize Sanayi Bolgesi 4. Cad. No 11", "Denizli, Turkey"],
             ["2. Consignee:", "Nordic Home Oy", "Teollisuuskatu 9", "00510 Helsinki, Finland"]),
            ("gap", 30),
            ("lines", ["3. Country of origin: Turkey", "4. Transport details: by road, Denizli - Helsinki",
                       "5. Invoice: EXP-2026-338, value EUR 18,640.00"]),
            ("gap", 30),
            ("table", Table(
                ["Description of goods", "HS code", "Quantity"],
                [["Cotton bath towels", "630260", "1,200 pcs"],
                 ["Linen bed sheets", "630231", "400 pcs"]],
                [0.52, 0.22, 0.26], "llr")),
            ("gap", 40),
            ("para", "The undersigned authority certifies that the goods described above originate in the "
                     "country shown in box 3."),
            ("line", "Istanbul Chamber of Commerce"),
        ]],
        expected={
            "doc_type": "certificate_of_origin", "certificate_number": "CO-2026-004417", "issue_date": "2026-07-08",
            "certificate_type": "non_preferential", "exporter.name": "Anatolia Textile A.S.",
            "consignee.name": "Nordic Home Oy", "issuing_authority": "Istanbul Chamber of Commerce",
            "goods_value.amount": 18640.0, "goods_value.currency": "EUR",
        },
        line_items=[
            {"description": "Cotton bath towels", "quantity": 1200},
            {"description": "Linen bed sheets", "quantity": 400},
        ],
    ),
    Doc(
        name="id_document_scan",
        schema="id_document",
        size=(1400, 900),
        margin=60,
        font_size=30,
        skew=0.8,
        note="ICAO Doc 9303 TD1 specimen (UTOPIA), no photo",
        pages=[[
            ("title", "UTOPIA"),
            ("line", "IDENTITY CARD / SPECIMEN"),
            ("gap", 20),
            ("cols", ["Surname", "ERIKSSON", "Given names", "ANNA MARIA", "Sex", "F"],
                     ["Nationality", "UTO", "Date of birth", "12 08 1974", "Document no.", "D23145890",
                      "Date of expiry", "15 04 2012"]),
            ("gap", 30),
            ("mono", ["I<UTOD231458907<<<<<<<<<<<<<<<", "7408122F1204159UTO<<<<<<<<<<<6",
                      "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"]),
        ]],
        expected={
            "doc_type": "id_document", "document_kind": "national_id", "document_number": "D23145890",
            "issuing_country": "UTO", "surname": "ERIKSSON", "given_names": "ANNA MARIA",
            "date_of_birth": "1974-08-12", "sex": "F", "date_of_expiry": "2012-04-15",
        },
    ),
    Doc(
        name="boarding_pass_scan",
        schema="boarding_pass",
        size=(1700, 760),
        margin=70,
        font_size=30,
        pages=[[
            ("title", "BOARDING PASS"),
            ("gap", 20),
            ("cols", ["Passenger", "OKONKWO/CHIDI MR", "Flight", "LH 1843", "From", "FRANKFURT FRA"],
                     ["Booking ref", "K7Q2PL", "Date", "03 OCT 2026  07:25", "To", "BARCELONA BCN"]),
            ("gap", 20),
            ("line", "Boarding 06:45    Gate A26    Seat 14C    Economy"),
        ]],
        expected={
            "doc_type": "boarding_pass", "passenger_name": "OKONKWO/CHIDI MR", "booking_reference": "K7Q2PL",
            "flight_number": "LH 1843", "seat": "14C", "gate": "A26",
        },
    ),
]


# ---- rendering -----------------------------------------------------------------------------------


def fonts(cache: Path) -> dict[str, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(FONT_ZIP["url"]).name
    if not archive.exists():
        print(f"downloading {FONT_ZIP['url']}")
        with urllib.request.urlopen(FONT_ZIP["url"], timeout=120) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != FONT_ZIP["sha256"]:
        raise SystemExit(f"{archive}: sha256 does not match the pinned value")
    paths = {}
    with zipfile.ZipFile(archive) as z:
        for key, name in (("regular", "DejaVuSans.ttf"), ("bold", "DejaVuSans-Bold.ttf"), ("mono", "DejaVuSansMono.ttf")):
            target = cache / name
            if not target.exists():
                target.write_bytes(z.read(f"dejavu-fonts-ttf-2.37/ttf/{name}"))
            paths[key] = target
    return paths


class Canvas:
    def __init__(self, doc: Doc, font_paths: dict[str, Path]):
        self.doc = doc
        self.image = Image.new("L", doc.size, 255)
        self.draw = ImageDraw.Draw(self.image)
        size = doc.font_size
        body = font_paths["mono" if doc.mono else "regular"]
        self.font = ImageFont.truetype(str(body), size)
        self.bold = ImageFont.truetype(str(font_paths["mono" if doc.mono else "bold"]), size)
        self.title = ImageFont.truetype(str(font_paths["bold"]), int(size * 1.6))
        self.mono = ImageFont.truetype(str(font_paths["mono"]), size)
        self.line_height = int(size * 1.45)
        self.x0, self.x1 = doc.margin, doc.size[0] - doc.margin
        self.y = doc.margin
        self.text: list[str] = []
        self.tables: list[list[list[str]]] = []

    def put(self, x: float, text: str, font=None) -> None:
        self.draw.text((x, self.y), text, font=font or self.font, fill=0)

    def width(self, text: str, font=None) -> float:
        return self.draw.textlength(text, font=font or self.font)

    def block(self, kind: str, *args) -> None:
        getattr(self, f"_{kind}")(*args)

    def _title(self, text):
        self.put(self.x0, text, self.title)
        self.y += int(self.line_height * 1.6)
        self.text.append(text)

    def _line(self, text):
        self.put(self.x0, text)
        self.y += self.line_height
        self.text.append(text)

    def _lines(self, lines):
        for line in lines:
            self._line(line)

    def _center(self, text):
        self.put((self.doc.size[0] - self.width(text)) / 2, text)
        self.y += self.line_height
        self.text.append(text)

    def _right(self, lines):
        for line in lines:
            self.put(self.x1 - self.width(line), line)
            self.y += self.line_height
            self.text.append(line)

    def _gap(self, pixels):
        self.y += pixels

    def _para(self, text):
        words, line = text.split(), ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if self.width(candidate) > self.x1 - self.x0 and line:
                self._line(line)
                line = word
            else:
                line = candidate
        if line:
            self._line(line)
        self.y += self.line_height // 3

    def _cols(self, left, right):
        top = self.y
        middle = self.x0 + (self.x1 - self.x0) * 0.55
        for line in left:
            self.put(self.x0, line)
            self.y += self.line_height
        bottom = self.y
        self.y = top
        for line in right:
            self.put(middle, line)
            self.y += self.line_height
        self.y = max(bottom, self.y)
        self.text.extend(left)
        self.text.extend(right)

    def _mono(self, lines):
        for line in lines:
            self.put(self.x0, line, self.mono)
            self.y += self.line_height
            self.text.append(line)

    def _table(self, table: Table):
        width = self.x1 - self.x0
        edges = [self.x0]
        for fraction in table.widths:
            edges.append(edges[-1] + fraction * width)
        pad = 12
        row_height = int(self.line_height * 1.25)

        def row(cells, font):
            for i, cell in enumerate(cells):
                if not cell:
                    continue
                if table.align[i] == "r":
                    x = edges[i + 1] - pad - self.width(cell, font)
                else:
                    x = edges[i] + pad
                self.draw.text((x, self.y + (row_height - self.line_height) // 2 + 4), cell, font=font, fill=0)

        top = self.y
        if table.ruled:
            self.draw.line((self.x0, self.y, self.x1, self.y), fill=0, width=2)
        row(table.header, self.bold)
        self.y += row_height
        if table.ruled:
            self.draw.line((self.x0, self.y, self.x1, self.y), fill=0, width=2)
        else:
            self.draw.line((self.x0, self.y, self.x1, self.y), fill=90, width=1)
        for cells in table.rows:
            row(cells, self.font)
            self.y += row_height
            if table.ruled:
                self.draw.line((self.x0, self.y, self.x1, self.y), fill=0, width=1)
        if table.ruled:
            for x in edges:
                self.draw.line((x, top, x, self.y), fill=0, width=1)
        self.y += 10
        self.tables.append([list(table.header)] + [list(r) for r in table.rows])
        self.text.append(" ".join(table.header))
        self.text.extend(" ".join(c for c in r if c) for r in table.rows)


def degrade(image: Image.Image, doc: Doc, seed: int) -> Image.Image:
    rng = random.Random(seed)
    if doc.downscale != 1.0:
        small = image.resize((int(image.width * doc.downscale), int(image.height * doc.downscale)), Image.BILINEAR)
        image = small.resize(image.size, Image.BILINEAR)
    if doc.skew:
        angle = doc.skew * (1 if rng.random() < 0.5 else -1)
        image = image.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    if doc.blur:
        image = image.filter(ImageFilter.GaussianBlur(doc.blur))
    if doc.noise:
        pixels = np.asarray(image, dtype=np.float32)
        pixels += np.random.default_rng(seed).normal(0.0, doc.noise, pixels.shape)
        image = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8))
    if doc.rotate:
        image = image.rotate(doc.rotate, expand=True)
    return image


def render(doc: Doc, font_paths: dict[str, Path]) -> tuple[list[Image.Image], list[str], list[dict]]:
    images, text, tables = [], [], []
    for number, page in enumerate(doc.pages, 1):
        canvas = Canvas(doc, font_paths)
        for block in page:
            canvas.block(*block)
        if canvas.y > doc.size[1] - doc.margin // 2:
            raise SystemExit(f"{doc.name} page {number} overflows ({canvas.y}px)")
        seed = int(hashlib.sha256(f"{doc.name}:{number}".encode()).hexdigest()[:8], 16)
        images.append(degrade(canvas.image, doc, seed))
        text.extend(canvas.text)
        tables.extend({"page": number, "cells": grid} for grid in canvas.tables)
    return images, text, tables


def outputs(doc: Doc, font_paths: dict[str, Path]) -> dict[Path, bytes]:
    images, text, tables = render(doc, font_paths)
    buffer = io.BytesIO()
    if len(images) == 1:
        target = OUT / f"{doc.name}.jpg"
        images[0].save(buffer, format="JPEG", quality=85, dpi=(DPI, DPI))
    else:
        target = OUT / f"{doc.name}.pdf"
        images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=DPI)
    expected = dict(doc.expected)
    expected["_schema"] = doc.schema
    expected["_line_items"] = doc.line_items
    expected["_tables"] = tables
    expected["_text"] = text
    expected["_source"] = "synthetic scan rendered by eval/build_golden.py" + (f"; {doc.note}" if doc.note else "")
    expected_bytes = (json.dumps(expected, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return {target: buffer.getvalue(), target.with_suffix(".expected.json"): expected_bytes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare with the committed files instead of writing")
    parser.add_argument("--cache", default=str(ROOT / ".cache" / "fonts"))
    args = parser.parse_args()
    font_paths = fonts(Path(args.cache))
    stale = []
    for doc in DOCS:
        for path, data in outputs(doc, font_paths).items():
            if args.check:
                # PDFs embed a creation date; compare their expected.json and page images instead.
                if path.suffix == ".pdf" or (path.exists() and path.read_bytes() == data):
                    continue
                stale.append(path.name)
            else:
                path.write_bytes(data)
    if args.check:
        print("up to date" if not stale else "stale: " + ", ".join(stale))
        sys.exit(1 if stale else 0)
    print(f"wrote {len(DOCS)} documents to {OUT}")


if __name__ == "__main__":
    main()
