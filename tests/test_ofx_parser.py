from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import ofxparse

from spectra.csv_parser import ParsedTransaction
from spectra.db import BookmarkDB
from spectra.import_service import _legacy_seen
from spectra.ofx_parser import parse_ofx


def _ofx(account_id: str):
    transaction = SimpleNamespace(
        id="FITID-1",
        date=datetime(2026, 2, 22),
        amount=-10.25,
        payee="Market",
        memo="Card payment",
    )
    account = SimpleNamespace(
        account_id=account_id,
        curdef="EUR",
        statement=SimpleNamespace(
            transactions=[transaction], balance=250.75, end_date=datetime(2026, 2, 28),
        ),
    )
    return SimpleNamespace(account=account)


def test_ofx_identity_is_scoped_and_account_number_is_not_logged(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    path = tmp_path / "statement.ofx"
    path.write_text("OFX sample")

    class FakeOfxParser:
        current = None

        @classmethod
        def parse(cls, _file):
            return cls.current

    monkeypatch.setattr(ofxparse, "OfxParser", FakeOfxParser)
    account_a = "1111222233334444"
    account_b = "9999222233334444"

    FakeOfxParser.current = _ofx(account_a)
    first = parse_ofx(path)
    FakeOfxParser.current = _ofx(account_b)
    second = parse_ofx(path)

    assert first[0].id != second[0].id
    assert first[0].legacy_id == second[0].legacy_id == "FITID-1"
    assert first[0].account_id != second[0].account_id
    assert first[0].account_name == "Account ••••4444"
    assert first[0].amount_decimal == "-10.25"
    assert first[0].account_balance == 250.75
    assert account_a not in caplog.text
    assert account_b not in caplog.text


def test_legacy_ofx_marker_skips_only_its_original_account(tmp_path) -> None:
    account_a = "1111222233334444"
    account_b = "9999222233334444"
    source_a = ParsedTransaction(
        id="scoped-a", legacy_id="FITID-1", legacy_account_name=account_a,
        date="2026-02-22", amount=-10.25, amount_decimal="-10.25",
        currency="EUR", raw_description="Market",
    )
    source_b = ParsedTransaction(
        id="scoped-b", legacy_id="FITID-1", legacy_account_name=account_b,
        date="2026-02-22", amount=-10.25, amount_decimal="-10.25",
        currency="EUR", raw_description="Market",
    )

    with BookmarkDB(tmp_path / "legacy-ofx.db") as db:
        db._conn.execute(
            """INSERT INTO tx_history
               (tx_id, date, clean_name, amount, account_name, amount_currency,
                amount_decimal, category, original_description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("FITID-1", "2026-02-22", "Market", -10.25, account_a, "EUR", "-10.25", "Food", "Market"),
        )
        db.mark_seen("FITID-1", source="OFX")

        assert _legacy_seen(db, source_a) is True
        assert _legacy_seen(db, source_b) is False
