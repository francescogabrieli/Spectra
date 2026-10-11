"""Tests for the AI categoriser — prompt building and JSON parsing."""

from spectra.ai import (
    _build_user_prompt,
    _extract_json,
    categorise,
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


def test_model_can_only_enrich_source_rows(monkeypatch) -> None:
    import json
    import spectra.ai

    monkeypatch.setattr(
        spectra.ai,
        "_call_gemini",
        lambda *_args: json.dumps([
            {
                "id": "source-row",
                "original": "BANK DESCRIPTION",
                "clean_name": "Market",
                "category": "Food",
                "amount": -90,
                "currency": "EUR",
                "date": "1999-01-01",
                "recurring": "Subscription",
            },
            {
                "id": "invented-row",
                "original": "UNMATCHED SOURCE",
                "clean_name": "Invented",
                "category": "Shopping",
                "amount": -1,
                "currency": "EUR",
                "date": "2026-02-22",
            },
        ]),
    )
    source = {
        "id": "source-row",
        "raw_description": "BANK DESCRIPTION",
        "amount": -100.25,
        "amount_decimal": "-100.25",
        "currency": "USD",
        "date": "2026-02-22",
        "counterpart": "Merchant of record",
        "account_name": "Card",
        "account_id": "acct-123",
        "statement_category": "Statement category",
    }

    results = categorise(
        [source], [], provider="gemini", api_key="test-only", model="mocked",
    )

    assert len(results) == 1
    result = results[0]
    assert result.id == "source-row"
    assert result.amount == -100.25
    assert result.currency == "USD"
    assert result.date == "2026-02-22"
    assert result.original_description == "BANK DESCRIPTION"
    assert result.counterpart == "Merchant of record"
    assert result.account_id == "acct-123"
    assert result.category == "Food"
    assert result.clean_name == "Market"
    assert result.recurring == "Subscription"


def test_incomplete_ai_output_is_visible_and_parse_errors_do_not_log_financial_text(
    caplog,
    monkeypatch,
) -> None:
    import json
    import spectra.ai

    source_rows = [
        {"id": "row-1", "raw_description": "FIRST", "amount": -1, "currency": "EUR", "date": "2026-01-01"},
        {"id": "row-2", "raw_description": "SECOND", "amount": -2, "currency": "EUR", "date": "2026-01-02"},
    ]
    monkeypatch.setattr(
        spectra.ai,
        "_call_gemini",
        lambda *_args: json.dumps([{
            "id": "row-1", "original": "FIRST", "clean_name": "Merchant",
            "category": "Food", "amount": -1, "currency": "EUR", "date": "2026-01-01",
        }]),
    )
    results = categorise(
        source_rows, [], provider="gemini", api_key="test-only", model="mocked",
    )
    assert [item.id for item in results] == ["row-1", "row-2"]
    assert results[1].category == "Uncategorized"
    assert results[1].needs_review is True

    with caplog.at_level("ERROR", logger="spectra.ai"):
        assert _extract_json("sensitive account detail 998877 cannot parse") == []
    assert "998877" not in caplog.text
