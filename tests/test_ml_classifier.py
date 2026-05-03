from __future__ import annotations

from spectra.ml_classifier import (
    build_training_examples,
    predict_details,
    train_classifier,
)


def test_build_training_examples_prefers_user_override_over_lower_priority_sources() -> None:
    examples = build_training_examples(
        [
            {
                "raw_description": "NETFLIX.COM",
                "clean_name": "Netflix",
                "category": "Shopping",
                "label_source": "tx_history",
            },
            {
                "raw_description": "NETFLIX.COM",
                "clean_name": "Netflix",
                "category": "Entertainment",
                "label_source": "merchant_memory",
            },
            {
                "raw_description": "NETFLIX.COM",
                "clean_name": "Netflix",
                "category": "Education",
                "label_source": "user_override",
            },
        ]
    )

    netflix = next(example for example in examples if example.raw_description == "NETFLIX.COM")
    assert netflix.category == "Education"
    assert netflix.label_source == "user_override"


def test_build_training_examples_prefers_merchant_memory_over_tx_history() -> None:
    examples = build_training_examples(
        [
            {
                "raw_description": "STARBUCKS",
                "clean_name": "Starbucks",
                "category": "Shopping",
                "label_source": "tx_history",
            },
            {
                "raw_description": "",
                "clean_name": "Starbucks",
                "category": "Food & Dining",
                "label_source": "merchant_memory",
            },
        ]
    )

    starbucks = next(
        example
        for example in examples
        if example.label_source == "merchant_memory" and example.clean_name == "Starbucks"
    )
    assert starbucks.category == "Food & Dining"
    assert starbucks.label_source == "merchant_memory"


def test_predict_details_returns_ranked_top_suggestions() -> None:
    clf = train_classifier()
    assert clf is not None

    result = predict_details(clf, "NETFLIX.COM")
    assert result.category == "Digital Subscriptions"
    assert len(result.suggestions) == 3
    assert result.suggestions[0].category == "Digital Subscriptions"
    assert result.suggestions[0].score >= result.suggestions[1].score >= result.suggestions[2].score
