"""Centralised configuration — every setting comes from env vars / .env file."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("spectra")


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _PROJECT_ROOT / ".env"
_LEGACY_DB_NOTICE_KEYS: set[str] = set()


def _log_legacy_db_notice_once(key: str, message: str, *args: object) -> None:
    if key in _LEGACY_DB_NOTICE_KEYS:
        return
    logger.warning(message, *args)
    _LEGACY_DB_NOTICE_KEYS.add(key)


def _prepare_db_path(db_path: Path) -> Path:
    """Resolve the active DB path and migrate the legacy default DB name if needed."""
    resolved = db_path if db_path.is_absolute() else (_PROJECT_ROOT / db_path).resolve()
    if resolved.name != "spectra.db":
        return resolved

    legacy_path = resolved.with_name("prism.db")
    if resolved.exists():
        if legacy_path.exists():
            _log_legacy_db_notice_once(
                f"both:{resolved}:{legacy_path}",
                "Both Spectra DB paths exist. Using %s and leaving legacy %s untouched.",
                resolved,
                legacy_path,
            )
        return resolved

    if not legacy_path.exists():
        return resolved

    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy_path.replace(resolved)
        action = "renamed"
    except OSError:
        shutil.copy2(legacy_path, resolved)
        action = "copied"

    _log_legacy_db_notice_once(
        f"migrated:{resolved}:{legacy_path}",
        "Migrated legacy Spectra database: %s %s -> %s",
        action,
        legacy_path,
        resolved,
    )
    return resolved


class Settings(BaseSettings):
    """All Spectra settings, loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ── Google Sheets ────────────────────────────────────────────
    google_sheets_credentials_b64: str = ""
    google_sheets_credentials_file: str = "credentials.json"
    spreadsheet_id: str = ""

    # ── Base Currency ────────────────────────────────────────────
    base_currency: str = Field(default="EUR")

    @field_validator("base_currency")
    @classmethod
    def _uppercase_currency(cls, v: str) -> str:
        return v.strip().upper()

    # ── AI Provider ──────────────────────────────────────────────
    ai_provider: Literal["gemini", "openai", "local"] = Field(
        default="gemini",
        validation_alias=AliasChoices("AI_PROVIDER", "AI_PrOVIDER"),
    )

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, v: str) -> str:
        return v.strip().lower() if isinstance(v, str) else v

    gemini_api_key: str = ""
    gemini_model: str = "gemma-3-27b-it"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # ── Database ─────────────────────────────────────────────────
    db_path: Path = Field(default=_PROJECT_ROOT / "data" / "spectra.db")

    # ── Behaviour ────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Validation ───────────────────────────────────────────────
    @model_validator(mode="after")
    def _check_required_secrets(self) -> "Settings":
        """Warn (don't crash) about missing secrets."""
        self.db_path = _prepare_db_path(self.db_path)

        credentials_file = Path(self.google_sheets_credentials_file)
        if not credentials_file.is_absolute():
            credentials_file = (_PROJECT_ROOT / credentials_file).resolve()
            self.google_sheets_credentials_file = str(credentials_file)

        missing: list[str] = []

        if not self.spreadsheet_id:
            missing.append("SPREADSHEET_ID")

        if not self.google_sheets_credentials_b64 and not credentials_file.exists():
            missing.append(
                "GOOGLE_SHEETS_CREDENTIALS_B64 or GOOGLE_SHEETS_CREDENTIALS_FILE"
            )

        if self.ai_provider == "gemini" and not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        elif self.ai_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        # 'local' mode needs no API keys

        if missing:
            logger.warning(
                "Missing secrets (some features may fail): %s", ", ".join(missing)
            )

        return self


def load_settings() -> Settings:
    """Load settings and configure logging."""
    settings = Settings()  # type: ignore[call-arg]

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-12s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(
        "Spectra config loaded (provider=%s, db=%s, env=%s)",
        settings.ai_provider,
        settings.db_path,
        _ENV_FILE,
    )
    return settings
