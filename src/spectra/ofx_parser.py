"""Universal OFX parser — reads bank export files from any institution."""

from __future__ import annotations

import logging
from pathlib import Path

from spectra.csv_parser import ParsedTransaction, _extract_counterpart

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
        logger.info(f"Account Number: {account.account_id}")
        logger.info(f"Routing Number: {account.routing_number}")

        # Access statement information
        statement = account.statement
        logger.info(f"Statement Start Date: {statement.start_date}")
        logger.info(f"Statement End Date: {statement.end_date}")
        logger.info(f"Current Balance: {statement.balance}")

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
            transactions.append(
                ParsedTransaction(
                    id=transaction.id,
                    date=transaction.date.strftime("%Y-%m-%d"),
                    amount=transaction.amount,
                    currency=currency_default.upper(),
                    raw_description=raw_description,
                    counterpart=_extract_counterpart(raw_description, explicit_counterpart=payee),
                )
            )
            logger.info(
                f"* Date: {transaction.date}, Amount: {transaction.amount}, Payee: {transaction.payee}, Memo: {transaction.memo}")

        return transactions
