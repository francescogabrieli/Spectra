"""Centralised configuration — every setting comes from env vars / .env file."""

from __future__ import annotations

import logging
import os
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
        default="local",
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

    # Internal service endpoints. Local defaults work for native multi-process
    # runs; Docker Compose overrides them with service DNS names.
    service_role: str = Field(default="gateway", validation_alias="SPECTRA_SERVICE_ROLE")
    ledger_url: str = "http://127.0.0.1:8081"
    import_service_url: str = "http://127.0.0.1:8082"
    enrichment_url: str = "http://127.0.0.1:8083"
    import_db_path: Path = Field(default=_PROJECT_ROOT / "data" / "imports.sqlite")
    fx_db_path: Path = Field(default=_PROJECT_ROOT / "data" / "fx-rates.sqlite")
    internal_api_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # Uploads are intentionally bounded because statements can contain sensitive
    # data and parsing is performed in a worker thread.
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)
    max_backup_bytes: int = Field(default=512 * 1024 * 1024, gt=0, le=2 * 1024 * 1024 * 1024)

    # ── Behaviour ────────────────────────────────────────────────
    log_level: str = "INFO"

    # ── Validation ───────────────────────────────────────────────
    @model_validator(mode="after")
    def _check_required_secrets(self) -> "Settings":
        """Warn (don't crash) about missing secrets."""
        # Only the ledger owns/migrates the canonical financial database in a
        # service deployment. Other processes never touch the ledger path.
        if self.service_role == "ledger":
            self.db_path = _prepare_db_path(self.db_path)
        elif not self.db_path.is_absolute():
            self.db_path = (_PROJECT_ROOT / self.db_path).resolve()

        for field_name in ("import_db_path", "fx_db_path"):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, (_PROJECT_ROOT / path).resolve())

        credentials_file = Path(self.google_sheets_credentials_file)
        if not credentials_file.is_absolute():
            credentials_file = (_PROJECT_ROOT / credentials_file).resolve()
            self.google_sheets_credentials_file = str(credentials_file)

        missing: list[str] = []
        if self.ai_provider == "gemini" and not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        elif self.ai_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        # 'local' mode needs no API keys

        if missing and self.service_role == "enrichment":
            logger.warning(
                "Missing secrets (some features may fail): %s", ", ".join(missing)
            )

        return self


def load_settings(
    *, service_role: str | None = None, env_file: Path | str | None = None,
) -> Settings:
    """Load settings and configure logging."""
    role = service_role or os.environ.get("SPECTRA_SERVICE_ROLE", "gateway")
    overrides: dict[str, object] = {"service_role": role}
    if role != "enrichment":
        # Explicit empty init values prevent Pydantic from copying provider
        # secrets out of .env or the process environment into unrelated apps.
        overrides.update({"openai_api_key": "", "gemini_api_key": ""})
    if role != "importer":
        overrides.update({
            "spreadsheet_id": "",
            "google_sheets_credentials_b64": "",
            "google_sheets_credentials_file": "credentials.json",
        })
    if env_file is None:
        settings = Settings(**overrides)  # type: ignore[call-arg]
    else:
        settings = Settings(**overrides, _env_file=env_file)  # type: ignore[call-arg]

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-12s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(
        "Spectra config loaded (provider=%s, db=%s, env=%s)",
        settings.ai_provider,
        settings.db_path,
        env_file or _ENV_FILE,
    )
    return settings
