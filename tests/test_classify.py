import pytest

from docket import classify as classify_module
from docket.classify import classify, classify_rules
from docket.schemas import DocType

INVOICE_TEXT = """
INVOICE
Invoice #: INV-2026-0042
Bill To: Wile E. Coyote
Amount Due: $1,250.00
Due Date: 2026-02-15
PO Number: PO-99123
"""

RECEIPT_TEXT = """
Corner Store — Receipt
Cashier: Jane
Thank you for your purchase!
Tendered: $20.00
Change Due: $3.50
"""

CONTRACT_TEXT = """
SERVICES AGREEMENT

This Agreement is entered into by and between Acme Corp and Wile E. Coyote.
WHEREAS the parties wish to formalize their working relationship,
the parties hereby agree as follows. Governing Law: State of Delaware.
"""

AMBIGUOUS_TEXT = "A short note with no distinguishing keywords at all."


def test_classifies_invoice_by_rules():
    result = classify(INVOICE_TEXT)
    assert result.doc_type == DocType.INVOICE
    assert result.method == "rules"
    assert result.confidence > 0


def test_classifies_receipt_by_rules():
    result = classify(RECEIPT_TEXT)
    assert result.doc_type == DocType.RECEIPT
    assert result.method == "rules"


def test_classifies_contract_by_rules():
    result = classify(CONTRACT_TEXT)
    assert result.doc_type == DocType.CONTRACT
    assert result.method == "rules"


def test_ambiguous_text_falls_back_to_llm(monkeypatch):
    monkeypatch.setattr(
        classify_module,
        "chat_json",
        lambda _prompt: {"doc_type": "receipt", "confidence": 0.4},
    )
    result = classify(AMBIGUOUS_TEXT)
    assert result.method == "llm"
    assert result.doc_type == DocType.RECEIPT
    assert result.confidence == 0.4


def test_llm_classifier_prompt_includes_boarding_pass(monkeypatch):
    captured = []

    def fake_chat(prompt):
        captured.append(prompt)
        return {"doc_type": "boarding_pass", "confidence": 0.8}

    monkeypatch.setattr(classify_module, "chat_json", fake_chat)
    result = classify_module.classify_llm("Passenger and itinerary")
    assert result.doc_type == DocType.BOARDING_PASS
    assert '"boarding_pass"' in captured[0]


def test_llm_classifier_reads_all_long_document_chunks(monkeypatch):
    prompts = []

    def fake_chat(prompt):
        prompts.append(prompt)
        if "TAIL_BOARDING_PASS" in prompt:
            return {"doc_type": "boarding_pass", "confidence": 1.0}
        return {"doc_type": "unknown", "confidence": 0.1}

    monkeypatch.setattr(classify_module, "chat_json", fake_chat)
    monkeypatch.setattr(classify_module.config, "EXTRACT_CHUNK_CHARS", 1000)
    result = classify_module.classify_llm("x" * 1100 + "TAIL_BOARDING_PASS")
    assert len(prompts) == 2
    assert result.doc_type == DocType.BOARDING_PASS


def test_llm_outage_falls_back_to_tfidf_answer(monkeypatch):
    """The LLM tier being down shouldn't lose the document — a low-confidence
    TF-IDF answer still routes it to a human, which beats an exception.
    """
    from docket.llm_client import LLMError

    def boom(_text):
        raise LLMError("Ollama request failed")

    monkeypatch.setattr(classify_module, "classify_llm", boom)
    monkeypatch.setattr(classify_module.config, "TFIDF_CONFIDENCE_FLOOR", 1.1)

    result = classify(AMBIGUOUS_TEXT)
    assert result.method in {"tfidf", "unavailable"}


def test_total_outage_returns_unknown_not_an_exception(monkeypatch):
    from docket.llm_client import LLMError

    def boom(_text):
        raise LLMError("Ollama request failed")

    monkeypatch.setattr(classify_module, "classify_llm", boom)
    monkeypatch.setattr(classify_module, "classify_tfidf", lambda _t: None)

    result = classify(AMBIGUOUS_TEXT)
    assert result.doc_type == DocType.UNKNOWN
    assert result.method == "unavailable"
    assert result.confidence == 0.0


