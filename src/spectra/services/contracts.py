"""Versioned JSON contracts shared by the local Spectra services."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# These operation names form a private, versioned compatibility adapter for
# dashboard actions while domain-specific commands are extracted. No SQL or
# table identifiers are part of this contract.
LEDGER_OPERATIONS = frozenset({
    "is_seen", "mark_seen", "mark_seen_batch", "find_legacy_transaction",
    "find_transfer_candidates", "get_category_rules", "get_merchant_categories",
    "get_merchant_history", "get_overrides", "get_training_data", "get_app_setting",
    "set_app_setting", "get_budget_limits", "get_budget_limits_exact",
    "count_non_base_budget_limits", "save_budget_limit", "list_legacy_unknown_currency",
    "confirm_legacy_currencies", "list_accounts", "get_category_rule",
    "add_category_rule", "move_category_rule", "update_category_rule",
    "delete_category_rule", "save_overrides", "record_learning_feedback",
    "get_recent_learning_feedback", "reapply_learning_to_history", "count_review_queue",
    "count_confirmed_transfer_groups", "reset_all_data", "get_review_queue",
    "list_all_review_queue", "list_transactions", "list_all_transactions",
    "get_transaction", "get_transactions_by_transfer_group", "update_transaction",
    "update_transactions", "save_merchant_category", "save_merchant_categories_batch",
    "list_known_categories",
})


class OperationRequest(BaseModel):
    """Allowlisted ledger operation request for internal service clients."""

    model_config = ConfigDict(extra="forbid")
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class OperationResponse(BaseModel):
    result: Any = None


class CommitImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str
    transactions: list[dict[str, Any]]
    learning_feedback: list[dict[str, Any]] = Field(default_factory=list)
    merchant_mappings: dict[str, str] = Field(default_factory=dict)
    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    existing_transfer_updates: list[dict[str, Any]] = Field(default_factory=list)


class CommitImportResponse(BaseModel):
    ok: bool = True
    already_committed: bool = False
    transaction_count: int = 0


class CategorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transactions: list[dict[str, Any]]
    categories: list[str] = Field(default_factory=list)
    provider: str = "local"
    base_currency: str = "EUR"
    training_data: list[dict[str, str]] = Field(default_factory=list)
    merchant_categories: dict[str, str] = Field(default_factory=dict)


class FxConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str
    from_currency: str
    to_currency: str
    rate_date: str
    persist_rate: bool = True


class ImportConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transactions: list[dict[str, Any]]

