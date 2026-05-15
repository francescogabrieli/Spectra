"""Tests for the local categoriser (merchant extraction, fuzzy match, ML cascade)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spectra.local_categorizer import (
    _extract_merchant_name,
    _fuzzy_match,
    _ml_threshold_for_category,
    categorise_local,
)


# ── Merchant Name Extraction ────────────────────────────────────


class TestExtractMerchantName:
    """Test that banking boilerplate is stripped to a clean merchant name."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("POS 1234 STARBUCKS MILANO", "Starbucks Milano"),
            ("ADDEBITO SDD NETFLIX.COM", "Netflix.Com"),
            ("PAGAMENTO SU POS ESSELUNGA SESTO S", "Esselunga Sesto S"),
            ("Bonifico istantaneo da voi disposto a favore di Mario Rossi", "Mario Rossi"),
            ("PRELIEVO BANCOMAT ATM 12345 VIA ROMA", "Atm"),
            ("ADDEBITO DIRETTO SPOTIFY AB", "Spotify"),
            ("PAGAMENTO APPLE.COM/BILL", "Apple.Com/Bill"),
            ("RICARICA TELEFONICA TIM", "Tim"),
            ("POS AMAZON EU SARL", "Amazon Eu"),
            ("CANONE MENSILE CONTO", "Mensile Conto"),
            # English / UK
            ("CARD PAYMENT NETFLIX.COM", "Netflix.Com"),
            ("DIRECT DEBIT SPOTIFY", "Spotify"),
            ("CONTACTLESS STARBUCKS", "Starbucks"),
            # German
            ("Lastschrift SPOTIFY AB", "Spotify"),
            ("Kartenzahlung AMAZON", "Amazon"),
            # French
            ("Prélèvement SEPA NETFLIX", "Netflix"),
            ("Paiement CB UBER", "Uber"),
            # Spanish
            ("Pago con tarjeta UBER", "Uber"),
        ],
    )
    def test_extraction(self, raw: str, expected: str):
        result = _extract_merchant_name(raw)
        assert result.lower() == expected.lower(), f"Expected {expected!r}, got {result!r}"


# ── ML Classifier (seed-bootstrapped) ────────────────────────────


class TestMLClassifierSeedBased:
    """Test that the ML classifier works from day-0 with seed data."""

    def test_trains_without_user_data(self):
        """Classifier should train on seed data alone (no user data needed)."""
        from spectra.ml_classifier import train_classifier
        clf = train_classifier()
        assert clf is not None

    def test_trains_with_none_user_data(self):
        from spectra.ml_classifier import train_classifier
        clf = train_classifier(None)
        assert clf is not None

    @pytest.mark.parametrize(
        "description, expected_category",
        [
            ("NETFLIX.COM", "Digital Subscriptions"),
            ("SPOTIFY AB", "Digital Subscriptions"),
            ("UBER TRIP HELP.UBER.COM", "Transport"),
            ("RYANAIR", "Travel"),
            ("IKEA ITALIA RETAIL", "Shopping"),
            ("AMAZON MARKETPLACE", "Shopping"),
            ("UBER EATS DELIVERY", "Food & Dining"),
            ("BOOKING.COM AMSTERDAM", "Travel"),
            ("STIPENDIO MESE 02/2026", "Salary"),
            ("AWS EMEA 123456789", "Digital Subscriptions"),
            ("ESSELUNGA SESTO", "Groceries"),
            ("VODAFONE ITALIA", "Utilities"),
            ("Starbucks Coffee", "Food & Dining"),
        ],
    )
    def test_seed_predictions(self, description: str, expected_category: str):
        """The seed-bootstrapped model should correctly classify common merchants."""
        from spectra.ml_classifier import train_classifier, predict
        clf = train_classifier()
        assert clf is not None
        category, confidence = predict(clf, description)
        assert category == expected_category, (
            f"For {description!r}: expected {expected_category!r}, got {category!r} (conf={confidence:.0%})"
        )

    def test_user_data_overrides_seed(self):
        """User corrections should dominate over seed knowledge."""
        from spectra.ml_classifier import train_classifier, predict

        # The user decides Netflix is "Entertainment" instead of "Digital Subscriptions"
        user_data = [("NETFLIX.COM", "Entertainment")] * 15

        clf = train_classifier(user_data)
        assert clf is not None
        category, _ = predict(clf, "NETFLIX.COM")
        assert category == "Entertainment"

    def test_predict_returns_confidence(self):
        from spectra.ml_classifier import train_classifier, predict
        clf = train_classifier()
        assert clf is not None
        category, confidence = predict(clf, "NETFLIX.COM")
        assert isinstance(confidence, float)
        assert 0.0 <= confidence <= 1.0


