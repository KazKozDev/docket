"""Document-type classification: cheap keyword rules first, a TF-IDF model
second, an LLM call only as a last resort when neither cheaper tier is
confident.

Every tier reads the schema catalog: the rules use each schema's weighted
keywords, TF-IDF trains on each schema's example sentences, and the LLM is
shown each schema's description. A registered custom schema takes part in
all three the moment it is registered.
"""
from __future__ import annotations

from . import catalog, config
from .classify_tfidf import classify_tfidf
from .llm_client import LLMError, chat_json
from .schemas import ClassificationResult

# If the top score isn't at least this many points clear of the runner-up,
# the rules are ambiguous and we defer to the next tier instead of guessing.
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
    specs = catalog.list_schemas()
    choices = "|".join(f'"{s.schema_id}"' for s in specs) + '|"unknown"'
    descriptions = "\n".join(f"- {s.schema_id}: {s.description}" for s in specs)
    return _LLM_PROMPT.format(choices=choices, descriptions=descriptions, text=text)


def _score(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for spec in catalog.list_schemas():
        scores[spec.schema_id] = sum(k.weight for k in spec.keywords if k.pattern.search(text))
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
            scores=scores,
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
        doc_type = catalog.parse_type(result.get("doc_type"))
        confidence = float(result.get("confidence", 0.0))
    else:
        votes: dict[str, float] = {}
        for result in results:
            candidate = catalog.parse_type(result.get("doc_type"))
            votes[candidate] = votes.get(candidate, 0.0) + float(
                result.get("confidence", 0.0)
            )
        doc_type = max(votes, key=votes.__getitem__)
        total = sum(votes.values())
        confidence = votes[doc_type] / total if total else 0.0
    return ClassificationResult(
        doc_type=doc_type,
        confidence=confidence,
        method="llm",
        scores=scores,
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

    # classify_tfidf answers None when some registered schema brought no
    # example sentences: a model that has never seen a type would file it
    # confidently under a neighbour, so the cascade goes to the LLM instead.
    tfidf_result = classify_tfidf(text)
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
            doc_type=catalog.UNKNOWN, confidence=0.0, method="unavailable", scores=_score(text)
        )
