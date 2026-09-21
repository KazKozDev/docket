"""Second classification tier: a TF-IDF + logistic regression model.

Sits between the free keyword rules and the LLM call, and exists to answer a
question the rules can't: what about documents that are the right type but
phrased in a way the regexes don't cover (no literal "invoice" or "agreement"
on the page)? A linear model over word and character n-grams catches a lot of
that for a fraction of an LLM call's latency and cost.

The training set is the example sentences each registered schema carries
(`SchemaSpec.examples`; the built-in ones are in `catalog/corpus.py`):
paraphrases in English, Spanish, German, French, Italian, Dutch and
Portuguese, roughly 12-26 per document type. No external file, no network
call, trains in well under a second. That keeps the model reproducible and
the repo self-contained, at the cost of being weaker than a model trained on
thousands of labeled documents. Character n-grams carry most of the weight
across languages and survive OCR noise better than whole words.

The tier only answers when its confidence clears
`config.TFIDF_CONFIDENCE_FLOOR`; below that the cascade asks the LLM.
"""
from __future__ import annotations

from functools import lru_cache

from . import catalog
from .schemas import ClassificationResult

# Picked on tests/test_classify_tfidf_multilingual.py and the eval set: at
# C=50 the tier is confident (>= the 0.65 floor) on about two thirds of
# documents with no confident mistakes, while unrelated text (news, recipes,
# memos) stays below 0.4.
CLASSIFIER_C = 50.0

def _training_set() -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """(texts, labels) from every registered schema's examples, or None if a
    registered schema has none — the model would then confidently file that
    type under a neighbour."""
    texts: list[str] = []
    labels: list[str] = []
    for spec in catalog.list_schemas():
        if not spec.examples:
            return None
        texts.extend(spec.examples)
        labels.extend([spec.schema_id] * len(spec.examples))
    return tuple(texts), tuple(labels)


@lru_cache(maxsize=4)
def _pipeline(texts: tuple[str, ...], labels: tuple[str, ...]):
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
    pipe.fit(list(texts), list(labels))
    return pipe


def classify_tfidf(text: str) -> ClassificationResult | None:
    """Returns None if scikit-learn isn't importable — callers should treat
    that exactly like "this tier had nothing to add" and move on to the LLM.
    """
    training = _training_set()
    if training is None:
        return None
    try:
        pipe = _pipeline(*training)
    except ImportError:
        return None

    proba = pipe.predict_proba([text])[0]
    classes = pipe.named_steps["clf"].classes_
    scores = dict(zip(classes, proba))
    best_label = max(scores.items(), key=lambda kv: kv[1])[0]

    return ClassificationResult(
        doc_type=best_label,
        confidence=round(float(scores[best_label]), 2),
        method="tfidf",
        scores={k: round(float(v), 4) for k, v in scores.items()},
    )
