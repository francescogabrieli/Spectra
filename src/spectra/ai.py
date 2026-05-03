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
    date: str
    id: str  # Hash ID for dedup
    recurring: str = ""   # "Subscription", "Salary/Income", or empty
    classification_source: str = ""
    category_confidence: float | None = None
    category_suggestions: list[CategorySuggestion] = Field(default_factory=list)
    needs_review: bool = False


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
6. Reply ONLY with a valid JSON array, no extra text.

OUTPUT FORMAT (JSON array):
[
  {
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
            f'- descrizione: "{t["raw_description"]}", '
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

    logger.error("Failed to parse LLM response as JSON: %s", text[:300])
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
    logger.debug("Prompt:\n%s", user_prompt)

    # Call the provider
    if provider == "gemini":
        raw = _call_gemini(user_prompt, api_key, model)
    elif provider == "openai":
        raw = _call_openai(user_prompt, api_key, model)
    else:
        raise ValueError(f"Unknown AI provider: {provider!r}")

    logger.debug("Raw LLM response:\n%s", raw)

    # Parse JSON
    items = _extract_json(raw)
    logger.info("LLM returned %d categorised item(s)", len(items))

    import hashlib
    results: list[CategorisedTransaction] = []
    for item in items:
        try:
            amount = float(item.get("amount", 0))

            # Create a deterministic ID for dedup (used later in the pipeline)
            date_str = item.get("date", "")
            desc_str = item.get("original", "")
            amount_str = str(amount)
            raw_id = f"{date_str}:{desc_str}:{amount_str}"
            txn_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()

            results.append(
                CategorisedTransaction(
                    id=txn_id,
                    original_description=desc_str,
                    clean_name=item.get("clean_name", ""),
                    category=item.get("category", "Other"),
                    amount=amount,
                    currency=item.get("currency", base_currency),
                    date=date_str,
                    recurring=_normalize_recurring(item.get("recurring", ""), amount),
                    # These will be filled by the FX converter in the pipeline if needed
                    original_amount=None,
                    original_currency=None,
                )
            )
        except Exception:
            logger.warning("Skipping malformed LLM item: %s", item)

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
