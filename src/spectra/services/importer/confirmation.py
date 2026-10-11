"""Review validation and idempotent confirmation through the ledger API."""

from __future__ import annotations

from typing import Any

from spectra.ai import CategorisedTransaction
from spectra.review import (
    TRANSFER_STATUS_NONE,
    apply_review_state,
    match_internal_transfers,
)
from spectra.services.clients import LedgerClient
from spectra.services.contracts import CommitImportRequest


_IMMUTABLE_FIELDS = (
    "id", "date", "amount", "amount_decimal", "currency", "original_description",
    "original_amount", "original_currency", "original_amount_decimal", "conversion_rate",
    "conversion_date", "conversion_source", "conversion_error", "currency_source",
    "account_id", "account_name", "account_balance", "account_balance_date",
    "import_batch_id",
)
_EDITABLE_FIELDS = (
    "merchant", "category", "recurring", "apply_to_future", "needs_review",
    "review_reason", "transfer_status", "excluded_from_spend",
)


def _same_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right or (left in (None, "") and right in (None, ""))
    try:
        return str(left) == str(right)
    except Exception:
        return False


def _review_transaction(payload: dict[str, Any], *, base_currency: str) -> CategorisedTransaction:
    return CategorisedTransaction(
        id=str(payload["id"]),
        original_description=str(payload.get("original_description", "") or ""),
        clean_name=str(payload.get("merchant", payload.get("clean_name", "")) or ""),
        category=str(payload.get("category", "Uncategorized") or "Uncategorized"),
        amount=float(payload.get("amount", 0) or 0),
        currency=str(payload.get("currency", base_currency) or base_currency),
        original_amount=payload.get("original_amount"),
        original_currency=payload.get("original_currency"),
        original_amount_decimal=payload.get("original_amount_decimal"),
        conversion_rate=payload.get("conversion_rate"),
        conversion_date=payload.get("conversion_date"),
        conversion_source=str(payload.get("conversion_source", "") or ""),
        conversion_error=str(payload.get("conversion_error", "") or ""),
        amount_decimal=payload.get("amount_decimal"),
        date=str(payload.get("date", "") or ""),
        statement_category=str(payload.get("statement_category", "") or ""),
        recurring=str(payload.get("recurring", "") or ""),
        counterpart=str(payload.get("counterpart", "") or ""),
        classification_source=str(payload.get("classification_source", "") or ""),
        category_confidence=payload.get("category_confidence"),
        category_suggestions=payload.get("category_suggestions", []),
        needs_review=bool(payload.get("needs_review", False)),
        review_reason=str(payload.get("review_reason", "") or ""),
        account_name=str(payload.get("account_name", "") or ""),
        account_id=str(payload.get("account_id", "") or ""),
        account_balance=payload.get("account_balance"),
        account_balance_date=str(payload.get("account_balance_date", "") or ""),
        currency_source=str(payload.get("currency_source", "parser") or "parser"),
        import_batch_id=str(payload.get("import_batch_id", "") or ""),
        transfer_group_id=str(payload.get("transfer_group_id", "") or ""),
        transfer_status=str(payload.get("transfer_status", TRANSFER_STATUS_NONE) or TRANSFER_STATUS_NONE),
        excluded_from_spend=bool(payload.get("excluded_from_spend", False)),
    )


def confirm_review(
    ledger: LedgerClient,
    staged_transactions: list[dict[str, Any]],
    submitted_transactions: list[dict[str, Any]],
    *,
    base_currency: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Commit only staged rows, preserving source money and identity metadata."""
    staged_by_id = {str(row.get("id", "")): row for row in staged_transactions}
    submitted_by_id = {str(row.get("id", "")): row for row in submitted_transactions}
    if not staged_by_id or set(submitted_by_id) != set(staged_by_id):
        raise ValueError("Review payload does not match the staged transaction batch")

    merged: list[dict[str, Any]] = []
    for tx_id, original in staged_by_id.items():
        submitted = submitted_by_id[tx_id]
        for key in _IMMUTABLE_FIELDS:
            if key in original and not _same_value(original.get(key), submitted.get(key)):
                raise ValueError(f"Transaction source field cannot be changed during review: {key}")
        row = dict(original)
        for key in _EDITABLE_FIELDS:
            if key in submitted:
                row[key] = submitted[key]
        merged.append(row)

    seen_rows: list[dict[str, Any]] = []
    unseen: list[dict[str, Any]] = []
    for row in merged:
        if ledger.is_seen(str(row.get("id", ""))):
            seen_rows.append(row)
        else:
            unseen.append(row)
    if not unseen:
        # Ask the ledger for the idempotency receipt even when every row is
        # already present. If the previous request committed and the importer
        # crashed before updating staging, this returns that original receipt.
        result = ledger.commit_import(
            CommitImportRequest(idempotency_key=idempotency_key, transactions=[]),
        )
        result.update({
            "message": "These transactions have already been saved.",
            "review_count": 0,
            "transfer_count": 0,
            "redirect_to": "/transactions",
            "already_saved": True,
        })
        return result

    rows = [_review_transaction(row, base_currency=base_currency) for row in unseen]
    feedback: list[dict[str, Any]] = []
    mappings: dict[str, str] = {}
    overrides: dict[str, dict[str, str]] = {}
    learned = 0
    for payload, transaction in zip(unseen, rows):
        apply_review_state(transaction)
        apply_to_future = bool(payload.get("apply_to_future", True))
        if transaction.clean_name and transaction.category != "Uncategorized":
            feedback.append({
                "tx_id": transaction.id,
                "original_description": transaction.original_description,
                "clean_name": transaction.clean_name,
                "category": transaction.category,
                "source": "upload_confirm",
                "apply_to_future": apply_to_future,
            })
            if apply_to_future:
                mappings[transaction.clean_name] = transaction.category
                if transaction.original_description:
                    overrides[transaction.original_description] = {
                        "clean_name": transaction.clean_name,
                        "category": transaction.category,
                    }
                learned += 1

    from spectra.import_service import load_existing_transfer_candidates

    candidates = load_existing_transfer_candidates(ledger, rows)
    transfer_updates = match_internal_transfers(rows, existing_candidates=candidates)
    commit = ledger.commit_import(CommitImportRequest(
        idempotency_key=idempotency_key,
        transactions=[row.model_dump(mode="json") for row in rows],
        learning_feedback=feedback,
        merchant_mappings=mappings,
        overrides=overrides,
        existing_transfer_updates=transfer_updates,
    ))
    review_count = sum(1 for tx in rows if tx.needs_review)
    transfer_count = sum(1 for tx in rows if tx.transfer_status == "suggested")
    return {
        **commit,
        "message": f"Saved {len(rows)} transactions to local DB · {review_count} need review · {transfer_count} transfer suggestion(s) · learned {learned} future mapping(s)",
        "review_count": review_count,
        "transfer_count": transfer_count,
        "redirect_to": "/review" if review_count else "/transactions",
        "already_saved": bool(commit.get("already_committed")),
        "previously_seen_count": len(seen_rows),
    }

