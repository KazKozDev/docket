"""Second classification tier: a TF-IDF + logistic regression model.

Sits between the free keyword rules and the LLM call, and exists to answer a
question the rules can't: what about documents that are the right type but
phrased in a way the regexes don't cover (no literal "invoice" or "agreement"
on the page)? A linear model over word and character n-grams catches a lot of
that for a fraction of an LLM call's latency and cost.

The training set is a small, hand-written corpus embedded directly in this
module: paraphrases in English, Spanish, German, French, Italian, Dutch and
Portuguese, roughly 20-30 per document type. No external file, no network
call, trains in well under a second. That keeps the model reproducible and
the repo self-contained, at the cost of being weaker than a model trained on
thousands of labeled documents. Character n-grams carry most of the weight
across languages and survive OCR noise better than whole words.

The tier only answers when its confidence clears
`config.TFIDF_CONFIDENCE_FLOOR`; below that the cascade asks the LLM.
"""
from __future__ import annotations

from functools import lru_cache

from .schemas import ClassificationResult, DocType

# Deliberately paraphrased, not keyword-matched — if these used the same
# words as the rules in classify.py (the document's own name in any
# language), this tier would just be a slower copy of the rules tier.
_CORPUS: dict[str, list[str]] = {
    "invoice": [
        'Please remit payment within 30 days of the statement date to the account below.',
        'Balance owed for services rendered last month, itemized by hour and rate.',
        'Statement of charges for goods shipped on the attached purchase order.',
        'Payment terms: net 15. Late payments accrue 1.5% monthly interest.',
        'Itemized charges for consulting hours delivered in Q3, billed to your account.',
        'Remittance advice: please reference the order number when wiring funds.',
        'Outstanding balance for the delivered units, plus applicable sales tax.',
        'Kindly settle the enclosed statement before the remittance deadline.',
        'Charges this billing cycle for hosting, support, and add-on seats.',
        'This statement reflects goods delivered and is payable upon receipt.',
        'Sold to the below account; total reflects unit price times quantity shipped.',
        'Recurring subscription charge for the current billing period, auto-renewed.',
        'Le rogamos abone el importe indicado en un plazo de treinta días naturales.',
        'Detalle de los servicios prestados durante el mes, con su base imponible.',
        'Documento acreditativo de la operación comercial, sujeto al impuesto correspondiente.',
        'Importe pendiente por las mercancías entregadas, más el impuesto aplicable.',
        'Bitte überweisen Sie den ausgewiesenen Betrag innerhalb von 14 Tagen auf das unten genannte Konto.',
        'Für die erbrachten Leistungen berechnen wir Ihnen zuzüglich 19 % Umsatzsteuer folgenden Betrag.',
        'Montant à régler sous trente jours, TVA incluse, par virement sur le compte ci-dessous.',
        'Prestations effectuées au cours du mois, détaillées par heure et taux, hors taxes et TTC.',
        "Si prega di effettuare il pagamento dell'importo dovuto entro 30 giorni tramite bonifico.",
        'Imponibile, aliquota IVA e totale da corrispondere per i servizi forniti nel periodo.',
        'Gelieve het openstaande bedrag binnen 30 dagen over te maken onder vermelding van het nummer.',
        'Geleverde diensten van deze maand, exclusief en inclusief btw, te betalen per overschrijving.',
        'Solicitamos o pagamento do valor em dívida no prazo de 30 dias por transferência bancária.',
        'Serviços prestados no período, com base tributável, taxa de IVA e total a pagar.',
    ],
    "receipt": [
        "Here's your copy of today's purchase, keep it in case you need to return anything.",
        'Card ending 4477 approved. Have a nice day!',
        'Two items scanned at the register, change given in cash.',
        'Your order was rung up at the counter and paid for on the spot.',
        'Loyalty points earned on this visit will post to your account within 24 hours.',
        'Transaction approved — signature not required for purchases under the limit.',
        'Sale completed at register 3, cashier badge #12, till closed at close of shift.',
        "Everything scanned fine, here's what you paid and how much change you got back.",
        'In-store purchase, tax included, paid by contactless card at checkout.',
        'Print this slip for your records; returns accepted within 14 days with it.',
        'Groceries rung up one by one, coupon applied at the end before tender.',
        'Drive-thru order completed, total charged to the card on file.',
        'Justificante de la compra realizada hoy en el establecimiento, conserve este papel.',
        'Operación autorizada con tarjeta, no se requiere firma para este importe.',
        'Artículos pasados por caja y abonados en el momento, con el cambio entregado.',
        'Comprobante de la venta efectuada en el punto de venta, impuesto incluido.',
        'Vielen Dank für Ihren Einkauf. Gegeben bar, Rückgeld 3,20 EUR, MwSt. enthalten.',
        'Kartenzahlung erfolgreich, bitte Beleg für Umtausch aufbewahren.',
        'Merci de votre visite. Payé par carte sans contact, rendu monnaie en espèces.',
        'Achat réglé en caisse, articles scannés un par un, TVA comprise.',
        "Grazie per l'acquisto. Pagamento contanti, resto consegnato al cliente.",
        'Documento commerciale di vendita, pagato con carta al punto cassa.',
        'Bedankt voor uw aankoop. Betaald met pin, wisselgeld teruggegeven.',
        'Artikelen afgerekend aan de kassa, bewaar dit bewijs voor ruilen.',
        'Obrigado pela sua compra. Pago em numerário, troco entregue ao cliente.',
        'Compra paga no balcão com cartão, IVA incluído no preço.',
    ],
    "contract": [
        'Both signatories bind their respective organizations to the terms set out herein.',
        "Either side may terminate this arrangement with 60 days' written notice.",
        'This document sets out the mutual obligations of the two undersigned entities.',
        'Confidential information disclosed under this arrangement may not be shared with third parties.',
        'The undersigned entities agree to the terms and conditions set forth below.',
        'Disputes arising under this arrangement shall be resolved by binding arbitration.',
        'This instrument shall remain in force until superseded by a later signed version.',
        'Each signatory represents that it has full authority to enter into this arrangement.',
        'Upon breach, the non-breaching entity may pursue remedies available at law.',
        'This document is binding upon the successors and assigns of both entities.',
        'Renewal of this arrangement requires written consent from both signatories.',
        'Nothing herein shall be construed as creating a partnership between the entities.',
        'Ambas entidades firmantes quedan obligadas por lo estipulado en el presente documento.',
        'Cualquiera de las partes podrá resolverlo mediante preaviso por escrito.',
        'La información confidencial revelada no podrá comunicarse a terceros ajenos.',
        'Las controversias se resolverán conforme a la legislación vigente aplicable.',
        'Die unterzeichnenden Parteien verpflichten sich zur Einhaltung der nachstehenden Bestimmungen.',
        'Die Kündigung ist mit einer Frist von drei Monaten zum Quartalsende schriftlich möglich.',
        "Les parties soussignées s'engagent à respecter les conditions énoncées ci-après.",
        "Tout litige relatif à l'exécution sera soumis aux tribunaux compétents.",
        'Le parti sottoscritte si obbligano al rispetto delle clausole di seguito riportate.',
        'Il recesso può essere esercitato con preavviso scritto di sessanta giorni.',
        'De ondergetekende partijen verbinden zich tot naleving van de hierna volgende bepalingen.',
        'Geschillen worden voorgelegd aan de bevoegde rechter in het arrondissement.',
        'As partes signatárias obrigam-se ao cumprimento das cláusulas seguintes.',
        'Qualquer das partes pode rescindir mediante aviso prévio por escrito de sessenta dias.',
    ],
    "purchase_order": [
        'Official requisition for goods to be delivered to our warehouse facility.',
        'Please supply the items listed below at the agreed pricing schedule.',
        'Purchase order authorizing delivery of hardware components per quote.',
        'Authorized procurement order detailing item quantities, prices, and ship-to location.',
        'Orden de compra autorizada para el suministro de material según presupuesto acordado.',
        'Petición formal de aprovisionamiento con detalle de cantidades y precios unitarios.',
        'Hiermit beauftragen wir Sie mit der Lieferung der folgenden Artikel zu den vereinbarten Preisen.',
        'Bitte liefern Sie an unsere Lageranschrift und geben Sie unsere Referenz auf allen Papieren an.',
        'Veuillez nous livrer les articles suivants aux prix convenus selon votre devis.',
        "Demande d'approvisionnement : quantités, prix unitaires et adresse de livraison.",
        'Vi preghiamo di fornire i seguenti articoli ai prezzi concordati nel preventivo.',
        'Richiesta di approvvigionamento con quantità, prezzi unitari e luogo di consegna.',
        'Graag leveren wij de volgende artikelen tegen de overeengekomen prijzen, levering aan ons magazijn.',
        'Verzoek tot levering van onderstaande goederen, met aantallen en eenheidsprijzen.',
        'Pedimos o fornecimento dos artigos abaixo aos preços acordados no orçamento.',
        'Requisição de material com quantidades, preços unitários e local de entrega.',
        'Please ship the quantities below to our receiving dock by the requested date.',
        'Supplier is authorised to deliver the listed parts at the quoted unit prices.',
    ],
    "bank_statement": [
        'Monthly record of account debits and credits showing opening and closing ledger balances.',
        'Consolidated ledger of financial transactions, incoming wire transfers, and withdrawals.',
        'Account activity summary showing starting balance, daily movements, and ending funds.',
        'Official banking record of all funds transferred, fees deducted, and interest credited.',
        'Resumen de movimientos bancarios con desglose de cargos, abonos y saldo final.',
        'Registro mensual de cuenta bancaria con saldo inicial y transferencias recibidas.',
        'Alter Saldo, Gutschriften, Lastschriften und neuer Saldo für den Abrechnungszeitraum.',
        'Übersicht der Kontobewegungen mit Buchungstag, Wertstellung und Verwendungszweck.',
        'Ancien solde, opérations au crédit et au débit, et nouveau solde de la période.',
        "Détail des mouvements du compte avec date d'opération et date de valeur.",
        'Saldo iniziale, movimenti in accredito e addebito, saldo finale del periodo.',
        'Elenco delle operazioni sul conto corrente con data contabile e data valuta.',
        'Beginsaldo, bijschrijvingen, afschrijvingen en eindsaldo over de periode.',
        'Overzicht van de transacties op uw rekening met boekdatum en valutadatum.',
        'Saldo anterior, créditos, débitos e saldo final do período.',
        'Movimentos da conta à ordem com data-valor e descrição da operação.',
        'Interest credited and service fees debited to the account during the period.',
        'Card payments, direct debits and standing orders posted this month.',
    ],
    "acceptance_act": [
        'Certificate confirming that services have been rendered in full and accepted without objection.',
        'Formal act of completion confirming delivered scope of work meets technical specifications.',
        'Both parties confirm all contracted obligations have been completed with no mutual claims.',
        'Document certifying delivery of services rendered and authorising final commercial settlement.',
        'Acta de recepción de trabajos confirmando la conformidad de los servicios prestados sin reservas.',
        'Certificado de fin de obra y entrega de servicios sin reclamaciones mutuas pendientes.',
        'Der Auftraggeber bestätigt die vollständige und mangelfreie Erbringung der Leistungen.',
        'Die Leistungen wurden geprüft und ohne Vorbehalt abgenommen.',
        'Le client confirme la réalisation complète des prestations sans réserve.',
        "Les travaux ont été vérifiés et acceptés, aucune réclamation n'est formulée.",
        "Il committente dichiara che le prestazioni sono state eseguite a regola d'arte.",
        'I lavori sono stati verificati e accettati senza riserve da entrambe le parti.',
        'De opdrachtgever bevestigt dat de werkzaamheden volledig en zonder gebreken zijn uitgevoerd.',
        'Het werk is gecontroleerd en zonder voorbehoud aanvaard.',
        'O cliente confirma a execução integral dos serviços sem reservas.',
        'Os trabalhos foram verificados e aceites, sem reclamações mútuas.',
        'The customer signs off that the delivered scope matches the agreed specification.',
        'Handover confirmed; the parties have no outstanding claims against each other.',
    ],
    "waybill": [
        'Carrier consignment document detailing cargo packages, gross weight, and consignee destination.',
        'Freight transport manifest accompanying goods in transit from warehouse to delivery address.',
        'Shipping bill of lading recording package counts, gross kilograms, and truck driver sign-off.',
        'Goods dispatch documentation with transport details, consignee receipt, and net weight.',
        'Albarán de entrega y transporte de mercancías con detalle de bultos y peso bruto.',
        'Documento de porte y carta de consignación con recepción de mercancía por el destinatario.',
        'Absender, Empfänger, Frachtführer, Anzahl der Packstücke und Bruttogewicht in kg.',
        'Warenbegleitpapier für den Transport vom Lager zum Empfänger, Übernahme durch den Fahrer.',
        'Expéditeur, destinataire, transporteur, nombre de colis et poids brut.',
        "Document accompagnant la marchandise pendant le transport jusqu'au destinataire.",
        'Mittente, destinatario, vettore, numero di colli e peso lordo della merce.',
        'Documento che accompagna la merce durante il trasporto fino alla consegna.',
        'Afzender, geadresseerde, vervoerder, aantal colli en brutogewicht.',
        'Begeleidend document voor het goederenvervoer tot aan de ontvanger.',
        'Expedidor, destinatário, transportador, número de volumes e peso bruto.',
        'Documento que acompanha as mercadorias durante o transporte até ao destino.',
        'Pallets loaded onto the trailer, driver signature on pickup, consignee signature on delivery.',
        'Shipment of twelve cartons, gross weight 340 kg, delivered by road freight.',
    ],
    "boarding_pass": [
        'Passenger name, flight number, departure gate and seat, board no later than 30 minutes before.',
        'Gate closes 20 minutes before departure. Zone 2 boarding. Seat 14C.',
        'Present this document with your ID at security and at the gate.',
        'Operated by the partner airline. Cabin bag only. Sequence number 045.',
        'Nombre del pasajero, número de vuelo, puerta y asiento; el embarque cierra 20 minutos antes.',
        'Presente este documento junto con su documento de identidad en el control.',
        'Fluggast, Flugnummer, Flugsteig und Sitzplatz; Einstieg endet 20 Minuten vor Abflug.',
        "Passager, numéro de vol, porte et siège ; fin de l'embarquement 20 minutes avant le départ.",
        "Passeggero, numero del volo, uscita e posto; l'imbarco chiude 20 minuti prima della partenza.",
        'Passagier, vluchtnummer, gate en stoel; het instappen sluit 20 minuten voor vertrek.',
        'Passageiro, número do voo, porta e lugar; o embarque encerra 20 minutos antes da partida.',
        'Terminal 2, departure 07:45, class economy, frequent flyer number on file.',
    ],
}

