"""Review-state and transfer-matching helpers."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any

TRANSFER_STATUS_NONE = "none"
TRANSFER_STATUS_SUGGESTED = "suggested"
TRANSFER_STATUS_CONFIRMED = "confirmed"
TRANSFER_STATUS_DISMISSED = "dismissed"

REVIEW_REASON_LOW_CONFIDENCE = "low_confidence_category"
REVIEW_REASON_AMBIGUOUS_MERCHANT = "ambiguous_merchant"
REVIEW_REASON_TRANSFER_CANDIDATE = "transfer_candidate"

_TRANSFER_CATEGORIES = {"Transfer", "Transfer In"}
_TRANSFER_TEXT_RE = re.compile(
    r"(?i)\b("
    r"bonifico|bank transfer|wire transfer|incoming transfer|transfer received|"
    r"transferencia|virement|giroconto|sepa transfer|disposizione di pagamento|"
    r"internal transfer|trasferimento"
    r")\b"
)
_AMBIGUOUS_PREFIX_RE = re.compile(
    r"(?i)\b("
    r"pos|card payment|direct debit|ach|purchase|contactless|pagamento|"
    r"addebito|bonifico|prelievo|commissione|canone|accredito"
    r")\b"
)


def _tx_get(tx: Any, name: str, default: Any = None) -> Any:
    if isinstance(tx, dict):
        return tx.get(name, default)
    return getattr(tx, name, default)


def _tx_set(tx: Any, name: str, value: Any) -> None:
    if isinstance(tx, dict):
        tx[name] = value
        return
    setattr(tx, name, value)


def build_import_batch_id() -> str:
    """Return a compact, sortable batch id for imports."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = uuid.uuid4().hex[:8]
    return f"batch-{timestamp}-{suffix}"


def build_transfer_group_id(*tx_ids: str) -> str:
    """Create a stable transfer-group id from the involved transaction ids."""
    joined = "|".join(sorted(str(tx_id) for tx_id in tx_ids if tx_id))
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    return f"xfer-{digest}"


def is_transfer_like(tx: Any) -> bool:
    """Return True when the transaction likely represents a transfer."""
    category = str(_tx_get(tx, "category", "") or "")
    if category in _TRANSFER_CATEGORIES:
        return True

    text = " ".join(
        str(_tx_get(tx, field, "") or "")
        for field in ("clean_name", "original_description", "counterpart")
    )
    return bool(_TRANSFER_TEXT_RE.search(text))


def is_ambiguous_merchant(clean_name: str, original_description: str) -> bool:
    """Detect merchant names that are still too close to noisy raw bank text."""
    merchant = str(clean_name or "").strip()
    raw = str(original_description or "").strip()

    if not merchant:
        return True
    if len(merchant) < 3:
        return True
    if merchant.lower() == raw.lower() and (_AMBIGUOUS_PREFIX_RE.search(raw) or any(ch.isdigit() for ch in raw)):
        return True
    if "|" in merchant:
        return True
    if _AMBIGUOUS_PREFIX_RE.match(merchant):
        return True
    return False


def evaluate_review_state(tx: Any) -> tuple[bool, str]:
    """Return the persisted review state for a transaction-like object."""
    transfer_status = str(_tx_get(tx, "transfer_status", TRANSFER_STATUS_NONE) or TRANSFER_STATUS_NONE)
    if transfer_status == TRANSFER_STATUS_SUGGESTED and _tx_get(tx, "transfer_group_id", ""):
        return True, REVIEW_REASON_TRANSFER_CANDIDATE

    category = str(_tx_get(tx, "category", "Uncategorized") or "Uncategorized")
    if category == "Uncategorized":
        return True, REVIEW_REASON_LOW_CONFIDENCE

    source = str(_tx_get(tx, "classification_source", "") or "").strip().lower()
    flagged_uncertain = bool(_tx_get(tx, "needs_review", False))
    confidence = _tx_get(tx, "category_confidence")

    if source in {"fallback", "hybrid"} and flagged_uncertain:
        return True, REVIEW_REASON_LOW_CONFIDENCE
    if source == "ml" and flagged_uncertain:
        return True, REVIEW_REASON_LOW_CONFIDENCE
    if source in {"openai", "gemini"} and category == "Uncategorized":
        return True, REVIEW_REASON_LOW_CONFIDENCE
    if confidence is not None and flagged_uncertain:
        return True, REVIEW_REASON_LOW_CONFIDENCE

    clean_name = str(_tx_get(tx, "clean_name", "") or "")
    original_description = str(_tx_get(tx, "original_description", "") or "")
    if is_ambiguous_merchant(clean_name, original_description):
        return True, REVIEW_REASON_AMBIGUOUS_MERCHANT

    return False, ""


