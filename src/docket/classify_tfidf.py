"""Second classification tier: a TF-IDF + logistic regression model.

Sits between the free keyword rules and the LLM call. It's the "lightweight
classifier" the job spec asks for by name, and it exists to answer a
specific question the rules can't: what about documents that are the right
type but phrased in a way the regexes don't cover (no literal "invoice" or
"agreement" on the page)? A few hundred KB of TF-IDF features and a linear
model catch a lot of that for a fraction of an LLM call's latency and cost.

The training set is a small, hand-written corpus embedded directly in this
module — no external file, no network call, trains in milliseconds. That
keeps the model reproducible and the repo self-contained, at the cost of
being weaker than a model trained on thousands of labeled examples; the
comparison table in the README reports that tradeoff honestly rather than
hiding it.
"""
from __future__ import annotations

from functools import lru_cache

from .schemas import ClassificationResult, DocType

# Deliberately paraphrased, not keyword-matched — if these used the same
# words as _RULES in classify.py, this tier would just be a slower copy of
# the rules tier instead of adding anything.
_TRAIN_TEXTS: list[str] = [
    # invoice
    "Please remit payment within 30 days of the statement date to the account below.",
    "Balance owed for services rendered last month, itemized by hour and rate.",
    "Statement of charges for goods shipped on the attached purchase order.",
    "Payment terms: net 15. Late payments accrue 1.5% monthly interest.",
    "Itemized charges for consulting hours delivered in Q3, billed to your account.",
    "Remittance advice: please reference the order number when wiring funds.",
    "Outstanding balance for the delivered units, plus applicable sales tax.",
    "Kindly settle the enclosed statement before the remittance deadline.",
    "Charges this billing cycle for hosting, support, and add-on seats.",
    "This statement reflects goods delivered and is payable upon receipt.",
    "Sold to the below account; total reflects unit price times quantity shipped.",
    "Recurring subscription charge for the current billing period, auto-renewed.",
    "Le rogamos abone el importe indicado en un plazo de treinta días naturales.",
    "Detalle de los servicios prestados durante el mes, con su base imponible.",
    "Documento acreditativo de la operación comercial, sujeto al impuesto correspondiente.",
    "Importe pendiente por las mercancías entregadas, más el impuesto aplicable.",
    # receipt
    "Here's your copy of today's purchase, keep it in case you need to return anything.",
    "Card ending 4477 approved. Have a nice day!",
    "Two items scanned at the register, change given in cash.",
    "Your order was rung up at the counter and paid for on the spot.",
    "Loyalty points earned on this visit will post to your account within 24 hours.",
    "Transaction approved — signature not required for purchases under the limit.",
    "Sale completed at register 3, cashier badge #12, till closed at close of shift.",
    "Everything scanned fine, here's what you paid and how much change you got back.",
    "In-store purchase, tax included, paid by contactless card at checkout.",
    "Print this slip for your records; returns accepted within 14 days with it.",
    "Groceries rung up one by one, coupon applied at the end before tender.",
    "Drive-thru order completed, total charged to the card on file.",
    "Justificante de la compra realizada hoy en el establecimiento, conserve este papel.",
    "Operación autorizada con tarjeta, no se requiere firma para este importe.",
    "Artículos pasados por caja y abonados en el momento, con el cambio entregado.",
    "Comprobante de la venta efectuada en el punto de venta, impuesto incluido.",
    # contract
    "Both signatories bind their respective organizations to the terms set out herein.",
    "Either side may terminate this arrangement with 60 days' written notice.",
    "This document sets out the mutual obligations of the two undersigned entities.",
    "Confidential information disclosed under this arrangement may not be shared with third parties.",
    "The undersigned entities agree to the terms and conditions set forth below.",
    "Disputes arising under this arrangement shall be resolved by binding arbitration.",
    "This instrument shall remain in force until superseded by a later signed version.",
    "Each signatory represents that it has full authority to enter into this arrangement.",
    "Upon breach, the non-breaching entity may pursue remedies available at law.",
    "This document is binding upon the successors and assigns of both entities.",
    "Renewal of this arrangement requires written consent from both signatories.",
    "Nothing herein shall be construed as creating a partnership between the entities.",
    "Ambas entidades firmantes quedan obligadas por lo estipulado en el presente documento.",
    "Cualquiera de las partes podrá resolverlo mediante preaviso por escrito.",
    "La información confidencial revelada no podrá comunicarse a terceros ajenos.",
    "Las controversias se resolverán conforme a la legislación vigente aplicable.",
]
_TRAIN_LABELS: list[str] = (
    ["invoice"] * 16 + ["receipt"] * 16 + ["contract"] * 16
)


@lru_cache(maxsize=1)
def _pipeline():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer

    pipe = Pipeline(
        [
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
            ("clf", LogisticRegression(max_iter=1000)),
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
