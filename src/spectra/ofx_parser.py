"""Universal OFX parser — reads bank export files from any institution."""

from __future__ import annotations

import logging
import hashlib
from pathlib import Path

from spectra.csv_parser import ParsedTransaction, _extract_counterpart
from spectra.money import decimal_text

logger = logging.getLogger("spectra.ofx_parser")

def parse_ofx(
    file_path: str | Path,
    currency: str = "EUR",
    encoding: str = "utf-8-sig", # utf-8-sig handles BOM from Excel exports
) -> list[ParsedTransaction]:
    """Parse a bank OFX export into a list of ParsedTransaction objects."""
    import codecs
    from ofxparse import OfxParser

    with codecs.open(file_path, 'r', encoding=encoding) as f:
        ofx = OfxParser.parse(f)
        account = ofx.account
        # Access statement information
        statement = account.statement

        # Iterate through transactions
        transactions: list[ParsedTransaction] = []
        if account.curdef:
            currency_default = account.curdef
        else:
            currency_default = currency
        for transaction in statement.transactions:
            if not transaction.amount and not transaction.date:
                continue
            payee = str(transaction.payee or "").strip()
            memo = str(transaction.memo or "").strip()
            raw_description = " ".join(part for part in [payee, memo] if part).strip()
            source_id = str(transaction.id or "")
            legacy_account_name = str(account.account_id or "").strip()
            if legacy_account_name:
                scoped_id = "OFX-" + hashlib.sha256(
                    f"{legacy_account_name.casefold()}|{source_id}".encode("utf-8")
                ).hexdigest()[:20]
                account_id = "acct-" + hashlib.sha256(
                    f"{legacy_account_name.casefold()}|{currency_default.upper()}".encode("utf-8")
                ).hexdigest()[:20]
                account_name = f"Account ••••{legacy_account_name[-4:]}"
            else:
                scoped_id = source_id
                account_id = ""
                account_name = ""
            transactions.append(
                ParsedTransaction(
                    id=scoped_id,
                    date=transaction.date.strftime("%Y-%m-%d"),
                    amount=transaction.amount,
                    currency=currency_default.upper(),
                    raw_description=raw_description,
                    amount_decimal=decimal_text(transaction.amount, currency_default),
                    counterpart=_extract_counterpart(raw_description, explicit_counterpart=payee),
                    account_name=account_name,
                    account_id=account_id,
                    legacy_id=source_id if account_name and source_id else "",
                    legacy_account_name=legacy_account_name,
                    account_balance=statement.balance,
                    account_balance_date=str(statement.end_date or ""),
                )
            )

        logger.info("Parsed %d OFX transaction(s)", len(transactions))

        return transactions