def apply_review_state(tx: Any) -> tuple[bool, str]:
    """Update a transaction-like object in-place with the current review state."""
    needs_review, review_reason = evaluate_review_state(tx)
    _tx_set(tx, "needs_review", needs_review)
    _tx_set(tx, "review_reason", review_reason)
    return needs_review, review_reason


def match_internal_transfers(
    transactions: list[Any],
    *,
    existing_candidates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Annotate likely internal transfers and return existing-row updates."""
    existing_candidates = existing_candidates or []
    if not transactions:
        return []

    used_ids: set[str] = set()
    external_updates: list[dict[str, Any]] = []
    pool: list[tuple[str, Any]] = [("new", tx) for tx in transactions] + [
        ("existing", candidate) for candidate in existing_candidates
    ]

    def score_pair(left: Any, right: Any) -> tuple[int, int]:
        left_date = datetime.strptime(str(_tx_get(left, "date")), "%Y-%m-%d").date()
        right_date = datetime.strptime(str(_tx_get(right, "date")), "%Y-%m-%d").date()
        day_gap = abs((left_date - right_date).days)
        both_transfer = 0 if is_transfer_like(left) and is_transfer_like(right) else 1
        return day_gap, both_transfer

    for tx in sorted(transactions, key=lambda item: (str(_tx_get(item, "date", "")), str(_tx_get(item, "id", "")))):
        tx_id = str(_tx_get(tx, "id", "") or "")
        if not tx_id or tx_id in used_ids:
            continue
        if str(_tx_get(tx, "transfer_status", TRANSFER_STATUS_NONE)) in {
            TRANSFER_STATUS_CONFIRMED,
            TRANSFER_STATUS_DISMISSED,
        }:
            continue

        amount = float(_tx_get(tx, "amount", 0) or 0)
        if amount == 0:
            continue

        matches: list[tuple[tuple[int, int], str, Any]] = []
        for origin, candidate in pool:
            candidate_id = str(_tx_get(candidate, "id", "") or _tx_get(candidate, "tx_id", "") or "")
            if not candidate_id or candidate_id == tx_id or candidate_id in used_ids:
                continue

            candidate_amount = float(_tx_get(candidate, "amount", 0) or 0)
            if candidate_amount == 0 or amount * candidate_amount >= 0:
                continue
            if abs(abs(amount) - abs(candidate_amount)) > 0.01:
                continue

            tx_date = datetime.strptime(str(_tx_get(tx, "date")), "%Y-%m-%d").date()
            candidate_date = datetime.strptime(str(_tx_get(candidate, "date")), "%Y-%m-%d").date()
            if abs((tx_date - candidate_date).days) > 3:
                continue
            if not (is_transfer_like(tx) or is_transfer_like(candidate)):
                continue

            matches.append((score_pair(tx, candidate), origin, candidate))

        if not matches:
            continue

        matches.sort(key=lambda item: item[0])
        _score, origin, partner = matches[0]
        partner_id = str(_tx_get(partner, "id", "") or _tx_get(partner, "tx_id", "") or "")
        group_id = build_transfer_group_id(tx_id, partner_id)

        for item in (tx, partner):
            _tx_set(item, "transfer_group_id", group_id)
            _tx_set(item, "transfer_status", TRANSFER_STATUS_SUGGESTED)
            _tx_set(item, "excluded_from_spend", False)
            _tx_set(item, "needs_review", True)
            _tx_set(item, "review_reason", REVIEW_REASON_TRANSFER_CANDIDATE)

        used_ids.add(tx_id)
        used_ids.add(partner_id)

        if origin == "existing":
            external_updates.append(
                {
                    "tx_id": partner_id,
                    "transfer_group_id": group_id,
                    "transfer_status": TRANSFER_STATUS_SUGGESTED,
                    "excluded_from_spend": False,
                    "needs_review": True,
                    "review_reason": REVIEW_REASON_TRANSFER_CANDIDATE,
                }
            )

    return external_updates
