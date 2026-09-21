"""Document-type classification: cheap keyword rules first, a TF-IDF model
second, an LLM call only as a last resort when neither cheaper tier is
confident. This is the three-way "pragmatic" split the job spec asks for
explicitly — see the comparison table in the README for measured
accuracy/latency/cost per tier.
"""
from __future__ import annotations

import re

from . import config, doctypes
from .classify_tfidf import classify_tfidf
from .llm_client import LLMError, chat_json
from .schemas import ClassificationResult, DocType

# (doc_type, [weighted keyword patterns])
#
# Spanish and Catalan terms carry the same weights as their English
# counterparts. Without them a "Factura" matched nothing, the TF-IDF tier is
# trained on English, and every Spanish document fell through to the most
# expensive tier — which is the opposite of the point of having tiers.
_RULES: dict[DocType, list[tuple[re.Pattern, float]]] = {
    DocType.INVOICE: [
        (re.compile(r"\binvoice\b|\bfactura\b", re.I), 3.0),
        (re.compile(r"\bbill to\b|\bfacturar a\b|\bcliente\b", re.I), 2.0),
        (re.compile(r"\bamount due\b|\bimporte total\b|\btotal a pagar\b", re.I), 2.0),
        (
            re.compile(r"\bdue date\b|\bfecha de vencimiento\b|\bvencimiento\b", re.I),
            1.0,
        ),
        (re.compile(r"\bpo number\b|\bpurchase order\b|\bpedido\b", re.I), 1.0),
        (re.compile(r"\bbase imponible\b|\bn[úu]mero de factura\b", re.I), 2.0),
    ],
    DocType.RECEIPT: [
        (
            re.compile(r"\breceipt\b|\brecibo\b|\btique\b|\bticket de compra\b", re.I),
            3.0,
        ),
        (
            re.compile(
                r"\bthank you for your purchase\b|\bgracias por su compra\b", re.I
            ),
            2.0,
        ),
        (re.compile(r"\bchange due\b|\bcambio\b|\bentregado\b", re.I), 2.0),
        (re.compile(r"\bcashier\b|\bcajero?a?\b", re.I), 1.0),
        (re.compile(r"\btender(ed)?\b|\befectivo\b", re.I), 1.0),
    ],
    DocType.BOARDING_PASS: [
        (re.compile(r"\bboarding pass\b|\btarjeta de embarque\b", re.I), 3.0),
        (re.compile(r"\bgate\b|\bpuerta de embarque\b", re.I), 2.0),
        (re.compile(r"\bseat\b|\basiento\b", re.I), 2.0),
        (re.compile(r"\bflight\b|\bvuelo\b", re.I), 2.0),
        (
            re.compile(r"\bboarding time\b|\bembarque\b|\bpnr\b|\bbooking ref", re.I),
            1.0,
        ),
    ],
    DocType.CONTRACT: [
        (re.compile(r"\bagreement\b|\bcontrato\b|\bacuerdo\b", re.I), 3.0),
        (re.compile(r"\bwhereas\b|\bexponen\b|\bmanifiestan\b", re.I), 2.0),
        (re.compile(r"\bhereby agrees?\b|\bacuerdan\b|\bcl[áa]usulas\b", re.I), 2.0),
        (
            re.compile(
                r"\bgoverning law\b|\blegislaci[óo]n aplicable\b|\bley aplicable\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bparty of the first part\b|\bthe parties\b|\blas partes\b|\bde una parte\b",
                re.I,
            ),
            1.0,
        ),
    ],
    DocType.PURCHASE_ORDER: [
        (
            re.compile(
                r"\bpurchase order\b|\border confirmation\b|\borden de compra\b", re.I
            ),
            3.0,
        ),
        (re.compile(r"\bpo number\b|\bn[úu]mero de pedido\b|\bpo #\b", re.I), 2.0),
        (
            re.compile(r"\bvendor\b|\bproveedor\b|\bship to\b|\bentregar en\b", re.I),
            2.0,
        ),
        (re.compile(r"\brequisition\b|\border date\b|\bfecha de pedido\b", re.I), 1.0),
    ],
    DocType.BANK_STATEMENT: [
        (
            re.compile(
                r"\bbank statement\b|\baccount statement\b|\bextracto bancario\b|\bвыписка\b",
                re.I,
            ),
            3.0,
        ),
        (
            re.compile(
                r"\bopening balance\b|\bclosing balance\b|\bsaldo inicial\b|\bsaldo final\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bdeposits?\b|\bwithdrawals?\b|\bmovimientos?\b|\btransacciones\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bstatement period\b|\bper[íi]odo del extracto\b|\baccount number\b",
                re.I,
            ),
            1.0,
        ),
    ],
    DocType.ACCEPTANCE_ACT: [
        (
            re.compile(
                r"\bacceptance act\b|\bact of acceptance\b|\bcertificate of acceptance\b|\bакт выполненных работ\b|\bакт приема\b",
                re.I,
            ),
            3.0,
        ),
        (
            re.compile(
                r"\bservices rendered\b|\bservicios prestados\b|\btrabajos realizados\b|\bacta de recepci[óo]n\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bno mutual claims\b|\bsin reclamaciones\b|\bпретензий не имеют\b|\bwork completed\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bcontractor\b|\bcontratista\b|\bподрядчик\b|\bзаказчик\b", re.I
            ),
            1.0,
        ),
    ],
    DocType.WAYBILL: [
        (
            re.compile(
                r"\bwaybill\b|\bbill of lading\b|\bconsignment note\b|\bcmr\b|\bтоварная накладная\b|\bторг-12\b|\balbar[áa]n\b",
                re.I,
            ),
            3.0,
        ),
        (
            re.compile(
                r"\bconsignee\b|\bconsignor\b|\bshipper\b|\bdestinatario\b|\bremitente\b|\bгрузополучатель\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bgross weight\b|\bnet weight\b|\bpeso bruto\b|\bpeso neto\b|\bвес брутто\b",
                re.I,
            ),
            2.0,
        ),
        (
            re.compile(
                r"\bcarrier\b|\btransportista\b|\bcarrier tracking\b|\bvehicle\b|\bveh[íi]culo\b",
                re.I,
            ),
            1.0,
        ),
    ],
}

