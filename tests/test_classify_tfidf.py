from docket.classify_tfidf import classify_tfidf


def test_returns_a_classification_result():
    result = classify_tfidf(
        "Please remit payment for services rendered within 30 days."
    )
    assert result is not None
    assert result.method == "tfidf"
    assert 0.0 <= result.confidence <= 1.0


def test_scores_sum_to_roughly_one():
    result = classify_tfidf("Here is your receipt, thanks for shopping with us today.")
    assert result is not None
    assert abs(sum(result.scores.values()) - 1.0) < 1e-3


def test_doc_type_is_one_of_the_three_known_types():
    result = classify_tfidf(
        "The parties hereby agree to the obligations set out below."
    )
    assert result is not None
    assert result.doc_type in {"invoice", "receipt", "contract"}
