"""Unit tests for the CSV parser."""

from pathlib import Path
from textwrap import dedent

import pytest

from spectra.csv_parser import ParsedTransaction, parse_csv, _parse_amount, _parse_date


class TestParseAmount:
    """Test Italian and English amount parsing."""

    def test_italian_negative(self) -> None:
        assert _parse_amount("-4,50") == -4.5

    def test_italian_thousands(self) -> None:
        assert _parse_amount("1.500,00") == 1500.0

    def test_italian_positive_sign(self) -> None:
        assert _parse_amount("+1.500,00") == 1500.0

    def test_english(self) -> None:
        assert _parse_amount("1,500.00") == 1500.0

    def test_negative_english(self) -> None:
        assert _parse_amount("-1,234.56") == -1234.56

    def test_simple_decimal(self) -> None:
        assert _parse_amount("9.99") == 9.99

    def test_with_euro_symbol(self) -> None:
        assert _parse_amount("€ 42,00") == 42.0

    def test_parentheses_negative(self) -> None:
        assert _parse_amount("(100.00)") == -100.0


class TestParseDate:
    """Test date format detection."""

    def test_iso(self) -> None:
        assert _parse_date("2026-02-22") == "2026-02-22"

    def test_eu_slash(self) -> None:
        assert _parse_date("22/02/2026") == "2026-02-22"

    def test_eu_dash(self) -> None:
        assert _parse_date("22-02-2026") == "2026-02-22"

    def test_eu_dot(self) -> None:
        assert _parse_date("22.02.2026") == "2026-02-22"

    def test_compact(self) -> None:
        assert _parse_date("20260222") == "2026-02-22"


class TestParseCsv:
    """Test full CSV parsing with different bank formats."""

    def test_isybank_format(self, tmp_path: Path) -> None:
        """ISyBank / Intesa format: semicolon, Italian amounts."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Data Operazione;Descrizione;Importo;Valuta
            22/02/2026;POS STARBUCKS;-4,50;EUR
            21/02/2026;STIPENDIO ACME SRL;+1.500,00;EUR
        """))
        txns = parse_csv(csv)
        assert len(txns) == 2
        assert txns[0].amount == -4.5
        assert txns[0].date == "2026-02-22"
        assert txns[1].amount == 1500.0

    def test_english_format(self, tmp_path: Path) -> None:
        """English format: comma-separated, English amounts."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Date,Description,Amount,Currency
            2026-02-22,STARBUCKS,-4.50,EUR
            2026-02-21,SALARY,1500.00,EUR
        """))
        txns = parse_csv(csv)
        assert len(txns) == 2
        assert txns[0].amount == -4.5
        assert txns[1].amount == 1500.0

    def test_split_debit_credit(self, tmp_path: Path) -> None:
        """Banks that split debit/credit into two columns."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Data;Descrizione;Addebito;Accredito
            22/02/2026;POS STARBUCKS;4,50;
            21/02/2026;STIPENDIO;;1500,00
        """))
        txns = parse_csv(csv)
        assert len(txns) == 2
        assert txns[0].amount == -4.5  # debit
        assert txns[1].amount == 1500.0  # credit

    def test_dedicated_counterparty_column(self, tmp_path: Path) -> None:
        """A dedicated payee/beneficiary column should populate the counterpart directly."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Date,Description,Payee,Amount,Currency
            2026-02-22,SEPA TRANSFER,Andre Silva,-42.50,EUR
        """))
        txns = parse_csv(csv)
        assert len(txns) == 1
        assert txns[0].counterpart == "Andre Silva"

    def test_isybank_transfer_extracts_counterpart_from_description(self, tmp_path: Path) -> None:
        """Transfer descriptions should resolve the recipient even when buried in free text."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Data;Operazione;Dettagli;Importo;Valuta
            1/31/26;Bonifico istantaneo da voi disposto a favore di Daniele Magri;3526013184395777 Bonifico da Voi disposto a favore di Daniele Magri Hotel firenze;-141,00;EUR
        """))
        txns = parse_csv(csv)
        assert len(txns) == 1
        assert txns[0].counterpart == "Daniele Magri"

    def test_generic_pos_operation_extracts_counterpart_from_details(self, tmp_path: Path) -> None:
        """When the operation is generic, details should still yield the merchant/counterpart."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Data;Operazione;Dettagli;Importo;Valuta
            1/18/26;Pagamento Effettuato Su Pos Estero;Effettuato Il 18/01/2026 Alle Ore 1338 Mediante La Carta 5341 Xxxx Xxxx Xx40 Presso Porkbun.com Sherwood (ctv. Di 1081 Usd Al Cambio Di 0863334);-9,33;EUR
        """))
        txns = parse_csv(csv)
        assert len(txns) == 1
        assert txns[0].counterpart == "Porkbun.Com Sherwood"

    def test_dedup_ids_are_stable(self, tmp_path: Path) -> None:
        """Same data should produce the same IDs."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Date,Description,Amount
            2026-02-22,STARBUCKS,-4.50
        """))
        txns1 = parse_csv(csv)
        txns2 = parse_csv(csv)
        assert txns1[0].id == txns2[0].id

    def test_empty_rows_skipped(self, tmp_path: Path) -> None:
        """Empty rows should be silently skipped."""
        csv = tmp_path / "test.csv"
        csv.write_text(dedent("""\
            Date,Description,Amount
            2026-02-22,STARBUCKS,-4.50

            2026-02-21,NETFLIX,-9.99
        """))
        txns = parse_csv(csv)
        assert len(txns) == 2

    def test_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_csv("/nonexistent/file.csv")
