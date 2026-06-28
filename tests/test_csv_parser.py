"""Unit tests for the CSV parser."""

from pathlib import Path
from textwrap import dedent

import pytest

from spectra.csv_parser import (
    _clean_description,
    _map_columns,
    _parse_amount,
    _parse_date,
    parse_csv,
)


FIXTURES = Path(__file__).parent / "fixtures"


class TestParseAmount:
    """Test mixed amount parsing across bank formats."""

    def test_italian_negative(self) -> None:
        assert _parse_amount("-4,50") == -4.5

    def test_italian_thousands(self) -> None:
        assert _parse_amount("1.500,00") == 1500.0

    def test_english(self) -> None:
        assert _parse_amount("1,500.00") == 1500.0

    def test_amount_with_currency_code(self) -> None:
        assert _parse_amount("USD 2,450.00") == 2450.0

    def test_parentheses_negative(self) -> None:
        assert _parse_amount("(100.00)") == -100.0

    def test_trailing_minus(self) -> None:
        assert _parse_amount("42,10-") == -42.1


class TestParseDate:
    """Test date parsing and ambiguity handling."""

    def test_iso(self) -> None:
        assert _parse_date("2026-02-22") == "2026-02-22"

    def test_eu_short_defaults_day_first(self) -> None:
        assert _parse_date("03/02/26") == "2026-02-03"

    def test_us_short_can_be_forced(self) -> None:
        assert _parse_date("02/14/26", prefer_day_first=False) == "2026-02-14"

    def test_compact(self) -> None:
        assert _parse_date("20260222") == "2026-02-22"


class TestHeaderMapping:
    def test_aliases_and_punctuation_are_recognized(self) -> None:
        mapping = _map_columns(
            [
                "Booking Date",
                "Transaction Description",
                "Amount (EUR)",
                "Counterparty Name",
            ]
        )
        assert mapping["date"] == 0
        assert mapping["description"] == 1
        assert mapping["amount"] == 2
        assert mapping["counterpart"] == 3


class TestDescriptionCleanup:
    def test_banking_boilerplate_is_removed(self) -> None:
        cleaned = _clean_description(
            "Pagamento Effettuato Su Pos Estero | Effettuato Il 18/01/2026 Alle Ore 1338 Presso Porkbun.com Sherwood"
        )
        assert "Pagamento Effettuato Su Pos Estero" not in cleaned
        assert "Porkbun.com Sherwood" in cleaned


class TestParseCsv:
    """Test full CSV parsing with realistic bank fixtures."""

    def test_isybank_fixture_extracts_counterparts(self) -> None:
        txns = parse_csv(FIXTURES / "isybank_transfer_sample.csv")
        assert len(txns) == 2
        assert txns[0].amount == -141.0
        assert txns[0].counterpart == "Daniele Magri"
        assert txns[1].counterpart == "Porkbun.Com Sherwood"

    def test_revolut_fixture_handles_header_aliases_and_multiline_fields(self) -> None:
        txns = parse_csv(FIXTURES / "revolut_mixed_headers.csv", currency="EUR")
        assert len(txns) == 2
        assert txns[0].date == "2026-02-14"
        assert txns[0].currency == "USD"
        assert "WHOLE FOODS MARKET" in txns[0].raw_description
        assert txns[0].counterpart == "Whole Foods Market"
        assert txns[1].amount == 2450.0

    def test_eu_fixture_infers_day_first_and_skips_noise(self) -> None:
        txns = parse_csv(FIXTURES / "eu_aliases_multiline.csv")
        assert len(txns) == 3
        assert txns[0].date == "2026-02-03"
        assert txns[0].statement_category == "Transfers"
        assert txns[1].amount == 1800.0
        assert "Coffee Shop | City Center" in txns[2].raw_description

    def test_split_debit_credit(self, tmp_path: Path) -> None:
        csv = tmp_path / "test.csv"
        csv.write_text(
            dedent(
                """\
                Data;Descrizione;Addebito;Accredito
                22/02/2026;POS STARBUCKS;4,50;
                21/02/2026;STIPENDIO;;1500,00
                """
            )
        )
        txns = parse_csv(csv)
        assert len(txns) == 2
        assert txns[0].amount == -4.5
        assert txns[1].amount == 1500.0

    def test_explicit_counterparty_column_wins(self, tmp_path: Path) -> None:
        csv = tmp_path / "test.csv"
        csv.write_text(
            dedent(
                """\
                Date,Description,Payee,Amount,Currency
                2026-02-22,SEPA TRANSFER,Andre Silva,-42.50,EUR
                """
            )
        )
        txns = parse_csv(csv)
        assert len(txns) == 1
        assert txns[0].counterpart == "Andre Silva"

    def test_missing_amount_columns_raise_clear_error(self, tmp_path: Path) -> None:
        csv = tmp_path / "invalid.csv"
        csv.write_text(
            dedent(
                """\
                Date,Description,Currency
                2026-02-22,STARBUCKS,EUR
                """
            )
        )
        with pytest.raises(ValueError, match="Cannot find amount column"):
            parse_csv(csv)

    def test_corrupted_rows_are_skipped(self, tmp_path: Path) -> None:
        csv = tmp_path / "corrupted.csv"
        csv.write_text(
            dedent(
                """\
                Date,Description,Amount,Currency
                2026-02-22,STARBUCKS,-4.50,EUR
                broken-row-without-columns
                2026-02-21,NETFLIX,-9.99,EUR
                """
            )
        )
        txns = parse_csv(csv)
        assert len(txns) == 2

    def test_dedup_ids_are_stable_for_realistic_fixture(self) -> None:
        fixture = FIXTURES / "revolut_mixed_headers.csv"
        txns1 = parse_csv(fixture)
        txns2 = parse_csv(fixture)
        assert [tx.id for tx in txns1] == [tx.id for tx in txns2]

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_csv("/nonexistent/file.csv")