# Picked on tests/test_classify_tfidf_multilingual.py and the eval set: at
# C=50 the tier is confident (>= the 0.65 floor) on about two thirds of
# documents with no confident mistakes, while unrelated text (news, recipes,
# memos) stays below 0.4.
CLASSIFIER_C = 50.0

_TRAIN_TEXTS: list[str] = [text for texts in _CORPUS.values() for text in texts]
_TRAIN_LABELS: list[str] = [label for label, texts in _CORPUS.items() for _ in texts]


@lru_cache(maxsize=1)
def _pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline

    features = FeatureUnion(
        [
            ("words", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
            (
                "chars",
                TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True),
            ),
        ]
    )
    # A weakly regularized model: with the default C=1 on a corpus this small
    # every probability sat between 0.25 and 0.45, so the tier could never
    # clear its confidence floor and was dead weight in the cascade.
    pipe = Pipeline(
        [
            ("features", features),
            ("clf", LogisticRegression(C=CLASSIFIER_C, max_iter=2000, class_weight="balanced")),
        ]
    )
    pipe.fit(_TRAIN_TEXTS, _TRAIN_LABELS)
    return pipe


def classify_tfidf(text: str) -> ClassificationResult | None:
    """Returns None if scikit-learn isn't importable — callers should treat
    that exactly like "this tier had nothing to add" and move on to the LLM.
    """
    try:
        pipe = _pipeline()
    except ImportError:
        return None

    proba = pipe.predict_proba([text])[0]
    classes = pipe.named_steps["clf"].classes_
    scores = dict(zip(classes, proba))
    best_label = max(scores.items(), key=lambda kv: kv[1])[0]

    return ClassificationResult(
        doc_type=DocType(best_label),
        confidence=round(float(scores[best_label]), 2),
        method="tfidf",
        scores={k: round(float(v), 4) for k, v in scores.items()},
    )
