"""LLM-based transaction categoriser — supports Gemini and OpenAI."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger("spectra.ai")


# ── Data models ──────────────────────────────────────────────────


class CategorySuggestion(BaseModel):
    """Candidate category returned by the local classifier for review."""

    category: str
    score: float


class CategorisedTransaction(BaseModel):
    """A transaction after LLM processing."""

    original_description: str
    clean_name: str
    category: str
    amount: float
    currency: str
    original_amount: float | None = None
    original_currency: str | None = None
    conversion_rate: float | None = None
    conversion_date: str | None = None
    conversion_source: str = ""
    conversion_error: str = ""
    date: str
    id: str  # Hash ID for dedup
    statement_category: str = ""
    recurring: str = ""   # "Subscription", "Salary/Income", or empty
    counterpart: str = ""
    classification_source: str = ""
    category_confidence: float | None = None
    category_suggestions: list[CategorySuggestion] = Field(default_factory=list)
    needs_review: bool = False
    review_reason: str = ""
    account_name: str = ""
    account_id: str = ""
    account_balance: float | None = None
    account_balance_date: str = ""
    amount_decimal: str | None = None
    original_amount_decimal: str | None = None
    currency_source: str = "parser"
    import_batch_id: str = ""
    transfer_group_id: str = ""
    transfer_status: str = "none"
    excluded_from_spend: bool = False


# ── Prompt ───────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a personal finance assistant. Analyze bank transactions and return a JSON array.

CATEGORIZATION RULES — read carefully:
1. Clean the raw description into a human-readable merchant name \
   (e.g. "POS 1234 STRBKS IT" → "Starbucks").
2. ALWAYS reuse an existing category if one fits. Do not create a new one \
   just because the merchant is different.
3. When creating a new category, keep it BROAD. Prefer general categories \
   over specific ones.
   GOOD: "Food & Dining", "Entertainment", "Transport", "Shopping", \
   "Digital Subscriptions", "Health & Fitness", "Travel", "Transfers"
   BAD: "Food & Drink - Cafe", "Food & Drink - Fast Food", "Sports Events", \
   "Train Tickets" (too narrow — use "Transport"), "Domain Registration" \
   (too narrow — use "Digital Subscriptions")
   NOTE: Apple, Spotify, Netflix, cloud services, domains, SaaS → \
   use "Digital Subscriptions", NOT "Shopping".
4. INCOME RULE: if amount > 0, use an income-style category:
   "Salary", "Pension", "Transfer In", "Investment Return", "Cash Deposit", \
   or "Other Income". Never mix income into expense categories.
5. Set "recurring" based on these rules:
   - "Subscription" for recurring outgoing payments \
     (Netflix, Spotify, Apple, gym, insurance, phone plans, SaaS).
   - "Salary/Income" for recurring incoming payments (salary, pension).
   - "" (empty string) for one-off transactions.
6. Treat id, amount, currency, and date as source data. Return id unchanged.
   Never calculate, correct, or replace these fields.
7. Reply ONLY with a valid JSON array, no extra text.

OUTPUT FORMAT (JSON array):
[
  {
    "id": "<id copied exactly from input>",
    "original": "<raw description>",
    "clean_name": "<merchant name>",
    "category": "<broad category>",
    "amount": <amount as number>,
    "currency": "<currency code>",
    "date": "<YYYY-MM-DD>",
    "recurring": "<Subscription|Salary/Income|>"
  }
]
"""


def _build_user_prompt(
    transactions: list[dict[str, Any]],
    existing_categories: list[str],
) -> str:
    """Build the user-part of the prompt with transactions + categories."""
    cats_str = ", ".join(f'"{c}"' for c in existing_categories) if existing_categories else "(nessuna ancora)"

    lines = [
        f"CATEGORIE ESISTENTI: [{cats_str}]",
        "",
        "TRANSAZIONI DA CLASSIFICARE:",
    ]
    for t in transactions:
        counterpart = str(t.get("counterpart", "")).strip()
        counterpart_part = f', controparte: "{counterpart}"' if counterpart else ""
        lines.append(
            f'- id: "{t.get("id", "")}", descrizione: "{t["raw_description"]}", '
            f'importo: {t["amount"]}, valuta: {t["currency"]}, data: {t["date"]}{counterpart_part}'
        )

    return "\n".join(lines)


# ── Provider implementations ─────────────────────────────────────


def _call_gemini(
    user_prompt: str,
    api_key: str,
    model: str,
) -> str:
    """Call Google Gemini and return the raw text response."""
    import google.generativeai as genai  # type: ignore[import-untyped]

    genai.configure(api_key=api_key)
    llm = genai.GenerativeModel(
        model_name=model,
        system_instruction=_SYSTEM_PROMPT,
    )
    response = llm.generate_content(user_prompt)
    return response.text


