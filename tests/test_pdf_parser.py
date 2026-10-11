"""Unit tests for PDF parser text fallback."""

from pathlib import Path

from spectra.pdf_parser import _extract_from_text_with_pypdf, _rows_to_transactions


def test_extract_from_text_with_pypdf_parses_valid_lines(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _: str) -> None:
            self.pages = [
                _Page("22/02/2026 POS STARBUCKS -4,50 EUR"),
                _Page("2026-02-21 STIPENDIO ACME +1.500,00"),
            ]

    txns = _extract_from_text_with_pypdf(pdf_path, _Reader, "EUR")

    assert len(txns) == 2
    assert txns[0].date == "2026-02-22"
    assert txns[0].amount == -4.5
    assert txns[0].raw_description == "POS STARBUCKS"
    assert txns[1].date == "2026-02-21"
    assert txns[1].amount == 1500.0


def test_extract_from_text_with_pypdf_skips_non_matching_lines(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _: str) -> None:
            self.pages = [
                _Page("header that should be ignored"),
                _Page("22-02-2026 AMAZON -19,99 EUR"),
                _Page("invalid 2026-02-22 line without amount"),
            ]

    txns = _extract_from_text_with_pypdf(pdf_path, _Reader, "EUR")

    assert len(txns) == 1
    assert txns[0].date == "2026-02-22"
    assert txns[0].amount == -19.99
    assert txns[0].raw_description == "AMAZON"


def test_extract_from_text_with_pypdf_parses_wrapped_transactions(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _: str) -> None:
            self.pages = [
                _Page(
                    "30.04.2026 Stipendio O Pensione SI Stipendi e\n"
                    "pensioni € 1.029,00\n"
                    "29.04.2026 Openai NO Altre uscite € -5,22"
                ),
            ]

    txns = _extract_from_text_with_pypdf(pdf_path, _Reader, "EUR")

    assert len(txns) == 2
    assert txns[0].date == "2026-04-30"
    assert txns[0].amount == 1029.0
    assert "Stipendio" in txns[0].raw_description
    assert txns[0].statement_category.lower() == "stipendi e pensioni"
    assert txns[1].date == "2026-04-29"
    assert txns[1].amount == -5.22


def test_extract_from_text_with_pypdf_ignores_noise_between_wrapped_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _: str) -> None:
            self.pages = [
                _Page(
                    "Statement summary page 1\n"
                    "14.02.2026 Coffee Shop NO Food & Dining € -3,50\n"
                    "Random footer that should be ignored\n"
                    "15.02.2026 Salary SI Income € 1.800,00"
                ),
            ]

    txns = _extract_from_text_with_pypdf(pdf_path, _Reader, "EUR")

    assert len(txns) == 2
    assert txns[0].statement_category == "Food & Dining"
    assert txns[1].statement_category == "Income"


def test_table_identity_includes_account_currency_and_repeated_rows() -> None:
    transactions = _rows_to_transactions(
        ["Date", "Description", "Amount", "Currency", "Account"],
        [
            ["22/02/2026", "MARKET", "-10.00", "EUR", "Checking A"],
            ["22/02/2026", "MARKET", "-10.00", "EUR", "Checking A"],
            ["22/02/2026", "MARKET", "-10.00", "EUR", "Checking B"],
            ["22/02/2026", "MARKET", "-10.00", "USD", "Checking A"],
        ],
        "EUR",
    )

    assert len({tx.id for tx in transactions}) == 4
    assert transactions[0].id != transactions[1].id
    assert transactions[0].id != transactions[2].id
    assert transactions[0].id != transactions[3].id
    assert transactions[0].account_id != transactions[2].account_id
    assert transactions[0].legacy_id == transactions[2].legacy_id


def test_text_identity_includes_currency_and_repeated_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    class _Page:
        def extract_text(self) -> str:
            return "\n".join([
                "22/02/2026 MARKET -10.00 EUR",
                "22/02/2026 MARKET -10.00 EUR",
            ])

    class _Reader:
        def __init__(self, _: str) -> None:
            self.pages = [_Page()]

    transactions = _extract_from_text_with_pypdf(pdf_path, _Reader, "USD")
    assert len(transactions) == 2
    assert transactions[0].id != transactions[1].id
    assert transactions[0].currency == "EUR"
    assert transactions[0].legacy_id == transactions[1].legacy_id
