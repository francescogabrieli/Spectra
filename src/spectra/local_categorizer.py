"""Offline local categoriser — merchant memory + fuzzy match + ML classifier."""

from __future__ import annotations

import logging
import re
from typing import Any

from spectra.ai import CategorySuggestion, CategorisedTransaction

logger = logging.getLogger("spectra.local")


# ── Adaptive ML confidence thresholds ───────────────────────────

_DEFAULT_ML_THRESHOLD = 0.20
_DEFAULT_ML_MARGIN = 0.08
_CATEGORY_ML_THRESHOLDS: dict[str, float] = {
    # Ambiguous high-volume spend classes use a stricter threshold
    "Shopping": 0.30,
    "Groceries": 0.28,
    "Food & Dining": 0.28,
    "Entertainment": 0.27,
    "Transport": 0.26,
    "Travel": 0.26,
    "Utilities": 0.24,
    "Health": 0.24,
    "Education": 0.24,
    "Health & Fitness": 0.24,
    # More specific classes can use a slightly lower threshold
    "Digital Subscriptions": 0.22,
    "Insurance": 0.22,
    "Taxes": 0.20,
    "Transfer": 0.19,
    "Transfer In": 0.18,
    "Reimbursement": 0.18,
    "Cash Withdrawal": 0.18,
    "Cash Deposit": 0.18,
    "Salary": 0.16,
    "Pension": 0.16,
}

_CATEGORY_ML_MARGINS: dict[str, float] = {
    "Shopping": 0.12,
    "Groceries": 0.10,
    "Food & Dining": 0.10,
    "Entertainment": 0.10,
    "Transport": 0.09,
    "Travel": 0.09,
    "Utilities": 0.08,
    "Health": 0.08,
    "Education": 0.08,
    "Health & Fitness": 0.08,
    "Digital Subscriptions": 0.07,
    "Insurance": 0.07,
    "Taxes": 0.07,
    "Transfer": 0.06,
    "Transfer In": 0.06,
    "Reimbursement": 0.06,
    "Cash Withdrawal": 0.06,
    "Cash Deposit": 0.06,
    "Salary": 0.05,
    "Pension": 0.05,
}


def _ml_threshold_for_category(category: str) -> float:
    return _CATEGORY_ML_THRESHOLDS.get(category, _DEFAULT_ML_THRESHOLD)


def _ml_margin_for_category(category: str) -> float:
    return _CATEGORY_ML_MARGINS.get(category, _DEFAULT_ML_MARGIN)


# ── Hybrid fallback (keyword / regex) ───────────────────────────

_SALARY_RE = re.compile(
    r"(?i)(?:"
    r"\b(stipendio|salary|payroll|n[oó]mina|salario|sal[aá]rio|salaire|gehalt|lohn)\b|"
    r"\bstipendi\s+e\s+pensioni\b|"
    r"\baccredito\s+stipendio\b|"
    r"\baccredito\s+retribuzione\b|"
    r"\baccredito\s+salario\b|"
    r"\baccredito\s+emolumenti\b|"
    r"\baccredito\s+competenze\b|"
    r"\baccredito\s+(?:mensile|mensili)\b|"
    r"\bbonafica\s+stipendio\b|"
    r"\bbonifico\s+stipendio\b|"
    r"\bretribuzione\b|"
    r"\bemolumenti\b|"
    r"\bcompetenze\s+mensili\b|"
    r"virement\s+salaire|"
    r"gehaltseingang|"
    r"payroll\s+credit"
    r")"
)
_TRANSFER_IN_RE = re.compile(
    r"(?i)\b(bonifico ricevuto|accredito bonifico|bonifico in entrata|incoming transfer|transfer received|virement re\u00e7u|transferencia recibida)\b"
)
_TRANSFER_RE = re.compile(
    r"(?i)\b(bonifico|bank transfer|wire transfer|virement|transferencia|sepa transfer|giroconto)\b"
)
_UTILITIES_RE = re.compile(
    r"(?i)\b(bolletta|utenza|gas luce|electricity|water bill|telecom|telefonica|tim|vodafone|wind tre|windtre|iliad|fastweb|enel|a2a|iren|hera|acea)\b"
)
_RECURRING_DEBIT_RE = re.compile(
    r"(?i)\b(addebito sdd|direct debit|prélèvement sepa|prelevement sepa|lastschrift)\b"
)
_SUBSCRIPTION_BRAND_RE = re.compile(
    r"(?i)\b(netflix|spotify|apple|youtube|disney|dazn|prime|icloud|google one|openai|chatgpt|adobe|dropbox|github|notion)\b"
)