def _call_openai(
    user_prompt: str,
    api_key: str,
    model: str,
) -> str:
    """Call OpenAI-compatible API and return the raw text response."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or "[]"


# ── Public API ───────────────────────────────────────────────────


def _extract_json(text: str) -> list[dict[str, Any]]:
    """Robustly extract a JSON array from LLM output."""
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            # Look for any key whose value is a list (e.g. "transactions", "data", etc.)
            for v in parsed.values():
                if isinstance(v, list):
                    return v
            # Single object — wrap in list
            return [parsed]
    except json.JSONDecodeError:
        pass

    # Try to find a JSON array buried in prose/markdown
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Provider output can echo full transaction descriptions; never log it.
    logger.error("Failed to parse provider output as JSON")
    return []


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def categorise(
    transactions: list[dict[str, Any]],
    existing_categories: list[str],
    *,
    provider: str,
    api_key: str,
    model: str,
    base_currency: str = "EUR",
) -> list[CategorisedTransaction]:
    """Send transactions to the LLM and return structured results.

    Parameters
    ----------
    transactions:
        List of dicts with keys: raw_description, amount, currency, date
    existing_categories:
        Categories already present in the spreadsheet.
    provider:
        "gemini" or "openai"
    api_key:
        The API key for the chosen provider.
    model:
        Model name/ID to use.
    """
    if not transactions:
        logger.info("No transactions to categorise")
        return []

    user_prompt = _build_user_prompt(transactions, existing_categories)
    logger.info(
        "Sending %d transaction(s) to %s (%s)", len(transactions), provider, model
    )

    # Call the provider
    if provider == "gemini":
        raw = _call_gemini(user_prompt, api_key, model)
    elif provider == "openai":
        raw = _call_openai(user_prompt, api_key, model)
    else:
        raise ValueError(f"Unknown AI provider: {provider!r}")


    # Parse JSON
    items = _extract_json(raw)
    logger.info("LLM returned %d categorised item(s)", len(items))

    results: list[CategorisedTransaction] = []
    by_id = {str(source.get("id")): source for source in transactions if source.get("id")}
    by_description: dict[str, list[dict[str, Any]]] = {}
    for source in transactions:
        by_description.setdefault(str(source.get("raw_description", "")), []).append(source)
    matched_ids: set[str] = set()
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("categorization item must be an object")
            explicit_id = str(item.get("id", "") or "")
            source = by_id.get(explicit_id) if explicit_id else None
            if source is not None:
                response_description = str(item.get("original", "") or "")
                if response_description and response_description != str(source.get("raw_description", "")):
                    logger.warning("Ignoring an AI result whose ID and source description disagree")
                    continue
            if source is None:
                source_description = str(item.get("original", "") or "")
                candidates = by_description.get(source_description, [])
                source = next(
                    (candidate for candidate in candidates if str(candidate.get("id", "")) not in matched_ids),
                    None,
                )
            if source is None:
                logger.warning("Ignoring unmatched AI categorization result")
                continue
            txn_id = str(source.get("id", ""))
            if not txn_id or txn_id in matched_ids:
                logger.warning("Ignoring duplicate or unidentified AI categorization result")
                continue
            category = str(item.get("category", "") or "").strip()
            clean_name = str(item.get("clean_name", "") or "").strip()
            if not category or not clean_name:
                raise ValueError("categorization output is missing category or merchant")
            amount = float(source["amount"])
            desc_str = str(source["raw_description"])

            results.append(
                CategorisedTransaction(
                    id=txn_id,
                    original_description=desc_str,
                    clean_name=clean_name,
                    category=category,
                    amount=amount,
                    amount_decimal=str(source.get("amount_decimal", "") or "") or None,
                    currency=str(source.get("currency", base_currency)),
                    date=str(source["date"]),
                    statement_category=str(source.get("statement_category", "") or ""),
                    recurring=_normalize_recurring(item.get("recurring", ""), amount),
                    counterpart=str(source.get("counterpart", "") or ""),
                    original_amount=None,
                    original_currency=None,
                    classification_source=provider,
                    account_name=str(source.get("account_name", "") or ""),
                    account_id=str(source.get("account_id", "") or ""),
                    account_balance=source.get("account_balance"),
                    account_balance_date=str(source.get("account_balance_date", "") or ""),
                )
            )
            matched_ids.add(txn_id)
        except Exception:
            logger.warning("Ignoring malformed AI categorization result")

    # An incomplete answer is visible to the user as a review item; it never
    # causes source rows to disappear or get paired by model-generated numbers.
    for source in transactions:
        txn_id = str(source.get("id", ""))
        if not txn_id or txn_id in matched_ids:
            continue
        amount = float(source.get("amount", 0))
        results.append(
            CategorisedTransaction(
                id=txn_id,
                original_description=str(source.get("raw_description", "")),
                clean_name=str(source.get("counterpart", "") or source.get("raw_description", "")),
                category="Uncategorized",
                amount=amount,
                amount_decimal=str(source.get("amount_decimal", "") or "") or None,
                currency=str(source.get("currency", base_currency)),
                date=str(source.get("date", "")),
                statement_category=str(source.get("statement_category", "") or ""),
                counterpart=str(source.get("counterpart", "") or ""),
                account_name=str(source.get("account_name", "") or ""),
                classification_source=provider,
                account_id=str(source.get("account_id", "") or ""),
                needs_review=True,
                review_reason="AI response incomplete or did not match this source transaction",
                account_balance=source.get("account_balance"),
                account_balance_date=str(source.get("account_balance_date", "") or ""),
            )
        )

    return results


def _normalize_recurring(value: Any, amount: float) -> str:
    """Normalize the AI's recurring field to a consistent label."""
    if isinstance(value, bool):
        if not value:
            return ""
        return "Salary/Income" if amount > 0 else "Subscription"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", "false", "no", "none"):
            return ""
        if v in ("true", "yes"):
            return "Salary/Income" if amount > 0 else "Subscription"
        if "subscription" in v:
            return "Subscription"
        if "salary" in v or "income" in v:
            return "Salary/Income"
        return value.strip()
    return ""