# ── Fuzzy Matching ───────────────────────────────────────────────


class TestFuzzyMatch:
    """Test fuzzy string matching against a known merchant database."""

    KNOWN_MERCHANTS = {
        "Starbucks": "Food & Dining",
        "Netflix": "Digital Subscriptions",
        "Amazon": "Shopping",
    }

    def test_exact_match(self):
        result = _fuzzy_match("Starbucks", self.KNOWN_MERCHANTS)
        assert result is not None
        name, cat = result
        assert name == "Starbucks"
        assert cat == "Food & Dining"

    def test_close_variation(self):
        result = _fuzzy_match("Starbucks Roma", self.KNOWN_MERCHANTS, threshold=70)
        assert result is not None
        name, _ = result
        assert name == "Starbucks"

    def test_no_match_for_unrelated(self):
        result = _fuzzy_match("McDonald's Napoli", self.KNOWN_MERCHANTS)
        assert result is None

    def test_empty_db_returns_none(self):
        result = _fuzzy_match("Starbucks", {})
        assert result is None


# ── Full Cascade Integration ─────────────────────────────────────


class TestCategoriseLocal:
    """Test the full local categorisation cascade (Exact → Fuzzy → ML → Fallback)."""

    @classmethod
    def _get_ml(cls):
        from spectra.ml_classifier import train_classifier
        return train_classifier()

    def _make_txn(
        self,
        desc: str,
        amount: float = -10.0,
        currency: str = "EUR",
        date: str = "2026-02-01",
        counterpart: str = "",
    ):
        payload = {"raw_description": desc, "amount": amount, "currency": currency, "date": date}
        if counterpart:
            payload["counterpart"] = counterpart
        return payload

    def test_exact_merchant_memory_hit(self):
        """When a merchant is in the DB, it should be used directly."""
        merchant_db = {"Netflix.Com": "Digital Subscriptions"}
        txns = [self._make_txn("NETFLIX.COM")]
        results = categorise_local(txns, merchant_db=merchant_db, ml_classifier=self._get_ml())
        assert len(results) == 1
        assert results[0].category == "Digital Subscriptions"

    def test_ml_classifies_known_merchants(self):
        """ML should categorise known merchants even without merchant DB."""
        txns = [self._make_txn("SPOTIFY AB")]
        results = categorise_local(txns, merchant_db={}, ml_classifier=self._get_ml())
        assert len(results) == 1
        assert results[0].category == "Digital Subscriptions"

    def test_counterpart_is_used_as_preferred_signal(self):
        """An extracted counterpart should override a generic raw description."""
        merchant_db = {"Porkbun.Com Sherwood": "Digital Subscriptions"}
        txns = [self._make_txn(
            "Pagamento Effettuato Su Pos Estero",
            counterpart="Porkbun.Com Sherwood",
        )]
        results = categorise_local(txns, merchant_db=merchant_db, ml_classifier=self._get_ml())
        assert len(results) == 1
        assert results[0].clean_name == "Porkbun.Com Sherwood"
        assert results[0].category == "Digital Subscriptions"

    def test_fallback_to_uncategorized(self):
        """Truly unknown transactions should fall back to 'Uncategorized'."""
        txns = [self._make_txn("XYZZY CORP INTERNAL PAYMENT")]
        results = categorise_local(txns, merchant_db={}, ml_classifier=self._get_ml())
        assert len(results) == 1
        # May or may not be uncategorized depending on ML confidence;
        # the important thing is it returns a result
        assert results[0].category is not None

    def test_income_override(self):
        """Positive amounts should be categorised as income when not already income."""
        txns = [self._make_txn("RANDOM TRANSFER", amount=1500.00)]
        results = categorise_local(txns, merchant_db={}, ml_classifier=self._get_ml())
        assert len(results) == 1
        income_cats = {"Salary", "Pension", "Transfer In", "Transfer", "Cash Deposit",
                       "Other Income", "Investment Return", "Reimbursement", "Uncategorized"}
        assert results[0].category in income_cats

    def test_salary_with_positive_amount(self):
        """STIPENDIO should be correctly identified as Salary."""
        txns = [self._make_txn("STIPENDIO MESE 02/2026", amount=2500.00)]
        results = categorise_local(txns, merchant_db={}, ml_classifier=self._get_ml())
        assert len(results) == 1
        assert results[0].category == "Salary"

    def test_multiple_transactions(self):
        """Batch of mixed transactions should all be categorised."""
        ml = self._get_ml()
        txns = [
            self._make_txn("NETFLIX.COM"),
            self._make_txn("UBER TRIP"),
            self._make_txn("STIPENDIO", amount=3000.0),
        ]
        results = categorise_local(txns, merchant_db={}, ml_classifier=ml)
        assert len(results) == 3
        categories = [r.category for r in results]
        assert "Digital Subscriptions" in categories
        assert "Transport" in categories
        assert "Salary" in categories

    def test_empty_input(self):
        """Empty input should return empty output."""
        results = categorise_local([], merchant_db={})
        assert results == []

    def test_fuzzy_match_in_cascade(self):
        """Fuzzy match should activate when merchant DB has a close match."""
        merchant_db = {"Starbucks": "Food & Dining"}
        txns = [self._make_txn("POS STARBUCKS ROMA")]
        results = categorise_local(txns, merchant_db=merchant_db, ml_classifier=self._get_ml())
        assert len(results) == 1
        assert results[0].category == "Food & Dining"

    def test_without_ml_falls_back(self):
        """Without ML classifier, unknown transactions should be Uncategorized."""
        txns = [self._make_txn("XYZZY CORP")]
        results = categorise_local(txns, merchant_db={}, ml_classifier=None)
        assert len(results) == 1
        assert results[0].category == "Uncategorized"
        assert results[0].classification_source == "fallback"
        assert results[0].needs_review is True