def _hybrid_keyword_fallback(
    raw: str,
    clean_name: str,
    amount: float,
    statement_category: str = "",
) -> str | None:
    """Rule-based fallback used when ML is missing or below confidence threshold."""
    text = f"{raw} {clean_name} {statement_category}"

    if _SALARY_RE.search(text) and amount > 0:
        return "Salary"

    if _TRANSFER_IN_RE.search(text) and amount > 0:
        return "Transfer In"

    if _RECURRING_DEBIT_RE.search(text) and _SUBSCRIPTION_BRAND_RE.search(text) and amount < 0:
        return "Digital Subscriptions"

    if _UTILITIES_RE.search(text):
        return "Utilities"

    if _TRANSFER_RE.search(text):
        return "Transfer In" if amount > 0 else "Transfer"

    return None


# ── Merchant name extraction ─────────────────────────────────────

_STRIP_PREFIXES = re.compile(
    r"(?i)^(?:"
    # Italian
    r"POS\s*\d*\s*|"
    r"ADDEBITO\s+SDD\s+|"
    r"ADDEBITO\s+DIRETTO\s+|"
    r"PAGAMENTO\s+(?:SU\s+POS\s+(?:ESTERO\s+)?)?|"
    r"PRELIEVO\s+(?:BANCOMAT\s+)?|"
    r"COMMISSIONE\s+|"
    r"CANONE\s+|"
    r"ACCREDITO\s+|"
    r"BONIFICO\s+(?:ISTANTANEO\s+)?(?:DA\s+VOI\s+DISPOSTO\s+)?A\s+FAVORE\s+DI\s+|"
    r"BONIFICO\s+(?:ISTANTANEO\s+)?A\s+VOSTRO\s+FAVORE\s+DISPOSTO\s+DA\s+|"
    r"RICARICA\s+(?:TELEFONICA\s+)?|"
    # English / UK / US / international
    r"CARD\s+PAYMENT\s+(?:TO\s+)?|"
    r"DIRECT\s+DEBIT\s+|"
    r"CONTACTLESS\s+(?:PAYMENT\s+)?|"
    r"FASTER\s+PAYMENT\s+(?:TO\s+)?|"
    r"STANDING\s+ORDER\s+(?:TO\s+)?|"
    r"BANK\s+TRANSFER\s+(?:TO\s+)?|"
    r"PURCHASE\s+AT\s+|"
    r"DEBIT\s+CARD\s+|"
    r"ACH\s+(?:DEBIT\s+|PAYMENT\s+)?|"
    # German
    r"Kartenzahlung\s+|"
    r"Lastschrift\s+|"
    r"\u00dcberweisung\s+(?:an\s+)?|"
    r"Dauerauftrag\s+(?:an\s+)?|"
    r"Geldautomat\s+|"
    # French
    r"Paiement\s+(?:par\s+carte\s+)?(?:CB\s+)?|"
    r"Pr\u00e9l\u00e8vement\s+(?:SEPA\s+)?|"
    r"Virement\s+(?:SEPA\s+)?|"
    r"Retrait\s+(?:DAB\s+)?|"
    # Spanish / Portuguese
    r"Pago\s+(?:con\s+tarjeta\s+)?|"
    r"Transferencia\s+(?:SEPA\s+)?|"
    r"Domiciliaci\u00f3n\s+"
    r")"
)

_STRIP_SUFFIXES = re.compile(
    r"(?i)(?:"
    r"\s+(?:IT|DE|FR|ES|NL|GB|US|EU|COM|SPA|SRL|SARL|GMBH|LTD|INC|AG|AB|BV|SA|PLC|NV|KG|OHG|SAS|EURL|OY|AS|PTY|SLU)\s*$|"
    r"\s+\d{2}/\d{2}/\d{2,4}$|"
    r"\s+CARTA\s.*$|"
    r"\s+ATM\s+\d+.*$|"
    r"\s+VIA\s+.*$"
    r")"
)

_STRIP_NOISE = re.compile(
    r"(?i)(?:"
    r"\bCarta\s+n\.?\s*\d*[\*X]+[\d\*X\s]+\b|"
    r"\b[A-Z0-9]{15,}\b|"
    r"\bABI\s+\d+\b|"
    r"\bCAB\s+\d+\b|"
    r"\bCOD\.?\s*\d+/?\d*\b|"
    r"\(\s*ctv\..*?\)|"
    r"\b\d{2}[/.]\d{2}[/.]\d{2,4}\b|"
    r"\b\d{4,}\b|"
    r"\*+"        # bare asterisks (e.g. "Massaua Ci*")
    r")"
)


