"""Deterministic recurring transaction detection via pattern matching."""

from __future__ import annotations

import re

# ── Known subscription merchants (lowercase) ─────────────────────
_SUBSCRIPTION_MERCHANTS = {
    # Streaming
    "netflix", "spotify", "apple.com/bill", "apple.com", "apple one",
    "disney+", "disney plus", "prime video", "amazon prime",
    "youtube premium", "youtube music", "dazn", "crunchyroll",
    "paramount+", "peacock", "now tv",
    # Cloud & SaaS
    "icloud", "google one", "google storage", "dropbox", "onedrive",
    "adobe", "notion", "chatgpt", "openai", "github", "1password",
    "lastpass", "bitwarden", "canva", "figma", "slack",
    # Telecom & Internet
    "vodafone", "iliad", "ho mobile",
    "fastweb", "tiscali", "poste mobile",
    # Fitness & Health
    "palestra", "virgin active", "mcfit",
    "technogym", "anytime fitness",
    # Insurance
    "assicurazione", "insurance", "unipolsai", "generali",
    "allianz", "zurich",
    # Domain & Hosting
    "porkbun", "godaddy", "namecheap", "cloudflare", "digitalocean",
    "heroku", "vercel", "netlify",
    # Gaming
    "playstation", "xbox game pass", "nintendo",
    # News & Media
    "medium", "substack", "patreon",
}

# Short names that need word-boundary matching to avoid false positives
# (e.g. "tre" must NOT match "trenitalia", "sky" must NOT match "skyline")
_SHORT_MERCHANTS_REGEX = [
    re.compile(r"\btre\b", re.IGNORECASE),     # Italian telecom
    re.compile(r"\btim\b", re.IGNORECASE),      # Italian telecom
    re.compile(r"\bwind\b", re.IGNORECASE),     # Italian telecom
    re.compile(r"\bsky\b", re.IGNORECASE),      # Sky TV
    re.compile(r"\bhbo\b", re.IGNORECASE),      # HBO
    re.compile(r"\bhulu\b", re.IGNORECASE),     # Hulu
    re.compile(r"\baxa\b", re.IGNORECASE),      # AXA insurance
    re.compile(r"\baws\b", re.IGNORECASE),      # Amazon Web Services
    re.compile(r"\bgym\b", re.IGNORECASE),      # Generic gym
    re.compile(r"\bxbox\b", re.IGNORECASE),     # Xbox
]

# ── Known income patterns (lowercase) ────────────────────────────
_INCOME_PATTERNS = [
    re.compile(r"\bstipendio\b", re.IGNORECASE),
    re.compile(r"\bsalary\b", re.IGNORECASE),
    re.compile(r"\bpensione\b", re.IGNORECASE),
    re.compile(r"\bpension\b", re.IGNORECASE),
    re.compile(r"\bstipendi?\s+e\s+pensioni\b", re.IGNORECASE),
    re.compile(r"\baccredito\s+(?:stipendio|competenze|emolumenti)\b", re.IGNORECASE),
    re.compile(r"\bbonifico\s+(?:a\s+(?:vostro|vs)\s+favore|ricevuto)\b", re.IGNORECASE),
    re.compile(r"\bpayroll\b", re.IGNORECASE),
    re.compile(r"\bwage\b", re.IGNORECASE),
    re.compile(r"\bcompensation\b", re.IGNORECASE),
]


def _detect_static_pattern(
    clean_name: str,
    original_description: str,
    amount: float,
    statement_category: str = "",
) -> str:
    """Return 'Subscription', 'Salary/Income', or '' based purely on hardcoded pattern matching."""
    combined = f"{clean_name} {original_description} {statement_category}".lower()

    # ── Subscriptions: long merchant names (substring match) ──────
    for merchant in _SUBSCRIPTION_MERCHANTS:
        if merchant in combined:
            return "Subscription"

    # ── Subscriptions: short merchant names (word-boundary match) ─
    for pattern in _SHORT_MERCHANTS_REGEX:
        if pattern.search(combined):
            return "Subscription"

    # ── Recurring income (positive amount only) ───────────────────
    if amount > 0:
        for pattern in _INCOME_PATTERNS:
            if pattern.search(combined):
                return "Salary/Income"

    return ""


def apply_recurring_tags(
    transactions: list[Any],
    history: dict[str, list[tuple[str, float]]],
) -> None:
    """Apply recurring tags in-place by mixing static patterns and historical temporal matching."""
    from datetime import datetime

    for t in transactions:
        # 1. Try static pattern matching first (fastest and most accurate for known entities)
        static_match = _detect_static_pattern(
            t.clean_name,
            t.original_description,
            t.amount,
            getattr(t, "statement_category", ""),
        )
        if static_match:
            t.recurring = static_match
            # Add to running history so subsequent items in the loop see it
            history.setdefault(t.clean_name, []).append((t.date, t.amount))
            continue
            
        # 2. Hybrid Temporal Matching (only for uncategorized)
        if not t.recurring:
            # Get historical entries for this specific merchant
            past_entries = history.get(t.clean_name, [])
            
            # If we don't have past entries, maybe we saw it earlier in this SAME batch
            # We add it to history now for FUTURE transactions in this loop to find
            
            if past_entries:
                try:
                    current_date = datetime.strptime(t.date, "%Y-%m-%d").date()
                    
                    matched = False
                    for p_date_str, p_amount in past_entries:
                        # Skip if it's the exact same transaction/date
                        if p_date_str == t.date:
                            continue
                            
                        # Tolerance: amount must be within 15% (prices change, FX rates change)
                        if abs(t.amount - p_amount) <= max(3.0, abs(p_amount * 0.15)):
                            p_date = datetime.strptime(p_date_str, "%Y-%m-%d").date()
                            days_diff = abs((current_date - p_date).days)
                            
                            # Standard billing cycles
                            # Weekly: ~7 days
                            # Monthly: 28-31 days
                            # Yearly: 360-370 days
                            
                            if (6 <= days_diff <= 8) or (27 <= days_diff <= 32) or (355 <= days_diff <= 370):
                                t.recurring = "Salary/Income" if t.amount > 0 else "Subscription"
                                matched = True
                                break
                    
                    if matched:
                        # Add to running history
                        history.setdefault(t.clean_name, []).append((t.date, t.amount))
                        continue
                        
                except Exception:
                    # Ignore parsing errors and just fall back to no tag
                    pass
            
            # Record it for the future items in this batch
            history.setdefault(t.clean_name, []).append((t.date, t.amount))


def detect_recurring_kind(clean_name: str, original_description: str, amount: float) -> str:
    """Public helper used by the web API to classify recurring transactions."""
    return _detect_static_pattern(clean_name, original_description, amount)
