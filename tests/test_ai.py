"""Tests for the AI categoriser — prompt building and JSON parsing."""

from spectra.ai import (
    CategorisedTransaction,
    _build_user_prompt,
    _extract_json,
)


class TestBuildUserPrompt:
    """Tests for _build_user_prompt."""

    def test_includes_existing_categories(self) -> None:
        prompt = _build_user_prompt(
            transactions=[
                {"raw_description": "POS STARBUCKS", "amount": -4.5, "currency": "EUR", "date": "2026-02-22"}
            ],
            existing_categories=["Bar & Caffè", "Supermercato"],
        )
        assert '"Bar & Caffè"' in prompt
        assert '"Supermercato"' in prompt

    def test_no_categories_shows_placeholder(self) -> None:
        prompt = _build_user_prompt(
            transactions=[
                {"raw_description": "TEST", "amount": -1, "currency": "EUR", "date": "2026-02-22"}
            ],
            existing_categories=[],
        )
        assert "(nessuna ancora)" in prompt

    def test_includes_transaction_details(self) -> None:
        prompt = _build_user_prompt(
            transactions=[
                {"raw_description": "ESSELUNGA 412", "amount": -62.30, "currency": "EUR", "date": "2026-02-22"}
            ],
            existing_categories=[],
        )
        assert "ESSELUNGA 412" in prompt
        assert "-62.3" in prompt

    def test_includes_counterpart_when_available(self) -> None:
        prompt = _build_user_prompt(
            transactions=[
                {
                    "raw_description": "SEPA TRANSFER",
                    "counterpart": "Andre Silva",
                    "amount": -42.30,
                    "currency": "EUR",
                    "date": "2026-02-22",
                }
            ],
            existing_categories=[],
        )
        assert 'controparte: "Andre Silva"' in prompt


class TestExtractJson:
    """Tests for robust JSON extraction from LLM output."""

    def test_clean_array(self) -> None:
        raw = '[{"original": "POS", "clean_name": "Starbucks", "category": "Bar", "amount": -4.5}]'
        result = _extract_json(raw)
        assert len(result) == 1
        assert result[0]["clean_name"] == "Starbucks"

    def test_markdown_wrapped(self) -> None:
        raw = '```json\n[{"original": "POS", "clean_name": "Test", "category": "X", "amount": -1}]\n```'
        result = _extract_json(raw)
        assert len(result) == 1

    def test_dict_with_transactions_key(self) -> None:
        raw = '{"transactions": [{"original": "A", "clean_name": "B", "category": "C", "amount": -1}]}'
        result = _extract_json(raw)
        assert len(result) == 1

    def test_single_dict(self) -> None:
        raw = '{"original": "A", "clean_name": "B", "category": "C", "amount": -1}'
        result = _extract_json(raw)
        assert len(result) == 1

    def test_garbage_returns_empty(self) -> None:
        result = _extract_json("this is not json at all")
        assert result == []

    def test_empty_array(self) -> None:
        assert _extract_json("[]") == []