def _extract_merchant_name(raw: str) -> str:
    """Extract a clean merchant name from a raw banking description."""
    text = raw.strip()

    # Strip banking prefixes
    text = _STRIP_PREFIXES.sub("", text)

    # Strip noise (card masks, trace IDs, dates, long numbers)
    text = _STRIP_NOISE.sub(" ", text)

    # Strip suffixes (country codes, legal forms, trailing dates)
    text = _STRIP_SUFFIXES.sub("", text)

    # Collapse whitespace and pipes
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .|,;:-*")

    # Deduplicate repeated words (e.g. "Massaua Ci Massaua Ci Torino" → "Massaua Ci Torino")
    words = text.split()
    seen: list[str] = []
    for w in words:
        if w.lower() not in [s.lower() for s in seen]:
            seen.append(w)
    text = " ".join(seen)

    if not text:
        return raw.strip()

    # Title-case for readability
    return text.title()


# ── Fuzzy matching ───────────────────────────────────────────────


def _fuzzy_match(
    name: str,
    known_merchants: dict[str, str],
    threshold: int = 75,
) -> tuple[str, str] | None:
    """Find the closest known merchant using fuzzy string matching.

    Returns (matched_merchant_name, category) or None.
    """
    if not known_merchants or not name:
        return None

    try:
        from rapidfuzz import fuzz
    except ImportError:
        logger.debug("rapidfuzz not installed — skipping fuzzy match")
        return None

    best_score = 0
    best_merchant = ""
    best_category = ""

    for merchant, category in known_merchants.items():
        score = fuzz.token_sort_ratio(name.lower(), merchant.lower())
        if score > best_score:
            best_score = score
            best_merchant = merchant
            best_category = category

    if best_score >= threshold:
        logger.debug("Fuzzy match: %r → %r (score=%d)", name, best_merchant, best_score)
        return best_merchant, best_category

    return None


# ── Public API ───────────────────────────────────────────────────