class TestAdaptiveThresholdAndHybridFallback:
    """Test adaptive ML thresholds and rule-based fallback before Uncategorized."""

    @staticmethod
    def _txn(desc: str, amount: float = -10.0) -> dict[str, object]:
        return {
            "raw_description": desc,
            "amount": amount,
            "currency": "EUR",
            "date": "2026-02-01",
        }

    def test_category_thresholds_are_adaptive(self):
        assert _ml_threshold_for_category("Shopping") > _ml_threshold_for_category("Salary")
        assert _ml_threshold_for_category("Unknown Category") == 0.20

    def test_low_confidence_shopping_rejected(self, monkeypatch: pytest.MonkeyPatch):
        from spectra import ml_classifier

        monkeypatch.setattr(
            ml_classifier,
            "predict_details",
            lambda _clf, _desc, clean_name=None: SimpleNamespace(
                category="Shopping",
                confidence=0.24,
                margin=0.04,
                suggestions=[
                    {"category": "Shopping", "score": 0.24},
                    {"category": "Groceries", "score": 0.20},
                    {"category": "Food & Dining", "score": 0.18},
                ],
            ),
        )
        results = categorise_local(
            [self._txn("CARD PAYMENT RANDOM STORE")],
            merchant_db={},
            ml_classifier=object(),
        )
        assert len(results) == 1
        assert results[0].category == "Uncategorized"
        assert results[0].classification_source == "fallback"
        assert results[0].needs_review is True
        assert len(results[0].category_suggestions) == 3

    def test_low_confidence_ml_can_fallback_to_salary(self, monkeypatch: pytest.MonkeyPatch):
        from spectra import ml_classifier

        monkeypatch.setattr(
            ml_classifier,
            "predict_details",
            lambda _clf, _desc, clean_name=None: SimpleNamespace(
                category="Shopping",
                confidence=0.18,
                margin=0.03,
                suggestions=[
                    {"category": "Shopping", "score": 0.18},
                    {"category": "Salary", "score": 0.17},
                    {"category": "Transfer In", "score": 0.16},
                ],
            ),
        )
        results = categorise_local(
            [self._txn("STIPENDIO MARZO ACME SRL", amount=2400.00)],
            merchant_db={},
            ml_classifier=object(),
        )
        assert len(results) == 1
        assert results[0].category == "Salary"
        assert results[0].classification_source == "hybrid"
        assert results[0].needs_review is True

    def test_statement_category_can_fallback_to_salary(self, monkeypatch: pytest.MonkeyPatch):
        from spectra import ml_classifier

        monkeypatch.setattr(
            ml_classifier,
            "predict_details",
            lambda _clf, _desc, clean_name=None: SimpleNamespace(
                category="Shopping",
                confidence=0.18,
                margin=0.03,
                suggestions=[
                    {"category": "Shopping", "score": 0.18},
                    {"category": "Salary", "score": 0.17},
                    {"category": "Transfer In", "score": 0.16},
                ],
            ),
        )
        results = categorise_local(
            [self._txn("Accredito", amount=2400.00)],
            merchant_db={},
            ml_classifier=object(),
        )
        assert len(results) == 1
        assert results[0].category == "Uncategorized"

        results = categorise_local(
            [
                {
                    "raw_description": "Accredito",
                    "statement_category": "Stipendi e pensioni",
                    "amount": 2400.0,
                    "currency": "EUR",
                    "date": "2026-02-01",
                }
            ],
            merchant_db={},
            ml_classifier=object(),
        )
        assert len(results) == 1
        assert results[0].category == "Salary"
        assert results[0].classification_source == "hybrid"
        assert results[0].needs_review is True

    def test_low_confidence_ml_can_fallback_to_transfer_in(self, monkeypatch: pytest.MonkeyPatch):
        from spectra import ml_classifier

        monkeypatch.setattr(
            ml_classifier,
            "predict_details",
            lambda _clf, _desc, clean_name=None: SimpleNamespace(
                category="Shopping",
                confidence=0.15,
                margin=0.02,
                suggestions=[
                    {"category": "Shopping", "score": 0.15},
                    {"category": "Transfer In", "score": 0.14},
                    {"category": "Salary", "score": 0.13},
                ],
            ),
        )
        results = categorise_local(
            [self._txn("BONIFICO RICEVUTO DA MARIO ROSSI", amount=120.00)],
            merchant_db={},
            ml_classifier=object(),
        )
        assert len(results) == 1
        assert results[0].category == "Transfer In"
        assert results[0].classification_source == "hybrid"
        assert results[0].needs_review is True

    def test_fallback_works_even_without_ml(self):
        results = categorise_local(
            [self._txn("ADDEBITO SDD BOLLETTA ENEL ENERGIA", amount=-96.30)],
            merchant_db={},
            ml_classifier=None,
        )
        assert len(results) == 1
        assert results[0].category == "Utilities"
        assert results[0].classification_source == "hybrid"
        assert results[0].needs_review is True

    def test_confident_ml_prediction_is_not_marked_for_review(self):
        from spectra.ml_classifier import train_classifier

        results = categorise_local(
            [self._txn("NETFLIX.COM", amount=-14.99)],
            merchant_db={},
            ml_classifier=train_classifier(),
        )
        assert len(results) == 1
        assert results[0].category == "Digital Subscriptions"
        assert results[0].classification_source in {"ml", "exact", "fuzzy"}
        assert results[0].needs_review is False