# If the top score isn't at least this many points clear of the runner-up,
# the rules are ambiguous and we defer to the LLM instead of guessing.
_CONFIDENCE_MARGIN = 2.0

_LLM_PROMPT = """You classify business documents. Read the text below and
respond with a JSON object: {{"doc_type": {choices}, "confidence": 0-1}}.

Document types:
{descriptions}
- unknown: none of the above

Text:
---
{text}
---
"""


def _llm_prompt(text: str) -> str:
    types = doctypes.list_document_types()
    choices = "|".join(f'"{t.name}"' for t in types) + '|"unknown"'
    descriptions = "\n".join(f"- {t.name}: {t.description}" for t in types)
    return _LLM_PROMPT.format(choices=choices, descriptions=descriptions, text=text)


def _name(doc_type: DocType | str) -> str:
    return doc_type.value if isinstance(doc_type, DocType) else doc_type


def _score(text: str) -> dict[DocType | str, float]:
    rules: dict[DocType | str, list[tuple[re.Pattern, float]]] = dict(_RULES)
    for custom in doctypes.custom_document_types():
        rules[custom.name] = list(custom.keywords)
    scores: dict[DocType | str, float] = {dt: 0.0 for dt in rules}
    for doc_type, patterns in rules.items():
        for pattern, weight in patterns:
            if pattern.search(text):
                scores[doc_type] += weight
    return scores


def classify_rules(text: str) -> ClassificationResult | None:
    """Tier 1: free, deterministic. Returns None if the keyword scores
    don't produce a clear winner — that's "ambiguous", not "unknown".
    """
    scores = _score(text)
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_type, top_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if top_score > 0 and (top_score - runner_up_score) >= _CONFIDENCE_MARGIN:
        total = sum(scores.values()) or 1.0
        return ClassificationResult(
            doc_type=top_type,
            confidence=round(top_score / total, 2),
            method="rules",
            scores={_name(k): v for k, v in scores.items()},
        )
    return None


def classify_llm(text: str) -> ClassificationResult:
    """Tier 3: the expensive fallback. Always returns a result — "unknown"
    with low confidence is a valid answer, not a failure.
    """
    scores = _score(text)
    chunk_size = max(1000, config.EXTRACT_CHUNK_CHARS)
    chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]
    results = [chat_json(_llm_prompt(chunk)) for chunk in chunks]
    if len(results) == 1:
        result = results[0]
        doc_type = doctypes.parse_type(result.get("doc_type"))
        confidence = float(result.get("confidence", 0.0))
    else:
        votes: dict[DocType | str, float] = {}
        for result in results:
            candidate = doctypes.parse_type(result.get("doc_type"))
            votes[candidate] = votes.get(candidate, 0.0) + float(
                result.get("confidence", 0.0)
            )
        doc_type = max(votes, key=votes.get)
        total = sum(votes.values())
        confidence = votes[doc_type] / total if total else 0.0
    return ClassificationResult(
        doc_type=doc_type,
        confidence=confidence,
        method="llm",
        scores={_name(k): v for k, v in scores.items()},
    )


def classify(text: str) -> ClassificationResult:
    """The pragmatic cascade: rules -> TF-IDF -> LLM, each tier only
    running because the previous one wasn't confident enough to trust.

    If the LLM tier is unreachable, the cascade falls back to whatever the
    TF-IDF tier said, below-floor confidence and all. That confidence is
    what routes the document to human review — a hedged guess a person will
    look at is more useful than a failed document.
    """
    rules_result = classify_rules(text)
    if rules_result is not None:
        return rules_result

    # The TF-IDF model was trained on the built-in types only. With custom
    # types registered it would confidently file a delivery note as a
    # waybill, so the cascade goes straight to the LLM, which is told
    # about every registered type.
    tfidf_result = None if doctypes.custom_document_types() else classify_tfidf(text)
    if (
        tfidf_result is not None
        and tfidf_result.confidence >= config.TFIDF_CONFIDENCE_FLOOR
    ):
        return tfidf_result

    try:
        return classify_llm(text)
    except LLMError:
        if tfidf_result is not None:
            return tfidf_result
        return ClassificationResult(
            doc_type=DocType.UNKNOWN,
            confidence=0.0,
            method="unavailable",
            scores={_name(k): v for k, v in _score(text).items()},
        )