def categorise_local(
    transactions: list[dict[str, Any]],
    merchant_db: dict[str, str],
    ml_classifier: Any | None = None,
) -> list[CategorisedTransaction]:
    """Categorise transactions using merchant memory + ML classifier.

    Cascade order:
    1. Exact merchant memory (clean_name lookup)
    2. Fuzzy match against known merchants
    3. ML classifier (always active — bootstrapped with seed data)
    4. Hybrid keyword/regex fallback (strong patterns)
    5. Fallback → "Uncategorized"

    Parameters
    ----------
    transactions:
        List of dicts with keys: raw_description, amount, currency, date
        and optional counterpart
    merchant_db:
        Dict of {clean_name: category} from the local DB.
    ml_classifier:
        Trained sklearn Pipeline (from ml_classifier.train_classifier).

    Returns
    -------
    List of CategorisedTransaction objects.
    """
    import hashlib

    if not transactions:
        logger.info("No transactions to categorise")
        return []

    logger.info("Categorising %d transaction(s) locally", len(transactions))

    results: list[CategorisedTransaction] = []
    stats = {"exact": 0, "fuzzy": 0, "ml": 0, "hybrid": 0, "fallback": 0}

    for t in transactions:
        raw = t["raw_description"]
        amount = float(t.get("amount", 0))
        currency = t.get("currency", "EUR")
        date = t.get("date", "")
        counterpart = str(t.get("counterpart", "") or "").strip()
        statement_category = str(t.get("statement_category", "") or "").strip()

        # Generate ID
        raw_id = f"{date}:{raw}:{amount}"
        txn_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()

        # Step 1: Extract clean merchant name
        clean_name = counterpart or _extract_merchant_name(raw)
        classification_source = ""
        category_confidence: float | None = None
        category_suggestions: list[CategorySuggestion] = []
        needs_review = False

        # Step 2: Exact merchant memory
        if clean_name in merchant_db:
            category = merchant_db[clean_name]
            classification_source = "exact"
            category_confidence = 1.0
            stats["exact"] += 1
            logger.debug("Exact match: %r → %s", clean_name, category)

        # Step 3: Fuzzy match against known merchants
        elif (fuzzy_result := _fuzzy_match(clean_name, merchant_db)):
            matched_name, category = fuzzy_result
            clean_name = matched_name  # Use the canonical name
            classification_source = "fuzzy"
            stats["fuzzy"] += 1

        # Step 4: ML classifier (always active thanks to seed data)
        elif ml_classifier is not None:
            try:
                from spectra.ml_classifier import predict_details

                prediction = predict_details(ml_classifier, raw, clean_name=clean_name)
                pred_cat = prediction.category
                confidence = prediction.confidence
                margin = prediction.margin
                min_confidence = _ml_threshold_for_category(pred_cat)
                min_margin = _ml_margin_for_category(pred_cat)
                category_confidence = confidence
                category_suggestions = list(prediction.suggestions[:3])
                if confidence >= min_confidence and margin >= min_margin:
                    category = pred_cat
                    classification_source = "ml"
                    stats["ml"] += 1
                    logger.debug(
                        "ML: %r → %s (%.0f%%, threshold=%.0f%%, margin=%.0f%%, min_margin=%.0f%%)",
                        raw[:50], category, confidence * 100, min_confidence * 100, margin * 100, min_margin * 100,
                    )
                else:
                    fallback_cat = _hybrid_keyword_fallback(raw, clean_name, amount, statement_category)
                    if fallback_cat:
                        category = fallback_cat
                        classification_source = "hybrid"
                        needs_review = True
                        stats["hybrid"] += 1
                        logger.debug(
                            "Hybrid fallback: %r → %s (ML %.0f%% / %.0f%%, margin %.0f%% / %.0f%%)",
                            raw[:50], category, confidence * 100, min_confidence * 100, margin * 100, min_margin * 100,
                        )
                    else:
                        category = "Uncategorized"
                        classification_source = "fallback"
                        needs_review = True
                        stats["fallback"] += 1
                        logger.debug(
                            "ML low confidence for %r (%.0f%% / %.0f%%, margin %.0f%% / %.0f%%) → Uncategorized",
                            raw[:50], confidence * 100, min_confidence * 100, margin * 100, min_margin * 100,
                        )
            except Exception:
                fallback_cat = _hybrid_keyword_fallback(raw, clean_name, amount, statement_category)
                if fallback_cat:
                    category = fallback_cat
                    classification_source = "hybrid"
                    needs_review = True
                    stats["hybrid"] += 1
                else:
                    category = "Uncategorized"
                    classification_source = "fallback"
                    needs_review = True
                    stats["fallback"] += 1

        # Step 5: Hybrid fallback (when ML is unavailable)
        elif (fallback_cat := _hybrid_keyword_fallback(raw, clean_name, amount, statement_category)):
            category = fallback_cat
            classification_source = "hybrid"
            needs_review = True
            stats["hybrid"] += 1

        # Step 6: Fallback
        else:
            category = "Uncategorized"
            classification_source = "fallback"
            needs_review = True
            stats["fallback"] += 1

        # Income override: positive amounts classified as generic expense categories
        # Only apply if the categorizer placed it in a non-income, non-transfer category
        # AND it's a positive credit transaction. Never override explicit income/transfer categories.
        _INCOME_CATS = {
            "Salary", "Pension", "Transfer In", "Transfer",
            "Cash Deposit", "Other Income", "Investment Return",
            "Reimbursement", "Uncategorized",
        }
        _EXPLICIT_EXPENSE_CATS = {
            "Shopping", "Groceries", "Food & Dining", "Entertainment",
            "Transport", "Travel", "Health", "Health & Fitness",
            "Digital Subscriptions", "Insurance", "Utilities",
            "Education", "Taxes", "Cash Withdrawal",
        }
        if amount > 0 and category not in _INCOME_CATS:
            # Only relabel to Other Income if it landed on a clear expense category
            # via a low-confidence path — don't override hybrid/keyword income signals
            if category in _EXPLICIT_EXPENSE_CATS and classification_source not in ("hybrid", "exact", "fuzzy"):
                category = "Other Income"
                category_confidence = None
                category_suggestions = []
                if not classification_source:
                    classification_source = "hybrid"

        recurring = ""
        results.append(
            CategorisedTransaction(
                id=txn_id,
                original_description=raw,
                clean_name=clean_name,
                category=category,
                amount=amount,
                currency=currency,
                date=date,
                statement_category=statement_category,
                recurring=recurring,
                original_amount=None,
                original_currency=None,
                classification_source=classification_source,
                category_confidence=category_confidence,
                category_suggestions=category_suggestions,
                needs_review=needs_review,
            )
        )

    logger.info(
        "Local categorisation: %d exact, %d fuzzy, %d ML, %d hybrid, %d fallback",
        stats["exact"], stats["fuzzy"], stats["ml"], stats["hybrid"], stats["fallback"],
    )

    return results
