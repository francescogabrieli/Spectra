"""Shared transaction import orchestration for the CLI and web UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from spectra.ai import CategorisedTransaction
from spectra.config import Settings
from spectra.csv_parser import ParsedTransaction
from spectra.money import decimal_text
from spectra.review import (
    apply_review_state,
    build_import_batch_id,
    match_internal_transfers,
)
from spectra.services.contracts import CategorizeRequest, FxConvertRequest
from spectra.rules import first_matching_rule

logger = logging.getLogger("spectra.import")

ProgressCallback = Callable[[int, str], None]


@dataclass
class ImportPreparation:
    transactions: list[CategorisedTransaction]
    sources: list[ParsedTransaction]
    skipped_duplicates: int
    transfer_updates: list[dict[str, Any]]
    import_batch_id: str


def parse_file(file_path: str | Path, currency: str) -> list[ParsedTransaction]:
    """Parse CSV, PDF, or OFX exports through one shared dispatch point."""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        from spectra.pdf_parser import parse_pdf
        return parse_pdf(path, currency=currency)
    if ext == ".ofx":
        from spectra.ofx_parser import parse_ofx
        return parse_ofx(path, currency=currency)
    from spectra.csv_parser import parse_csv
    return parse_csv(path, currency=currency)


def _legacy_seen(db: Any, source: ParsedTransaction) -> bool:
    legacy_id = str(getattr(source, "legacy_id", "") or "")
    if not legacy_id or not db.is_seen(legacy_id):
        return False
    old = db.find_legacy_transaction(legacy_id)
    if old is None:
        # Older versions may have recorded a seen marker without a local row.
        # There is no safe way to resolve its account scope, so avoid duplicating it.
        logger.warning("Skipping legacy import identity with no recoverable transaction row")
        return True
    old_account = str(old.get("account_name") or "").strip().casefold()
    new_account = str(
        getattr(source, "legacy_account_name", "")
        or getattr(source, "account_name", "")
        or ""
    ).strip().casefold()
    if not old_account or not new_account or old_account == new_account:
        # A blank legacy account means the former unscoped ID cannot identify
        # which account produced it; skipping is safer than replaying history.
        return True
    return False


def _source_as_dict(source: ParsedTransaction) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "raw_description": str(source.raw_description),
        "counterpart": str(getattr(source, "counterpart", "") or ""),
        "statement_category": str(getattr(source, "statement_category", "") or ""),
        "amount": float(source.amount),
        "amount_decimal": getattr(source, "amount_decimal", "") or decimal_text(source.amount, source.currency),
        "currency": str(source.currency).upper(),
        "date": str(source.date),
        "account_name": str(getattr(source, "account_name", "") or ""),
        "account_id": str(getattr(source, "account_id", "") or ""),
        "account_balance": getattr(source, "account_balance", None),
        "account_balance_date": str(getattr(source, "account_balance_date", "") or ""),
    }


def enrich_from_sources(
    categorised: list[CategorisedTransaction],
    *,
    source_transactions: list[ParsedTransaction],
    import_batch_id: str,
    provider: str,
) -> list[CategorisedTransaction]:
    """Restore authoritative source fields and make every omission reviewable."""
    by_id = {str(source.id): source for source in source_transactions}
    by_description: dict[str, list[ParsedTransaction]] = {}
    for source in source_transactions:
        by_description.setdefault(str(source.raw_description), []).append(source)

    used: set[str] = set()
    enriched: list[CategorisedTransaction] = []
    for tx in categorised:
        explicit_id = str(getattr(tx, "id", "") or "")
        source = by_id.get(explicit_id) if explicit_id else None
        if explicit_id and source is None:
            source_description = str(getattr(tx, "original_description", "") or "")
            source = next(
                (candidate for candidate in by_description.get(source_description, [])
                 if str(candidate.id) not in used),
                None,
            )
            if source is None:
                logger.warning("Ignoring a categorization result with an unknown source identity")
                continue
            tx.needs_review = True
            tx.review_reason = "ai_response_missing_source_id"
        if source is not None:
            response_description = str(getattr(tx, "original_description", "") or "")
            if response_description and response_description != str(source.raw_description):
                logger.warning("Ignoring a categorization result whose identity and description disagree")
                continue
        if source is None:
            source_description = str(getattr(tx, "original_description", "") or "")
            source = next(
                (candidate for candidate in by_description.get(source_description, [])
                 if str(candidate.id) not in used),
                None,
            )
            if source is None:
                logger.warning("Ignoring a categorization result with no source match")
                continue
            # Description-only association is retained for old in-process
            # adapters and is deliberately marked for a human check.
            tx.needs_review = True
            tx.review_reason = "ai_response_missing_source_id"

        source_id = str(source.id)
        if source_id in used:
            logger.warning("Ignoring duplicate categorization for a source row")
            continue
        used.add(source_id)

        tx.id = source_id
        tx.original_description = str(source.raw_description)
        tx.amount = float(source.amount)
        tx.amount_decimal = getattr(source, "amount_decimal", "") or decimal_text(source.amount, source.currency)
        tx.currency = str(source.currency).upper()
        tx.date = str(source.date)
        tx.statement_category = str(getattr(source, "statement_category", "") or "")
        tx.counterpart = str(getattr(source, "counterpart", "") or "")
        tx.account_name = str(getattr(source, "account_name", "") or "")
        tx.account_id = str(getattr(source, "account_id", "") or "")
        tx.account_balance = getattr(source, "account_balance", None)
        tx.account_balance_date = str(getattr(source, "account_balance_date", "") or "")
        tx.import_batch_id = import_batch_id
        if not tx.classification_source:
            tx.classification_source = provider
        enriched.append(tx)

    for source in source_transactions:
        source_id = str(source.id)
        if source_id in used:
            continue
        fallback = CategorisedTransaction(
            id=source_id,
            original_description=str(source.raw_description),
            clean_name=str(getattr(source, "counterpart", "") or source.raw_description),
            category="Uncategorized",
            amount=float(source.amount),
            amount_decimal=getattr(source, "amount_decimal", "") or decimal_text(source.amount, source.currency),
            currency=str(source.currency).upper(),
            date=str(source.date),
            statement_category=str(getattr(source, "statement_category", "") or ""),
            counterpart=str(getattr(source, "counterpart", "") or ""),
            account_name=str(getattr(source, "account_name", "") or ""),
            account_id=str(getattr(source, "account_id", "") or ""),
            account_balance=getattr(source, "account_balance", None),
            account_balance_date=str(getattr(source, "account_balance_date", "") or ""),
            classification_source=provider,
            needs_review=True,
            review_reason="ai_response_incomplete",
            import_batch_id=import_batch_id,
        )
        enriched.append(fallback)
    return enriched


def load_existing_transfer_candidates(
    db: Any, transactions: list[CategorisedTransaction],
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        for row in db.find_transfer_candidates(
            tx_date=tx.date,
            amount=float(tx.amount),
            currency=tx.currency,
            exclude_ids=[str(tx.id)],
        ):
            candidates.setdefault(str(row["id"]), row)
    return list(candidates.values())


def prepare_import(
    parsed: list[ParsedTransaction],
    *,
    settings: Settings,
    db: Any,
    existing_categories: list[str] | None = None,
    import_batch_id: str | None = None,
    categorizer: Callable[..., list[CategorisedTransaction]] | None = None,
    base_currency: str | None = None,
    progress: ProgressCallback | None = None,
    enrichment_client: Any | None = None,
    persist_fx_cache: bool = True,
) -> ImportPreparation:
    """Deduplicate, categorize, review, convert, and match one parsed import."""
    def report(pct: int, label: str) -> None:
        if progress:
            progress(pct, label)

    report(22, "Checking for duplicate transactions")
    sources = [source for source in parsed if not db.is_seen(source.id) and not _legacy_seen(db, source)]
    batch_id = import_batch_id or build_import_batch_id()
    if not sources:
        report(100, "All transactions have already been imported")
        return ImportPreparation([], [], len(parsed), [], batch_id)

    overrides = db.get_overrides()
    category_rules = db.get_category_rules()
    merchant_db = db.get_merchant_categories()
    training_data = db.get_training_data()
    db_categories = db.list_known_categories()
    categories = existing_categories if existing_categories is not None else db_categories
    pre_categorised: list[CategorisedTransaction] = []
    to_process: list[ParsedTransaction] = []

    for source in sources:
        od = str(source.raw_description)
        counterpart = str(getattr(source, "counterpart", "") or "")
        if od in overrides:
            override = overrides[od]
            pre_categorised.append(CategorisedTransaction(
                id=source.id, original_description=od,
                clean_name=override["clean_name"], category=override["category"],
                amount=float(source.amount), amount_decimal=getattr(source, "amount_decimal", "") or None,
                currency=source.currency, date=source.date, counterpart=counterpart,
                statement_category=getattr(source, "statement_category", ""),
                account_name=getattr(source, "account_name", ""),
                account_id=getattr(source, "account_id", ""),
                account_balance=getattr(source, "account_balance", None),
                account_balance_date=getattr(source, "account_balance_date", ""),
                classification_source="override", import_batch_id=batch_id,
            ))
            continue

        rule = first_matching_rule(
            category_rules, clean_name=counterpart or od, raw_description=od,
        )
        if rule:
            pre_categorised.append(CategorisedTransaction(
                id=source.id, original_description=od,
                clean_name=counterpart or od, category=str(rule["category"]),
                amount=float(source.amount), amount_decimal=getattr(source, "amount_decimal", "") or None,
                currency=source.currency, date=source.date, counterpart=counterpart,
                statement_category=getattr(source, "statement_category", ""),
                account_name=getattr(source, "account_name", ""),
                account_id=getattr(source, "account_id", ""),
                account_balance=getattr(source, "account_balance", None),
                account_balance_date=getattr(source, "account_balance_date", ""),
                classification_source="rule", import_batch_id=batch_id,
            ))
            continue
        to_process.append(source)

    categorised = list(pre_categorised)
    if to_process:
        flat = [_source_as_dict(source) for source in to_process]
        report(32, f"Categorizing {len(to_process)} transaction(s)")
        if enrichment_client is not None:
            categorized_rows = enrichment_client.categorize(CategorizeRequest(
                transactions=flat,
                categories=categories,
                provider=settings.ai_provider,
                base_currency=str(base_currency or settings.base_currency or "EUR").upper(),
                training_data=training_data,
                merchant_categories=merchant_db,
            ))
            categorised.extend(
                row if isinstance(row, CategorisedTransaction)
                else CategorisedTransaction.model_validate(row)
                for row in categorized_rows
            )
        elif settings.ai_provider == "local":
            from spectra.local_categorizer import categorise_local
            from spectra.ml_classifier import train_classifier

            classifier = train_classifier(training_data)
            categorised.extend(categorise_local(
                flat, merchant_db=merchant_db, ml_classifier=classifier,
            ))
        else:
            if categorizer is None:
                from spectra.ai import categorise as categorizer
            if settings.ai_provider == "gemini":
                api_key, model = settings.gemini_api_key, settings.gemini_model
            else:
                api_key, model = settings.openai_api_key, settings.openai_model
            categorised.extend(categorizer(
                flat,
                categories,
                provider=settings.ai_provider,
                api_key=api_key,
                model=model,
                base_currency=settings.base_currency,
            ))

    categorised = enrich_from_sources(
        categorised,
        source_transactions=sources,
        import_batch_id=batch_id,
        provider=settings.ai_provider,
    )

    report(78, "Checking recurring payments")
    from spectra.recurring import apply_recurring_tags
    apply_recurring_tags(categorised, db.get_merchant_history())

    report(85, "Converting currencies with verified rates")
    from spectra.fx import ConversionResult, FXConversionError, convert_currency_result
    base_currency = str(base_currency or settings.base_currency or "EUR").upper()
    for tx in categorised:
        source_currency = str(tx.currency or "").upper()
        if not source_currency or source_currency == base_currency:
            tx.amount_decimal = decimal_text(tx.amount_decimal or tx.amount, source_currency or base_currency)
            tx.currency_source = "parser"
            continue
        original_amount = tx.amount
        original_decimal = tx.amount_decimal or decimal_text(original_amount, source_currency)
        try:
            if enrichment_client is not None:
                wire_result = enrichment_client.convert(FxConvertRequest(
                    amount=original_decimal,
                    from_currency=source_currency,
                    to_currency=base_currency,
                    rate_date=tx.date,
                    persist_rate=persist_fx_cache,
                ))
                result = ConversionResult(
                    amount=Decimal(str(wire_result["amount"])),
                    rate=Decimal(str(wire_result["rate"])),
                    requested_date=str(wire_result["requested_date"]),
                    effective_date=str(wire_result["effective_date"]),
                    source=str(wire_result["source"]),
                )
            else:
                result = convert_currency_result(
                    original_decimal, source_currency, base_currency, tx.date, rate_store=db,
                )
            tx.amount = float(result.amount)
            tx.amount_decimal = str(result.amount)
            tx.currency = base_currency
            tx.original_amount = original_amount
            tx.original_amount_decimal = original_decimal
            tx.original_currency = source_currency
            tx.conversion_rate = float(result.rate)
            tx.conversion_date = result.effective_date
            tx.conversion_source = result.source
            tx.currency_source = "fx-converted"
        except (FXConversionError, ValueError) as exc:
            # Preserve the original denomination and amount on failure.
            tx.amount = original_amount
            tx.amount_decimal = original_decimal
            tx.currency = source_currency
            tx.original_amount = original_amount
            tx.original_amount_decimal = original_decimal
            tx.original_currency = source_currency
            tx.conversion_error = str(exc)
            tx.currency_source = "parser"
            tx.needs_review = True
            tx.review_reason = "currency_conversion_failed"
            logger.warning("FX conversion unavailable for transaction %s", tx.id)

    for tx in categorised:
        apply_review_state(tx)

    report(94, "Matching possible internal transfers")
    existing_candidates = load_existing_transfer_candidates(db, categorised)
    transfer_updates = match_internal_transfers(
        categorised, existing_candidates=existing_candidates,
    )
    report(100, f"{len(categorised)} transaction(s) ready for review")
    return ImportPreparation(
        categorised, sources, len(parsed) - len(sources), transfer_updates, batch_id,
    )
