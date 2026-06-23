"""Pipeline orchestrator — CSV/PDF → AI → Google Sheets."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from spectra.ai import CategorisedTransaction, categorise
from spectra.config import Settings, load_settings
from spectra.csv_parser import ParsedTransaction, parse_csv
from spectra.dashboard import refresh_dashboard
from spectra.db import BookmarkDB
from spectra.review import apply_review_state, build_import_batch_id, match_internal_transfers
from spectra.rules import first_matching_rule
from spectra.sheets import SheetsClient

logger = logging.getLogger("spectra")


def _parse_file(file_path: str, currency: str) -> list[ParsedTransaction]:
    """Auto-detect CSV or PDF and parse accordingly."""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        from spectra.pdf_parser import parse_pdf
        return parse_pdf(file_path, currency=currency)
    elif ext == ".ofx":
        from spectra.ofx_parser import parse_ofx
        return parse_ofx(file_path)
    else:
        return parse_csv(file_path, currency=currency)


def _source_key(*, tx_date: str, raw_description: str, amount: float) -> tuple[str, str, float]:
    return (str(tx_date), str(raw_description), round(float(amount), 2))


def _enrich_categorised_transactions(
    categorised: list[CategorisedTransaction],
    *,
    source_transactions: list[ParsedTransaction],
    import_batch_id: str,
    provider: str,
) -> None:
    """Propagate parser metadata and stable IDs into categorized rows."""
    source_lookup: dict[tuple[str, str, float], list[ParsedTransaction]] = {}
    for source_tx in source_transactions:
        key = _source_key(
            tx_date=source_tx.date,
            raw_description=source_tx.raw_description,
            amount=source_tx.amount,
        )
        source_lookup.setdefault(key, []).append(source_tx)

    for tx in categorised:
        key = _source_key(
            tx_date=tx.date,
            raw_description=tx.original_description,
            amount=tx.amount,
        )
        source_matches = source_lookup.get(key, [])
        source_tx = source_matches.pop(0) if source_matches else None
        if source_tx is not None:
            tx.id = source_tx.id
            tx.counterpart = str(getattr(source_tx, "counterpart", "") or tx.counterpart or "")
            tx.account_name = str(getattr(source_tx, "account_name", "") or "")
            if not tx.statement_category:
                tx.statement_category = str(getattr(source_tx, "statement_category", "") or "")
        if not tx.classification_source:
            tx.classification_source = provider
        tx.import_batch_id = import_batch_id


def _load_existing_transfer_candidates(db: BookmarkDB, transactions: list[CategorisedTransaction]) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for tx in transactions:
        for row in db.find_transfer_candidates(
            tx_date=tx.date,
            amount=float(tx.amount),
            exclude_ids=[str(tx.id)],
        ):
            candidates.setdefault(str(row["id"]), row)
    return list(candidates.values())

def run(settings: Settings, file: str, currency: str, dry_run: bool) -> None:
    """Process a bank CSV or PDF export → AI → Google Sheets."""

    ext = Path(file).suffix.upper()
    logger.info("📂 Reading %s: %s", ext, file)
    parsed = _parse_file(file, currency=currency)

    if not parsed:
        logger.info("✅ No transactions found in CSV")
        return

    # ── Step 2: Filter duplicates ────────────────────────────────
    with BookmarkDB(settings.db_path) as db:
        new_txns = [t for t in parsed if not db.is_seen(t.id)]
        logger.info("📥 %d total, %d new", len(parsed), len(new_txns))

        if not new_txns:
            logger.info("✅ All transactions already imported — nothing to do")
            return

        # ── Step 3: Read existing categories and Overrides ───────
        if dry_run:
            existing_categories: list[str] = []
            overrides = db.get_overrides()
            if not isinstance(overrides, dict):
                logger.warning("⚠️ Invalid local overrides format; ignoring")
                overrides = {}
        else:
            if not settings.google_sheets_credentials_b64 and not Path(settings.google_sheets_credentials_file).exists():
                logger.error("❌ Google Sheets credentials missing! Cannot run in production mode.")
                logger.error("   Please set GOOGLE_SHEETS_CREDENTIALS_B64 or GOOGLE_SHEETS_CREDENTIALS_FILE in .env")
                logger.error("   Or run with --dry-run to test locally without Google Sheets.")
                sys.exit(1)

            sheets = SheetsClient(
                spreadsheet_id=settings.spreadsheet_id,
                credentials_b64=settings.google_sheets_credentials_b64,
                credentials_file=settings.google_sheets_credentials_file,
            )
            existing_categories = sheets.get_existing_categories()
            logger.info("🔄 Syncing manual overrides from Sheets...")
            overrides = sheets.fetch_overrides()
            if not isinstance(overrides, dict):
                logger.warning("⚠️ Invalid overrides payload from Sheets; ignoring")
                overrides = {}
            db.save_overrides(overrides)

        logger.info("📂 Existing categories: %s", existing_categories or "(none)")
        category_rules = db.get_category_rules()
        import_batch_id = build_import_batch_id()

        # ── Step 3b: Local Override Matching (skip LLM if mapped) ────
        to_llm = []
        pre_categorised: list[CategorisedTransaction] = []
        override_count = 0
        rule_count = 0
        
        for t in new_txns:
            # ParsedTransaction (from csv/pdf) uses raw_description
            od = t.raw_description
            counterpart = str(getattr(t, "counterpart", "") or "")
            if od in overrides:
                # We have a manual classification for this exact description
                pre_categorised.append(
                    CategorisedTransaction(
                        id=t.id,
                        original_description=od,
                        clean_name=overrides[od]["clean_name"],
                        category=overrides[od]["category"],
                        amount=t.amount,
                        currency=t.currency,
                        date=t.date,
                        statement_category=getattr(t, "statement_category", ""),
                        counterpart=counterpart,
                        classification_source="override",
                        account_name=str(getattr(t, "account_name", "") or ""),
                        import_batch_id=import_batch_id,
                    )
                )
                override_count += 1
                continue

            matched_rule = first_matching_rule(
                category_rules,
                clean_name=counterpart or od,
                raw_description=od,
            )
            if matched_rule:
                pre_categorised.append(
                    CategorisedTransaction(
                        id=t.id,
                        original_description=od,
                        clean_name=counterpart or od,
                        category=str(matched_rule["category"]),
                        amount=t.amount,
                        currency=t.currency,
                        date=t.date,
                        statement_category=getattr(t, "statement_category", ""),
                        counterpart=counterpart,
                        classification_source="rule",
                        account_name=str(getattr(t, "account_name", "") or ""),
                        import_batch_id=import_batch_id,
                    )
                )
                rule_count += 1
            else:
                to_llm.append(t)
                
        if override_count or rule_count:
            logger.info(
                "Applied local mapping to %d transaction(s): %d overrides, %d rules",
                override_count + rule_count,
                override_count,
                rule_count,
            )

        # ── Step 4: Categorisation (LLM or Local) ────────────────
        categorised = []
        if to_llm:
            flat = [
                {
                    "raw_description": t.raw_description,
                    "counterpart": getattr(t, "counterpart", ""),
                    "statement_category": getattr(t, "statement_category", ""),
                    "id": t.id,
                    "amount": t.amount,
                    "currency": t.currency,
                    "date": t.date,
                }
                for t in to_llm
            ]

            if settings.ai_provider == "local":
                from spectra.local_categorizer import categorise_local
                from spectra.ml_classifier import train_classifier

                merchant_db = db.get_merchant_categories()
                training_data = db.get_training_data()
                ml_clf = train_classifier(training_data)

                local_results = categorise_local(
                    flat,
                    merchant_db=merchant_db,
                    ml_classifier=ml_clf,
                )
                if not local_results:
                    logger.warning("⚠️  Local categoriser returned no results for %d transactions", len(to_llm))
                else:
                    categorised.extend(local_results)
            else:
                if settings.ai_provider == "gemini":
                    api_key, model = settings.gemini_api_key, settings.gemini_model
                else:
                    api_key, model = settings.openai_api_key, settings.openai_model

                llm_results = categorise(
                    flat, existing_categories,
                    provider=settings.ai_provider, api_key=api_key, model=model,
                    base_currency=settings.base_currency,
                )

                if not llm_results:
                    logger.warning("⚠️  LLM returned no results for %d transactions", len(to_llm))
                else:
                    categorised.extend(llm_results)

        # Merge pre-categorised (overrides) and LLM-categorised
        categorised.extend(pre_categorised)

        if not categorised:
            logger.warning("⚠️ No transactions were categorised.")
            return

        _enrich_categorised_transactions(
            categorised,
            source_transactions=new_txns,
            import_batch_id=import_batch_id,
            provider=settings.ai_provider,
        )

        # ── Step 4b: Deterministic recurring detection ────────────
        from spectra.recurring import apply_recurring_tags
        
        # We fetch history from the DB to do temporal matching
        history = db.get_merchant_history()
        apply_recurring_tags(categorised, history)

        # ── Step 4c: Currency conversion ─────────────────────────
        from spectra.fx import convert_currency
        for t in categorised:
            if t.currency.upper() != settings.base_currency:
                original_amt = t.amount
                original_cur = t.currency.upper()
                converted = convert_currency(original_amt, original_cur, settings.base_currency, t.date)
                
                t.original_amount = original_amt
                t.original_currency = original_cur
                t.amount = converted
                t.currency = settings.base_currency

        for tx in categorised:
            apply_review_state(tx)

        existing_transfer_candidates = _load_existing_transfer_candidates(db, categorised)
        existing_transfer_updates = match_internal_transfers(
            categorised,
            existing_candidates=existing_transfer_candidates,
        )

        # ── Step 5: Write or print ───────────────────────────────
        if dry_run:
            from spectra.reporter import generate_html_report
            logger.info("🧪 DRY RUN — writing to HTML report")
            generate_html_report(categorised)
            logger.info("✅ Report generated and opened in browser")
        else:
            # Save history (date, amount, clean_name) for future temporal recurring detection
            db.save_history(categorised)
            for update in existing_transfer_updates:
                tx_id = str(update.pop("tx_id"))
                db.update_transaction(tx_id, **update)
            # Save merchant→category mappings for future local mode runs
            new_mappings = {t.clean_name: t.category for t in categorised if t.category != "Uncategorized"}
            db.save_merchant_categories_batch(new_mappings)
            sheets.append_transactions(categorised)
            logger.info("✅ Done — %d rows written to Google Sheets", len(categorised))

            # Refresh the Dashboard tab with updated charts
            try:
                refresh_dashboard(sheets)
            except Exception:
                logger.warning("⚠️ Dashboard update failed — continuing", exc_info=True)


def run_inbox(settings: Settings, inbox_dir: str, currency: str, dry_run: bool) -> None:
    """Process ALL csv/pdf files in a folder, then move them to processed/."""
    import shutil

    inbox = Path(inbox_dir)
    if not inbox.exists():
        logger.info("📂 Inbox folder %s does not exist — nothing to do", inbox)
        return

    files = sorted(
        list(inbox.glob("*.csv")) + list(inbox.glob("*.pdf")) + list(inbox.glob("*.ofx"))
    )
    if not files:
        logger.info("📂 No supported files in %s — nothing to do", inbox)
        return

    # Create processed/ as sibling of inbox/
    processed = inbox.parent / "processed"
    processed.mkdir(exist_ok=True)

    logger.info("📂 Found %d file(s) in %s", len(files), inbox)

    for bank_file in files:
        logger.info("─" * 40)
        logger.info("Processing: %s", bank_file.name)
        try:
            run(settings, file=str(bank_file), currency=currency, dry_run=dry_run)

            if not dry_run:
                dest = processed / bank_file.name
                if dest.exists():
                    from datetime import datetime, timezone
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    dest = processed / f"{bank_file.stem}_{ts}{bank_file.suffix}"
                shutil.move(str(bank_file), str(dest))
                logger.info("📦 Moved to %s", dest)
        except Exception:
            logger.exception("❌ Failed to process %s", bank_file.name)

    logger.info("✅ Inbox processing complete")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="spectra",
        description="Spectra — Bank CSV → AI → Google Sheets",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="Path to a bank CSV or PDF export")
    group.add_argument("--inbox", help="Path to folder with CSV/PDF files (processes all)")
    group.add_argument("--serve", action="store_true", help="Launch the web dashboard")

    parser.add_argument("--currency", default=None, help="Currency code (default: from .env BASE_CURRENCY)")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to Sheets")
    parser.add_argument("--port", type=int, default=8080, help="Port for the web dashboard (default: 8080)")

    args = parser.parse_args()
    settings = load_settings()

    if args.serve:
        from spectra.web.server import serve
        serve(port=args.port)
        return

    target_currency = args.currency or settings.base_currency

    try:
        if args.inbox:
            run_inbox(settings, inbox_dir=args.inbox, currency=target_currency, dry_run=args.dry_run)
        else:
            run(settings, file=args.file, currency=target_currency, dry_run=args.dry_run)
    except Exception:
        logger.exception("❌ Pipeline failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
