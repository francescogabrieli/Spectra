from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time

from spectra import ml_classifier


def test_classifier_reuses_identical_training_data_and_invalidates_on_feedback(monkeypatch) -> None:
    ml_classifier.clear_classifier_cache()
    calls: list[list[object]] = []

    def train(training_data):
        model = object()
        calls.append(list(training_data or []))
        return model

    monkeypatch.setattr(ml_classifier, "_train_classifier_uncached", train)
    training_data = [{"combined_text": "MARKET", "clean_name": "MARKET", "category": "Food"}]

    first = ml_classifier.train_classifier(training_data)
    second = ml_classifier.train_classifier(training_data)
    assert first is second
    assert len(calls) == 1

    changed = training_data + [
        {"combined_text": "TRAIN", "clean_name": "TRAIN", "category": "Transport"}
    ]
    third = ml_classifier.train_classifier(changed)
    assert third is not first
    assert len(calls) == 2
    assert ml_classifier.classifier_cache_info() == {"size": 2, "hits": 1, "misses": 2}

    ml_classifier.clear_classifier_cache()


def test_concurrent_imports_train_once_for_same_feedback(monkeypatch) -> None:
    ml_classifier.clear_classifier_cache()
    calls = 0
    calls_lock = Lock()
    model = object()

    def train(_training_data):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        return model

    monkeypatch.setattr(ml_classifier, "_train_classifier_uncached", train)
    training_data = [("SHOP", "Groceries")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(ml_classifier.train_classifier, [training_data] * 4))

    assert calls == 1
    assert all(result is model for result in results)
    assert ml_classifier.classifier_cache_info() == {"size": 1, "hits": 3, "misses": 1}
    ml_classifier.clear_classifier_cache()
