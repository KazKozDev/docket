"""Held-out check for the TF-IDF tier: phrasings that are NOT in its training
corpus, across the EU languages it claims to cover, and without the
document's own name (that is the rules tier's job).

The bar is the one the cascade relies on: when the tier is confident enough
to be trusted (confidence >= TFIDF_CONFIDENCE_FLOOR) it must be right, and it
must be confident often enough to save LLM calls at all.
"""
from docket import config
from docket.catalog import BUILTIN_SCHEMAS
from docket.catalog.corpus import CORPUS
from docket.classify_tfidf import classify_tfidf

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
    ("credit_note", "We refund the price difference for the faulty items against your earlier bill; nothing is payable."),
    ("credit_note", "Gutgeschriebener Betrag für die beschädigte Lieferung, Bezug: ursprüngliche Rechnung vom 3. März."),
    ("utility_bill", "Zählerstand alt 18.240, neu 18.912, Verbrauch 672 kWh, Grundpreis und Arbeitspreis."),
    ("utility_bill", "Electricity used between the two readings, standing charge and unit rate for the quarter."),
    ("delivery_note", "Goods received complete and undamaged, signed by the storekeeper at the loading dock."),
    ("delivery_note", "Marchandises livrées ce jour, quantités contrôlées à la réception, signature du destinataire."),
    ("certificate_of_origin", "The chamber certifies that the machinery described is of German origin; exporter and consignee listed."),
    ("certificate_of_origin", "Si attesta che le merci sotto descritte sono originarie dell'Italia, timbro della camera."),
    ("id_document", "Surname, given names, nationality, date of birth and expiry date; holder signature below."),
    ("id_document", "Nom, prénoms, sexe, nationalité, date de naissance et date d'expiration du document."),
    ("tax_invoice", "Supplier GST number, taxable value and GST payable shown for each line; input tax may be claimed."),
    ("tax_invoice", "USt-IdNr. des Leistenden, Leistungsdatum, Nettoentgelt und ausgewiesene Umsatzsteuer."),
]


def test_held_out_is_really_held_out():
    training = {t for texts in CORPUS.values() for t in texts}
    assert not {text for _, text in HELD_OUT} & training


def test_confident_answers_are_correct_and_frequent():
    floor = config.TFIDF_CONFIDENCE_FLOOR
    confident = correct_confident = correct = 0
    for label, text in HELD_OUT:
        result = classify_tfidf(text)
        correct += result.doc_type == label
        if result.confidence >= floor:
            confident += 1
            correct_confident += result.doc_type == label
    assert correct / len(HELD_OUT) >= 0.8, f"accuracy {correct}/{len(HELD_OUT)}"
    assert confident >= len(HELD_OUT) // 3, f"only {confident} confident answers"
    assert correct_confident == confident, (
        f"{confident - correct_confident} confident answers were wrong"
    )


def test_every_builtin_schema_is_trained():
    assert set(CORPUS) == {s.schema_id for s in BUILTIN_SCHEMAS}
    assert all(len(texts) >= 12 for texts in CORPUS.values())
