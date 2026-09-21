"""Held-out check for the TF-IDF tier: phrasings that are NOT in its training
corpus, across the EU languages it claims to cover, and without the
document's own name (that is the rules tier's job).

The bar is the one the cascade relies on: when the tier is confident enough
to be trusted (confidence >= TFIDF_CONFIDENCE_FLOOR) it must be right, and it
must be confident often enough to save LLM calls at all.
"""
from docket import config
from docket.classify_tfidf import _CORPUS, classify_tfidf

HELD_OUT = [
    ("invoice", "Zahlbar innerhalb von 30 Tagen netto ohne Abzug. Nettobetrag, USt 19 %, Gesamtbetrag."),
    ("invoice", "Total HT, TVA 20 %, total TTC. Échéance de paiement : 30 jours fin de mois."),
    ("invoice", "Importo imponibile, IVA 22 %, totale documento da saldare entro fine mese."),
    ("invoice", "Kindly pay the balance of the charges below by bank transfer within 21 days."),
    ("receipt", "Barzahlung. Gegeben 20,00 EUR, zurück 4,35 EUR. Danke und bis bald!"),
    ("receipt", "Payé en espèces, monnaie rendue 2,50 EUR. Merci et à bientôt."),
    ("receipt", "Paid with Visa contactless at till 2. Keep this slip for refunds."),
    ("contract", "Der Auftragnehmer verpflichtet sich, die Leistungen gemäß den folgenden Bestimmungen zu erbringen; Gerichtsstand ist München."),
    ("contract", "Les parties conviennent de ce qui suit. Le présent engagement prend effet à la date de signature."),
    ("contract", "The parties shall keep all disclosed information confidential and may terminate on written notice."),
    ("purchase_order", "Wir bitten um Lieferung folgender Positionen frei Haus an unser Zentrallager bis KW 42."),
    ("purchase_order", "Merci de livrer les références suivantes à notre entrepôt aux prix de votre offre."),
    ("bank_statement", "Buchungstag, Valuta, Verwendungszweck, Soll, Haben. Kontostand am Ende des Zeitraums."),
    ("bank_statement", "Solde au début de la période, débits, crédits, solde en fin de période."),
    ("acceptance_act", "Die Leistungen wurden vollständig erbracht und vom Auftraggeber ohne Mängel abgenommen."),
    ("acceptance_act", "Les prestations ont été réalisées et réceptionnées sans réserve par le client."),
    ("waybill", "Absender, Empfänger, Frachtführer, 18 Packstücke, Gesamtgewicht brutto 612 kg."),
    ("waybill", "Consignor, consignee and carrier details; 6 pallets, gross weight 1,240 kg."),
    ("boarding_pass", "Flugsteig B12, Sitz 23A, Einstieg ab 09:10, Abflug 09:40."),
    ("boarding_pass", "Porte 34, siège 7F, embarquement 14:05, départ 14:35."),
]


def test_held_out_is_really_held_out():
    training = {t for texts in _CORPUS.values() for t in texts}
    assert not {text for _, text in HELD_OUT} & training


def test_confident_answers_are_correct_and_frequent():
    floor = config.TFIDF_CONFIDENCE_FLOOR
    confident = correct_confident = correct = 0
    for label, text in HELD_OUT:
        result = classify_tfidf(text)
        correct += result.type_name == label
        if result.confidence >= floor:
            confident += 1
            correct_confident += result.type_name == label
    assert correct / len(HELD_OUT) >= 0.8, f"accuracy {correct}/{len(HELD_OUT)}"
    assert confident >= len(HELD_OUT) // 3, f"only {confident} confident answers"
    assert correct_confident == confident, (
        f"{confident - correct_confident} confident answers were wrong"
    )


def test_every_builtin_type_is_trained():
    from docket.schemas import SCHEMA_BY_DOC_TYPE

    assert set(_CORPUS) == {t.value for t in SCHEMA_BY_DOC_TYPE}