SPANISH_INVOICE = """
FACTURA

Número de factura: FAC-2026-0042
Cliente: Aerolíneas del Sur S.L.
Base imponible: 1.234,56
IVA 21%: 259,26
Importe total: 1.493,82
Fecha de vencimiento: 15/04/2026
"""

SPANISH_RECEIPT = """
Recibo de compra
Cajero: Marta
Gracias por su compra
Efectivo entregado: 20,00
Cambio: 3,50
"""

SPANISH_CONTRACT = """
CONTRATO DE PRESTACIÓN DE SERVICIOS

De una parte, Vertex Consulting S.L., y de otra, Meridian Retail S.A.
Las partes acuerdan las siguientes cláusulas.
Legislación aplicable: derecho español.
"""


def test_spanish_invoice_classified_by_rules():
    """A Barcelona deployment reads "Factura", not "Invoice". Without the
    Spanish terms these documents matched nothing and fell through to the
    most expensive tier — the opposite of the point of having tiers.
    """
    result = classify(SPANISH_INVOICE)
    assert result.doc_type == DocType.INVOICE
    assert result.method == "rules"


def test_spanish_receipt_classified_by_rules():
    result = classify(SPANISH_RECEIPT)
    assert result.doc_type == DocType.RECEIPT
    assert result.method == "rules"


def test_spanish_contract_classified_by_rules():
    result = classify(SPANISH_CONTRACT)
    assert result.doc_type == DocType.CONTRACT
    assert result.method == "rules"


def test_bank_statement_classified_by_rules():
    text = """
    OFFICIAL BANK STATEMENT
    Account Statement Period: 01/01/2026 to 31/01/2026
    Opening balance: 5,420.00 EUR
    Closing balance: 7,150.00 EUR
    Deposits: 3,000.00 EUR
    Withdrawals: 1,270.00 EUR
    """
    result = classify(text)
    assert result.doc_type == DocType.BANK_STATEMENT
    assert result.method == "rules"


def test_acceptance_act_classified_by_rules():
    text = """
    CERTIFICATE OF ACCEPTANCE / ACT OF ACCEPTANCE
    Act of completion for services rendered under contract #123.
    Contractor confirms all work completed in full.
    Both parties confirm no mutual claims exist.
    """
    result = classify(text)
    assert result.doc_type == DocType.ACCEPTANCE_ACT
    assert result.method == "rules"


def test_waybill_classified_by_rules():
    text = """
    INTERNATIONAL CONSIGNMENT NOTE / WAYBILL (CMR)
    Shipper: Factory Central
    Consignee: Destination Hub
    Carrier: International Freight Co.
    Total gross weight: 450.0 kg
    """
    result = classify(text)
    assert result.doc_type == DocType.WAYBILL
    assert result.method == "rules"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("RECHNUNG Nr. 2026-17", "invoice"),
        ("FACTURE N° 88", "invoice"),
        ("Fattura n. 12 del 3 marzo", "invoice"),
        ("Factuur 2026-004", "invoice"),
        ("Kassenbon Filiale 12", "receipt"),
        ("Scontrino fiscale", "receipt"),
        ("Vertrag über Dienstleistungen", "contract"),
        ("Contrat de prestation de services", "contract"),
        ("Bon de commande 4711", "purchase_order"),
        ("Kontoauszug Nr. 9 / 2026", "bank_statement"),
        ("Estratto conto corrente", "bank_statement"),
        ("Abnahmeprotokoll Projekt Alpha", "acceptance_act"),
        ("Frachtbrief / lettre de voiture", "waybill"),
        ("Bordkarte LH 123", "boarding_pass"),
    ],
)
def test_eu_document_names(text, expected):
    result = classify_rules(text)
    assert result is not None and result.type_name == expected
